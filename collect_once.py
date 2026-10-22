#!/usr/bin/env python3
"""一次跑完全部采集（等价于 .github/workflows/collect_hot_data.yaml 的流水线）。

默认只抓取并打印，**不写任何 JSON 文件**；加 `--write` 才会归档落盘。

采集内容：
  1. 腾讯视频热度：hot_num + attent_num → data.json/data-raw.json、
     hot.json/hot-raw.json，并更新 data-hot-max.json（当日 / 历史最高）
  2. 抖音创作者 profile：尚公主抖音号粉丝数、获赞总数
  3. 抖音话题：9 个话题的播放数（viewCount）/ 参与人数（userCount）
  4. 小红书话题：9 个话题的浏览数（view_num）

凭据从环境变量或 `.env` 读取（`.env` 已在 .gitignore 中）：
  DOUYIN_PROFILE_URL / DOUYIN_PROFILE_COOKIE  - 抖音号粉丝 / 点赞
  XHS_COOKIE                                  - 小红书话题
  QQ_VID / QQ_CID                             - 腾讯视频（有默认值）

macOS 自带 Python 常见 CERTIFICATE_VERIFY_FAILED，可加 `--insecure`
（等价于 DY_SSL_VERIFY=0 / XHS_SSL_VERIFY=0 / QQ_SSL_VERIFY=0）。

用法：
  python3 collect_once.py                 # 只抓取 + 打印，不写任何文件（默认）
  python3 collect_once.py --write         # 额外把结果归档进各类 JSON
  python3 collect_once.py --skip-xhs      # 跳过小红书
  python3 collect_once.py --skip-douyin   # 跳过抖音（含 profile）
  python3 collect_once.py --skip-qq       # 跳过腾讯视频
"""
import argparse
import json
import os
import random
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BJ_OFFSET = 8 * 3600

DEFAULT_VID = "l4101w6a4vt"
DEFAULT_CID = "mzc002001w361jz"
QQ_ENDPOINT = (
    "https://pbaccess.video.qq.com/trpc.vector_layout.page_view.PageService/getPage"
    "?vdevice_guid=74145a20676415e3&video_appid=3000010"
    "&vversion_name=8.5.96&vversion_platform=2"
)

# (HID, 中文话题名, alias) —— 与 workflow 的 PAIRS 保持一致
HASHTAGS = [
    ("1785612028243012", "昀牵孟绕", "yqmr"),
    ("1681434850032712", "尚公主", "sgz"),
    ("1699801670926349", "扶摇直尚", "fyzs"),
    ("1786447903179972", "孟子义李昀锐", "mzylyr"),
    ("1788234355891339", "李昀锐孟子义", "lyrmzy"),
    ("1590105432134660", "孟子义", "mzy"),
    ("1630071429441543", "李昀锐", "lyr"),
    ("1726555098811470", "暮晚摇", "mwy"),
    ("1720768196728832", "言尚", "ys"),
]

# 用户点名关注的话题（输出摘要时按这个顺序展示）
WANTED = [
    ("sgz", "尚公主"),
    ("mwy", "暮晚摇"),
    ("ys", "言尚"),
    ("fyzs", "扶摇直尚"),
    ("yqmr", "昀牵孟绕"),
    ("mzy", "孟子义"),
    ("lyr", "李昀锐"),
]

XHS_KEYS = {
    "sgz": "尚公主",
    "mwy": "暮晚摇",
    "ys": "言尚",
    "fyzs": "扶摇直尚",
    "yqmr": "昀牵孟绕",
    "mzy": "孟子义",
    "lyr": "李昀锐",
}


