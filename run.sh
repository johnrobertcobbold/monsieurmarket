#!/bin/bash
# run.sh — start MonsieurMarket (which starts Bloomberg monitor automatically)
# Usage:
#   ./run.sh            # headless Bloomberg (default)
#   ./run.sh --visible  # watch Bloomberg browse in a real browser window

cd "$(dirname "$0")"
source venv/bin/activate

echo "🎩 Starting MonsieurMarket..."
python monsieur_market.py "$@"