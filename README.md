# 🎩 MonsieurMarket

Personal multi-asset geopolitical and macro trading intelligence system.

MonsieurMarket monitors market prices, news flow, political signals, scheduled events, and whale activity; alerts via Telegram; can execute IG trades automatically; and is evolving toward its own IG-native charting, alerting, and remote monitoring layer.

Built on Anthropic Claude (Haiku for filtering, Sonnet for analysis) and Camoufox for Bloomberg scraping.

---

## Overview

MonsieurMarket is a personal trading workstation backend for event-driven trading.

It combines four layers:

- **Signal intelligence** — Bloomberg liveblogs, political posts, Polymarket flow, and scheduled macro/geopolitical events
- **Market data** — live IG streaming data and historical backfill
- **Execution** — IG trade placement, position management, and stop logic
- **Visualization & control** — Telegram alerts, local charts, future chart snapshots, and a remote dashboard

The system is **not Brent-only**. Brent is simply the current primary market and most advanced workflow.

The architecture is intended to support other assets and themes over time, including:

- equity indices such as **CAC 40**
- defense and geopolitical equities
- FX
- other commodities
- event-driven thematic trades

### Current focus

The strongest implementation today is around:

- IG streaming market data
- Brent-related geopolitical monitoring
- IG knockout options and straddles
- Telegram-driven alerts and execution
- Bloomberg, Trump, Polymarket, and scheduled event monitoring

Long term, the goal is broader:

> **One system that can ingest signals, watch markets, alert, chart, and execute across multiple assets.**

---

## Core principles

### Single source of truth
IG market data should become the source of truth for:

- price monitoring
- charting
- alert triggering
- position visualization
- future stop-loss automation

This reduces dependence on external charting tools and avoids feed mismatch between charting and execution.

### Local-first, cloud-ready
Development starts locally on the MacBook for fast iteration and debugging.

The always-on deployment target is **AWS EC2**.

### TradingView optional, not required
TradingView can still be used for familiar chart reading, but it is no longer a dependency for:

- live monitoring
- alerts
- chart display
- position overlays
- future stop-management visuals

The system is evolving toward its own charting and alerting layer built from IG data.

---

## What it does

### Real-time / sub-second
- **IG price stream** via Lightstreamer — current implementation monitors Brent and can be extended to any subscribed instrument
- **Polymarket websocket** — whale trades tracked in real time, with alerts on large prints and directional flow
- **Bloomberg on-demand refresh** — triggered automatically on significant price action or scheduled checks

### Near real-time
- **Trump watcher** — polls `trumpstruth.org/feed` every 2 minutes; Haiku filters relevant posts and Sonnet analyses important ones
- **Bloomberg monitor** — scrapes Bloomberg liveblogs or the best “In Focus” tag on a market-hours-aware schedule
- **News-triggered analysis** — Sonnet plus web search for material signals

### Scheduled event monitoring
- **Event watcher** — hot-reloadable `scheduled_events.json` defines known events such as NFP, OPEC, speeches, and geopolitical windows
- Arms at event start, monitors price response, confirms over N minutes, and can trigger simulated or live action
- **RSS fetch at open** — fetches curated RSS feeds after event open
- **Post-trigger analysis** — optional Haiku/Sonnet workflow
- **Trade/event log** — stages written to markdown logs in `data/trades/`

### Automated trading (IG)
- **Telegram command** — `/straddle [notional] [barrier_%]` opens a balanced IG knockout straddle
- **Smart pre-checks** — validates barrier availability and real cost before placing
- **Both legs verified** — confirms positions via IG before reporting success
- **Demo and live support** — demo-tested, live-ready when funded

### Visualization & monitoring
- **Own charting stack** based on IG tick data
- **Local chart on MacBook** to validate TradingView replacement before subscription expiry
- **Manual alert levels** overlaid directly on the chart

Planned overlays include:

- open positions
- stop loss and take profit
- bot actions
- event markers
- Telegram chart snapshots
- secure remote dashboard access

### Whale intelligence
- Polymarket whale ledger tracks cumulative 24-hour flow per pseudonym
- Alerts at every $50k band crossed
- Three trigger types:
  - single trade over $50k
  - single trader over $75k
  - net directional flow over $150k at 70%+ one-way

