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
TIMEOUT = int(os.environ.get("TIMEOUT", "30"))
DEBUG = "DEBUG" in os.environ

URL = f"https://www.douyin.com/hashtag/{HID}"

# Primary: full RSC flight payload. The payload is embedded inside a JS
# string literal so JSON quotes are escaped as `\"`. Anchor on the requested
# `cid`, then capture chaName + viewCount + userCount.
# Example: \"cid\":\"1785612028243012\",\"chaName\":\"...\",\"viewCount\":10154780119,\"userCount\":304945
PAT_FLIGHT = re.compile(
    r'\\"cid\\":\\"(\d+)\\"[,\\][^\\]*?'
    r'\\"chaName\\":\\"([^"\\]+)\\"[,\\][^\\]*?'
    r'\\"viewCount\\":(\d+)[,\\][^\\]*?'
    r'\\"userCount\\":(\d+)'
)

# Fallback for viewCount only: rendered display text, e.g.
#   >101.5亿<!-- -->次播放</span>
PAT_DISPLAY = re.compile(
    r'>([\d.]+)亿<!--\s*-->次播放'
)


def fetch(url: str, ua: str, timeout: int) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
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

    m = PAT_FLIGHT.search(text)
    if m:
        cid, cha_name, view_count, user_count = m.groups()
        if cid != HID:
            print(
                f"CID_MISMATCH: payload cid={cid} != requested HID={HID}",
                file=sys.stderr,
            )
            return 1
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

    print("NOT_FOUND", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())