#!/usr/bin/env python3
"""
find_epic.py — Discover Brent knockout barriers and test order placement on DEMO.

Flow:
    1. Connect to DEMO
    2. Fetch market details via internal API (price + quoteId + barrier levels)
    3. Calculate target barriers
    4. Test order placement with various expiry formats

Usage:
    python scripts/find_epic.py
"""

import os
import json
import time
import requests as req
from dotenv import load_dotenv
load_dotenv()

try:
    from trading_ig import IGService
except ImportError:
    print("❌ trading-ig not installed. Run: pip install trading-ig pandas munch tenacity")
    exit(1)

# ─────────────────────────────────────────────
# DEMO — connect
# ─────────────────────────────────────────────
print("=" * 60)
print("Connect to DEMO")
print("=" * 60)

demo_user    = os.getenv("IG_USERNAME")
demo_pass    = os.getenv("IG_PASSWORD")
demo_key     = os.getenv("IG_API_KEY")
demo_acc_num = os.getenv("IG_ACC_NUMBER")

if not all([demo_user, demo_pass, demo_key]):
    print("❌ IG_USERNAME / IG_PASSWORD / IG_API_KEY missing in .env")
    exit(1)

try:
    demo_svc = IGService(demo_user, demo_pass, demo_key, acc_type="DEMO")
    demo_svc.create_session()
    if demo_acc_num:
        demo_svc.switch_account(demo_acc_num, False)
    print("✅ DEMO connected\n")
except Exception as e:
    print(f"❌ DEMO login failed: {e}")
    exit(1)

demo_headers_raw = demo_svc.crud_session.session.headers
demo_base_url    = demo_svc.crud_session.BASE_URL

