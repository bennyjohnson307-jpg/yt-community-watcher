#!/usr/bin/env python3
"""
YouTube Community Post traffic watcher.
"""

import json
import os
import re
import sys
import time
import urllib.request

POST_URL = os.environ["COMMUNITY_POST_URL"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SPIKE_LIKES_PER_MIN = float(os.environ.get("SPIKE_LIKES_PER_MIN", "5"))
SPIKE_COMMENTS_PER_MIN = float(os.environ.get("SPIKE_COMMENTS_PER_MIN", "2"))
DEBUG = os.environ.get("DEBUG") == "1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="ignore")


def extract_yt_initial_data(html: str) -> dict:
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
    if not m:
        m = re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
    if not m:
        raise RuntimeError(
            "Could not find ytInitialData in the page. YouTube may have "
            "changed its markup, or the URL didn't load a real post "
            "(check for a login/consent wall)."
        )
    return json.loads(m.group(1))


def parse_count(text: str):
    if not text:
        return None
    text = text.strip().upper().replace(",", "")
    mult = 1
    if text.endswith("K"):
        mult, text = 1_000, text[:-1]
    elif text.endswith("M"):
        mult, text = 1_000_000, text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        return None


def find_counts(data: dict):
    likes = comments = None

    def text_of(node):
        if not node:
            return ""
        if "simpleText" in node:
            return node["simpleText"]
        return "".join(r.get("text", "") for r in node.get("runs", []))

    def walk(obj):
        nonlocal likes, comments
        if isinstance(obj, dict):
            post = obj.get("backstagePostRenderer")
            if post:
                vc = text_of(post.get("voteCount"))
                rc = text_of(
                    post.get("actionButtons", {})
                    .get("commentActionButtonsRenderer", {})
                    .get("replyButton", {})
                    .get("buttonRenderer", {})
                    .get("text", {})
                )
                if vc:
                    likes = parse_count(vc)
                if rc:
                    comments = parse_count(rc)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return likes, comments

def get_innertube_key(html: str):
    m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    return m.group(1) if m else None


def get_client_version(html: str):
    m = re.search(r'"clientVersion":"([^"]+)"', html)
    return m.group(1) if m else "2.20240101.00.00"


def find_all_continuations(data: dict):
    """Find every continuation token in the page, tagged with a hint of
    what section it belongs to, so we can identify the comments one."""
    found = []

    def walk(obj, path=""):
        if isinstance(obj, dict):
            cmd = obj.get("continuationCommand") or obj.get("nextContinuationData")
            token = None
            if cmd:
                token = cmd.get("token") or cmd.get("continuation")
            if token:
                found.append({"path": path, "token": token})
            for k, v in obj.items():
                walk(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    walk(data)
    return found


def fetch_comments_page(api_key: str, client_version: str, cont_token: str):
    body = json.dumps({
        "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
        "continuation": cont_token,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://www.youtube.com/youtubei/v1/next?key={api_key}",
        data=body,
        headers={**HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def count_fresh_toplevel_comments(resp: dict):
    """Counts top-level comments (not replies) whose posted time says
    'X seconds ago' - i.e. genuinely fresh."""
    fresh_count = [0]

    def walk(obj):
        if isinstance(obj, dict):
            thread = obj.get("commentThreadRenderer")
            if thread:
                comment = thread.get("comment", {}).get("commentRenderer", {})
                published = comment.get("publishedTimeText", {})
                text = "".join(r.get("text", "") for r in published.get("runs", []))
                if "second" in text.lower():
                    fresh_count[0] += 1
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(resp)
    return fresh_count[0]

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def notify(title: str, message: str, priority: str = "high"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "fire,speaker",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    html = fetch_html(POST_URL)
    data = extract_yt_initial_data(html)

    if DEBUG:
        with open("debug_ytInitialData.json", "w") as f:
            json.dump(data, f, indent=2)
        print("Wrote debug_ytInitialData.json")

    if DEBUG:
        api_key = get_innertube_key(html)
        client_version = get_client_version(html)
        conts = find_all_continuations(data)
        print(f"DEBUG: found {len(conts)} continuation tokens:")
        for c in conts:
            print(f"  path={c['path']}")
        print(f"DEBUG: api_key found = {bool(api_key)}")
    likes, comments = find_counts(data)
    now = time.time()
    state = load_state()

    if likes is None and comments is None:
        print("Could not parse like/comment counts. Try DEBUG=1 to inspect the JSON.")
        sys.exit(0)

    prev = state.get(POST_URL)
    state[POST_URL] = {"t": now, "likes": likes, "comments": comments}
    save_state(state)

    print(f"likes={likes} comments={comments}")

    if prev:
        dt_min = max((now - prev["t"]) / 60.0, 0.01)
        d_likes = (
            (likes - prev["likes"]) / dt_min
            if likes is not None and prev.get("likes") is not None
            else 0
        )
        d_comments = (
            (comments - prev["comments"]) / dt_min
            if comments is not None and prev.get("comments") is not None
            else 0
        )
        print(f"rate: +{d_likes:.2f} likes/min, +{d_comments:.2f} comments/min")

        if d_likes >= SPIKE_LIKES_PER_MIN or d_comments >= SPIKE_COMMENTS_PER_MIN:
            notify(
                "Community post is heating up",
                f"{POST_URL}\n"
                f"+{d_likes:.1f} likes/min, +{d_comments:.1f} comments/min\n"
                f"Totals so far: {int(likes) if likes else '?'} likes, "
                f"{int(comments) if comments else '?'} comments",
            )
            print("Spike detected - notification sent.")
    else:
        print("First run - baseline recorded, nothing to compare yet.")


if __name__ == "__main__":
    main()