---

## Architecture

```text
run.sh
  └── monsieur_market.py              <- brain, orchestrator, signal router
        │
        ├── MM Signal API :3456       <- /signal endpoint, all sources POST here
        │
        ├── bloomberg/
        │     └── monitor.py          <- Camoufox browser, Flask :3457
        │                                no internal timer — waits for MM /refresh calls
        │                                POSTs signals back to MM on new posts
        │
        ├── ig/
        │     ├── service.py          <- IG session management
        │     ├── streamer.py         <- IG streaming subscriptions, tick callbacks
        │     ├── straddle.py         <- knockout straddle execution (demo + live)
        │     ├── positions.py        <- position tracking / management
        │     └── history.py          <- historical price backfill (planned)
        │
        ├── market_data/              <- local market data layer (planned)
        │     ├── store.py            <- tick / candle persistence
        │     ├── resample.py         <- OHLC generation from ticks
        │     └── alerts.py           <- bot-side price / level monitoring
        │
        ├── charting/                 <- local + remote charting layer (planned)
        │     ├── server.py           <- live chart app / dashboard
        │     ├── render.py           <- chart snapshot generation
        │     └── overlays.py         <- levels, positions, stops, markers
        │
        ├── polymarket/
        │     └── polymarket.py       <- trades, ledger, websocket
        │
        ├── scheduled_event_watcher.py <- event windows, trigger logic, logs
        └── config.py                  <- all configuration
```

### Signal flow

```text
monitor.py      →  POST /signal {source: bloomberg, type: ready|post|error}
                →  MM receives, BloombergScheduler starts, Telegram sent

Price spike     →  BloombergScheduler.trigger_now()
                →  GET /refresh on monitor
                →  monitor scrapes, POSTs new posts as signals
                →  MM sends to Telegram

Trump post      →  Haiku filter
                →  Sonnet + web search analysis
                →  Telegram alert

Telegram cmd    →  /straddle 500 4
                →  ig/straddle.py open_straddle()
                →  barrier check + cost check
                →  place call + put via IG internal API
                →  verify both legs in positions
                →  Telegram confirmation

Future charting →  IG tick stream stored locally
                →  OHLC candles built from ticks
                →  chart rendered locally / remotely
                →  manual levels + positions + stops overlaid
                →  Telegram /chart snapshot and dashboard view
```

---

## Telegram

All Telegram messages originate from `monsieur_market.py` via `telegram_client.py`.

The Bloomberg monitor never sends Telegram messages directly. It signals MM, and MM decides what to send.

Telegram is both:

- an **alert destination**
- and, increasingly, a **control and remote-view interface**

### Current commands

| Command | Description |
|---|---|
| `/straddle` | Open straddle with default notional and barrier |
| `/straddle 500` | Open straddle, 500€ per leg |
| `/straddle 500 4` | Open straddle, 500€ per leg, 4% barrier distance |
| `/cut_call` | Close call leg only |
| `/cut_put` | Close put leg only |
| `/close` | Close both legs |
| `/status` | Show open positions + P&L (TODO) |
| `/history` | Show last 5 trades (TODO) |
| `/call 500` | Open call only — directional bet (TODO) |
| `/put 500` | Open put only — directional bet (TODO) |
| `/pause` | Pause automated alerts |
| `/resume` | Resume automated alerts |
| `/help` | Show available commands |

### Planned Telegram extensions

| Command | Description |
|---|---|
| `/chart` | Send current chart snapshot |
| `/chart 5m` | Send chart snapshot on chosen timeframe |
| `/levels` | Show active manual or bot-side alert levels |
| `/position` | Show current position with stop and target |
| `/setlevel` | Add a local price alert level |
| `/setstop` | Set or adjust stop logic |

### Straddle flow
1. Bot checks barrier availability at the requested distance
2. Bot fetches real EUR cost per leg via the IG costs API
3. If cost exceeds notional, it rejects with a suggestion
4. Places call and put sequentially
5. Verifies both positions via the IG positions API
6. Sends confirmation with KO levels, distances, and real costs

---

## Folder structure

