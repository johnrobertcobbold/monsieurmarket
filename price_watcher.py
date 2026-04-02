#!/usr/bin/env python3
"""
price_watcher.py — Real-time Brent price stream via IG Lightstreamer.

Subscribes to tick-by-tick price updates for the Brent knock-out epic.
Triggers a Telegram alert when:
  1. Price moves > TRIGGER_PCT_FROM_OPEN % from day open (absolute move)
  2. Price moves > TRIGGER_PCT_ROLLING % within a rolling ROLLING_WINDOW_MIN window

Run alongside monsieur_market.py, or standalone for testing.

Usage:
    /opt/homebrew/bin/python3 price_watcher.py
"""

import os
import time
import logging
import requests
import threading
from collections import deque
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from trading_ig import IGService, IGStreamService
from trading_ig.streamer.manager import StreamingManager

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
EPIC = os.getenv("IG_BRENT_EPIC", "CC.D.LCO.OPTCALL.IP")

# Alert thresholds
TRIGGER_PCT_FROM_OPEN = 1.5    # alert if price moves >1.5% from day open
TRIGGER_PCT_ROLLING   = 1.0    # alert if price moves >1.0% within rolling window
ROLLING_WINDOW_MIN    = 10     # rolling window in minutes
COOLDOWN_MIN          = 15     # minimum minutes between price alerts (avoid spam)

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("price_watcher.log"),
    ],
)
log = logging.getLogger("PriceWatcher")

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials not set")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        r.raise_for_status()
        log.info("Telegram alert sent")
    except Exception as e:
        log.error(f"Telegram failed: {e}")

# ─────────────────────────────────────────────
# PRICE STATE
# ─────────────────────────────────────────────
class PriceState:
    def __init__(self):
        self.last_alert_time = 0       # epoch seconds of last alert sent
        # Rolling window: deque of (timestamp_epoch, mid_price)
        self.tick_history = deque()

    def add_tick(self, mid: float):
        now = time.time()
        self.tick_history.append((now, mid))
        # Prune ticks older than rolling window
        cutoff = now - ROLLING_WINDOW_MIN * 60
        while self.tick_history and self.tick_history[0][0] < cutoff:
            self.tick_history.popleft()

    def rolling_change_pct(self) -> float | None:
        """% change from oldest tick in rolling window to latest."""
        if len(self.tick_history) < 2:
            return None
        oldest_price = self.tick_history[0][1]
        latest_price = self.tick_history[-1][1]
        if oldest_price == 0:
            return None
        return (latest_price - oldest_price) / oldest_price * 100

    def can_alert(self) -> bool:
        return (time.time() - self.last_alert_time) > COOLDOWN_MIN * 60

    def mark_alerted(self):
        self.last_alert_time = time.time()

state = PriceState()

