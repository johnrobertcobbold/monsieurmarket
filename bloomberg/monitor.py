#!/usr/bin/env python3
"""
Bloomberg Camoufox Monitor
Finds today's Iran liveblog or best In Focus tag, scrapes posts/headlines,
serves via Flask API.

Flow:
    1. Open bloomberg.com homepage
    2. Scan for liveblog link → navigate if found
    3. If not → click "See all latest" (human mouse movement)
    4. Read In Focus tags (data-testid="link-pill")
    5. Haiku picks best tag (once per day, saved to disk)
    6. Human click on chosen tag
    7. Scrape headlines with UTC timestamps
    8. Wait for /refresh calls from MonsieurMarket (no internal timer)

Behavior updates:
    - Homepage is a temporary probe page, not a resting place.
    - After a failed homepage liveblog probe, browser returns to the current
      valid source page.
    - After page recycle, browser reopens the current valid source page.
    - Ended liveblogs are put on a short per-URL cooldown so we don't loop
      homepage → dead liveblog → homepage, but we still retry later because
      Bloomberg may re-open the same URL.

Signal flow:
    monitor → POST /signal to MonsieurMarket on:
        - startup ready (source found)
        - no_source (MM decides when to retry)
        - new posts found after /refresh
        - liveblog_ended
        - error

    MonsieurMarket → GET /refresh to trigger a scrape
    Monitor has NO internal poll timer — MM owns the schedule.

Endpoints:
    GET /health          — status, date, source type, next retry
    GET /posts?since=    — posts/headlines newer than timestamp
    GET /latest          — most recent item
    GET /refresh         — immediate scrape, returns new items

Run:
    python bloomberg_camoufox/monitor.py
    python bloomberg_camoufox/monitor.py --visible
"""

import json
import time
import threading
import logging
import os
import sys
import re
import random
import gc
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

from camoufox.sync_api import Camoufox
from flask import Flask, jsonify, request
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env')

import anthropic

# ─────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
SESSION_FILE = BASE_DIR / 'bloomberg_session.json'
FEED_FILE    = BASE_DIR / 'bloomberg_feed.json'
STATE_FILE   = BASE_DIR / 'monitor_state.json'

# ─────────────────────────────────────────────
# MONSIEUR MARKET SIGNAL URL
# ─────────────────────────────────────────────
MM_SIGNAL_URL = os.getenv('MM_SIGNAL_URL', 'http://localhost:3456/signal')

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('BloombergMonitor')
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
HEADLESS = '--visible' not in sys.argv

CONFIG = {
    'port':               3457,
    'poll_interval_min':  15,
    'haiku_model':        'claude-haiku-4-5-20251001',
    'relevance_keywords': [
        'iran', 'oil', 'houthi', 'war', 'energy',
        'opec', 'hormuz', 'brent', 'crude', 'red sea',
    ],
    'ended_liveblog_cooldown_min': 15,
}

# How many scrape cycles before we close + recreate the page object.
# At 15-min polls this is every ~2.5 hours. Lower = safer, more navigations.
PAGE_RECYCLE_CYCLES = 10


# ─────────────────────────────────────────────
# SIGNAL TO MONSIEUR MARKET
# ─────────────────────────────────────────────
def signal_mm(signal_type: str, data: dict, retries: int = 5, delay: int = 3):
    """
    POST a signal to MonsieurMarket's /signal endpoint.
    Retries with backoff in case MM isn't up yet (startup race condition).

    signal_type: 'ready' | 'no_source' | 'post' | 'liveblog_ended' | 'error'
    data: signal-specific payload
    """
    payload = {
        'source': 'bloomberg',
        'type':   signal_type,
        'data':   data,
        'ts':     time.time(),
    }
    for attempt in range(retries):
        try:
            r = requests.post(MM_SIGNAL_URL, json=payload, timeout=3)
            r.raise_for_status()
            log.debug(f"📡 Signal sent to MM: {signal_type}")
            return True
        except Exception as e:
            if attempt < retries - 1:
                log.debug(f"MM signal attempt {attempt + 1} failed ({e}) — retrying in {delay}s")
                time.sleep(delay)
            else:
                log.warning(f"MM signal failed after {retries} attempts: {e}")
    return False


# ─────────────────────────────────────────────
# PERSISTENT STATE — survives restarts
# ─────────────────────────────────────────────
def load_persistent_state() -> dict:
    try:
        if STATE_FILE.exists():
            saved = json.loads(STATE_FILE.read_text())
            log.info(
                f"📂 Persistent state loaded — "
                f"tag: {saved.get('latest_tag_name')} "
                f"({saved.get('tag_set_date')})"
            )
            return {
                'latest_tag_url':      saved.get('latest_tag_url'),
                'latest_tag_name':     saved.get('latest_tag_name'),
                'tag_set_date':        saved.get('tag_set_date'),
                'ended_liveblogs':     saved.get('ended_liveblogs', {}),
                'last_valid_source_url':  saved.get('last_valid_source_url'),
                'last_valid_source_type': saved.get('last_valid_source_type'),
            }
    except Exception as e:
        log.debug(f"Persistent state load failed: {e}")
    return {}


