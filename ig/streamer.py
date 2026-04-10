"""
ig/streamer.py — IG Markets Lightstreamer price feed for Brent.

Connects to IG streaming API, fires registered callbacks on every tick.
MM registers callbacks and owns all reaction logic.
"""

import os
import time
import math
import logging

log = logging.getLogger('MonsieurMarket')

try:
    from trading_ig import IGService, IGStreamService
    from trading_ig.streamer.manager import StreamingManager
    from lightstreamer.client import SubscriptionListener, ItemUpdate
    IG_AVAILABLE = True
except ImportError:
    IG_AVAILABLE = False

from ig.service import get_ig_service, IG_AVAILABLE

# ─────────────────────────────────────────────
# TICK CALLBACKS
# ─────────────────────────────────────────────
_tick_callbacks = []

def register_tick_callback(fn):
    """Register a function to be called on every Brent price tick."""
    _tick_callbacks.append(fn)


# ─────────────────────────────────────────────
# PRICE STATE — rolling tick history
# ─────────────────────────────────────────────
_tick_count  = 0


# ─────────────────────────────────────────────
# TICK PARSER — fires callbacks
# ─────────────────────────────────────────────
def _on_brent_tick(ticker):
    """Parse raw ticker and fire all registered callbacks."""
    def _get(attr):
        v = getattr(ticker, attr, None)
        if isinstance(v, float) and math.isnan(v):
            return None
        return v

    def valid(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != 0

    bid      = _get("bid")
    offer    = _get("offer")
    day_pct  = _get("day_percent_change_mid")
    day_open = _get("day_open_mid")
    day_high = _get("day_high")
    day_low  = _get("day_low")

    if valid(bid) and valid(offer):
        mid = (bid + offer) / 2
    elif valid(day_open) and valid(day_pct):
        mid = day_open * (1 + day_pct / 100)
    elif valid(day_open):
        mid = day_open
    else:
        return

    global _tick_count
    _tick_count += 1
    if _tick_count % 10 == 1:
        log.info(
            f"Brent  mid={mid:.2f}"
            + (f"  bid={bid:.2f} ask={offer:.2f}" if valid(bid) and valid(offer) else "  (derived)")
            + (f"  day={day_pct:+.2f}%" if valid(day_pct) else "")
            + (f"  [{day_low:.2f}–{day_high:.2f}]" if valid(day_low) and valid(day_high) else "")
        )

    # fire all registered callbacks
    for fn in _tick_callbacks:
        try:
            fn(
                mid=mid,
                bid=bid if valid(bid) else None,
                ask=offer if valid(offer) else None,
                day_pct=day_pct if valid(day_pct) else None,
                day_open=day_open if valid(day_open) else None,
                day_high=day_high if valid(day_high) else None,
                day_low=day_low if valid(day_low) else None,
            )
        except Exception as e:
            log.warning(f"Tick callback error: {e}")


# ─────────────────────────────────────────────
# MARKET LISTENER — Lightstreamer
# ─────────────────────────────────────────────
class _MarketListener(SubscriptionListener if IG_AVAILABLE else object):
    def onItemUpdate(self, update: "ItemUpdate"):
        try:
            def to_f(v):
                try:
                    return float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    return None

            class _T:
                pass
            t = _T()
            t.bid                    = to_f(update.getValue("BID"))
            t.offer                  = to_f(update.getValue("OFFER"))
            t.day_percent_change_mid = to_f(update.getValue("CHANGE_PCT"))
            t.day_net_change_mid     = to_f(update.getValue("CHANGE"))
            t.day_high               = to_f(update.getValue("HIGH"))
            t.day_low                = to_f(update.getValue("LOW"))
            t.day_open_mid           = (
                t.bid / (1 + t.day_percent_change_mid / 100)
                if t.bid and t.day_percent_change_mid
                else None
            )
            t.timestamp    = update.getValue("UPDATE_TIME")
            t.market_state = update.getValue("MARKET_STATE")

            _on_brent_tick(t)

        except Exception as e:
            log.debug(f"Market listener parse error: {e}")

    def onSubscription(self):
        log.info("Brent MARKET subscription active ✅")

    def onSubscriptionError(self, code, message):
        log.warning(f"Brent MARKET subscription error: {code} — {message}")

    def onUnsubscription(self):
        log.info("Brent MARKET subscription stopped")


# ─────────────────────────────────────────────
# STREAM WORKER
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# PUBLIC START
# ─────────────────────────────────────────────
def start_price_watcher(config: dict):
    if not config.get("price_watcher", {}).get("enabled"):
        log.info("Price watcher disabled in config")
        return
    if not IG_AVAILABLE:
        log.warning("Price watcher: trading-ig not installed — skipping")
        return

    import threading
    t = threading.Thread(target=_stream_worker, name="PriceWatcher", daemon=True)
    t.start()
    log.info("Price watcher thread started")