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
    8. Poll every 15 min or on-demand via /refresh

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

state_lock     = threading.Lock()
_refresh_event = threading.Event()
_page          = None
_context       = None


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
        state['posts']      = state['posts'][:200]
        state['post_count'] = len(state['posts'])
        state['last_post_ts'] = time.time()

    save_feed()

    # show raw timestamp in log — "26 min ago" is more readable than full ISO
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

        # smooth mouse move
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

        # wait for navigation to complete
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


def find_liveblog_on_homepage(page, date: str) -> str | None:
    try:
        log.info("🏠 Opening Bloomberg homepage...")
        page.goto(
            'https://www.bloomberg.com',
            timeout=20000,
            wait_until='domcontentloaded',
        )
        page.wait_for_timeout(2000)

        # find ALL liveblogs on homepage, not just today's
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

        # now filter for today's date
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

        # only add URLs we haven't seen before
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
# STEP 2 — CLICK SEE ALL LATEST + PICK TAG
# ─────────────────────────────────────────────
def pick_best_tag(page) -> tuple[str | None, str | None]:
    try:
        log.info("👆 Clicking 'See all latest'...")
        click_see_all_latest(page)
        page.wait_for_timeout(3000)

        # scrape In Focus tags
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

        # Haiku picks best tag — ONE call, saved to disk
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

        # human click on chosen tag
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
    # 1. check homepage for liveblog
    liveblog = find_liveblog_on_homepage(page, date)
    if liveblog:
        return 'liveblog', liveblog

    # 2. saved tag for today?
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

    # 3. click through + one Haiku call
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
            page.goto(source_url, timeout=30000, wait_until='networkidle')
            page.wait_for_timeout(2000)

            # ── human: scroll down a bit after landing ──
            page.mouse.wheel(0, random.randint(200, 600))
            page.wait_for_timeout(random.randint(800, 2000))            

        if source_type == 'liveblog':
            return _scrape_liveblog(page)
        else:
            return _scrape_latest_tag(page)

    except Exception as e:
        log_error(f"Scrape failed ({source_type}): {e}")
        return []


def _scrape_liveblog(page) -> list[dict]:
    scraped_at = datetime.now(timezone.utc)

    raw = page.evaluate("""() => {
        const selectors = [
            '[data-testid="live-blog-post"]',
            '[class*="live-blog-post"]',
            '[class*="livePost"]',
            'article[data-id]',
        ];
        let els = [];
        for (const sel of selectors) {
            els = Array.from(document.querySelectorAll(sel));
            if (els.length > 0) break;
        }
        return els.map(el => {
            const id      = el.dataset?.id || el.id || el.dataset?.postId || '';
            const titleEl = el.querySelector('h2, h3, [class*="title"], [class*="headline"]');
            const bodyEl  = el.querySelector('[class*="body"], [class*="content"], p');
            const timeEl  = el.querySelector('time, [class*="timestamp"]');
            return {
                id:            id,
                title:         titleEl?.textContent?.trim() || '',
                body:          bodyEl?.textContent?.trim()?.slice(0, 500) || '',
                timestamp_raw: timeEl?.getAttribute('datetime') || timeEl?.textContent?.trim() || '',
                url:           window.location.href + (id ? '#' + id : ''),
                source:        'Bloomberg Liveblog',
                source_type:   'liveblog',
            };
        }).filter(p => p.title || p.body);
    }""") or []

    for item in raw:
        item['timestamp_approx'] = parse_relative_time(
            item.get('timestamp_raw', ''), scraped_at)

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

        while True:
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

            # new day check
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
                else:
                    retry_sec  = get_retry_interval_sec()
                    retry_time = datetime.fromtimestamp(
                        time.time() + retry_sec, tz=timezone.utc
                    ).isoformat()
                    update_state(status='no_source', next_retry=retry_time)
                    log.info(f"📭 No source — retrying in {retry_sec // 60} min")
                    _refresh_event.wait(timeout=retry_sec)
                    _refresh_event.clear()
                    continue

            # scrape
            try:
                posts     = scrape_page(_page, source_url, source_type)
                new_count = sum(1 for p in posts if add_post(p))

                update_state(
                    status='streaming',
                    last_refresh=datetime.now(timezone.utc).isoformat(),
                )

                log.info(f"🔄 [{source_type}] {new_count} new / {len(posts)} total")

                save_session_cookies(_context)

                # hourly liveblog check when on latest_tag
                if source_type == 'latest_tag':
                    with state_lock:
                        last_refresh = state.get('last_refresh')

                    should_check = True
                    if last_refresh:
                        try:
                            last_dt    = datetime.fromisoformat(last_refresh)
                            mins_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                            should_check = mins_since >= 60
                        except Exception:
                            pass

                    if should_check:
                        log.info("🔍 Hourly liveblog check...")
                        liveblog = find_liveblog_on_homepage(_page, today)
                        if liveblog:
                            log.info("🎉 Liveblog appeared — switching!")
                            update_state(source_type='liveblog', source_url=liveblog)

            except Exception as e:
                log_error(f"Scrape cycle error: {e}")
                update_state(status='error')

            jitter      = random.randint(-2, 4)  # -2 to +4 minutes
            poll_min    = CONFIG['poll_interval_min'] + jitter
            poll_sec    = poll_min * 60
            log.info(f"⏳ Next poll in {poll_min} min")
            _refresh_event.wait(timeout=poll_sec)


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
    log.info("⚡ On-demand refresh triggered")

    with state_lock:
        status       = state['status']
        before_count = state['post_count']

    if status == 'no_source':
        return jsonify({
            'status':    'no_source',
            'message':   'No Bloomberg source found today',
            'new_posts': [],
        })

    if status == 'auth_failed':
        return jsonify({
            'status':    'auth_failed',
            'message':   'Session expired — run setup.py',
            'new_posts': [],
        })

    _refresh_event.set()

    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.5)
        with state_lock:
            if state['last_refresh'] and state['post_count'] != before_count:
                break

    with state_lock:
        new_count = max(0, state['post_count'] - before_count)
        new_posts = state['posts'][:new_count]
        status    = state['status']

    return jsonify({
        'status':    status,
        'new_posts': new_posts,
        'count':     len(new_posts),
    })


# ─────────────────────────────────────────────
# BOOT
# ─────────────────────────────────────────────
if __name__ == '__main__':
    log.info("🎩 Bloomberg Camoufox Monitor starting...")
    log.info(f"   Headless: {HEADLESS}")
    log.info(f"   Session:  {SESSION_FILE}")
    log.info(f"   Feed:     {FEED_FILE}")
    log.info(f"   State:    {STATE_FILE}")

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