# Stock Recommendations & Automated Trading — Design Document

> A full-stack technical-analysis and automated paper-trading platform.
> FastAPI + SQLAlchemy backend · React (CDN) frontend · Alpaca broker · Neon Postgres · Deployed on Google Cloud Run.

---

## 1. Mission & Scope

The system ingests a user-managed watchlist, runs multi-timeframe technical
analysis across seven timeframes (5m → 1mo), generates directional signals
with explicit entry / stop-loss / three targets, and routes those signals
into a bracket-ordered paper trading account at Alpaca. Capital is
deployed across both stocks and options (long puts synthesised from bear
theses on tickers with no BUY). All trades are tracked in a first-party
ledger that supports trailing stops, partial profit-taking, stale-trade
recycling, post-mortems, news-context lookup, and correlation/sector caps.

Real-time news is ingested separately (Alpaca News → VADER sentiment →
`news_events` table) for observability and trade-vs-news correlation
analysis; the auto-trader does not yet consume it directly.

The platform runs as a single FastAPI process that serves the REST API, a
WebSocket quote stream, the static React SPA, and APScheduler background
jobs. It is publicly deployed on Cloud Run with HTTPS and a shared-secret
auth layer.

---

## 2. System Architecture

```
                              ┌─────────────────────────────┐
                              │  Browser (React SPA)        │
                              │   · Lightweight-Charts      │
                              │   · Tailwind CDN            │
                              │   · CSS-var theming (dark/  │
                              │     light, localStorage)    │
                              │   · Login screen + token    │
                              └──────────┬──────────────────┘
                                         │ REST + WebSocket
                                         │ X-API-Key header /
                                         │ ?token= on /ws/quotes
                                         ▼
                      ┌────────────────────────────────────┐
                      │  Cloud Run service (stockrecs)     │
                      │  FastAPI + uvicorn on :8080        │
                      │  min-instances=1 (scheduler)       │
                      │                                    │
                      │  Routers:                          │
                      │    analysis · options · news       │
                      │    watchlist · trading · backtest  │
                      │    stream · _auth                  │
                      │                                    │
                      │  Services:                         │
                      │    signal_generator                │
                      │    auto_trader (strategy engine)   │
                      │    paper_trader (Alpaca bracket)   │
                      │    news (Alpaca + VADER)           │
                      │    earnings (yfinance gate)        │
                      │    options_analyzer                │
                      │    bear_thesis · post_mortem       │
                      │    data_fetcher · live_quotes      │
                      │    indicators · support_resistance │
                      │    fibonacci · gap_detector        │
                      │    supply_demand · backtester      │
                      │                                    │
                      │  Scheduler jobs:                   │
                      │    watchlist_scan  every 15m       │
                      │    auto_trader_manage every 60s    │
                      │    news_poll  every 2m             │
                      │    calibration_job  03:10 UTC      │
                      └────────┬────────────────┬──────────┘
                               │                │
                     ┌─────────▼──────┐  ┌──────▼─────────┐
                     │ Alpaca         │  │ Neon Postgres  │
                     │  · TradingAPI  │  │ serverless PG  │
                     │  · Market data │  │ pool_pre_ping  │
                     │  · News API    │  └────────────────┘
                     │    (/v1beta1)  │
                     └────────────────┘
                               │
                     ┌─────────▼─────────┐
                     │ Yahoo Finance v8  │
                     │ (OHLCV · options  │
                     │  · earnings_dates)│
                     │ rate-limited 30/m │
                     └───────────────────┘
```

### 2.1 Runtime Topology — Dual-service architecture (2026-04-25)

Production runs **two** Cloud Run services that share the same Cloud SQL
database. They differ only in `RUN_MODE`:

```
                ┌──────────────────────────┐    ┌──────────────────────────┐
   Browser ───▶ │ stockrecs (RUN_MODE=api) │    │ stockrecs-manager        │
                │  HTTP API                │    │  (RUN_MODE=manager)      │
                │  + scanner schedule      │    │  internal-only           │
                │  + signal generation     │    │                          │
                │  + entry submission      │    │  + 20s manage loop       │
                │  + alt-data refresh jobs │    │  + 60min reconciliation  │
                └──────────┬───────────────┘    └──────────┬───────────────┘
                           │                                │
                           └─────────────┬──────────────────┘
                                         │
                              ┌──────────▼───────────┐
                              │ Cloud SQL Postgres   │
                              │ (shared state)       │
                              └──────────────────────┘
```

**Why two services**: a crash, rate-limit, or scheduler misfire in the
`api` service (which does heavy work — yfinance polling, scanner runs,
signal generation, alt-data refresh) cannot leave open positions
unmanaged. The `manager` service does **only** the position-management
loop and broker reconciliation; it has near-zero work between ticks and
fewer failure modes.

**Coordination**:
- Both services connect to the same `stockrecs-db` Cloud SQL instance.
- The `api` service writes new `auto_trades` rows (status=pending/open).
- The `manager` service reads + updates those rows (target hits, stop
  trails, force-close).
- Job partitioning is enforced in `main.py:lifespan` based on `RUN_MODE`:
  manager-mode `return`s early after registering its two jobs; api-mode
  registers all the rest.
- BP reservation, circuit breakers, in-memory caches are **per-process**
  by design — the manager doesn't make new entries (no need for BP
  reservation), and circuit breakers fire independently in the service
  that experienced the broker error.

**Resource sizing**:
- `api`: 1 vCPU / 1 GiB / max-instances 3 — handles concurrent HTTP +
  scheduled scans + alt-data fetches.
- `manager`: 1 vCPU / 512 MiB / max-instances 1 — single instance is
  correct here; doubling would dual-fire the manage loop.

**Deploy**:
- `./deploy.sh` builds and deploys the api service.
- `./deploy-manager.sh` builds and deploys the manager service.
  Same image, different `RUN_MODE`.

### 2.2 Legacy single-process topology (pre-2026-04-25)
- **Single Cloud Run instance** (1 vCPU, 1 GiB, min-instances=1 to keep
  the APScheduler ticking). All components in one Python process.
- The dual-service architecture above replaced this; `RUN_MODE=api`
  with no manager service deployed reverts to single-process behaviour
  (manage loop will simply not run).
- **Static React SPA** is baked into the container image and served at
  `/`.
- **Thread model**: FastAPI event loop + `ThreadPoolExecutor` pools for
  parallel watchlist scans, parallel overview price lookups, and
  non-blocking post-mortems.
- **Database**: Cloud SQL Postgres (pool_size=8, max_overflow=7,
  pool_pre_ping=True). Migrated from Neon in revision 12.

---

## 3. Authentication & Deployment

### 3.1 API-key authentication

All `/api/*` routers carry `Depends(require_api_key)` which validates the
`X-API-Key` header against `APP_API_KEY` env var using `hmac.compare_digest`.

- WebSocket `/ws/quotes` authenticates via `?token=<key>` query param
  (browsers cannot set custom headers on WebSockets). Invalid tokens →
  close code 1008 (policy violation).
- `/api/health` is intentionally open for uptime probes.
- When `APP_API_KEY` is unset, auth is a no-op (local dev mode).
- Real-money live-trading requires *both* `APP_API_KEY` set *and*
  `ALPACA_LIVE=1` + `I_UNDERSTAND_LIVE_RISK=yes`.

### 3.2 Frontend login flow

- `LoginScreen` component prompts for the key on first visit.
- On submit, probes `/api/analysis/overview` with the candidate key. 401 →
  "Invalid key". 200 → persist to `localStorage['app_api_key']`.
- All `api.*` helpers attach `X-API-Key` from localStorage. The WebSocket
  client appends `?token=` when opening `/ws/quotes`.
- A global `app:unauthorized` event flips the app back to the login screen
  if any response returns 401 (handles key rotation / expiry gracefully).
- Log-out clears localStorage + unmounts the authenticated tree.

### 3.3 Cloud Run deployment

- Dockerfile: multi-stage `python:3.12-slim`, installs requirements,
  applies `alpaca_websocket_patch.py` to fix `extra_headers` vs
  `additional_headers` compat on websockets 14+.
- `deploy.sh` wraps `gcloud run deploy --source .` with env-var injection
  from `backend/.env` (Alpaca keys, Neon URL, APP_API_KEY, CORS).
- Cloud Run config: `--min-instances=1 --max-instances=3 --memory=1Gi
  --cpu=1 --port=8080 --timeout=300s --allow-unauthenticated`.
- Live URL: **https://stockrecs-zcm5tboivq-uc.a.run.app**
  (shared-secret gated — no Google IAM prompt).

---

## 4. Data Model

Persisted in `backend/database.py` via SQLAlchemy.

### 4.1 `WatchlistStock`
Canonical watchlist. `auto_trade_enabled` is a per-ticker gate the
auto-trader honours even when the global switch is on.

### 4.2 `Signal`
One row per (ticker, timeframe, generation time). Captures direction,
confidence, entry / stop / T1 / T2 / T3, reasoning text, detected
patterns, and the strategy that produced it. Backtest metadata (win rate,
best strategy, score) is blended in when available.

