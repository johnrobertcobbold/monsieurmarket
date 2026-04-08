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

Memory fix:
    - Page object is closed and recreated every PAGE_RECYCLE_CYCLES scrape
      cycles. This kills the renderer process and lets the OS reclaim its
      heap. page.reload() does NOT do this — the process stays alive.

Signal flow:
    monitor → POST /signal to MonsieurMarket on:
        - startup ready (source found)
        - no_source (MM decides when to retry)
        - new posts found after /refresh
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

    signal_type: 'ready' | 'no_source' | 'post' | 'error'
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
                'latest_tag_url':  saved.get('latest_tag_url'),
                'latest_tag_name': saved.get('latest_tag_name'),
                'tag_set_date':    saved.get('tag_set_date'),
            }
    except Exception as e:
        log.debug(f"Persistent state load failed: {e}")
    return {}


def save_persistent_state():
    try:
        with state_lock:
            to_save = {
                'latest_tag_url':  state.get('latest_tag_url'),
                'latest_tag_name': state.get('latest_tag_name'),
                'tag_set_date':    state.get('tag_set_date'),
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
    'status':          'starting',
    'date':            None,
    'source_type':     None,
    'source_url':      None,
    'posts':           load_persistent_posts(),
    'post_count':      0,
    'last_post_ts':    0,
    'last_refresh':    None,
    'latest_tag_url':  None,
    'latest_tag_name': None,
    'tag_set_date':    None,
    'next_retry':      None,
    'errors':          [],
}

# restore persisted tag fields on startup
state.update(load_persistent_state())

state_lock           = threading.Lock()
_refresh_event       = threading.Event()
_refresh_lock        = threading.Lock()
_refresh_in_progress = False
_page                = None
_context             = None


def update_state(**kwargs):
    with state_lock:
        state.update(kwargs)
    save_feed()


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
# HOMEPAGE SCAN
# ─────────────────────────────────────────────
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

        link = next(
            (lb['href'] for lb in all_liveblogs if date in lb['href']),
            None
        )

        if link:
            log.info(f"✅ Today's liveblog selected: {link}")
        else:
            log.info(f"📰 No liveblog for {date} on homepage")

        return link

    except Exception as e:
        log_error(f"Homepage scan failed: {e}")
        return None


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
        return 'latest_tag', saved_url

    tag_name, tag_url = pick_best_tag(page)

    if tag_url:
        update_state(
            latest_tag_url=tag_url,
            latest_tag_name=tag_name,
            tag_set_date=date,
        )
        save_persistent_state()
        return 'latest_tag', tag_url

    log.warning("⚠️  Could not find any source")
    return None, None


# ─────────────────────────────────────────────
# SCRAPERS
# ─────────────────────────────────────────────
def scrape_page(page, source_url: str, source_type: str) -> list[dict]:
    try:
        if page.url != source_url:
            if source_type == 'liveblog':
                # Liveblogs have constant network activity (SSE streams, analytics
                # pings, ad beacons) so networkidle never fires — use domcontentloaded
                # and a fixed pause to let the initial posts render via JS.
                page.goto(source_url, timeout=30000, wait_until='domcontentloaded')
                page.wait_for_timeout(3000)
            else:
                # Static tag pages go quiet after assets load, so networkidle is
                # safe and gives _scrape_latest_tag a fully settled DOM to query.
                # The selector wait inside _scrape_latest_tag adds a second layer
                # of precision on top of this.
                page.goto(source_url, timeout=30000, wait_until='networkidle')
                page.wait_for_timeout(1000)

            # Human-like scroll after navigation — helps bypass bot detection
            # and ensures lazy-loaded content is triggered before scraping.
            page.mouse.wheel(0, random.randint(200, 600))
            page.wait_for_timeout(random.randint(800, 2000))

        # If page.url already matches source_url (normal polling cycle, or after
        # recycle_page sets url to about:blank and the previous block re-navigated),
        # skip navigation entirely and scrape the live DOM directly.
        if source_type == 'liveblog':
            return _scrape_liveblog(page)
        else:
            return _scrape_latest_tag(page)

    except Exception as e:
        log_error(f"Scrape failed ({source_type}): {e}")
        return []


