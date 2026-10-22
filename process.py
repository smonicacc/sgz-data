#!/usr/bin/env python3
"""分层归档时间序列（降采样规则对任意 record 通用）：
  - <RAW_FILE>：append-only，全量保留，不降采样
  - <JSON_FILE>：
      <24h         全保留
      24h~3d       按小时桶，桶末
      3d~7d        按 2 小时桶（双数小时），桶首
      >7d          按天桶，取 BJ 0:00 之后的第一条（前一天与当天分界）

  所有"天/小时"边界均按 UTC+8 计算（与 index.html 一致）

  两种用法（由环境变量区分）：
    1) 预约数据（默认）：DATA_FILE=data.json, DATA_RAW_FILE=data-raw.json
       record = {ts, value[, hot_num]}
    2) 热度专用：DATA_FILE=hot.json, DATA_RAW_FILE=hot-raw.json
       record = {ts, hot_num}（无 value，不再推 qq_video_subscribe）

  另维护一个 HOT_MAX_FILE（每次 record 带 hot_num 时更新）：
    - all_time_max_hot_num / all_time_max_ts：历史上最大的 hot_num
    - today_max_hot_num / today_max_ts / today_bj_date：
        BJ 日期上"当天"最大的 hot_num；BJ 日期跨过 0 点自动重置
  文件不存在时按首次写入初始化；index.html 拉到后会在热度折线图上画两条 markLine

运行时通过环境变量接收本次采集参数：
  RECORD_TS      - 本次记录时间戳（秒）
  RECORD_VAL     - 可选；本次预约数值（热度专用模式不传）
  RECORD_HOT_NUM - 可选；非空整数时附加 hot_num
  两者至少给一个
  DATA_FILE / DATA_RAW_FILE / HOT_MAX_FILE - 可选，覆盖默认文件名
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
HOT_MAX_FILE = os.environ.get("HOT_MAX_FILE", "data-hot-max.json")
now_ts = int(time.time())

new_record = {"ts": int(os.environ["RECORD_TS"])}

# value（预约数）与 hot_num 都是可选字段，两者至少给一个：
#   - 预约流程：RECORD_VAL 必填，RECORD_HOT_NUM 可选
#   - 热度专用流程：只传 RECORD_HOT_NUM，record 就是 {ts, hot_num}
_val_env = os.environ.get("RECORD_VAL", "").strip()
if _val_env:
    new_record["value"] = int(_val_env)

_hot_num_env = os.environ.get("RECORD_HOT_NUM", "").strip()
if _hot_num_env:
    new_record["hot_num"] = int(_hot_num_env)

if "value" not in new_record and "hot_num" not in new_record:
    raise SystemExit("process.py: at least one of RECORD_VAL / RECORD_HOT_NUM is required")

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

# 1.5) 热管理（仅在本次 record 带 hot_num 时更新；无 hot_num 则保持原文件不变）
#  - all_time_max：单值最大，跨 BJ 日期不重置
#  - today_max：以 today_bj_date 键入，跨过 BJ 0 点整文件重置为本次 hot_num
if "hot_num" in new_record:
    current_hot = new_record["hot_num"]
    current_ts = new_record["ts"]
    # 当前 BJ 日期（"当天" 用 UTC+8 日期切分，不跟 now_ts 走避免零点附近漂移）
    bj_date_str = time.strftime("%Y-%m-%d", time.gmtime(current_ts + BJ_OFFSET))

    if Path(HOT_MAX_FILE).exists():
        with open(HOT_MAX_FILE, "r", encoding="utf-8") as f:
            hot_max = json.load(f)
    else:
        hot_max = {}

    if current_hot > hot_max.get("all_time_max_hot_num", -1):
        hot_max["all_time_max_hot_num"] = current_hot
        hot_max["all_time_max_ts"] = current_ts

    if hot_max.get("today_bj_date") != bj_date_str:
        # 跨过 BJ 0 点：当日最高重置为本次采集值
        hot_max["today_max_hot_num"] = current_hot
        hot_max["today_max_ts"] = current_ts
        hot_max["today_bj_date"] = bj_date_str
    elif current_hot > hot_max.get("today_max_hot_num", -1):
        hot_max["today_max_hot_num"] = current_hot
        hot_max["today_max_ts"] = current_ts

    with open(HOT_MAX_FILE, "w", encoding="utf-8") as f:
        json.dump(hot_max, f, ensure_ascii=False, indent=2)

# 2) 加载降采样文件作为降采样基础
if Path(JSON_FILE).exists():
    with open(JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
else:
    data = []
data.append(new_record)

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
# hot_num 跟 record 一起保留在降采样文件里，供 push_to_grafana.py 回填
# qq_video_hot_num；index.html 的预约图只读 {ts, value}，不会把它当作 metric。
with open(JSON_FILE, "w", encoding="utf-8") as f:
    json.dump(keep, f, ensure_ascii=False, indent=2)