def save_persistent_state():
    try:
        with state_lock:
            to_save = {
                'latest_tag_url':         state.get('latest_tag_url'),
                'latest_tag_name':        state.get('latest_tag_name'),
                'tag_set_date':           state.get('tag_set_date'),
                'ended_liveblogs':        state.get('ended_liveblogs', {}),
                'last_valid_source_url':  state.get('last_valid_source_url'),
                'last_valid_source_type': state.get('last_valid_source_type'),
            }
        STATE_FILE.write_text(json.dumps(to_save, indent=2))
        log.debug("💾 Persistent state saved")
    except Exception as e:
        log.debug(f"Persistent state save failed: {e}")


def load_persistent_posts() -> list:
    """Reload posts from feed file on startup."""
    try:
        if FEED_FILE.exists():
            data = json.loads(FEED_FILE.read_text())
            posts = data.get('posts', [])
            if posts:
                log.info(f"📂 Loaded {len(posts)} posts from feed file")
            return posts
    except Exception as e:
        log.debug(f"Feed load failed: {e}")
    return []


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
state = {
    'status':               'starting',
    'date':                 None,
    'source_type':          None,
    'source_url':           None,
    'posts':                load_persistent_posts(),
    'post_count':           0,
    'last_post_ts':         0,
    'last_refresh':         None,
    'last_liveblog_check':  None,
    'latest_tag_url':       None,
    'latest_tag_name':      None,
    'tag_set_date':         None,
    'next_retry':           None,
    'errors':               [],
    'ended_liveblogs':      {},
    'last_valid_source_url':  None,
    'last_valid_source_type': None,
}

# restore persisted fields on startup
state.update(load_persistent_state())

state_lock           = threading.Lock()
_refresh_event       = threading.Event()
_refresh_lock        = threading.Lock()
_refresh_in_progress = False
_page                = None
_context             = None


def update_state(**kwargs):
    persist_needed = any(
        k in kwargs for k in (
            'latest_tag_url',
            'latest_tag_name',
            'tag_set_date',
            'ended_liveblogs',
            'last_valid_source_url',
            'last_valid_source_type',
        )
    )
    with state_lock:
        state.update(kwargs)
    save_feed()
    if persist_needed:
        save_persistent_state()


def save_feed():
    with state_lock:
        data = {
            'status':       state['status'],
            'date':         state['date'],
            'source_type':  state['source_type'],
            'source_url':   state['source_url'],
            'posts':        state['posts'],
            'post_count':   state['post_count'],
            'last_refresh': state['last_refresh'],
            'last_post_ts': state['last_post_ts'],
            'updated':      time.time(),
        }
    FEED_FILE.write_text(json.dumps(data, indent=2))


def log_error(msg: str):
    log.error(msg)
    with state_lock:
        state['errors'].append({
            'msg': msg,
            'ts':  datetime.now(timezone.utc).isoformat(),
        })
        state['errors'] = state['errors'][-10:]


def add_post(post: dict) -> bool:
    """Add post to state, deduplicate. Returns True if new."""
    with state_lock:
        exists = any(
            (p.get('id')  and p.get('id')  == post.get('id')) or
            (p.get('url') and p.get('url') == post.get('url'))
            for p in state['posts']
        )
        if exists:
            return False

        post['captured_at'] = datetime.now(timezone.utc).isoformat()
        state['posts'].insert(0, post)
        state['posts']        = state['posts'][:200]
        state['post_count']   = len(state['posts'])
        state['last_post_ts'] = time.time()

    save_feed()

    ts_raw = post.get('timestamp_raw', '?')
    log.info(f"📝 New [{ts_raw}]: {post.get('title', 'untitled')[:65]}")
    return True


# ─────────────────────────────────────────────
# TIMESTAMP PARSER — UTC
# ─────────────────────────────────────────────
def parse_relative_time(timestamp_text: str, scraped_at: datetime) -> str:
    """
    Convert Bloomberg relative timestamps to approximate UTC ISO.
    '26 min ago' → '2026-04-03T15:55:00+00:00'
    '2 hr ago'   → '2026-04-03T14:21:00+00:00'
    'April 2, 2026' → '2026-04-02'
    """
    t = timestamp_text.strip().lower()

    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)

    try:
        if 'just now' in t or t == 'now':
            return scraped_at.isoformat()

        m = re.search(r'(\d+)\s*min', t)
        if m:
            mins = int(m.group(1))
            return (scraped_at - timedelta(minutes=mins)).isoformat()

        m = re.search(r'(\d+)\s*hr', t)
        if m:
            hrs = int(m.group(1))
            return (scraped_at - timedelta(hours=hrs)).isoformat()

        try:
            dt = datetime.strptime(timestamp_text.strip(), '%B %d, %Y')
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            pass

    except Exception:
        pass

    return timestamp_text


