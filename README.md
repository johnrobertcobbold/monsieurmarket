# 🎩 MonsieurMarket

Personal geopolitical trading intelligence bot. Monitors Brent crude price, news signals, Trump posts, and Polymarket whale trades. Alerts via Telegram.

Built on Anthropic Claude (Haiku for filtering, Sonnet for analysis).

---

## What It Does

### Real-time (sub-second)
- **Brent price stream** via IG Lightstreamer — fires on 5min/10min/day-open moves
- **Polymarket websocket** — whale trades ($10k+) tracked in real-time, instant alert >$100k

### Near real-time (<2 min)
- **Trump watcher** — polls trumpstruth.org/feed every 2 min, Haiku filters, Sonnet analyses relevant posts immediately

### Scheduled event monitoring
- **Event watcher** — hot-reloadable `scheduled_events.json` defines known events (NFP, OPEC, speeches)
- Arms at event start, watches for price move, confirms over N minutes, fires simulated order
- **RSS fetch at open** — fetches curated RSS feeds X minutes after event opens, filters by timestamp and keywords, sends fresh headlines to Telegram
- **Post-execution web search** — Haiku web search fires X minutes after simulated order, strict prompt guard to only find post-event articles
- **Trade log** — every stage (ARMED, TRIGGERED, FADED, CONFIRMED, SIMULATED_ORDER, RSS_AT_OPEN, WEB_SEARCH, WINDOW_CLOSED) logged to `trades/YYYY-MM-DD_eventid.md`

### Whale intelligence
- Polymarket whale ledger tracks cumulative 24h flow per pseudonym
- Alerts at every $50k band crossed
- Three trigger types: single trade >$50k, single trader >$75k this cycle, net directional flow >$150k at 70%+ one-way

---

## Files

| File | Purpose |
|---|---|
| `monsieur_market.py` | Main script |
| `scheduled_event_watcher.py` | Event monitoring, RSS fetch, web search, trade logging |
| `scheduled_events.json` | Hot-reloadable event definitions |
| `rss_sources.json` | Curated RSS feed list with timezone metadata |
| `polymarket_markets.json` | Polymarket markets to watch |
| `trades/` | Auto-generated markdown trade logs per event |
| `.env` | Your secrets — never commit this |
| `monsieur_market_state.json` | Auto-generated dedup state — safe to delete to reset |
| `monsieur_market.log` | Auto-generated log file |

---

## Setup

### 1. Install dependencies

```bash
pip install anthropic requests schedule python-dotenv
```

### 2. Create your `.env` file

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
TELEGRAM_BOT_TOKEN=7123456789:AAF-your-token-here
TELEGRAM_CHAT_ID=your-chat-id-here

