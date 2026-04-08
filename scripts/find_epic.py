#!/usr/bin/env python3
"""
find_epic.py — Test knockout order on DEMO using real endpoint and payload.

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
    print("❌ trading-ig not installed.")
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
    print("❌ IG_USERNAME / IG_PASSWORD / IG_API_KEY missing")
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

# build headers AFTER switch
demo_headers_raw = demo_svc.crud_session.session.headers
demo_base_url    = demo_svc.crud_session.BASE_URL

demo_headers = {
    'X-IG-API-KEY':     demo_headers_raw.get('X-IG-API-KEY'),
    'CST':              demo_headers_raw.get('CST'),
    'X-SECURITY-TOKEN': demo_headers_raw.get('X-SECURITY-TOKEN'),
    'IG-ACCOUNT-ID':    demo_acc_num,
    'Content-Type':     'application/json',
    'Accept':           'application/json; charset=UTF-8',
    'x-device-user-agent':   'vendor=IG Group | applicationType=ig | platform=WTP | version=0.6631.0+073d24c3',
}

print(f"Account:  {demo_acc_num}")
print(f"Token:    {demo_headers['X-SECURITY-TOKEN'][:20]}...")

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
INTERNAL_BASE = 'https://demo-deal.ig.com'

def get_market_details(epic: str) -> dict:
    r = req.get(
        f'{INTERNAL_BASE}/nwtpdeal/v3/markets/details',
        headers={**demo_headers, 'Version': '1'},
        params={'epic': epic},
    )
    if r.status_code == 200:
        return r.json()
    print(f"❌ market details failed: {r.status_code} — {r.text[:200]}")
    return {}


def find_closest_level(levels: list, target: float) -> int:
    if not levels:
        return int(target)
    return min(levels, key=lambda x: abs(x - target))


# ─────────────────────────────────────────────
# FETCH MARKET DETAILS
# ─────────────────────────────────────────────
print("\n=== CALL market details ===")
call_details    = get_market_details('CC.D.LCO.OPTCALL.IP')
call_snapshot   = call_details.get('marketSnapshotData', {})
call_knockout   = call_details.get('knockoutData', {})

call_ask        = float(call_snapshot.get('ask', 0))
call_bid        = float(call_snapshot.get('bid', 0))
call_quote_id   = call_snapshot.get('askQuoteId')
call_default_kl = call_knockout.get('defaultLevel')
call_levels     = call_knockout.get('levels', [])

print(f"Bid/Ask:        {call_bid} / {call_ask}")
print(f"Ask QuoteId:    {call_quote_id}")
print(f"Default KO lvl: {call_default_kl}")
print(f"Levels ({len(call_levels)}): {call_levels[:5]}...{call_levels[-5:]}")

print("\n=== PUT market details ===")
put_details    = get_market_details('CC.D.LCO.OPTPUT.IP')
put_snapshot   = put_details.get('marketSnapshotData', {})
put_knockout   = put_details.get('knockoutData', {})

put_ask        = float(put_snapshot.get('ask', 0))
put_bid        = float(put_snapshot.get('bid', 0))
put_quote_id   = put_snapshot.get('bidQuoteId')
put_default_kl = put_knockout.get('defaultLevel')
put_levels     = put_knockout.get('levels', [])

print(f"Bid/Ask:        {put_bid} / {put_ask}")
print(f"Bid QuoteId:    {put_quote_id}")
print(f"Default KO lvl: {put_default_kl}")

current_price = (call_bid + call_ask) / 2
print(f"\nCurrent Brent mid: {current_price:.1f}")

# ─────────────────────────────────────────────
# CALCULATE BARRIERS
# ─────────────────────────────────────────────
call_barrier = find_closest_level(call_levels, current_price * 0.85)
put_barrier  = find_closest_level(put_levels,  current_price * 1.15)

print(f"Call barrier (15%↓): {call_barrier}")
print(f"Put barrier  (15%↑): {put_barrier}")


# ─────────────────────────────────────────────
# PLACE ORDER HELPER
# ─────────────────────────────────────────────
def place_order(epic, direction, knockout_level, label):
    print(f"\n=== {label} ===")

    # always refresh quote — expires in seconds
    details  = get_market_details(epic)
    snapshot = details.get('marketSnapshotData', {})

    if direction == 'BUY':
        price    = float(snapshot.get('ask', 0))
        quote_id = snapshot.get('askQuoteId')
    else:
        price    = float(snapshot.get('bid', 0))
        quote_id = snapshot.get('bidQuoteId')

    payload = {
        "epic":                  epic,
        "expiry":                "  FEB27",  # two spaces
        "direction":             direction,
        "size":                  1,
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

    print(f"Payload: {json.dumps(payload, indent=2)}")

    # try both endpoints
    for endpoint in [
        f'{INTERNAL_BASE}/nwtpdeal/v3/orders/positions/otc',
        f'{demo_base_url}/positions/otc',
    ]:
        print(f"\nTrying: {endpoint}")
        r = req.post(
            endpoint,
            headers={**demo_headers, 'Version': '2'},
            json=payload,
        )
        print(f"Status: {r.status_code} — {r.text[:200]}")

        if r.status_code == 200:
            ref = r.json().get('dealReference')
            print(f"Deal reference: {ref}")
            time.sleep(1)
            confirm = req.get(
                f'{demo_base_url}/confirms/{ref}',
                headers={**demo_headers, 'Version': '1'},
            ).json()
            print(f"\nConfirm: {json.dumps(confirm, indent=2)}")
            status = confirm.get('dealStatus')
            if status == 'ACCEPTED':
                print(f"\n🎉 ORDER ACCEPTED on {endpoint}!")
                return confirm
            break

    return None


# ─────────────────────────────────────────────
# TEST ORDERS
# ─────────────────────────────────────────────
call_confirm = place_order(
    epic           = 'CC.D.LCO.OPTCALL.IP',
    direction      = 'BUY',
    knockout_level = call_default_kl,
    label          = f'CALL default KO={call_default_kl}',
)

time.sleep(3)

put_confirm = place_order(
    epic           = 'CC.D.LCO.OPTPUT.IP',
    direction      = 'BUY',
    knockout_level = put_default_kl,
    label          = f'PUT default KO={put_default_kl}',
)

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