# ─────────────────────────────────────────────
# LIVEBLOG COOLDOWN HELPERS
# ─────────────────────────────────────────────
def _prune_ended_liveblogs(ended_map: dict | None = None) -> dict:
    """Drop cooldown entries older than 24h."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    if ended_map is None:
        with state_lock:
            ended_map = dict(state.get('ended_liveblogs', {}))

    pruned = {}
    for url, meta in ended_map.items():
        try:
            ended_at = datetime.fromisoformat(meta.get('ended_at'))
            if ended_at >= cutoff:
                pruned[url] = meta
        except Exception:
            continue
    return pruned


def get_ended_liveblogs() -> dict:
    with state_lock:
        ended_map = _prune_ended_liveblogs(state.get('ended_liveblogs', {}))
        if ended_map != state.get('ended_liveblogs', {}):
            state['ended_liveblogs'] = ended_map
            save_feed()
            save_persistent_state()
        return dict(ended_map)


def mark_liveblog_ended(url: str, reason: str = ''):
    cooldown_min = CONFIG.get('ended_liveblog_cooldown_min', 15)
    now = datetime.now(timezone.utc)
    retry_after = now + timedelta(minutes=cooldown_min)

    ended_map = get_ended_liveblogs()
    ended_map[url] = {
        'ended_at': now.isoformat(),
        'retry_after': retry_after.isoformat(),
        'reason': reason,
    }
    update_state(ended_liveblogs=ended_map)
    log.info(f"🧊 Cooldown liveblog for {cooldown_min}min: {url}")


def is_liveblog_on_cooldown(url: str) -> tuple[bool, str]:
    ended_map = get_ended_liveblogs()
    meta = ended_map.get(url)
    if not meta:
        return False, ''

    try:
        retry_after = datetime.fromisoformat(meta['retry_after'])
        if retry_after > datetime.now(timezone.utc):
            return True, meta.get('reason', '')
    except Exception:
        return False, ''

    return False, ''


# ─────────────────────────────────────────────
# HUMAN-LIKE MOUSE MOVEMENT
# ─────────────────────────────────────────────
def human_click(page, selector: str) -> bool:
    """Move mouse smoothly to element then click."""
    try:
        element = page.query_selector(selector)
        if not element:
            return False

        box = element.bounding_box()
        if not box:
            return False

        target_x = box['x'] + box['width']  / 2
        target_y = box['y'] + box['height'] / 2

        current_x, current_y = 200, 150
        steps = 25

        for i in range(steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)
            x = current_x + (target_x - current_x) * t
            y = current_y + (target_y - current_y) * t
            page.mouse.move(x, y)
            page.wait_for_timeout(8)

        page.wait_for_timeout(120)
        page.mouse.click(target_x, target_y)
        log.debug(f"🖱️  Clicked: {selector}")
        return True

    except Exception as e:
        log.debug(f"human_click failed ({selector}): {e}")
        return False


def click_see_all_latest(page) -> bool:
    """Scroll down homepage and click 'See all latest'."""
    try:
        # scroll down like a human
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(1000)
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(800)

        # find VISIBLE link and scroll to it
        found = page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a'));
            const matches = links.filter(a =>
                a.textContent?.trim().toLowerCase() === 'see all latest news' ||
                a.textContent?.trim().toLowerCase() === 'see all latest' ||
                a.textContent?.trim().toLowerCase() === 'all latest news'
            );
            const link = matches.find(a => {
                const rect  = a.getBoundingClientRect();
                const style = window.getComputedStyle(a);
                return rect.width > 0 &&
                       rect.height > 0 &&
                       style.display !== 'none' &&
                       style.visibility !== 'hidden' &&
                       style.opacity !== '0';
            });
            if (!link) return null;
            link.scrollIntoView({ behavior: 'instant', block: 'center' });
            return link.href;
        }""")

        if not found:
            log.info("  'See all latest' not found — navigating directly")
            page.goto('https://www.bloomberg.com/latest',
                      timeout=15000, wait_until='domcontentloaded')
            return True

        log.info(f"  Found 'See all latest' → {found}")
        page.wait_for_timeout(1000)

        # measure VISIBLE link after scroll settles
        box = page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a'));
            const matches = links.filter(a =>
                a.textContent?.trim().toLowerCase() === 'see all latest news' ||
                a.textContent?.trim().toLowerCase() === 'see all latest' ||
                a.textContent?.trim().toLowerCase() === 'all latest news'
            );
            const link = matches.find(a => {
                const rect  = a.getBoundingClientRect();
                const style = window.getComputedStyle(a);
                return rect.width > 0 &&
                       rect.height > 0 &&
                       style.display !== 'none' &&
                       style.visibility !== 'hidden' &&
                       style.opacity !== '0';
            });
            if (!link) return null;
            const rect = link.getBoundingClientRect();
            return {
                x:    rect.left + rect.width  / 2,
                y:    rect.top  + rect.height / 2,
                href: link.href,
            };
        }""")

        if not box or box['x'] == 0 or box['y'] == 0:
            log.info("  Invalid coordinates — navigating directly")
            page.goto(found, timeout=15000, wait_until='domcontentloaded')
            return True

        log.info(f"  Coordinates: ({box['x']:.0f}, {box['y']:.0f})")

        target_x = box['x']
        target_y = box['y']
        current_x, current_y = 200, 150
        steps = 25

        for i in range(steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)
            x = current_x + (target_x - current_x) * t
            y = current_y + (target_y - current_y) * t
            page.mouse.move(x, y)
            page.wait_for_timeout(8)

        page.wait_for_timeout(120)
        page.mouse.click(target_x, target_y)

        # wait for navigation to complete — /latest is a static tag page so
        # networkidle is safe here
        page.wait_for_load_state('networkidle', timeout=15000)
        page.wait_for_timeout(500)
        log.info(f"  ✅ Clicked — now at: {page.url}")
        return True

    except Exception as e:
        log.debug(f"click_see_all_latest failed: {e}")
        log.info("  Exception — navigating directly to /latest")
        page.goto('https://www.bloomberg.com/latest',
                  timeout=15000, wait_until='domcontentloaded')
        return True


# ─────────────────────────────────────────────
# RETRY SCHEDULE
# ─────────────────────────────────────────────
def get_retry_interval_sec() -> int:
    hour = datetime.now(timezone.utc).hour
    if   0 <= hour < 6:  return 60 * 60
    elif 6 <= hour < 8:  return 15 * 60
    elif 8 <= hour < 22: return  5 * 60
    else:                return 30 * 60


# ─────────────────────────────────────────────
# SESSION
# ─────────────────────────────────────────────
def load_session() -> dict | None:
    try:
        if not SESSION_FILE.exists():
            log_error('No session file — run: python bloomberg_camoufox/setup.py')
            return None
        data = json.loads(SESSION_FILE.read_text())
        log.info(f"📂 Session loaded (saved {data.get('savedAt')})")
        return data
    except Exception as e:
        log_error(f'Session load failed: {e}')
        return None


def save_session_cookies(context):
    try:
        session = load_session()
        if session:
            session['cookies']       = context.cookies()
            session['lastRefreshed'] = datetime.now(timezone.utc).isoformat()
            SESSION_FILE.write_text(json.dumps(session, indent=2))
            log.debug('🔄 Session cookies refreshed')
    except Exception as e:
        log.debug(f'Cookie refresh failed: {e}')


# ─────────────────────────────────────────────
# NAVIGATION HELPERS
# ─────────────────────────────────────────────
def navigate_to_source(page, source_url: str, source_type: str, *, force: bool = False) -> bool:
    """Navigate to the canonical source page if needed."""
    try:
        if not force and page.url == source_url:
            return True

        if source_type == 'liveblog':
            page.goto(source_url, timeout=30000, wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
        else:
            page.goto(source_url, timeout=30000, wait_until='networkidle')
            page.wait_for_timeout(1000)

        # Human-like scroll after navigation — helps bypass bot detection
        # and ensures lazy-loaded content is triggered before scraping.
        page.mouse.wheel(0, random.randint(200, 600))
        page.wait_for_timeout(random.randint(800, 2000))
        return True
    except Exception as e:
        log_error(f"Navigation failed ({source_type}): {e}")
        return False


def restore_source_page(page, source_url: str | None, source_type: str | None) -> bool:
    """Return the visible browser to the current valid monitored source."""
    if not source_url or not source_type:
        return False
    log.info(f"↩️ Returning to current source: [{source_type}] {source_url}")
    return navigate_to_source(page, source_url, source_type, force=True)


def set_current_source(source_type: str, source_url: str, *, status: str = 'streaming'):
    update_state(
        status=status,
        source_type=source_type,
        source_url=source_url,
        last_valid_source_type=source_type,
        last_valid_source_url=source_url,
    )


# ─────────────────────────────────────────────
# HOMEPAGE SCAN
# ─────────────────────────────────────────────
HISTORY_FILE = BASE_DIR / 'liveblog_history.json'


def _record_liveblogs(liveblogs: list, date: str):
    """Append newly seen liveblog URLs to history file."""
    try:
        history = []
        if HISTORY_FILE.exists():
            history = json.loads(HISTORY_FILE.read_text())

        seen_urls = {h['url'] for h in history}
        now = datetime.now(timezone.utc).isoformat()

        new_entries = [
            {
                'date':     date,
                'url':      lb['href'],
                'text':     lb['text'],
                'found_at': now,
            }
            for lb in liveblogs
            if lb['href'] not in seen_urls
        ]

        if new_entries:
            history.extend(new_entries)
            HISTORY_FILE.write_text(json.dumps(history, indent=2))
            for e in new_entries:
                log.info(f"📚 New liveblog recorded: {e['text']} → {e['url']}")

    except Exception as e:
        log.debug(f"Liveblog history write failed: {e}")


def _pick_liveblog_candidate(all_liveblogs: list, date: str) -> str | None:
    """Prefer today, then yesterday, while skipping URLs on cooldown."""
    candidates = []

    today_matches = [lb['href'] for lb in all_liveblogs if date in lb['href']]
    if today_matches:
        candidates.extend(today_matches)

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    yesterday_matches = [lb['href'] for lb in all_liveblogs if yesterday in lb['href']]
    for href in yesterday_matches:
        if href not in candidates:
            candidates.append(href)

    for href in candidates:
        on_cooldown, reason = is_liveblog_on_cooldown(href)
        if on_cooldown:
            log.info(f"🧊 Skipping cooled-down liveblog: {href} ({reason or 'recently ended'})")
            continue
        return href

    return None


def find_liveblog_on_homepage(page, date: str) -> str | None:
    try:
        log.info("🏠 Opening Bloomberg homepage...")
        page.goto(
            'https://www.bloomberg.com',
            timeout=20000,
            wait_until='domcontentloaded',
        )
        page.wait_for_timeout(2000)

        all_liveblogs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a'))
                .filter(a => a.href?.includes('live-blog'))
                .map(a => ({
                    href: a.href,
                    text: a.textContent?.trim()?.slice(0, 80),
                }));
        }""")

        if all_liveblogs:
            log.info(f"🔍 All liveblogs on homepage ({len(all_liveblogs)}):")
            _record_liveblogs(all_liveblogs, date)
            for lb in all_liveblogs:
                log.info(f"   [{lb['text']}] → {lb['href']}")
        else:
            log.info("🔍 No liveblogs found on homepage at all")

        link = _pick_liveblog_candidate(all_liveblogs, date)

        if link:
            if date in link:
                log.info(f"✅ Today's liveblog selected: {link}")
            else:
                log.info(f"📰 Using yesterday's liveblog — still featured on homepage: {link}")
            return link

        log.info("📰 No eligible liveblog found on homepage")
        return None

    except Exception as e:
        log_error(f"Homepage scan failed: {e}")
        return None


