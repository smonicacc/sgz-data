#!/usr/bin/env python3
"""Downsample data/user_posts_history.json.

Rule:
  - Records within the last 7 days (Beijing time, UTC+8) are kept as-is.
  - Records older than 7 days are grouped by Beijing calendar day; for each
    day, only the record that has passed BJ midnight (00:00 +08:00) and is
    closest to midnight is kept.

Usage:
  python3 downsample_user_posts.py
  python3 downsample_user_posts.py <input> <output>
"""
import json
import sys
from datetime import datetime, timedelta, timezone

BJ = timezone(timedelta(hours=8))


def bj_day_start_ts(ts: int) -> int:
    dt_bj = datetime.fromtimestamp(ts, tz=BJ)
    midnight_bj = datetime(dt_bj.year, dt_bj.month, dt_bj.day, tzinfo=BJ)
    return int(midnight_bj.timestamp())


def downsample(records: list, cutoff_ts: int) -> list:
    recent = [r for r in records if r["ts"] >= cutoff_ts]
    older = [r for r in records if r["ts"] < cutoff_ts]

    best = {}
    for rec in older:
        ds = bj_day_start_ts(rec["ts"])
        offset = rec["ts"] - ds
        if offset < 0:
            continue
        if ds not in best or offset < best[ds][1]:
            best[ds] = (rec, offset)

    older_kept = [v[0] for v in best.values()]
    merged = recent + older_kept
    merged.sort(key=lambda r: r["ts"], reverse=True)
    return merged


def main() -> None:
    in_path = sys.argv[1] if len(sys.argv) > 1 else "data/user_posts_history.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else in_path

    with open(in_path) as f:
        data = json.load(f)

    today_bj = datetime.now(BJ).date()
    cutoff_bj = datetime(today_bj.year, today_bj.month, today_bj.day, tzinfo=BJ) - timedelta(days=7)
    cutoff_ts = int(cutoff_bj.timestamp())

    print(f"cutoff (BJ): {cutoff_bj}  ts: {cutoff_ts}")

    result = {k: downsample(arr, cutoff_ts) for k, arr in data.items()}

    total_before = sum(len(v) for v in data.values())
    total_after = sum(len(v) for v in result.values())
    print(f"total: {total_before} -> {total_after}")

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"written: {out_path}")


if __name__ == "__main__":
    main()