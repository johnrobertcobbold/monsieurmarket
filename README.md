# 🎩 MonsieurMarket

Personal geopolitical trading intelligence bot. Monitors Brent crude price, Bloomberg liveblogs, news signals, Trump posts, and Polymarket whale trades. Alerts via Telegram.

Built on Anthropic Claude (Haiku for filtering, Sonnet for analysis) + Camoufox for Bloomberg scraping.

---

## What It Does

### Real-time (sub-second)
- **Brent price stream** via IG Lightstreamer — fires on 5min/10min/day-open moves
- **Polymarket websocket** — whale trades ($10k+) tracked in real-time, instant alert >$100k
- **Bloomberg on-demand** — triggered automatically on Brent price alerts, fetches fresh context

### Near real-time (less than 2 min)
- **Trump watcher** — polls trumpstruth.org/feed every 2 min, Haiku filters, Sonnet analyses relevant posts immediately
- **Bloomberg monitor** — scrapes Bloomberg homepage and /latest/war-with-iran every 15 min via Camoufox

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
monsieur_market.py              <- orchestrator
        |
        +-- bloomberg_watcher.py        <- reads Bloomberg API
        |       +-- bloomberg_camoufox/
        |               +-- monitor.py  <- Camoufox browser, Flask :3457
        |               +-- setup.py    <- one-time Bloomberg auth
        |
        +-- polymarket/
        |       +-- polymarket.py       <- trades, ledger, websocket
        |
        +-- scheduled_event_watcher.py  <- event windows, trade logs
        +-- config.py                   <- all configuration
        +-- bloomberg_watcher.py        <- MonsieurMarket <-> Bloomberg bridge
```

---

## Folder Structure

```
monsieurmarket/
+-- .env                          <- secrets, never commit
+-- config.py                     <- all configuration + themes
+-- monsieur_market.py            <- main entry point
+-- bloomberg_watcher.py          <- Bloomberg connector
+-- scheduled_event_watcher.py    <- event monitoring
+-- scheduled_events.json         <- hot-reloadable event definitions
+-- rss_sources.json              <- curated RSS feeds
+-- run.sh                        <- start script
|
+-- bloomberg_camoufox/           <- Bloomberg scraping layer
|   +-- monitor.py                <- Camoufox browser + Flask API :3457
|   +-- setup.py                  <- one-time headful login
|   +-- bloomberg_session.json    <- gitignored, saved cookies
|   +-- bloomberg_feed.json       <- gitignored, scraped posts
|   +-- monitor_state.json        <- gitignored, saved tag state
|
+-- polymarket/                   <- Polymarket intelligence
|   +-- polymarket.py             <- trades, ledger, websocket
|   +-- check_whale_portfolio.py  <- portfolio analysis
|   +-- polymarket_markets.json   <- gitignored, watched markets
|   +-- market_relevance.json     <- gitignored, relevance cache
|
+-- data/                         <- gitignored, runtime state
|   +-- monsieur_market_state.json
|   +-- monsieur_market.log
|   +-- trades/                   <- markdown trade logs per event
|
+-- scripts/                      <- one-off setup utilities
|   +-- find_epic.py              <- find IG instrument epics
|   +-- find_polymarket.py        <- search Polymarket markets
|   +-- get_token_ids.py          <- get yes/no token IDs
|
+-- venv/                         <- gitignored, Python virtualenv
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
# terminal 1 - Bloomberg monitor
python bloomberg_camoufox/monitor.py

# terminal 2 - main bot
python monsieur_market.py
```

---

## Bloomberg Monitor

Runs a persistent Camoufox (Firefox) browser session that:

1. Opens Bloomberg homepage and scans for active liveblog
2. If no liveblog, clicks "See all latest", Haiku picks best In Focus tag (once per day, saved to disk)
3. Navigates to /latest/war-with-iran or equivalent
4. Scrapes headlines every 15 min with jitter
5. Serves via Flask API on :3457

Run modes:

```bash
python bloomberg_camoufox/monitor.py            # headless
python bloomberg_camoufox/monitor.py --visible  # watch it work
```

API endpoints:

| Endpoint | Description |
|---|---|
| GET /health | Status, source type, post count, next retry |
| GET /posts?since=ts | Posts newer than timestamp |
| GET /latest | Most recent post |
| GET /refresh | Immediate scrape, called on price spike |

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
| Trump filter | Haiku | Per relevant post | ~$0.00 |
| Trump analysis | Sonnet + web search | Per relevant post | ~$0.02 |
| RSS/Bloomberg filter | Haiku | Per poll cycle | ~$0.00 |
| Signal analysis | Sonnet + web search | When signals present | ~$0.05 |
| Weekly digest | Sonnet + web search | Sunday 9am | ~$0.05 |

---

## Weekly Event Calendar

Ask Claude to generate scheduled_events.json for the coming week:

"Generate my MonsieurMarket scheduled_events.json for next week — include NFP, EIA inventory, any OPEC meetings, Fed speakers, and known geopolitical events relevant to Brent crude."

Claude will search for current event dates and output a ready-to-paste JSON with calibrated settings per event type.

---

## Roadmap

### Done
- [x] Git repo initialised
- [x] Camoufox vs PerimeterX working
- [x] Bloomberg monitor scraping liveblog and /latest/war-with-iran
- [x] Bloomberg watcher plugged into MonsieurMarket
- [x] Bloomberg on-demand refresh on price spike
- [x] Polymarket extracted to polymarket/ module
- [x] config.py extracted
- [x] data/ folder for runtime state

### Near-term
- [ ] state.py — extract state management
- [ ] sources/ — extract RSS, Trump, IG into modules
- [ ] analysis/ — extract Haiku/Sonnet into modules
- [ ] trade_logger.py — shared trade log module
- [ ] T+1 trade review — Sonnet post-mortem 24h after execution
- [ ] IG live execution — real POST /positions/otc on IG demo then live
- [ ] Enable run_poll() with token budget

### Medium-term
- [ ] ATR-based dynamic thresholds
- [ ] Telegram command interface — /status /events /positions /close
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

---

## .gitignore

```
.env
data/
venv/
polymarket/market_relevance.json
polymarket/polymarket_markets.json
bloomberg_camoufox/bloomberg_session.json
bloomberg_camoufox/bloomberg_feed.json
bloomberg_camoufox/monitor_state.json
bloomberg_camoufox/chrome_profile/
__pycache__/
*.pyc
```

---

## License

Private use only.
