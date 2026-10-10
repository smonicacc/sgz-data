#!/usr/bin/env python3
"""分层归档时间序列：
  - data-raw.json：append-only，全量保留，不降采样；可选带 hot_num
  - data.json    ：
      <24h         全保留
      24h~3d       按小时桶，桶末
      3d~7d        按 2 小时桶（双数小时），桶首
      >7d          按天桶，取 BJ 0:00 之后的第一条（前一天与当天分界）

  所有"天/小时"边界均按 UTC+8 计算（与 index.html 一致）
  hot_num 仅写入 data-raw.json，不会进 data.json

运行时通过环境变量接收本次采集参数：
  RECORD_TS      - 本次记录时间戳（秒）
  RECORD_VAL     - 本次记录数值
  RECORD_HOT_NUM - 可选；非空整数时附加 hot_num（仅 data-raw.json）
"""
import json
import os
import time
from collections import defaultdict
from pathlib import Path

BJ_OFFSET = 8 * 3600  # UTC+8


def bj_day_start_utc(ts):
    """返回 ts 所在北京时间"今日 0 点"的 UTC 秒"""
    bj_ts = ts + BJ_OFFSET
    bj_day_start = (bj_ts // 86400) * 86400
    return bj_day_start - BJ_OFFSET


def bj_hour_start_utc(ts):
    """返回 ts 所在北京时区"本小时整点"的 UTC 秒"""
    bj_ts = ts + BJ_OFFSET
    bj_h = bj_ts // 3600
    return bj_h * 3600 - BJ_OFFSET


def bj_2h_bucket_utc(ts):
    """返回 ts 所在北京时区双数小时桶起点（00,02,04...22）的 UTC 秒"""
    bj_ts = ts + BJ_OFFSET
    bj_h = bj_ts // 3600
    even_h = bj_h - (bj_h % 2)
    return even_h * 3600 - BJ_OFFSET


def bucket_pick(records, mode):
    """mode='first' 取 ts 最小；mode='last' 取 ts 最大"""
    if mode == 'first':
        return min(records, key=lambda r: r["ts"])
    return max(records, key=lambda r: r["ts"])


JSON_FILE = os.environ.get("DATA_FILE", "data.json")
RAW_FILE = os.environ.get("DATA_RAW_FILE", "data-raw.json")
now_ts = int(time.time())

new_record = {
    "ts": int(os.environ["RECORD_TS"]),
    "value": int(os.environ["RECORD_VAL"]),
}

# Optional hot_num: only attached when RECORD_HOT_NUM is set to a non-empty
# integer (collect_data.yaml emits empty string when COLLECT_HOT_NUM=false or
# extraction failed). hot_num is appended to data-raw.json — never written to
# data.json (downsampled view is the canonical "single value" plot).
_hot_num_env = os.environ.get("RECORD_HOT_NUM", "").strip()
if _hot_num_env:
    new_record["hot_num"] = int(_hot_num_env)

# ds_record (no hot_num) for data.json; raw_record (with hot_num) for data-raw.json
ds_record = {"ts": new_record["ts"], "value": new_record["value"]}
raw_record = new_record

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
raw_data.append(raw_record)
with open(RAW_FILE, "w", encoding="utf-8") as f:
    json.dump(raw_data, f, ensure_ascii=False, indent=2)

# 2) 加载降采样文件作为降采样基础
if Path(JSON_FILE).exists():
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
else:
    data = []
data.append(ds_record)

# 时间边界
sec1d = 86400
sec3d = 3 * 86400
sec7d = 7 * 86400
cut1d = now_ts - sec1d
cut3d = now_ts - sec3d
cut7d = now_ts - sec7d

recent_raw = [r for r in data if r["ts"] >= cut1d]                  # <24h
mid_raw    = [r for r in data if cut3d <= r["ts"] < cut1d]            # 24h~3d
mid2_raw   = [r for r in data if cut7d <= r["ts"] < cut3d]            # 3d~7d
old_raw    = [r for r in data if r["ts"] < cut7d]                     # >7d

keep = []
keep.extend(recent_raw)

# 24h~3d：按小时桶，每桶取最接近整小时（HH:00）的那条
hour_groups = defaultdict(list)
for rec in mid_raw:
    hour_groups[bj_hour_start_utc(rec["ts"])].append(rec)
for lst in hour_groups.values():
    # 桶内取到 HH:00（桶起点）最近的那条；并列时取 ts 最小的（更早采集的）
    closest = min(lst, key=lambda r: (
        abs(r["ts"] - bj_hour_start_utc(r["ts"])),
        r["ts"]
    ))
    keep.append(closest)

# 3d~7d：按 2 小时桶（双数小时），每桶取最早一条
bucket_groups = defaultdict(list)
for rec in mid2_raw:
    bucket_groups[bj_2h_bucket_utc(rec["ts"])].append(rec)
for lst in bucket_groups.values():
    keep.append(bucket_pick(lst, 'first'))

# >7d：按天桶，每桶取当天最早一条（BJ 0:00 之后的第一条）
day_groups = defaultdict(list)
for rec in old_raw:
    day_groups[bj_day_start_utc(rec["ts"])].append(rec)
for lst in day_groups.values():
    keep.append(bucket_pick(lst, 'first'))

keep.sort(key=lambda x: x["ts"])
# hot_num 仅属于全量存档（data-raw.json）；data.json 必须不带这个字段，
# 否则下游会把它当作 metric 渲染。顺手清掉旧记录里残留的 hot_num。
for r in keep:
    r.pop("hot_num", None)
with open(JSON_FILE, "w", encoding="utf-8") as f:
    json.dump(keep, f, ensure_ascii=False, indent=2)
