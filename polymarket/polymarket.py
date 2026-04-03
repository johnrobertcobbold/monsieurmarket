# polymarket/polymarket.py
"""
Polymarket — whale trade monitoring, ledger, and websocket stream.
"""

import json
import time
import logging
import threading
import requests
from pathlib import Path
from datetime import datetime

from config import CONFIG

log = logging.getLogger("MonsieurMarket")

# ─────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────
MARKETS_FILE    = Path(CONFIG["polymarket_markets_file"])
RELEVANCE_FILE  = Path(__file__).parent / "market_relevance.json"

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
POLYMARKET_DATA_API  = "https://data-api.polymarket.com"
POLYMARKET_WS        = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WHALE_LEDGER_WINDOW_H = 24
WHALE_REPEAT_TRIGGER  = 50000


# ─────────────────────────────────────────────
# MARKETS
# ─────────────────────────────────────────────
def load_polymarket_markets() -> list:
    """
    Load Polymarket markets from external JSON file.
    Hot-reloadable — edit polymarket_markets.json without restarting.
    """
    try:
        markets = json.loads(MARKETS_FILE.read_text())
        markets = [m for m in markets if m.get("active", True)]
        log.debug(f"Loaded {len(markets)} Polymarket market(s)")
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
# TRADES
# ─────────────────────────────────────────────
def fetch_polymarket_trades(condition_id: str) -> list:
    """Fetch recent whale trades for a market via data-api."""
    try:
        r = requests.get(
            f"{POLYMARKET_DATA_API}/trades",
            params={
                "market":      condition_id,
                "limit":       50,
                "takerOnly":   "true",
                "filterType":  "CASH",
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
    signals   = []
    threshold = CONFIG["whale_threshold_usd"]
    markets   = load_polymarket_markets()

    for market_config in markets:
        condition_id = market_config["conditionId"]
        label        = market_config["label"]
        trades       = fetch_polymarket_trades(condition_id)

        if not trades:
            log.debug(f"Polymarket: no trades for {label}")

        for trade in trades:
            try:
                trade_id  = trade.get("transactionHash", "")
                size      = float(trade.get("size", 0))
                price     = float(trade.get("price", 0))
                outcome   = trade.get("outcome", "")
                side      = trade.get("side", "BUY")
                pseudonym = trade.get("pseudonym") or ""
                proxy     = trade.get("proxyWallet", "")
                trader    = pseudonym or proxy or "anon"
                amount    = size * price

                if trade_id in state["seen_trade_ids"]:
                    continue

                state["seen_trade_ids"][trade_id] = time.time()

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
                    "type":         "whale_trade",
                    "market":       label,
                    "trader":       str(trader)[:30],
                    "proxy_wallet": proxy,
                    "amount":       amount,
                    "outcome":      outcome,
                    "price":        price,
                    "side":         side,
                    "trade_id":     trade_id,
                    "ts":           trade_ts,
                })
                log.info(
                    f"🐋 Polymarket whale: {trader} "
                    f"${amount:,.0f} {side} {outcome} "
                    f"@ {price:.2f} on {label}"
                )

            except Exception as e:
                log.debug(f"Trade parse error: {e}")
                continue

        # price movement check
        try:
            if trades:
                latest_price = float(trades[0].get("price", 0))
                last_price   = state["last_prices"].get(condition_id)
                if last_price and abs(latest_price - last_price) >= 0.05:
                    signals.append({
                        "type":       "price_move",
                        "market":     label,
                        "from_price": last_price,
                        "to_price":   latest_price,
                        "move_pct":   (latest_price - last_price) / last_price * 100,
                    })
                state["last_prices"][condition_id] = latest_price
        except Exception:
            pass

    return signals, state


# ─────────────────────────────────────────────
# WHALE AGGREGATION
# ─────────────────────────────────────────────
def aggregate_whale_signals(whale_signals: list) -> dict:
    """Summarise raw whale trades into actionable intel."""
    if not whale_signals:
        return {}

    total   = 0.0
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

        is_yes_bullish = (outcome == "YES" and side == "BUY") or \
                         (outcome == "NO"  and side == "SELL")
        is_no_bullish  = (outcome == "NO"  and side == "BUY") or \
                         (outcome == "YES" and side == "SELL")

        total += amount
        if is_yes_bullish:
            yes_vol += amount
        elif is_no_bullish:
            no_vol  += amount

        if trader not in traders:
            traders[trader] = {"yes": 0.0, "no": 0.0}
        if is_yes_bullish:
            traders[trader]["yes"] += amount
        elif is_no_bullish:
            traders[trader]["no"]  += amount

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

    top_traders = sorted(
        [
            {
                "name":      name,
                "total_usd": v["yes"] + v["no"],
                "direction": "YES" if v["yes"] >= v["no"] else "NO",
                "yes_usd":   v["yes"],
                "no_usd":    v["no"],
            }
            for name, v in traders.items()
        ],
        key=lambda x: x["total_usd"],
        reverse=True,
    )[:5]

    direction_str = (
        "strongly BUY Yes"             if dominant == "YES" and net_yes > total * 0.5 else
        "leaning BUY Yes"              if dominant == "YES" else
        "strongly BUY No (bearish)"    if dominant == "NO"  and abs(net_yes) > total * 0.5 else
        "leaning BUY No"               if dominant == "NO"  else
        "mixed / neutral"
    )

    top_name = top_traders[0]["name"]      if top_traders else "unknown"
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
        "net_yes_usd":      net_yes,
        "yes_volume":       yes_vol,
        "no_volume":        no_vol,
        "dominant_side":    dominant,
        "top_traders":      top_traders,
        "market_breakdown": markets,
        "summary_text":     summary_text,
    }