### 4.3 `AutoTraderConfig` — singleton row (id=1)

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Global auto-trade switch |
| `killed` | `false` | Persistent kill flag — survives restarts |
| `dry_run` | `false` | Record trades without broker submission |
| `confidence_threshold` | 75 | Minimum signal confidence to open a trade |
| `max_pct_of_equity` | **1.0** | Total deployable capital ceiling |
| `stock_pct_of_equity` | **0.50** | Stock bucket (≈ $49k on $98k equity) |
| `option_pct_of_equity` | **0.50** | Options bucket |
| `max_risk_per_trade_pct` | 0.02 | Stop-loss dollar risk cap per entry |
| `daily_loss_limit_pct` | 0.03 | Halt entries after this realized loss |
| `max_concurrent_positions` | **15** | Hard cap across portfolio |
| `max_per_sector` | **5** | Soft correlation cap |
| `stop_atr_mult` | 2.0 | Default stop distance in ATR units |
| `chandelier_atr_mult` | 3.0 | Trailing stop overlay (0 = off; adaptive x0.83/1.33 on live) |
| `signal_timeframes` | "1h,4h,1d" | Eligible timeframes for entry |
| `trade_options` | **true** | Enable PUT auto-buy |
| `flatten_by_eod` | **true** (since r32) | 15:55 ET liquidation (intraday mode). Was false; defaulted-true pre-live to bound overnight gap risk. |
| `ml_scoring_enabled` | false | Flip to True after `/api/ml/calibration` shows aligned predicted-vs-actual buckets (target: ≥2 weeks of paper data). Default False = shadow mode (predictions logged, multiplier 1.0). |
| `aggressive_options_mode` | false | Lowers option-leg confidence floors and lets sub-threshold setups through. |
| `trade_calls` | true | CALL play auto-buy. |
| `use_universe_scanner` | true | Read from `CandidatePool` in addition to watchlist. |
| `universe_top_n` | 50 | Pool size to consume per scan. |
| `entry_order_type` | `limit_at_mid` | `market` or `limit_at_mid`. Limit saves ~half the spread on liquid names. |
| `ticker_blacklist` | "" | Comma-separated symbols never to trade (e.g. "GOOGL"). |

### 4.4 `AutoTrade`
Per-entry lifecycle. Status values: `pending`, `open`, `closed_target`,
`closed_stop`, `closed_reverse`, `closed_stale`, `closed_slippage`,
`closed_manual`, `error`. Key fields:

- `entry_price` / `requested_entry` — fill vs signal
- `stop_loss` (original) / `current_stop` (mutated by trailing)
- `target1/2/3` + `level_index` (state machine cursor)
- `high_water_mark` / `low_water_mark` — chandelier calculation
- `realized_pl` — accumulates partial-fill gains (T1 + T2 trims)
- `parent_order_id`, `stop_order_id`, `tp_order_id` — broker refs
- `idempotency_key` — SHA1 of ticker|side|rounded levels|tf|conf bucket|UTC day
- `sector` — captured at entry for correlation cap
- `post_mortem` — JSON analysis populated only on losing stops

### 4.5 `NewsEvent`
One row per Alpaca news article, de-duped on `external_id`.

| Field | Purpose |
|---|---|
| `external_id` | Alpaca article id (unique, indexed) |
| `ticker` / `symbols` | Primary + all mentioned tickers |
| `source` / `author` | Feed + byline |
| `headline` / `summary` / `url` | Article content |
| `published_at` | Article timestamp, indexed |
| `fetched_at` | When our poller ingested it |
| `sentiment_score` | Compound ∈ [-1, +1] (VADER default; FinBERT opt-in) |
| `sentiment_label` | positive / negative / neutral |
| `severity` | `abs(score)` × 100, 0–100 |

Not linked to trades via FK — join at query time by ticker + time overlap.

### 4.6 Alt-data tables (Revisions 24-30)

| Table | Source | Cadence | Used by |
|---|---|---|---|
| `Fundamentals` | yfinance .info | Weekly Sun 04:30 UTC | `quality_multiplier` (±8%), `short_interest_multiplier` (±8%), `beta_weight` (heat cap) |
| `AnalystRating` | yfinance .info | 4×/day | `rating_multiplier` (±10%) |
| `MacroEvent` | Recurrence rules + FRED | Daily 05:00 UTC + 15min FRED fetch | `is_in_blackout` gate on entries |
| `InsiderSummary` | SEC EDGAR Form 4 | Weekly Sun 04:45 UTC | `insider_multiplier` (±6%) |
| `SocialSentiment` | Stocktwits public API | 4×/day | `sentiment_multiplier` (±4%, mcap < $50B only) |
| `WSBMention` | Reddit r/wallstreetbets | Every 30 min | `wsb_multiplier` (±3%, mcap < $50B only) |
| `InstitutionalHoldings` | yfinance institutional_holders (13F proxy) | Weekly Sun 05:15 UTC | `institutional_multiplier` (±3%) |

All have hash-based change detection where appropriate (Fundamentals)
or quarterly-stale-tolerant designs.

### 4.7 ML tables (Revisions 19-20)

| Table | Purpose |
|---|---|
| `MLPrediction` | Every signal that hits the scorer logs predicted_winrate + signal_confidence + (post-close) outcome + realized_pl. Drives the calibration endpoint. |
| `MLArtifact` | Trained model bytes + meta JSON + status JSON, single-row-per-name. Survives container churn since Cloud Run /tmp is per-instance. |

### 4.8 Operational tables

| Table | Purpose |
|---|---|
| `Alert` | Operator alert inbox (severity, category, message, ticker, trade_id, ack timestamps). 5-minute dedup on category+message. |
| `CandidatePool` | Universe scanner output — top-N tickers ranked by composite RVOL/ADX/RS/52w-high score. Refreshed 4×/day. |
| `BestStrategyPerTicker` | Walk-forward winner per (ticker, direction) updated weekly Sun 04:00 UTC. |
| `ConfidenceCalibration` | Per-confidence-bucket realized win rate from closed auto-trades, computed nightly. |

---

## 5. Trading Strategy

### 5.1 Signal Generation (`services/signal_generator.py`)

Composite rule-based evaluation per timeframe, blending:

1. **Trend regime**: EMA20/50/200 alignment
2. **Momentum**: RSI, MACD, ROC
3. **Volatility regime**: ADX
4. **Structure**: pivot points (R1/S1), swing levels, support/resistance
   clusters, Fibonacci retracements + extensions, gap / fair-value-gap
   magnets, supply/demand zones
5. **Volume**: relative volume, OBV divergence
6. **Backtest blend**: strategies with ≥3 historical trades on the ticker
   get their score folded into confidence; new/chronically-losing
   strategies are down-weighted 25%

**Stop calibration** (`_calibrate_long_stop`) picks the second-tightest
candidate among ATR-distance, swing-low structural buffer, and 3×ATR
ceiling — drops the noisiest candidate to survive normal wicks.

**Targets**: collected from Fibonacci extensions (127.2 / 161.8 / 200%),
pivot levels (R1/R2/R3), fresh supply/demand zones, gap magnets, and
swing highs. If fewer than three valid levels exist, falls back to
R-multiple projections (1.5×, 2.5×, 4× risk). Minimum T1 distance
enforced: `entry + max(1R, 0.5×ATR)`.

### 5.2 Entry Gates (`consider_signal`)

Every gate short-circuits on failure. Order matters — cheap checks first.

| # | Gate | Reason |
|---|---|---|
| 1 | Buying-power circuit breaker not tripped | Prevents retry storms on 422 |
| 2 | `enabled=true`, `killed=false`, broker connected | Global switches |
| 3 | Signal is BUY, confidence ≥ threshold | Direction + quality floor |
| 4 | Daily loss limit not hit | Realized-PnL-today gate |
| 5 | `max_concurrent_positions` not reached | Hard portfolio cap |
| 5b | **Regime tightening** (VIX > 25 or SPY < 200EMA → cap÷3; VIX > 20 → cap×2/3) | r34: fewer ideas in chop |
| 6 | **Portfolio heat ≤ 10% of equity** | Σ live $-at-risk bounded (beta-weighted) |
| 7 | **Opening-15-min filter** (intraday TFs 9:30–9:45 ET) | Whipsaw window |
| 8 | Signal freshness (age ≤ 1× timeframe, clamped 10m–90m) | Prevents stale entries |
| 9 | Timeframe in `signal_timeframes` | Default 1h / 4h / 1d only |
| 10 | Stop geometry sane (`stop < entry`, risk 0.1–10%) | Fat-finger guard |
| 11 | **T1 > entry × 1.004** | Catches inverted-target bugs (MU-style) |
| 12 | **Stop distance ≥ 0.8 × daily ATR** | Rejects too-tight stops |
| 13 | **Gap-open ≤ 2%** from signal entry | Rejects stale-entry signals |
| 14 | **Median 20-day daily $-volume ≥ $10M** | r34: liquidity gate (spread/slippage drag) |
| 15 | **No earnings within 48h** (yfinance) | Event-driven variance |
| 16 | Macro release blackout (CPI/NFP/FOMC pre+post window) | Event-driven variance |
| 17 | Per-ticker `auto_trade_enabled` + global blacklist | Per-symbol gate |
| 18 | No existing open/pending trade on this ticker | One-per-ticker |
| 19 | Idempotency hash not seen in last 12h | Dedupe retries |
| 20 | Sector count < `max_per_sector` (5) | Correlation cap |
| 21 | Position qty ≥ 1 after sizing | Capital check |

