#!/bin/bash
# run.sh — start MonsieurMarket + Bloomberg monitor
# Usage:
#   ./run.sh            # headless (default)
#   ./run.sh --visible  # watch Bloomberg browse

cd "$(dirname "$0")"
source venv/bin/activate

# pass --visible through to monitor if requested
BLOOMBERG_FLAG=""
if [[ "$1" == "--visible" ]]; then
    BLOOMBERG_FLAG="--visible"
    echo "🦊 Starting Bloomberg monitor (headful)..."
else
    echo "🦊 Starting Bloomberg monitor (headless)..."
fi

python bloomberg_camoufox/monitor.py $BLOOMBERG_FLAG &
BLOOMBERG_PID=$!

echo "🎩 Starting MonsieurMarket..."
python monsieur_market.py

# clean shutdown — kill bloomberg when monsieur_market exits
echo "👋 Shutting down Bloomberg monitor..."
kill $BLOOMBERG_PID 2>/dev/null
wait $BLOOMBERG_PID 2>/dev/null
echo "✅ All stopped"