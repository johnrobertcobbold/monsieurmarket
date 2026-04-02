#!/opt/homebrew/bin/python3
"""
MonsieurMarket — Personal Trading Intelligence Bot
Monitors geopolitical signals and whale trades, alerts via Telegram.

Setup:
    pip install anthropic requests schedule python-dotenv

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
import schedule
import requests
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from scheduled_event_watcher import ScheduledEventWatcher

from dotenv import load_dotenv
load_dotenv()  # loads .env from same directory

import anthropic

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
        logging.FileHandler("monsieur_market.log"),
    ],
)
log = logging.getLogger("MonsieurMarket")


# ─────────────────────────────────────────────
# CONFIGURATION — edit this section
# ─────────────────────────────────────────────
CONFIG = {
    # Polling interval in minutes — news is background context, not the trigger
    # Real triggers are price moves (streaming) and whale flow (Polymarket)
    "poll_interval_minutes": 60,

    # Deduplication window in hours
    # Signals older than this are forgotten and can re-alert
    "dedup_window_hours": 4,

    # Minimum Sonnet materiality score to trigger alert (1–10)
    "alert_threshold": 6,

    # Quiet hours (Paris time, 24h format) — no alerts except YANBU override
    "quiet_hours_start": 0,   # midnight
    "quiet_hours_end": 7,     # 7am

    # Minimum trade size to track at all (goes into ledger)
    "whale_threshold_usd": 10000,
    # Minimum single trade for INSTANT Telegram alert (bypass accumulation)
    "whale_instant_alert_usd": 100000,

    # External file for Polymarket markets — edit without restarting the bot
    # To refresh tokens: python3 -c "import requests,json; d=requests.get('https://gamma-api.polymarket.com/events/158299').json(); [print(m['question'], json.loads(m['clobTokenIds'])) for m in d['markets']]"
    "polymarket_markets_file": "polymarket_markets.json",

    # ── THEMES ──────────────────────────────────────────────────────────────
    "themes": [
        {
            "name": "US Ground Op / Kharg",
            "active": True,
            "wake_override": False,  # respects quiet hours
            "keywords": [
                "Kharg", "Kharg Island", "Hormuz", "Strait of Hormuz",
                "boots on the ground", "ground forces Iran",
                "US troops Iran", "special forces Iran",
                "Iran military operation", "Iran invasion",
                "carrier group Iran", "amphibious Iran",
            ],
            "tradables": [
                {
                    "name": "Brent Crude",
                    "ticker": "BRN",
                    "type": "turbo_long",
                    "my_position": "long",
                    "signal_direction": "bullish",
                    "conviction": "high",
                },
                {
                    "name": "Thales",
                    "ticker": "HO.PA",
                    "type": "equity",
                    "my_position": "long",
                    "signal_direction": "bullish",
                    "conviction": "high",
                },
                {
                    "name": "TotalEnergies",
                    "ticker": "TTE.PA",
                    "type": "equity",
                    "my_position": "watching",
                    "signal_direction": "bullish",
                    "conviction": "medium",
                },
                {
                    "name": "FTSE 100",
                    "ticker": "UKX",
                    "type": "index",
                    "my_position": "watching",
                    "signal_direction": "bullish",
                    "conviction": "medium",
                    "note": "Oil-heavy index, benefits from Brent spike",
                },
                {
                    "name": "CAC 40",
                    "ticker": "PX1",
                    "type": "index",
                    "my_position": "short_candidate",
                    "signal_direction": "bearish",
                    "conviction": "medium",
                    "note": "Luxury-heavy, hurt by oil shock and risk-off",
                },
            ],
        },
        {
            "name": "Houthi Escalation",
            "active": True,
            "wake_override": False,  # set True per escalation trigger below
            "keywords": [
                "Houthi", "Houthis", "Ansarallah",
                "Yemen", "Yemen strike", "Yemen missile",
                "Red Sea attack", "Red Sea shipping",
                "tanker attack", "shipping disruption",
                "Bab el-Mandeb", "Gulf of Aden",
                "Houthi missile Israel", "Houthi drone",
            ],
            # These keywords trigger wake override AND level-3 alert
            "escalation_triggers": [
                "Yanbu",
                "Saudi oil",
                "Aramco",
                "Ras Tanura",
            ],
            "tradables": [
                {
                    "name": "Brent Crude",
                    "ticker": "BRN",
                    "type": "turbo_long",
                    "my_position": "long",
                    "signal_direction": "bullish",
                    "conviction": "high",
                },
                {
                    "name": "Thales",
                    "ticker": "HO.PA",
                    "type": "equity",
                    "my_position": "long",
                    "signal_direction": "bullish",
                    "conviction": "medium",
                },
                {
                    "name": "TotalEnergies",
                    "ticker": "TTE.PA",
                    "type": "equity",
                    "my_position": "watching",
                    "signal_direction": "bullish",
                    "conviction": "medium",
                },
            ],
            # Houthi escalation ladder
            "alert_levels": {
                "level_1": {
                    "description": "Houthis firing at Israel",
                    "score_range": [4, 6],
                    "note": "Ongoing background noise, mild Brent positive",
                },
                "level_2": {
                    "description": "Houthis targeting Red Sea shipping",
                    "score_range": [6, 8],
                    "note": "Brent +$5-10 expected. Check turbo barrier.",
                },
                "level_3": {
                    "description": "Houthis targeting Yanbu / Saudi infrastructure",
                    "score_range": [8, 10],
                    "note": "Bloomberg Economics: $140 Brent scenario. ACT NOW.",
                    "wake_override": True,  # always wake for this
                },
            },
        },

        # ── Future themes — flip active: True to enable ──────────────────
        {
            "name": "European Rearmament",
            "active": False,
            "wake_override": False,
            "keywords": [
                "NATO spending", "defense budget Europe",
                "rearmament", "European army",
                "defense procurement",
            ],
            "tradables": [
                {"name": "Rheinmetall", "ticker": "RHM.DE",
                 "signal_direction": "bullish", "conviction": "high"},
                {"name": "Leonardo", "ticker": "LDO.MI",
                 "signal_direction": "bullish", "conviction": "high"},
                {"name": "BAE Systems", "ticker": "BA.L",
                 "signal_direction": "bullish", "conviction": "high"},
            ],
        },
        {
            "name": "French Political Risk",
            "active": False,
            "wake_override": False,
            "keywords": [
                "dissolution Assemblée", "élections France",
                "motion de censure", "gouvernement chute",
                "crise politique France",
            ],
            "tradables": [
                {"name": "CAC 40", "ticker": "PX1",
                 "signal_direction": "bearish", "conviction": "high"},
            ],
        },
    ],

    # ── PRICE WATCHER (Lightstreamer real-time stream) ──────────────────────
    "price_watcher": {
        "enabled": True,
        "trigger_pct_from_open": 1.5,   # alert if day move exceeds this %
        "cooldown_min": 15,             # minimum minutes between price alerts
        # Multiple rolling windows — catches moves at different speeds
        # Format: (window_minutes, threshold_pct, label)
        "rolling_windows": [
            # (1,  0.3, "1min"),  # disabled — too noisy without IG execution
            (5,  0.6,  "5min"),   # fast move — 0.6% in 5min
            (10, 1.0,  "10min"),  # sustained move — 1.0% in 10min
        ],
    },

    # ── WHALE AGGREGATION ───────────────────────────────────────────────────
    "whale_triggers": {
        # Single trade above this → always triggers Sonnet (strong conviction)
        "single_trade_usd": 50000,
        # One trader's cumulative flow this cycle above this → triggers
        "single_trader_usd": 75000,
        # Net directional flow (all Yes buys minus all No buys) above this → triggers
        # Must also be >70% one-directional (not mixed)
        "net_flow_usd": 150000,
        "net_flow_min_pct": 0.70,   # 70% of volume must be one direction
    },

    # ── WEEKLY DIGEST ───────────────────────────────────────────────────────
    "weekly_digest": {
        "enabled": True,
        "day": "sunday",
        "time": "09:00",  # Paris time
    },
}


# ─────────────────────────────────────────────
# POLYMARKET MARKETS — loaded from external JSON
# ─────────────────────────────────────────────
MARKETS_FILE = Path(CONFIG["polymarket_markets_file"])


def load_polymarket_markets() -> list:
    """
    Load Polymarket markets from external JSON file.
    Hot-reloadable — edit polymarket_markets.json without restarting the bot.
    """
    try:
        markets = json.loads(MARKETS_FILE.read_text())
        markets = [m for m in markets if m.get("active", True)]        
        log.debug(f"Loaded {len(markets)} Polymarket market(s) from {MARKETS_FILE}")
        return markets
    except FileNotFoundError:
        log.error(f"Polymarket markets file not found: {MARKETS_FILE}")
        return []
    except json.JSONDecodeError as e:
        log.error(f"Invalid JSON in {MARKETS_FILE}: {e}")
        return []
    except Exception as e:
        log.error(f"Failed to load {MARKETS_FILE}: {e}")
        return []


# ─────────────────────────────────────────────
# STATE — remembers what we already alerted on
# ─────────────────────────────────────────────
STATE_FILE = Path("monsieur_market_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "seen_news_urls": {},        # url -> timestamp
        "seen_trade_ids": {},        # trade_id -> timestamp
        "last_prices": {},           # market_slug -> price
        "weekly_signals": [],        # accumulated for digest
        "last_digest": None,
        "trump_last_seen_url": None, # last Trump post URL we processed
        "whale_ledger": {},          # pseudonym -> [{amount, side, outcome, ts}]
        "last_news_poll": 0,         # epoch of last RSS+Haiku cycle
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def clean_state(state: dict) -> dict:
    """Remove entries older than dedup window."""
    window = CONFIG["dedup_window_hours"] * 3600
    now = time.time()
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
# TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram(message: str, force: bool = False) -> bool:
    """Send message to Telegram. Respects quiet hours unless force=True."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials not set")
        return False

    # Quiet hours check (Paris = UTC+2 in summer, UTC+1 in winter)
    # Simple approach: use UTC+1 as approximation
    paris_hour = (datetime.now(timezone.utc).hour + 1) % 24
    in_quiet = (
        CONFIG["quiet_hours_start"] <= paris_hour < CONFIG["quiet_hours_end"]
    )
    if in_quiet and not force:
        log.info(f"Quiet hours — suppressing alert (Paris hour: {paris_hour})")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram alert sent")
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ─────────────────────────────────────────────
# POLYMARKET
# ─────────────────────────────────────────────
POLYMARKET_DATA_API = "https://data-api.polymarket.com"


