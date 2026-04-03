#!/usr/bin/env python3
"""Get clobTokenIds for our watched markets — needed for Polymarket websocket."""
import json, requests

MARKETS = {
    "US forces enter Iran — April 30":
        "0x6d0e09d0f04572d9b1adad84703458b0297bc5603b69dccbde93147ee4443246",
    "US forces enter Iran — Dec 31":
        "0xe4b9a52d2bb336ff9d84799b70e72e8e5f4507df881af60f3f4daeb9f541a80e",
}

for label, condition_id in MARKETS.items():
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"condition_id": condition_id},
        timeout=10,
    )
    data = r.json()
    if not data:
        print(f"{label}: NOT FOUND")
        continue
    m = data[0]
    token_ids = json.loads(m.get("clobTokenIds", "[]"))
    print(f"\n{label}")
    print(f"  conditionId: {condition_id}")
    print(f"  Yes token: {token_ids[0] if token_ids else '?'}")
    print(f"  No token:  {token_ids[1] if len(token_ids) > 1 else '?'}")
    print(f"  lastTradePrice: {m.get('lastTradePrice')}  bestBid: {m.get('bestBid')}  bestAsk: {m.get('bestAsk')}")
