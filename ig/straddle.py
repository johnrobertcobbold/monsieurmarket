"""
ig/straddle.py — IG knockout straddle execution
"""

import os
import logging
from ig.service import get_ig_service

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger('MonsieurMarket')


def open_straddle(notional: float, ig_service=None) -> dict | None:
    """Fetch knockout products and execute balanced straddle — TODO."""
    return None


def close_straddle(positions: list, ig_service=None) -> bool:
    """Close all open straddle legs — TODO."""
    return False


# ─────────────────────────────────────────────
# DEV / DISCOVERY
# ─────────────────────────────────────────────
if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()

    import os
    print(f"IG_USERNAME: {os.getenv('IG_USERNAME')}")
    print(f"IG_PASSWORD: {'set' if os.getenv('IG_PASSWORD') else 'NOT SET'}")
    print(f"IG_API_KEY:  {'set' if os.getenv('IG_API_KEY') else 'NOT SET'}")
    print(f"IG_ACC_NUMBER: {os.getenv('IG_ACC_NUMBER')}")

    svc = get_ig_service()
    print(f"Service: {svc}")

    try:
        print("\n=== SEARCH: Brent knockout call ===")
        results = svc.search_markets("Brent knockout call")
        print(results)
    except Exception as e:
        print(f"ERROR: {e}")

    try:
        print("\n=== SEARCH: Brent knockout put ===")
        results = svc.search_markets("Brent knockout put")
        print(results)
    except Exception as e:
        print(f"ERROR: {e}")