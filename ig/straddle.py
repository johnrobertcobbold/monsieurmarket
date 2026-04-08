"""
ig/straddle.py — IG knockout straddle execution

Flow:
    1. GET /nwtpdeal/v3/markets/details → quoteId + barrier levels
    2. find_closest_level() → select barrier at target distance
    3. POST /nwtpdeal/v3/orders/positions/otc → place order
    4. Return position details for stop management
"""

import os
import time
import json
import logging
import requests
from config import CONFIG

log = logging.getLogger('MonsieurMarket')

CALL_EPIC = CONFIG['ig']['brent_call_epic']
PUT_EPIC  = CONFIG['ig']['brent_put_epic']
EXPIRY    = CONFIG['ig']['expiry']


def _get_headers(svc) -> dict:
    """Build request headers from active IG session."""
    raw = svc.crud_session.session.headers
    acc = os.getenv("IG_ACC_NUMBER")
    return {
        'X-IG-API-KEY':        raw.get('X-IG-API-KEY'),
        'CST':                 raw.get('CST'),
        'X-SECURITY-TOKEN':    raw.get('X-SECURITY-TOKEN'),
        'IG-ACCOUNT-ID':       acc,
        'Content-Type':        'application/json',
        'Accept':              'application/json; charset=UTF-8',
        'x-device-user-agent': CONFIG['ig']['device_user_agent'],
    }


def _get_base_urls(is_live: bool) -> tuple[str, str]:
    env = 'live' if is_live else 'demo'
    return (
        CONFIG['ig'][env]['deal_base'],
        CONFIG['ig'][env]['api_base'],
    )


def _get_market_details(epic: str, api_base: str, headers: dict) -> dict:
    """Fetch market details including quoteId and available barrier levels."""
    r = requests.get(
        f'{api_base}/nwtpdeal/v3/markets/details',
        headers={**headers, 'Version': '1'},
        params={'epic': epic},
        timeout=10,
    )
    if r.status_code == 200:
        return r.json()
    log.error(f"market details failed: {r.status_code} — {r.text[:200]}")
    return {}


def _find_closest_level(levels: list, target: float) -> int:
    """Find available barrier level closest to target price."""
    if not levels:
        return int(target)
    return min(levels, key=lambda x: abs(x - target))


def _verify_order(ref: str, deal_base: str, headers: dict, max_wait: int = 5) -> bool:
    """Check barrier account positions via internal WTP endpoint."""
    for i in range(max_wait):
        time.sleep(1)
        r = requests.get(
            f'{deal_base}/nwtpdeal/wtp/orders/positions',
            headers={**headers, 'Version': '1'},
            timeout=10,
        )
        if r.status_code == 200 and ref in r.text:
            log.info(f"Order {ref} confirmed ✅")
            return True
    log.warning(f"Order {ref} not found after {max_wait}s")
    return False


def _get_leg_cost(epic: str, barrier: float, deal_base: str, headers: dict, api_base: str) -> float | None:
    """Get real EUR cost per unit via costs and charges endpoint."""
    acc     = headers.get('IG-ACCOUNT-ID')
    details = _get_market_details(epic, api_base, headers)

    snapshot      = details.get('marketSnapshotData', {})
    instrument    = details.get('instrumentData', {})
    ask           = float(snapshot.get('ask', 0))
    bid           = float(snapshot.get('bid', 0))
    quote_id      = snapshot.get('askQuoteId', '')
    instrument_id = instrument.get('igInstrumentId', '')

    knockout_premium = snapshot.get('knockoutPremium')
    if not knockout_premium:
        log.error(f"knockoutPremium not found for {epic}")
        return None

    # call: barrier below price → premium = price - barrier
    # put:  barrier above price → premium = barrier - price
    if epic == CALL_EPIC:
        premium_ask = ask - barrier
        premium_bid = bid - barrier
    else:
        premium_ask = barrier - ask
        premium_bid = barrier - bid

    payload = {
        "accountId":        acc,
        "epic":             epic,
        "direction":        "BUY",
        "size":             1,
        "ask":              premium_ask,
        "bid":              premium_bid,
        "dealReference":    quote_id,
        "dealCurrencyCode": "USD",
        "knockoutPremium":  knockout_premium,
        "guaranteedStop":   False,
        "stop":             None,
        "limit":            None,
        "editType":         None,
        "priceLevel":       premium_ask,
        "instrumentId":     instrument_id,
    }

    r = requests.post(
        f'{deal_base}/dealing-gateway/costsandcharges/{acc}/open',
        headers={**headers, 'Version': '1'},
        json=payload,
        timeout=10,
    )
    if r.status_code == 200:
        cost = r.json().get('notionalValueInUserCurrency')
        log.info(f"Cost for {epic} barrier={barrier}: {cost:.2f}€")
        return cost
    log.error(f"Cost check failed: {r.status_code} — {r.text[:200]}")
    return None