### 5.3 Position Sizing

```
risk_budget = equity × max_risk_per_trade_pct
           × confidence_multiplier    # 1.0 at threshold → 1.75 at 100%
           × kelly_multiplier         # 1.0 below 55% WR → 1.35 at 100% WR
qty = min(
  risk_budget / risk_per_share,
  stock_remaining / entry,
  (stock_budget × 0.30) / entry,     # per-ticker cap = 30% of stock bucket
  cash / entry,
  buying_power / entry
)
```

Bracket order submitted to Alpaca:
- Parent: market BUY
- SL leg: stop at signal stop (held by Alpaca)
- TP leg: parked 10×R away (never fires — we exit via trailing stop)

### 5.4 Exit State Machine (`manage_open_positions`, 60s cadence)

For each open trade:

1. **Promote** pending → open on parent fill. Reshape SL/TP qty on
   partial fills.
2. **Slippage guard**: fill drifts > 1.0×ATR → force-close; 0.3–1.0×ATR →
   shift all targets by the slippage and cap stop below the original
   risk-per-share (never tighten into chop).
3. **SL invariant check**: if broker's stop leg is missing
   (`canceled` / `replaced` / `rejected`) and position is naked-long,
   resubmit a fresh stop.
4. **Reverse-thesis check**: opposing high-conviction signal on a
   timeframe ≥ source TF (with 60s grace) → close at market.
5. **Stale-trade guard**: trades not hitting T1 after `8 × timeframe_min`
   get closed if price is not meaningfully winning (< 0.3×R above entry).
6. **Trailing state machine** with `_TARGET_CONFIRM_TICKS=2` debounce:
   - **T1**: trim **ADX-aware fraction** (r34, `trim_fraction_for_adx`):
     ADX ≤ 25 → 33% (stocks) / 50% (options); ADX ≥ 40 → 15% (let the
     runner run); linear in between. Then move to **soft BE** at
     `entry − 0.3×initial_risk` (not full entry — post-mortem found full BE
     chopped out winners on 1% retraces). If T1 is < 0.5×ATR from entry
     (NaN-safe check), BE is skipped and the chandelier overlay takes over.
   - **T2**: trim ADX-aware fraction (default 33% of remaining runner) →
     stop to **entry (full BE)**. Runner now ~45% of original position.
   - **T3**: stop → T2 AND **recompute T1/T2/T3 from current price**.
     Recompute runs **ONCE per trade** — past `level_index ≥ 3` we hand
     exclusively to the chandelier to avoid BE-like resets on extensions.
7. **Chandelier overlay** — adaptive to trend strength:
   - `ADX > 30` (strong trend): base × **1.33** (give winners room)
   - `ADX < 20` (chop): base × **0.83** (cut bleed)
   - `20 ≤ ADX ≤ 30`: config default (3.0×ATR)
8. **Reconcile**: parent/leg filled → compute realized P/L, set status,
   enqueue post-mortem for losing stops.

### 5.5 Options Strategy

`consider_put_play(ticker)` runs after every per-ticker analysis when no
strong BUY exists.

- **Earnings gate** (shared with stocks): reject within 48h of earnings
  (puts have higher IV-crush exposure).
- Bear thesis built via `services/bear_thesis` (price action, MACD/RSI
  divergence, pattern breaks).
- Contract filter (`services/options_analyzer`):
  - R:R ≥ 2:1 at T1
  - Bid-ask spread < 5% of strike
  - `MIN_DTE = 10` (raised from 2 — 3DTE weeklies are theta traps)
  - `MAX_DTE = 90`, `MIN_VOLUME = 5`, `MIN_OI = 25`
  - **IV ≤ 1.75 × realized-vol** (20-day annualized, 6h cache) — skips
    over-priced premium prone to vol crush
  - Score = R:R × DTE-sweet-spot × liquidity × delta-proxy

Exit conditions (whichever fires first):
- Underlying hits T1/T2/T3 → ADX-aware trim on T1 (15-50% of original
  contracts), trail underlying-stop tighter
- Premium decay ≥ 50% (skipped within first 5 min if underlying not against
  thesis — spread-artifact guard)
- **Theta stop (r34)**: held ≥ 48h with < 0.2R underlying progress → close
- Underlying breaches bear stop
- Reverse-thesis BUY on higher TF

### 5.6 Budget Allocation (current config)

- Equity: **~$98,600**
- Stock bucket: 50% = **$49,300**
- Options bucket: 50% = **$49,300**
- Max risk per trade: 2% = **$1,972**
- Confidence-scaled risk multiplier: 1.0 → 1.75× at 100% confidence
- Kelly-lite multiplier: 1.0 → 1.35× with backtest win rate ≥ 55%
- Per-ticker cap: 30% of stock bucket = **$14,790**
- Portfolio heat cap: 10% of equity = **$9,860** max $-at-risk across all open trades
- Max concurrent positions: 15
- Max per sector: 5

---

## 6. News Ingestion (Phase 1)

### 6.1 Pipeline

```
Alpaca News API  ──►  services/news.poll_watchlist (every 2m)
  (/v1beta1/news)        │
                         ├──► fetch_alpaca_news()  (httpx, 10s timeout)
                         ├──► ingest() — de-dup on external_id
                         │     └──► score_text() — VADER compound + 30+
                         │         finance-lexicon boosts (beat, upgrade,
                         │         miss, downgrade, lawsuit, fraud, …)
                         └──► NewsEvent row persisted
```

### 6.2 Endpoints

- `GET /api/news?limit=50&hours=24` — recent across watchlist
- `GET /api/news/{ticker}?limit=25&hours=72` — per-ticker feed
- `GET /api/news/trade/{id}/context?before_hours=24&after_hours=24` —
  pre / during / post articles for a trade + alignment verdict
- `GET /api/news/analysis/summary?days=7` — 2×2 sentiment×outcome matrix
  with per-trade breakdown
- `POST /api/news/poll` — manual trigger

### 6.3 Trade-vs-News Correlation

`summary_analysis` bucket closed trades by during-trade average sentiment
and outcome, producing:

| sentiment during trade | wins | losses | flat |
|---|---|---|---|
| positive | … | … | … |
| negative | … | … | … |
| neutral | … | … | … |
| no news | … | … | … |

**Alignment rate** = `(positive_wins + negative_losses) / trades_with_news`.
After ≥1 week of data this tells us whether sentiment is predictive on
our strategy. If ≥ 60% consistently, phase 2 (wiring news into
auto-trader gates) is justified; if ~50%, news would be noise.

Phase 1 is **observability only** — the auto-trader does not consume
`news_events` yet.

---

## 7. Performance Design

### 7.1 Backend

- **Parallel watchlist scan** (`scheduled_scan`): 4-worker ThreadPoolExecutor
  so finish time ≈ slowest ticker, not Σ tickers. Capped at 4 to stay
  inside the Yahoo token bucket (30 req/min).
- **Parallel overview price fetch**: 8-worker pool on `get_current_price`.
- **Lazy puts-watch**: the expensive `/api/options/puts-watch` endpoint is
  no longer on the panel load path — fetched only when user expands that
  section.
- **TTL caches**:
  - OHLCV: per-timeframe TTL (300s for 5m → 86400s for 1mo), LRU 512
  - Backtest results: 1h TTL, keyed on `(ticker, AutoTraderConfig.updated_at)`
  - Overview payload: 20s TTL, fingerprinted on watchlist membership
  - Price fallback: 30s TTL
  - Daily ATR (chandelier): 5-minute TTL
  - Daily ADX (adaptive chandelier): 5-minute TTL
  - Earnings dates: 12h TTL per ticker
  - Realized vol (20d, for IV gate): 6h TTL per ticker
- **Manage-loop locking**: snapshots trade IDs in a short session, then
  processes each trade in its own session so Alpaca REST round-trips
  don't hold the writer lock.
- **Post-mortem async**: 2-thread pool off the manage loop.

### 7.2 Frontend

- **CDN React + Babel standalone**: fast cold start, no build pipeline.
- **Debounced chart fetch** (160ms): rapid timeframe clicks don't cascade
  full chart reloads.
- **AbortController** on in-flight fetches during ticker/timeframe swaps.
- **Dogpile guards** on overview + trading-panel polling.
- **Stable hook identities** via `useCallback` / `useRef` so WebSocket
  subscriptions don't churn.
- **Visible-bar windowing**: `setVisibleLogicalRange` shows a readable
  slice per timeframe (78 bars on 5m, 200 on 1d).
- **Skeleton loaders** on AutoTraderPanel so first paint is instant.
- **Collapsible scrolling sections** cap panel vertical height — Auto-
  Trades / Open Positions / Recent Orders all render inside a max-height
  scrollable frame instead of expanding the page.

---

## 8. UI / UX

### 8.1 Theming (dark + light)

CSS variables in `index.html` define the palette for both themes:

| Token | Dark | Light |
|---|---|---|
| `--bg-0` | `#070a12` | `#f6f8fb` |
| `--surface` | rgba(17,24,39,0.72) | rgba(255,255,255,0.85) |
| `--text-primary` | `#e5e7eb` | `#0f172a` |
| `--accent` | `#3b82f6` | `#2563eb` |
| `--success` | `#10b981` | `#059669` |
| `--danger` | `#ef4444` | `#dc2626` |
| `--chart-bg` | `#0f1419` | `#ffffff` |
| `--chart-grid` | `#1f2937` | `#e5e7eb` |
| `--chart-text` | `#d1d5db` | `#1e293b` |

