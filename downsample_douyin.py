#!/usr/bin/env python3
"""分层归档抖音时间序列：
  - douyin-raw.json：append-only，全量保留，不降采样
  - douyin.json    ：
      <=3d         全保留
      3d<age<=7d    按 4 小时桶（BJ 00/04/08/12/16/20），保留桶起点之后最近的那条
      >7d          按天桶，保留 BJ 0:00 之后最近的那条

  所有"天/小时"边界均按 UTC+8 计算（与 douyin.html 的 bucketizeFourHourClosest
  / bucketizeDayClosest 一致）。

本次新记录从文件读取（collect_data.yaml 的 "Append to douyin.json" step
会先在 inline Python 里把本条 record 写成 JSON 文件，再调用本脚本）：
  NEW_RECORD_FILE    - 新记录 JSON 文件路径，默认 new_douyin_record.json

文件路径可通过 env 覆盖（默认与 process.py 对齐）：
  DATA_FILE      - 降采样输出文件，默认 douyin.json
  DATA_RAW_FILE  - 全量存档，默认 douyin-raw.json
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))

JSON_FILE = os.environ.get("DATA_FILE", "douyin.json")
RAW_FILE = os.environ.get("DATA_RAW_FILE", "douyin-raw.json")
NEW_RECORD_FILE = os.environ.get("NEW_RECORD_FILE", "new_douyin_record.json")


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


with open(NEW_RECORD_FILE, "r", encoding="utf-8") as f:
    new_record = json.load(f)

# 1) 全量存档：append-only，永不降采样
if Path(RAW_FILE).exists():
    with open(RAW_FILE, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
elif Path(JSON_FILE).exists():
    # 首次切换到双文件：用降采样文件里的历史数据回填，避免数据丢失
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
else:
    raw_data = []
raw_data.append(new_record)
with open(RAW_FILE, "w", encoding="utf-8") as f:
    json.dump(raw_data, f, ensure_ascii=False, indent=2)

# 2) 加载并降采样：始终从 raw_data 出发，保证 douyin.json 与 douyin-raw.json 一致
data = raw_data
if not data:
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump([], f, ensure_ascii=False, indent=2)
    raise SystemExit(0)

# 用"最新一条记录的 ts"作为 now 基准，避免本次刚追加的记录被旧 wall-clock 推到
# 较老的桶里丢失细节（与 process.py 的 now_ts=wall-clock 不同，因为抖音 step 只
# 在 due 时才跑，最新记录的 ts 与实际采集时刻相差很小，可作为基准）。
now_ts = max(r["ts"] for r in data)

sec3d = 3 * 86400
sec7d = 7 * 86400
cut3d = now_ts - sec3d
cut7d = now_ts - sec7d

recent = [r for r in data if r["ts"] >= cut3d]                  # <=3d
mid    = [r for r in data if cut7d <= r["ts"] < cut3d]            # 3d~7d
old    = [r for r in data if r["ts"] < cut7d]                     # >7d

keep = []
keep.extend(recent)

# 3d~7d: 按 BJ 4 小时桶，每桶取桶起点之后 offset 最小者；并列时取 ts 最小
bucket_groups = defaultdict(list)
for rec in mid:
    bucket_groups[bj_4h_bucket_start(rec["ts"])].append(rec)
for lst in bucket_groups.values():
    closest = min(lst, key=lambda r: (r["ts"] - bj_4h_bucket_start(r["ts"]), r["ts"]))
    keep.append(closest)

# >7d: 按 BJ 天桶，每桶取 BJ 0:00 之后 offset 最小者；并列时取 ts 最小
day_groups = defaultdict(list)
for rec in old:
    day_groups[bj_day_start(rec["ts"])].append(rec)
for lst in day_groups.values():
    closest = min(lst, key=lambda r: (r["ts"] - bj_day_start(r["ts"]), r["ts"]))
    keep.append(closest)

keep.sort(key=lambda x: x["ts"])
with open(JSON_FILE, "w", encoding="utf-8") as f:
    json.dump(keep, f, ensure_ascii=False, indent=2)