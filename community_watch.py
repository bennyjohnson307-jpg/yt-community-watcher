#!/usr/bin/env python3
"""
YouTube Community Post traffic watcher - watches multiple posts.
Includes the channel name in every alert, adaptive per-post
thresholds, comment-flood detection, US-timing context, and
watchdog alerts on repeated failures.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

POST_URLS = [
    u.strip() for u in os.environ["COMMUNITY_POST_URLS"].split(",") if u.strip()
]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SPIKE_LIKES_PER_MIN_FLOOR = float(os.environ.get("SPIKE_LIKES_PER_MIN", "50"))
SPIKE_COMMENTS_PER_MIN = float(os.environ.get("SPIKE_COMMENTS_PER_MIN", "10"))
FLOOD_COMMENT_COUNT = float(os.environ.get("FLOOD_COMMENT_COUNT", "3"))
RATE_HISTORY_LEN = 20
MIN_HISTORY_FOR_ADAPTIVE = 5
ADAPTIVE_MULTIPLIER = 3.0
FAIL_ALERT_THRESHOLD = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def is_us_prime_time() -> bool:
    et_now = datetime.now(ZoneInfo("America/New_York"))
    return 7 <= et_now.hour < 23


def us_time_label() -> str:
    et_now = datetime.now(ZoneInfo("America/New_York"))
    time_str = et_now.strftime("%I:%M %p ET")
    if is_us_prime_time():
        return f"US prime time ({time_str}) - high visibility window"
    return f"outside typical US hours ({time_str}) - lower visibility likely"


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="ignore")


def extract_yt_initial_data(html: str) -> dict:
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
    if not m:
        m = re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
    if not m:
        raise RuntimeError("Could not find ytInitialData in the page.")
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
    channel_name = None

    def text_of(node):
        if not node:
            return ""
        if "simpleText" in node:
            return node["simpleText"]
        return "".join(r.get("text", "") for r in node.get("runs", []))

    def walk(obj):
        nonlocal likes, comments, channel_name
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
                author = text_of(post.get("authorText"))
                handle = (
                    post.get("authorEndpoint", {})
                    .get("browseEndpoint", {})
                    .get("canonicalBaseUrl", "")
                )
                if handle.startswith("/@"):
                    channel_name = handle[1:]
                elif author:
                    channel_name = author
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return likes, comments, channel_name


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
        headers={"Title": title, "Priority": priority, "Tags": "fire,speaker"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def check_post(post_url: str, state: dict):
    entry = state.get(post_url, {})
    fail_count = entry.get("fail_count", 0)

    try:
        html = fetch_html(post_url)
        data = extract_yt_initial_data(html)
        likes, comments, channel_name = find_counts(data)
    except Exception as e:
        fail_count += 1
        print(f"[{post_url}] fetch/parse failed ({fail_count}x): {e}")
        entry["fail_count"] = fail_count
        state[post_url] = entry
        if fail_count == FAIL_ALERT_THRESHOLD:
            notify(
                "Watcher problem",
                f"{post_url}\nFailed to read this post {fail_count} times in a row - "
                f"it may need a look.",
                priority="default",
            )
        return

    now = time.time()
    channel_label = channel_name or "Unknown channel"

    if likes is None and comments is None:
        fail_count += 1
        print(f"[{post_url}] could not parse counts ({fail_count}x).")
        entry["fail_count"] = fail_count
        state[post_url] = entry
        if fail_count == FAIL_ALERT_THRESHOLD:
            notify(
                "Watcher problem",
                f"{post_url}\nCouldn't read counts {fail_count} times in a row - "
                f"it may need a look.",
                priority="default",
            )
        return

    prev = entry if entry.get("t") else None
    rate_history = entry.get("rate_history", [])

    print(f"[{channel_label}] [{post_url}] likes={likes} comments={comments}")

    if not prev:
        state[post_url] = {
            "t": now, "likes": likes, "comments": comments,
            "rate_history": [], "fail_count": 0,
        }
        print(f"[{post_url}] first run - baseline recorded.")
        return

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
    print(f"[{post_url}] rate: +{d_likes:.2f} likes/min, +{d_comments:.2f} comments/min")

    baseline = None
    if len(rate_history) >= MIN_HISTORY_FOR_ADAPTIVE:
        baseline = sum(rate_history) / len(rate_history)
        adaptive_threshold = max(
            SPIKE_LIKES_PER_MIN_FLOOR * 0.3, baseline * ADAPTIVE_MULTIPLIER
        )
    else:
        adaptive_threshold = SPIKE_LIKES_PER_MIN_FLOOR

    rate_history.append(d_likes)
    rate_history = rate_history[-RATE_HISTORY_LEN:]

    comments_gained = (
        comments - prev["comments"]
        if comments is not None and prev.get("comments") is not None
        else 0
    )
    if comments_gained >= FLOOD_COMMENT_COUNT:
        notify(
            "Comments flooding in",
            f"{channel_label}\n{post_url}\n"
            f"{int(comments_gained)} new comments since last check - jump in now!\n"
            f"{us_time_label()}",
        )
        print(f"[{post_url}] flood detected: {int(comments_gained)} new comments.")

    if d_likes >= adaptive_threshold or d_comments >= SPIKE_COMMENTS_PER_MIN:
        baseline_note = f" (this post's normal pace: {baseline:.1f}/min)" if baseline is not None else ""
        notify(
            "Community post is heating up",
            f"{channel_label}\n{post_url}\n"
            f"+{d_likes:.1f} likes/min, +{d_comments:.1f} comments/min{baseline_note}\n"
            f"Totals so far: {int(likes) if likes else '?'} likes, "
            f"{int(comments) if comments else '?'} comments\n"
            f"{us_time_label()}",
        )
        print(f"[{post_url}] spike detected - notification sent.")

    state[post_url] = {
        "t": now, "likes": likes, "comments": comments,
        "rate_history": rate_history, "fail_count": 0,
    }


def main():
    state = load_state()
    for post_url in POST_URLS:
        check_post(post_url, state)
    save_state(state)


if __name__ == "__main__":
    main()
