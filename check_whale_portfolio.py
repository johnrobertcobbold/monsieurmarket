#!/opt/homebrew/bin/python3
"""
check_whale_portfolio.py — Discover new relevant markets from a whale's trade history.

Called by monsieur_market.py when a whale crosses a cumulative threshold.
Returns JSON to stdout:
{
  "wallet": "0x...",
  "checked": N,           # new markets evaluated
  "already_known": N,     # markets already in relevance cache
  "new_relevant": [       # newly discovered relevant markets
    {
      "condition_id": "0x...",
      "title": "...",
      "reason": "...",
      "whale_amount": 12500.0,
      "direction": "YES"
    }
  ]
}

Usage:
    /opt/homebrew/bin/python3 check_whale_portfolio.py <proxyWallet>
"""

import os
import sys
import json
import time
import requests
import anthropic
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SCRIPT_DIR          = Path(__file__).parent
RELEVANCE_FILE      = SCRIPT_DIR / "market_relevance.json"
DATA_API            = "https://data-api.polymarket.com"
GAMMA_API           = "https://gamma-api.polymarket.com"

MIN_WHALE_AMOUNT    = 5_000   # ignore markets where whale traded < $5k
MAX_NEW_CHECKS      = 10      # max Haiku calls per run (cost guard)
TRADE_LOOKBACK      = 200     # fetch last N trades from wallet

# Known watched condition IDs — never re-alert on these
WATCHED_CONDITION_IDS = {
    "0x6d0e09d0f04572d9b1adad84703458b0297bc5603b69dccbde93147ee4443246",  # Iran April 30
    "0xe4b9a52d2bb336ff9d84799b70e72e8e5f4507df881af60f3f4daeb9f541a80e",  # Iran Dec 31
    "0x306d10d4a4d51b41910dbc779ca00908bd917c131541c5c42bbbc736258d2d56",  # Iran March 31
}


# ─────────────────────────────────────────────
# RELEVANCE CACHE
# ─────────────────────────────────────────────
def load_relevance() -> dict:
    if RELEVANCE_FILE.exists():
        try:
            return json.loads(RELEVANCE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_relevance(cache: dict):
    RELEVANCE_FILE.write_text(json.dumps(cache, indent=2))


# ─────────────────────────────────────────────
# FETCH WALLET TRADES
# ─────────────────────────────────────────────
def fetch_wallet_trades(proxy_wallet: str) -> list[dict]:
    """Fetch recent trades for a wallet address."""
    try:
        r = requests.get(
            f"{DATA_API}/trades",
            params={"user": proxy_wallet, "limit": TRADE_LOOKBACK, "takerOnly": "true"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        print(f"[error] fetch_wallet_trades: {e}", file=sys.stderr)
        return []


# ─────────────────────────────────────────────
# AGGREGATE BY MARKET
# ─────────────────────────────────────────────
def aggregate_by_market(trades: list) -> dict:
    """
    Returns {condition_id: {yes_usd, no_usd, total_usd, direction}}
    Only markets with total_usd >= MIN_WHALE_AMOUNT.
    """
    markets = {}
    for t in trades:
        cid     = t.get("conditionId", "")
        size    = float(t.get("size", 0))
        price   = float(t.get("price", 0))
        outcome = t.get("outcome", "").upper()
        side    = t.get("side", "BUY").upper()
        amount  = size * price

        if not cid or amount <= 0:
            continue

        if cid not in markets:
            markets[cid] = {"yes_usd": 0, "no_usd": 0, "total_usd": 0, "title": t.get("title", "")}

        is_yes = (outcome == "YES" and side == "BUY") or (outcome == "NO" and side == "SELL")
        if is_yes:
            markets[cid]["yes_usd"] += amount
        else:
            markets[cid]["no_usd"]  += amount
        markets[cid]["total_usd"] += amount

    # Filter and add direction
    result = {}
    for cid, v in markets.items():
        if v["total_usd"] >= MIN_WHALE_AMOUNT:
            v["direction"] = "YES" if v["yes_usd"] >= v["no_usd"] else "NO"
            result[cid] = v
    return result


# ─────────────────────────────────────────────
# HAIKU RELEVANCE CHECK
# ─────────────────────────────────────────────
def haiku_is_relevant(title: str) -> tuple[bool, str]:
    """
    Ask Haiku if this market title is relevant to oil/Iran/geopolitics.
    Returns (is_relevant, reason).
    """
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content":
                f'Polymarket: "{title}"\n\n'
                f"Is this market relevant to: oil prices, Brent crude, Iran conflict, "
                f"Middle East geopolitics, OPEC, energy supply, or US military action?\n\n"
                f"Reply format: YES: <one short reason> OR NO: <one short reason>"
            }],
        )
        text = response.content[0].text.strip()
        is_yes = text.upper().startswith("YES")
        reason = text.split(":", 1)[-1].strip() if ":" in text else text
        return is_yes, reason
    except Exception as e:
        return False, f"haiku error: {e}"


# ─────────────────────────────────────────────
# FETCH MARKET TITLE
# ─────────────────────────────────────────────
def fetch_market_title(condition_id: str, known_title: str = "") -> str:
    """Get market question/title from gamma API if not already known."""
    if known_title and len(known_title) > 10:
        return known_title
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"conditionId": condition_id},
            timeout=8,
        )
        data = r.json()
        if isinstance(data, list) and data:
            return data[0].get("question", condition_id)
        elif isinstance(data, dict):
            return data.get("question", condition_id)
    except Exception:
        pass
    return condition_id


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: check_whale_portfolio.py <proxyWallet>"}))
        sys.exit(1)

    proxy_wallet = sys.argv[1].strip()
    relevance    = load_relevance()

    # Fetch wallet trades
    trades = fetch_wallet_trades(proxy_wallet)
    if not trades:
        print(json.dumps({
            "wallet": proxy_wallet, "checked": 0,
            "already_known": 0, "new_relevant": []
        }))
        return

    # Aggregate by market
    by_market = aggregate_by_market(trades)

    already_known = 0
    checked       = 0
    new_relevant  = []

    for cid, market_data in by_market.items():
        # Skip already watched markets
        if cid in WATCHED_CONDITION_IDS:
            already_known += 1
            continue

        # Already in relevance cache
        if cid in relevance:
            already_known += 1
            continue

        # Cap new checks per run
        if checked >= MAX_NEW_CHECKS:
            break

        # Fetch title if needed
        title = fetch_market_title(cid, market_data.get("title", ""))

        # Ask Haiku
        is_relevant, reason = haiku_is_relevant(title)
        checked += 1

        # Cache result
        relevance[cid] = {
            "title":     title,
            "relevant":  is_relevant,
            "reason":    reason,
            "checked":   time.strftime("%Y-%m-%d"),
        }
        save_relevance(relevance)

        if is_relevant:
            new_relevant.append({
                "condition_id":  cid,
                "title":         title,
                "reason":        reason,
                "whale_amount":  market_data["total_usd"],
                "direction":     market_data["direction"],
                "yes_usd":       market_data["yes_usd"],
                "no_usd":        market_data["no_usd"],
            })

        # Small delay to avoid Haiku rate limits
        time.sleep(0.3)

    print(json.dumps({
        "wallet":       proxy_wallet,
        "checked":      checked,
        "already_known": already_known,
        "new_relevant": new_relevant,
    }))


if __name__ == "__main__":
    main()
