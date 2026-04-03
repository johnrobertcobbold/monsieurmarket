# bloomberg_watcher.py — drop in monsieurmarket root
"""
Bloomberg Watcher — connects monitor.py to MonsieurMarket.

Reads posts from the Bloomberg Camoufox monitor API (:3457)
and converts them to the same headline format as RSS feeds.

Usage in monsieur_market.py:
    from bloomberg_watcher import BloombergWatcher
    _bloomberg = BloombergWatcher()

    # in run_poll() after Step 1:
    bloomberg_headlines = _bloomberg.fetch_new_headlines()
    if bloomberg_headlines:
        all_headlines = bloomberg_headlines + all_headlines
"""

import time
import logging
import requests

log = logging.getLogger('MonsieurMarket')

BLOOMBERG_BRIDGE_URL = 'http://localhost:3457'

BLOOMBERG_KEYWORDS = [
    'Hormuz', 'Iran', 'Houthi', 'Brent', 'oil',
    'strike', 'missile', 'nuclear', 'Kharg',
    'Strait', 'tanker', 'OPEC', 'crude',
    'Yanbu', 'Aramco', 'Red Sea', 'Gulf',
    'attack', 'pipeline', 'refinery', 'embargo',
]


class BloombergWatcher:

    def __init__(self):
        self.last_seen_ts  = 0       # epoch — only fetch posts newer than this
        self._available    = None    # None = unchecked, True/False after first check
        self._last_check   = 0       # epoch of last availability check
        self._check_every  = 60      # re-check availability every 60s

    # ─────────────────────────────────────────────
    # AVAILABILITY CHECK
    # ─────────────────────────────────────────────
    def _check_available(self) -> bool:
        """
        Check if monitor bridge is running.
        Cached for 60s to avoid hammering localhost on every poll.
        """
        now = time.time()
        if now - self._last_check < self._check_every and self._available is not None:
            return self._available

        try:
            r = requests.get(f"{BLOOMBERG_BRIDGE_URL}/health", timeout=2)
            data = r.json()
            status = data.get('status', '')

            # not running or bad state
            if status in ('auth_failed', 'no_session'):
                log.warning(
                    f"Bloomberg monitor: {status} — "
                    f"run: python bloomberg_camoufox/setup.py"
                )
                self._available = False
            elif status in ('streaming', 'polling', 'connected'):
                self._available = True
            else:
                # starting, searching, no_source — not ready yet
                log.debug(f"Bloomberg monitor: not ready ({status})")
                self._available = False

        except requests.ConnectionError:
            log.debug("Bloomberg monitor not running — skipping")
            self._available = False
        except Exception as e:
            log.debug(f"Bloomberg health check failed: {e}")
            self._available = False

        self._last_check = now
        return self._available

    # ─────────────────────────────────────────────
    # FETCH
    # ─────────────────────────────────────────────
    def fetch_new_headlines(self) -> list[dict]:
        """
        Fetch new Bloomberg posts since last check.
        Returns list of headlines in RSS format ready for Haiku filter.
        Returns [] if monitor not running or no new posts.
        """
        if not self._check_available():
            return []

        try:
            r = requests.get(
                f"{BLOOMBERG_BRIDGE_URL}/posts",
                params={'since': self.last_seen_ts},
                timeout=5,
            )
            r.raise_for_status()
            data  = r.json()
            posts = data.get('posts', [])

            if not posts:
                return []

            # update last seen timestamp
            self.last_seen_ts = max(
                time.mktime(
                    __import__('datetime').datetime.fromisoformat(
                        p.get('captured_at', '2000-01-01T00:00:00+00:00')
                    ).timetuple()
                )
                for p in posts
            )

            # keyword filter — only pass relevant posts to Haiku
            relevant = [p for p in posts if self._is_relevant(p)]

            if relevant:
                log.info(
                    f"Bloomberg: {len(relevant)} relevant / "
                    f"{len(posts)} total new posts "
                    f"[{data.get('source_type', '?')}]"
                )

            return [self._to_headline(p) for p in relevant]

        except requests.ConnectionError:
            self._available = False
            log.debug("Bloomberg monitor went offline")
            return []
        except Exception as e:
            log.warning(f"Bloomberg fetch error: {e}")
            return []

    # ─────────────────────────────────────────────
    # ON-DEMAND REFRESH — called on price spike
    # ─────────────────────────────────────────────
    def refresh_now(self) -> list[dict]:
        """
        Trigger immediate Bloomberg scrape.
        Called by MonsieurMarket when Brent moves sharply.
        Returns fresh headlines.
        """
        if not self._check_available():
            return []

        try:
            log.info("Bloomberg: triggering on-demand refresh...")
            r = requests.get(
                f"{BLOOMBERG_BRIDGE_URL}/refresh",
                timeout=35,  # monitor takes up to 30s to scrape
            )
            r.raise_for_status()
            data      = r.json()
            new_posts = data.get('new_posts', [])

            if new_posts:
                log.info(f"Bloomberg on-demand: {len(new_posts)} fresh post(s)")

            relevant = [p for p in new_posts if self._is_relevant(p)]
            return [self._to_headline(p) for p in relevant]

        except Exception as e:
            log.warning(f"Bloomberg on-demand refresh failed: {e}")
            return []

    # ─────────────────────────────────────────────
    # KEYWORD FILTER
    # ─────────────────────────────────────────────
    def _is_relevant(self, post: dict) -> bool:
        """Quick keyword check before spending a Haiku call."""
        text = (
            post.get('title', '') + ' ' +
            post.get('body',  '')
        ).lower()
        return any(kw.lower() in text for kw in BLOOMBERG_KEYWORDS)

    # ─────────────────────────────────────────────
    # FORMAT CONVERSION
    # ─────────────────────────────────────────────
    def _to_headline(self, post: dict) -> dict:
        """
        Convert Bloomberg post to RSS headline format
        so it works with existing Haiku filter + Sonnet analysis.
        """
        return {
            'title':     post.get('title', 'Bloomberg update'),
            'url':       post.get('url', ''),
            'source':    'Bloomberg',
            'body':      post.get('body', ''),           # bonus vs RSS
            'timestamp': post.get('timestamp_approx') or
                         post.get('timestamp_raw', ''),
            'source_type': post.get('source_type', ''),  # liveblog or latest_tag
        }

    # ─────────────────────────────────────────────
    # STATUS — for startup message
    # ─────────────────────────────────────────────
    def get_status(self) -> str:
        try:
            r = requests.get(f"{BLOOMBERG_BRIDGE_URL}/health", timeout=2)
            data = r.json()
            status   = data.get('status', 'unknown')
            tag      = data.get('latest_tag', '')
            src_type = data.get('source_type', '')
            count    = data.get('post_count', 0)
            return (
                f"{status} | {src_type} | "
                f"{tag} | {count} posts"
            )
        except Exception:
            return 'not running'