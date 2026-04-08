# 🎩 MonsieurMarket

Personal geopolitical trading intelligence bot. Monitors Brent crude price, Bloomberg liveblogs, news signals, Trump posts, and Polymarket whale trades. Alerts via Telegram. Can execute IG knockout straddles automatically.

Built on Anthropic Claude (Haiku for filtering, Sonnet for analysis) + Camoufox for Bloomberg scraping.

---

## What It Does

### Real-time (sub-second)
- **Brent price stream** via IG Lightstreamer — fires on 5min/10min/day-open moves
- **Polymarket websocket** — whale trades ($10k+) tracked in real-time, instant alert >$100k
- **Bloomberg on-demand** — triggered automatically on Brent price alerts, fetches fresh context via scheduler

### Near real-time
- **Trump watcher** — polls trumpstruth.org/feed every 2 min, Haiku filters (no keyword pre-filter — oblique posts handled), Sonnet analyses relevant posts immediately
- **Bloomberg monitor** — scrapes Bloomberg liveblog or best In Focus tag on a market-hours-aware schedule (5 min during 08-22 UTC, 15 min pre-market, 60 min overnight). MM owns the schedule, monitor is purely reactive.

### Scheduled event monitoring
- **Event watcher** — hot-reloadable scheduled_events.json defines known events (NFP, OPEC, speeches)
- Arms at event start, watches for price move, confirms over N minutes, fires simulated order
- **RSS fetch at open** — fetches curated RSS feeds X minutes after event opens
- **Post-execution web search** — Haiku web search fires X minutes after simulated order
- **Trade log** — every stage logged to data/trades/YYYY-MM-DD_eventid.md

### Automated trading (IG Knockouts)
- **Telegram command** — `/straddle [notional] [barrier_%]` opens balanced Brent knockout straddle
- **Smart pre-checks** — validates barrier availability and real EUR cost before placing
- **Both legs verified** — confirms positions via internal IG API before reporting success
- **Demo + live support** — tested on demo, ready for live when funded

### Whale intelligence
- Polymarket whale ledger tracks cumulative 24h flow per pseudonym
- Alerts at every $50k band crossed
- Three trigger types: single trade >$50k, single trader >$75k, net directional flow >$150k at 70%+ one-way

---

## Architecture

