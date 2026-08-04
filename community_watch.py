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
                if DEBUG:
                    print(f"DEBUG: post keys = {list(post.keys())}")
                    action_buttons = post.get("actionButtons", {})
                    print(f"DEBUG: actionButtons = {json.dumps(action_buttons)[:800]}")
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
def find_comment_mentions(data: dict):
    """Search the entire page for anything mentioning 'comment', to find
    where YouTube actually stores the comment count for this post type."""
    matches = []

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "comment" in k.lower():
                    snippet = json.dumps(v)[:300]
                    matches.append(f"{path}/{k} = {snippet}")
                walk(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    walk(data)
    return matches
    if DEBUG:
        with open("debug_ytInitialData.json", "w") as f:
            json.dump(data, f, indent=2)
        print("Wrote debug_ytInitialData.json")
    if DEBUG:
        mentions = find_comment_mentions(data)
        print(f"DEBUG: found {len(mentions)} comment-related keys:")
        for m in mentions[:20]:
            print(f"  {m}")

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
        comments_gained = (
            comments - prev["comments"]
            if comments is not None and prev.get("comments") is not None
            else 0
        )
        FLOOD_THRESHOLD = float(os.environ.get("FLOOD_COMMENT_COUNT", "3"))
        if comments_gained >= FLOOD_THRESHOLD:
            notify(
                "Comments flooding in",
                f"{POST_URL}\n"
                f"{int(comments_gained)} new comments since last check - jump in now!",
            )
            print(f"Flood detected: {int(comments_gained)} new comments.")

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