# ─────────────────────────────────────────────
# TAG PICKER
# ─────────────────────────────────────────────
def pick_best_tag(page) -> tuple[str | None, str | None]:
    try:
        log.info("👆 Clicking 'See all latest'...")
        click_see_all_latest(page)
        page.wait_for_timeout(3000)

        tags = page.evaluate("""() => {
            return Array.from(
                document.querySelectorAll('[data-testid="link-pill"]')
            ).map(a => ({
                name: a.querySelector('span')?.textContent?.trim()
                      || a.textContent?.trim(),
                url:  a.href,
            })).filter(t => t.name && t.url);
        }""")

        if not tags:
            log.warning("⚠️  No In Focus tags found")
            return None, None

        log.info(f"🏷️  {len(tags)} tags: {[t['name'] for t in tags[:8]]}")

        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            log.warning("No ANTHROPIC_API_KEY — keyword fallback")
            return _keyword_pick_tag(tags)

        client   = anthropic.Anthropic(api_key=api_key)
        tags_txt = '\n'.join(f"- {t['name']}: {t['url']}" for t in tags)

        response = client.messages.create(
            model=CONFIG['haiku_model'],
            max_tokens=50,
            messages=[{
                'role': 'user',
                'content': (
                    f"Bloomberg In Focus tags:\n{tags_txt}\n\n"
                    f"Which ONE tag is most relevant to: "
                    f"Iran war, oil prices, Houthi attacks, "
                    f"Strait of Hormuz, Red Sea shipping, OPEC?\n\n"
                    f"Reply with ONLY the exact tag name. Nothing else."
                ),
            }],
        )

        chosen_name = response.content[0].text.strip()
        log.info(f"🤖 Haiku chose: '{chosen_name}'")

        chosen = next(
            (t for t in tags if t['name'].lower() == chosen_name.lower()), None)
        if not chosen:
            chosen = next(
                (t for t in tags if chosen_name.lower() in t['name'].lower()), None)
        if not chosen:
            name, url = _keyword_pick_tag(tags)
            chosen = next((t for t in tags if t['url'] == url), None)
        if not chosen:
            chosen = tags[0]

        log.info(f"🎯 Selected: {chosen['name']} → {chosen['url']}")

        slug = chosen['url'].split('/latest/')[-1].rstrip('/')
        clicked_tag = human_click(
            page, f'[data-testid="link-pill"][href*="{slug}"]'
        )

        if clicked_tag:
            page.wait_for_load_state('domcontentloaded', timeout=15000)
            page.wait_for_timeout(1500)
            log.info(f"🖱️  Navigated to tag page: {page.url}")
        else:
            url = chosen['url']
            if url.startswith('/'):
                url = f"https://www.bloomberg.com{url}"
            page.goto(url, timeout=15000, wait_until='domcontentloaded')

        return chosen['name'], chosen['url']

    except Exception as e:
        log_error(f"Tag picker failed: {e}")
        return None, None