`data-theme` attr on `<html>` swaps the entire palette. A pre-paint
script in `index.html` applies the saved choice before React mounts so
there's never a flash. Legacy Tailwind gray-palette classes are
remapped under `[data-theme="light"]` so existing markup just works.

### 8.2 Charts

- Theme-aware creation: `chartThemeOptions()` reads CSS vars at creation
  time. Chart instance is recreated on theme change; the data-load
  effect also re-runs (theme is in the deps) so candles/indicators are
  re-applied to the new instance.
- **Hide-all-indicators toggle** — single checkbox in the chart header
  collapses EMAs, RSI, MACD, S/R, supply/demand zones, Fib lines, and
  gaps/FVGs — leaving just candles + volume. Preference persisted to
  localStorage.
- Live-tick extension on the most recent bar (mutates high/low/close as
  WS quotes arrive).

### 8.3 Navigation

Three views switched via header tabs:
- **Charts & Analysis**: left watchlist + center chart/analysis panel
  with timeframe selector, signal card, timeframe alignment, options
  table, news panel, and backtest.
- **Trading**: auto-trader panel + news-alignment summary + paper
  trading account/positions/orders.

Header also carries: live-stream indicator, theme toggle pill, log-out.

### 8.4 Key panels (modernized)

**AutoTraderPanel** (Trading view):
- Hero stats row: Equity · Deployed · Open Trades · Today P/L
- Budget gauges — gradient-filled bars (blue→indigo for stocks,
  purple→fuchsia for options)
- Running/Paused pill with live-pulse dot; Start/Pause button
- Config drawer with proper field spacing + strategy explainer
- Auto-Trades as cards (status pill, trail-level badge, 4-column qty /
  entry / stop / targets grid, inline post-mortem + news expansion)
