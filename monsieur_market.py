#!/opt/homebrew/bin/python3
"""
MonsieurMarket — Personal Trading Intelligence Bot
Monitors geopolitical signals and whale trades, alerts via Telegram.

Setup:
    pip install anthropic requests schedule python-dotenv flask

Environment variables required:
    ANTHROPIC_API_KEY   — your Anthropic key
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_CHAT_ID    — your personal chat ID

Run:
    python monsieur_market.py
"""

import os
import json
import time
import logging
import random
import schedule
import requests
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, request
import sys

Path("data").mkdir(exist_ok=True)
Path("data/trades").mkdir(exist_ok=True)

from scheduled_event_watcher import ScheduledEventWatcher
from config import CONFIG
from telegram import send_message, format_bloomberg_post, start_telegram_receiver
from trumpstruth import start_trump_watcher

from dotenv import load_dotenv
load_dotenv()

import anthropic

# Polymarket
from polymarket import (
    load_polymarket_markets,
    check_polymarket,
    update_whale_ledger,
    format_repeat_whale_alert,
    aggregate_whale_signals,
    run_portfolio_check,
    start_polymarket_ws,
    WHALE_REPEAT_TRIGGER,
)

# IG Markets (optional — only imported if credentials present)
try:
    from trading_ig import IGService, IGStreamService
    from trading_ig.streamer.manager import StreamingManager
    from lightstreamer.client import Subscription, SubscriptionListener, ItemUpdate
    IG_AVAILABLE = True
except ImportError:
    IG_AVAILABLE = False

event_watcher = None

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/monsieur_market.log"),
    ],
)
log = logging.getLogger("MonsieurMarket")