def _keyword_pick_tag(tags: list) -> tuple[str | None, str | None]:
    for tag in tags:
        if any(kw in tag['name'].lower() for kw in CONFIG['relevance_keywords']):
            return tag['name'], tag['url']
    return (tags[0]['name'], tags[0]['url']) if tags else (None, None)


# ─────────────────────────────────────────────
# MASTER SOURCE FINDER
# ─────────────────────────────────────────────
def get_or_find_source(page, date: str) -> tuple[str, str] | tuple[None, None]:
    """
    Full human browsing flow.
    AI cost: 0 if liveblog found or tag saved, 1x Haiku otherwise.
    """
    liveblog = find_liveblog_on_homepage(page, date)
    if liveblog:
        return 'liveblog', liveblog

    with state_lock:
        saved_url  = state.get('latest_tag_url')
        saved_name = state.get('latest_tag_name')
        saved_date = state.get('tag_set_date')

    if saved_url and saved_date == date:
        log.info(f"📌 Using saved tag: {saved_name} → {saved_url}")
        url = saved_url if not saved_url.startswith('/') \
              else f"https://www.bloomberg.com{saved_url}"
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        page.wait_for_timeout(1000)
        return 'latest_tag', saved_url

    tag_name, tag_url = pick_best_tag(page)

    if tag_url:
        update_state(
            latest_tag_url=tag_url,
            latest_tag_name=tag_name,
            tag_set_date=date,
        )
        return 'latest_tag', tag_url

    log.warning("⚠️  Could not find any source")
    return None, None


# ─────────────────────────────────────────────
# SCRAPERS
# ─────────────────────────────────────────────
def scrape_page(page, source_url: str, source_type: str) -> list[dict]:
    try:
        if not navigate_to_source(page, source_url, source_type):
            return []

        if source_type == 'liveblog':
            return _scrape_liveblog(page)
        return _scrape_latest_tag(page)

    except Exception as e:
        log_error(f"Scrape failed ({source_type}): {e}")
        return []


