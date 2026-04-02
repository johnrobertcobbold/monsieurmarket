"""
scheduled_event_watcher.py — MonsieurMarket
Watches for price moves around known scheduled events (speeches, OPEC, NFP, etc.)
Hot-reloadable from scheduled_events.json — no restart needed.

Drop into monsieur_market.py:
    from scheduled_event_watcher import ScheduledEventWatcher
    event_watcher = ScheduledEventWatcher(send_telegram_fn=send_telegram, anthropic_api_key="sk-...")

Then call on every Brent tick:
    event_watcher.on_tick(mid)
"""

import email.utils
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

log = logging.getLogger("MonsieurMarket.EventWatcher")

EVENTS_FILE = Path("scheduled_events.json")
RSS_FILE    = Path("rss_sources.json")
TRADES_DIR  = Path("trades")


def _load_events() -> list:
    """Load active events from JSON file. Returns [] on any error."""
    try:
        events = json.loads(EVENTS_FILE.read_text())
        return [e for e in events if e.get("active", True)]
    except FileNotFoundError:
        log.warning("scheduled_events.json not found — no events loaded")
        return []
    except json.JSONDecodeError as e:
        log.error(f"scheduled_events.json parse error: {e}")
        return []


def _load_rss_sources() -> list:
    """Load active RSS sources from rss_sources.json. Returns [] on any error."""
    try:
        sources = json.loads(RSS_FILE.read_text())
        return [s for s in sources if s.get("active", True)]
    except FileNotFoundError:
        log.warning("rss_sources.json not found — RSS disabled")
        return []
    except json.JSONDecodeError as e:
        log.error(f"rss_sources.json parse error: {e}")
        return []


def _fmt_price(price) -> str:
    """Safely format a price that may be None."""
    return f"{price:.2f}" if price is not None else "N/A"