\`\`\`
run.sh
  └── monsieur_market.py          <- brain, orchestrator, signal router
        │
        ├── MM Signal API :3456   <- /signal endpoint, all sources POST here
        │
        ├── bloomberg/
        │     └── monitor.py      <- Camoufox browser, Flask :3457
        │                            NO internal timer — waits for MM /refresh calls
        │                            POSTs signals back to MM on new posts
        │
        ├── ig/
        │     ├── service.py      <- IGService session management
        │     ├── streamer.py     <- Brent price watcher, tick callbacks
        │     ├── straddle.py     <- knockout straddle execution (demo + live)
        │     └── positions.py   <- position tracking (TODO)
        │
        ├── polymarket/
        │     └── polymarket.py   <- trades, ledger, websocket
        │
        ├── scheduled_event_watcher.py  <- event windows, trade logs
        └── config.py                   <- all configuration
\`\`\`

### Signal flow

\`\`\`
monitor.py      →  POST /signal {source: bloomberg, type: ready|post|error}
                →  MM receives, BloombergScheduler starts, Telegram sent

Brent spike     →  BloombergScheduler.trigger_now()
                →  GET /refresh on monitor
                →  monitor scrapes, POSTs new posts as signals
                →  MM sends to Telegram

Trump post      →  Haiku filter (no keyword pre-check)
                →  Sonnet + web search analysis
                →  Telegram alert

Telegram cmd    →  /straddle 500 4
                →  ig/straddle.py open_straddle()
                →  barrier check + cost check
                →  place call + put via IG internal API
                →  verify both legs in positions
                →  Telegram confirmation
\`\`\`

### Telegram responsibility
All Telegram messages originate from \`monsieur_market.py\` via \`telegram_client.py\`.
Monitor never sends Telegram directly — it signals MM, MM decides.

---

## Folder Structure

\`\`\`
monsieurmarket/
├── .env                          <- secrets, never commit
├── config.py                     <- all configuration + themes
├── monsieur_market.py            <- main entry point + brain
├── telegram_client.py            <- single place for all Telegram messaging
├── scheduled_event_watcher.py    <- event monitoring
├── scheduled_events.json         <- hot-reloadable event definitions
├── rss_sources.json              <- curated RSS feeds
├── run.sh                        <- start script (starts MM which starts monitor)
│
├── bloomberg/           <- Bloomberg scraping layer
│   ├── monitor.py                <- Camoufox browser + Flask API :3457
│   └── setup.py                  <- one-time headful login
│   (gitignored: bloomberg_session.json, bloomberg_feed.json, monitor_state.json)
│
├── ig/                  <- IG Markets trading layer
│   ├── __init__.py               <- exports: get_ig_service, open_straddle etc.
│   ├── service.py                <- IGService session, account switch, headers
│   ├── streamer.py               <- Brent Lightstreamer price watcher
│   ├── straddle.py               <- knockout straddle execution (demo + live)
│   └── positions.py              <- position tracking (TODO)
│
├── polymarket/                   <- Polymarket intelligence
│   ├── polymarket.py             <- trades, ledger, websocket
│   ├── check_whale_portfolio.py  <- portfolio analysis
│   (gitignored: polymarket_markets.json, market_relevance.json)
│
├── data/                         <- gitignored, runtime state
│   ├── monsieur_market_state.json
│   ├── monsieur_market.log
│   └── trades/                   <- markdown trade logs per event
│
├── scripts/                      <- one-off setup utilities
│   ├── find_epic.py              <- IG API discovery + order testing
│   ├── find_polymarket.py        <- search Polymarket markets
│   └── get_token_ids.py          <- get yes/no token IDs
│
└── venv/                         <- gitignored, Python virtualenv
\`\`\`

---

## Setup

### 1. Clone and create virtualenv

\`\`\`bash
git clone <your-repo>
cd monsieurmarket
/opt/homebrew/bin/python3 -m venv venv
source venv/bin/activate
\`\`\`

### 2. Install dependencies

\`\`\`bash
pip install anthropic requests schedule python-dotenv flask camoufox playwright trading-ig pandas munch tenacity
python -m camoufox fetch
\`\`\`

### 3. Create .env

\`\`\`
ANTHROPIC_API_KEY=sk-ant-your-key-here
TELEGRAM_BOT_TOKEN=7123456789:AAF-your-token-here
TELEGRAM_CHAT_ID=your-chat-id-here

# IG Demo
IG_USERNAME=your-ig-username
IG_PASSWORD=your-ig-password
IG_API_KEY=your-ig-api-key
IG_ACC_NUMBER=Z69ZML  # Barrières et Options account

# IG Live (optional, for real trading)
IG_USERNAME_LIVE=your-live-username
IG_PASSWORD_LIVE=your-live-password
IG_API_KEY_LIVE=your-live-api-key
IG_ACC_NUMBER_LIVE=your-live-barrier-account
\`\`\`

### 4. Bloomberg one-time setup

\`\`\`bash
python bloomberg/setup.py
# browser opens, log in to Bloomberg manually
# session saved automatically
\`\`\`

### 5. Run

\`\`\`bash
./run.sh             # headless Bloomberg (default)
./run.sh --visible   # watch Bloomberg browse in a real browser window
\`\`\`

MM starts everything — no need to run monitor separately.

---

## Bloomberg Monitor

Runs a persistent Camoufox (Firefox) browser session that:

1. Opens Bloomberg homepage and scans for active liveblog (today's date in URL)
2. If no liveblog, clicks "See all latest", Haiku picks best In Focus tag (once per day, saved to disk)
3. Navigates to the liveblog or tag page
4. Waits for \`/refresh\` calls from MonsieurMarket — **no internal timer**
5. On scrape, POSTs each new post to MM \`/signal\` endpoint
6. Serves via Flask API on :3457

MM owns the refresh schedule:
- 08:00–22:00 UTC: every ~5 min (with jitter)
- 06:00–08:00 UTC: every ~15 min
- 22:00–06:00 UTC: every ~60 min

Monitor API (internal, used by MM):

| Endpoint | Description |
|---|---|
| GET /health | Status, source type, post count |
| GET /posts?since=ts | Posts newer than timestamp |
| GET /latest | Most recent post |
| GET /refresh | Immediate scrape triggered by MM |

---

## MM Signal API (:3456)

MonsieurMarket exposes a \`/signal\` endpoint that all sources POST to:

\`\`\`json
{
    "source": "bloomberg",
    "type":   "ready|no_source|post|error",
    "data":   { "title": "...", "timestamp_raw": "...", ... },
    "ts":     1744030000
}
\`\`\`

This is the brain's single entry point. Future signal sources (Reuters, Twitter, etc.) plug in here without touching any other code.

---

## Telegram Commands

| Command | Description |
|---|---|
| \`/straddle\` | Open straddle with default notional + barrier |
| \`/straddle 500\` | Open straddle, 500€ per leg |
| \`/straddle 500 4\` | Open straddle, 500€ per leg, 4% barrier distance |
| \`/cut_call\` | Close call leg only |
| \`/cut_put\` | Close put leg only |
| \`/close\` | Close both legs |
| \`/status\` | Show open positions + P&L (TODO) |
| \`/history\` | Show last 5 trades (TODO) |
| \`/call 500\` | Open call only — directional bet (TODO) |
| \`/put 500\` | Open put only — directional bet (TODO) |
| \`/pause\` | Pause automated alerts |
| \`/resume\` | Resume automated alerts |
| \`/help\` | Show available commands |

### Straddle flow
1. Bot checks barrier availability at requested % distance
2. Bot fetches real EUR cost per leg via IG costs API
3. If cost > notional → rejects with suggestion e.g. \`Try: /straddle 420 4\`
4. Places call + put legs sequentially (parallel TODO)
5. Verifies both positions opened via internal IG positions API
6. Sends confirmation with KO levels, distances, real costs

---

## IG Knockout Trading

### API notes
IG's public REST API does not support knockout order placement on demo accounts. MonsieurMarket uses IG's internal web platform API (reverse-engineered from browser network traffic):

| Action | Endpoint |
|---|---|
| Market details + quoteId + barrier levels | \`{api_base}/nwtpdeal/v3/markets/details\` |
| Place order | \`{deal_base}/nwtpdeal/v3/orders/positions/otc\` |
| Verify positions | \`{deal_base}/nwtpdeal/wtp/orders/positions\` |
| Cost estimate | \`{deal_base}/dealing-gateway/costsandcharges/{acc}/open\` |

Required header: \`x-device-user-agent: vendor=IG Group | applicationType=ig | platform=WTP | version=...\`

### Account setup
IG has separate accounts for CFD and knockout products:
- CFD account (default): standard instruments
- **Barrières et Options account**: required for knockout order placement

Set \`IG_ACC_NUMBER\` to the barrier account ID (e.g. \`Z69ZML\` on demo).

### Cost model
At typical Brent volatility (~10% intraday), IG knockout straddles cost ~350-450€ per leg at 4% barrier distance. Suitable for overnight/geopolitical events where automation justifies the higher cost vs SocGen turbos (~15-20€/unit).

---

## Scheduled Events (scheduled_events.json)

Hot-reloadable — edit and save, picks up on next Brent tick. No restart needed.

### Event fields

| Field | Required | Description |
|---|---|---|
| id | yes | Unique identifier |
| name | yes | Human-readable label |
| active | yes | true to monitor |
| watch_from_utc | yes | Window start UTC e.g. "2026-04-03 12:30:00" |
| watch_duration_min | yes | How long to watch |
| trigger_pct | yes | % move to trigger |
| confirm_wait_min | yes | Minutes move must hold |
| action | yes | buy_ig_barrier etc. |
| rss_at_open | no | Fetch RSS after window opens |
| web_search | no | Run Haiku web search after execution |
| log_trade | no | Write markdown trade log |

### Event timing calibration

| Event type | trigger_pct | confirm_wait_min |
|---|---|---|
| EU open | 1.0% | 5 min |
| NFP | 1.5% | 10 min |
| OPEC | 1.5% | 10 min |
| EIA inventory | 1.0% | 5 min |
| Trump speech | 2.0% | 15 min |

---

## Polymarket Markets (polymarket/polymarket_markets.json)

\`\`\`json
[
  {
    "conditionId": "0xabc...",
    "label": "US forces enter Iran by April 30?",
    "yes_token": "0x123...",
    "no_token": "0x456...",
    "active": true
  }
]
\`\`\`

To find token IDs for a new market:

\`\`\`bash
python scripts/get_token_ids.py
\`\`\`

---

## Alert Levels

| Emoji | Score | Meaning |
|---|---|---|
| FYI | 6-7 | Relevant but not urgent |
| WATCH | 7-9 | Material signal, review positions |
| ACT NOW | 9-10 | High conviction, immediate attention |
| ESCALATION | override | Yanbu / Saudi infrastructure, wakes you at 3am |

---

## AI Usage

| Component | Model | When | Approx cost |
|---|---|---|---|
| Bloomberg tag picker | Haiku | Once per day | ~$0.00 |
| Trump filter | Haiku | Every Trump post (~few/day) | ~$0.00 |
| Trump analysis | Sonnet + web search | Per relevant post | ~$0.02 |
| Signal analysis | Sonnet + web search | When signals present (disabled) | ~$0.05 |
| Weekly digest | Sonnet + web search | Sunday 9am (disabled) | ~$0.05 |

---

## Weekly Event Calendar

Ask Claude to generate scheduled_events.json for the coming week:

> "Generate my MonsieurMarket scheduled_events.json for next week — include NFP, EIA inventory, any OPEC meetings, Fed speakers, and known geopolitical events relevant to Brent crude."

Claude will search for current event dates and output a ready-to-paste JSON with calibrated settings per event type.

---

## Roadmap

### Priority 1 — IG execution (finish before anything else)
- [ ] \`close_straddle()\` — close both legs via \`/close\`
- [ ] \`cut_leg()\` — close one leg \`/cut_call\` or \`/cut_put\`
- [ ] Stop loss management — set stops automatically after open, edit via \`/setstop\`
- [ ] \`/status\` — show open positions + P&L
- [ ] \`assess_straddle()\` — query barriers + costs, appended to news alerts
- [ ] \`/call\` \`/put\` — directional single-leg bets for strong conviction
- [ ] Trade history — store locally + fetch from IG \`/history/transactions\`
- [ ] Simultaneous leg placement — parallel threads to reduce slippage

### Priority 2 — 7-day demo test
- [ ] Top up demo account with virtual USD
- [ ] Run bot on demo for 7 days with real news events
- [ ] Track all straddles — entry, exit, P&L
- [ ] T+1 trade review — Sonnet post-mortem 24h after execution
- [ ] Review results before going live

### Priority 3 — Signal quality
- [ ] UKMTO monitor — maritime warnings, often precedes Bloomberg by 10-30 min
- [ ] Sonnet correlation — explain price move using Bloomberg posts
- [ ] Enable run_poll() with token budget
- [ ] Signal buffer in MM for Bloomberg integration

### Priority 4 — Infrastructure
- [ ] monitor.py split — scrapers.py, navigation.py, source_finder.py
- [ ] state.py — extract state management
- [ ] ATR-based dynamic thresholds
- [ ] Whale reputation scoring

### Later — Pi Migration
- [ ] Move to Raspberry Pi (always-on)
- [ ] Copy bloomberg_session.json via scp
- [ ] noVNC — view Pi browser session from Mac
- [ ] Claude Computer Use agent watching browser session

### Future
- [ ] Multi-asset streaming — Thales, TotalEnergies
- [ ] European Rearmament theme activation
- [ ] X/Twitter monitoring ($100/month API tier)
- [ ] Additional signal sources via /signal API
- [ ] Local model evaluation — Ollama on M3 Air for zero-cost filtering (trump pre-filter, Bloomberg relevance, UKMTO triage). Benchmark quality vs Haiku. If viable, design as optional drop-in so the same code runs on MacBook, Pi, or EC2 with local inference where available and Haiku as fallback.

---

## License

Private use only.