demo_headers = {
    'X-IG-API-KEY':     demo_headers_raw.get('X-IG-API-KEY'),
    'CST':              demo_headers_raw.get('CST'),
    'X-SECURITY-TOKEN': demo_headers_raw.get('X-SECURITY-TOKEN'),
    'Content-Type':     'application/json',
    'Accept':           'application/json; charset=UTF-8',
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def get_market_details(epic: str) -> dict:
    """Fetch full market details including quoteId and available barrier levels."""
    r = req.get(
        'https://demo-api.ig.com/nwtpdeal/v3/markets/details',
        headers={**demo_headers, 'Version': '1'},
        params={'epic': epic},
    )
    if r.status_code == 200:
        return r.json()
    print(f"❌ market details failed: {r.status_code} — {r.text[:200]}")
    return {}


def find_closest_level(levels: list, target: float) -> int:
    """Find the available barrier level closest to target price."""
    if not levels:
        return int(target)
    return min(levels, key=lambda x: abs(x - target))


# ─────────────────────────────────────────────
# FETCH MARKET DETAILS
# ─────────────────────────────────────────────
print("=== CALL market details ===")
call_details    = get_market_details('CC.D.LCO.OPTCALL.IP')
call_snapshot   = call_details.get('marketSnapshotData', {})
call_knockout   = call_details.get('knockoutData', {})
call_instrument = call_details.get('instrumentData', {})

call_bid        = float(call_snapshot.get('bid', 0))
call_ask        = float(call_snapshot.get('ask', 0))
call_quote_id   = call_snapshot.get('askQuoteId')
call_default_kl = call_knockout.get('defaultLevel')
call_levels     = call_knockout.get('levels', [])
call_expiry     = call_instrument.get('expiry', '')

print(f"Bid:             {call_bid}")
print(f"Ask:             {call_ask}")
print(f"Ask QuoteId:     {call_quote_id}")
print(f"Default KO lvl:  {call_default_kl}")
print(f"Expiry from API: '{call_expiry}'")
print(f"Available levels ({len(call_levels)}): {call_levels[:5]}...{call_levels[-5:]}")

print("\n=== PUT market details ===")
put_details    = get_market_details('CC.D.LCO.OPTPUT.IP')
put_snapshot   = put_details.get('marketSnapshotData', {})
put_knockout   = put_details.get('knockoutData', {})
put_instrument = put_details.get('instrumentData', {})

put_bid        = float(put_snapshot.get('bid', 0))
put_ask        = float(put_snapshot.get('ask', 0))
put_quote_id   = put_snapshot.get('bidQuoteId')
put_default_kl = put_knockout.get('defaultLevel')
put_levels     = put_knockout.get('levels', [])
put_expiry     = put_instrument.get('expiry', '')

print(f"Bid:             {put_bid}")
print(f"Ask:             {put_ask}")
print(f"Bid QuoteId:     {put_quote_id}")
print(f"Default KO lvl:  {put_default_kl}")
print(f"Expiry from API: '{put_expiry}'")
print(f"Available levels ({len(put_levels)}): {put_levels[:5]}...{put_levels[-5:]}")

# ─────────────────────────────────────────────
# CURRENT PRICE + BARRIERS
# ─────────────────────────────────────────────
current_price = (call_bid + call_ask) / 2
print(f"\nCurrent Brent mid: {current_price:.1f}")

call_target  = current_price * 0.85
put_target   = current_price * 1.15
call_barrier = find_closest_level(call_levels, call_target)
put_barrier  = find_closest_level(put_levels,  put_target)

print(f"\nSelected barriers (15% distance, closest available):")
print(f"  Call barrier (15%↓): {call_barrier}")
print(f"  Put barrier  (15%↑): {put_barrier}")

# ─────────────────────────────────────────────
# TEST EXPIRY FORMATS
# ─────────────────────────────────────────────
print("\n=== Testing expiry formats ===")

for expiry in [
    call_expiry,
    'FEB-27',
    'FEB27',
    '  FEB-27',
    '  FEB27',
    ' FEB-27',
    ' FEB27',
    'feb-27',
]:
    # refresh quote on every attempt — quotes expire quickly
    details  = get_market_details('CC.D.LCO.OPTCALL.IP')
    snapshot = details.get('marketSnapshotData', {})
    ask      = float(snapshot.get('ask', 0))
    quote_id = snapshot.get('askQuoteId')

    payload = {
        "epic":                  "CC.D.LCO.OPTCALL.IP",
        "expiry":                expiry,
        "direction":             "BUY",
        "size":                  1,
        "orderType":             "QUOTE",
        "quoteId":               quote_id,
        "level":                 ask,
        "currencyCode":          "USD",
        "guaranteedStop":        False,
        "forceOpen":             True,
        "knockoutLevel":         call_default_kl,
        "timeInForce":           "EXECUTE_AND_ELIMINATE",
        "trailingStop":          False,
        "stopDistance":          None,
        "limitDistance":         None,
        "trailingStopIncrement": None,
    }

    r = req.post(
        f'{demo_base_url}/positions/otc',
        headers={**demo_headers, 'Version': '2'},
        json=payload,
    )

    if r.status_code == 400:
        print(f"  '{expiry}': 400 — {r.json().get('errorCode')}")
    elif r.status_code == 200:
        ref = r.json().get('dealReference')
        time.sleep(1)
        confirm = req.get(
            f'{demo_base_url}/confirms/{ref}',
            headers={**demo_headers, 'Version': '1'},
        ).json()
        status = confirm.get('dealStatus')
        reason = confirm.get('reason')
        print(f"  '{expiry}': {status} — {reason}")
        if status == 'ACCEPTED':
            print(f"  🎉 ORDER ACCEPTED! deal_id={confirm.get('dealId')}")
            print(f"  level={confirm.get('level')} size={confirm.get('size')}")
            break
    else:
        print(f"  '{expiry}': {r.status_code} — {r.text[:100]}")

    time.sleep(2)

# ─────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────
print("\n=== DEMO open positions ===")
try:
    positions = demo_svc.fetch_open_positions()
    if len(positions) > 0:
        print(positions[['epic', 'direction', 'size', 'level',
                          'bid', 'offer', 'stopLevel', 'limitLevel',
                          'dealId']].to_string())
    else:
        print("No open positions")
except Exception as e:
    print(f"❌ {e}")

print("\n---")
print("Done.")