def _check_liveblog_still_active(page) -> tuple[bool, str]:
    """
    Returns (is_active, last_updated_text)
    - is_active = False if badge != 'Live' OR last updated > 1hr ago
    """
    try:
        result = page.evaluate("""() => {
            const badge = document.querySelector('[class*="LiveblogStatus_badge"]');
            const badgeText = badge?.textContent?.trim() || '';

            const timeEl = document.querySelector('[class*="LiveblogStatus_timestamp"] time');
            const datetime = timeEl?.getAttribute('datetime') || '';
            const updatedText = timeEl?.textContent?.trim() || '';

            return { badgeText, datetime, updatedText };
        }""")

        # badge must say "Live"
        if result['badgeText'].lower() != 'live':
            return False, result['badgeText']

        # check datetime freshness
        if result['datetime']:
            last_updated = datetime.fromisoformat(result['datetime'].replace('Z', '+00:00'))
            mins_ago = (datetime.now(timezone.utc) - last_updated).total_seconds() / 60
            if mins_ago >= 60:
                return False, f"last updated {mins_ago:.0f}min ago"

        return True, result['updatedText']

    except Exception:
        return True, ''


def _scrape_liveblog(page) -> list[dict]:
    scraped_at = datetime.now(timezone.utc)

    raw = page.evaluate("""() => {
        return Array.from(
            document.querySelectorAll('.LiveblogPost_post__XXK17')
        ).map(el => {
            const id      = el.id || '';
            const bodyEl  = el.querySelector('.LiveblogPostBody_body__VtWJ_');
            const byline  = el.querySelector('[class*="LiveblogPostHeader_byline"]');

            // Match both LiveblogPostHeader_timestamp__ and
            // LiveblogPostHeader_recentTimestamp__ (used for very fresh posts)
            const timeEl  = el.querySelector('[class*="Timestamp"] time[datetime]');

            return {
                id:            id,
                title:         bodyEl?.textContent?.trim()?.slice(0, 300) || '',
                body:          bodyEl?.textContent?.trim()?.slice(0, 1000) || '',
                timestamp_raw: timeEl?.getAttribute('datetime') || '',
                url:           window.location.href.split('#')[0] + (id ? '#' + id : ''),
                author:        byline?.textContent?.replace('By ', '').trim() || '',
                source:        'Bloomberg Liveblog',
                source_type:   'liveblog',
            };
        }).filter(p => p.body);
    }""") or []

    for item in raw:
        item['timestamp_approx'] = item['timestamp_raw'] or scraped_at.isoformat()

    return raw


def _scrape_latest_tag(page) -> list[dict]:
    scraped_at = datetime.now(timezone.utc)

    try:
        page.wait_for_selector('.styles_itemContainer__t2ZQc', timeout=10000)
    except Exception:
        pass

    raw = page.evaluate("""() => {
        return Array.from(
            document.querySelectorAll('.styles_itemContainer__t2ZQc')
        ).map(el => {
            const linkEl  = el.querySelector('a.styles_itemLink__VgyXJ');
            const titleEl = el.querySelector('[data-testid="headline"] span');
            const timeEl  = el.querySelector('time');

            const title = titleEl?.textContent?.trim() || '';
            const href  = linkEl?.href || '';
            const ts    = timeEl?.textContent?.trim() || '';

            if (!title || !href) return null;

            return {
                id:            href,
                title:         title,
                timestamp_raw: ts,
                url:           href,
                source:        'Bloomberg Latest',
                source_type:   'latest_tag',
            };
        }).filter(Boolean);
    }""") or []

    for item in raw:
        item['timestamp_approx'] = parse_relative_time(
            item.get('timestamp_raw', ''), scraped_at)

    return raw


# ─────────────────────────────────────────────
# PAGE RECYCLER
# ─────────────────────────────────────────────
def recycle_page(context, session: dict, source_url: str | None = None, source_type: str | None = None):
    """
    Close the current page and open a fresh one.
    Kills the renderer process so the OS reclaims its heap.
    Much cheaper than restarting the whole browser.

    Reopens the current valid source so visible mode remains meaningful.
    """
    global _page
    try:
        log.info("♻️  Recycling page to free renderer memory...")
        _page.close()
    except Exception as e:
        log.debug(f"Page close error (ignored): {e}")

    _page = context.new_page()
    context.add_cookies(session['cookies'])

    if source_url and source_type:
        try:
            navigate_to_source(_page, source_url, source_type, force=True)
        except Exception as e:
            log.warning(f"Post-recycle navigation failed: {e}")

    log.info(f"♻️  Fresh page ready at: {_page.url}")
    gc.collect()


# ─────────────────────────────────────────────
# SOURCE RESOLUTION AFTER LIVEBLOG END
# ─────────────────────────────────────────────
def resolve_source_after_liveblog_end(page, date: str) -> tuple[str | None, str | None]:
    """
    Liveblog just ended. Do NOT return to that ended liveblog.
    Find the best currently valid source:
      1) new eligible homepage liveblog
      2) today's saved latest tag
      3) new best tag
    """
    liveblog = find_liveblog_on_homepage(page, date)
    if liveblog:
        return 'liveblog', liveblog

    with state_lock:
        saved_url  = state.get('latest_tag_url')
        saved_name = state.get('latest_tag_name')
        saved_date = state.get('tag_set_date')

    if saved_url and saved_date == date:
        log.info(f"📌 Falling back to saved tag after liveblog end: {saved_name} → {saved_url}")
        url = saved_url if not saved_url.startswith('/') \
              else f"https://www.bloomberg.com{saved_url}"
        page.goto(url, timeout=15000, wait_until='domcontentloaded')
        page.wait_for_timeout(1000)
        return 'latest_tag', saved_url

    tag_name, tag_url = pick_best_tag(page)
    if tag_url:
        update_state(
            latest_tag_url=tag_url,
            latest_tag_name=tag_name,
            tag_set_date=date,
        )
        return 'latest_tag', tag_url

    return None, None


