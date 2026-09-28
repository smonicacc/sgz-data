#!/usr/bin/env python3
"""Fetch a Douyin user's post list via bash curl + Python parsing.

Architecture:
  HTTP layer is bash curl (proven to work — handles HTTP/2, brotli/zstd,
  and Douyin's TLS fingerprint all natively, no Python deps needed).
  Python's role is to:
    1. generate — read the captured cURL, emit a bash pagination script
    2. parse    — read response JSON files, aggregate into final output

Workflow:
  python3 fetch_user_posts.py generate > run_pages.sh
  bash run_pages.sh                                # produces responses/page_*.json
  python3 fetch_user_posts.py parse                # produces data/user_posts.json

Env:
  SEC_USER_ID    - target user (required for generate)
  CAPTURED_FILE  - path to captured cURL (default: captured/<BJ-today>.sh)
  RESPONSES_DIR  - dir for per-page JSON (default: responses)
  OUTPUT_FILE    - aggregated output (default: data/user_posts.json)
  MAX_PAGES      - safety cap on pagination (default: 50)
  RUN_SCRIPT     - where generate writes the bash script (default: stdout,
                   or captured/run_pages.sh if --write-run-script)
"""
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


# ---------- cURL parsing (shared with previous version) ------------------- #

def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"\\\s*\n\s*", " ", text)
    return text


def parse_curl(text: str) -> dict:
    """Parse a Chrome "Copy as cURL (bash)" command.

    Returns {url, headers, cookie, compressed}.
    """
    text = re.sub(r"^\s*curl\s+", "", text.strip())
    tokens = shlex.split(text)
    if not tokens:
        raise ValueError("empty curl command")

    url = tokens[0]
    headers: dict[str, str] = {}
    cookies: list[str] = []
    compressed = False

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header"):
            kv = tokens[i + 1]
            k, _, v = kv.partition(":")
            headers[k.strip()] = v.strip()
            i += 2
        elif tok in ("-b", "--cookie"):
            cookies.append(tokens[i + 1])
            i += 2
        elif tok in ("--compressed",):
            compressed = True
            i += 1
        elif tok in ("-d", "--data", "--data-raw", "--data-binary",
                     "-X", "--request", "-A", "--user-agent",
                     "-e", "--referer", "--url"):
            i += 2  # skip value
        else:
            i += 1

    return {
        "url": url,
        "headers": headers,
        "cookie": "; ".join(cookies),
        "compressed": compressed,
    }


def _validate_endpoint(parsed: dict) -> None:
    expected = "/aweme/v1/web/aweme/post/"
    if urlparse(parsed["url"]).path != expected:
        raise ValueError(
            f"captured cURL endpoint mismatch: expected {expected!r}, "
            f"got {urlparse(parsed['url']).path!r}. Copy the aweme/post "
            f"request from DevTools, not profile/other/ or hashtag/."
        )


def _required_keys(parsed: dict) -> None:
    url = parsed["url"]
    missing = [k for k in ("sec_user_id", "max_cursor", "a_bogus", "uifid")
               if f"{k}=" not in url]
    if missing:
        raise ValueError(
            f"captured cURL missing query keys: {missing}. Re-copy from "
            f"DevTools (filter: aweme/post)."
        )


# ---------- generate: emit bash pagination script ----------------------- #

BJ_OFFSET = 8 * 3600


def _bj_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + BJ_OFFSET))