def load_env_file(path: Path) -> None:
    """读取 KEY=VALUE 形式的 .env；不覆盖已存在的环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _ssl_context(verify_env: str):
    if os.environ.get(verify_env, "1") == "0":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _http_json(url: str, headers: dict, timeout: int, ctx) -> dict:
    req = urllib.request.Request(url, headers=headers)
    kwargs = {"timeout": timeout}
    if ctx is not None:
        kwargs["context"] = ctx
    with urllib.request.urlopen(req, **kwargs) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------
# 腾讯视频：attent_num + hot_num
# --------------------------------------------------------------------------
def fetch_qq(vid: str, cid: str, timeout: int) -> dict:
    body = {
        "page_params": {
            "ad_wechat_authorization_status": "0",
            "req_from": "web_vsite",
            "ad_exp_ids": "",
            "pc_sdk_version": "",
            "pc_oaid": "",
            "new_mark_label_enabled": "1",
            "pc_device_info": "",
            "support_pc_yyyb_mobile_app_engine": "0",
            "pc_wegame_version": "",
            "cid": cid,
            "history_vid": "",
            "vid": vid,
            "is_pc_new_detail_page": "0",
            "is_from_web_flyflow": "1",
            "lid": "",
        },
        "page_bypass_params": {
            "params": {"caller_id": "3000010", "platform_id": "2"},
            "scene": "desk_detail",
            "app_version": "",
            "abtest_bypass_id": "74145a20676415e3",
        },
        "page_context": {},
    }
    req = urllib.request.Request(
        QQ_ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Referer": "https://v.qq.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    kwargs = {"timeout": timeout}
    ctx = _ssl_context("QQ_SSL_VERIFY")
    if ctx is not None:
        kwargs["context"] = ctx
    with urllib.request.urlopen(req, **kwargs) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    cards = ((payload.get("data") or {}).get("CardList")) or []
    attent_num = None
    hot_num = None
    for card in cards:
        if not isinstance(card, dict):
            continue
        try:
            params = card["children_list"]["0"]["cards"][0]["params"]
        except (KeyError, TypeError, IndexError):
            continue
        if card.get("type") == "pc_detail_attent_banner" and params.get("attent_num") is not None:
            attent_num = int(params["attent_num"])
        elif card.get("type") == "pc_introduction" and params.get("hot_num") is not None:
            hot_num = int(params["hot_num"])

    if attent_num is None and hot_num is None:
        raise RuntimeError(f"响应里没有 attent_num / hot_num（cards={len(cards)}）")
    return {"attent_num": attent_num, "hot_num": hot_num}


def archive_qq(ts: int, qq: dict) -> None:
    """复用 process.py 的分层归档：data 系列 + hot 系列都写，并更新热管理文件。"""
    if qq.get("attent_num") is None and qq.get("hot_num") is None:
        return
    base = {
        "RECORD_TS": str(ts),
        "RECORD_VAL": "" if qq.get("attent_num") is None else str(qq["attent_num"]),
        "RECORD_HOT_NUM": "" if qq.get("hot_num") is None else str(qq["hot_num"]),
    }
    targets = []
    if qq.get("attent_num") is not None:
        targets.append(("data.json", "data-raw.json", True))
    if qq.get("hot_num") is not None:
        # hot.json 的 record 约定是 {ts, hot_num}，不带 value（见 process.py 注释）
        targets.append(("hot.json", "hot-raw.json", False))
    for data_file, raw_file, include_val in targets:
        env = os.environ.copy()
        env.update(base)
        if not include_val:
            env["RECORD_VAL"] = ""
        env["DATA_FILE"] = data_file
        env["DATA_RAW_FILE"] = raw_file
        _run([sys.executable, "process.py"], env=env)


# --------------------------------------------------------------------------
# 抖音
# --------------------------------------------------------------------------
def fetch_profile(url: str, cookie: str, timeout: int) -> dict:
    payload = _http_json(
        url,
        {
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.douyin.com",
            "referer": "https://www.douyin.com/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
            "cookie": cookie,
        },
        timeout,
        _ssl_context("DY_SSL_VERIFY"),
    )
    user = payload.get("user") or {}
    follower = user.get("follower_count")
    favorited = user.get("total_favorited")
    if follower is None or favorited is None:
        raise RuntimeError("profile 响应缺少 follower_count / total_favorited")
    return {"follower_count": int(follower), "total_favorited": int(favorited)}


def fetch_hashtag(hid: str, tag_name: str, timeout: int, attempts: int = 4) -> dict:
    env = os.environ.copy()
    env.update({"HID": hid, "TAG_NAME": tag_name, "TIMEOUT": str(timeout)})
    # CI 出口 IP 容易撞风控页；有 profile cookie 时交替带/不带重试，
    # 避免"带 cookie 被拦"和"不带 cookie 被拦"两种情况同时卡死。
    cookie = os.environ.get("DOUYIN_PROFILE_COOKIE", "").strip()
    last_err = None
    for attempt in range(attempts):
        if cookie and attempt % 2 == 0:
            env["COOKIE"] = cookie
        else:
            env.pop("COOKIE", None)
        try:
            proc = _run(
                [sys.executable, "fetch_douyin.py"],
                env=env,
                capture_output=True,
            )
            lines = (proc.stdout or "").strip().splitlines()
            if not lines:
                raise RuntimeError("fetch_douyin.py 没有输出")
            data = json.loads(lines[-1])
            # 偶发反爬/SSR 半渲染会返回 0；话题数据不可能为 0，重试而不是写脏数据。
            if int(data.get("viewCount") or 0) > 0 and data.get("userCount") is not None:
                return data
            last_err = f"无效数据 {data}"
        except (RuntimeError, json.JSONDecodeError) as e:
            # NOT_FOUND / TLS / 超时等一律重试：CI 出口 IP 更容易撞上抖音风控页。
            last_err = str(e)

        if attempt + 1 < attempts:
            delay = min(2 ** attempt, 8) + random.uniform(0, 1)
            print(
                f"[douyin] {tag_name} 第 {attempt + 1}/{attempts} 次失败"
                f"（{last_err}），{delay:.1f}s 后重试",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise RuntimeError(f"{tag_name} 连续 {attempts} 次失败，最后一次：{last_err}")


def archive_douyin(ts: int, profile: dict | None, tags: dict) -> dict:
    record = {"ts": ts}
    if profile:
        record["sgz_follower_count"] = profile["follower_count"]
        record["sgz_total_favorited"] = profile["total_favorited"]
    for alias, fetched in tags.items():
        record[f"{alias}_douyin_tag_view_count"] = int(fetched["viewCount"])
        user_count = fetched.get("userCount")
        record[f"{alias}_douyin_tag_user_count"] = (
            int(user_count) if user_count is not None else None
        )
    new_record = ROOT / "new_douyin_record.json"
    new_record.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    _run([sys.executable, "downsample_douyin.py"])
    return record


# --------------------------------------------------------------------------
# 小红书
# --------------------------------------------------------------------------
def fetch_xhs(timeout: int, dry_run: bool = False) -> dict:
    env = os.environ.copy()
    env["TIMEOUT"] = str(timeout)
    env["XHS_DRY_RUN"] = "1" if dry_run else "0"
    proc = _run([sys.executable, "fetch_xhs_topic.py"], env=env, capture_output=True)
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        raise RuntimeError("fetch_xhs_topic.py 没有输出")
    return json.loads(lines[-1])


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _run(cmd, env=None, capture_output=False):
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=capture_output,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        # 只显示脚本名，避免 CI 日志里出现一长串 interpreter 绝对路径
        script = next(
            (Path(c).name for c in cmd[1:] if str(c).endswith(".py")),
            Path(cmd[0]).name,
        )
        raise RuntimeError(f"{script} 失败（exit {proc.returncode}）: {detail[-800:]}")
    return proc


def _bj_time(ts: int | None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts + BJ_OFFSET))


def _fmt_num(value) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def read_today_max(dry_run_hot: int | None) -> dict:
    path = ROOT / "data-hot-max.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    today_bj = time.strftime("%Y-%m-%d", time.gmtime(time.time() + BJ_OFFSET))
    if data.get("today_bj_date") != today_bj:
        # 跨过 BJ 0 点或首次采集：当日值以本次采集为准
        if dry_run_hot is not None:
            data = {
                "today_max_hot_num": dry_run_hot,
                "today_max_ts": int(time.time()),
                "today_bj_date": today_bj,
            }
    elif dry_run_hot is not None and dry_run_hot > data.get("today_max_hot_num", -1):
        data = dict(data)
        data["today_max_hot_num"] = dry_run_hot
        data["today_max_ts"] = int(time.time())
        data["today_bj_date"] = today_bj
    return data


def print_summary(ts, qq, profile, tags, xhs, persist) -> None:
    print()
    print("=" * 72)
    print(f"采集时间：{_bj_time(ts)}（北京时间）")
    print("=" * 72)

    print("\n抖音话题（播放数 / 参与人数）")
    for alias, name in WANTED:
        fetched = tags.get(alias)
        if not fetched:
            print(f"  {name:<6}  （未采集）")
            continue
        print(
            f"  {name:<6}  播放 {_fmt_num(fetched.get('viewCount'))}"
            f"    参与 {_fmt_num(fetched.get('userCount'))}"
        )

    print("\n小红书话题浏览数")
    for alias, name in WANTED:
        key = f"{alias}_tag_view_num"
        print(f"  {name:<6}  {_fmt_num((xhs or {}).get(key))}")

    print("\n尚公主抖音号")
    if profile:
        print(f"  粉丝数   {_fmt_num(profile['follower_count'])}")
        print(f"  获赞总数 {_fmt_num(profile['total_favorited'])}")
    else:
        print("  粉丝数 / 获赞总数  （未采集：缺少 DOUYIN_PROFILE_URL / DOUYIN_PROFILE_COOKIE）")

    hot_max = read_today_max(qq.get("hot_num") if qq else None)
    print("\n腾讯视频热度")
    print(f"  当日最高热度    {_fmt_num(hot_max.get('today_max_hot_num'))}"
          f"  （{_bj_time(hot_max.get('today_max_ts'))}）")
    if not persist:
        print("\n[只打印] 未写入任何数据文件。")


def main() -> int:
    parser = argparse.ArgumentParser(description="一次采集腾讯视频 + 抖音 + 小红书数据")
    parser.add_argument("--env-file", default=".env", help="凭据文件（默认 .env）")
    parser.add_argument(
        "--write",
        action="store_true",
        help="把采集结果写入各类 JSON（默认只打印，不写文件）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="兼容旧参数：只打印、不写文件（现在也是默认行为）",
    )
    parser.add_argument("--skip-qq", action="store_true", help="跳过腾讯视频")
    parser.add_argument("--skip-douyin", action="store_true", help="跳过抖音（含 profile）")
    parser.add_argument("--skip-profile", action="store_true", help="跳过抖音号粉丝 / 点赞")
    parser.add_argument("--skip-xhs", action="store_true", help="跳过小红书")
    parser.add_argument("--insecure", action="store_true", help="关闭 TLS 校验（macOS 证书问题）")
    parser.add_argument("--timeout", type=int, default=30, help="单请求超时秒数（默认 30）")
    args = parser.parse_args()

    load_env_file(ROOT / args.env_file)
    if args.insecure:
        os.environ["DY_SSL_VERIFY"] = "0"
        os.environ["XHS_SSL_VERIFY"] = "0"
        os.environ["QQ_SSL_VERIFY"] = "0"

    # 默认只打印，不碰任何 JSON；--write 才归档。
    persist = args.write and not args.dry_run
    ts = int(time.time())
    errors = []

    qq = None
    if not args.skip_qq:
        try:
            qq = fetch_qq(
                os.environ.get("QQ_VID", DEFAULT_VID),
                os.environ.get("QQ_CID", DEFAULT_CID),
                args.timeout,
            )
            print("[qq] ok")
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as e:
            errors.append(f"腾讯视频: {e}")
            print(f"[qq] 失败: {e}", file=sys.stderr)

    profile = None
    if not args.skip_douyin and not args.skip_profile:
        url = os.environ.get("DOUYIN_PROFILE_URL", "").strip()
        cookie = os.environ.get("DOUYIN_PROFILE_COOKIE", "").strip()
        if url and cookie:
            try:
                profile = fetch_profile(url, cookie, args.timeout)
                print(
                    f"[douyin-profile] followers={profile['follower_count']} "
                    f"total_favorited={profile['total_favorited']}"
                )
            except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as e:
                errors.append(f"抖音 profile: {e}")
                print(f"[douyin-profile] 失败: {e}", file=sys.stderr)
        else:
            print(
                "[douyin-profile] 跳过：未设置 DOUYIN_PROFILE_URL / DOUYIN_PROFILE_COOKIE",
                file=sys.stderr,
            )

    tags = {}
    if not args.skip_douyin:
        try:
            for idx, (hid, name, alias) in enumerate(HASHTAGS):
                if idx:
                    # 连续 9 个请求容易被风控，插入 0.6~1.4s 随机间隔
                    time.sleep(random.uniform(0.6, 1.4))
                fetched = fetch_hashtag(hid, name, args.timeout)
                tags[alias] = fetched
                print(
                    f"[douyin] {name} view={fetched['viewCount']} "
                    f"users={fetched.get('userCount')}"
                )
        except (RuntimeError, ValueError, json.JSONDecodeError) as e:
            errors.append(f"抖音话题: {e}")
            print(f"[douyin] 失败: {e}", file=sys.stderr)
            tags = {}  # 不写半条记录

    xhs = None
    if not args.skip_xhs:
        cookie = os.environ.get("XHS_COOKIE", "").strip()
        if not cookie:
            print("[xhs] 跳过：未设置 XHS_COOKIE", file=sys.stderr)
        else:
            try:
                xhs = fetch_xhs(args.timeout, dry_run=not persist)
                print(f"[xhs] {json.dumps(xhs, ensure_ascii=False)}")
            except (RuntimeError, ValueError, json.JSONDecodeError) as e:
                errors.append(f"小红书: {e}")
                print(f"[xhs] 失败: {e}", file=sys.stderr)

    if persist:
        if qq:
            try:
                archive_qq(ts, qq)
            except RuntimeError as e:
                errors.append(f"腾讯视频归档: {e}")
                print(f"[qq] 归档失败: {e}", file=sys.stderr)
        if tags:
            try:
                archive_douyin(ts, profile, tags)
            except RuntimeError as e:
                errors.append(f"抖音归档: {e}")
                print(f"[douyin] 归档失败: {e}", file=sys.stderr)
    else:
        print("[只打印] 跳过归档（process.py / downsample_douyin.py）", file=sys.stderr)

    print_summary(ts, qq, profile, tags, xhs, persist)

    if errors:
        print("\n失败项：", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
