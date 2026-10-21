#!/usr/bin/env python3
"""分层归档小红书话题阅读量（与 downsample_douyin.py 同一套策略）：
  - xhs_topic-raw.json：append-only，全量保留，不降采样
  - xhs_topic.json    ：
      <=3d         全保留（约 1 小时粒度）
      3d<age<=7d    按 4 小时桶（BJ 00/04/08/12/16/20），保留桶起点之后最近的那条
      >7d          按天桶，保留 BJ 0:00 之后最近的那条

  所有"天/小时"边界均按 UTC+8 计算（与 douyin.html 的 bucketizeFourHourClosest
  / bucketizeDayClosest 一致）。3d / 7d / 天的分桶粒度正好对应页面 3d → 1h、
  7d → 4h、14d 及以上 → 1d 的折线渲染，因此降采样不会损失图表可见分辨率。

  记录按整条原样复制，不关心字段名：现有的 9 个 *_tag_view_num 以及以后新增
  的字段都会自动透传。

xhs_topic.json 只基于它自己的现有内容 + 本次新记录做增量降采样，**永不从
xhs_topic-raw.json 重建**：手工回填到 json 的历史不会被采集覆盖掉。
xhs_topic-raw.json 单独 append-only 全量存档，两者互不覆盖。

本次新记录从文件读取（与 downsample_douyin.py 相同）：
  NEW_RECORD_FILE    - 新记录 JSON 文件路径，默认 new_xhs_topic_record.json

文件路径可通过 env 覆盖：
  DATA_FILE      - 降采样输出文件，默认 xhs_topic.json
  DATA_RAW_FILE  - 全量存档，默认 xhs_topic-raw.json
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))

JSON_FILE = Path(os.environ.get("DATA_FILE", "xhs_topic.json"))
RAW_FILE = Path(os.environ.get("DATA_RAW_FILE", "xhs_topic-raw.json"))
NEW_RECORD_FILE = Path(os.environ.get("NEW_RECORD_FILE", "new_xhs_topic_record.json"))

SEC3D = 3 * 86400
SEC7D = 7 * 86400


def bj_day_start(ts: int) -> int:
    """返回 ts 所在北京时间"今日 0 点"的 UTC 秒"""
    dt = datetime.fromtimestamp(ts, tz=BJ)
    midnight_bj = datetime(dt.year, dt.month, dt.day, tzinfo=BJ)
    return int(midnight_bj.timestamp())


def bj_4h_bucket_start(ts: int) -> int:
    """返回 ts 所在北京时区 4 小时桶起点（00/04/08/12/16/20）的 UTC 秒"""
    dt = datetime.fromtimestamp(ts, tz=BJ)
    bucket_hour = (dt.hour // 4) * 4  # 0, 4, 8, 12, 16, 20
    start = datetime(dt.year, dt.month, dt.day, bucket_hour, tzinfo=BJ)
    return int(start.timestamp())


def downsample(data: list, now_ts: int | None = None) -> list:
    """按记录年龄分层保留；返回按 ts 升序排列的新列表。"""
    if not data:
        return []
    if now_ts is None:
        # 用"最新一条记录的 ts"作为 now 基准，避免工作流延迟执行时把刚追加的
        # 记录推进较老的桶里丢失细节（与 downsample_douyin.py 一致）。
        now_ts = max(r["ts"] for r in data)

    cut3d = now_ts - SEC3D
    cut7d = now_ts - SEC7D

    recent = [r for r in data if r["ts"] >= cut3d]                  # <=3d
    mid = [r for r in data if cut7d <= r["ts"] < cut3d]             # 3d~7d
    old = [r for r in data if r["ts"] < cut7d]                      # >7d

    keep = list(recent)

    # 3d~7d: 按 BJ 4 小时桶，每桶取桶起点之后 offset 最小者；并列时取 ts 最小
    bucket_groups = defaultdict(list)
    for rec in mid:
        bucket_groups[bj_4h_bucket_start(rec["ts"])].append(rec)
    for lst in bucket_groups.values():
        keep.append(
            min(lst, key=lambda r: (r["ts"] - bj_4h_bucket_start(r["ts"]), r["ts"]))
        )

    # >7d: 按 BJ 天桶，每桶取 BJ 0:00 之后 offset 最小者；并列时取 ts 最小
    day_groups = defaultdict(list)
    for rec in old:
        day_groups[bj_day_start(rec["ts"])].append(rec)
    for lst in day_groups.values():
        keep.append(min(lst, key=lambda r: (r["ts"] - bj_day_start(r["ts"]), r["ts"])))

    keep.sort(key=lambda x: x["ts"])
    return keep


def _load(path: Path, default: list) -> list:
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"{path} is not a JSON list (got {type(data).__name__})")
    return data


def _write(path: Path, data: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_and_downsample(
    record: dict,
    json_file: Path = JSON_FILE,
    raw_file: Path = RAW_FILE,
) -> None:
    """把 record 追加到 raw 全量存档，并对 json 做增量降采样。"""
    json_file = Path(json_file)
    raw_file = Path(raw_file)

    # 1) 全量存档：append-only，永不降采样
    if raw_file.exists():
        raw_data = _load(raw_file, [])
    elif json_file.exists():
        # 首次切换到双文件：用降采样文件里的历史数据回填，避免数据丢失
        raw_data = _load(json_file, [])
    else:
        raw_data = []
    raw_data.append(record)
    _write(raw_file, raw_data)

    # 2) 降采样：数据源是 json 自己 + 本次新记录，绝不从 raw 重建。
    #    这样手工回填进 json 的历史不会被 raw 覆盖；json 不存在时（首次生成）
    #    才用完整存档 raw_data（已含新记录）初始化。
    if json_file.exists():
        data = _load(json_file, [])
        data.append(record)
    else:
        data = list(raw_data)

    _write(json_file, downsample(data))


def main() -> int:
    with open(NEW_RECORD_FILE, "r", encoding="utf-8") as f:
        new_record = json.load(f)
    append_and_downsample(new_record, JSON_FILE, RAW_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
