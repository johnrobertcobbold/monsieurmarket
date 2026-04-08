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

## Telegram Commands

| Command | Description |
|---|---|
| `/straddle` | Open straddle with default notional + barrier |
| `/straddle 500` | Open straddle, 500€ per leg |
| `/straddle 500 4` | Open straddle, 500€ per leg, 4% barrier distance |
| `/status` | Show open positions + P&L (TODO) |
| `/pause` | Pause automated alerts |
| `/resume` | Resume automated alerts |
| `/help` | Show available commands |

### Straddle flow
1. Bot checks barrier availability at requested % distance
2. Bot fetches real EUR cost per leg via IG costs API
3. If cost > notional → rejects with suggestion e.g. `Try: /straddle 420 4`
4. Places call + put legs simultaneously (TODO) or sequentially
5. Verifies both positions opened via internal IG positions API
6. Sends confirmation with KO levels, distances, real costs

---

## IG Knockout Trading

### API notes
IG's public REST API does not support knockout order placement on demo accounts. MonsieurMarket uses IG's internal web platform API (reverse-engineered from browser network traffic):

| Action | Endpoint |
|---|---|
| Market details + quoteId + barrier levels | `{api_base}/nwtpdeal/v3/markets/details` |
| Place order | `{deal_base}/nwtpdeal/v3/orders/positions/otc` |
| Verify positions | `{deal_base}/nwtpdeal/wtp/orders/positions` |
| Cost estimate | `{deal_base}/dealing-gateway/costsandcharges/{acc}/open` |

Required header: `x-device-user-agent: vendor=IG Group | applicationType=ig | platform=WTP | version=...`

### Account setup
IG has separate accounts for CFD and knockout products:
- CFD account (default): standard instruments
- **Barrières et Options account**: required for knockout order placement

Set `IG_ACC_NUMBER` to the barrier account ID (e.g. `Z69ZML` on demo).

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
- [ ] assess_straddle() — query available barriers + costs at various distances, used by news alerts to suggest trades
- [ ] Simultaneous leg placement — place call + put in parallel threads to reduce slippage
- [ ] close_straddle() — close all open legs via Telegram /close command
- [ ] /status command — show open IG positions + P&L
- [ ] Scale-out alert — Telegram prompt when leg up 25%+ suggesting partial close
- [ ] Auto-close at 02:00 UTC — prevent overnight knockout exposure
- [ ] UKMTO monitor — scrape https://www.ukmto.org/ukmto-products/warnings/2026 for maritime warnings, plug into /signal (primary source, often precedes Bloomberg by 10-30 min)
- [ ] monitor.py split — scrapers.py, navigation.py, source_finder.py
- [ ] state.py — extract state management
- [ ] Enable run_poll() with token budget
- [ ] T+1 trade review — Sonnet post-mortem 24h after execution
- [ ] Sonnet correlation — explain price move using Bloomberg posts

### Medium-term
- [ ] IG live execution — fund live account, verify public API works, migrate from internal API if possible
- [ ] ATR-based dynamic thresholds
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