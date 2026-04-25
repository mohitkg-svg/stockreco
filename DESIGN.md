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
| `flatten_by_eod` | false | 15:55 ET liquidation (intraday mode) |

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

### 4.5 `NewsEvent` (new)
One row per Alpaca news article, de-duped on `external_id`.

| Field | Purpose |
|---|---|
| `external_id` | Alpaca article id (unique, indexed) |
| `ticker` / `symbols` | Primary + all mentioned tickers |
| `source` / `author` | Feed + byline |
| `headline` / `summary` / `url` | Article content |
| `published_at` | Article timestamp, indexed |
| `fetched_at` | When our poller ingested it |
| `sentiment_score` | VADER compound ∈ [-1, +1] |
| `sentiment_label` | positive / negative / neutral |
| `severity` | `abs(score)` × 100, 0–100 |

Not linked to trades via FK — join at query time by ticker + time overlap
so the news-context query works for trades that closed before news
ingestion started.

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
| 6 | **Portfolio heat ≤ 10% of equity** | New: Σ live $-at-risk bounded |
| 7 | **Opening-15-min filter** (intraday TFs 9:30–9:45 ET) | New: whipsaw window |
| 8 | Signal freshness (age ≤ 2× timeframe, clamped 15m–4h) | Prevents stale entries |
| 9 | Timeframe in `signal_timeframes` | Default 1h / 4h / 1d only |
| 10 | Stop geometry sane (`stop < entry`, risk 0.1–10%) | Fat-finger guard |
| 11 | **T1 > entry × 1.004** | New: catches inverted-target bugs (MU-style) |
| 12 | Per-ticker `auto_trade_enabled` | Per-symbol gate |
| 13 | No existing open/pending trade on this ticker | One-per-ticker |
| 14 | Idempotency hash not seen in last 12h | Dedupe retries |
| 15 | Sector count < `max_per_sector` (5) | Correlation cap |
| 16 | **Stop distance ≥ 0.8 × daily ATR** | New: rejects too-tight stops |
| 17 | **Gap-open ≤ 2%** from signal entry | New: rejects stale-entry signals |
| 18 | **No earnings within 48h** (yfinance) | New: event-driven variance |
| 19 | Position qty ≥ 1 after sizing | Capital check |

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
   - **T1**: trim 1/3 at market → **soft BE** at `entry − 0.3×initial_risk`
     (not full entry — post-mortem found full BE chopped out winners on 1%
     retraces). If T1 is < 0.5×ATR from entry (NaN-safe check), BE is
     skipped and the chandelier overlay takes over.
   - **T2**: trim 33% of remaining runner → stop to **entry (full BE)**.
     Runner now ~45% of original position.
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
- Underlying hits T1/T2/T3 → trim half contracts on T1, trail underlying-stop tighter
- Premium decay ≥ 50%
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

### Entry-side gates (17 total)
Confidence, timeframe allow-list, signal freshness, timeframe-of-day (9:30–9:45 ET skip), geometry (stop < entry, T1 > entry × 1.004, risk-per-share 0.1%–10%), stop-vs-ATR ≥ 0.8×, gap-open ≤ 2%, earnings < 48h, idempotency, per-ticker cap, sector cap, concurrent cap, portfolio-heat cap, daily-loss cap, fat-finger guard, BP circuit breaker.

### Exit-side guarantees
SL-invariant check (resubmit if broker drops the leg), slippage reject, reverse-thesis close, stale-trade recycle, debounced target touches, atomic stop-replacement (broker ack gates the DB update), adaptive chandelier never loosens existing stop.

### Sanity
- Two-key live-trading gate (`ALPACA_LIVE=1` + `I_UNDERSTAND_LIVE_RISK=yes` + `APP_API_KEY`).
- Persistent kill switch — survives deploys; unkill does NOT re-enable (two-step re-arm).
- Idempotency hash deduping retries within 12h.
- Post-mortem auto-generated on every losing stop.

### Auth & access
- Shared-secret `APP_API_KEY` gating all `/api/*` and `/ws/quotes`.
- Frontend login screen with localStorage cache.
- 401-global-event flips UI back to login.

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

| Var | Purpose |
|---|---|
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Alpaca credentials |
| `ALPACA_LIVE` | "1" to flip from paper to live |
| `I_UNDERSTAND_LIVE_RISK` | "yes" second-key live-gate |
| `APP_API_KEY` | Shared-secret auth; empty = dev mode open |
| `DATABASE_URL` | Postgres connection string (Neon) |
| `CORS_ALLOW_ORIGINS` | Comma-separated origins |
| `LOG_DIR` | Rotating log location |

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

### Post-loss triage
1. Check `/api/trading/auto/trades` for the losing trade id.
2. Expand it in UI: **"Why did this lose?"** shows the auto-generated
   post-mortem with verdict, findings (severity-tagged), and lessons.
3. Click **"News during trade"** to see pre / during / post articles with
   sentiment — catches event-driven losses the post-mortem can't know.
4. Correlate across trades: Trading view → `News ↔ Trade Alignment`
   summary (3 / 7 / 14 / 30 day windows).

### Config tuning
- Budget changes: Trading view → Auto-Trader panel → ⚙ Config drawer.
  Changes hit `POST /api/trading/auto/config` and apply from the next
  scan tick.
- Kill switch: `POST /api/trading/kill` (optionally `flatten=true`).
  Survives restarts. Unkill with `POST /api/trading/unkill` then flip
  `enabled=true` via `/auto/config` (deliberate two-step).

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
