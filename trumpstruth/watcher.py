"""
trumpstruth/watcher.py — Trump Truth Social monitor.

Polls trumpstruth.org/feed every 2 min.
Detects new posts and signals MM via /signal endpoint.
MM handles all filtering, analysis, and Telegram.

Note: trumpstruth.org is a third-party mirror, not an official source.
If it goes down, replace TRUMP_RSS with another mirror or use the X/Twitter API.
"""

import re
import time
import logging
import threading
import requests
from pathlib import Path
import json

log = logging.getLogger('MonsieurMarket')

TRUMP_RSS     = "https://trumpstruth.org/feed"
POLL_INTERVAL = 120  # seconds
MM_SIGNAL_URL = "http://localhost:3456/signal"


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
def _load_state() -> dict:
    state_file = Path("data/monsieur_market_state.json")
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {"trump_last_seen_url": None}


def _save_state(state: dict):
    Path("data/monsieur_market_state.json").write_text(json.dumps(state, indent=2))


# ─────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────
def _fetch_trump_posts() -> list[dict]:
    """Fetch latest Trump Truth Social posts from RSS mirror."""
    try:
        r = requests.get(
            TRUMP_RSS,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MonsieurMarket/1.0)"}
        )
        r.raise_for_status()

        items = re.findall(r"<item\b.*?>(.*?)</item>", r.text, re.DOTALL | re.IGNORECASE)
        posts = []

        for item in items[:10]:
            title_m = re.search(
                r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>",
                item,
                re.DOTALL | re.IGNORECASE,
            )
            link_m = re.search(r"<link>(.*?)</link>", item, re.DOTALL | re.IGNORECASE)
            guid_m = re.search(r"<guid.*?>(.*?)</guid>", item, re.DOTALL | re.IGNORECASE)

            title = ""
            if title_m:
                title = (title_m.group(1) or title_m.group(2) or "").strip()

            url = ""
            if link_m:
                url = link_m.group(1).strip()
            elif guid_m:
                url = guid_m.group(1).strip()

            if url:
                posts.append({
                    "url": url,
                    "title": title or "[media/no text post]"
                })

        log.info(f"Trump RSS: fetched {len(posts)} parseable post(s)")
        if posts:
            log.info(f"Trump RSS latest: {posts[0]['url']} | {posts[0]['title'][:80]}")

        return posts

    except Exception as e:
        log.warning(f"Trump RSS fetch error: {e}")
        return []


# ─────────────────────────────────────────────
# SIGNAL MM
# ─────────────────────────────────────────────
def _signal_mm(post: dict):
    """POST new Trump post to MM /signal endpoint. MM handles everything else."""
    try:
        requests.post(MM_SIGNAL_URL, json={
            "source": "trump",
            "type":   "post",
            "data":   {"title": post["title"], "url": post["url"]},
            "ts":     time.time(),
        }, timeout=5)
        log.info(f"Trump signal → MM: {post['title'][:60]}")
    except Exception as e:
        log.warning(f"Trump signal to MM failed: {e}")


# ─────────────────────────────────────────────
# WATCHER LOOP
# ─────────────────────────────────────────────
def _trump_watcher_worker():
    log.info("Trump watcher started — polling every 2 min")

    while True:
        try:
            state     = _load_state()
            last_seen = state.get("trump_last_seen_url")
            posts     = _fetch_trump_posts()

            if not posts:
                log.warning("Trump watcher: feed returned 0 parseable posts")                
                time.sleep(POLL_INTERVAL)
                continue

            new_posts = []
            for post in posts:
                if post["url"] == last_seen:
                    break
                new_posts.append(post)

            if not new_posts:
                log.info(f"Trump watcher: no new posts (last_seen={last_seen})")
                time.sleep(POLL_INTERVAL)
                continue

            log.info(f"Trump watcher: {len(new_posts)} new post(s)")

            # Persist last seen immediately — avoids reprocessing on restart
            state["trump_last_seen_url"] = posts[0]["url"]
            _save_state(state)

            for post in new_posts:
                log.info(f"Trump post: {post['title'][:80]}")
                _signal_mm(post)

        except Exception as e:
            log.error(f"Trump watcher error: {e}")

        time.sleep(POLL_INTERVAL)


def start_trump_watcher():
    """Launch the Trump watcher in a daemon background thread."""
    t = threading.Thread(target=_trump_watcher_worker, name="TrumpWatcher", daemon=True)
    t.start()
    log.info("Trump watcher thread started")