```text
monsieurmarket/
├── .env                          <- secrets, never commit
├── config.py                     <- all configuration + themes
├── monsieur_market.py            <- main entry point + brain
├── telegram_client.py            <- single place for all Telegram messaging
├── scheduled_event_watcher.py    <- event monitoring
├── scheduled_events.json         <- hot-reloadable event definitions
├── rss_sources.json              <- curated RSS feeds
├── run.sh                        <- start script
│
├── bloomberg/                    <- Bloomberg scraping layer
│   ├── monitor.py                <- Camoufox browser + Flask API :3457
│   └── setup.py                  <- one-time headful login
│   (gitignored: bloomberg_session.json, bloomberg_feed.json, monitor_state.json)
│
├── ig/                           <- IG trading + market access
│   ├── __init__.py               <- exports: get_ig_service, open_straddle, etc.
│   ├── service.py                <- IG session, account switch, headers
│   ├── streamer.py               <- Lightstreamer subscriptions
│   ├── straddle.py               <- knockout straddle execution (demo + live)
│   ├── positions.py              <- position tracking / management
│   └── history.py                <- historical price retrieval (planned)
│
├── market_data/                  <- local price database layer (planned)
│   ├── store.py                  <- tick / candle persistence
│   ├── resample.py               <- build OHLC from ticks
│   └── alerts.py                 <- local price-level alert engine
│
├── charting/                     <- chart UI + snapshot generation (planned)
│   ├── server.py                 <- local or remote chart dashboard
│   ├── render.py                 <- generate chart images for Telegram
│   └── overlays.py               <- levels, positions, stops, annotations
│
├── polymarket/                   <- Polymarket intelligence
│   ├── polymarket.py             <- trades, ledger, websocket
│   ├── check_whale_portfolio.py  <- portfolio analysis
│   (gitignored: polymarket_markets.json, market_relevance.json)
│
├── data/                         <- gitignored, runtime state
│   ├── monsieur_market_state.json
│   ├── monsieur_market.log
│   ├── market_data/              <- local tick/candle store
│   └── trades/                   <- markdown trade logs per event
│
├── scripts/                      <- one-off setup utilities
│   ├── find_epic.py              <- IG API discovery + order testing
│   ├── find_polymarket.py        <- search Polymarket markets
│   └── get_token_ids.py          <- get yes/no token IDs
│
└── venv/                         <- gitignored, Python virtualenv
```

---

## Environments

### Local development
Primary development environment is the **MacBook**.

Used for:

- strategy iteration
- stream debugging
- chart prototyping
- local alert testing
- visual feasibility testing before TradingView expiry

### Always-on deployment
Target deployment environment is **AWS EC2**.

Used for:

- 24/7 monitoring
- scheduled event handling
- Telegram alerting
- signal ingestion
- chart/dashboard hosting
- future live execution

### Trading accounts
Supports both:

- **IG Demo**
- **IG Live**

with separate credentials and accounts.

---

## Setup

### 1. Clone and create virtualenv

```bash
git clone <your-repo>
cd monsieurmarket
/opt/homebrew/bin/python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install anthropic requests schedule python-dotenv flask camoufox playwright trading-ig pandas munch tenacity
python -m camoufox fetch
```

Additional charting and dashboard dependencies will be added as the chart layer is implemented.

### 3. Create `.env`

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
TELEGRAM_BOT_TOKEN=7123456789:AAF-your-token-here
TELEGRAM_CHAT_ID=your-chat-id-here

# IG Demo
IG_USERNAME=your-ig-username
IG_PASSWORD=your-ig-password
IG_API_KEY=your-ig-api-key
IG_ACC_NUMBER=Z69ZML