# ─────────────────────────────────────────────
# POLYMARKET — imported from polymarket/ module
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────
STATE_FILE = Path("data/monsieur_market_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "seen_news_urls":         {},
        "seen_trade_ids":         {},
        "last_prices":            {},
        "weekly_signals":         [],
        "last_digest":            None,
        "trump_last_seen_url":    None,
        "whale_ledger":           {},
        "last_news_poll":         0,
        "bloomberg_last_seen_ts": 0,
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def clean_state(state: dict) -> dict:
    window = CONFIG["dedup_window_hours"] * 3600
    now    = time.time()
    state["seen_news_urls"] = {
        k: v for k, v in state["seen_news_urls"].items()
        if now - v < window
    }
    state["seen_trade_ids"] = {
        k: v for k, v in state["seen_trade_ids"].items()
        if now - v < window
    }
    return state


# ─────────────────────────────────────────────
# HAIKU — cheap first-pass filter
# ─────────────────────────────────────────────
def haiku_filter_news(headlines: list[dict], theme: dict) -> list[dict]:
    if not headlines:
        return []

    client     = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    keywords   = theme["keywords"]
    theme_name = theme["name"]

    headlines_text = "\n".join(
        f"- [{h.get('source','')}] {h.get('title','')} | {h.get('url','')}"
        for h in headlines
    )

    prompt = f"""You are a news filter for a trading alert system.

Theme: {theme_name}
Keywords: {', '.join(keywords)}

Headlines to evaluate:
{headlines_text}

Return ONLY a JSON array of relevant headline URLs (strings).
A headline is relevant if it directly relates to the theme keywords.
If nothing is relevant, return an empty array [].
Return raw JSON only, no markdown, no explanation."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text  = response.content[0].text.strip()
        text  = text.replace("```json", "").replace("```", "").strip()
        start = text.find("[")
        end   = text.rfind("]") + 1
        if start != -1 and end > start:
            text = text[start:end]
        relevant_urls = json.loads(text)
        return [h for h in headlines if h.get("url") in relevant_urls]
    except Exception as e:
        log.warning(f"Haiku filter error: {e}")
        kw_lower = [k.lower() for k in keywords]
        return [
            h for h in headlines
            if any(kw in h.get("title", "").lower() for kw in kw_lower)
        ]


# ─────────────────────────────────────────────
# SONNET — deep analysis + materiality scoring
# ─────────────────────────────────────────────
def sonnet_analyze(
    news_signals:  list[dict],
    whale_signals: list[dict],
    theme:         dict,
) -> dict | None:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    tradables_text = "\n".join(
        f"  - {t['name']} ({t['ticker']}): {t['signal_direction']} | "
        f"my position: {t.get('my_position','none')} | "
        f"conviction: {t.get('conviction','medium')}"
        + (f" | note: {t['note']}" if t.get("note") else "")
        for t in theme.get("tradables", [])
    )

    news_text = "\n".join(
        f"  - [{s.get('source','')}] {s.get('title','')} — {s.get('url','')}"
        for s in news_signals
    ) or "  None"

    whale_agg  = aggregate_whale_signals([s for s in whale_signals if s.get("type") == "whale_trade"])
    raw_trades = "\n".join(
        f"  - {s['trader']} ${s['amount']:,.0f} {s['side']} {s['outcome']} "
        f"@ {s['price']:.2f} on {s['market']}"
        if s["type"] == "whale_trade"
        else f"  - Price moved {s['move_pct']:+.1f}% on {s['market']}"
        for s in whale_signals[:10]
    ) or "  None"

    whale_text = ""
    if whale_agg:
        whale_text += f"  SUMMARY: {whale_agg['summary_text']}\n\n"
        whale_text += "  Top traders:\n"
        for t in whale_agg.get("top_traders", []):
            whale_text += (
                f"    • {t['name']}: ${t['total_usd']:,.0f} "
                f"(${t['yes_usd']:,.0f} Yes / ${t['no_usd']:,.0f} No)\n"
            )
        whale_text += f"\n  Recent trades (sample):\n{raw_trades}"
    else:
        whale_text = "  None"

    escalation_triggers = theme.get("escalation_triggers", [])
    all_text  = news_text + whale_text
    triggered = [t for t in escalation_triggers if t.lower() in all_text.lower()]

    prompt = f"""You are a geopolitical trading analyst for a private trader.

THEME: {theme['name']}

TRADABLES THE TRADER HOLDS OR WATCHES:
{tradables_text}

NEW NEWS SIGNALS:
{news_text}

NEW POLYMARKET WHALE SIGNALS:
{whale_text}

{"⚠️ NOTE: No news signals this cycle — analysis is WHALE-FLOW-ONLY." if not news_signals else ""}
{"⚠️ ESCALATION TRIGGER DETECTED: " + ", ".join(triggered) if triggered else ""}

Your task:
1. Search the web to fetch full context on the most important news signals above
2. Assess overall materiality for the trader's thesis on a scale of 1-10
3. For each tradable, state impact: BULLISH / BEARISH / NEUTRAL + one sentence why
4. If you see tradables the trader is MISSING that are highly relevant, list up to 3
5. Write a brief alert summary (3-5 sentences max, direct and actionable)

Respond in this exact JSON format:
{{
  "score": <int 1-10>,
  "summary": "<3-5 sentence alert summary>",
  "tradable_impacts": [
    {{"name": "<ticker>", "impact": "BULLISH|BEARISH|NEUTRAL", "reason": "<one sentence>"}}
  ],
  "missing_tradables": [
    {{"name": "<n>", "ticker": "<ticker>", "reason": "<why relevant>"}}
  ],
  "escalation_triggered": <true|false>,
  "wake_override": <true|false>
}}

Return raw JSON only. No markdown."""

    def _parse_sonnet_response(response) -> dict | None:
        text_blocks = [
            block.text for block in response.content
            if hasattr(block, "text") and block.text and block.text.strip()
        ]
        full_text = " ".join(text_blocks).strip()
        if not full_text:
            return None
        clean = full_text.replace("```json", "").replace("```", "").strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start != -1 and end > start:
            clean = clean[start:end]
        return json.loads(clean)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 3,
            }],
        )

        result = _parse_sonnet_response(response)

        if result is None:
            log.warning("Sonnet gave no text with web search — retrying without search tool")
            response2 = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            result = _parse_sonnet_response(response2)

        if result is None:
            log.warning("Sonnet returned no parseable JSON after fallback")
            return None

        if triggered:
            result["wake_override"]        = True
            result["escalation_triggered"] = True

        return result

    except Exception as e:
        log.error(f"Sonnet analysis error: {e}")
        return None


# ─────────────────────────────────────────────
# ALERT FORMATTING
# ─────────────────────────────────────────────
def format_alert(
    theme:          dict,
    analysis:       dict,
    news_signals:   list,
    whale_signals:  list,
    ig_data:        dict | None = None,
    trigger_reason: str = "",
) -> str:
    score = analysis.get("score", 0)

    if score >= 9:
        severity = "🚨 ACT NOW"
    elif score >= 7:
        severity = "⚠️ WATCH"
    else:
        severity = "📰 FYI"

    if analysis.get("escalation_triggered"):
        severity = "🚨🚨 ESCALATION ALERT"

    summary = analysis.get("summary", "")
    advice_triggers = [
        "Stay long", "Stay short", "Consider initiating",
        "warrants initiating", "initiate a", "add to",
        "close your", "take profit",
    ]
    for trigger in advice_triggers:
        idx = summary.find(trigger)
        if idx > 0:
            cut = summary.rfind(". ", 0, idx)
            if cut > 0:
                summary = summary[:cut + 1]
            break

    lines = [
        f"<b>MonsieurMarket — {theme['name']}</b>",
        f"{severity} | Score: {score}/10",
    ]

    if trigger_reason:
        lines.append(f"⚡ <i>Triggered by: {trigger_reason}</i>")

    lines += ["", f"📋 {summary}"]

    if news_signals:
        lines.append("\n📰 <b>News:</b>")
        for s in news_signals[:3]:
            lines.append(f"  • [{s.get('source','')}] {s.get('title','')[:80]}")

    if whale_signals:
        lines.append("\n🐋 <b>Polymarket Whales:</b>")
        for s in whale_signals[:5]:
            if s["type"] == "whale_trade":
                ts_str = ""
                ts = s.get("ts") or s.get("timestamp")
                if ts:
                    try:
                        ts_str = f" [{datetime.fromtimestamp(float(ts)).strftime('%d/%m %H:%M')}]"
                    except Exception:
                        pass
                lines.append(
                    f"  • {s['trader']}{ts_str} — ${s['amount']:,.0f} "
                    f"{s['outcome']} @ {s['price']:.2f} on {s['market']}"
                )
            else:
                arrow = "📈" if s["move_pct"] > 0 else "📉"
                lines.append(
                    f"  • {arrow} {s['market']}: "
                    f"{s['from_price']:.2f} → {s['to_price']:.2f} "
                    f"({s['move_pct']:+.1f}%)"
                )

    impacts = analysis.get("tradable_impacts", [])
    if impacts:
        lines.append("\n📊 <b>Your Positions:</b>")
        icons = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}
        for t in impacts:
            icon = icons.get(t.get("impact", ""), "⚪")
            lines.append(f"  {icon} <b>{t['name']}</b> — {t.get('reason','')[:60]}")

    missing = analysis.get("missing_tradables", [])
    if missing:
        lines.append("\n💡 <b>Sonnet suggests you're not tracking:</b>")
        for m in missing:
            lines.append(f"  • {m['name']} ({m['ticker']}) — {m.get('reason','')[:60]}")

    ig_block = format_ig_block(ig_data) if ig_data else ""
    if ig_block:
        lines.append(ig_block)

    lines.append(f"\n⏰ {datetime.now().strftime('%d/%m %H:%M')}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# WEEKLY DIGEST
# ─────────────────────────────────────────────
def send_weekly_digest(state: dict):
    if not CONFIG["weekly_digest"]["enabled"]:
        return

    log.info("Generating weekly digest...")
    client        = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    signals       = state.get("weekly_signals", [])
    active_themes = [t["name"] for t in CONFIG["themes"] if t.get("active")]
    tradables     = []
    for theme in CONFIG["themes"]:
        if theme.get("active"):
            tradables.extend(theme.get("tradables", []))

    prompt = f"""You are a weekly trading intelligence analyst.

Active themes this week: {', '.join(active_themes)}

Signals fired this week:
{json.dumps(signals, indent=2) if signals else 'No signals fired this week.'}

Trader's current tradables:
{json.dumps([t['ticker'] for t in tradables], indent=2)}

Write a Sunday morning digest (maximum 200 words) covering:
1. Key developments this week relevant to each active theme
2. How the trader's positions held up / what moved
3. What to watch next week
4. 1-2 tradables the trader might be missing given current themes

Search the web for current context before writing.
Keep it punchy and actionable. No fluff."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 3,
            }],
        )
        digest_text = " ".join(
            block.text for block in response.content
            if hasattr(block, "text")
        ).strip()

        send_message(
            f"<b>☕ MonsieurMarket — Weekly Digest</b>\n"
            f"Week ending {datetime.now().strftime('%d/%m/%Y')}\n\n"
            f"{digest_text}"
        )

        state["weekly_signals"] = []
        state["last_digest"]    = time.time()
        save_state(state)

    except Exception as e:
        log.error(f"Weekly digest error: {e}")