def _parse_pub_date(pub_date_str: str, source_timezone: str) -> datetime | None:
    """
    Parse an RSS pubDate string into a UTC-aware datetime.
    Returns None if parsing fails — caller skips the item.

    Strategy:
    1. Try email.utils.parsedate_to_datetime (handles RFC 2822 with offset)
    2. Fallback: strip timezone suffix, parse naively, apply source_timezone offset
    3. If source_timezone is unrecognised → log warning, return None (skip feed)
    """
    if not pub_date_str:
        return None

    # Attempt 1 — standard RFC 2822 parse (handles -0500, GMT, etc.)
    try:
        dt = email.utils.parsedate_to_datetime(pub_date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Attempt 2 — fallback with source_timezone
    try:
        clean = re.sub(r'\s+[A-Z]{2,4}$|\s+[+-]\d{4}$', '', pub_date_str.strip())
        dt_naive = datetime.strptime(clean, "%a, %d %b %Y %H:%M:%S")

        offset_hours = 0.0
        tz_str = source_timezone.strip().upper()
        if tz_str == "UTC":
            offset_hours = 0.0
        else:
            match = re.match(r'UTC([+-])(\d+(?:\.\d+)?)', tz_str)
            if match:
                sign = 1 if match.group(1) == '+' else -1
                offset_hours = sign * float(match.group(2))
            else:
                log.warning(f"RSS: unrecognised timezone '{source_timezone}' — skipping feed")
                return None

        dt_utc = (dt_naive - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)
        return dt_utc

    except Exception as e:
        log.warning(f"RSS: failed to parse pubDate '{pub_date_str}': {e}")
        return None


class _EventState:
    """Runtime state for a single event — not persisted across restarts."""
    def __init__(self, event_id: str):
        self.event_id        = event_id
        self.armed           = False
        self.armed_at_utc    = None   # datetime when armed — used for RSS filtering
        self.reference_price = None
        self.triggered       = False
        self.trigger_time    = None
        self.trigger_price   = None
        self.confirmed       = False
        self.executed        = False
        self.window_expired  = False
        self.rss_done        = False  # True once RSS at-open fetch fired
        self.web_search_done = False  # True once post-exec web search fired


class ScheduledEventWatcher:
    """
    Loads scheduled_events.json on every tick (cheap — just file mtime check).
    Manages arming, triggering, confirmation, action, trade logging,
    RSS fetch at window open, and optional post-execution web search.

    Events whose watch_from_utc is in the past are handled gracefully —
    they arm immediately on the first tick and watch until window expires.
    """

    def __init__(self, send_telegram_fn, anthropic_api_key: str = ""):
        self._send    = send_telegram_fn
        self._api_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._states: dict[str, _EventState] = {}
        self._last_mtime: float = 0.0
        self._events: list = []
        TRADES_DIR.mkdir(exist_ok=True)
        self._reload()

    # ─────────────────────────────────────────────
    # FILE LOADING — hot reload on mtime change
    # ─────────────────────────────────────────────
    def _reload(self):
        try:
            mtime = EVENTS_FILE.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0

        if mtime == self._last_mtime:
            return

        self._last_mtime = mtime
        self._events = _load_events()
        log.info(f"EventWatcher: loaded {len(self._events)} active event(s) from {EVENTS_FILE}")

        known_ids = {e["id"] for e in self._events}
        for eid in list(self._states.keys()):
            if eid not in known_ids:
                del self._states[eid]
                log.info(f"EventWatcher: removed state for expired event {eid}")

    def _get_state(self, event_id: str) -> _EventState:
        if event_id not in self._states:
            self._states[event_id] = _EventState(event_id)
        return self._states[event_id]

    # ─────────────────────────────────────────────
    # TICK HANDLER
    # ─────────────────────────────────────────────
    def on_tick(self, mid: float):
        self._reload()
        now_utc   = datetime.now(timezone.utc)
        now_epoch = time.time()
        for event in self._events:
            try:
                self._process_event(event, mid, now_utc, now_epoch)
            except Exception as e:
                log.error(f"EventWatcher error on event {event.get('id')}: {e}", exc_info=True)

    # ─────────────────────────────────────────────
    # CORE STATE MACHINE
    # ─────────────────────────────────────────────
    def _process_event(self, event: dict, mid: float, now_utc: datetime, now_epoch: float):
        eid = event["id"]
        st  = self._get_state(eid)

        if st.executed or st.window_expired:
            return

        watch_from_str    = event["watch_from_utc"]
        duration_min      = event.get("watch_duration_min", 60)
        watch_from_dt     = datetime.fromisoformat(watch_from_str).replace(tzinfo=timezone.utc)
        watch_until_epoch = watch_from_dt.timestamp() + duration_min * 60

        # Not yet in window
        if now_utc < watch_from_dt:
            return

        # Window expired
        if now_epoch > watch_until_epoch:
            if not st.window_expired:
                st.window_expired = True
                log.info(f"EventWatcher: window expired for [{eid}] {event['name']}")
                if not st.executed:
                    self._send(
                        f"⏱ <b>MonsieurMarket — Event Window Closed</b>\n\n"
                        f"<b>{event['name']}</b>\n"
                        f"No {event.get('trigger_pct', '?')}% move detected within "
                        f"{duration_min} min window.\n"
                        f"Reference: {_fmt_price(st.reference_price)} | Current: {mid:.2f}\n"
                        f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris",
                        force=True,
                    )
                    self._log_event(event, st, "WINDOW_CLOSED", mid,
                                    note=f"No trigger in {duration_min}min window.")
            return

        # Arm — first tick inside window
        if not st.armed:
            st.armed           = True
            st.armed_at_utc    = now_utc
            st.reference_price = mid
            log.info(f"EventWatcher: ARMED [{eid}] {event['name']} | reference={mid:.2f} | window={duration_min}min")
            self._send(
                f"⚡ <b>MonsieurMarket — Event Window Open</b>\n\n"
                f"<b>{event['name']}</b>\n"
                f"Watching for ±{event.get('trigger_pct', '?')}% move "
                f"over next {duration_min} min\n"
                f"Reference price: <b>{mid:.2f}</b>\n"
                f"Confirm wait: {event.get('confirm_wait_min', 15)} min\n"
                f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris",
                force=True,
            )
            self._log_event(event, st, "ARMED", mid)

            # Schedule RSS fetch at open if enabled
            if event.get("rss_at_open") and not st.rss_done:
                delay_min = event.get("rss_at_open_delay_min", 5)
                log.info(f"EventWatcher: RSS fetch scheduled in {delay_min} min for [{eid}]")
                threading.Thread(
                    target=self._delayed_rss_fetch,
                    args=(event, st, delay_min),
                    daemon=True,
                ).start()
            return

        if st.reference_price is None or st.reference_price == 0:
            return

        pct_move         = (mid - st.reference_price) / st.reference_price * 100
        trigger_pct      = event.get("trigger_pct", 2.0)
        confirm_wait_sec = event.get("confirm_wait_min", 15) * 60

        # Trigger
        if not st.triggered and abs(pct_move) >= trigger_pct:
            st.triggered     = True
            st.trigger_time  = now_epoch
            st.trigger_price = mid
            direction = "📈 UP" if pct_move > 0 else "📉 DOWN"
            log.info(f"EventWatcher: TRIGGERED [{eid}] {event['name']} | move={pct_move:+.2f}%")
            self._send(
                f"🔔 <b>MonsieurMarket — Event Trigger</b>\n\n"
                f"<b>{event['name']}</b>\n"
                f"Brent {direction} <b>{pct_move:+.2f}%</b> from event open\n"
                f"Reference: {st.reference_price:.2f} → Now: {mid:.2f}\n"
                f"Waiting <b>{event.get('confirm_wait_min', 15)} min</b> to confirm...\n"
                f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris",
                force=True,
            )
            self._log_event(event, st, "TRIGGERED", mid, note=f"move={pct_move:+.2f}%")
            return

        # Confirmation wait
        if st.triggered and not st.confirmed:
            elapsed = now_epoch - st.trigger_time
            if elapsed < confirm_wait_sec:
                log.debug(
                    f"EventWatcher: confirming [{eid}] — "
                    f"{int((confirm_wait_sec - elapsed) / 60)}min left | move={pct_move:+.2f}%"
                )
                return

            if abs(pct_move) < trigger_pct:
                log.info(f"EventWatcher: move faded [{eid}] — {pct_move:+.2f}% — resetting")
                self._send(
                    f"↩️ <b>MonsieurMarket — Move Faded</b>\n\n"
                    f"<b>{event['name']}</b>\n"
                    f"Move retreated to {pct_move:+.2f}% — below trigger threshold.\n"
                    f"Continuing to watch...\n"
                    f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris",
                    force=True,
                )
                self._log_event(event, st, "FADED", mid, note=f"move={pct_move:+.2f}%")
                st.triggered     = False
                st.trigger_time  = None
                st.trigger_price = None
                return

            st.confirmed = True
            log.info(f"EventWatcher: CONFIRMED [{eid}] {event['name']} | move={pct_move:+.2f}% sustained")
            self._log_event(event, st, "CONFIRMED", mid, note=f"move={pct_move:+.2f}% sustained")

        # Execute — once only
        if st.confirmed and not st.executed:
            st.executed = True
            self._fire_action(event, st, mid, pct_move)

    # ─────────────────────────────────────────────
    # ACTION
    # ─────────────────────────────────────────────
    def _fire_action(self, event: dict, st: _EventState, mid: float, pct_move: float):
        eid    = event["id"]
        action = event.get("action", "buy_ig_barrier")
        log.info(f"EventWatcher: FIRING [{action}] for [{eid}] {event['name']}")

        if action in ("buy_ig_call_barrier", "buy_ig_put_barrier", "buy_ig_barrier"):
            if action == "buy_ig_call_barrier":
                side = "CALL 📈"
            elif action == "buy_ig_put_barrier":
                side = "PUT 📉"
            else:
                side = "CALL 📈" if pct_move > 0 else "PUT 📉"

            self._send(
                f"🤖 <b>MonsieurMarket — SIMULATED ORDER</b>\n\n"
                f"🛢 BUYING IG {side} BARRIER on Brent\n\n"
                f"Event: <b>{event['name']}</b>\n"
                f"Brent move: <b>{pct_move:+.2f}%</b> from event open\n"
                f"Reference: {_fmt_price(st.reference_price)} | "
                f"Trigger: {_fmt_price(st.trigger_price)} | "
                f"Current: <b>{mid:.2f}</b>\n"
                f"Confirmed over {event.get('confirm_wait_min', 15)} min\n\n"
                f"⚠️ <i>Simulation only — no real order placed</i>\n"
                f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris",
                force=True,
            )
            self._log_event(event, st, "SIMULATED_ORDER", mid,
                            note=f"side={side} move={pct_move:+.2f}%")
        else:
            log.warning(f"EventWatcher: unknown action '{action}' for event {eid}")
            return

        # Schedule post-execution web search if enabled
        if event.get("web_search") and self._api_key:
            delay_min = event.get("web_search_delay_min", 15)
            log.info(f"EventWatcher: web search scheduled in {delay_min} min for [{eid}]")
            threading.Thread(
                target=self._delayed_web_search,
                args=(event, st, mid, pct_move, delay_min),
                daemon=True,
            ).start()

    # ─────────────────────────────────────────────
    # RSS FETCH AT OPEN (one-shot, delayed)
    # ─────────────────────────────────────────────
    def _delayed_rss_fetch(self, event: dict, st: _EventState, delay_min: int):
        """
        Fires once, delay_min after window arms.
        Fetches all active RSS sources, filters to articles published
        after the event window opened, keyword pre-filters, sends to Telegram.
        Skips any feed whose timestamps cannot be parsed reliably.
        """
        time.sleep(delay_min * 60)
        if st.rss_done:
            return
        st.rss_done = True

        eid      = event["id"]
        keywords = [k.lower() for k in event.get("rss_keywords", [])]
        since_utc = st.armed_at_utc or (
            datetime.now(timezone.utc) - timedelta(minutes=delay_min + 5)
        )

        log.info(f"EventWatcher: RSS fetch for [{eid}] since={since_utc.strftime('%H:%M')} UTC keywords={keywords}")

        sources   = _load_rss_sources()
        headlines = []

        for source in sources:
            url    = source["url"]
            tz_str = source.get("timezone", "UTC")
            name   = source["name"]
            try:
                r = requests.get(url, timeout=8, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; MonsieurMarket/1.0)"
                })
                r.raise_for_status()

                items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
                feed_count = 0

                for item in items[:30]:
                    title_m   = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", item)
                    link_m    = re.search(r"<link>(.*?)</link>|<guid[^>]*>(.*?)</guid>", item)
                    pubdate_m = re.search(r"<pubDate>(.*?)</pubDate>", item)
                    desc_m    = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>|<description>(.*?)</description>", item, re.DOTALL)

                    title   = (title_m.group(1) or title_m.group(2) or "").strip() if title_m else ""
                    url_art = (link_m.group(1) or link_m.group(2) or "").strip() if link_m else ""
                    pub_raw = pubdate_m.group(1).strip() if pubdate_m else ""
                    desc    = (desc_m.group(1) or desc_m.group(2) or "").strip()[:200] if desc_m else ""

                    if not title or not pub_raw:
                        continue

                    # Parse timestamp — skip item if unparseable
                    pub_utc = _parse_pub_date(pub_raw, tz_str)
                    if pub_utc is None:
                        continue

                    # Only keep articles published after window opened
                    if pub_utc < since_utc:
                        continue

                    # Keyword pre-filter
                    combined = (title + " " + desc).lower()
                    if keywords and not any(kw in combined for kw in keywords):
                        continue

                    headlines.append({
                        "source":  name,
                        "title":   title,
                        "desc":    desc,
                        "url":     url_art,
                        "pub_utc": pub_utc.strftime("%H:%M UTC"),
                    })
                    feed_count += 1

                log.info(f"RSS [{name}]: {feed_count} fresh relevant article(s)")

            except Exception as e:
                log.warning(f"RSS [{name}]: fetch failed — {e} — skipping")
                continue

        if not headlines:
            self._send(
                f"📰 <b>MonsieurMarket — RSS at Open</b>\n\n"
                f"<b>{event['name']}</b> (+{delay_min} min)\n\n"
                f"No fresh articles matching keywords.\n"
                f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris",
                force=True,
            )
            self._log_event(event, st, "RSS_AT_OPEN", 0.0, note="No fresh articles found.")
            return

        # Format top 5 for Telegram
        lines = [
            f"📰 <b>MonsieurMarket — RSS at Open</b>\n",
            f"<b>{event['name']}</b> (+{delay_min} min)\n",
        ]
        for h in headlines[:5]:
            lines.append(f"  • [{h['source']} {h['pub_utc']}] {h['title'][:80]}")
            if h["desc"]:
                lines.append(f"    <i>{h['desc'][:100]}</i>")

        lines.append(f"\n⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris")
        self._send("\n".join(lines), force=True)

        note = f"{len(headlines)} articles: " + " | ".join(
            f"[{h['source']}] {h['title'][:50]}" for h in headlines[:3]
        )
        self._log_event(event, st, "RSS_AT_OPEN", 0.0, note=note)
        log.info(f"EventWatcher: RSS at open sent for [{eid}] — {len(headlines)} article(s)")

    # ─────────────────────────────────────────────
    # POST-EXECUTION WEB SEARCH (one-shot, delayed)
    # ─────────────────────────────────────────────
    def _delayed_web_search(self, event: dict, st: _EventState,
                             exec_price: float, pct_move: float, delay_min: int):
        """Fires once, delay_min after execution. Finds fresh post-event news only."""
        time.sleep(delay_min * 60)
        if st.web_search_done:
            return
        st.web_search_done = True

        eid   = event["id"]
        query = event.get("web_search_query", f"{event['name']} oil Brent reaction")
        log.info(f"EventWatcher: firing web search for [{eid}] query='{query}'")

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self._api_key)

            prompt = (
                f"Event: {event['name']}\n"
                f"Brent moved {pct_move:+.2f}% from {_fmt_price(st.reference_price)} "
                f"to {exec_price:.2f} at execution.\n"
                f"Search query: {query}\n\n"
                f"Search the web and find news articles published in the LAST "
                f"{delay_min + 5} MINUTES ONLY that explain or react to this price move.\n\n"
                f"STRICT RULES:\n"
                f"- Only include articles published AFTER the event — no previews or predictions\n"
                f"- Do NOT invent or infer reasons if no fresh article is found\n"
                f"- If nothing fresh found, reply EXACTLY: "
                f"'No post-event articles found yet — market reaction still developing.'\n"
                f"- If found: 2-3 sentences max covering what happened, "
                f"the market reaction, and one thing to watch next."
            )

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 2,
                }],
            )

            text = " ".join(
                b.text for b in response.content
                if hasattr(b, "text") and b.text and b.text.strip()
            ).strip() or "No post-event articles found yet."

            log.info(f"EventWatcher: web search result [{eid}]: {text[:100]}")

            self._send(
                f"🔍 <b>MonsieurMarket — Post-Event Search</b>\n\n"
                f"<b>{event['name']}</b> (+{delay_min} min)\n\n"
                f"{text}\n\n"
                f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris",
                force=True,
            )
            self._log_event(event, st, "WEB_SEARCH", exec_price, note=text)

        except Exception as e:
            log.error(f"EventWatcher: web search failed for [{eid}]: {e}")

    # ─────────────────────────────────────────────
    # TRADE LOG — appends to trades/YYYY-MM-DD_eventid.md
    # ─────────────────────────────────────────────
    def _log_event(self, event: dict, st: _EventState,
                   stage: str, price: float, note: str = ""):
        """Append a timestamped row to the event's markdown trade log."""
        if not event.get("log_trade", True):
            return

        eid      = event["id"]
        now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = TRADES_DIR / f"{date_str}_{eid}.md"

        if not filename.exists():
            header = (
                f"# {event['name']}\n\n"
                f"| Field | Value |\n"
                f"|---|---|\n"
                f"| Event ID | `{eid}` |\n"
                f"| Watch from | {event['watch_from_utc']} UTC |\n"
                f"| Trigger | ±{event.get('trigger_pct')}% |\n"
                f"| Confirm | {event.get('confirm_wait_min')}min |\n"
                f"| Window | {event.get('watch_duration_min')}min |\n"
                f"| Action | {event.get('action')} |\n"
                f"| RSS at open | {event.get('rss_at_open_delay_min', '-')}min delay |\n"
                f"| Web search | {event.get('web_search_delay_min', '-')}min delay |\n"
                f"| Notes | {event.get('notes', '')} |\n\n"
                f"---\n\n"
                f"## Event Log\n\n"
                f"| Time (Paris) | Stage | Price | Note |\n"
                f"|---|---|---|---|\n"
            )
            filename.write_text(header, encoding="utf-8")

        note_clean = note.replace("|", "/").replace("\n", " ")[:200]
        price_str  = f"{price:.2f}" if price else "—"
        row = f"| {now_str} | **{stage}** | {price_str} | ref={_fmt_price(st.reference_price)} {note_clean} |\n"

        with filename.open("a", encoding="utf-8") as f:
            f.write(row)

        log.debug(f"EventWatcher: logged [{stage}] to {filename}")