# IG Live (optional)
IG_USERNAME_LIVE=your-live-username
IG_PASSWORD_LIVE=your-live-password
IG_API_KEY_LIVE=your-live-api-key
IG_ACC_NUMBER_LIVE=your-live-barrier-account
```

### 4. Bloomberg one-time setup

```bash
python bloomberg/setup.py
# browser opens, log in to Bloomberg manually
# session saved automatically
```

### 5. Run

```bash
./run.sh             # headless Bloomberg (default)
./run.sh --visible   # watch Bloomberg browse in a real browser window
```

MM starts everything — no need to run the monitor separately.

---

## Bloomberg monitor

Runs a persistent Camoufox (Firefox) browser session that:

1. Opens Bloomberg homepage and scans for an active liveblog
2. If no liveblog exists, clicks “See all latest” and lets Haiku pick the best In Focus tag
3. Navigates to the chosen liveblog or tag page
4. Waits for `/refresh` calls from MonsieurMarket — **no internal timer**
5. On scrape, POSTs each new post to MM `/signal`
6. Serves via Flask API on `:3457`

MM owns the refresh schedule:

- 08:00–22:00 UTC: every ~5 min (with jitter)
- 06:00–08:00 UTC: every ~15 min
- 22:00–06:00 UTC: every ~60 min

### Monitor API

| Endpoint | Description |
|---|---|
| `GET /health` | Status, source type, post count |
| `GET /posts?since=ts` | Posts newer than timestamp |
| `GET /latest` | Most recent post |
| `GET /refresh` | Immediate scrape triggered by MM |

---

## MM Signal API (`:3456`)

MonsieurMarket exposes a `/signal` endpoint that all sources POST to:

```json
{
  "source": "bloomberg",
  "type": "ready|no_source|post|error",
  "data": { "title": "...", "timestamp_raw": "...", "...": "..." },
  "ts": 1744030000
}
```

This is the brain’s single entry point.

Future signal sources can plug into the same API without changing the rest of the system.

Potential future sources include:

- Reuters
- X / Twitter
- UKMTO
- other RSS or direct feeds
- custom asset-specific scanners

---

## IG trading

### API notes
IG’s public REST API does not fully support all knockout workflows on demo accounts. MonsieurMarket uses IG’s internal web platform API for certain actions.

| Action | Endpoint |
|---|---|
| Market details + quoteId + barrier levels | `{api_base}/nwtpdeal/v3/markets/details` |
| Place order | `{deal_base}/nwtpdeal/v3/orders/positions/otc` |
| Verify positions | `{deal_base}/nwtpdeal/wtp/orders/positions` |
| Cost estimate | `{deal_base}/dealing-gateway/costsandcharges/{acc}/open` |

Required header:

```text
x-device-user-agent: vendor=IG Group | applicationType=ig | platform=WTP | version=...
```

### Account setup
IG has separate accounts for CFD and knockout products:

- CFD account — standard instruments
- **Barrières et Options account** — required for knockout order placement

Set `IG_ACC_NUMBER` to the barrier account ID.

### Cost model
Knockout straddles can be expensive during high volatility, so pre-trade validation is critical.

Current emphasis is not on blind automation, but on:

- cost-aware execution
- barrier availability validation
- post-placement verification

---

## Market data & charting direction

This is the next major capability area.

### Goal
Build an IG-native charting and alerting layer so the bot can:

- display live charts from collected IG data
- overlay manual alert levels
- overlay current positions
- overlay stop loss and take profit
- mark bot actions visually
- send chart snapshots through Telegram
- expose a secure remote dashboard

### Planned flow

```text
IG Lightstreamer ticks
    → local tick store
    → OHLC candle builder
    → live chart
    → overlay levels / positions / stops
    → Telegram screenshot or remote dashboard