def _scrape_liveblog(page) -> list[dict]:
    scraped_at = datetime.now(timezone.utc)

    # Check for "Live ended" badge before scraping posts
    live_ended = page.evaluate("""() => {
        const badges = Array.from(document.querySelectorAll('[class*="LiveblogStatus_badge"]'));
        return badges.some(b => b.textContent?.toLowerCase().includes('live ended'));
    }""") or False

    if live_ended:
        log.info("🔚 Liveblog 'Live ended' badge detected")

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
                live_ended:    False,
            };
        }).filter(p => p.body);
    }""") or []

    for item in raw:
        item['timestamp_approx'] = item['timestamp_raw'] or scraped_at.isoformat()

    # Tag the first post with live_ended so browser_loop can detect it
    if live_ended and raw:
        raw[0]['live_ended'] = True

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
def recycle_page(context, session: dict):
    """
    Close the current page and open a fresh one.
    Kills the renderer process so the OS reclaims its heap.
    Much cheaper than restarting the whole browser.
    """
    global _page
    try:
        log.info("♻️  Recycling page to free renderer memory...")
        _page.close()
    except Exception as e:
        log.debug(f"Page close error (ignored): {e}")

    _page = context.new_page()
    # Re-inject session cookies so we stay authenticated
    context.add_cookies(session['cookies'])
    log.info("♻️  Fresh page ready")
    gc.collect()


# ─────────────────────────────────────────────
# MAIN BROWSER LOOP
# ─────────────────────────────────────────────
def browser_loop():
    global _page, _context

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

            # new day — reset source so we search again tomorrow
            with state_lock:
                stored_date = state.get('date')

            if stored_date and stored_date != today:
                log.info(f"📅 New day ({today}) — resetting source")
                update_state(date=None, source_type=None,
                             source_url=None, status='searching')

            # find source if needed
            with state_lock:
                source_url  = state.get('source_url')
                source_type = state.get('source_type')

            if not source_url:
                s_type, s_url = get_or_find_source(_page, today)

                if s_url:
                    update_state(
                        status='streaming',
                        date=today,
                        source_type=s_type,
                        source_url=s_url,
                    )
                    source_type = s_type
                    source_url  = s_url
                    log.info(f"📡 Source: [{s_type}] {s_url}")

                    # Tell MM we're ready — it will start its refresh timer
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
                    log.info(f"📭 No source — notifying MM, waiting for /refresh")

                    # Tell MM we have no source — it decides when to retry
                    signal_mm('no_source', {'next_retry': retry_time})

                    # Wait indefinitely for MM to trigger a /refresh
                    _refresh_event.wait()
                    _refresh_event.clear()
                    continue

            # scrape
            try:
                posts     = scrape_page(_page, source_url, source_type)
                new_posts = []
                for p in posts:
                    if add_post(p):
                        new_posts.append(p)

                update_state(
                    status='streaming',
                    last_refresh=datetime.now(timezone.utc).isoformat(),
                )

                log.info(f"🔄 [{source_type}] {len(new_posts)} new / {len(posts)} total")

                # Signal MM for each new post — MM decides what to do with them
                for post in new_posts:
                    signal_mm('post', post)

                save_session_cookies(_context)

                # Check if liveblog has ended — reset source so we go back to homepage
                if source_type == 'liveblog':
                    live_ended = any(p.get('live_ended') for p in posts)
                    if live_ended:
                        log.info("🔚 Liveblog ended — resetting source, will scan homepage next cycle")
                        signal_mm('liveblog_ended', {
                            'source_url': source_url,
                            'msg':        'Liveblog ended — switching to tag source',
                        })
                        update_state(
                            source_type=None,
                            source_url=None,
                            status='searching',
                        )
                        continue

                save_session_cookies(_context)

                # hourly liveblog check when on latest_tag —
                # Bloomberg may start a liveblog mid-morning
                if source_type == 'latest_tag':
                    with state_lock:
                        last_refresh = state.get('last_refresh')

                    should_check = True
                    hour            = datetime.now(timezone.utc).hour
                    check_interval  = 15 if 6 <= hour < 20 else 60
                    if last_refresh:
                        try:
                            last_dt         = datetime.fromisoformat(last_refresh)
                            mins_since      = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                            should_check    = mins_since >= check_interval
                        except Exception:
                            pass

                    if should_check:
                        log.info(f"🔍 Liveblog check (every {check_interval}min)...")
                        liveblog = find_liveblog_on_homepage(_page, today)
                        if liveblog:
                            log.info("🎉 Liveblog appeared — switching!")
                            update_state(source_type='liveblog', source_url=liveblog)
                            # Tell MM about the source switch
                            signal_mm('ready', {
                                'source_type': 'liveblog',
                                'source_url':  liveblog,
                                'status':      'switched',
                            })

                # recycle page every N cycles to free renderer memory
                scrape_cycle += 1
                if scrape_cycle % PAGE_RECYCLE_CYCLES == 0:
                    recycle_page(_context, session)

            except Exception as e:
                log_error(f"Scrape cycle error: {e}")
                update_state(status='error')
                signal_mm('error', {'msg': str(e)})

            # No internal timer — wait for MM to trigger next /refresh
            log.info("⏳ Waiting for next /refresh from MonsieurMarket...")
            _refresh_event.wait()
            _refresh_event.clear()


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

    if not SESSION_FILE.exists():
        log.error("No session file — run: python bloomberg_camoufox/setup.py")
        sys.exit(1)

    t = threading.Thread(target=browser_loop, name='BrowserLoop', daemon=True)
    t.start()

    log.info(f"🌐 API on http://localhost:{CONFIG['port']}")
    log.info(f"   GET /health")
    log.info(f"   GET /posts?since=0")
    log.info(f"   GET /latest")
    log.info(f"   GET /refresh")

    app.run(
        host='0.0.0.0',
        port=CONFIG['port'],
        debug=False,
        use_reloader=False,
    )