# ─────────────────────────────────────────────
# WHALE LEDGER
# ─────────────────────────────────────────────
def update_whale_ledger(state: dict, whale_signals: list) -> tuple[dict, list]:
    """
    Add trades to ledger, prune old ones, return repeat whale alerts.
    Fires alert every time a trader crosses a new $50k band.
    """
    now    = time.time()
    cutoff = now - WHALE_LEDGER_WINDOW_H * 3600
    ledger = state.get("whale_ledger", {})

    # prune old entries
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

        is_yes    = (outcome.upper() == "YES" and side.upper() == "BUY") or \
                    (outcome.upper() == "NO"  and side.upper() == "SELL")
        direction = "YES" if is_yes else "NO"

        if trader not in ledger:
            ledger[trader] = []

        prev_total = sum(e["amount"] for e in ledger[trader])
        ledger[trader].append({
            "amount":       amount,
            "direction":    direction,
            "proxy_wallet": proxy,
            "ts":           now,
        })
        new_total = prev_total + amount

        prev_band = int(prev_total / WHALE_REPEAT_TRIGGER)
        new_band  = int(new_total  / WHALE_REPEAT_TRIGGER)

        if new_band > prev_band:
            yes_total = sum(e["amount"] for e in ledger[trader] if e["direction"] == "YES")
            no_total  = sum(e["amount"] for e in ledger[trader] if e["direction"] == "NO")
            dominant  = "YES (bullish)" if yes_total >= no_total else "NO (bearish)"
            trades    = len(ledger[trader])
            band_usd  = new_band * WHALE_REPEAT_TRIGGER
            proxy     = next(
                (e.get("proxy_wallet", "") for e in ledger[trader] if e.get("proxy_wallet")),
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
                f"🐋 Repeat whale: {trader} crossed ${band_usd:,.0f} "
                f"(total ${new_total:,.0f} over {trades} trades) — {dominant}"
            )

    state["whale_ledger"] = ledger
    return state, repeat_alerts


def format_repeat_whale_alert(alerts: list, market: str = "") -> str | None:
    """Format Telegram message for repeat whale accumulation alerts."""
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
# PORTFOLIO CHECK
# ─────────────────────────────────────────────
def run_portfolio_check(trader: str, proxy_wallet: str, send_telegram_fn) -> None:
    """
    Run check_whale_portfolio.py in subprocess and send results via Telegram.
    Called in a daemon thread when a whale crosses a threshold.
    """
    import subprocess
    script = Path(__file__).parent / "check_whale_portfolio.py"
    if not script.exists():
        log.debug("check_whale_portfolio.py not found — skipping")
        return
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/python3", str(script), proxy_wallet],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).parent),
        )
        if result.returncode != 0:
            log.warning(f"Portfolio check failed: {result.stderr[:200]}")
            return
        data         = json.loads(result.stdout)
        new_relevant = data.get("new_relevant", [])
        if not new_relevant:
            log.info(f"Portfolio check for {trader}: no new relevant markets")
            return
        lines = [f"🔍 <b>Whale Portfolio — {trader}</b>\n",
                 "New relevant markets found:\n"]
        for m in new_relevant:
            lines.append(f"  • <b>{m['title']}</b>")
            lines.append(
                f"    {m['reason']}  |  "
                f"whale position: ${m['whale_amount']:,.0f} {m['direction']}"
            )
        lines.append(f"\n⏰ {datetime.now().strftime('%d/%m %H:%M')} Paris")
        send_telegram_fn("\n".join(lines), force=False)
        log.info(f"Portfolio check for {trader}: {len(new_relevant)} new relevant market(s)")
    except Exception as e:
        log.warning(f"Portfolio check error: {e}")

def _lookup_trader(tx_hash: str) -> str:
    if not tx_hash:
        return "unknown"
    try:
        time.sleep(2)
        r = requests.get(
            f"{POLYMARKET_DATA_API}/trades",
            params={"transaction_hash": tx_hash, "limit": 5},
            timeout=5,
        )
        trades = r.json() or []
        log.info(f"[trader] lookup for {tx_hash[:16]} — got {len(trades)} trades: {trades}")
        for t in trades:
            ps = t.get("pseudonym") or t.get("proxyWallet", "")
            if ps:
                return ps
    except Exception as e:
        log.debug(f"Trader lookup failed: {e}")
    return "unknown"