def _place_order(
    epic:           str,
    direction:      str,
    knockout_level: int,
    deal_base:      str,
    api_base:       str,
    headers:        dict,
    size:           int = 1,
) -> dict | None:
    """Place a single knockout order. Returns position dict or None."""

    # fresh quote — expires in seconds
    details  = _get_market_details(epic, api_base, headers)
    snapshot = details.get('marketSnapshotData', {})
    price    = float(snapshot.get('ask' if direction == 'BUY' else 'bid', 0))
    quote_id = snapshot.get('askQuoteId' if direction == 'BUY' else 'bidQuoteId')

    if not price or not quote_id:
        log.error(f"Could not get quote for {epic}")
        return None

    payload = {
        "epic":                  epic,
        "expiry":                EXPIRY,
        "direction":             direction,
        "size":                  size,
        "orderType":             "QUOTE",
        "quoteId":               quote_id,
        "level":                 price,
        "currencyCode":          "USD",
        "guaranteedStop":        False,
        "forceOpen":             True,
        "timeInForce":           "EXECUTE_AND_ELIMINATE",
        "trailingStop":          False,
        "trailingStopIncrement": None,
        "limitDistance":         None,
        "stopDistance":          None,
        "knockoutLevel":         knockout_level,
        "submittedTimestamp":    int(time.time() * 1000),
    }

    log.info(f"Placing {direction} {epic} KO={knockout_level} size={size}")

    r = requests.post(
        f'{deal_base}/nwtpdeal/v3/orders/positions/otc',
        headers={**headers, 'Version': '2'},
        json=payload,
        timeout=10,
    )

    if r.status_code != 200:
        log.error(f"Order failed: {r.status_code} — {r.text[:200]}")
        return None

    ref = r.json().get('dealReference')
    log.info(f"Deal reference: {ref}")

    if not _verify_order(ref, deal_base, headers):
        log.error(f"Order {ref} not found in positions — likely rejected")
        return None

    return {
        'dealReference': ref,
        'epic':          epic,
        'direction':     direction,
        'knockoutLevel': knockout_level,
        'size':          size,
        'level':         price,
    }


