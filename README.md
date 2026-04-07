# 🎩 MonsieurMarket

Personal geopolitical trading intelligence bot. Monitors Brent crude price, Bloomberg liveblogs, news signals, Trump posts, and Polymarket whale trades. Alerts via Telegram.

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

### Whale intelligence
- Polymarket whale ledger tracks cumulative 24h flow per pseudonym
- Alerts at every $50k band crossed
- Three trigger types: single trade >$50k, single trader >$75k, net directional flow >$150k at 70%+ one-way

---

## Architecture

```
run.sh
  └── monsieur_market.py          <- brain, orchestrator, signal router
        │
        ├── MM Signal API :3456   <- /signal endpoint, all sources POST here
        │
        ├── bloomberg_camoufox/
        │     └── monitor.py      <- Camoufox browser, Flask :3457
        │                            NO internal timer — waits for MM /refresh calls
        │                            POSTs signals back to MM on new posts
        │
        ├── polymarket/
        │     └── polymarket.py   <- trades, ledger, websocket
        │
        ├── scheduled_event_watcher.py  <- event windows, trade logs
        └── config.py                   <- all configuration
```

### Signal flow

```
monitor.py      →  POST /signal {source: bloomberg, type: ready|post|error}
                →  MM receives, BloombergScheduler starts, Telegram sent

Brent spike     →  BloombergScheduler.trigger_now()
                →  GET /refresh on monitor
                →  monitor scrapes, POSTs new posts as signals
                →  MM sends to Telegram

Trump post      →  Haiku filter (no keyword pre-check)
                →  Sonnet + web search analysis
                →  Telegram alert
```

### Telegram responsibility
All Telegram messages originate from `monsieur_market.py` via `telegram_client.py`.
Monitor never sends Telegram directly — it signals MM, MM decides.

---

## Folder Structure

```
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
├── bloomberg_camoufox/           <- Bloomberg scraping layer
│   ├── monitor.py                <- Camoufox browser + Flask API :3457
│   └── setup.py                  <- one-time headful login
│   (gitignored: bloomberg_session.json, bloomberg_feed.json, monitor_state.json)
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
│   ├── find_epic.py              <- find IG instrument epics
│   ├── find_polymarket.py        <- search Polymarket markets
│   └── get_token_ids.py          <- get yes/no token IDs
│
└── venv/                         <- gitignored, Python virtualenv
```

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
pip install anthropic requests schedule python-dotenv flask camoufox playwright
python -m camoufox fetch
```

### 3. Create .env

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
TELEGRAM_BOT_TOKEN=7123456789:AAF-your-token-here
TELEGRAM_CHAT_ID=your-chat-id-here

IG_USERNAME=your-ig-username
IG_PASSWORD=your-ig-password
IG_API_KEY=your-ig-api-key
IG_ACC_NUMBER=your-ig-account-number
IG_BRENT_EPIC=CC.D.LCO.OPTCALL.IP
```

### 4. Bloomberg one-time setup

```bash
python bloomberg_camoufox/setup.py
# browser opens, log in to Bloomberg manually
# session saved automatically
```

### 5. Run

```bash
./run.sh             # headless Bloomberg (default)
./run.sh --visible   # watch Bloomberg browse in a real browser window
```

MM starts everything — no need to run monitor separately.

---

## Bloomberg Monitor

Runs a persistent Camoufox (Firefox) browser session that:

1. Opens Bloomberg homepage and scans for active liveblog (today's date in URL)
2. If no liveblog, clicks "See all latest", Haiku picks best In Focus tag (once per day, saved to disk)
3. Navigates to the liveblog or tag page
4. Waits for `/refresh` calls from MonsieurMarket — **no internal timer**
5. On scrape, POSTs each new post to MM `/signal` endpoint
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

MonsieurMarket exposes a `/signal` endpoint that all sources POST to:

```json
{
    "source": "bloomberg",
    "type":   "ready|no_source|post|error",
    "data":   { "title": "...", "timestamp_raw": "...", ... },
    "ts":     1744030000
}
```

This is the brain's single entry point. Future signal sources (Reuters, Twitter, etc.) plug in here without touching any other code.

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

### Near-term
- [ ] UKMTO monitor — scrape https://www.ukmto.org/ukmto-products/warnings/2026 for maritime warnings, plug into /signal (primary source, often precedes Bloomberg by 10-30 min)
- [ ] ig/ folder — extract Brent price watcher and IG REST into ig/ module
- [ ] monitor.py split — scrapers.py, navigation.py, source_finder.py
- [ ] state.py — extract state management
- [ ] Enable run_poll() with token budget
- [ ] T+1 trade review — Sonnet post-mortem 24h after execution
- [ ] IG live execution — real POST /positions/otc on IG demo then live
- [ ] Sonnet correlation — explain price move using Bloomberg posts

### Medium-term
- [ ] ATR-based dynamic thresholds
- [ ] Telegram command interface — /status /events /positions /close
- [ ] Whale reputation scoring
- [ ] Signal buffer in MM for run_poll() Bloomberg integration

### Later — Pi Migration
- [ ] Move to Raspberry Pi (always-on)
- [ ] Copy bloomberg_session.json via scp
- [ ] noVNC — view Pi browser session from Mac
- [ ] Claude Computer Use agent watching browser session

### Future
- [ ] Multi-asset streaming — Thales, TotalEnergies
- [ ] European Rearmament theme activation
- [ ] X/Twitter monitoring ($100/month API tier)
- [ ] Additional signal sources via /signal API (Reuters live, FT, etc.)
- [ ] Local model evaluation — assess Ollama (Gemma 4, Qwen, Phi) on M3 Air for zero-cost filtering (trump pre-filter, Bloomberg relevance, UKMTO triage). Benchmark quality vs Haiku. If viable, design as optional drop-in so the same code runs on MacBook, Pi, or EC2 with local inference where available and Haiku as fallback.

---

## License

Private use only.