# ─────────────────────────────────────────────
# WEBSOCKET — real-time trade stream
# ─────────────────────────────────────────────
def _polymarket_ws_worker(send_telegram_fn, load_state_fn, save_state_fn):
    """
    Background thread: connects to Polymarket CLOB websocket and streams
    trade executions in real-time.
    """
    try:
        import websocket as ws_lib
    except ImportError:
        log.warning("websocket-client not installed — Polymarket RT stream unavailable")
        return

    threshold = CONFIG["whale_threshold_usd"]

    def _build_token_map() -> dict:
        token_map = {}
        for m in load_polymarket_markets():
            if m.get("yes_token"):
                token_map[m["yes_token"]] = {"label": m["label"], "outcome": "YES"}
            if m.get("no_token"):
                token_map[m["no_token"]] = {"label": m["label"], "outcome": "NO"}
        return token_map

    def on_open(ws):
        log.info("Polymarket WS: connected")
        token_map  = _build_token_map()
        all_tokens = list(token_map.keys())
        ws.send(json.dumps({"type": "subscribe", "assets_ids": all_tokens}))
        log.info(f"Polymarket WS: subscribed to {len(all_tokens)} tokens")
        ws._token_map = token_map

    def on_message(ws, raw):
        try:
            if raw == "pong":
                return
            data = json.loads(raw)
            if not isinstance(data, dict):
                return

            msg_type = data.get("event_type") or data.get("type")
            if msg_type != "last_trade_price":
                return

            token_map = getattr(ws, "_token_map", {})
            asset_id  = data.get("asset_id", "")
            price     = float(data.get("price", 0))
            size      = float(data.get("size", 0))
            side      = data.get("side", "BUY").upper()
            tx_hash   = data.get("transaction_hash", "")
            amount    = price * size

            if asset_id not in token_map or amount < threshold:
                return

            market_info = token_map[asset_id]
            label   = market_info["label"]
            outcome = market_info["outcome"]

            log.info(f"Polymarket trade: {label} {outcome} {side} ${amount:.0f} @ {price:.3f}")
            trader = _lookup_trader(tx_hash)
            log.info(f"🐋 RT Whale: {trader} {label} {outcome} {side} ${amount:,.0f}")

            state    = load_state_fn()
            trade_id = f"ws_{asset_id[:8]}_{data.get('timestamp', int(time.time()))}"

            if trade_id not in state.get("seen_trade_ids", {}):
                state.setdefault("seen_trade_ids", {})[trade_id] = time.time()
                whale_signal = [{
                    "type":     "whale_trade",
                    "market":   label,
                    "trader":   trader,
                    "amount":   amount,
                    "outcome":  outcome,
                    "price":    price,
                    "side":     side,
                    "trade_id": trade_id,
                }]
                state, repeat_alerts = update_whale_ledger(state, whale_signal)

                now_str   = datetime.now().strftime("%d/%m %H:%M")
                direction = (
                    "📈 BUY Yes"
                    if (outcome == "YES" and side == "BUY") or
                       (outcome == "NO"  and side == "SELL")
                    else "📉 BUY No"
                )
                size_emoji = (
                    "🐋🐋" if amount >= CONFIG["whale_instant_alert_usd"] else "🐋"
                )

                ts_raw = data.get("timestamp")
                trade_time_str = ""
                if ts_raw:
                    try:
                        ts = int(ts_raw)
                        ts = ts / 1000 if ts > 1e10 else ts
                        trade_time_str = (
                            f"\nTrade time: "
                            f"{datetime.fromtimestamp(ts).strftime('%d/%m %H:%M:%S')}"
                        )
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
                send_telegram_fn(msg, force=False)

                if repeat_alerts:
                    repeat_msg = format_repeat_whale_alert(repeat_alerts, label)
                    if repeat_msg:
                        send_telegram_fn(repeat_msg, force=False)

                # portfolio check in background
                for alert in repeat_alerts:
                    if alert.get("proxy_wallet"):
                        threading.Thread(
                            target=run_portfolio_check,
                            args=(alert["trader"], alert["proxy_wallet"], send_telegram_fn),
                            daemon=True,
                        ).start()

                save_state_fn(state)

        except Exception as e:
            log.debug(f"Polymarket WS message error: {e}")

    def on_error(ws, error):
        log.warning(f"Polymarket WS error: {error}")

    def on_close(ws, code, msg):
        log.info(f"Polymarket WS closed: {code} {msg}")

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
            threading.Thread(target=ping_loop, args=(ws,), daemon=True).start()
            ws.run_forever()
            log.warning("Polymarket WS disconnected — reconnecting in 10s")
        except Exception as e:
            log.error(f"Polymarket WS fatal: {e} — reconnecting in 30s")
            time.sleep(30)
        time.sleep(10)


def start_polymarket_ws(send_telegram_fn, load_state_fn, save_state_fn):
    """Launch Polymarket websocket in a daemon background thread."""
    t = threading.Thread(
        target=_polymarket_ws_worker,
        args=(send_telegram_fn, load_state_fn, save_state_fn),
        name="PolymarketWS",
        daemon=True,
    )
    t.start()
    log.info("Polymarket WS thread started")