# ─────────────────────────────────────────────
# IG MARKETS — REAL-TIME PRICE STREAM
# ─────────────────────────────────────────────
class _PriceState:
    def __init__(self):
        self.last_alert_time = 0
        self.tick_history    = deque()

    def add_tick(self, mid: float):
        now = time.time()
        self.tick_history.append((now, mid))
        cutoff = now - 15 * 60
        while self.tick_history and self.tick_history[0][0] < cutoff:
            self.tick_history.popleft()

    def change_pct_over_window(self, window_min: float) -> float | None:
        if len(self.tick_history) < 2:
            return None
        now          = time.time()
        cutoff       = now - window_min * 60
        window_ticks = [t for t in self.tick_history if t[0] >= cutoff]
        if len(window_ticks) < 2:
            return None
        oldest = window_ticks[0][1]
        latest = window_ticks[-1][1]
        return (latest - oldest) / oldest * 100 if oldest else None

    def rolling_change_pct(self):
        return self.change_pct_over_window(10)

    def can_alert(self):
        return (time.time() - self.last_alert_time) > \
               CONFIG["price_watcher"]["cooldown_min"] * 60

    def mark_alerted(self):
        self.last_alert_time = time.time()


_price_state = _PriceState()
_tick_count  = 0


def _on_brent_tick(ticker):
    """Called on every Lightstreamer price tick."""
    import math

    def _get(attr):
        v = getattr(ticker, attr, None)
        if isinstance(v, float) and math.isnan(v):
            return None
        return v

    bid      = _get("bid")
    offer    = _get("offer")
    day_pct  = _get("day_percent_change_mid")
    day_open = _get("day_open_mid")
    day_high = _get("day_high")
    day_low  = _get("day_low")

    def valid(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != 0

    if valid(bid) and valid(offer):
        mid = (bid + offer) / 2
    elif valid(day_open) and valid(day_pct):
        mid = day_open * (1 + day_pct / 100)
    elif valid(day_open):
        mid = day_open
    else:
        return

    _price_state.add_tick(mid)

    global _tick_count
    _tick_count += 1
    if _tick_count % 10 == 1:
        log.info(
            f"Brent  mid={mid:.2f}"
            + (f"  bid={bid:.2f} ask={offer:.2f}" if valid(bid) and valid(offer) else "  (derived)")
            + (f"  day={day_pct:+.2f}%" if valid(day_pct) else "")
            + (f"  [{day_low:.2f}–{day_high:.2f}]" if valid(day_low) and valid(day_high) else "")
        )

    if event_watcher is not None:
        event_watcher.on_tick(mid)

    if not _price_state.can_alert():
        return

    cfg          = CONFIG["price_watcher"]
    alert_reason = None
    alert_emoji  = ""

    # Trigger 1 — big move from day open
    if valid(day_pct) and abs(day_pct) >= cfg["trigger_pct_from_open"]:
        direction    = "📈 UP" if day_pct > 0 else "📉 DOWN"
        alert_reason = (
            f"Brent {direction} <b>{day_pct:+.2f}%</b> from today's open"
            f" ({day_open:.2f} → {mid:.2f})"
        )
        alert_emoji = "🚨"

    # Trigger 2 — multi-window rolling moves (shortest window wins)
    for (window_min, threshold_pct, label) in cfg["rolling_windows"]:
        chg = _price_state.change_pct_over_window(window_min)
        if chg is not None and abs(chg) >= threshold_pct:
            direction   = "📈 UP" if chg > 0 else "📉 DOWN"
            rolling_msg = (
                f"Brent {direction} <b>{chg:+.2f}%</b> in last {label}"
                f" → {mid:.2f}"
            )
            if alert_reason is None:
                alert_reason = rolling_msg
                alert_emoji  = "⚡" if window_min <= 1 else "⚠️" if window_min <= 5 else "📊"
            break

    if not alert_reason:
        return

    now_str    = datetime.now().strftime("%d/%m %H:%M")
    price_line = (
        f"Bid: {bid:.2f}  Ask: {offer:.2f}\n"
        if valid(bid) and valid(offer)
        else f"Mid (derived): {mid:.2f}\n"
    )
    range_line = (
        f"Day range: {day_low:.2f} – {day_high:.2f}\n"
        if valid(day_low) and valid(day_high) else ""
    )

    send_message(
        f"{alert_emoji} <b>MonsieurMarket — Price Alert</b>\n\n"
        f"{alert_reason}\n\n"
        f"{price_line}{range_line}"
        f"\n⏰ {now_str}"
    )
    _price_state.mark_alerted()
    log.info(f"Price alert sent — {alert_reason[:60]}")

    # Brent moved — ask Bloomberg monitor to scrape immediately
    bloomberg_scheduler.trigger_now(reason=alert_reason[:60])


# ─────────────────────────────────────────────
# BLOOMBERG SCHEDULER — MM owns the refresh timer
# ─────────────────────────────────────────────
class BloombergScheduler:
    """
    MM-owned timer that decides when to ask monitor to scrape.
    Monitor has no internal timer — this is the brain's schedule.

    Refresh intervals (French market hours, hardcoded UTC offsets for CEST):
        06:00–20:00 UTC (08:00–22:00 Paris) — every ~5 min
        04:00–06:00 UTC (06:00–08:00 Paris) — every ~15 min
        else                                 — every 60 min overnight

    Immediate refresh triggered by:
        - Brent price spike
        - Trump post about Iran
        - Any future signal source
    """
    def __init__(self):
        self._timer = None
        self._lock  = threading.Lock()
        self._ready = False

    def on_ready(self):
        """Monitor found a source — start the refresh schedule."""
        self._ready = True
        log.info("Bloomberg scheduler: monitor ready — starting refresh schedule")
        self._schedule_next()

    def on_no_source(self):
        """Monitor has no source — schedule a retry."""
        self._ready = False
        self.cancel()
        retry_sec = self._interval_sec()
        log.info(f"Bloomberg scheduler: no source — retrying in {retry_sec // 60} min")
        with self._lock:
            self._timer = threading.Timer(retry_sec, self._trigger_refresh)
            self._timer.daemon = True
            self._timer.start()

    def trigger_now(self, reason: str = ''):
        """Immediate refresh — called on Brent spike, Trump post, etc."""
        if not self._ready:
            log.debug("Bloomberg scheduler: trigger_now called but monitor not ready yet")
            return
        self.cancel()
        log.info(f"Bloomberg scheduler: immediate refresh{' (' + reason + ')' if reason else ''}")
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def cancel(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _schedule_next(self):
        interval = self._interval_sec()
        log.info(f"Bloomberg scheduler: next refresh in {interval // 60} min")
        with self._lock:
            self._timer = threading.Timer(interval, self._trigger_refresh)
            self._timer.daemon = True
            self._timer.start()

    def _trigger_refresh(self):
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        """
        GET /refresh on monitor.
        New posts come back as POST /signal calls — no polling needed.

        TODO: Sonnet correlation on price spike
        Pass price context here so /signal handler can correlate news with the move.
        """
        try:
            log.info("Bloomberg scheduler: calling /refresh on monitor...")
            r = requests.get('http://localhost:3457/refresh', timeout=35)
            r.raise_for_status()
            log.debug("Bloomberg scheduler: /refresh complete")
        except Exception as e:
            log.warning(f"Bloomberg scheduler: /refresh failed: {e}")
        finally:
            log.info(f"Bloomberg scheduler: _do_refresh finally — _ready={self._ready}")
            self._schedule_next()

    def _interval_sec(self) -> int:
        """
        Market-hours aware interval.
        Hardcoded for CEST (UTC+2) — covers April-October.
        Paris 08:00-22:00 = UTC 06:00-20:00
        """
        now  = datetime.now(timezone.utc)
        hour = now.hour

        if 6 <= hour < 20:
            return 5 * 60 + random.randint(-30, 90)    # market hours

        elif 4 <= hour < 6:
            # pre-market — align to market open if next refresh would overshoot
            next_refresh = now + timedelta(seconds=15 * 60)
            if next_refresh.hour >= 6:
                target = now.replace(hour=6, minute=0, second=0, microsecond=0)
                secs = int((target - now).total_seconds())
                log.info(f"Bloomberg scheduler: aligning to market open in {secs // 60}min")
                return secs
            return 15 * 60 + random.randint(-60, 60)

        else:
            # overnight — align to pre-market if next refresh would overshoot
            next_refresh = now + timedelta(seconds=60 * 60)
            if next_refresh.hour >= 4:
                target = now.replace(hour=4, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                secs = int((target - now).total_seconds())
                log.info(f"Bloomberg scheduler: aligning to pre-market in {secs // 60}min")
                return secs
            return 60 * 60  # deep overnight


bloomberg_scheduler = BloombergScheduler()


# ─────────────────────────────────────────────
# MM FLASK SIGNAL API — port 3456
# ─────────────────────────────────────────────
mm_app = Flask('MonsieurMarketAPI')


@mm_app.route('/signal', methods=['POST'])
def receive_signal():
    """
    Single entry point for all signals from all sources.
    Returns 200 immediately — processing happens in background thread.

    Payload:
    {
        "source": "bloomberg|price|trump|polymarket",
        "type":   "ready|no_source|post|error",
        "data":   { ... source-specific ... },
        "ts":     1744030000
    }
    """
    try:
        payload = request.get_json(force=True)
        if not payload:
            return jsonify({'status': 'error', 'msg': 'no payload'}), 400

        source = payload.get('source', '')
        stype  = payload.get('type', '')
        data   = payload.get('data', {})

        log.info(f"📨 Signal: {source}/{stype}")

        # Process in background — don't block the monitor
        threading.Thread(
            target=_handle_signal,
            args=(source, stype, data),
            daemon=True,
        ).start()

        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        log.error(f"/signal endpoint error: {e}")
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@mm_app.route('/health', methods=['GET'])
def mm_health():
    return jsonify({'status': 'ok', 'ts': time.time()}), 200


def _handle_signal(source: str, stype: str, data: dict):
    """Brain — routes and processes each signal."""
    try:
        if source == 'bloomberg':
            _handle_bloomberg_signal(stype, data)
        elif source == 'trump':
            _handle_trump_signal(stype, data)
        elif source == 'telegram':
            _handle_telegram_signal(stype, data)
        # Future sources plug in here:
        # elif source == 'ukmto':      _handle_ukmto_signal(stype, data)
        # elif source == 'polymarket': _handle_polymarket_signal(stype, data)
    except Exception as e:
        log.error(f"Signal handler error ({source}/{stype}): {e}")


def _handle_bloomberg_signal(stype: str, data: dict):
    """Handle signals from the Bloomberg monitor."""

    if stype == 'ready':
        src  = data.get('source_type', '?')
        url  = data.get('source_url', '')[:70]
        log.info(f"Bloomberg ready: {src} — {url}")
        bloomberg_scheduler.on_ready()

    elif stype == 'no_source':
        log.info("Bloomberg: no source — scheduler will retry")
        bloomberg_scheduler.on_no_source()

    elif stype == 'liveblog_ended':
        log.info("Bloomberg: liveblog ended — monitor will scan homepage next cycle")
        bloomberg_scheduler.on_no_source()
        send_message(
            "📰 <b>Bloomberg liveblog ended</b>\n"
            "Switching back to tag source — monitor scanning homepage.\n"
            f"⏰ {datetime.now().strftime('%d/%m %H:%M')}"
        )

    elif stype == 'post':
        # New post — send to Telegram immediately.
        # Brain logic here later: score, correlate with price/whales, etc.
        log.info(f"Bloomberg post: {data.get('title', '')[:60]}")
        send_message(format_bloomberg_post(data))

    elif stype == 'error':
        msg = data.get('msg', '')
        log.warning(f"Bloomberg monitor error: {msg}")
        send_message(
            f"🚨 <b>Bloomberg Monitor Error</b>\n\n"
            f"{msg[:200]}\n\n"
            f"⏰ {datetime.now().strftime('%d/%m %H:%M')}"
        )        


def _handle_trump_signal(stype: str, data: dict):
    """
    Handle Trump post signals from trumpstruth watcher.
    Haiku filters, Sonnet analyses, MM sends to Telegram.
    """
    if stype != 'post':
        return

    post  = data
    title = post.get('title', '')
    url   = post.get('url', '')
    log.info(f"Trump signal received: {title[:80]}")

    # Haiku filter — cheap yes/no
    try:
        client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content":
                f"Does this Trump post relate to oil prices, Iran, Saudi Arabia, "
                f"OPEC, Middle East conflict, energy markets, or military action?\n\n"
                f'"{title}"\n\n'
                f"Reply YES or NO only."
            }],
        )
        relevant = response.content[0].text.strip().upper().startswith("YES")
    except Exception as e:
        log.debug(f"Haiku Trump filter error: {e}")
        relevant = False

    if not relevant:
        log.info("  → not relevant — skipping")
        return

    log.info("  → RELEVANT — firing Sonnet analysis")

    # Sonnet analysis + web search
    try:
        themes_text = ", ".join(t["name"] for t in CONFIG["themes"] if t.get("active"))
        response    = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{"role": "user", "content":
                f"Trump just posted on Truth Social:\n\n"
                f'"{title}"\n\n'
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
        analysis = " ".join(
            block.text for block in response.content
            if hasattr(block, "text") and block.text and block.text.strip()
        ).strip()
    except Exception as e:
        log.warning(f"Sonnet Trump analysis error: {e}")
        analysis = None

    if analysis:
        send_message(
            f"🇺🇸 <b>MonsieurMarket — Trump Alert</b>\n\n"
            f"<i>{title[:300]}</i>\n\n"
            f"🧠 <b>Sonnet:</b> {analysis}\n\n"
            f"⏰ {datetime.now().strftime('%d/%m %H:%M')}"
        )
        log.info("  → Trump alert sent")


def start_mm_api():
    """Start MM Flask signal API in a background thread."""
    def _run():
        log.info("🧠 MM Signal API on port 3456")
        mm_app.run(
            host='0.0.0.0',
            port=3456,
            debug=False,
            use_reloader=False,
        )
    t = threading.Thread(target=_run, name='MMApi', daemon=True)
    t.start()
    # Give Flask a moment to bind before other threads try to connect
    time.sleep(1)
    log.info("MM Signal API ready")


# ─────────────────────────────────────────────
# IG MARKETS — MARKET LISTENER
# ─────────────────────────────────────────────
class _MarketListener(SubscriptionListener if IG_AVAILABLE else object):
    def onItemUpdate(self, update: "ItemUpdate"):
        try:
            bid        = update.getValue("BID")
            offer      = update.getValue("OFFER")
            change_pct = update.getValue("CHANGE_PCT")
            change     = update.getValue("CHANGE")
            high       = update.getValue("HIGH")
            low        = update.getValue("LOW")
            state      = update.getValue("MARKET_STATE")
            upd_time   = update.getValue("UPDATE_TIME")

            def to_f(v):
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None

            class _T:
                pass
            t = _T()
            t.bid                    = to_f(bid)
            t.offer                  = to_f(offer)
            t.day_percent_change_mid = to_f(change_pct)
            t.day_net_change_mid     = to_f(change)
            t.day_high               = to_f(high)
            t.day_low                = to_f(low)
            t.day_open_mid           = (
                t.bid / (1 + t.day_percent_change_mid / 100)
                if t.bid and t.day_percent_change_mid
                else None
            )
            t.timestamp    = upd_time
            t.market_state = state

            _on_brent_tick(t)

        except Exception as e:
            log.debug(f"Market listener parse error: {e}")

    def onSubscription(self):
        log.info("Brent MARKET subscription active ✅")

    def onSubscriptionError(self, code, message):
        log.warning(f"Brent MARKET subscription error: {code} — {message}")

    def onUnsubscription(self):
        log.info("Brent MARKET subscription stopped")


def _stream_worker():
    username = os.getenv("IG_USERNAME")
    password = os.getenv("IG_PASSWORD")
    api_key  = os.getenv("IG_API_KEY")
    acc_num  = os.getenv("IG_ACC_NUMBER")
    epic     = os.getenv("IG_BRENT_EPIC", "").strip()

    if not all([username, password, api_key, epic]):
        log.warning("Price watcher: IG credentials or epic not set — skipping stream")
        return

    while True:
        try:
            log.info(f"Price watcher: connecting (epic={epic})")
            ig_svc = IGService(
                username, password, api_key,
                acc_type="DEMO",
                acc_number=acc_num,
            )
            ig_stream = IGStreamService(ig_svc)
            ig_stream.acc_number = acc_num
            ig_stream.create_session(version="3")

            sm     = StreamingManager(ig_stream)
            sm.start_tick_subscription(epic)
            ticker = sm.ticker(epic, timeout_length=10)
            log.info(f"Price watcher: ✅ got ticker — {ticker}")

            last_ts = None
            while True:
                if ticker.timestamp != last_ts:
                    last_ts = ticker.timestamp
                    _on_brent_tick(ticker)
                time.sleep(0.5)

        except Exception as e:
            log.error(f"Price watcher stream error: {e} — reconnecting in 30s")
            time.sleep(30)


def start_price_watcher():
    if not CONFIG["price_watcher"]["enabled"]:
        log.info("Price watcher disabled in config")
        return
    if not IG_AVAILABLE:
        log.warning("Price watcher: trading-ig not installed — skipping")
        return
    t = threading.Thread(target=_stream_worker, name="PriceWatcher", daemon=True)
    t.start()
    log.info("Price watcher thread started")


# ─────────────────────────────────────────────
# POLYMARKET WEBSOCKET — extracted to polymarket/
# ─────────────────────────────────────────────
# start_polymarket_ws is imported from polymarket module
# callbacks passed so it can send Telegram and manage state


# ─────────────────────────────────────────────
# IG MARKETS — REST SNAPSHOT
# ─────────────────────────────────────────────
_ig_service = None


def _get_ig_service():
    global _ig_service
    if not IG_AVAILABLE:
        return None
    username = os.getenv("IG_USERNAME")
    password = os.getenv("IG_PASSWORD")
    api_key  = os.getenv("IG_API_KEY")
    if not all([username, password, api_key]):
        return None
    if _ig_service is not None:
        return _ig_service
    try:
        svc = IGService(username, password, api_key, acc_type="DEMO")
        svc.create_session()
        acc_number = os.getenv("IG_ACC_NUMBER")
        if acc_number:
            svc.switch_account(acc_number, False)
        _ig_service = svc
        log.info("IG Markets session established (demo)")
    except Exception as e:
        log.warning(f"IG Markets login failed: {e}")
        _ig_service = None
    return _ig_service


def check_ig_brent() -> dict | None:
    svc  = _get_ig_service()
    epic = os.getenv("IG_BRENT_EPIC", "").strip()
    if svc is None or not epic:
        return None
    try:
        info         = svc.fetch_market_by_epic(epic)
        snap         = info.get("snapshot", {}) if isinstance(info, dict) else {}
        inst         = info.get("instrument", {}) if isinstance(info, dict) else {}
        bid          = float(snap.get("bid") or 0)
        ask          = float(snap.get("offer") or snap.get("ask") or 0)
        mid          = (bid + ask) / 2 if bid and ask else 0
        net_chg      = float(snap.get("netChange") or 0)
        net_chg_pct  = float(snap.get("percentageChange") or 0)
        high         = float(snap.get("high") or 0)
        low          = float(snap.get("low") or 0)
        market_state = snap.get("marketStatus", "UNKNOWN")

        knock_out = None
        for field in ["knockoutLevel", "limitLevel", "stopLevel"]:
            val = inst.get(field) or snap.get(field)
            if val:
                try:
                    knock_out = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        barrier_distance_pct = None
        barrier_warning      = False
        if knock_out and mid:
            barrier_distance_pct = abs((mid - knock_out) / mid) * 100
            barrier_warning      = barrier_distance_pct < 5.0

        result = {
            "epic": epic, "bid": bid, "ask": ask, "mid": mid,
            "net_chg": net_chg, "net_chg_pct": net_chg_pct,
            "high": high, "low": low, "market_state": market_state,
            "knock_out_level": knock_out,
            "barrier_distance_pct": barrier_distance_pct,
            "barrier_warning": barrier_warning,
        }
        log.info(
            f"IG Brent: mid={mid:.2f} chg={net_chg_pct:+.2f}% state={market_state}"
            + (f" ⚠️ barrier {barrier_distance_pct:.1f}% away" if barrier_warning else "")
        )
        return result
    except Exception as e:
        lvl = log.debug if "500" in str(e) else log.warning
        lvl(f"IG REST snapshot failed: {e}")
        global _ig_service
        _ig_service = None
        return None


def format_ig_block(ig: dict) -> str:
    if not ig:
        return ""
    arrow      = "📈" if ig["net_chg_pct"] >= 0 else "📉"
    state_icon = "🟢" if ig["market_state"] == "TRADEABLE" else "🔴"
    lines = [
        "",
        f"🛢 <b>Brent (IG live)</b> {state_icon}",
        f"  Bid {ig['bid']:.2f} / Ask {ig['ask']:.2f}  {arrow} {ig['net_chg_pct']:+.2f}% today",
        f"  Range: {ig['low']:.2f} – {ig['high']:.2f}",
    ]
    if ig.get("knock_out_level"):
        lines.append(
            f"  Knock-out barrier: <b>{ig['knock_out_level']:.2f}</b>  "
            f"({ig['barrier_distance_pct']:.1f}% away)"
        )
        if ig["barrier_warning"]:
            lines.append("  ⚠️ <b>BARRIER PROXIMITY ALERT — within 5%</b>")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN POLLING LOOP (disabled — too expensive)
# ─────────────────────────────────────────────


def fetch_rss_headlines(state: dict) -> tuple[list[dict], dict]:
    """Fetch recent headlines from free RSS feeds. Filters out already-seen URLs."""
    sources = [
        {"name": "Reuters",    "url": "https://feeds.reuters.com/reuters/topNews"},
        {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"name": "OilPrice",   "url": "https://oilprice.com/rss/main"},
        {"name": "CNBC",       "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
        {"name": "CNN",        "url": "http://rss.cnn.com/rss/cnn_latest.rss"},
    ]
    headlines = []
    for source in sources:
        try:
            import re as _re
            r = requests.get(source["url"], timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (compatible; MonsieurMarket/1.0)"
            })
            items = _re.findall(r"<item>(.*?)</item>", r.text, _re.DOTALL)
            for item in items[:20]:
                title_m = _re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", item)
                link_m  = _re.search(r"<link>(.*?)</link>|<guid>(.*?)</guid>", item)
                title   = (title_m.group(1) or title_m.group(2) or "").strip() if title_m else ""
                url     = (link_m.group(1)  or link_m.group(2)  or "").strip() if link_m  else ""
                if title and url and url not in state["seen_news_urls"]:
                    headlines.append({"title": title, "url": url, "source": source["name"]})
        except Exception as e:
            log.debug(f"RSS fetch error {source['name']}: {e}")
    return headlines, state


def run_poll():
    """
    Main polling function — currently disabled (Haiku + Sonnet/web-search cost).
    Re-enable in main() when budget allows:
        run_poll()
        schedule.every(CONFIG["poll_interval_minutes"]).minutes.do(run_poll)
    """
    log.info("── Poll cycle starting ──")
    state = load_state()
    state = clean_state(state)

    ig_data = check_ig_brent()

    if ig_data and ig_data.get("barrier_warning"):
        send_message(
            f"⚠️ <b>Brent Barrier Proximity Alert</b>\n"
            f"Knock-out at <b>{ig_data['knock_out_level']:.2f}</b> — "
            f"only {ig_data['barrier_distance_pct']:.1f}% away\n"
            f"Current mid: {ig_data['mid']:.2f}  "
            f"({ig_data['net_chg_pct']:+.2f}% today)\n"
            f"⏰ {datetime.now().strftime('%d/%m %H:%M')}"
        )

    whale_signals, state = check_polymarket(state)
    if whale_signals:
        log.info(f"Polymarket: {len(whale_signals)} new whale signal(s)")

    whale_agg_global     = aggregate_whale_signals(
        [s for s in whale_signals if s.get("type") == "whale_trade"]
    )
    cfg_wt               = CONFIG["whale_triggers"]
    whale_flow_trigger   = False
    whale_trigger_reason = ""

    if whale_signals:
        max_single = max(
            (s.get("amount", 0) for s in whale_signals if s.get("type") == "whale_trade"),
            default=0
        )
        if max_single >= cfg_wt["single_trade_usd"]:
            whale_flow_trigger   = True
            whale_trigger_reason = f"single trade ${max_single:,.0f}"

        if not whale_flow_trigger and whale_agg_global.get("top_traders"):
            top = whale_agg_global["top_traders"][0]
            if top["total_usd"] >= cfg_wt["single_trader_usd"]:
                whale_flow_trigger   = True
                whale_trigger_reason = f"{top['name']} cumulative ${top['total_usd']:,.0f}"

        if not whale_flow_trigger:
            total = whale_agg_global.get("total_volume_usd", 0)
            net   = abs(whale_agg_global.get("net_yes_usd", 0))
            if (total >= cfg_wt["net_flow_usd"] and
                    total > 0 and net / total >= cfg_wt["net_flow_min_pct"]):
                whale_flow_trigger   = True
                direction            = whale_agg_global.get("dominant_side", "?")
                whale_trigger_reason = f"${total:,.0f} total, {net/total*100:.0f}% {direction}"

    if whale_flow_trigger:
        log.info(f"🐋 Whale trigger: {whale_trigger_reason}")

    state, repeat_whale_alerts = update_whale_ledger(state, whale_signals)
    if repeat_whale_alerts:
        msg = format_repeat_whale_alert(repeat_whale_alerts, market="US forces enter Iran")
        if msg:
            send_message(msg)
            whale_flow_trigger = True
            if not whale_trigger_reason:
                whale_trigger_reason = f"{len(repeat_whale_alerts)} repeat whale(s) crossed ${WHALE_REPEAT_TRIGGER:,} threshold"

        for alert in repeat_whale_alerts:
            proxy  = alert.get("proxy_wallet", "")
            trader = alert.get("trader", "")
            if proxy:
                threading.Thread(
                    target=run_portfolio_check,
                    args=(trader, proxy, send_message),
                    daemon=True,
                ).start()

    poll_interval_sec = CONFIG["poll_interval_minutes"] * 60
    time_since_last   = time.time() - state.get("last_news_poll", 0)
    force_news        = whale_flow_trigger or bool(
        _price_state.tick_history and
        abs(_price_state.rolling_change_pct() or 0) >= CONFIG["price_watcher"]["trigger_pct_from_open"]
    )

    if time_since_last < poll_interval_sec and not force_news:
        all_headlines = []
    else:
        if force_news and time_since_last < poll_interval_sec:
            log.info("RSS: forced fetch — active price/whale trigger")
        all_headlines, state    = fetch_rss_headlines(state)
        state["last_news_poll"] = time.time()
        log.info(f"RSS: {len(all_headlines)} new headlines")

    # Bloomberg posts arrive via /signal — no fetch needed here
    # TODO: implement signal buffer for run_poll() integration
    bloomberg_headlines = []

    for theme in CONFIG["themes"]:
        if not theme.get("active"):
            continue

        theme_name         = theme["name"]
        relevant_headlines = haiku_filter_news(all_headlines, theme)

        if not relevant_headlines and not whale_flow_trigger:
            continue

        analysis = sonnet_analyze(relevant_headlines, whale_signals, theme)
        if not analysis:
            continue

        score = analysis.get("score", 0)
        if score < CONFIG["alert_threshold"]:
            for h in relevant_headlines:
                state["seen_news_urls"][h["url"]] = time.time()
            continue

        trigger_parts = []
        if relevant_headlines:
            trigger_parts.append(f"{len(relevant_headlines)} news signal(s)")
        if whale_trigger_reason:
            trigger_parts.append(whale_trigger_reason)
        trigger_str = " + ".join(trigger_parts) if trigger_parts else "scheduled poll"

        message = format_alert(
            theme, analysis, relevant_headlines, whale_signals, ig_data,
            trigger_reason=trigger_str,
        )
        send_message(message)

        for h in relevant_headlines:
            state["seen_news_urls"][h["url"]] = time.time()

        state["weekly_signals"].append({
            "theme":     theme_name,
            "score":     score,
            "summary":   analysis.get("summary", ""),
            "timestamp": datetime.now().isoformat(),
        })

    save_state(state)
    log.info("── Poll cycle complete ──")


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def _handle_telegram_signal(stype: str, data: dict):
    """Handle incoming Telegram commands."""
    log.info(f"📩 Telegram command: /{stype}")

    if stype == 'help':
        send_message(
            "🎩 <b>MonsieurMarket Commands</b>\n\n"
            "/straddle 500 — open balanced straddle\n\n"
            "/status — show open positions\n"
            "/pause  — pause autonomous trading\n"
            "/resume — resume autonomous trading\n"
            "/help   — this message"
        )

    elif stype == 'straddle':
        parts = data.get('parts', [])
        if len(parts) < 2:
            send_message("Usage: /straddle <notional>\ne.g. /straddle 500")
            return
        try:
            notional = float(parts[1])
        except ValueError:
            send_message("❌ Invalid notional — use a number e.g. /straddle 500")
            return
        execute_trade({'action': 'straddle', 'notional': notional, 'source': 'telegram'})

    elif stype == 'status':
        _send_status()

    elif stype == 'pause':
        send_message("⏸ Autonomous trading paused")

    elif stype == 'resume':
        send_message("▶️ Autonomous trading resumed")

    else:
        send_message(f"❓ Unknown command: /{stype}\nTry /help")


def execute_trade(intent: dict):
    action = intent.get('action')
    if action == 'straddle':
        _open_straddle(intent.get('notional', 250))
    elif action == 'close':
        _close_straddle()

def _open_straddle(notional: float):
    """Fetch IG knockouts and execute balanced straddle — TODO."""
    send_message(
        f"🛢 Straddle requested — {notional:.0f}€\n"
        f"⚙️ Fetching IG knockout products...\n"
        f"(not yet implemented)"
    )

def _close_straddle():
    """Close all open straddle legs — TODO."""
    send_message(
        "🔴 Close straddle requested\n"
        "(not yet implemented)"
    )

def _send_status():
    """Placeholder — show current positions."""
    send_message(
        "📊 <b>Status</b>\n"
        "No open positions\n"
        "(not yet implemented)"
    )

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
_monitor_proc = None


def start_bloomberg_monitor():
    """
    Launch bloomberg monitor as a managed subprocess.
    MM owns the monitor lifecycle — if it crashes, MM can restart it.
    --visible flag is passed through from MM's own argv.
    """
    global _monitor_proc
    import subprocess

    cmd = [sys.executable, 'bloomberg/monitor.py']
    if '--visible' in sys.argv:
        cmd.append('--visible')
        log.info("🦊 Starting Bloomberg monitor (headful)...")
    else:
        log.info("🦊 Starting Bloomberg monitor (headless)...")

    _monitor_proc = subprocess.Popen(cmd)
    log.info(f"Bloomberg monitor started (pid={_monitor_proc.pid})")


def main():
    log.info("MonsieurMarket starting up 🎩")

    missing = [
        v for v in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
        if not os.getenv(v)
    ]
    if missing:
        log.error(f"Missing environment variables: {', '.join(missing)}")
        return

    markets = load_polymarket_markets()
    if not markets:
        log.warning("No Polymarket markets loaded — whale tracking inactive")
    else:
        log.info(f"Loaded {len(markets)} Polymarket market(s)")

    ig_status = "not configured"
    if IG_AVAILABLE and all(os.getenv(v) for v in ["IG_USERNAME", "IG_PASSWORD", "IG_API_KEY"]):
        ig_svc    = _get_ig_service()
        ig_status = "✅ connected (demo)" if ig_svc else "❌ login failed"
    elif not IG_AVAILABLE:
        ig_status = "⚠️ trading-ig not installed"

    # Start MM Signal API first so monitor can POST /signal on startup
    start_mm_api()

    # Launch monitor — it will signal MM when ready
    start_bloomberg_monitor()

    # Start Telegram polling
    start_telegram_receiver()

    send_message(
        "🎩 <b>MonsieurMarket is online</b>\n"
        f"Signal API: port 3456 🧠\n"
        f"Bloomberg monitor: starting (will signal when ready)\n"
        f"Trump watcher: every 2 min 🇺🇸\n"
        f"Active themes: {', '.join(t['name'] for t in CONFIG['themes'] if t.get('active'))}\n"
        f"Polymarket markets: {len(markets)} loaded\n"
        f"Whale threshold: ${CONFIG['whale_threshold_usd']:,}\n"
        f"IG Markets: {ig_status}"
    )

    start_price_watcher()
    start_polymarket_ws(
        send_telegram_fn=send_message,
        load_state_fn=load_state,
        save_state_fn=save_state,
    )
    start_trump_watcher()

    global event_watcher
    event_watcher = ScheduledEventWatcher(
        send_telegram_fn=send_message,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "")
    )
    log.info("Event watcher initialised ✅")

    log.info("All workers started. Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    finally:
        if _monitor_proc:
            log.info("Stopping Bloomberg monitor...")
            _monitor_proc.terminate()
            _monitor_proc.wait(timeout=5)


if __name__ == "__main__":
    main()