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
from ig import get_ig_service, check_ig_brent, format_ig_block
from ig import start_price_watcher, register_tick_callback, price_state
from ig import open_straddle, close_straddle

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
# IG — PRICE TICK CALLBACK
# MM owns all reaction logic — streamer just delivers ticks
# ─────────────────────────────────────────────
def _on_brent_price(mid, bid, ask, day_pct, day_open, day_high, day_low):
    """MM's reaction to every Brent tick."""
    if event_watcher is not None:
        event_watcher.on_tick(mid)

    cfg = CONFIG["price_watcher"]
    if not price_state.can_alert(cfg["cooldown_min"]):
        return

    def valid(v):
        return v is not None and v != 0

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
        chg = price_state.change_pct_over_window(window_min)
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
        f"Bid: {bid:.2f}  Ask: {ask:.2f}\n"
        if valid(bid) and valid(ask)
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
    price_state.mark_alerted()
    log.info(f"Price alert sent — {alert_reason[:60]}")
    bloomberg_scheduler.trigger_now(reason=alert_reason[:60])


# ─────────────────────────────────────────────
# BLOOMBERG SCHEDULER — MM owns the refresh timer
# ─────────────────────────────────────────────
class BloombergScheduler:
    def __init__(self):
        self._timer = None
        self._lock  = threading.Lock()
        self._ready = False

    def on_ready(self):
        self._ready = True
        log.info("Bloomberg scheduler: monitor ready — starting refresh schedule")
        self._schedule_next()

    def on_no_source(self):
        self._ready = False
        self.cancel()
        retry_sec = self._interval_sec()
        log.info(f"Bloomberg scheduler: no source — retrying in {retry_sec // 60} min")
        self._schedule_in(retry_sec)

    def trigger_now(self, reason: str = ''):
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
        self._schedule_in(interval)

    def _trigger_refresh(self):
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self):
        try:
            log.info("Bloomberg scheduler: calling /refresh on monitor...")
            r = requests.get('http://localhost:3457/refresh', timeout=35)
            r.raise_for_status()
            log.debug("Bloomberg scheduler: /refresh complete")
        except Exception as e:
            log.warning(f"Bloomberg scheduler: /refresh failed: {e}")
        finally:
            if self._ready:
                self._schedule_next()
            # else: on_no_source() or on_liveblog_ended() will re-schedule
            # based on the signal MM receives back from the monitor

    def _schedule_in(self, seconds: int):
        """Schedule a single refresh after a fixed delay."""
        with self._lock:
            self._timer = threading.Timer(seconds, self._trigger_refresh)
            self._timer.daemon = True
            self._timer.start()

    def on_liveblog_ended(self, cooldown_min: int = 10):
        """Wait before retrying — old liveblog may still be on homepage."""
        self._ready = False
        self.cancel()
        cooldown_sec = cooldown_min * 60
        log.info(f"Bloomberg scheduler: liveblog ended — retrying homepage in {cooldown_min}min")
        self._schedule_in(cooldown_sec)

    def _interval_sec(self) -> int:
        now  = datetime.now(timezone.utc)
        hour = now.hour

        if 6 <= hour < 20:
            return 5 * 60 + random.randint(-30, 90)

        elif 4 <= hour < 6:
            next_refresh = now + timedelta(seconds=15 * 60)
            if next_refresh.hour >= 6:
                target = now.replace(hour=6, minute=0, second=0, microsecond=0)
                secs = int((target - now).total_seconds())
                log.info(f"Bloomberg scheduler: aligning to market open in {secs // 60}min")
                return secs
            return 15 * 60 + random.randint(-60, 60)

        else:
            next_refresh = now + timedelta(seconds=60 * 60)
            if next_refresh.hour >= 4:
                target = now.replace(hour=4, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                secs = int((target - now).total_seconds())
                log.info(f"Bloomberg scheduler: aligning to pre-market in {secs // 60}min")
                return secs
            return 60 * 60


bloomberg_scheduler = BloombergScheduler()


# ─────────────────────────────────────────────
# MM FLASK SIGNAL API — port 3456
# ─────────────────────────────────────────────
mm_app = Flask('MonsieurMarketAPI')


@mm_app.route('/signal', methods=['POST'])
def receive_signal():
    try:
        payload = request.get_json(force=True)
        if not payload:
            return jsonify({'status': 'error', 'msg': 'no payload'}), 400

        source = payload.get('source', '')
        stype  = payload.get('type', '')
        data   = payload.get('data', {})

        log.info(f"📨 Signal: {source}/{stype}")

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
    try:
        if source == 'bloomberg':
            _handle_bloomberg_signal(stype, data)
        elif source == 'trump':
            _handle_trump_signal(stype, data)
        elif source == 'telegram':
            _handle_telegram_signal(stype, data)
    except Exception as e:
        log.error(f"Signal handler error ({source}/{stype}): {e}")


def _handle_bloomberg_signal(stype: str, data: dict):
    if stype == 'ready':
        src  = data.get('source_type', '?')
        url  = data.get('source_url', '')[:70]
        log.info(f"Bloomberg ready: {src} — {url}")
        bloomberg_scheduler.on_ready()

    elif stype == 'no_source':
        log.info("Bloomberg: no source — scheduler will retry")
        bloomberg_scheduler.on_no_source()

    elif stype == 'liveblog_ended':
        log.info("Bloomberg: liveblog ended")
        bloomberg_scheduler.on_liveblog_ended() 
        send_message(
            "📰 <b>Bloomberg liveblog ended</b>\n"
            "Will attempt to scan homepage.\n"
            f"⏰ {datetime.now().strftime('%d/%m %H:%M')}"
        )

    elif stype == 'post':
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
    if stype != 'post':
        return

    post  = data
    title = post.get('title', '')
    log.info(f"Trump signal received: {title[:80]}")

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
    def _run():
        log.info("🧠 MM Signal API on port 3456")
        mm_app.run(host='0.0.0.0', port=3456, debug=False, use_reloader=False)
    t = threading.Thread(target=_run, name='MMApi', daemon=True)
    t.start()
    time.sleep(1)
    log.info("MM Signal API ready")


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def _handle_telegram_signal(stype: str, data: dict):
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
        parts       = data.get('parts', [])
        notional    = float(parts[1]) if len(parts) > 1 else CONFIG['ig']['default_notional']
        barrier_pct = float(parts[2]) / 100 if len(parts) > 2 else CONFIG['ig']['default_barrier_pct']
        execute_trade({
            'action':      'straddle',
            'notional':    notional,
            'barrier_pct': barrier_pct,
            'source':      'telegram',
        })

    elif stype == 'status':
        _send_status()

    elif stype == 'pause':
        send_message("⏸ Autonomous trading paused")

    elif stype == 'resume':
        send_message("▶️ Autonomous trading resumed")

    elif stype == 'discover':
        svc = get_ig_service()
        try:
            # fetch market navigation nodes
            result = svc.fetch_top_level_navigation_nodes()
            log.info(f"top level nodes: {result}")
        except Exception as e:
            log.error(f"top nodes failed: {e}")
    
        try:
            # try with known knockout epic format variations
            for epic in [
                'CC.D.LCO.OPTCALL.IP.9000',
                'KO.D.LCO.CALL.9000.IP',
                'IX.D.LCO.OPTCALL.9000.IP',
            ]:
                try:
                    result = svc.fetch_market_by_epic(epic)
                    log.info(f"✅ {epic}: {result}")
                except Exception as e:
                    log.info(f"❌ {epic}: {e}")
        except Exception as e:
            log.error(f"epic test failed: {e}")

    else:
        send_message(f"❓ Unknown command: /{stype}\nTry /help")


def execute_trade(intent: dict):
    action = intent.get('action')
    if action == 'straddle':
        _open_straddle(
            intent.get('notional',    CONFIG['ig']['default_notional']),
            intent.get('barrier_pct', CONFIG['ig']['default_barrier_pct']),
        )
    elif action == 'close':
        _close_straddle()


def _open_straddle(notional: float, barrier_pct: float):
    from ig.straddle import open_straddle
    svc = get_ig_service()
    if not svc:
        send_message("❌ IG service not available")
        return
    send_message(f"🛢 Opening straddle — {notional:.0f}€ barrier {barrier_pct*100:.0f}%...")
    result = open_straddle(svc, notional=notional, barrier_pct=barrier_pct)
    if not result:
        send_message("❌ Straddle failed")
    elif 'error' in result:
        send_message(f"❌ {result['error']}")
        if 'call' in result:
            send_message("⚠️ Call leg is open — close manually on IG!")
    else:
        send_message(
            f"✅ Straddle opened\n"
            f"📈 Call KO: {result['call_barrier']} "
            f"({(result['current_price']-result['call_barrier'])/result['current_price']*100:.1f}%↓)\n"
            f"📉 Put KO:  {result['put_barrier']} "
            f"({(result['put_barrier']-result['current_price'])/result['current_price']*100:.1f}%↑)\n"
            f"💰 Brent: {result['current_price']:.0f}"
        )


def _close_straddle():
    send_message(
        "🔴 Close straddle requested\n"
        "(not yet implemented)"
    )


def _send_status():
    send_message(
        "📊 <b>Status</b>\n"
        "No open positions\n"
        "(not yet implemented)"
    )


# ─────────────────────────────────────────────
# POLLING LOOP (disabled — too expensive)
# ─────────────────────────────────────────────
def fetch_rss_headlines(state: dict) -> tuple[list[dict], dict]:
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
        price_state.tick_history and
        abs(price_state.rolling_change_pct() or 0) >= CONFIG["price_watcher"]["trigger_pct_from_open"]
    )

    if time_since_last < poll_interval_sec and not force_news:
        all_headlines = []
    else:
        if force_news and time_since_last < poll_interval_sec:
            log.info("RSS: forced fetch — active price/whale trigger")
        all_headlines, state    = fetch_rss_headlines(state)
        state["last_news_poll"] = time.time()
        log.info(f"RSS: {len(all_headlines)} new headlines")

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
# ENTRY POINT
# ─────────────────────────────────────────────
_monitor_proc = None


def start_bloomberg_monitor():
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
    if all(os.getenv(v) for v in ["IG_USERNAME", "IG_PASSWORD", "IG_API_KEY"]):
        ig_svc    = get_ig_service()
        ig_status = "✅ connected (demo)" if ig_svc else "❌ login failed"

    start_mm_api()
    start_bloomberg_monitor()
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

    register_tick_callback(_on_brent_price)
    start_price_watcher(CONFIG)
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