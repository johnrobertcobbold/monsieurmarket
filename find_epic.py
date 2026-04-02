#!/usr/bin/env python3
"""
find_epic.py — Find your Brent instrument epic on IG Markets.
Run this once to identify the right epic for monsieur_market.py.

Usage:
    python find_epic.py
"""

import os
from dotenv import load_dotenv
load_dotenv()

try:
    from trading_ig import IGService
except ImportError:
    print("❌ trading-ig not installed. Run: pip install trading-ig pandas munch tenacity")
    exit(1)

username = os.getenv("IG_USERNAME")
password = os.getenv("IG_PASSWORD")
api_key  = os.getenv("IG_API_KEY")
acc_num  = os.getenv("IG_ACC_NUMBER")

if not all([username, password, api_key]):
    print("❌ IG credentials missing in .env (IG_USERNAME, IG_PASSWORD, IG_API_KEY)")
    exit(1)

print("Connecting to IG demo account...")
try:
    svc = IGService(username, password, api_key, acc_type="DEMO")
    svc.create_session()
    if acc_num:
        svc.switch_account(acc_num, False)
    print("✅ Connected\n")
except Exception as e:
    print(f"❌ Login failed: {e}")
    exit(1)

print("Searching for Brent instruments...\n")
try:
    results = svc.search_markets("Brent")

    # trading-ig may return a DataFrame, a dict, or a list depending on version
    import pandas as pd
    if isinstance(results, pd.DataFrame):
        rows = results.to_dict(orient="records")
    elif isinstance(results, dict):
        # often wrapped as {"markets": [...]}
        rows = results.get("markets", [results])
    elif isinstance(results, list):
        rows = results
    else:
        rows = [results]

    if not rows:
        print("No results returned.")
        exit(1)

    # Print as a clean table
    keys = ["epic", "instrumentName", "instrumentType", "expiry"]
    # Print header
    print(f"{'EPIC':<35} {'NAME':<45} {'TYPE':<20} {'EXPIRY'}")
    print("-" * 110)
    for row in rows:
        if isinstance(row, dict):
            epic  = row.get("epic", "")
            name  = row.get("instrumentName", row.get("description", ""))
            itype = row.get("instrumentType", "")
            exp   = row.get("expiry", "-")
            print(f"{epic:<35} {name:<45} {itype:<20} {exp}")
        else:
            print(row)

except Exception as e:
    print(f"❌ Search failed: {e}")
    import traceback; traceback.print_exc()
    exit(1)

print("\n---")
print("Copy the epic you want into .env as: IG_BRENT_EPIC=<epic>")
print("For spot price tracking, look for no expiry / rolling front month.")