# IG Markets (for Brent price stream)
IG_USERNAME=your-ig-username
IG_PASSWORD=your-ig-password
IG_API_KEY=your-ig-api-key
IG_ACC_NUMBER=your-ig-account-number
IG_BRENT_EPIC=CC.D.LCO.OPTCALL.IP
```

### 3. Run

```bash
python monsieur_market.py
```

---

## Scheduled Events (`scheduled_events.json`)

Hot-reloadable — edit and save, script picks up changes on next Brent tick. No restart needed.

### Event fields

| Field | Required | Description |
|---|---|---|
| `id` | ✅ | Unique identifier, used for trade log filename |
| `name` | ✅ | Human-readable label |
| `active` | ✅ | `true` to monitor, `false` to skip |
| `watch_from_utc` | ✅ | Window start time in UTC (`"2026-04-03 12:30:00"`) |
| `watch_duration_min` | ✅ | How long to watch for a move |
| `trigger_pct` | ✅ | % move from reference price to trigger |
| `confirm_wait_min` | ✅ | Minutes move must hold to confirm |
| `action` | ✅ | `buy_ig_barrier`, `buy_ig_call_barrier`, or `buy_ig_put_barrier` |
| `rss_at_open` | ○ | `true` to fetch RSS after window opens |
| `rss_at_open_delay_min` | ○ | Minutes after arm to fetch RSS (default 5) |
| `rss_keywords` | ○ | Keywords to filter RSS headlines |
| `web_search` | ○ | `true` to run Haiku web search after execution |
| `web_search_query` | ○ | Search query string |
| `web_search_delay_min` | ○ | Minutes after execution to search (default 15) |
| `log_trade` | ○ | `true` to write trade log markdown (recommended) |
| `notes` | ○ | Human notes, written to trade log header |

### Example event

```json
{
  "id": "nfp_20260403",
  "name": "US NFP April 3",
  "active": true,
  "watch_from_utc": "2026-04-03 12:30:00",
  "watch_duration_min": 60,
  "trigger_pct": 1.5,
  "confirm_wait_min": 10,
  "action": "buy_ig_barrier",
  "rss_at_open": true,
  "rss_at_open_delay_min": 5,
  "rss_keywords": ["non-farm", "payrolls", "jobs", "employment", "oil", "brent"],
  "web_search": true,
  "web_search_query": "NFP non-farm payrolls April 2026 result oil reaction",
  "web_search_delay_min": 15,
  "log_trade": true,
  "notes": "Non-farm payrolls. Weak NFP = recession fear = Brent down."
}
```

### Event timing calibration

| Event type | trigger_pct | confirm_wait_min | rss_delay | web_search_delay |
|---|---|---|---|---|
| Trump speech | 2.0% | 15 min | — | — |
| EU open | 1.0% | 5 min | 3 min | — |
| NFP | 1.5% | 10 min | 5 min | 15 min |
| OPEC | 1.5% | 10 min | 10 min | 20 min |
| EIA inventory | 1.0% | 5 min | 5 min | 10 min |

---

## RSS Sources (`rss_sources.json`)

Curated list of working feeds verified against real timestamps. Edit to add/remove/disable.

| Field | Description |
|---|---|
| `name` | Display name used in Telegram alerts |
| `url` | RSS feed URL |
| `timezone` | Feed's timestamp timezone (`"UTC"`, `"UTC-5"` etc.) |
| `active` | `true` to include in fetches |
| `notes` | Human notes |

Current active sources: Bloomberg Markets, Bloomberg Politics, Al Jazeera, BBC World, OilPrice.

**Important**: Reuters and AP News RSS feeds are dead as of 2026. Do not add them.

---

## Trade Logs (`trades/`)

Every active event with `log_trade: true` generates a markdown file:

```
trades/2026-04-03_nfp_20260403.md
```

Contains a header table with event metadata, followed by a timestamped event log:

| Stage | Meaning |
|---|---|
| `ARMED` | Window opened, reference price snapped |
| `TRIGGERED` | Price moved ±trigger_pct% from reference |
| `FADED` | Move retreated below threshold during confirm wait |
| `CONFIRMED` | Move held through confirm_wait_min |
| `SIMULATED_ORDER` | Action fired (currently simulation only) |
| `RSS_AT_OPEN` | RSS headlines fetched and sent |
| `WEB_SEARCH` | Post-execution web search result |
| `WINDOW_CLOSED` | Window expired without trigger |

These files are designed to be reviewed post-event and eventually fed to a model for automated post-mortem analysis.

---

## Polymarket Markets (`polymarket_markets.json`)

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
python3 -c "
import requests, json
d = requests.get('https://gamma-api.polymarket.com/events/YOUR_EVENT_ID').json()
[print(m['question'], json.loads(m['clobTokenIds'])) for m in d['markets']]
"
```

---

## Alert Levels

| Emoji | Score | Meaning |
|---|---|---|
| 📰 FYI | 6–7 | Relevant but not urgent |
| ⚠️ WATCH | 7–9 | Material signal, review positions |
| 🚨 ACT NOW | 9–10 | High conviction, immediate attention |
| 🚨🚨 ESCALATION | override | Yanbu / Saudi infrastructure — wakes you at 3am |

---

## Weekly Event Calendar

Ask Claude to generate `scheduled_events.json` for the coming week:

> "Generate my MonsieurMarket scheduled_events.json for next week — include NFP, EIA inventory, any OPEC meetings, Fed speakers, and known geopolitical events relevant to Brent crude."

Claude will search for current event dates and output a ready-to-paste JSON with calibrated trigger/confirm settings per event type.

---

## Roadmap

### Near-term


- [ ]  Git repo — initialise, .gitignore in place, first commit with all current files
- [ ] **`trade_logger.py`** — extract trade logging into a shared module so all components (event watcher, price alerts, whale alerts) can write to the same trade file format
- [ ] **`rss_fetcher.py`** — extract RSS fetching into a shared module callable from any component
- [ ] **T+1 trade review** — 24 hours after execution, Sonnet reads the full trade log, fetches what Brent actually did, and writes a post-mortem section to the trade file. Feeds back into calibration.
- [ ] **IG live execution** — swap simulated Telegram alert for real `POST /positions/otc` on IG demo account, then live

### Medium-term

- [ ] **ATR-based dynamic thresholds** — replace fixed `trigger_pct_from_open` with multiples of 14-day ATR. Noisy days won't false-trigger; quiet days won't miss real moves. Full spec in legacy README.
- [ ] **Telegram command interface** — `/status`, `/events`, `/positions`, `/close <event_id>`
- [ ] **Whale reputation scoring** — track which pseudonyms position correctly before price moves. Score 0-10. High-score $15k whale > noise $50k whale.

### Later

- [ ] Multi-asset streaming — Thales, TotalEnergies alongside Brent
- [ ] European Rearmament theme activation
- [ ] X/Twitter monitoring ($100/month API tier)

---

## .gitignore

```
.env
monsieur_market_state.json
monsieur_market.log
trades/
```

---

## License

Private use only.