# ─────────────────────────────────────────────
# MAIN BROWSER LOOP
# ─────────────────────────────────────────────
def browser_loop():
    global _page, _context

    try:
        session = load_session()
        if not session:
            update_state(status='auth_failed')
            return

        log.info(f"🦊 Starting Camoufox (headless={HEADLESS})...")

        with Camoufox(headless=HEADLESS) as browser:
            _context = browser.new_context()
            _context.add_cookies(session['cookies'])
            _page = _context.new_page()

            log.info("🔐 Verifying session...")
            _page.goto('https://www.bloomberg.com',
                       timeout=15000, wait_until='domcontentloaded')
            cookies  = _context.cookies()
            has_auth = any(c['name'] in ('_user-token', 'session_key') for c in cookies)
            log.info("✅ Session valid" if has_auth else "⚠️  Auth cookies not found")

            scrape_cycle = 0

            while True:
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

                with state_lock:
                    stored_date = state.get('date')

                if stored_date and stored_date != today:
                    log.info(f"📅 New day ({today}) — resetting source")
                    update_state(
                        date=None,
                        source_type=None,
                        source_url=None,
                        status='searching',
                        last_valid_source_url=None,
                        last_valid_source_type=None,
                    )

                with state_lock:
                    source_url  = state.get('source_url')
                    source_type = state.get('source_type')

                if not source_url:
                    s_type, s_url = get_or_find_source(_page, today)

                    if s_url:
                        set_current_source(s_type, s_url, status='streaming')
                        source_type = s_type
                        source_url  = s_url
                    
                        updates = {'date': today}
                        if s_type == 'latest_tag':
                            # We just checked the homepage during source discovery,
                            # so don't immediately do a second homepage probe.
                            updates['last_liveblog_check'] = datetime.now(timezone.utc).isoformat()
                    
                        update_state(**updates)
                    
                        log.info(f"📡 Source: [{s_type}] {s_url}")
                        signal_mm('ready', {
                            'source_type': s_type,
                            'source_url':  s_url,
                            'status':      'ready',
                        })
                    else:
                        retry_sec  = get_retry_interval_sec()
                        retry_time = datetime.fromtimestamp(
                            time.time() + retry_sec, tz=timezone.utc
                        ).isoformat()
                        update_state(status='no_source', next_retry=retry_time)
                        log.info("📭 No source — notifying MM, waiting for /refresh")
                        signal_mm('no_source', {'next_retry': retry_time})
                        _refresh_event.wait()
                        _refresh_event.clear()
                        continue

                try:
                    posts     = scrape_page(_page, source_url, source_type)
                    new_posts = []
                    for p in posts:
                        if add_post(p):
                            new_posts.append(p)

                    update_state(
                        status='streaming',
                        last_refresh=datetime.now(timezone.utc).isoformat(),
                        next_retry=None,
                    )

                    log.info(f"🔄 [{source_type}] {len(new_posts)} new / {len(posts)} total")

                    for post in new_posts:
                        signal_mm('post', post)

                    save_session_cookies(_context)

                    if source_type == 'liveblog':
                        is_active, updated_text = _check_liveblog_still_active(_page)
                        if not is_active:
                            log.info(f"🔚 Liveblog inactive ({updated_text})")
                            mark_liveblog_ended(source_url, updated_text)

                            signal_mm('liveblog_ended', {
                                'source_url': source_url,
                                'msg': f'Liveblog inactive: {updated_text}',
                            })

                            new_type, new_url = resolve_source_after_liveblog_end(_page, today)
                            if new_url:
                                log.info(f"🔁 Switched after ended liveblog → [{new_type}] {new_url}")
                                set_current_source(new_type, new_url, status='streaming')
                                update_state(date=today)
                                signal_mm('ready', {
                                    'source_type': new_type,
                                    'source_url':  new_url,
                                    'status':      'switched_after_liveblog_end',
                                })
                            else:
                                retry_sec  = get_retry_interval_sec()
                                retry_time = datetime.fromtimestamp(
                                    time.time() + retry_sec, tz=timezone.utc
                                ).isoformat()
                                update_state(
                                    source_type=None,
                                    source_url=None,
                                    status='no_source',
                                    next_retry=retry_time,
                                )
                                signal_mm('no_source', {'next_retry': retry_time})
                            continue

                    save_session_cookies(_context)

                    # dynamic liveblog check when on latest_tag —
                    # Bloomberg may start a liveblog at any time during market hours
                    if source_type == 'latest_tag':
                        with state_lock:
                            last_liveblog_check = state.get('last_liveblog_check')

                        should_check   = True
                        hour           = datetime.now(timezone.utc).hour
                        check_interval = 15 if 6 <= hour < 20 else 60

                        if last_liveblog_check:
                            try:
                                last_dt      = datetime.fromisoformat(last_liveblog_check)
                                mins_since   = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                                should_check = mins_since >= check_interval
                            except Exception:
                                pass

                        if should_check:
                            log.info(f"🔍 Liveblog check (every {check_interval}min)...")
                            update_state(last_liveblog_check=datetime.now(timezone.utc).isoformat())

                            origin_url = source_url
                            origin_type = source_type

                            liveblog = find_liveblog_on_homepage(_page, today)
                            if liveblog:
                                log.info("🎉 Liveblog appeared — switching!")
                                set_current_source('liveblog', liveblog, status='streaming')
                                signal_mm('ready', {
                                    'source_type': 'liveblog',
                                    'source_url':  liveblog,
                                    'status':      'switched',
                                })
                            else:
                                restore_source_page(_page, origin_url, origin_type)

                    scrape_cycle += 1
                    if scrape_cycle % PAGE_RECYCLE_CYCLES == 0:
                        with state_lock:
                            recycle_url = state.get('source_url')
                            recycle_type = state.get('source_type')
                        recycle_page(_context, session, recycle_url, recycle_type)

                except Exception as e:
                    log_error(f"Scrape cycle error: {e}")
                    update_state(status='error')
                    signal_mm('error', {'msg': str(e)})

                log.info("⏳ Waiting for next /refresh from MonsieurMarket...")
                _refresh_event.wait()
                _refresh_event.clear()

    except Exception as e:
        log.error(f"🔴 BrowserLoop crashed: {e}")
        signal_mm('error', {'msg': f"BrowserLoop crashed: {e}"})


