#!/usr/bin/env python3
"""Push metrics to Grafana Cloud Prometheus via Remote Write.

Generic entry point supporting three subcommands, one per data domain:

  qq-video      data.json style {ts, value}  → qq_video_subscribe
  douyin-user   douyin.json style {ts, sgz_*, *_tag_*}
  douyin-posts  user_posts[.history].json

Each subcommand supports both single-timestamp and multi-timestamp modes
(see --help for the exact flags).

Environment variables:
  GRAFANA_REMOTE_WRITE_URL  - Remote Write endpoint（含 /api/prom/push 后缀）
  GRAFANA_USER              - Basic auth 用户名（Grafana Cloud Instance ID）
  GRAFANA_TOKEN             - Basic auth 密码（Grafana Cloud API Token）

依赖：prometheus-remote-writer（pip install prometheus-remote-writer）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Iterable

from prometheus_remote_writer import RemoteWriter


# --------------------------------------------------------------------------- #
# Built-in field/suffix maps and default labels per data domain.
# --------------------------------------------------------------------------- #

# data.json record shape: {"ts": <int>, "value": <int>}
QQ_VIDEO_FIELD_MAP: dict[str, dict[str, Any]] = {
    "value": {"name": "qq_video_subscribe"},
}
QQ_VIDEO_DEFAULT_LABELS: dict[str, str] = {"job": "qq_video_collector"}

# douyin.json record shape:
#   {"ts": <int>, "sgz_follower_count": <int>, "sgz_total_favorited": <int>,
#    "<alias>_douyin_tag_view_count": <int>, "<alias>_douyin_tag_user_count": <int|null>}
DOUYIN_USER_FIELD_MAP: dict[str, dict[str, Any]] = {
    "sgz_follower_count":  {"name": "douyin_user_follower_count",
                            "labels": {"account": "sgz"}},
    "sgz_total_favorited": {"name": "douyin_user_total_favorited",
                            "labels": {"account": "sgz"}},
}
DOUYIN_USER_SUFFIX_MAP: list[tuple[str, str, str]] = [
    # (field_suffix, metric_name, label_key)
    ("_douyin_tag_view_count", "douyin_tag_view_count", "tag"),
    ("_douyin_tag_user_count", "douyin_tag_user_count", "tag"),
]
DOUYIN_USER_DEFAULT_LABELS: dict[str, str] = {"job": "douyin_collector"}

# data/user_posts.json snapshot:
#   {"fetched_at": <int>, "aweme_list": [{"aweme_id": str, "statistics": {...}}, ...]}
# data/user_posts_history.json:
#   {aweme_id: [{"ts": <int>, "digg_count": <int>, "comment_count": <int>,
#                "share_count": <int>}, ...]}
DOUYIN_POSTS_DEFAULT_LABELS: dict[str, str] = {"job": "douyin_user_posts_collector"}
DOUYIN_POSTS_STATS_FIELDS: tuple[str, ...] = (
    "digg_count", "comment_count", "share_count",
    "collect_count", "play_count", "admire_count", "recommend_count",
)
# History snapshots only carry digg/comment/share (see fetch_user_posts.py writer).
DOUYIN_POSTS_HISTORY_STATS_FIELDS: tuple[str, ...] = (
    "digg_count", "comment_count", "share_count",
)


# --------------------------------------------------------------------------- #
# Layer 1 — network
# --------------------------------------------------------------------------- #

def build_writer() -> RemoteWriter:
    """Build a RemoteWriter from GRAFANA_* environment variables."""
    return RemoteWriter(
        url=os.environ["GRAFANA_REMOTE_WRITE_URL"],
        auth={
            "username": os.environ["GRAFANA_USER"],
            "password": os.environ["GRAFANA_TOKEN"],
        },
    )


def push_metrics(writer: RemoteWriter, metrics: list[dict]) -> None:
    """Send a list of metric dicts to Grafana via RemoteWriter.

    Each metric dict: {"name": str, "labels": dict, "value": number,
                       "timestamp_ms": int}.
    """
    if not metrics:
        return
    data = [
        {
            "metric": {"__name__": m["name"], **(m.get("labels") or {})},
            "values": [m["value"]],
            "timestamps": [m["timestamp_ms"]],
        }
        for m in metrics
    ]
    writer.send(data)


# --------------------------------------------------------------------------- #
# Layer 2 — single record / snapshot → list of metric dicts
# --------------------------------------------------------------------------- #

def record_to_metrics(
    record: dict,
    *,
    ts_field: str = "ts",
    ts_ms: int | None = None,
    field_map: dict[str, dict[str, Any]] | None = None,
    suffix_map: list[tuple[str, str, str]] | None = None,
    default_labels: dict[str, str] | None = None,
    skip_fields: set[str] | None = None,
) -> list[dict]:
    """Convert one record dict (with a `ts` field) into a list of metrics.

    Mapping rules (in order, first match wins per field):
      1. field in field_map  → use field_map[field] (name + extra labels)
      2. field ends with any (suffix, name, label_key) in suffix_map
                                → strip suffix, name=<name>, label=<value>
      3. otherwise            → drop

    `ts_ms` overrides the record's timestamp (in milliseconds since epoch).
    Records missing the timestamp field fall back to `int(time.time() * 1000)`.
    """
    if ts_ms is None:
        raw_ts = record.get(ts_field)
        ts_ms = int(raw_ts * 1000) if raw_ts is not None else int(time.time() * 1000)

    default_labels = default_labels or {}
    skip_fields = skip_fields or set()
    metrics: list[dict] = []

    for field, value in record.items():
        if field == ts_field or field in skip_fields or value is None:
            continue

        spec = (field_map or {}).get(field)
        if spec is not None:
            name = spec.get("name", field)
            labels = {**default_labels, **(spec.get("labels") or {})}
            metrics.append({
                "name": name, "labels": labels, "value": value,
                "timestamp_ms": ts_ms,
            })
            continue

        if suffix_map:
            matched = False
            for suffix, name, label_key in suffix_map:
                if field.endswith(suffix) and len(field) > len(suffix):
                    tag = field[: -len(suffix)]
                    labels = {**default_labels, label_key: tag}
                    metrics.append({
                        "name": name, "labels": labels, "value": value,
                        "timestamp_ms": ts_ms,
                    })
                    matched = True
                    break
            if matched:
                continue

        # No mapping found — skip. Make this explicit (no silent use of field
        # name as metric name) to avoid typos like `sgz_folloer_count` silently
        # creating an unintended metric.

    return metrics


def snapshot_per_aweme_metrics(
    snapshot: dict,
    *,
    ts_field: str = "fetched_at",
    default_labels: dict[str, str] | None = None,
    stats_fields: tuple[str, ...] = DOUYIN_POSTS_STATS_FIELDS,
    aweme_id_field: str = "aweme_id",
    stats_container: str = "statistics",
) -> list[dict]:
    """Convert a user_posts snapshot into per-aweme metrics.

    snapshot["aweme_list"] is a list of items; for each item we emit one
    metric per non-None `statistics[<stats_field>]` value. Every metric gets
    an `aweme_id` label. Items without `aweme_id` are skipped.
    """
    raw_ts = snapshot.get(ts_field)
    ts_ms = int(raw_ts * 1000) if raw_ts is not None else int(time.time() * 1000)

    default_labels = default_labels or {}
    metrics: list[dict] = []

    for item in snapshot.get("aweme_list") or []:
        aweme_id = item.get(aweme_id_field)
        if not aweme_id:
            continue
        stats = item.get(stats_container) or {}
        for sf in stats_fields:
            v = stats.get(sf)
            if v is None:
                continue
            # metric name = douyin_user_post_<stem>, drop "_count" suffix
            stem = sf[:-len("_count")] if sf.endswith("_count") else sf
            name = f"douyin_user_post_{stem}"
            labels = {**default_labels, "aweme_id": aweme_id}
            metrics.append({
                "name": name, "labels": labels, "value": v,
                "timestamp_ms": ts_ms,
            })

    return metrics


# --------------------------------------------------------------------------- #
# Layer 3 — multi-record / multi-snapshot → list of metric dicts
# --------------------------------------------------------------------------- #

def _filter_by_age(
    items: Iterable[dict],
    ts_key: str,
    max_age_days: int | None,
    *,
    now_ts: int | None = None,
) -> list[dict]:
    """Drop items whose `ts_key` value is older than `max_age_days` days.

    Items missing `ts_key` are kept (we don't silently drop them — only the
    age filter applies). If `max_age_days` is None, return the list as-is.
    """
    if max_age_days is None:
        return list(items)
    now = now_ts if now_ts is not None else int(time.time())
    cutoff = now - max_age_days * 86400
    out: list[dict] = []
    for it in items:
        ts = it.get(ts_key)
        if ts is None or ts >= cutoff:
            out.append(it)
    return out


def records_to_metrics(
    records: Iterable[dict],
    *,
    ts_field: str = "ts",
    field_map: dict[str, dict[str, Any]] | None = None,
    suffix_map: list[tuple[str, str, str]] | None = None,
    default_labels: dict[str, str] | None = None,
    skip_fields: set[str] | None = None,
) -> list[dict]:
    """Apply record_to_metrics to each record and concatenate results."""
    out: list[dict] = []
    for rec in records:
        out.extend(record_to_metrics(
            rec,
            ts_field=ts_field,
            field_map=field_map,
            suffix_map=suffix_map,
            default_labels=default_labels,
            skip_fields=skip_fields,
        ))
    return out


def history_to_metrics(
    history: dict,                 # {aweme_id: [snapshot, ...]}
    *,
    default_labels: dict[str, str] | None = None,
    stats_fields: tuple[str, ...] = DOUYIN_POSTS_HISTORY_STATS_FIELDS,
) -> list[dict]:
    """Push per-aweme metrics for every snapshot in a history dict.

    Each snapshot uses its own `ts` (not fetched_at) as the timestamp.
    Each metric is labeled with `aweme_id` (the history key).

    Note: snapshot dicts in `user_posts_history.json` already carry aweme_id
    conceptually, but the outer dict keys are the source of truth — we read
    `aweme_id` from the outer key, not the snapshot body (which doesn't have
    one in the history file format).
    """
    default_labels = default_labels or {}
    metrics: list[dict] = []

    for aweme_id, snapshots in history.items():
        if not aweme_id:
            continue
        for snap in snapshots or []:
            raw_ts = snap.get("ts")
            ts_ms = int(raw_ts * 1000) if raw_ts is not None else int(time.time() * 1000)
            for sf in stats_fields:
                v = snap.get(sf)
                if v is None:
                    continue
                stem = sf[:-len("_count")] if sf.endswith("_count") else sf
                name = f"douyin_user_post_{stem}"
                labels = {**default_labels, "aweme_id": aweme_id}
                metrics.append({
                    "name": name, "labels": labels, "value": v,
                    "timestamp_ms": ts_ms,
                })

    return metrics


# --------------------------------------------------------------------------- #
# Layer 4 — CLI subcommands
# --------------------------------------------------------------------------- #

def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _emit_or_send(metrics: list[dict], dry_run: bool, print_only: bool) -> int:
    """Print metrics (dry-run/print-only) or send to Grafana. Returns 0/1."""
    if dry_run or print_only:
        json.dump(metrics, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        if not print_only:
            return 0
    if dry_run:
        return 0
    writer = build_writer()
    push_metrics(writer, metrics)
    print(f"Pushed {len(metrics)} metrics", file=sys.stderr)
    return 0


def _add_global_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true",
                   help="打印待发送的 metrics JSON 到 stdout，不调网络")
    p.add_argument("--print-only", action="store_true",
                   help="同上，但保留 stderr 进度日志；默认静默")
    p.add_argument("--max-age-days", type=int, default=None, metavar="N",
                   help="跳过 ts 早于 (now - N 天) 的 record/snapshot。"
                        "Grafana Cloud Mimir 默认拒收太老的样本（err-mimir-sample-"
                        "timestamp-too-old），回填时通常需要这个参数。"
                        "不指定则不过滤。")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Push metrics to Grafana Cloud Prometheus (generic).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<subcommand>")

    # ---- qq-video --------------------------------------------------------- #
    p_qq = sub.add_parser(
        "qq-video",
        help="QQ 视频单点 → qq_video_subscribe；或 data.json 全量回填",
    )
    qq_mode = p_qq.add_mutually_exclusive_group(required=True)
    qq_mode.add_argument("--val", type=int,
                         help="单点模式：直接推送一个整数")
    qq_mode.add_argument("--records-file", type=str,
                         help="多点模式：读 JSON 数组，每条 {ts, value} 都推一次")
    _add_global_flags(p_qq)

    # ---- douyin-user ------------------------------------------------------ #
    p_du = sub.add_parser(
        "douyin-user",
        help="douyin.json 风格 record → 用户/话题指标",
    )
    p_du.add_argument("--record-file", type=str, required=True,
                      help="JSON 文件：单条 record（配 --last 用）或 record 数组")
    p_du.add_argument("--last", action="store_true",
                      help="只取 record-file 数组的最后一条（单点模式）")
    _add_global_flags(p_du)

    # ---- douyin-posts ----------------------------------------------------- #
    p_dp = sub.add_parser(
        "douyin-posts",
        help="data/user_posts[.history].json → per-aweme 指标",
    )
    dp_mode = p_dp.add_mutually_exclusive_group(required=True)
    dp_mode.add_argument("--snapshot-file", type=str,
                         help="单点模式：data/user_posts.json 快照，"
                              "用 fetched_at 作时间戳")
    dp_mode.add_argument("--history-file", type=str,
                         help="多点模式：data/user_posts_history.json，"
                              "每个 (aweme_id, snapshot) 都推一次")
    _add_global_flags(p_dp)

    return p


# ---- qq-video ------------------------------------------------------------- #

def cmd_qq_video(args: argparse.Namespace) -> int:
    if args.val is not None:
        ts_ms = int(time.time() * 1000)
        metrics = [{
            "name": "qq_video_subscribe",
            "labels": dict(QQ_VIDEO_DEFAULT_LABELS),
            "value": args.val,
            "timestamp_ms": ts_ms,
        }]
        return _emit_or_send(metrics, args.dry_run, args.print_only)

    # --records-file
    records = _load_json(args.records_file)
    if not isinstance(records, list):
        print(f"qq-video --records-file expects a JSON array, got {type(records).__name__}",
              file=sys.stderr)
        return 2
    before = len(records)
    records = _filter_by_age(records, "ts", args.max_age_days)
    if args.max_age_days is not None and len(records) != before:
        print(f"[qq-video] age filter kept {len(records)}/{before} records "
              f"(cutoff: last {args.max_age_days} days)",
              file=sys.stderr)
    metrics = records_to_metrics(
        records,
        field_map=QQ_VIDEO_FIELD_MAP,
        default_labels=QQ_VIDEO_DEFAULT_LABELS,
    )
    return _emit_or_send(metrics, args.dry_run, args.print_only)


# ---- douyin-user ---------------------------------------------------------- #

def cmd_douyin_user(args: argparse.Namespace) -> int:
    data = _load_json(args.record_file)
    if args.last:
        if not isinstance(data, list) or not data:
            print("douyin-user --last expects a non-empty JSON array",
                  file=sys.stderr)
            return 2
        records = [data[-1]]
    else:
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = [data]
        else:
            print(f"douyin-user --record-file expects JSON object or array, "
                  f"got {type(data).__name__}", file=sys.stderr)
            return 2

    before = len(records)
    records = _filter_by_age(records, "ts", args.max_age_days)
    if args.max_age_days is not None and len(records) != before:
        print(f"[douyin-user] age filter kept {len(records)}/{before} records "
              f"(cutoff: last {args.max_age_days} days)",
              file=sys.stderr)

    metrics = records_to_metrics(
        records,
        field_map=DOUYIN_USER_FIELD_MAP,
        suffix_map=DOUYIN_USER_SUFFIX_MAP,
        default_labels=DOUYIN_USER_DEFAULT_LABELS,
    )
    return _emit_or_send(metrics, args.dry_run, args.print_only)


# ---- douyin-posts --------------------------------------------------------- #

def cmd_douyin_posts(args: argparse.Namespace) -> int:
    if args.snapshot_file:
        snapshot = _load_json(args.snapshot_file)
        if not isinstance(snapshot, dict):
            print(f"douyin-posts --snapshot-file expects a JSON object, "
                  f"got {type(snapshot).__name__}", file=sys.stderr)
            return 2
        if args.max_age_days is not None:
            fetched_at = snapshot.get("fetched_at")
            if fetched_at is not None:
                cutoff = int(time.time()) - args.max_age_days * 86400
                if fetched_at < cutoff:
                    print(
                        f"[douyin-posts] snapshot fetched_at={fetched_at} is "
                        f"older than {args.max_age_days} days; skipping",
                        file=sys.stderr,
                    )
                    return 0
        metrics = snapshot_per_aweme_metrics(
            snapshot,
            default_labels=DOUYIN_POSTS_DEFAULT_LABELS,
            stats_fields=DOUYIN_POSTS_STATS_FIELDS,
        )
        return _emit_or_send(metrics, args.dry_run, args.print_only)

    # --history-file
    history = _load_json(args.history_file)
    if not isinstance(history, dict):
        print(f"douyin-posts --history-file expects a JSON object, "
              f"got {type(history).__name__}", file=sys.stderr)
        return 2
    if args.max_age_days is not None:
        cutoff = int(time.time()) - args.max_age_days * 86400
        kept_aweme = 0
        kept_snaps = 0
        for aweme_id, snaps in list(history.items()):
            kept = [s for s in (snaps or []) if s.get("ts", 0) >= cutoff]
            if kept:
                history[aweme_id] = kept
                kept_aweme += 1
                kept_snaps += len(kept)
            else:
                del history[aweme_id]
        print(
            f"[douyin-posts] age filter kept {kept_snaps} snapshots across "
            f"{kept_aweme} aweme_ids (cutoff: last {args.max_age_days} days)",
            file=sys.stderr,
        )
    metrics = history_to_metrics(
        history,
        default_labels=DOUYIN_POSTS_DEFAULT_LABELS,
        stats_fields=DOUYIN_POSTS_HISTORY_STATS_FIELDS,
    )
    return _emit_or_send(metrics, args.dry_run, args.print_only)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "qq-video":
        return cmd_qq_video(args)
    if args.cmd == "douyin-user":
        return cmd_douyin_user(args)
    if args.cmd == "douyin-posts":
        return cmd_douyin_posts(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())