```

### Why this matters
This replaces reliance on TradingView for operational monitoring and gives a single visual source of truth for:

- the exact contract being traded
- the exact feed used for execution logic
- future automated stop updates
- auditability of bot decisions

### Historical recovery
If data collection misses a window, the system should use IG historical retrieval to backfill missing periods where possible.

---

## Scheduled events (`scheduled_events.json`)

Hot-reloadable — edit and save, picks up on the next relevant price tick. No restart needed.

### Event fields

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique identifier |
| `name` | yes | Human-readable label |
| `active` | yes | `true` to monitor |
| `watch_from_utc` | yes | Window start UTC |
| `watch_duration_min` | yes | How long to watch |
| `trigger_pct` | yes | % move to trigger |
| `confirm_wait_min` | yes | Minutes move must hold |
| `action` | yes | Action to run |
| `rss_at_open` | no | Fetch RSS after window opens |
| `web_search` | no | Run search after execution |
| `log_trade` | no | Write markdown trade log |

### Event timing calibration

| Event type | trigger_pct | confirm_wait_min |
|---|---|---|
| EU open | 1.0% | 5 min |
| NFP | 1.5% | 10 min |
| OPEC | 1.5% | 10 min |
| EIA inventory | 1.0% | 5 min |
| Trump speech | 2.0% | 15 min |

These are heuristics, not fixed truths, and should evolve by asset and regime.

---

## Polymarket markets (`polymarket/polymarket_markets.json`)

```json
[
  {
    "conditionId": "0xabc...",
    "label": "US forces enter Iran by April 30?",
    "yes_token": "0x123...",
    "no_token": "0x456...",
    "active": true
  }
]
```

To find token IDs for a new market:

```bash
python scripts/get_token_ids.py
```

---

## Alert levels

| Label | Score | Meaning |
|---|---|---|
| FYI | 6–7 | Relevant but not urgent |
| WATCH | 7–9 | Material signal, review positions |
| ACT NOW | 9–10 | High conviction, immediate attention |
| ESCALATION | override | Exceptional geopolitical urgency |

---

## AI usage

| Component | Model | When | Approx cost |
|---|---|---|---|
| Bloomberg tag picker | Haiku | Once per day | ~$0.00 |
| Trump filter | Haiku | Every Trump post | ~$0.00 |
| Trump analysis | Sonnet + web search | Per relevant post | ~$0.02 |
| Signal analysis | Sonnet + web search | When signals present | ~$0.05 |
| Weekly digest | Sonnet + web search | Sunday 9am | ~$0.05 |

---

## Weekly event calendar

Ask Claude to generate `scheduled_events.json` for the coming week:

> “Generate my MonsieurMarket scheduled_events.json for next week — include NFP, EIA inventory, any OPEC meetings, Fed speakers, and known geopolitical events relevant to my monitored markets.”

Claude can output ready-to-paste JSON with calibrated settings.

---

## Roadmap

### Priority 1 — Charting feasibility before TradingView expiry
- [ ] Stream subscribed IG ticks reliably on MacBook
- [ ] Persist raw tick data locally
- [ ] Build OHLC candles from collected ticks
- [ ] Display first live local chart from own data
- [ ] Overlay manual alert levels on chart
- [ ] Confirm workflow is usable before TradingView expiry on April 24

### Priority 2 — Bot-side monitoring and visual overlays
- [ ] Local price-level alerts independent of TradingView
- [ ] Show open position overlays on chart
- [ ] Show stop loss / target overlays
- [ ] Event markers and trigger markers on chart
- [ ] Log every bot action as visual annotation + event record

### Priority 3 — Telegram charting and remote monitoring
- [ ] `/chart` Telegram command
- [ ] Render current chart snapshot with levels and positions
- [ ] `/position` and richer `/status`
- [ ] Authenticated remote page for live monitoring
- [ ] Secure remote access to chart/dashboard from phone or laptop

### Priority 4 — IG execution improvements
- [ ] `close_straddle()` — close both legs
- [ ] `cut_leg()` — close one leg
- [ ] Stop-loss management — set / move stops automatically
- [ ] `/status` — open positions + P&L
- [ ] `assess_straddle()` — barrier + cost summary appended to alerts
- [ ] `/call` and `/put` — directional single-leg trades
- [ ] Trade history — store locally + fetch from IG history
- [ ] Simultaneous leg placement to reduce slippage

### Priority 5 — Deployment and resilience
- [ ] EC2 deployment for always-on runtime
- [ ] Systemd / process supervision
- [ ] Persistent database / storage strategy
- [ ] Historical backfill after outages
- [ ] Secure dashboard hosting
- [ ] Remote log inspection and health checks

### Priority 6 — Signal quality and expansion
- [ ] UKMTO monitor
- [ ] Sonnet correlation — explain price move using live signals
- [ ] Enable broader polling with token-budget controls
- [ ] Signal buffer in MM for richer cross-source synthesis

### Future
- [ ] Multi-asset streaming beyond the current Brent workflow
- [ ] CAC 40 / European geopolitical equity monitoring
- [ ] Defense / energy / macro thematic baskets
- [ ] Additional signal sources via `/signal` API
- [ ] X/Twitter monitoring
- [ ] Optional local-model evaluation (Ollama) as a drop-in low-cost filter layer

---

## License

Private use only.