def cmd_generate(args) -> int:
    sec_user_id = os.environ.get("SEC_USER_ID", "").strip()
    captured_file = os.environ.get("CAPTURED_FILE", f"captured/{_bj_today()}.sh")
    responses_dir = os.environ.get("RESPONSES_DIR", "responses")
    max_pages = int(os.environ.get("MAX_PAGES", "50"))

    if not sec_user_id:
        print("SEC_USER_ID env var is required for generate", file=sys.stderr)
        return 2

    captured_path = Path(captured_file)
    if not captured_path.exists():
        print(f"captured file not found: {captured_path}", file=sys.stderr)
        return 2

    parsed = parse_curl(_read(str(captured_path)))
    _validate_endpoint(parsed)
    _required_keys(parsed)

    # Sanity check: the captured sec_user_id should match the env var.
    m = re.search(r"sec_user_id=([^&]+)", parsed["url"])
    captured_uid = m.group(1) if m else ""
    if captured_uid and captured_uid != sec_user_id:
        print(
            f"WARNING: SEC_USER_ID={sec_user_id} but captured cURL has "
            f"sec_user_id={captured_uid}. The script will still substitute, "
            f"but the a_bogus signature was computed for {captured_uid} — "
            f"re-capture cURL for the same user if requests start failing.",
            file=sys.stderr,
        )

    script = _build_bash_script(
        captured_path=captured_path,
        sec_user_id=sec_user_id,
        responses_dir=responses_dir,
        max_pages=max_pages,
    )

    if args.write_run_script:
        out = Path(os.environ.get("RUN_SCRIPT", "captured/run_pages.sh"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(script, encoding="utf-8")
        out.chmod(0o755)
        print(f"[generate] wrote {out}", file=sys.stderr)
        print(f"[generate] run with: bash {out}", file=sys.stderr)
    else:
        sys.stdout.write(script)

    return 0


def _build_bash_script(captured_path: Path, sec_user_id: str,
                       responses_dir: str, max_pages: int) -> str:
    """Return a bash script that paginates the captured cURL via curl."""
    # We embed the captured cURL inline so the script is self-contained.
    # Read it raw (preserving the original line breaks for readability).
    raw = captured_path.read_text(encoding="utf-8")

    # Header: safety + setup. Args parsed by parse_curl must match what
    # the user copied, but we re-substitute sec_user_id and max_cursor
    # per page via sed.
    header = f"""#!/bin/bash
# Auto-generated by fetch_user_posts.py generate
# Source cURL: {captured_path}
# Target sec_user_id: {sec_user_id}
# Run: bash {captured_path.parent.name}/{captured_path.name}
#      (this script wraps that with pagination)
set -e
MAX_PAGES={max_pages}
RESPONSES_DIR={responses_dir}
SEC_USER_ID={shlex.quote(sec_user_id)}
TEMPLATE={shlex.quote(str(captured_path.resolve()))}
mkdir -p "$RESPONSES_DIR"

cursor=0
page=1
prev_cursor=-1  # force first iteration
while [ "$page" -le "$MAX_PAGES" ] && [ "$cursor" != "$prev_cursor" ]; do
    prev_cursor="$cursor"
    out="${{RESPONSES_DIR}}/page_${{page}}.json"
    page_sh="${{RESPONSES_DIR}}/page_${{page}}.sh"

    # Generate page-specific cURL by replacing max_cursor=... and (optionally)
    # sec_user_id=... in the captured template. Uses python for correctness
    # (URL-encoded values can contain '=' which trips up sed).
    python3 - "$TEMPLATE" "$SEC_USER_ID" "$cursor" "$page_sh" <<'PYEOF'
import re, sys
template_path, sec_uid, cursor, out_path = sys.argv[1:5]
text = open(template_path, encoding="utf-8").read()
text = re.sub(r"sec_user_id=([^&]+)", "sec_user_id=" + sec_uid, text)
text = re.sub(r"max_cursor=([^&]+)", "max_cursor=" + str(cursor), text)
open(out_path, "w", encoding="utf-8").write(text)
PYEOF

    echo "[page $page] running $page_sh" >&2
    if ! bash "$page_sh" > "$out" 2>"${{out}}.stderr"; then
        echo "[page $page] FAILED — see ${{out}}.stderr" >&2
        exit 1
    fi

    # Extract cursor + has_more via python (jq may not be installed).
    eval "$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print("next_cursor=" + str(d.get("max_cursor", 0)))
    print("has_more=" + str(int(d.get("has_more", 0) or 0)))
    print("items=" + str(len(d.get("aweme_list", []) or [])))
except Exception as e:
    print("next_cursor=0")
    print("has_more=0")
    print("items=0")
    print("# parse error: " + str(e), file=sys.stderr)
' "$out")"

    echo "[page $page] items=$items has_more=$has_more next_cursor=$next_cursor" >&2

    if [ "$has_more" = "0" ]; then
        echo "[done] reached end of pagination after $page pages" >&2
        break
    fi
    cursor="$next_cursor"
    page=$((page + 1))
done
"""
    return header


# ---------- parse: aggregate response files into final output ------------ #

def cmd_parse(args) -> int:
    responses_dir = Path(os.environ.get("RESPONSES_DIR", "responses"))
    output_file = Path(os.environ.get("OUTPUT_FILE", "data/user_posts.json"))
    sec_user_id = os.environ.get("SEC_USER_ID", "").strip()
    captured_file = os.environ.get("CAPTURED_FILE", f"captured/{_bj_today()}.sh")

    if not responses_dir.exists():
        print(f"responses dir not found: {responses_dir}", file=sys.stderr)
        return 2

    page_files = sorted(responses_dir.glob("page_*.json"))
    if not page_files:
        print(f"no page_*.json files in {responses_dir}", file=sys.stderr)
        return 2

    all_aweme: list[dict] = []
    pages_fetched = 0
    error_count = 0

    for pf in page_files:
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"[parse] {pf.name}: invalid JSON ({e}); skipping",
                  file=sys.stderr)
            error_count += 1
            continue

        if data.get("status_code", 0) != 0:
            print(
                f"[parse] {pf.name}: status_code={data.get('status_code')} "
                f"status_msg={data.get('status_msg')!r}; skipping",
                file=sys.stderr,
            )
            error_count += 1

        items = data.get("aweme_list") or []
        all_aweme.extend(items)
        pages_fetched += 1
        print(f"[parse] {pf.name}: {len(items)} items "
              f"(running total: {len(all_aweme)})", file=sys.stderr)

    payload = {
        "sec_user_id": sec_user_id,
        "fetched_at": int(time.time()),
        "captured_file": captured_file,
        "responses_dir": str(responses_dir),
        "pages_fetched": pages_fetched,
        "page_errors": error_count,
        "total_items": len(all_aweme),
        "aweme_list": all_aweme,
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[parse] wrote {output_file}", file=sys.stderr)
    print(f"[parse] {pages_fetched} pages, {len(all_aweme)} items, "
          f"{error_count} page errors", file=sys.stderr)

    # ---------- append to history (data/user_posts_history.json) ----------
    # Stable timestamp = mtime of first response file (when bash wrote it).
    # This makes parse idempotent: re-running on the same responses produces
    # the same `ts`, so dedup skips re-inserted snapshots.
    history_path = output_file.parent / "user_posts_history.json"
    history: dict = {}
    if history_path.exists():
        raw = history_path.read_text(encoding="utf-8").strip()
        # Tolerate empty / missing file (e.g. cleared by hand): treat as {}.
        if raw:
            history = json.loads(raw)

    fetch_ts = int(page_files[0].stat().st_mtime)
    new_count = 0
    for item in all_aweme:
        aweme_id = item.get("aweme_id")
        if not aweme_id:
            continue
        stats = item.get("statistics", {})
        snapshot = {
            "ts": fetch_ts,
            "digg_count": stats.get("digg_count", 0),
            "comment_count": stats.get("comment_count", 0),
            "share_count": stats.get("share_count", 0),
        }
        arr = history.setdefault(aweme_id, [])
        if any(s["ts"] == fetch_ts for s in arr):
            continue
        arr.insert(0, snapshot)
        new_count += 1

    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"[parse] wrote {history_path} (+{new_count} snapshots, "
        f"{len(history)} aweme_ids total, ts={fetch_ts})",
        file=sys.stderr,
    )

    # ---------- delta summary log ----------------------------------------
    # For each aweme_id with ≥2 snapshots, compute (current - previous)
    # digg_count delta and print sorted by Δ_digg descending.
    deltas: list[tuple[str, int, int]] = []
    for aweme_id, snaps in history.items():
        if len(snaps) < 2:
            continue
        curr, prev = snaps[0], snaps[1]
        deltas.append((aweme_id, prev["digg_count"], curr["digg_count"]))
    if deltas:
        # Sort by current digg_count descending (highest first)
        deltas.sort(key=lambda r: r[2], reverse=True)
        total_digg = sum(c - p for _, p, c in deltas)
        non_zero = [d for d in deltas if d[2] - d[1] != 0]
        print(
            f"[parse] delta summary: {len(deltas)} videos with prior data, "
            f"{len(non_zero)} changed, total Δ_digg={total_digg:+,}",
            file=sys.stderr,
        )
        for aweme_id, p_digg, c_digg in non_zero:
            print(
                f"[parse]   {aweme_id}  "
                f"digg={c_digg:,}  Δ={c_digg - p_digg:+,}",
                file=sys.stderr,
            )
        unchanged = len(deltas) - len(non_zero)
        if unchanged:
            print(
                f"[parse]   ({unchanged} videos unchanged)",
                file=sys.stderr,
            )
    else:
        print(
            "[parse] delta summary: first run, no prior data to compare",
            file=sys.stderr,
        )
    return 0


# ---------- run: execute multi-page captured cURLs ----------------------- #

def cmd_run(args) -> int:
    """Run each captured/curl_pN.sh via bash, capture to responses/.

    Reads captured/curl_p*.sh in sorted order (curl_p1.sh, curl_p2.sh, ...
    up to whatever exists). N is whatever files happen to be on disk.

    Each cURL has its own valid a_bogus (because it was captured live from
    the browser), so we run them verbatim — no URL modification. This
    bypasses Plan Y's signature-replacement limit.
    """
    import glob
    import subprocess

    captured_glob = os.environ.get(
        "CAPTURED_GLOB", "captured/curl_p*.sh",
    )
    responses_dir = Path(os.environ.get("RESPONSES_DIR", "responses"))
    responses_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(glob.glob(captured_glob))
    if not files:
        print(f"no files matching: {captured_glob}", file=sys.stderr)
        print("  (capture each scrolled page from DevTools and save as "
              "curl_p1.sh, curl_p2.sh, ...)", file=sys.stderr)
        return 2

    print(f"[run] glob={captured_glob} → {len(files)} files", file=sys.stderr)

    error_count = 0
    for i, fpath in enumerate(files, start=1):
        out = responses_dir / f"page_{i}.json"
        err = responses_dir / f"page_{i}.json.stderr"
        print(f"[run] {fpath} -> {out}", file=sys.stderr)

        result = subprocess.run(
            ["bash", fpath],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out.write_text(result.stdout, encoding="utf-8")
        err.write_text(result.stderr, encoding="utf-8")

        if result.returncode != 0:
            print(f"[run] page {i}: curl exited {result.returncode}",
                  file=sys.stderr)
            error_count += 1
            continue

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"[run] page {i}: response not JSON ({e}); "
                  f"a_bogus likely expired — re-capture this page",
                  file=sys.stderr)
            error_count += 1
            continue

        if data.get("status_code", 0) != 0:
            print(f"[run] page {i}: status_code={data.get('status_code')} "
                  f"status_msg={data.get('status_msg')!r}",
                  file=sys.stderr)
            error_count += 1
            continue

        items = len(data.get("aweme_list") or [])
        print(f"[run] page {i}: {items} items OK", file=sys.stderr)

    print(f"[run] done: {len(files)} pages, {error_count} errors",
          file=sys.stderr)
    return 0 if error_count == 0 else 1


# ---------- entry point --------------------------------------------------- #

def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Douyin user posts via bash curl + Python parsing",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate",
                       help="emit bash pagination script (stdout or "
                            "--write-run-script)")
    g.add_argument("--write-run-script", action="store_true",
                   help="write script to captured/run_pages.sh instead of stdout")
    sub.add_parser("parse",
                   help="aggregate responses/*.json into data file")
    sub.add_parser("run",
                   help="execute captured/curl_p*.sh and capture responses "
                        "(each cURL has its own valid a_bogus; reads p1..pN "
                        "in sorted order, N is whatever files exist)")

    args = p.parse_args(argv)
    if args.cmd == "generate":
        return cmd_generate(args)
    if args.cmd == "parse":
        return cmd_parse(args)
    if args.cmd == "run":
        return cmd_run(args)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))