- Lazy Put-Play Watch (doesn't block first paint)

**TradingPanel**:
- Same hero-stats + capital-deployment gauge pattern
- Position cards (symbol, side pill, large P/L, hover lift)
- Themed order table with sticky header

**NewsPanel** (per-ticker in Charts view):
- Headline feed with VADER sentiment pill (pos/neu/neg + score)
- Source / author / relative-time metadata
- Click-through to full article

**TradeNewsContext** (inline on closed auto-trades):
- Pre-trade / during-trade / post-trade article buckets
- Average sentiment during trade
- `aligned` / `contrary` / `no-news` / `neutral-news` verdict pill

**NewsAnalysisSummary** (Trading view):
- 3 / 7 / 14 / 30 day window toggles
- Alignment-rate stat
- 2×2 matrix of (sentiment × outcome)
- Per-trade details

**CollapsibleSection** (reusable):
- Header with title, count pill, subtitle, expand caret
- Body has `max-height` + `overflow-y: auto` + themed thin scrollbar
- Used for Auto-Trades, Open Positions, Recent Orders so long lists
  don't stretch the panel vertically

---

## 9. API Surface

### Watchlist
- `GET /api/watchlist` — list rows
- `POST /api/watchlist` — add ticker
- `DELETE /api/watchlist/{ticker}` — remove
- `PATCH /api/watchlist/{ticker}/auto-trade` — toggle per-ticker gate

### Analysis
- `GET /api/analysis/overview` — watchlist snapshot (cached 20s)
- `GET /api/analysis/{ticker}` — full analysis
- `GET /api/analysis/{ticker}/chart?timeframe=1h` — candles + indicators + S/R + zones + fibs + gaps
- `GET /api/analysis/{ticker}/signals?timeframe=1d` — raw signals
- `POST /api/analysis/scan` — manual trigger (parallel)

### Trading
- `GET /api/trading/account` — balance snapshot
- `GET /api/trading/positions` — open Alpaca positions
- `GET /api/trading/orders?status=all&limit=20` — recent orders
- `POST /api/trading/order` — manual bracket order
- `POST /api/trading/close/{symbol}` — flatten position
- `DELETE /api/trading/orders/{id}` — cancel order
- `GET /api/trading/auto/status` — budget snapshot + config
- `GET /api/trading/auto/trades?limit=50` — trade ledger
- `POST /api/trading/auto/config` — update singleton config (now accepts
  `max_per_sector`, `signal_timeframes`, `stop_atr_mult`,
  `chandelier_atr_mult`, `dry_run` in addition to original fields)
- `POST /api/trading/kill` / `POST /api/trading/unkill`
- `POST /api/trading/auto/postmortem/{id}` — regenerate post-mortem

### Options
- `GET /api/options/puts-watch` — scan watchlist for bear plays (slow — lazy-loaded in UI)
- `GET /api/options/{ticker}?timeframe=4h&side=auto|calls|puts`

### News
- `GET /api/news?limit=50&hours=24` — recent across watchlist
- `GET /api/news/{ticker}?limit=25&hours=72` — per-ticker feed
- `GET /api/news/trade/{id}/context` — trade ↔ news correlation
- `GET /api/news/analysis/summary?days=7` — aggregate analysis
- `POST /api/news/poll` — manual trigger

### Backtest
- `POST /api/backtest/{ticker}` — evaluate all strategies on 2y daily

### Health & WS
- `GET /api/health` — subsystem heartbeat (open, no auth)
- `WS /ws/quotes?token=<key>` — live stock tick broadcast (token-gated)

### Auth
All `/api/*` endpoints carry `Depends(require_api_key)` which rejects
missing/wrong `X-API-Key` with 401. When `APP_API_KEY` env var is unset,
auth is a no-op (local dev).

---

## 10. Observability

- **Rotating log file** (container `/app/backend/logs/backend.log`, 5MB × 5).
- **Rate-limited formatter** deduplicates noisy Alpaca messages (60s window).
- **`/api/health`** surfaces: scheduler_started, live_quotes_started,
  stream_stale_secs, last_scan_at, last_manage_at, realized_pnl_today,
  open_positions, auth_configured, alpaca_live.
- **Metrics counters** (`services/metrics.py`): `autotrade_event` tagged
  by event. Current event taxonomy:
  - `opened`, `opened_put`
  - `closed_target`, `closed_stop`, `closed_reverse`, `closed_stale`,
    `closed_slippage`, `closed_manual`
  - `partial_t1`, `partial_t2`
  - `sl_resubmitted`, `bp_exhausted`, `entry_lock_timeout`
  - `fat_finger_reject`, `bad_t1_geometry`, `stop_too_tight_atr`,
    `gap_open_reject`, `portfolio_heat_cap`, `opening_filter`,
    `daily_loss_halt`, `earnings_skip`, `earnings_skip_put`
  - `killed`, `unkilled`
- **Nightly calibration job** (03:10 UTC): buckets closed trades by
  confidence and logs per-bucket win-rate + avg P/L.

---

## 11. Safety & Risk Controls

### Entry-side gates (~27 total — expanded since r19)
Confidence threshold, timeframe allow-list, signal freshness (1× TF cap 90m), 9:30-9:45 ET filter, geometry (stop < entry, T1 > entry × 1.004, risk-per-share 0.1-10%), stop-vs-ATR ≥ 0.8×, gap-open ≤ 2%, **liquidity gate** (median 20-day $-volume ≥ $10M, r34), earnings < 48h window, idempotency dedup, per-ticker cap, sector cap (max 5), concurrent cap (15), **regime-tightened concurrent cap** (cap÷3 in VIX>25 or SPY<200EMA; cap×2/3 in VIX>20, r34), beta-weighted portfolio-heat cap (10% of equity), daily-loss cap (3% of equity), fat-finger guard, BP circuit breaker, broker-down circuit breaker, **macro release blackout** (CPI/NFP/FOMC/etc.; pre+post window with options 1.5× wider), **opening-bell options blackout** (15 min after open), **EOD options blackout** (45 min before close), **MIN_DTE=10** filter on options chains, **adaptive risk size** (×0.5 when VIX > 25 OR recent WR < 55%), **VIX-scaled options bucket** (×0.3-0.75 at VIX > 20-30), **cheap-options gamma cap** (sub-$0.50 premium → 0.5% equity cap), ticker blacklist.

### Multiplier stack (~10 factors, hard-capped at 2.0×)
Confidence-headroom × Kelly × calibration × per-strategy × VIX × analyst-rating × fundamentals-quality × short-interest × Stocktwits × WSB × institutional × insider × ML-scorer (shadow) × **AI judge (r36, shadow by default, 0.6×–1.4× envelope)**. Compound is clamped to `RISK_MULT_CEILING = 2.0` (Critical-audit fix #1) so a winning streak across all factors can't compound to runaway risk. Heat-aware throttle (r35: 0.85× / 0.60× / 0.40× as live heat crosses 50% / 70% / 85% of cap) applies AFTER the ceiling.

### Exit-side guarantees
SL-invariant check (resubmit on broker drop), slippage reject, reverse-thesis close (gate ≥80 conf + same-or-higher TF, with correct CALL/PUT direction post-r22), stale-trade recycle, debounced target touches (2-tick confirm), atomic stop-replacement (broker ack gates DB update), adaptive chandelier (ADX-driven 0.83-1.33× of base mult, never loosens existing stop), **ADX-aware T1/T2 trim fractions** (r34: 15% in strong trend, default in chop), ATR-capped Soft BE (`max(0.3R, 0.25×ATR)` to survive 1-bar wicks), premium-stop spread-artifact guard (skip when held < 5 min AND underlying not against thesis), **options theta stop** (r34: close when held ≥ 48h with < 0.2R underlying progress).

### Crash-resilience (r33)
- **Dual-service architecture**: position management runs in a dedicated `stockrecs-manager` Cloud Run service (internal-ingress, min/max=1 instance). The `stockrecs` (api) service handles HTTP + scanner + alt-data; a crash there cannot leave open positions unmanaged.
- **Cloud Run liveness probes** on both services targeting `/api/health`. Manager's probe trips on `manage_loop_stuck` (≥120s since last manage tick) → Cloud Run auto-restarts the container.
- **Boot-time reconciliation**: manager runs `detect_unexpected_positions` once at startup so any drift from a prior incarnation is reconciled before the manage loop begins normal operation.
- **WS reconnect**: jittered exponential backoff + escalating `stream_reconnect_loop` alert at cap.
- **Stuck-job alert**: dedicated `manage_loop_stuck` category, 5-min dedup.
- **WS staleness alert**: `stream_stale` fires when quote stream > 30s during RTH.

### Sanity
- Two-key live-trading gate (`ALPACA_LIVE=1` + `I_UNDERSTAND_LIVE_RISK=yes` + `APP_API_KEY` + explicit `CORS_ALLOW_ORIGINS`).
- Persistent kill switch — survives deploys; unkill does NOT re-enable trading (two-step re-arm).
- Idempotency hash deduping retries within 12h, bucket-aware on confidence.
- Post-mortem auto-generated on every losing stop (skipped for `closed_reverse`).
- Synthetic-data regression suite (93 tests) gates every deploy via `deploy.sh`.

### Auth & access
- Shared-secret `APP_API_KEY` gating all `/api/*` and `/ws/quotes`.
- Frontend login screen with localStorage cache.
- 401-global-event flips UI back to login.
- Manager service is `--ingress internal`, not reachable from public internet at all.

---

## 12. Deployment

### 12.1 Local / Cloud Workstation

```bash
./run.sh   # uvicorn on 0.0.0.0:8000 with venv auto-detect
```

### 12.2 Cloud Run (production, currently serving)

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com
./deploy.sh            # default region us-central1
```

`deploy.sh` sources `backend/.env` and sets: `APCA_API_KEY_ID`,
`APCA_API_SECRET_KEY`, `DATABASE_URL`, `APP_API_KEY`, `CORS_ALLOW_ORIGINS=*`.

### 12.3 Environment Variables

See `.env.example` at repo root for the full list. Summary:

| Var | Purpose |
|---|---|
| `RUN_MODE` | `api` (default) or `manager` — partitions scheduler jobs across the dual-service architecture |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Alpaca credentials |
| `ALPACA_LIVE` / `I_UNDERSTAND_LIVE_RISK` | Live-mode two-key gate |
| `APP_API_KEY` | Shared-secret auth; empty = dev mode open |
| `DATABASE_URL` | Cloud SQL Postgres connection string |
| `CORS_ALLOW_ORIGINS` | Comma-separated origins |
| `ALPACA_DATA_FEED` | `sip` (Algo Trader Plus) or `iex` (free tier) |
| `ANTHROPIC_API_KEY` | Enables in-app Claude chat widget |
| `FRED_API_KEY` | Enables post-release macro values |
| `SENTIMENT_BACKEND` | `vader` (default) or `finbert` |
| `LOG_JSON` | `1` (default Cloud Run) for structured stdout, `0` for plaintext |
| `LOG_DIR` | Rotating-file log location |

### 12.4 Rotating the API key

```bash
NEW=$(openssl rand -hex 32)
gcloud run services update stockrecs --region us-central1 \
  --update-env-vars "APP_API_KEY=$NEW"
echo "$NEW"
```

All browsers get 401 on next request and prompt for the new key.

---

## 13. Operational Playbook

### Deploying

```bash
# Deploy api service (HTTP + scanner + alt-data jobs)
./deploy.sh us-central1

# Deploy manager service (manage loop only — separate Cloud Run service)
./deploy-manager.sh us-central1
```

Both scripts gate on the regression suite (78+ tests, ~3s) before
building. `SKIP_TESTS=1` to override.

### Post-loss triage
1. Check `/api/trading/auto/trades` for the losing trade id.
2. Click the green **"📊 Why this trade?"** expander on the trade card —
   shows scanner snapshot (if scanner-picked), full signal reasoning,
   backtest evidence, fundamentals quality score, analyst consensus,
   macro events ±48h.
3. For losers, click **"🔍 Why did this lose?"** — auto-generated
   post-mortem with verdict, findings (severity-tagged), lessons.
4. Click **"📰 News during trade"** to see pre / during / post
   articles with sentiment — catches event-driven losses.
5. Correlate across trades: Trading view → `News ↔ Trade Alignment`
   summary (3 / 7 / 14 / 30 day windows).
6. **Codify the bug**: if a post-mortem surfaces a code-path bug,
   add a synthetic test to `backend/tests/test_bug_scenarios.py`
   that fails against the bug and passes after the fix. The test
   gate prevents regression on the next deploy.

### Config tuning
- Budget changes: Trading view → Auto-Trader panel → ⚙ Config drawer.
  Changes hit `POST /api/trading/auto/config` and apply from the next
  scan tick.
- Kill switch: `POST /api/trading/auto/kill` with `flatten=true`.
  Survives restarts. Unkill with `POST /api/trading/auto/unkill` then
  flip `enabled=true` via `/auto/config` (deliberate two-step).
- ML scoring: `ml_scoring_enabled` defaults False (shadow mode).
  Watch `/api/ml/calibration` for ≥2 weeks of paper data; flip to
  True when predicted-vs-actual buckets are aligned within ±5%.

### Resilience verification
- `/api/health` on **api service**: should always show `degraded=false`
  with `last_manage_at=None` (api doesn't manage).
- `/api/health` on **manager service** (internal-only, hit from inside
  the project): `last_manage_at` should refresh every 20s during RTH.
  If it stales > 120s, the liveness probe trips and Cloud Run
  auto-restarts the container.
- Cloud Logging filter `resource.labels.service_name="stockrecs-manager"
  AND jsonPayload.message=~".*manage_loop_stuck.*"` surfaces the alert
  if it ever fires.

### News analysis workflow (after ≥1 week of ingestion)
1. Trading view → `News ↔ Trade Alignment` — set window to 7d.
2. Review the 2×2 matrix: if positive-news win-rate meaningfully exceeds
   negative-news win-rate, sentiment has predictive value.
3. If alignment rate ≥ 60% consistently → phase 2 worth wiring news gate
   into `consider_signal` (reject on high-severity adverse news < 30 min
   old).
4. If alignment rate ~ 50% → news is noise for our strategy; don't wire.

---

## 14. Changelog (current → past)

### Revision 36 — AI judge layer: entry veto + news-driven exit + sizing multiplier (shadow by default)
- **`services/ai_judge.py`** wraps three Claude (Haiku) call sites with a
  shared client, tool-use-forced JSON schemas, latency budget, and a
  fail-open guarantee. Each call site has its own env mode flag
  (`AI_ENTRY_VETO_MODE`, `AI_NEWS_EXIT_MODE`, `AI_CONFIDENCE_MULT_MODE`)
  that cycles `off → shadow → active`. **Defaults to `off` everywhere** —
  no behavior change at deploy. Flip to `shadow` first, review ≥ 200
  decisions in `AIDecisionLog`, then promote to `active`.
- **Hard guarantee**: any failure (no API key, network, schema mismatch,
  malformed response, timeout) returns the abstain value
  (proceed / hold / 1.0×). Live trading is never blocked by Claude
  unavailability. 6 unit tests pin this contract.
- **Entry veto** (`consider_signal`, after every other gate passes):
  Claude reviews `{signal, fundamentals, recent_news, same-sector
  positions, analyst rating, insider, social}` and returns
  `{verdict: proceed | skip, reason}`. Active-mode skip → `autotrade_skip
  {reason=ai_veto}`.
- **Confidence multiplier** in the sizing stack: returns
  `multiplier ∈ [0.6, 1.4]` that joins `conf × kelly × cal × strat ×
  vix × ai_mult`. Already bounded by `RISK_MULT_CEILING=2.0×`. Shadow
  mode logs the requested value but feeds 1.0 to the sizer.
- **News-driven exit** (post-ingest hook in `services/news.py`): on
  each freshly-inserted medium+ severity news item that matches an open
  position, Claude returns `{is_thesis_relevant, action: hold | trim |
  close, reason}`. Honored `close` triggers `force_close_trade` with
  `status=closed_news_ai`; honored `trim` halves the position at market.
- **`AIDecisionLog`** table — every call (off / shadow / active) logged
  with prompt summary, response, latency, honored flag. Operator review
  via `GET /api/ai-judge/decisions` (filterable by call_site +
  honored), `GET /api/ai-judge/summary`, `GET /api/ai-judge/modes`.
- **Cost**: ~$0.001/Haiku call × ≤ 50 high-conf signals/day ≈ $0.05/day
  per active call site. Move to Opus only after measuring shadow
  accuracy.

### Revision 35 — Pre-live BACKLOG sweep: backtest stress windows, heat-aware sizing, signal validation, close notifications
- **Portfolio backtest stress windows** (`portfolio_backtest.STRESS_WINDOWS`):
  five canned historical drawdown periods (Aug 2024 carry unwind, Mar 2020
  COVID, Feb 2018 volmageddon, Q4 2018 Powell, Aug 2015 China). Replays the
  strategy over the fixed range with today's caps. Pre-2024 windows
  auto-trigger an extended-history fetch (`10y` instead of cached `2y`).
  Endpoint: `POST /api/backtest/portfolio/run?stress_window=<key>` and
  `GET /api/backtest/portfolio/stress-windows`.
- **Always preload ^VIX** in portfolio backtest so regime tagging actually
  works (silent None bug previously collapsed `high_vix` regime into
  `normal`).
- **Realized pair-correlation diagnostic** in portfolio-backtest stats
  (`avg_pair_corr`, `max_pair_corr` over the traded ticker universe).
  Read-only — pairwise cap enforcement is still Tier C.
- **Heat-aware risk-per-trade** (`risk_manager.heat_aware_risk_multiplier`):
  per-trade risk shrinks 0.85× / 0.60× / 0.40× as live beta-weighted heat
  crosses 50% / 70% / 85% of the 10%-of-equity cap. Applied at all three
  sizing call sites (stock, option, option-2nd-site). The hard reject at
  100% in `consider_signal` still protects the book; this softens the
  approach so the last few entries before the cap are smaller probes
  rather than full 2% positions.
- **Pydantic `SignalPayload` model** (in `models.py`): validates the
  signal dict at the `consider_signal` boundary. Required fields
  (`ticker`, `timeframe`, `signal_type`, `confidence`) + range checks
  (`0 ≤ confidence ≤ 100`); enums (`Timeframe`, `SignalType`) catch
  string-typo bugs. `extra='allow'` so the long tail of enrichment
  fields (sentiment, news, ml_prob, …) doesn't break on every new add.
  Failed validation → log + skip (`autotrade_skip{reason=malformed_signal}`)
  instead of letting `signal.get("entry") or 0` silently coerce 0
  downstream.
- **`trade_closed` push notifications**: companion to `target_hit`. WS
  broadcast on every exit path (target / stop / reverse / theta / stale)
  with `realized_pl`. Frontend `TargetHitToasts` component now renders
  win/loss-styled close toasts (12s sticky vs 8s for trails) + browser
  Notification when tab is backgrounded.
- 12 new tests (3 portfolio-backtest, 7 heat-aware multiplier, 9
  signal-validation; 112 total now).

### Revision 34 — Tier A from external review: regime tightening + ADX trim + liquidity + theta stop
- **Regime-aware concurrent-position cap** (`risk_manager.regime_concurrent_cap`):
  VIX > 25 OR SPY below 200-EMA → base // 3 (typically 5); VIX > 20 → base × 2/3
  (typically 10). Layered on top of adaptive-risk + VIX-options scaling that
  already shrink size — this layer additionally limits the *number* of
  concurrent ideas when regime is hostile. Wired into `consider_signal`.
- **ADX-aware T1/T2 trim fractions** (`auto_trader.trim_fraction_for_adx`):
  weak trend (ADX ≤ 25) → default trim (33% stock / 50% option); strong
  trend (ADX ≥ 40) → 15% trim (let the runner run); linear interpolation
  between. Applied at three sites: option T1, option T2, stock T1.
- **Liquidity gate** in `consider_signal`: reject entries on tickers with
  median 20-day daily $-volume < $10M. Sub-threshold names produce wide
  spreads that quietly erode R-multiples; threshold deliberately
  conservative (most large-caps clear $100M+/day).
- **Options theta stop**: close any option position that has held ≥48h
  with < 0.2R underlying progress toward target. Catches the slow-bleed
  failure mode where the thesis isn't wrong enough to trip the underlying-
  stop but isn't right enough to make money before theta eats the premium.
- **Sharpe annualization factor by timeframe** (`backtester.py`):
  `sqrt(bars_per_year)` instead of hardcoded `sqrt(252)`. Equity curve in
  `_simulate` is per-bar, so annualizing as if it were daily inflated
  intraday Sharpe by 8.8× on 5m, 14× on 1m. `portfolio_backtest.py` is
  unchanged (already daily).
- **ATR fallback improvement**: when ATR_14 is missing/zero, fall back to
  trailing 14-bar median High–Low range instead of hardcoded 2% of Close.
  Adapts to the symbol's actual realized range. Applied in `backtester.py`
  (both `_simulate` and `_evaluate`) and `position_manager.recalculate_targets`.
- **OCC parser consolidation**: removed dead inline P/C-detection in
  `_manage_option_trade` — `is_call_option()` in `position_manager` is
  now the single source of truth.
- **BACKLOG additions**: IV percentile gate (deferred — needs 252d history
  ingestion); ML scorer graduation criteria (≥200 closed trades + AUC > 0.60).

### Revision 33 — Dual-service architecture + observability hardening (Tiers 1+2+3+4)
- **`RUN_MODE` env-var splits the app across two Cloud Run services**.
  - `stockrecs` (RUN_MODE=api): HTTP, scanner, signal generation, all alt-data
    refresh jobs. Min 1, max 3 instances.
  - `stockrecs-manager` (RUN_MODE=manager): internal-ingress only. Runs
    **only** the 20s manage loop + 60min broker reconciliation + boot-time
    reconciliation. Min/max 1 instance. 512 MiB.
  - Both services share the same Cloud SQL DB. api writes new auto_trades;
    manager reads + updates them. Process-local state (BP reservations,
    circuit breakers, caches) is per-service by design.
  - New `deploy-manager.sh` deploys the manager service.
- **Cloud Run liveness + startup probes** in both deploy scripts. Manager's
  liveness probe trips when `last_manage_at > 120s` stale during RTH,
  causing Cloud Run to auto-restart the container.
- **Stuck-job detector**: `/api/health` on the manager raises a
  `manage_loop_stuck` alert + flags `degraded=True` if manage hasn't ticked
  in 120s.
- **WS reconnect hardening**: jittered exponential backoff (±50% randomization
  to avoid thundering-herd) + escalating `stream_reconnect_loop` alert when
  ≥5 consecutive failures with backoff at cap.
- **Documentation hygiene**: root README.md (setup, runbook, env vars, risk
  warnings); `.env.example` template; pinned all `requirements.txt` versions
  with `==` (added `curl_cffi` as transitive yfinance dep).
- **Prometheus metrics extended**: new `autotrader_skips_total{reason=...}`
  counter; gate-by-gate `metrics.inc("autotrade_skip", reason=...)` on 10+
  rejection paths in `consider_signal`.
- 15 new tests in `test_bug_scenarios.py` (risk_math pure helpers,
  risk_manager state isolation, adaptive-risk neutral defaults, RUN_MODE
  resolution). 93 tests total.

### Revision 32 — Tier A+B risk tightening + observability
- **Retail-sentiment market-cap filter**: Stocktwits + WSB multipliers now
  return neutral above $50B market cap. Prevents retail-sentiment noise
  from moving confidence on AAPL/NVDA where the tape already reflects
  retail flow.
- **Kelly cap 1.35 → 1.2** (config). Tightened pre-live: not enough closed
  trades to trust bucket win-rates aggressively.
- **`flatten_by_eod` default-true** for new `AutoTraderConfig` rows.
  Existing rows preserved.
- **Adaptive risk sizing**: `max_risk_per_trade_pct` × 0.5 when VIX > 25 OR
  recent-30d realized win-rate < 55%; × 0.75 when VIX > 20.
- **VIX-scaled options bucket**: `option_pct_of_equity` × 0.3/0.5/0.75 at
  VIX > 30/25/20. Stocks bucket untouched.
- **Profit factor + by-regime stats** in portfolio backtest. Each trade
  tagged with entry ADX + VIX; stats split by trending / chop / high_vix /
  normal. Profit factor = gross wins / |gross losses|.
- **Overnight-gap simulation** in portfolio backtest. Resting stops fill
  at the day's OPEN if it gapped through.
- **WebSocket staleness alert**: fires when stream stale > 30s during RTH
  (5-min dedup).

### Revision 31 — auto_trader.py decomposition (risk / execution / position modules)
Three-checkpoint refactor split the 3,200-LOC `auto_trader.py` into
focused modules. Net 300 LOC reduction with back-compat aliases preserved
so no external call site changes.

- **`services/risk_manager.py`** (4a): owns BP reservation state +
  helpers, BP/broker/SL circuit breakers, strategy + calibration multiplier
  caches, `adaptive_risk_multiplier`, `vix_options_bucket_multiplier`.
  Module-local `reset_for_tests()`.
- **`services/execution_engine.py`** (4b): owns Alpaca broker ops —
  `replace_stop` + idempotency cache, `get_legs`/`identify_legs`,
  `force_close_trade` (callback-driven for trade-state cleanup).
- **`services/position_manager.py`** (4c): owns chandelier (ATR/ADX/adaptive)
  + caches, `current_price` lookup, `recalculate_targets`,
  `record_target_history`, `is_call_option`, `check_reversal`,
  `check_reversals_for`, `REVERSE_CONFIDENCE_GATE`, `_TF_RANK`.
- `auto_trader.py` retains: `consider_signal`/`consider_put_play`/
  `consider_call_play`, `manage_open_positions`, `_manage_option_trade`,
  config CRUD, kill switch, calibration job. Plus thin back-compat
  aliases to the moved helpers.

### Revision 30 — Pydantic schemas + state view + risk_math extract
- **`services/schemas.py`**: SignalData, TradeContext, MultiplierStack,
  MacroBlackoutStatus. Additive — existing `Dict[str, Any]` call sites
  still work; new code constructs the model directly.
- **`services/auto_trader_state.py`**: read-view of auto_trader module
  state for monitoring (`state_view()`) and `reset_for_tests()` to clear
  caches between tests. Avoided full class encapsulation as documented in
  BACKLOG (Python modules already singletons — wrapper would be ceremony).
- **`services/risk_math.py`**: pure-function helpers extracted —
  `signal_idempotency_key`, `clamp_multiplier_stack`,
  `confidence_risk_mult`, `kelly_risk_mult`, `position_size_by_risk`. Zero
  module-state coupling; trivially unit-testable.

### Revision 29 — Pluggable sentiment + portfolio backtest
- **`services/sentiment.py`**: pluggable backend via `SENTIMENT_BACKEND`
  env. Default VADER (instant, no deps); opt-in FinBERT (`transformers` +
  `torch`, ~1GB image bloat). Backend choice invisible at the call site
  (`services.news.score_text` shim preserved).
- **`services/portfolio_backtest.py`**: book-level walk-forward backtester
  that respects the live trader's caps (max_concurrent, max_per_sector,
  beta-weighted portfolio heat, daily loss limit). Returns composite equity
  curve, max drawdown %/days, Sharpe ratio, cap-rejection count, peak
  per-sector concentration.
- `POST /api/backtest/portfolio/run` exposes it.

### Revision 28 — Tier-2 alt-data + push notifications
- **`services/wsb_scraper.py`**: Reddit JSON API public endpoint poll
  every 30 min; counts ticker mentions in posts + comments with
  bullish/bearish keyword hints. `WSBMention` table.
  `wsb_multiplier` ±3% envelope; requires ≥10 mentions + 2:1 lean to tilt.
- **`services/institutional.py`**: 13F-proxy via yfinance
  `.institutional_holders` + `.mutualfund_holders`. `InstitutionalHoldings`
  table tracks holder count, weighted QoQ pct change, new initiations.
  Weekly Sun 05:15 UTC. ±3% multiplier envelope.
- **Push notifications on T1/T2/T3 hits** via existing WS channel.
  `services.live_quotes.broadcast_event_safe()` thread-safe helper for
  scheduler-thread emissions; auto_trader manage loop broadcasts
  `target_hit` events on stock + options paths. Frontend
  `TargetHitToasts` component renders in-app toast + browser
  Notification when tab is backgrounded.

### Revision 27 — Tier-1 alt-data: short interest, Stocktwits, SEC Form 4
- **Short interest** via Fundamentals (yfinance .info already exposes it).
  New columns `short_pct_float` + `short_ratio`. `short_interest_multiplier`
  envelope 0.92..1.02. BUY: ≥25% shorted → 0.92 (respect skepticism);
  15-25% → 1.02 (squeeze tilt). SELL: mirror — already-crowded shorts
  = 0.92 (late to the party).
- **`services/social_sentiment.py`**: Stocktwits public stream API,
  aggregates last 24h bullish/bearish tagged messages. `SocialSentiment`
  table. 4×/day refresh. 0.96..1.04 envelope; requires ≥20 messages and
  ≥60% lean.
- **`services/insider_trades.py`**: SEC EDGAR Atom-feed parser per CIK,
  extracts Form 4 nonDerivativeTransaction codes (`P` = open-market
  purchase, `S` = open-market sale; ignores 10b5-1 mechanical codes
  M/A/F by construction). `InsiderSummary` table tracks 30d/90d buy/sell
  counts + net buy ratio + $ value. Weekly Sun 04:45 UTC; serial 200ms
  pacing under SEC's 10 req/s limit. 0.97..1.06 envelope; requires ≥3
  90d transactions for signal.
- All three multipliers wired into signal_generator BUY + SELL paths.

### Revision 26 — Beta-weighted portfolio heat + ATR-capped soft BE
- **Beta-weighted heat**: `beta` column on Fundamentals (yfinance .info,
  weekly refresh). The 10%-of-equity portfolio heat cap multiplies each
  open trade's $-at-risk by `clamp(beta, 0.5, 2.0)`. 5 high-beta tech
  longs now contribute more heat than 5 utilities at the same raw
  $-at-risk.
- **ATR-capped Soft BE cleanup**: `services/auto_trader.py` had a
  duplicate Soft BE block (old `entry − 0.3R` and new `max(0.3R, 0.25×ATR)`
  side-by-side, both executing). Cleaned up to a single line:
  `stop_dist = max(0.3R, 0.25×ATR)`.

### Revision 25 — Trade rationale endpoint + UI expander
- New `GET /api/trading/auto/rationale/{trade_id}` aggregates:
  origin classification (watchlist | scanner | watchlist+pool | unknown),
  scanner snapshot (when applicable), originating signal reasoning bullets,
  best-strategy-per-ticker backtest evidence, fundamentals quality score,
  analyst consensus + target premium, macro events ±48h of opened_at.
- Frontend `TradeRationale` component renders themed sections (indigo
  scanner / emerald signal / blue backtest / purple fundamentals / amber
  analyst / rose macro) under a green "📊 Why this trade?" expander on
  every trade card. Lazy-loaded on click.

### Revision 24 — Fundamentals quality score
- New `Fundamentals` table with hash-based change detection (SHA256 over
  20 stable fields; unchanged fetches only bump `last_checked_at`). 20
  metrics from yfinance .info (P/E, PEG, P/B, P/S, EV/EBITDA, revenue +
  EPS YoY, profit/operating margins, ROE/ROA, D/E, current ratio, FCF,
  dividend yield, sector/industry).
- `compute_quality_score` returns -100..+100 composite from 4 buckets
  (profitability/growth/balance/valuation, 25 pts each).
- `quality_multiplier` 0.92..1.08 envelope. Asymmetric — penalty heavier
  than boost (betting against junk fundamentals on a long is the
  asymmetric risk).
- Weekly Sun 04:30 UTC refresh. `/api/fundamentals/{ticker}` +
  `/refresh` + `/refresh-all`.

### Revision 23 — Synthetic-data regression suite + pre-deploy gate
- New `backend/tests/test_bug_scenarios.py` — 27 initial tests covering
  the bug families surfaced in production losses (reverse-thesis
  direction, OCC parser, macro blackout windows, ticker blacklist, risk
  multiplier ceiling, cheap-options sizing cap, ML multiplier envelope,
  trade-rationale endpoint shape).
- Each test class targets a specific bug family that naive code review
  missed. Verified the suite catches its targets by reintroducing the
  AMKR bug and watching the test fail.
- `deploy.sh` now runs the suite as a hard gate before every gcloud
  build. ~3s. `SKIP_TESTS=1` to override.

### Revision 22 — Options-loss postmortem fixes (4 root-cause items)
Investigation of $-10K paper loss on 2026-04-24 surfaced four bugs:

1. **Reverse-thesis direction bug for CALL plays**. `_check_reversal`
   hardcoded `opposing="BUY"` for ALL options. Correct for PUT (long-put
   = bearish, opposing BUY); WRONG for CALL (long-call = bullish,
   opposing should be SELL). New `_is_call_option()` parses OCC symbol.
   The AMKR -$1,190 paper loss was caused by a CONFIRMING BUY signal
   force-closing the long call.
2. **Premium-stop spread-artifact guard**. The 50%-premium-decay rule
   force-closed VTWO in 24 seconds for $-6,500 because the "decay" was
   bid-ask spread cross at market open. Now skip premium-stop when
   (held < 5 min) AND (underlying not moving against thesis).
3. **Opening-bell options entry blackout**. All three losses opened in
   the first 18 min of the session — bid-ask spreads at their widest.
   Mirror of the EOD guard: skip new option entries within 15 min of
   open. New `paper_trader.minutes_since_open()` helper.
4. **Cheap-options gamma cap**. CNTA $0.30 premium → 122 contracts (122
   × $30 = $3.7K notional) wiped $2,440 on a 1% adverse move. Tiered
   per-position dollar cap fraction:
     * Premium < $0.50 → 0.5% of equity
     * $0.50 - $2.00   → 1% of equity
     * $2.00+          → 2% (original aggressive cap)

### Revision 21 — Code-review hygiene
- **`services/config.py`** extended: cross-cutting `RISK_*` (max-conf-mult,
  Kelly cap, mult ceiling, portfolio heat, slippage), `ML_MULT_*` envelope,
  `CHAT_MODEL`/`CHAT_MAX_TOKENS`. auto_trader, ml_scorer, routers/chat
  import from config. Feature-local thresholds intentionally stay
  co-located with their logic.
- **JSON-formatted logs** to stdout (env-gated via `LOG_JSON=1`, default
  on for Cloud Run). Cloud Logging parses each line into structured
  fields (severity, logger, message + any `extra` kwargs) instead of
  regex-grepping flat strings. On-disk rotating file stays plaintext.
- **Backtester sanity**: skip bars where `High < Low` or `Volume <= 0`
  before simulating fills. Yahoo / Alpaca occasionally emit malformed
  bars from corporate-action adjustments or zero-volume halts.

### Revision 20 — ML training resilience (background thread + DB persistence)
- `POST /api/ml/train` now returns `{accepted: true}` immediately and
  trains in a background thread. Cloud Run's 300s request timeout was
  shorter than the training run; sync mode failed.
- New `MLArtifact` table: model bytes (text), meta JSON, status JSON
  persisted in Postgres. Single-row-per-name pattern with upsert. Cloud
  Run /tmp is per-instance, so a model trained on instance A was
  invisible to instance B; durable storage in DB fixes this.
- Scorer hydrates model from DB into local /tmp on first inference if
  the file is missing.
- `get_status()` reads DB first, falls back to local file. New
  `/api/ml/status` endpoint exposes training progress (queued |
  collecting | training | done | error).

### Revision 19 — ML scorer (LightGBM, shadow mode) + Alpaca tape microstructure
- `services/ml_features.py` — single feature-extractor used at both
  train and inference time. ~30 features: technicals, macro proximity,
  VIX regime, correlated-asset 20d returns (GLD/SLV/USO/UUP/TLT/QQQ/SPY),
  signal shape, microstructure (Alpaca tape, 30-min lookback), analyst.
- `services/alpaca_tape.py` — pulls Alpaca SIP-tape trades. Per-day cache
  during training; 60s live cache for inference.
- `services/ml_trainer.py` — walk-forward over historical daily bars,
  generates labeled samples (BUY/SELL win-loss within 10-bar horizon),
  4-fold chronological CV, persists LightGBM `model.txt` + `meta.json`.
- `services/ml_scorer.py` — lazy-loads model, predicts P(win), maps to
  multiplier 0.88..1.12. **Shadow mode default** — predictions logged
  to `ml_predictions` but multiplier returns 1.0 unless
  `cfg.ml_scoring_enabled = True`.
- Wired into `signal_generator` BUY + SELL paths. Reasoning line
  surfaces "🤖 ML P(win)=X (shadow|×N.NN)".
- `MLPrediction` table; `auto_trader_config.ml_scoring_enabled` flag.
- Routers `/api/ml/{train, scorecard, predict/{ticker}, calibration}`.
- Weekly retrain (Sun 06:00 UTC); 30-min outcome backfill that joins
  predictions to closed AutoTrades.

### Revision 18 — Macro release calendar + blackout gates
- `MacroEvent` table + `services/macro_calendar.py`. 60-day rolling
  window of US releases (NFP/CPI/PPI/FOMC/PCE/GDP/ISM/Sentiment) from
  recurrence rules + hardcoded FOMC list.
- Pre/post-release blackout: 30m / 60m for high-importance, 15m / 30m
  for medium. Options paths use 1.5× window for IV-crush + gamma.
- Wired into `consider_signal`, `consider_put_play`, `consider_call_play`.
- Daily 05:00 UTC populate; 15-min FRED actuals fetch (no-op without
  `FRED_API_KEY`).
- `/api/macro/{calendar, recent, blackout, refresh, fetch-actuals}`.

### Revision 17 — Analyst ratings as a signal input
- `AnalystRating` table + `services/analyst_ratings.py`. Pulls
  `recommendationMean`, `recommendationKey`, analyst count, target from
  yfinance `.info`. Refreshed 4× daily for watchlist + candidate pool.
- `rating_multiplier(ticker, direction)` returns 0.88..1.10 envelope for
  signal-generator confidence. Asymmetric — disagreement penalty heavier
  than agreement boost.
- `/api/analyst-ratings/{ticker, /{ticker}/refresh, /refresh-all}`.

### Revision 16 — In-app Claude chat widget
- New `/api/chat` SSE endpoint (Anthropic SDK, Opus 4.7, adaptive
  thinking, prompt caching on context snapshot).
- Floating chat button in the SPA. Streaming UI, theme-aware.
- Context: live config + open positions + last 25 closed trades + alerts.
- `ANTHROPIC_API_KEY` env var enables; "not configured" UX otherwise.

### Revision 15 — Trade frequency increase + readiness
- Watchlist scan cadence 15m → 5m (3× more entry opportunities).
- Universe top-N 30 → 50 candidates per scan.
- `max_concurrent_positions` 10 → 15.
- Surfaced `max_concurrent_positions`, `daily_loss_limit_pct`,
  `flatten_by_eod` in `/auto/status` config dict.

### Revision 14 — Losing-trade post-mortem fixes
- Options conf floor: aggressive 45 → 60, non-aggressive 0.7× → 0.85×.
  Prevents conf-53 entries with weak volume.
- `expirations[:3]` MIN_DTE bypass closed in options_analyzer.
- EOD guard: refuse new options entries within 45 min of close
  (`paper_trader.minutes_to_close()`).
- Post-mortem for options: anchors ATR/stop/target analysis to underlying
  instead of premium. Direction-aware path analysis (Low vs T1 for SHORT).

### Revision 13 — Critical audit fixes
- Multiplier stack cap at 2× (raw conf × kelly × cal × strategy × VIX).
- WF confidence fold-count penalty.
- Theta-efficiency weeklies: skip dte_score double-count for DTE ≤ 7.
- Chandelier activates from bar 1 with 0.5R favor gate.
- Universe scanner price floor $5 → $10; sub-$20 score penalty.
- Reverse-thesis gate raised: same-or-higher TF + ≥80 conf.
- Signal freshness 2× TF (cap 240m) → 1× TF (cap 90m).
- Options trim uses `original_qty` instead of current qty.

### Revision 12 — Cloud SQL migration + stability
- Migrated DB from Neon to Cloud SQL `stockrecs-db` (us-central1, db-g1-small).
- 1.9MB dump + 6 tables, zero data loss.
- Pool config: `pool_size=8`, `max_overflow=7`, `pool_recycle=3600`.

### Revision 11
- Collapsible scrolling frames for Auto-Trades / Positions / Orders
- `CollapsibleSection` reusable component

### Revision 10
- AutoTraderPanel modernization: hero cards, gradient gauges, trade
  cards, skeleton loader, config drawer, lazy Put-Play Watch

### Revision 9
- News ingestion phase 1: Alpaca News poller, VADER sentiment,
  `NewsEvent` table, /api/news endpoints, News panel + trade-context +
  alignment summary UI

### Revision 8
- Earnings-calendar gate (yfinance 48h window, stocks + puts)
- IV-vs-realized-vol gate for options (1.75×RV ceiling)
- Adaptive chandelier (ADX > 30 → ×1.33, ADX < 20 → ×0.83)

### Revision 7
- Portfolio-heat cap (10% of equity)
- Opening-15-min filter for intraday TFs
- Gap-open reject (> 2% drift)
- T2 partial trim 50% → 33% (bigger runner)
- Target recompute runs once only (level_index ≥ 3 → chandelier-only)

### Revision 6
- Reject BUY where T1 ≤ entry × 1.004 (MU fix)
- Reject stop distance < 0.8 × daily ATR (CRWV fix)
- Soft-BE at T1 (entry − 0.3×R) instead of full BE (AAPL/MRVL fix)
- NaN-safe ATR fallthrough
- Min option DTE raised 2 → 10

### Revision 5
- Chart theme-swap fix (data effect re-runs on theme change)
- Backtest chart uses themed options

### Revision 4
- Hook-ordering bug fix (split `App` dispatcher from `AuthedApp`)

### Revision 3
- `APP_API_KEY` enforced on all routers and `/ws/quotes`
- WebSocket token query-param auth
- Login screen with localStorage cache
- 401 global event → force re-login

### Revision 2
- Additional auth on analysis + options routers
- Gap-fix on websockets 14+ via `alpaca_websocket_patch.py`

### Revision 1 (initial deploy)
- Dockerfile + deploy.sh + Cloud Run `stockrecs` service created
- Multi-stage Python 3.12 image, min-instances=1
- Neon Postgres via `DATABASE_URL`
- Full budget deployed: $49,307 stocks + $49,307 options

---

## 15. Future Work

> **Detailed ML data-source backlog with cost/lift estimates lives in
> [BACKLOG.md](./BACKLOG.md).** This section keeps non-ML deferred work.

### Strategy
- Short-selling for SELL signals (currently long-only stocks).
- Debit-spread options (verticals, calendars) for defined-risk exposure.
- Portfolio-heat-aware risk-per-trade (scale down when net unrealized
  drawdown is large).
- Per-timeframe backtest blending (currently only 2y daily).

### News pipeline (already-collecting, not yet consuming in entries)
- Wire news into `consider_signal` as a reject gate (high-severity
  negative < 30 min old on a BUY candidate).
- News exit in `manage_open_positions` (flatten long on breaking
  negative news above severity threshold).
- FinBERT swap-in for VADER (75–80% sentiment accuracy vs ~65%).
- Historical news replay for backtest validation.

### UX
- Push notifications on T1/T2/T3 hits via existing WS channel.
- Chart overlay: news markers + macro-event markers at their timestamps.
- ML calibration plot in the SPA (read `/api/ml/calibration`, render bar
  chart of predicted vs actual win-rate).