def open_straddle(
    svc,
    notional:    float = 500,
    barrier_pct: float = None,
    is_live:     bool  = False,
) -> dict:
    """
    Open a balanced straddle on Brent knockouts.
    Returns dict with 'error' key on failure, full position details on success.
    """
    if barrier_pct is None:
        barrier_pct = CONFIG['ig']['default_barrier_pct']

    deal_base, api_base = _get_base_urls(is_live)
    headers             = _get_headers(svc)

    # ── fetch market details ──────────────────────────────────────────────
    log.info("Fetching call market details...")
    call_details  = _get_market_details(CALL_EPIC, api_base, headers)
    call_snapshot = call_details.get('marketSnapshotData', {})
    call_knockout = call_details.get('knockoutData', {})
    call_ask      = float(call_snapshot.get('ask', 0))
    call_bid      = float(call_snapshot.get('bid', 0))
    call_levels   = call_knockout.get('levels', [])

    log.info("Fetching put market details...")
    put_details   = _get_market_details(PUT_EPIC, api_base, headers)
    put_snapshot  = put_details.get('marketSnapshotData', {})
    put_knockout  = put_details.get('knockoutData', {})
    put_ask       = float(put_snapshot.get('ask', 0))
    put_bid       = float(put_snapshot.get('bid', 0))
    put_levels    = put_knockout.get('levels', [])

    if not call_ask or not put_bid:
        log.error("Could not fetch market prices")
        return {'error': "Could not fetch market prices"}

    current_price = (call_bid + call_ask) / 2
    log.info(f"Current Brent mid: {current_price:.1f}")

    # ── check available barrier range ─────────────────────────────────────
    call_max_pct = (current_price - min(call_levels)) / current_price
    put_max_pct  = (max(put_levels) - current_price) / current_price
    max_balanced = min(call_max_pct, put_max_pct)

    log.info(f"Max call distance: {call_max_pct*100:.1f}%  Max put: {put_max_pct*100:.1f}%  Max balanced: {max_balanced*100:.1f}%")

    if barrier_pct > max_balanced:
        return {'error': (
            f"Barrier {barrier_pct*100:.0f}% not available\n"
            f"Max call: {call_max_pct*100:.1f}%\n"
            f"Max put:  {put_max_pct*100:.1f}%\n"
            f"Max balanced: {max_balanced*100:.1f}%"
        )}

    # ── select barrier levels ─────────────────────────────────────────────
    call_barrier = _find_closest_level(call_levels, current_price * (1 - barrier_pct))
    put_barrier  = _find_closest_level(put_levels,  current_price * (1 + barrier_pct))

    log.info(f"Call barrier ({barrier_pct*100:.0f}%↓): {call_barrier}")
    log.info(f"Put barrier  ({barrier_pct*100:.0f}%↑): {put_barrier}")

    # ── check actual costs ────────────────────────────────────────────────
    call_cost = _get_leg_cost(CALL_EPIC, call_barrier, deal_base, headers, api_base)
    put_cost  = _get_leg_cost(PUT_EPIC,  put_barrier,  deal_base, headers, api_base)

    if not call_cost or not put_cost:
        return {'error': "Could not fetch costs"}

    if call_cost <= 0 or put_cost <= 0:
        return {'error': f"Invalid costs — call: {call_cost:.0f}€ put: {put_cost:.0f}€"}

    total_cost = call_cost + put_cost
    log.info(f"Call cost: {call_cost:.0f}€  Put cost: {put_cost:.0f}€  Total: {total_cost:.0f}€")

    if call_cost > notional or put_cost > notional:
        return {'error': (
            f"Cost per leg exceeds notional ({notional:.0f}€)\n"
            f"Call: ~{call_cost:.0f}€  Put: ~{put_cost:.0f}€\n"
            f"Try: /straddle {int(max(call_cost, put_cost) * 1.15):.0f} {int(barrier_pct*100)}"
        )}

    # ── place orders ──────────────────────────────────────────────────────
    log.info(f"Notional: {notional}€ — placing size=1 per leg")

    call_result = _place_order(
        epic           = CALL_EPIC,
        direction      = 'BUY',
        knockout_level = call_barrier,
        deal_base      = deal_base,
        api_base       = api_base,
        headers        = headers,
    )
    if not call_result:
        return {'error': "Call leg failed"}

    log.info(f"✅ Call leg placed: {call_result}")

    put_result = _place_order(
        epic           = PUT_EPIC,
        direction      = 'BUY',
        knockout_level = put_barrier,
        deal_base      = deal_base,
        api_base       = api_base,
        headers        = headers,
    )
    if not put_result:
        log.error("Put leg failed — call is open!")
        return {
            'error': "Put leg failed — call is open, close manually!",
            'call':  call_result,
        }

    log.info(f"✅ Put leg placed: {put_result}")

    return {
        'call':          call_result,
        'put':           put_result,
        'current_price': current_price,
        'call_barrier':  call_barrier,
        'put_barrier':   put_barrier,
        'call_cost':     call_cost,
        'put_cost':      put_cost,
        'total_cost':    total_cost,
        'notional':      notional,
        'opened_at':     time.time(),
    }


def close_straddle(positions: list, svc, is_live: bool = False) -> bool:
    """Close all open straddle legs — TODO."""
    log.warning("close_straddle not yet implemented")
    return False