# ─────────────────────────────────────────────
# FLASK API
# ─────────────────────────────────────────────
app = Flask(__name__)


@app.route('/health')
def health():
    with state_lock:
        return jsonify({
            'status':       state['status'],
            'date':         state['date'],
            'source_type':  state['source_type'],
            'source_url':   state['source_url'],
            'post_count':   state['post_count'],
            'last_refresh': state['last_refresh'],
            'next_retry':   state['next_retry'],
            'is_today':     state['date'] == datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            'latest_tag':   state.get('latest_tag_name'),
            'ended_liveblogs': state.get('ended_liveblogs', {}),
            'errors':       state['errors'][-3:],
        })


@app.route('/posts')
def posts():
    since = float(request.args.get('since', 0))
    with state_lock:
        filtered = []
        for p in state['posts']:
            try:
                captured = datetime.fromisoformat(
                    p.get('captured_at', '2000-01-01T00:00:00+00:00')
                ).timestamp()
                if captured > since:
                    filtered.append(p)
            except Exception:
                filtered.append(p)

    return jsonify({
        'status':      state['status'],
        'source_type': state['source_type'],
        'posts':       filtered,
        'total':       state['post_count'],
    })


@app.route('/latest')
def latest():
    with state_lock:
        post = state['posts'][0] if state['posts'] else None
    return jsonify(post)


@app.route('/refresh')
def refresh():
    """Immediate scrape — called by MonsieurMarket on price spike."""
    global _refresh_in_progress

    with _refresh_lock:
        if _refresh_in_progress:
            return jsonify({
                'status':    'busy',
                'message':   'Refresh already in progress',
                'new_posts': [],
                'count':     0,
            }), 429
        _refresh_in_progress = True

    try:
        log.info("⚡ On-demand refresh triggered")

        with state_lock:
            status         = state['status']
            before_count   = state['post_count']
            before_refresh = state.get('last_refresh')

        if status == 'no_source':
            return jsonify({
                'status':    'no_source',
                'message':   'No Bloomberg source found today',
                'new_posts': [],
                'count':     0,
            })

        if status == 'auth_failed':
            return jsonify({
                'status':    'auth_failed',
                'message':   'Session expired — run setup.py',
                'new_posts': [],
                'count':     0,
            })

        _refresh_event.set()

        deadline = time.time() + 30
        scraper_responded = False
        while time.time() < deadline:
            time.sleep(0.5)
            with state_lock:
                if state.get('last_refresh') and state.get('last_refresh') != before_refresh:
                    log.info("⚡ Refresh complete — scraper responded")
                    scraper_responded = True
                    break

        if not scraper_responded:
            log.warning("⚡ Refresh timed out — scraper did not respond in 30s ⚠️")

        with state_lock:
            new_count = max(0, state['post_count'] - before_count)
            new_posts = state['posts'][:new_count]
            status    = state['status']

        return jsonify({
            'status':    status,
            'new_posts': new_posts,
            'count':     len(new_posts),
        })

    finally:
        with _refresh_lock:
            _refresh_in_progress = False


# ─────────────────────────────────────────────
# BOOT
# ─────────────────────────────────────────────
if __name__ == '__main__':
    log.info("🎩 Bloomberg Camoufox Monitor starting...")
    log.info(f"   Headless: {HEADLESS}")
    log.info(f"   Session:  {SESSION_FILE}")
    log.info(f"   Feed:     {FEED_FILE}")
    log.info(f"   State:    {STATE_FILE}")
    log.info(f"   Page recycled every {PAGE_RECYCLE_CYCLES} cycles "
             f"(~{PAGE_RECYCLE_CYCLES * CONFIG['poll_interval_min']} min)")
    log.info(f"   Ended liveblog cooldown: {CONFIG['ended_liveblog_cooldown_min']} min")

    if not SESSION_FILE.exists():
        log.error("No session file — run: python bloomberg_camoufox/setup.py")
        sys.exit(1)

    t = threading.Thread(target=browser_loop, name='BrowserLoop', daemon=True)
    t.start()

    log.info(f"🌐 API on http://localhost:{CONFIG['port']}")
    log.info("   GET /health")
    log.info("   GET /posts?since=0")
    log.info("   GET /latest")
    log.info("   GET /refresh")

    app.run(
        host='0.0.0.0',
        port=CONFIG['port'],
        debug=False,
        use_reloader=False,
    )