def fetch_polymarket_trades(condition_id: str) -> list:
    """Fetch recent whale trades for a Polymarket market via data-api."""
    try:
        r = requests.get(
            f"{POLYMARKET_DATA_API}/trades",
            params={
                "market": condition_id,
                "limit": 50,
                "takerOnly": "true",
                "filterType": "CASH",
                "filterAmount": CONFIG["whale_threshold_usd"],
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        log.warning(f"Polymarket fetch failed for {condition_id[:16]}…: {e}")
        return []


def check_polymarket(state: dict) -> tuple[list, dict]:
    """
    Check all watched markets for new whale trades and price moves.
    Returns (new_signals, updated_state).
    """
    signals = []
    threshold = CONFIG["whale_threshold_usd"]
    markets = load_polymarket_markets()

    for market_config in markets:
        condition_id = market_config["conditionId"]
        label        = market_config["label"]
        trades       = fetch_polymarket_trades(condition_id)

        if not trades:
            log.debug(f"Polymarket: no trades for {label}")

        for trade in trades:
            try:
                trade_id = trade.get("transactionHash", "")
                size     = float(trade.get("size", 0))
                price    = float(trade.get("price", 0))
                outcome  = trade.get("outcome", "")
                side     = trade.get("side", "BUY")
                pseudonym  = trade.get("pseudonym") or ""
                proxy      = trade.get("proxyWallet", "")
                trader     = pseudonym or proxy or "anon"
                amount     = size * price  # CASH value of trade

                if trade_id in state["seen_trade_ids"]:
                    continue

                state["seen_trade_ids"][trade_id] = time.time()

                # Save wallet mapping for portfolio lookups
                if pseudonym and proxy:
                    wallets = state.setdefault("whale_wallets", {})
                    wallets[pseudonym] = proxy

                if amount < threshold:
                    continue

                trade_ts = trade.get("timestamp") or trade.get("createdAt") or time.time()
                try:
                    trade_ts = float(trade_ts)
                except Exception:
                    trade_ts = time.time()

                signals.append({
                    "type": "whale_trade",
                    "market": label,
                    "trader": str(trader)[:30],
                    "proxy_wallet": proxy,
                    "amount": amount,
                    "outcome": outcome,
                    "price": price,
                    "side": side,
                    "trade_id": trade_id,
                    "ts": trade_ts,
                })
                log.info(f"🐋 Polymarket whale: {trader} ${amount:,.0f} {side} {outcome} @ {price:.2f} on {label}")

            except Exception as e:
                log.debug(f"Trade parse error: {e}")
                continue

        # Check price movement (last trade price vs previous)
        try:
            if trades:
                latest_price = float(trades[0].get("price", 0))
                last_price   = state["last_prices"].get(condition_id)
                if last_price and abs(latest_price - last_price) >= 0.05:
                    signals.append({
                        "type": "price_move",
                        "market": label,
                        "from_price": last_price,
                        "to_price": latest_price,
                        "move_pct": (latest_price - last_price) / last_price * 100,
                    })
                state["last_prices"][condition_id] = latest_price
        except Exception:
            pass

    return signals, state


# ─────────────────────────────────────────────
# HAIKU — cheap first-pass filter
# ─────────────────────────────────────────────
def haiku_filter_news(headlines: list[dict], theme: dict) -> list[dict]:
    """
    Use Haiku to quickly check if any headlines are relevant to a theme.
    Returns filtered list of relevant headlines.
    """
    if not headlines:
        return []

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    keywords = theme["keywords"]
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
        text = response.content[0].text.strip()
        # Strip markdown fences if Haiku wrapped the response
        text = text.replace("```json", "").replace("```", "").strip()
        # Extract JSON array if there's surrounding text
        start = text.find("[")
        end   = text.rfind("]") + 1
        if start != -1 and end > start:
            text = text[start:end]
        relevant_urls = json.loads(text)
        return [h for h in headlines if h.get("url") in relevant_urls]
    except Exception as e:
        log.warning(f"Haiku filter error: {e}")
        # If Haiku fails, fall back to keyword matching
        kw_lower = [k.lower() for k in keywords]
        return [
            h for h in headlines
            if any(kw in h.get("title", "").lower() for kw in kw_lower)
        ]


# ─────────────────────────────────────────────
# SONNET — deep analysis + materiality scoring
# ─────────────────────────────────────────────
def sonnet_analyze(
    news_signals: list[dict],
    whale_signals: list[dict],
    theme: dict,
) -> dict | None:
    """
    Use Sonnet with web search to deeply analyze signals.
    Returns analysis dict or None if nothing material.
    """
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

    # Aggregate whale signals for Sonnet
    whale_agg = aggregate_whale_signals([s for s in whale_signals if s.get("type") == "whale_trade"])

    raw_trades = "\n".join(
        f"  - {s['trader']} ${s['amount']:,.0f} {s['side']} {s['outcome']} "
        f"@ {s['price']:.2f} on {s['market']}"
        if s["type"] == "whale_trade"
        else f"  - Price moved {s['move_pct']:+.1f}% on {s['market']}"
        for s in whale_signals[:10]  # cap at 10 raw trades to save tokens
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

    # Check for escalation triggers (Houthi theme)
    escalation_triggers = theme.get("escalation_triggers", [])
    all_text = news_text + whale_text
    triggered = [t for t in escalation_triggers if t.lower() in all_text.lower()]

    prompt = f"""You are a geopolitical trading analyst for a private trader.

THEME: {theme['name']}

TRADABLES THE TRADER HOLDS OR WATCHES:
{tradables_text}

NEW NEWS SIGNALS:
{news_text}

NEW POLYMARKET WHALE SIGNALS:
{whale_text}

{"⚠️ NOTE: No news signals this cycle — analysis is WHALE-FLOW-ONLY. Focus entirely on what the Polymarket positioning implies about smart money expectations." if not news_signals else ""}

{"⚠️ ESCALATION TRIGGER DETECTED: " + ", ".join(triggered) if triggered else ""}

Your task:
1. Search the web to fetch full context on the most important news signals above
2. Assess overall materiality for the trader's thesis on a scale of 1-10
3. For each tradable, state impact: BULLISH / BEARISH / NEUTRAL + one sentence why
4. If you see tradables the trader is MISSING that are highly relevant to this theme, list up to 3 suggestions
5. Write a brief alert summary (3-5 sentences max, direct and actionable)

Respond in this exact JSON format:
{{
  "score": <int 1-10>,
  "summary": "<3-5 sentence alert summary>",
  "tradable_impacts": [
    {{"name": "<ticker>", "impact": "BULLISH|BEARISH|NEUTRAL", "reason": "<one sentence>"}}
  ],
  "missing_tradables": [
    {{"name": "<name>", "ticker": "<ticker>", "reason": "<why relevant>"}}
  ],
  "escalation_triggered": <true|false>,
  "wake_override": <true|false>
}}

Return raw JSON only. No markdown."""

    def _parse_sonnet_response(response) -> dict | None:
        """Extract JSON from a Sonnet response, handling multi-block tool-use responses."""
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
        # First attempt: with web search
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
            # Fallback: no web search, just reason from the signals provided
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

        # Force wake override if escalation trigger found
        if triggered:
            result["wake_override"] = True
            result["escalation_triggered"] = True

        return result

    except Exception as e:
        log.error(f"Sonnet analysis error: {e}")
        return None


# ─────────────────────────────────────────────
# ALERT FORMATTING — Haiku formats the message
# ─────────────────────────────────────────────
def format_alert(
    theme: dict,
    analysis: dict,
    news_signals: list,
    whale_signals: list,
    ig_data: dict | None = None,
    trigger_reason: str = "",
) -> str:
    """Format the Telegram alert message."""
    score = analysis.get("score", 0)

    # Severity emoji
    if score >= 9:
        severity = "🚨 ACT NOW"
    elif score >= 7:
        severity = "⚠️ WATCH"
    else:
        severity = "📰 FYI"

    # Escalation override
    if analysis.get("escalation_triggered"):
        severity = "🚨🚨 ESCALATION ALERT"

    # Summary — strip trading advice (positions block covers it)
    summary = analysis.get("summary", "")
    # Truncate at the last sentence before any "Stay long / short / initiate" advice
    advice_triggers = ["Stay long", "Stay short", "Consider initiating", "warrants initiating",
                       "initiate a", "add to", "close your", "take profit"]
    for trigger in advice_triggers:
        idx = summary.find(trigger)
        if idx > 0:
            # Cut at the sentence boundary before the advice
            cut = summary.rfind(". ", 0, idx)
            if cut > 0:
                summary = summary[:cut + 1]
            break

    lines = [
        f"<b>MonsieurMarket — {theme['name']}</b>",
        f"{severity} | Score: {score}/10",
    ]

    # Trigger line — what fired this alert
    if trigger_reason:
        lines.append(f"⚡ <i>Triggered by: {trigger_reason}</i>")

    lines += ["", f"📋 {summary}"]

    # News signals
    if news_signals:
        lines.append("\n📰 <b>News:</b>")
        for s in news_signals[:3]:
            lines.append(f"  • [{s.get('source','')}] {s.get('title','')[:80]}")

    # Whale signals
    if whale_signals:
        lines.append("\n🐋 <b>Polymarket Whales:</b>")
        for s in whale_signals[:5]:
            if s["type"] == "whale_trade":
                # Show timestamp if available
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

    # Tradable impacts
    impacts = analysis.get("tradable_impacts", [])
    if impacts:
        lines.append("\n📊 <b>Your Positions:</b>")
        icons = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}
        for t in impacts:
            icon = icons.get(t.get("impact", ""), "⚪")
            lines.append(f"  {icon} <b>{t['name']}</b> — {t.get('reason','')[:60]}")

    # Missing tradables suggestion
    missing = analysis.get("missing_tradables", [])
    if missing:
        lines.append("\n💡 <b>Sonnet suggests you're not tracking:</b>")
        for m in missing:
            lines.append(f"  • {m['name']} ({m['ticker']}) — {m.get('reason','')[:60]}")

    # IG live price block
    ig_block = format_ig_block(ig_data) if ig_data else ""
    if ig_block:
        lines.append(ig_block)

    lines.append(f"\n⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# WEEKLY DIGEST
# ─────────────────────────────────────────────
def send_weekly_digest(state: dict):
    """Send Sunday morning digest via Sonnet."""
    if not CONFIG["weekly_digest"]["enabled"]:
        return

    log.info("Generating weekly digest...")
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    signals = state.get("weekly_signals", [])
    active_themes = [t["name"] for t in CONFIG["themes"] if t.get("active")]
    tradables = []
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

        message = (
            f"<b>☕ MonsieurMarket — Weekly Digest</b>\n"
            f"Week ending {datetime.now().strftime('%d/%m/%Y')}\n\n"
            f"{digest_text}"
        )
        send_telegram(message, force=True)  # always send digest

        # Reset weekly signals
        state["weekly_signals"] = []
        state["last_digest"] = time.time()
        save_state(state)

    except Exception as e:
        log.error(f"Weekly digest error: {e}")


# ─────────────────────────────────────────────
# IG MARKETS — REAL-TIME PRICE STREAM
# ─────────────────────────────────────────────
class _PriceState:
    def __init__(self):
        self.last_alert_time = 0
        # Keep enough history for the longest window (10 min + buffer)
        self.tick_history = deque()  # (epoch, mid)

    def add_tick(self, mid: float):
        now = time.time()
        self.tick_history.append((now, mid))
        # Keep 15 min of history max
        cutoff = now - 15 * 60
        while self.tick_history and self.tick_history[0][0] < cutoff:
            self.tick_history.popleft()

    def change_pct_over_window(self, window_min: float) -> float | None:
        """% change from oldest tick within window_min to latest tick."""
        if len(self.tick_history) < 2:
            return None
        now    = time.time()
        cutoff = now - window_min * 60
        # Find oldest tick within the window
        window_ticks = [t for t in self.tick_history if t[0] >= cutoff]
        if len(window_ticks) < 2:
            return None
        oldest = window_ticks[0][1]
        latest = window_ticks[-1][1]
        return (latest - oldest) / oldest * 100 if oldest else None

    def rolling_change_pct(self):
        """Compat — returns 10min change."""
        return self.change_pct_over_window(10)

    def can_alert(self):
        return (time.time() - self.last_alert_time) > \
               CONFIG["price_watcher"]["cooldown_min"] * 60

    def mark_alerted(self):
        self.last_alert_time = time.time()

_price_state = _PriceState()
_tick_count  = 0


def _sonnet_explain_price_move(alert_reason: str, mid: float, day_pct: float) -> str | None:
    """
    Ask Sonnet (with web search) to explain a sudden Brent price move.
    Returns a 2-3 sentence explanation, or None on failure.
    Called synchronously from the price watcher thread — keep it fast.
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        prompt = (
            f"Brent crude just moved: {alert_reason}\n"
            f"Current mid: {mid:.2f} points  Day change: {day_pct:+.2f}%\n\n"
            f"Search the web for news published in the LAST 30 MINUTES ONLY that "
            f"directly explains this specific price move. "
            f"Look for: breaking geopolitical events, OPEC announcements, US/Iran, "
            f"Houthi attacks, Red Sea incidents, supply disruptions.\n\n"
            f"STRICT RULES:\n"
            f"- Only report something if you find a news item from the last 30 minutes\n"
            f"- Do NOT summarise background macro context or existing situations\n"
            f"- Do NOT invent or infer a reason if none is found\n"
            f"- If nothing fresh explains it, reply EXACTLY: "
            f"'No fresh catalyst found — likely technical move or thin liquidity.'\n"
            f"- If you find something, one sentence only: what happened and when."
        )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
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
        log.warning(f"Sonnet price explanation failed: {e}")
        return None


def _on_brent_tick(ticker):
    """Called on every Lightstreamer price tick."""
    import math

    # Handle both real Ticker objects and _T namespace from MARKET listener
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

    # Derive mid — prefer live bid/offer, fall back to open + day%
    def valid(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != 0

    if valid(bid) and valid(offer):
        mid = (bid + offer) / 2
    elif valid(day_open) and valid(day_pct):
        mid = day_open * (1 + day_pct / 100)
    elif valid(day_open):
        mid = day_open
    else:
        return  # nothing useful in this tick

    _price_state.add_tick(mid)

    global _tick_count
    _tick_count += 1
    if _tick_count % 10 == 1:  # log every 10th tick
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

    cfg = CONFIG["price_watcher"]
    alert_reason = None
    alert_emoji  = ""

    # Trigger 1 — big move from day open
    if valid(day_pct) and abs(day_pct) >= cfg["trigger_pct_from_open"]:
        direction = "📈 UP" if day_pct > 0 else "📉 DOWN"
        alert_reason = (
            f"Brent {direction} <b>{day_pct:+.2f}%</b> from today's open"
            f" ({day_open:.2f} → {mid:.2f})"
        )
        alert_emoji = "🚨"

    # Trigger 2 — multi-window rolling moves (1min, 5min, 10min)
    # Check shortest window first — fastest signal wins
    for (window_min, threshold_pct, label) in cfg["rolling_windows"]:
        chg = _price_state.change_pct_over_window(window_min)
        if chg is not None and abs(chg) >= threshold_pct:
            direction = "📈 UP" if chg > 0 else "📉 DOWN"
            rolling_msg = (
                f"Brent {direction} <b>{chg:+.2f}%</b> in last {label}"
                f" → {mid:.2f}"
            )
            # Use this trigger if no day trigger, or if it's a stronger signal
            if alert_reason is None:
                alert_reason = rolling_msg
                alert_emoji  = "⚡" if window_min <= 1 else "⚠️" if window_min <= 5 else "📊"
            break  # don't stack multiple window alerts for same move

    if not alert_reason:
        return

    now_str = datetime.now().strftime("%d/%m %H:%M")
    price_line = (
        f"Bid: {bid:.2f}  Ask: {offer:.2f}\n"
        if valid(bid) and valid(offer)
        else f"Mid (derived): {mid:.2f}\n"
    )
    range_line = (
        f"Day range: {day_low:.2f} – {day_high:.2f}\n"
        if valid(day_low) and valid(day_high) else ""
    )

    # Sonnet price explanation disabled — fires too frequently on sustained moves
    explanation_block = ""

    message = (
        f"{alert_emoji} <b>MonsieurMarket — Price Alert</b>\n\n"
        f"{alert_reason}\n\n"
        f"{price_line}{range_line}"
        f"{explanation_block}"
        f"\n⏰ {now_str} Paris"
    )
    send_telegram(message, force=True)
    _price_state.mark_alerted()
    log.info(f"Price alert sent — {alert_reason[:60]}")

class _MarketListener(SubscriptionListener if IG_AVAILABLE else object):
    """Lightstreamer MARKET subscription listener — called on every price update."""

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

            # Build a simple namespace so _on_brent_tick can stay unchanged
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
            t.timestamp = upd_time
            t.market_state = state

            log.debug(f"MARKET tick: bid={bid} offer={offer} chg={change_pct}% state={state} t={upd_time}")
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
    """Background thread: connects Lightstreamer and streams Brent ticks."""
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
            ig_stream.create_session(version="3")  # v3 required for StreamingManager

            sm = StreamingManager(ig_stream)
            sm.start_tick_subscription(epic)
            ticker = sm.ticker(epic, timeout_length=10)
            log.info(f"Price watcher: ✅ got ticker — {ticker}")

            last_ts = None
            while True:
                if ticker.timestamp != last_ts:
                    last_ts = ticker.timestamp
                    _on_brent_tick(ticker)
                else:
                    # Heartbeat every 60s
                    pass
                time.sleep(0.5)

        except Exception as e:
            log.error(f"Price watcher stream error: {e} — reconnecting in 30s")
            time.sleep(30)


# ─────────────────────────────────────────────
# POLYMARKET WEBSOCKET — real-time trade stream
# ─────────────────────────────────────────────
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _polymarket_ws_worker():
    """
    Background thread: connects to Polymarket CLOB websocket and streams
    trade executions in real-time. Fires whale alerts immediately on large trades.
    """
    try:
        import websocket as ws_lib
    except ImportError:
        log.warning("websocket-client not installed — Polymarket RT stream unavailable")
        log.warning("Run: /opt/homebrew/bin/python3 -m pip install websocket-client --break-system-packages")
        return

    # Build token→market label map from external file (loaded fresh on each WS connect)
    def _build_token_map() -> dict:
        token_map = {}
        for m in load_polymarket_markets():
            if m.get("yes_token"):
                token_map[m["yes_token"]] = {"label": m["label"], "outcome": "YES"}
            if m.get("no_token"):
                token_map[m["no_token"]] = {"label": m["label"], "outcome": "NO"}
        return token_map

    threshold = CONFIG["whale_threshold_usd"]

    def _lookup_trader_by_hash(tx_hash: str) -> str:
        """Resolve pseudonym from a specific transaction hash. Fast and exact."""
        if not tx_hash:
            return "unknown"
        try:
            time.sleep(2)  # give indexer a moment to catch up
            r = requests.get(
                f"{POLYMARKET_DATA_API}/trades",
                params={"transaction_hash": tx_hash, "limit": 5},
                timeout=5,
            )
            trades = r.json() or []
            for t in trades:
                ps = t.get("pseudonym") or t.get("proxyWallet", "")
                if ps:
                    log.info(f"[trader] hash lookup resolved: {ps}")
                    return ps
        except Exception as e:
            log.info(f"[trader] hash lookup failed: {e}")
        return "unknown"

    def on_open(ws):
        log.info("Polymarket WS: connected")
        token_map = _build_token_map()
        all_tokens = list(token_map.keys())
        sub = {
            "type": "subscribe",
            "assets_ids": all_tokens,
        }
        ws.send(json.dumps(sub))
        log.info(f"Polymarket WS: subscribed to {len(all_tokens)} tokens")
        # Store token_map on ws for use in on_message
        ws._token_map = token_map

    def on_message(ws, raw):
        try:
            if raw == "pong":
                return

            data = json.loads(raw)
            if not isinstance(data, dict):
                return

            # event_type is nested in the object (not top-level "type")
            msg_type = data.get("event_type") or data.get("type")

            # last_trade_price = trade execution event
            if msg_type != "last_trade_price":
                return

            token_map = getattr(ws, "_token_map", {})

            asset_id  = data.get("asset_id", "")
            price     = float(data.get("price", 0))
            size      = float(data.get("size", 0))
            side      = data.get("side", "BUY").upper()
            tx_hash   = data.get("transaction_hash", "")

            amount    = price * size

            if asset_id not in token_map:
                return

            market_info = token_map[asset_id]
            label   = market_info["label"]
            outcome = market_info["outcome"]

            if amount < threshold:
                return

            log.info(f"Polymarket trade: {label} {outcome} {side} ${amount:.0f} @ {price:.3f} size={size}")

            # Resolve trader pseudonym
            trader = _lookup_trader_by_hash(tx_hash)

            log.info(f"🐋 RT Whale: {trader} {label} {outcome} {side} ${amount:,.0f} @ {price:.3f}")

            # Update whale ledger (thread-safe via state file)
            state = load_state()
            trade_id = f"ws_{asset_id[:8]}_{data.get('timestamp', int(time.time()))}"
            if trade_id not in state.get("seen_trade_ids", {}):
                state.setdefault("seen_trade_ids", {})[trade_id] = time.time()
                whale_signal = [{
                    "type": "whale_trade",
                    "market": label,
                    "trader": trader,
                    "amount": amount,
                    "outcome": outcome,
                    "price": price,
                    "side": side,
                    "trade_id": trade_id,
                }]
                state, repeat_alerts = update_whale_ledger(state, whale_signal)

                # Alert on ALL trades above whale_threshold_usd — no Sonnet, no news, just raw signal
                # Double whale emoji for mega trades (≥ whale_instant_alert_usd)
                now_str = datetime.now().strftime("%d/%m %H:%M")
                direction = "📈 BUY Yes" if (outcome == "YES" and side == "BUY") or \
                                           (outcome == "NO"  and side == "SELL") \
                           else "📉 BUY No"
                size_emoji = "🐋🐋" if amount >= CONFIG["whale_instant_alert_usd"] else "🐋"
                # Include WS timestamp if available (helps correlate with Polymarket UI)
                ts_raw = data.get("timestamp")
                trade_time_str = ""
                if ts_raw:
                    try:
                        trade_time_str = f"\nTrade time: {datetime.fromtimestamp(int(ts_raw)/1000 if int(ts_raw) > 1e10 else int(ts_raw)).strftime('%d/%m %H:%M:%S')}"
                    except Exception:
                        pass
                msg = (
                    f"{size_emoji} <b>Polymarket — Whale</b>\n\n"
                    f"<b>{trader}</b>\n"
                    f"{direction} <b>${amount:,.0f}</b> @ {price:.2f}\n"
                    f"Market: {label}"
                    f"{trade_time_str}\n"
                    f"\n⏰ {now_str} Paris"
                )
                send_telegram(msg, force=False)

                # Repeat whale alerts
                if repeat_alerts:
                    repeat_msg = format_repeat_whale_alert(repeat_alerts, label)
                    if repeat_msg:
                        send_telegram(repeat_msg, force=False)

                save_state(state)

        except Exception as e:
            log.debug(f"Polymarket WS message error: {e}")

    def on_error(ws, error):
        log.warning(f"Polymarket WS error: {error}")

    def on_close(ws, code, msg):
        log.info(f"Polymarket WS closed: {code} {msg}")

    # Ping thread — send ping every 10s
    def ping_loop(ws):
        while True:
            time.sleep(10)
            try:
                ws.send("ping")
            except Exception:
                break

    while True:
        try:
            log.info("Polymarket WS: connecting...")
            ws = ws_lib.WebSocketApp(
                POLYMARKET_WS,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            # Start ping thread
            ping_thread = threading.Thread(target=ping_loop, args=(ws,), daemon=True)
            ping_thread.start()

            ws.run_forever()
            log.warning("Polymarket WS disconnected — reconnecting in 10s")
        except Exception as e:
            log.error(f"Polymarket WS fatal: {e} — reconnecting in 30s")
            time.sleep(30)
        time.sleep(10)


def start_polymarket_ws():
    """Launch Polymarket real-time websocket in a daemon background thread."""
    t = threading.Thread(target=_polymarket_ws_worker, name="PolymarketWS", daemon=True)
    t.start()
    log.info("Polymarket WS thread started")


# ─────────────────────────────────────────────
# TRUMP WATCHER — polls Truth Social RSS every 2 min
# ─────────────────────────────────────────────
TRUMP_RSS = "https://trumpstruth.org/feed"

TRUMP_KEYWORDS = [
    "iran", "saudi", "opec", "oil", "brent", "petroleum",
    "hormuz", "houthi", "yemen", "israel", "middle east",
    "khamenei", "nuclear", "sanctions", "tariff", "energy",
    "russia", "ukraine", "war", "military", "strike", "bomb",
]


def _fetch_trump_posts() -> list[dict]:
    """Fetch latest Trump Truth Social posts. Returns list of {url, title, text}."""
    try:
        import re
        r = requests.get(TRUMP_RSS, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (compatible; MonsieurMarket/1.0)"
        })
        r.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        posts = []
        for item in items[:10]:  # last 10 posts
            title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item, re.DOTALL)
            link_m  = re.search(r"<link>(.*?)</link>", item)
            title = title_m.group(1).strip() if title_m else ""
            url   = link_m.group(1).strip()  if link_m  else ""
            if title and url:
                posts.append({"url": url, "title": title})
        return posts
    except Exception as e:
        log.debug(f"Trump RSS fetch error: {e}")
        return []


def _haiku_trump_filter(post: dict) -> bool:
    """
    Quick Haiku check: is this Trump post relevant to oil/Iran/geopolitics?
    Returns True if relevant.
    """
    # Fast keyword pre-check before spending a Haiku call
    text_lower = post["title"].lower()
    if not any(kw in text_lower for kw in TRUMP_KEYWORDS):
        return False

    # Keyword hit — confirm with Haiku
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content":
                f"Does this Trump post relate to oil prices, Iran, Saudi Arabia, "
                f"OPEC, Middle East conflict, or energy markets?\n\n"
                f'"{post["title"]}"\n\n'
                f"Reply YES or NO only."
            }],
        )
        answer = response.content[0].text.strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        log.debug(f"Haiku Trump filter error: {e}")
        # Fall back to keyword match result
        return True


def _sonnet_analyze_trump(post: dict) -> str | None:
    """
    Sonnet analyzes a relevant Trump post for trading implications.
    Returns a 3-4 sentence alert text.
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        themes_text = ", ".join(
            t["name"] for t in CONFIG["themes"] if t.get("active")
        )
        response = client.messages.create(
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


def _trump_watcher_worker():
    """Background thread: polls Trump RSS every 2 minutes."""
    log.info("Trump watcher started — polling every 2 min")
    POLL_INTERVAL = 120  # seconds

    while True:
        try:
            state = load_state()
            last_seen = state.get("trump_last_seen_url")

            posts = _fetch_trump_posts()
            if not posts:
                time.sleep(POLL_INTERVAL)
                continue

            # Find new posts — everything before the last seen URL
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

            # Update last seen immediately (avoid reprocessing on error)
            state["trump_last_seen_url"] = posts[0]["url"]
            save_state(state)

            # Process each new post newest-first
            for post in new_posts:
                log.info(f"Trump post: {post['title'][:80]}")

                if not _haiku_trump_filter(post):
                    log.info("  → not relevant to oil/Iran — skipping")
                    continue

                log.info("  → RELEVANT — firing Sonnet analysis")
                analysis = _sonnet_analyze_trump(post)

                if analysis:
                    now_str = datetime.now().strftime("%d/%m %H:%M")
                    message = (
                        f"🇺🇸 <b>MonsieurMarket — Trump Alert</b>\n\n"
                        f"<i>{post['title'][:300]}</i>\n\n"
                        f"🧠 <b>Sonnet:</b> {analysis}\n\n"
                        f"⏰ {now_str} Paris"
                    )
                    send_telegram(message, force=True)
                    log.info("  → Trump alert sent")

        except Exception as e:
            log.error(f"Trump watcher error: {e}")

        time.sleep(POLL_INTERVAL)


def start_trump_watcher():
    """Launch the Trump watcher in a daemon background thread."""
    t = threading.Thread(target=_trump_watcher_worker, name="TrumpWatcher", daemon=True)
    t.start()
    log.info("Trump watcher thread started")


def start_price_watcher():
    """Launch the Lightstreamer stream in a daemon background thread."""
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
# IG MARKETS — BRENT PRICE MONITOR (REST snapshot)
# ─────────────────────────────────────────────
_ig_service = None  # module-level singleton


def _get_ig_service():
    """Lazy-init and return a connected IGService, or None if unavailable."""
    global _ig_service

    if not IG_AVAILABLE:
        return None

    username = os.getenv("IG_USERNAME")
    password = os.getenv("IG_PASSWORD")
    api_key = os.getenv("IG_API_KEY")

    if not all([username, password, api_key]):
        return None  # credentials not configured yet — silently skip

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
    """
    Fetch live Brent turbo price from IG.
    Returns a dict with price context, or None if unavailable.

    Returned dict shape:
    {
        "epic": str,
        "bid": float,
        "ask": float,
        "mid": float,
        "net_chg": float,       # change vs open
        "net_chg_pct": float,   # % change vs open
        "high": float,
        "low": float,
        "market_state": str,    # "TRADEABLE" | "CLOSED" | etc.
        "knock_out_level": float | None,
        "barrier_distance_pct": float | None,  # how far mid is from knock-out
        "barrier_warning": bool,               # True if within 5%
    }
    """
    svc = _get_ig_service()
    if svc is None:
        return None

    epic = os.getenv("IG_BRENT_EPIC", "").strip()
    if not epic:
        log.debug("IG_BRENT_EPIC not set — skipping price check")
        return None

    try:
        info = svc.fetch_market_by_epic(epic)

        # trading_ig returns a nested object; field names vary by version
        snap = info.get("snapshot", {}) if isinstance(info, dict) else {}
        inst = info.get("instrument", {}) if isinstance(info, dict) else {}

        bid = float(snap.get("bid") or 0)
        ask = float(snap.get("offer") or snap.get("ask") or 0)
        mid = (bid + ask) / 2 if bid and ask else 0
        net_chg = float(snap.get("netChange") or 0)
        net_chg_pct = float(snap.get("percentageChange") or 0)
        high = float(snap.get("high") or 0)
        low = float(snap.get("low") or 0)
        market_state = snap.get("marketStatus", "UNKNOWN")

        # Knock-out / barrier level — lives in instrument details for turbos
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
        barrier_warning = False
        if knock_out and mid:
            barrier_distance_pct = abs((mid - knock_out) / mid) * 100
            barrier_warning = barrier_distance_pct < 5.0  # within 5% → warn

        result = {
            "epic": epic,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "net_chg": net_chg,
            "net_chg_pct": net_chg_pct,
            "high": high,
            "low": low,
            "market_state": market_state,
            "knock_out_level": knock_out,
            "barrier_distance_pct": barrier_distance_pct,
            "barrier_warning": barrier_warning,
        }
        log.info(
            f"IG Brent: mid={mid:.2f} chg={net_chg_pct:+.2f}% "
            f"state={market_state}"
            + (f" ⚠️ barrier {barrier_distance_pct:.1f}% away" if barrier_warning else "")
        )
        return result

    except Exception as e:
        # 500 on knock-out epics is a known IG demo limitation — log at debug only
        lvl = log.debug if "500" in str(e) else log.warning
        lvl(f"IG REST snapshot failed: {e}")
        global _ig_service
        _ig_service = None
        return None


def format_ig_block(ig: dict) -> str:
    """Format the IG price block for inclusion in a Telegram alert."""
    if not ig:
        return ""

    arrow = "📈" if ig["net_chg_pct"] >= 0 else "📉"
    state_icon = "🟢" if ig["market_state"] == "TRADEABLE" else "🔴"

    lines = [
        "",
        f"🛢 <b>Brent (IG live)</b> {state_icon}",
        f"  Bid {ig['bid']:.2f} / Ask {ig['ask']:.2f}  "
        f"{arrow} {ig['net_chg_pct']:+.2f}% today",
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
# WHALE AGGREGATION
# ─────────────────────────────────────────────
def aggregate_whale_signals(whale_signals: list) -> dict:
    """
    Summarise raw whale trades into actionable intel.

    Returns:
    {
        "total_volume_usd": float,
        "net_yes_usd": float,       # positive = net BUY Yes, negative = net SELL Yes
        "yes_volume": float,
        "no_volume": float,
        "dominant_side": "YES" | "NO" | "MIXED",
        "top_traders": [{"name": str, "total_usd": float, "direction": str}],
        "market_breakdown": {"market_label": {"yes": float, "no": float}},
        "summary_text": str,        # human-readable one-liner for Sonnet
    }
    """
    if not whale_signals:
        return {}

    total = 0.0
    yes_vol = 0.0
    no_vol  = 0.0
    traders: dict = {}
    markets: dict = {}

    for s in whale_signals:
        if s.get("type") != "whale_trade":
            continue
        amount  = s.get("amount", 0)
        outcome = s.get("outcome", "").upper()
        side    = s.get("side", "BUY").upper()
        trader  = s.get("trader", "anon")
        market  = s.get("market", "unknown")

        # Count as Yes if buying Yes OR selling No (both are bullish on Yes)
        is_yes_bullish = (outcome == "YES" and side == "BUY") or \
                         (outcome == "NO"  and side == "SELL")
        is_no_bullish  = (outcome == "NO"  and side == "BUY") or \
                         (outcome == "YES" and side == "SELL")

        total += amount
        if is_yes_bullish:
            yes_vol += amount
        elif is_no_bullish:
            no_vol  += amount

        # Per-trader aggregation
        if trader not in traders:
            traders[trader] = {"yes": 0.0, "no": 0.0}
        if is_yes_bullish:
            traders[trader]["yes"] += amount
        elif is_no_bullish:
            traders[trader]["no"]  += amount

        # Per-market aggregation
        if market not in markets:
            markets[market] = {"yes": 0.0, "no": 0.0}
        if is_yes_bullish:
            markets[market]["yes"] += amount
        elif is_no_bullish:
            markets[market]["no"]  += amount

    net_yes = yes_vol - no_vol
    if abs(net_yes) < total * 0.1:
        dominant = "MIXED"
    elif net_yes > 0:
        dominant = "YES"
    else:
        dominant = "NO"

    # Top 5 traders by total volume
    top_traders = sorted(
        [
            {
                "name": name,
                "total_usd": v["yes"] + v["no"],
                "direction": "YES" if v["yes"] >= v["no"] else "NO",
                "yes_usd": v["yes"],
                "no_usd":  v["no"],
            }
            for name, v in traders.items()
        ],
        key=lambda x: x["total_usd"],
        reverse=True,
    )[:5]

    # One-liner summary
    direction_str = "strongly BUY Yes" if dominant == "YES" and net_yes > total * 0.5 \
        else "leaning BUY Yes" if dominant == "YES" \
        else "strongly BUY No (bearish on event)" if dominant == "NO" and abs(net_yes) > total * 0.5 \
        else "leaning BUY No" if dominant == "NO" \
        else "mixed / neutral"

    top_name = top_traders[0]["name"] if top_traders else "unknown"
    top_amt  = top_traders[0]["total_usd"] if top_traders else 0

    market_lines = ", ".join(
        f"{m}: ${v['yes']:,.0f} Yes / ${v['no']:,.0f} No"
        for m, v in markets.items()
    )

    summary_text = (
        f"${total:,.0f} total whale flow — {direction_str} "
        f"(net ${abs(net_yes):,.0f} {'Yes' if net_yes >= 0 else 'No'}). "
        f"Top trader: {top_name} ${top_amt:,.0f}. "
        f"Markets: {market_lines}"
    )

    log.info(f"🐋 Whale summary: {summary_text}")

    return {
        "total_volume_usd": total,
        "net_yes_usd": net_yes,
        "yes_volume": yes_vol,
        "no_volume":  no_vol,
        "dominant_side": dominant,
        "top_traders": top_traders,
        "market_breakdown": markets,
        "summary_text": summary_text,
    }


# ─────────────────────────────────────────────
# WHALE LEDGER — cross-cycle accumulation tracking
# ─────────────────────────────────────────────
WHALE_LEDGER_WINDOW_H = 24   # hours to keep whale history
WHALE_REPEAT_TRIGGER  = 50000  # cumulative USD within window to flag as repeat whale


def update_whale_ledger(state: dict, whale_signals: list) -> tuple[dict, list]:
    """
    Add new whale trades to the ledger, prune old ones,
    and return (updated_state, list of repeat whale alerts).

    A repeat whale alert fires when a pseudonym crosses WHALE_REPEAT_TRIGGER
    cumulative USD within the last WHALE_LEDGER_WINDOW_H hours —
    but only the FIRST TIME they cross that threshold (not every cycle after).
    """
    now = time.time()
    cutoff = now - WHALE_LEDGER_WINDOW_H * 3600
    ledger = state.get("whale_ledger", {})

    # Prune old entries
    for name in list(ledger.keys()):
        ledger[name] = [e for e in ledger[name] if e["ts"] > cutoff]
        if not ledger[name]:
            del ledger[name]

    repeat_alerts = []

    for s in whale_signals:
        if s.get("type") != "whale_trade":
            continue

        trader  = s.get("trader", "anon")
        proxy   = s.get("proxy_wallet", "")
        amount  = s.get("amount", 0)
        outcome = s.get("outcome", "")
        side    = s.get("side", "BUY")

        # Determine direction (Yes-bullish or No-bullish)
        is_yes = (outcome.upper() == "YES" and side.upper() == "BUY") or \
                 (outcome.upper() == "NO"  and side.upper() == "SELL")
        direction = "YES" if is_yes else "NO"

        if trader not in ledger:
            ledger[trader] = []

        # Check cumulative BEFORE adding this trade
        prev_total = sum(e["amount"] for e in ledger[trader])
        ledger[trader].append({
            "amount": amount,
            "direction": direction,
            "proxy_wallet": proxy,
            "ts": now,
        })
        new_total = prev_total + amount

        # Fire alert every time trader crosses a new $50k band
        # e.g. alerts at $50k, $100k, $150k, $200k...
        prev_band = int(prev_total / WHALE_REPEAT_TRIGGER)
        new_band  = int(new_total  / WHALE_REPEAT_TRIGGER)
        if new_band > prev_band:
            yes_total = sum(e["amount"] for e in ledger[trader] if e["direction"] == "YES")
            no_total  = sum(e["amount"] for e in ledger[trader] if e["direction"] == "NO")
            dominant  = "YES (bullish)" if yes_total >= no_total else "NO (bearish)"
            trades    = len(ledger[trader])
            band_usd  = new_band * WHALE_REPEAT_TRIGGER
            # Get proxy wallet from ledger entries
            proxy = next(
                (e.get("proxy_wallet","") for e in ledger[trader] if e.get("proxy_wallet")),
                ""
            )
            repeat_alerts.append({
                "trader":       trader,
                "proxy_wallet": proxy,
                "total_usd":    new_total,
                "band_usd":     band_usd,
                "dominant":     dominant,
                "yes_usd":      yes_total,
                "no_usd":       no_total,
                "trades":       trades,
            })
            log.info(
                f"🐋 Repeat whale: {trader} crossed ${band_usd:,.0f} band "
                f"(total ${new_total:,.0f} over {trades} trades) — {dominant}"
            )

    state["whale_ledger"] = ledger
    return state, repeat_alerts


def format_repeat_whale_alert(alerts: list, market: str = "") -> str | None:
    """Format a Telegram message for repeat whale accumulation alerts."""
    if not alerts:
        return None

    lines = ["🐋 <b>MonsieurMarket — Repeat Whale Alert</b>\n"]
    for a in alerts:
        band = a.get("band_usd", a["total_usd"])
        lines.append(
            f"<b>{a['trader']}</b> crossed <b>${band:,.0f}</b> threshold\n"
            f"  Total (24h): ${a['total_usd']:,.0f} over {a['trades']} trades\n"
            f"  Direction: {a['dominant']}\n"
            f"  ${a['yes_usd']:,.0f} Yes / ${a['no_usd']:,.0f} No"
        )
    if market:
        lines.append(f"\nMarket: {market}")
    lines.append(f"\n⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# MAIN POLLING LOOP
# ─────────────────────────────────────────────
def _run_portfolio_check(trader: str, proxy_wallet: str):
    """Call check_whale_portfolio.py in a subprocess and send results via Telegram."""
    import subprocess
    script = Path(__file__).parent / "check_whale_portfolio.py"
    if not script.exists():
        log.debug("check_whale_portfolio.py not found — skipping")
        return
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", str(script), proxy_wallet],
            capture_output=True, text=True, timeout=60,
            cwd=str(Path(__file__).parent),
        )
        if result.returncode != 0:
            log.warning(f"Portfolio check failed: {result.stderr[:200]}")
            return
        data = json.loads(result.stdout)
        new_relevant = data.get("new_relevant", [])
        if not new_relevant:
            log.info(f"Portfolio check for {trader}: no new relevant markets")
            return
        lines = [f"🔍 <b>Whale Portfolio — {trader}</b>\n"]
        lines.append(f"New relevant markets found:\n")
        for m in new_relevant:
            lines.append(f"  • <b>{m['title']}</b>")
            lines.append(f"    {m['reason']}  |  whale position: ${m['whale_amount']:,.0f} {m['direction']}")
        lines.append(f"\n⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris")
        send_telegram("\n".join(lines), force=False)
        log.info(f"Portfolio check for {trader}: {len(new_relevant)} new relevant market(s)")
    except Exception as e:
        log.warning(f"Portfolio check error: {e}")


def fetch_rss_headlines(state: dict) -> tuple[list[dict], dict]:
    """
    Fetch recent headlines from free RSS feeds.
    Filters out already-seen URLs.
    """
    sources = [
        {
            "name": "Reuters",
            "url": "https://feeds.reuters.com/reuters/topNews",
        },
        {
            "name": "Al Jazeera",
            "url": "https://www.aljazeera.com/xml/rss/all.xml",
        },
        {
            "name": "OilPrice",
            "url": "https://oilprice.com/rss/main",
        },
        {
            "name": "CNBC",
            "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        },
        {
            "name": "CNN",
            "url": "http://rss.cnn.com/rss/cnn_latest.rss",
        },
    ]

    headlines = []
    for source in sources:
        try:
            r = requests.get(source["url"], timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (compatible; MonsieurMarket/1.0)"
            })
            # Simple XML title/link extraction without extra dependencies
            import re
            items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
            for item in items[:20]:
                title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", item)
                link_m = re.search(r"<link>(.*?)</link>|<guid>(.*?)</guid>", item)
                title = (title_m.group(1) or title_m.group(2) or "").strip() if title_m else ""
                url = (link_m.group(1) or link_m.group(2) or "").strip() if link_m else ""
                if title and url and url not in state["seen_news_urls"]:
                    headlines.append({
                        "title": title,
                        "url": url,
                        "source": source["name"],
                    })
        except Exception as e:
            log.debug(f"RSS fetch error {source['name']}: {e}")

    return headlines, state


def run_poll():
    """Main polling function — runs every N minutes."""
    log.info("── Poll cycle starting ──")
    state = load_state()
    state = clean_state(state)

    # ── Step 0: Fetch live Brent price from IG (always, best-effort) ──
    ig_data = check_ig_brent()

    # Barrier proximity warning — send independently of news signals
    if ig_data and ig_data.get("barrier_warning"):
        barrier_msg = (
            f"⚠️ <b>Brent Barrier Proximity Alert</b>\n"
            f"Knock-out at <b>{ig_data['knock_out_level']:.2f}</b> — "
            f"only {ig_data['barrier_distance_pct']:.1f}% away\n"
            f"Current mid: {ig_data['mid']:.2f}  "
            f"({ig_data['net_chg_pct']:+.2f}% today)\n"
            f"⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris"
        )
        send_telegram(barrier_msg, force=True)  # always wake for barrier risk

    # ── Step 1: Fetch Polymarket whale trades (always) ──
    whale_signals, state = check_polymarket(state)
    if whale_signals:
        log.info(f"Polymarket: {len(whale_signals)} new whale signal(s)")

    # ── Step 1b: Aggregate and check for standalone whale trigger ──
    whale_agg_global = aggregate_whale_signals(
        [s for s in whale_signals if s.get("type") == "whale_trade"]
    )

    cfg_wt = CONFIG["whale_triggers"]
    whale_flow_trigger = False
    whale_trigger_reason = ""

    if whale_signals:
        # Trigger 1: single trade ≥ threshold (poll cycle — WS handles RT)
        max_single = max(
            (s.get("amount", 0) for s in whale_signals if s.get("type") == "whale_trade"),
            default=0
        )
        if max_single >= cfg_wt["single_trade_usd"]:
            whale_flow_trigger = True
            whale_trigger_reason = f"single trade ${max_single:,.0f}"

        # Trigger 2: one trader's cumulative flow ≥ threshold
        if not whale_flow_trigger and whale_agg_global.get("top_traders"):
            top = whale_agg_global["top_traders"][0]
            if top["total_usd"] >= cfg_wt["single_trader_usd"]:
                whale_flow_trigger = True
                whale_trigger_reason = (
                    f"{top['name']} cumulative ${top['total_usd']:,.0f}"
                )

        # Trigger 3: large net directional flow (not just noise)
        if not whale_flow_trigger:
            total = whale_agg_global.get("total_volume_usd", 0)
            net   = abs(whale_agg_global.get("net_yes_usd", 0))
            if (total >= cfg_wt["net_flow_usd"] and
                    total > 0 and net / total >= cfg_wt["net_flow_min_pct"]):
                whale_flow_trigger = True
                direction = whale_agg_global.get("dominant_side", "?")
                whale_trigger_reason = (
                    f"${total:,.0f} total, {net/total*100:.0f}% {direction}"
                )

    if whale_flow_trigger:
        log.info(f"🐋 Whale trigger: {whale_trigger_reason} — Sonnet will fire even without news")

    # ── Step 1c: Update whale ledger and check for repeat whales ──
    state, repeat_whale_alerts = update_whale_ledger(state, whale_signals)
    if repeat_whale_alerts:
        msg = format_repeat_whale_alert(repeat_whale_alerts, market="US forces enter Iran")
        if msg:
            send_telegram(msg, force=False)
            whale_flow_trigger = True
            if not whale_trigger_reason:
                whale_trigger_reason = f"{len(repeat_whale_alerts)} repeat whale(s) crossed ${WHALE_REPEAT_TRIGGER:,} threshold"

        # Run portfolio check for each whale that crossed a threshold
        for alert in repeat_whale_alerts:
            proxy = alert.get("proxy_wallet", "")
            trader = alert.get("trader", "")
            if proxy:
                threading.Thread(
                    target=_run_portfolio_check,
                    args=(trader, proxy),
                    daemon=True,
                ).start()

    # ── Step 2: Fetch news headlines ──
    # Skip if polled recently — UNLESS a price or whale trigger is active
    # (something is moving → we want news context NOW regardless of cooldown)
    poll_interval_sec = CONFIG["poll_interval_minutes"] * 60
    time_since_last   = time.time() - state.get("last_news_poll", 0)
    force_news = whale_flow_trigger or bool(
        _price_state.tick_history and
        abs(_price_state.rolling_change_pct() or 0) >= CONFIG["price_watcher"]["trigger_pct_from_open"]
    )

    if time_since_last < poll_interval_sec and not force_news:
        mins_remaining = int((poll_interval_sec - time_since_last) / 60)
        log.info(f"RSS: skipping — next poll in {mins_remaining} min")
        all_headlines = []
    else:
        if force_news and time_since_last < poll_interval_sec:
            log.info("RSS: forced fetch — active price/whale trigger")
        all_headlines, state = fetch_rss_headlines(state)
        state["last_news_poll"] = time.time()
        log.info(f"RSS: {len(all_headlines)} new headlines")

    # ── Step 3: Per-theme processing ──
    for theme in CONFIG["themes"]:
        if not theme.get("active"):
            continue

        theme_name = theme["name"]
        log.info(f"Processing theme: {theme_name}")

        # Haiku filters headlines for this theme
        relevant_headlines = haiku_filter_news(all_headlines, theme)
        log.info(f"  Haiku filtered: {len(relevant_headlines)} relevant headlines")

        # Skip only if no news AND no whale flow trigger
        if not relevant_headlines and not whale_flow_trigger:
            log.info(f"  No signals for {theme_name} — skipping Sonnet")
            continue
        if not relevant_headlines and whale_flow_trigger:
            log.info(f"  No news but whale flow triggered — running Sonnet on whale data")

        # ── Step 4: Sonnet deep analysis (only when signals exist) ──
        analysis = sonnet_analyze(relevant_headlines, whale_signals, theme)
        if not analysis:
            continue

        score = analysis.get("score", 0)
        log.info(f"  Sonnet score: {score}/10")

        # Below threshold → no alert
        if score < CONFIG["alert_threshold"]:
            log.info(f"  Score {score} below threshold — no alert")
            # Mark headlines as seen anyway to avoid reprocessing
            for h in relevant_headlines:
                state["seen_news_urls"][h["url"]] = time.time()
            continue

        # ── Step 5: Format and send alert ──
        # Build trigger reason for alert header
        trigger_parts = []
        if relevant_headlines:
            trigger_parts.append(f"{len(relevant_headlines)} news signal(s)")
        if whale_trigger_reason:
            trigger_parts.append(whale_trigger_reason)
        trigger_str = " + ".join(trigger_parts) if trigger_parts else "scheduled poll"

        message = format_alert(theme, analysis, relevant_headlines, whale_signals, ig_data,
                               trigger_reason=trigger_str)
        wake = analysis.get("wake_override", False) or theme.get("wake_override", False)
        sent = send_telegram(message, force=wake)

        if sent:
            log.info(f"  Alert sent for {theme_name}")
            # Mark signals as seen
            for h in relevant_headlines:
                state["seen_news_urls"][h["url"]] = time.time()
            # Accumulate for weekly digest
            state["weekly_signals"].append({
                "theme": theme_name,
                "score": score,
                "summary": analysis.get("summary", ""),
                "timestamp": datetime.now().isoformat(),
            })

    save_state(state)
    log.info("── Poll cycle complete ──")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    log.info("MonsieurMarket starting up 🎩")

    # Validate env
    missing = [
        v for v in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
        if not os.getenv(v)
    ]
    if missing:
        log.error(f"Missing environment variables: {', '.join(missing)}")
        log.error("Set them and restart.")
        return

    # Validate markets file exists and is readable
    markets = load_polymarket_markets()
    if not markets:
        log.warning(f"No Polymarket markets loaded from {MARKETS_FILE} — whale tracking will be inactive")
    else:
        log.info(f"Loaded {len(markets)} Polymarket market(s) from {MARKETS_FILE}")

    # Check IG availability
    ig_status = "not configured"
    if IG_AVAILABLE and all(os.getenv(v) for v in ["IG_USERNAME", "IG_PASSWORD", "IG_API_KEY"]):
        ig_svc = _get_ig_service()
        ig_status = "✅ connected (demo)" if ig_svc else "❌ login failed"
    elif not IG_AVAILABLE:
        ig_status = "⚠️ trading-ig not installed"

    # Send startup message
    send_telegram(
        "🎩 <b>MonsieurMarket is online</b>\n"
        f"News poll: every {CONFIG['poll_interval_minutes']} min\n"
        f"Trump watcher: every 2 min 🇺🇸\n"
        f"Active themes: {', '.join(t['name'] for t in CONFIG['themes'] if t.get('active'))}\n"
        f"Polymarket markets: {len(markets)} loaded from {MARKETS_FILE}\n"
        f"Whale threshold: ${CONFIG['whale_threshold_usd']:,}\n"
        f"IG Markets: {ig_status}",
        force=True,
    )

    # Start real-time price stream in background thread
    start_price_watcher()

    # Start Polymarket real-time websocket
    start_polymarket_ws()

    # Start Trump Truth Social watcher
    start_trump_watcher()

    global event_watcher
    event_watcher = ScheduledEventWatcher(
        send_telegram_fn=send_telegram,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "")
    )
    log.info("Event watcher initialised ✅")

    # RSS poll loop disabled — too expensive (Haiku filter + Sonnet/web-search every 60min)
    # Re-enable once we have a token budget for it.
    # run_poll()
    # schedule.every(CONFIG["poll_interval_minutes"]).minutes.do(run_poll)

    # Weekly digest also disabled (depends on poll signals)
    # if CONFIG["weekly_digest"]["enabled"]:
    #     digest_day = CONFIG["weekly_digest"]["day"]
    #     digest_time = CONFIG["weekly_digest"]["time"]
    #     getattr(schedule.every(), digest_day).at(digest_time).do(
    #         lambda: send_weekly_digest(load_state())
    #     )
    #     log.info(f"Weekly digest scheduled: {digest_day} at {digest_time}")

    log.info(f"Polling every {CONFIG['poll_interval_minutes']} minutes. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
