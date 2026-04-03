#!/usr/bin/env python3
"""
find_polymarket.py — Find conditionIds for Polymarket markets.
Run once to get the right IDs for monsieur_market.py config.

Usage:
    /opt/homebrew/bin/python3 find_polymarket.py
"""
import requests

# Event ID 158299 = "US forces enter Iran by..?"
r = requests.get("https://gamma-api.polymarket.com/events/158299", timeout=10)
event = r.json()

print(f"Event: {event['title']}")
print(f"Volume: ${event['volume']:,.0f}  |  24h: ${event['volume24hr']:,.0f}")
print(f"Open interest: ${event['openInterest']:,.0f}")
print()
print(f"{'QUESTION':<45} {'CONDITION ID':<70} {'CLOSED':<8} {'YES%'}")
print("-" * 140)

for m in event.get("markets", []):
    question   = m.get("question", "")
    cid        = m.get("conditionId", "")
    closed     = m.get("closed", False)
    prices_raw = m.get("outcomePrices", '["?","?"]')
    try:
        import json
        prices = json.loads(prices_raw)
        yes_pct = f"{float(prices[0])*100:.1f}%"
    except Exception:
        yes_pct = "?"
    print(f"{question:<45} {cid:<70} {'YES' if closed else 'open':<8} {yes_pct}")
