#!/usr/bin/env python3
"""Fetch viewCount + userCount for the douyin hashtag page (SSR).

Reads HTML from https://www.douyin.com/hashtag/<HID>, extracts
`viewCount` and `userCount` (both integers) from the React Server Component
flight payload embedded in the SSR HTML.

Output on success (stdout, single line):
  {"viewCount": 10154780119, "userCount": 304945, "tagName": "昀牵孟绕"}

On failure prints an error to stderr and exits non-zero.

Env:
  HID       - hashtag id (default: 1785612028243012)
  TAG_NAME  - expected Chinese tag name; if set, validated against the
              `chaName` in the page payload (sanity check, like HID).
              Echoed back in the output when available.
  UA        - user agent string (optional)
  TIMEOUT   - seconds (default 30)
  DEBUG     - if set, dump HTML to /tmp/dy_debug.html
  DY_SSL_VERIFY=0 disables TLS verification (macOS Python cert workaround)
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

# Workaround for macOS Python missing root certs (CERTIFICATE_VERIFY_FAILED).
if os.environ.get("DY_SSL_VERIFY", "1") == "0":
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    SSL_CTX = ctx
else:
    SSL_CTX = None

HID = os.environ.get("HID", "1785612028243012")
# Optional sanity-check value. If empty/unset, no check is performed.
TAG_NAME = os.environ.get("TAG_NAME", "").strip()
UA = os.environ.get(
    "UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)
# 可选：带上登录态 Cookie。CI 出口 IP 比家用 IP 更容易被风控，
# 带上同域 Cookie 能明显降低拿到占位页/验证页的概率。
COOKIE = os.environ.get("COOKIE", "").strip()
TIMEOUT = int(os.environ.get("TIMEOUT", "30"))
DEBUG = "DEBUG" in os.environ

URL = f"https://www.douyin.com/hashtag/{HID}"

# Fallback for viewCount only: rendered display text, e.g.
#   >101.5亿<!-- -->次播放</span>
PAT_DISPLAY = re.compile(
    r'>([\d.]+)亿<!--\s*-->次播放'
)


def _field_int(window: str, quote: str, name: str):
    m = re.search(re.escape(quote) + name + re.escape(quote) + r':\s*(\d+)', window)
    return int(m.group(1)) if m else None


def _field_str(window: str, quote: str, name: str):
    pat = (
        re.escape(quote) + name + re.escape(quote)
        + r':\s*' + re.escape(quote) + r'([^"\\]*)' + re.escape(quote)
    )
    m = re.search(pat, window)
    return m.group(1) if m else None


def parse_flight(text: str, hid: str):
    """从 RSC flight payload 里取 chaName / viewCount / userCount。

    Payload 嵌在 JS 字符串里，引号一般被转义成 `\\"`，偶发也会是普通 `"`，
    两种都试。字段顺序可能变化，所以先定位 `cid` 为 HID 的对象窗口，再在
    窗口内独立匹配字段，而不是用一条要求固定顺序的大正则。
    """
    for quote in ('\\"', '"'):
        anchor = quote + "cid" + quote + ":" + quote + hid + quote
        start = 0
        while True:
            idx = text.find(anchor, start)
            if idx < 0:
                break
            start = idx + 1
            # 窗口截止到下一个 cid 对象，避免误取相邻话题的字段
            nxt = text.find(quote + "cid" + quote + ":", idx + len(anchor))
            window = text[idx: nxt if nxt > idx else idx + 4000]
            view_count = _field_int(window, quote, "viewCount")
            user_count = _field_int(window, quote, "userCount")
            if view_count is not None and user_count is not None:
                return _field_str(window, quote, "chaName"), view_count, user_count
    return None


def fetch(url: str, ua: str, timeout: int) -> bytes:
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.douyin.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if COOKIE:
        headers["Cookie"] = COOKIE
    req = urllib.request.Request(url, headers=headers)
    if SSL_CTX is not None:
        return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX).read()
    return urllib.request.urlopen(req, timeout=timeout).read()


def main() -> int:
    try:
        body = fetch(URL, UA, TIMEOUT)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"FETCH_ERROR: {e}", file=sys.stderr)
        return 2

    if DEBUG:
        with open("/tmp/dy_debug.html", "wb") as f:
            f.write(body)
        print(f"DEBUG: wrote {len(body)} bytes to /tmp/dy_debug.html", file=sys.stderr)

    text = body.decode("utf-8", errors="replace")

    parsed = parse_flight(text, HID)
    if parsed:
        cha_name, view_count, user_count = parsed
        if TAG_NAME and cha_name != TAG_NAME:
            print(
                f"TAG_NAME_MISMATCH: payload chaName={cha_name!r} != requested TAG_NAME={TAG_NAME!r}",
                file=sys.stderr,
            )
            return 1
        out = {
            "viewCount": int(view_count),
            "userCount": int(user_count),
        }
        if cha_name:
            out["tagName"] = cha_name
        print(json.dumps(out, ensure_ascii=False))
        return 0

    # Fallback: viewCount is recoverable from rendered text; userCount is
    # not in the visible DOM.
    m = PAT_DISPLAY.search(text)
    if m:
        yi = float(m.group(1))
        out = {"viewCount": int(yi * 1e8), "userCount": None}
        if TAG_NAME:
            out["tagName"] = TAG_NAME
        print(json.dumps(out, ensure_ascii=False))
        return 0

    # CI 上偶发拿到风控/占位页；把页面大小和 title 带出来，方便判断是
    # 网络/反爬（重试可能恢复）还是页面结构变化。
    title_match = re.search(r"<title[^>]*>([^<]*)", text, re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else ""
    print(f"NOT_FOUND: bytes={len(body)} title={title!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())