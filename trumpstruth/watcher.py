"""
trumpstruth/watcher.py — Trump Truth Social monitor.

Polls trumpstruth.org/feed (third-party RSS mirror of Truth Social) every 2 min.
Haiku filters for oil/Iran/geopolitical relevance — no keyword pre-filter since
Trump often signals obliquely (e.g. 'a civilization will die tonight').
Sonnet + web search analyses relevant posts and sends to Telegram.

Note: trumpstruth.org is a third-party mirror, not an official source.
If it goes down, replace TRUMP_RSS with another mirror or use the X/Twitter API.
"""

import os
import re
import time
import logging
import threading
import requests
from datetime import datetime

import anthropic

from config import CONFIG
from telegram_client import send_message

log = logging.getLogger('MonsieurMarket')

TRUMP_RSS     = "https://trumpstruth.org/feed"
POLL_INTERVAL = 120  # seconds


# ─────────────────────────────────────────────
# STATE — imported from MM to share the same file
# ─────────────────────────────────────────────
def _load_state() -> dict:
    """Load state from MM state file."""
    from pathlib import Path
    import json
    state_file = Path("data/monsieur_market_state.json")
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {"trump_last_seen_url": None}


def _save_state(state: dict):
    from pathlib import Path
    import json
    Path("data/monsieur_market_state.json").write_text(json.dumps(state, indent=2))


# ─────────────────────────────────────────────
# FETCH
# ─────────────────────────────────────────────
def _fetch_trump_posts() -> list[dict]:
    """Fetch latest Trump Truth Social posts from RSS mirror."""
    try:
        r = requests.get(TRUMP_RSS, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (compatible; MonsieurMarket/1.0)"
        })
        r.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        posts = []
        for item in items[:10]:
            title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item, re.DOTALL)
            link_m  = re.search(r"<link>(.*?)</link>", item)
            title   = title_m.group(1).strip() if title_m else ""
            url     = link_m.group(1).strip()  if link_m  else ""
            if title and url:
                posts.append({"url": url, "title": title})
        return posts
    except Exception as e:
        log.debug(f"Trump RSS fetch error: {e}")
        return []


# ─────────────────────────────────────────────
# HAIKU FILTER
# ─────────────────────────────────────────────
def _haiku_trump_filter(post: dict) -> bool:
    """
    Haiku decides if a Trump post is relevant to oil/Iran/geopolitics.
    No keyword pre-filter — Trump often signals obliquely
    (e.g. 'a civilization will die tonight' = Iran strike context).
    Haiku calls are cheap and Trump posts are infrequent (~few per day).
    """
    try:
        client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content":
                f"Does this Trump post relate to oil prices, Iran, Saudi Arabia, "
                f"OPEC, Middle East conflict, energy markets, or military action?\n\n"
                f'"{post["title"]}"\n\n'
                f"Reply YES or NO only."
            }],
        )
        return response.content[0].text.strip().upper().startswith("YES")
    except Exception as e:
        log.debug(f"Haiku Trump filter error: {e}")
        return False


# ─────────────────────────────────────────────
# SONNET ANALYSIS
# ─────────────────────────────────────────────
def _sonnet_analyze_trump(post: dict) -> str | None:
    try:
        client      = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        themes_text = ", ".join(t["name"] for t in CONFIG["themes"] if t.get("active"))
        response    = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content":
                f"Trump just posted on Truth Social:\n\n"
                f'"{post["title"]}"\n\n'
                f"Active trading themes: {themes_text}\n"
                f"The trader is long Brent crude (knock-out turbo) and long Thales.\n\n"
                f"In 4-5 sentences max: what does this post imply for Brent crude price? "
                f"Bullish or bearish and why? What 2 things should the trader watch next? "
                f"Be direct and specific — no fluff. No markdown headers."
            }],
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 2,
            }],
        )
        text_blocks = [
            block.text for block in response.content
            if hasattr(block, "text") and block.text and block.text.strip()
        ]
        return " ".join(text_blocks).strip() or None
    except Exception as e:
        log.warning(f"Sonnet Trump analysis error: {e}")
        return None


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
                time.sleep(POLL_INTERVAL)
                continue

            new_posts = []
            for post in posts:
                if post["url"] == last_seen:
                    break
                new_posts.append(post)

            if not new_posts:
                log.debug("Trump watcher: no new posts")
                time.sleep(POLL_INTERVAL)
                continue

            log.info(f"Trump watcher: {len(new_posts)} new post(s)")

            # Persist last seen immediately — avoids reprocessing on restart
            state["trump_last_seen_url"] = posts[0]["url"]
            _save_state(state)

            for post in new_posts:
                log.info(f"Trump post: {post['title'][:80]}")

                if not _haiku_trump_filter(post):
                    log.info("  → not relevant — skipping")
                    continue

                log.info("  → RELEVANT — firing Sonnet analysis")
                analysis = _sonnet_analyze_trump(post)

                if analysis:
                    send_message(
                        f"🇺🇸 <b>MonsieurMarket — Trump Alert</b>\n\n"
                        f"<i>{post['title'][:300]}</i>\n\n"
                        f"🧠 <b>Sonnet:</b> {analysis}\n\n"
                        f"⏰ {datetime.now().strftime('%d/%m %H:%M')}"
                    )
                    log.info("  → Trump alert sent")

        except Exception as e:
            log.error(f"Trump watcher error: {e}")

        time.sleep(POLL_INTERVAL)


def start_trump_watcher():
    """Launch the Trump watcher in a daemon background thread."""
    t = threading.Thread(target=_trump_watcher_worker, name="TrumpWatcher", daemon=True)
    t.start()
    log.info("Trump watcher thread started")