# ─────────────────────────────────────────────
# TICK HANDLER
# ─────────────────────────────────────────────
def on_tick(ticker):
    """Called on every price tick from Lightstreamer."""
    bid = ticker.bid
    offer = ticker.offer
    day_open = ticker.day_open_mid
    day_pct   = ticker.day_percent_change_mid
    day_high  = ticker.day_high
    day_low   = ticker.day_low

    # Skip incomplete ticks
    if not bid or not offer:
        return

    mid = (bid + offer) / 2
    state.add_tick(mid)

    # ── Log every tick (concise) ──
    log.info(
        f"Brent  bid={bid:.2f}  ask={offer:.2f}  mid={mid:.2f}"
        + (f"  day={day_pct:+.2f}%" if day_pct else "")
        + (f"  [{day_low:.2f}–{day_high:.2f}]" if day_low and day_high else "")
    )

    if not state.can_alert():
        return

    alert_reason = None
    alert_emoji  = ""

    # ── Trigger 1: Big move from day open ──
    if day_pct is not None and abs(day_pct) >= TRIGGER_PCT_FROM_OPEN:
        direction = "📈 UP" if day_pct > 0 else "📉 DOWN"
        alert_reason = (
            f"Brent {direction} <b>{day_pct:+.2f}%</b> from today's open"
            f" ({day_open:.2f} → {mid:.2f})"
        )
        alert_emoji = "🚨"

    # ── Trigger 2: Fast move within rolling window ──
    rolling_pct = state.rolling_change_pct()
    if rolling_pct is not None and abs(rolling_pct) >= TRIGGER_PCT_ROLLING:
        direction = "📈 UP" if rolling_pct > 0 else "📉 DOWN"
        msg = (
            f"Brent {direction} <b>{rolling_pct:+.2f}%</b>"
            f" in last {ROLLING_WINDOW_MIN} min"
            f" ({state.tick_history[0][1]:.2f} → {mid:.2f})"
        )
        # Use rolling trigger if stronger signal or no day-open trigger yet
        if alert_reason is None or abs(rolling_pct) > abs(day_pct or 0):
            alert_reason = msg
            alert_emoji = "⚡"

    if not alert_reason:
        return

    # ── Format and send ──
    now_str = datetime.now().strftime("%d/%m %H:%M")
    message = (
        f"{alert_emoji} <b>MonsieurMarket — Price Alert</b>\n\n"
        f"{alert_reason}\n\n"
        f"Bid: {bid:.2f}  Ask: {offer:.2f}\n"
        + (f"Day range: {day_low:.2f} – {day_high:.2f}\n" if day_low and day_high else "")
        + f"\n⏰ {now_str} Paris"
    )
    send_telegram(message)
    state.mark_alerted()

# ─────────────────────────────────────────────
# STREAMING LOOP WITH RECONNECT
# ─────────────────────────────────────────────
def connect_and_stream():
    username = os.getenv("IG_USERNAME")
    password = os.getenv("IG_PASSWORD")
    api_key  = os.getenv("IG_API_KEY")
    acc_num  = os.getenv("IG_ACC_NUMBER")

    if not all([username, password, api_key]):
        log.error("IG credentials missing in .env")
        return

    log.info(f"Connecting to IG (demo)... epic={EPIC}")

    ig_svc = IGService(username, password, api_key, acc_type="DEMO")
    ig_stream = IGStreamService(ig_svc)
    ig_stream.acc_number = acc_num
    ig_stream.create_session(version="2")

    manager = StreamingManager(ig_stream)
    manager.start_tick_subscription(EPIC)

    log.info(f"✅ Streaming Brent ticks from {EPIC}")
    send_telegram(
        f"🛢 <b>PriceWatcher online</b>\n"
        f"Streaming: <code>{EPIC}</code>\n"
        f"Triggers: >{TRIGGER_PCT_FROM_OPEN}% from open | "
        f">{TRIGGER_PCT_ROLLING}% in {ROLLING_WINDOW_MIN}min\n"
        f"Cooldown: {COOLDOWN_MIN}min between alerts"
    )

    # Poll the manager's ticker and call our handler
    last_ts = None
    while True:
        try:
            tickers = manager.tickers
            if EPIC in tickers:
                ticker = tickers[EPIC]
                # Only process if timestamp changed (new tick)
                if ticker.timestamp != last_ts:
                    last_ts = ticker.timestamp
                    on_tick(ticker)
            time.sleep(0.5)  # check for new ticks every 500ms
        except KeyboardInterrupt:
            log.info("Shutting down...")
            manager.stop_subscriptions()
            break
        except Exception as e:
            log.error(f"Tick loop error: {e}")
            time.sleep(5)

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    log.info("PriceWatcher starting 🛢")

    missing = [v for v in [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "IG_USERNAME", "IG_PASSWORD", "IG_API_KEY"
    ] if not os.getenv(v)]

    if missing:
        log.error(f"Missing env vars: {', '.join(missing)}")
        return

    # Reconnect loop — if stream drops, wait 30s and retry
    while True:
        try:
            connect_and_stream()
        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Stream connection failed: {e} — retrying in 30s")
            time.sleep(30)

if __name__ == "__main__":
    main()
