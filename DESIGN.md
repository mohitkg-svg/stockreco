# Stock Recommendations & Automated Trading — Design Document

> A full-stack technical-analysis and automated paper-trading platform.
> FastAPI + SQLAlchemy backend · React (CDN) frontend · Alpaca broker · Neon Postgres.

---

## 1. Mission & Scope

The system ingests a user-managed watchlist, runs multi-timeframe technical
analysis across seven timeframes (5m → 1mo), generates directional signals
with explicit entry / stop-loss / three targets, and optionally routes those
signals into a bracket-ordered paper trading account at Alpaca. Capital is
deployed across both stocks and options (long puts synthesised from bear
theses on tickers with no BUY). All trades are tracked in a first-party
ledger that supports trailing stops, partial profit-taking, stale-trade
recycling, post-mortems, and correlation/sector caps.

The platform runs on a single FastAPI process that serves the REST API, a
WebSocket quote stream, and the static React SPA from port 8000.

---

## 2. System Architecture

```
                              ┌────────────────────────┐
                              │ Browser (React SPA)    │
                              │  · Lightweight-Charts  │
                              │  · Tailwind (CDN)      │
                              └──────────┬─────────────┘
                                         │ REST + WebSocket
            ┌────────────────────────────┴────────────────────────────┐
            │                     FastAPI process                     │
            │                                                         │
            │  Routers:  watchlist · analysis · options · trading     │
            │            backtest · stream · health                   │
            │                                                         │
            │  Services: signal_generator · auto_trader · paper_trader │
            │            data_fetcher · live_quotes · backtester      │
            │            options_analyzer · bear_thesis · post_mortem │
            │            indicators · support_resistance · fibonacci  │
            │                                                         │
            │  Scheduler (APScheduler):                                │
            │    · watchlist_scan  every 15m  (parallel 4x)           │
            │    · auto_trader_manage  every 60s                      │
            │    · calibration_job  03:10 UTC nightly                 │
            └────────┬───────────────────────────┬────────────────────┘
                     │                           │
                     │                           │
           ┌─────────▼──────────┐       ┌────────▼────────┐
           │ Alpaca Paper API   │       │ Neon Postgres   │
           │  · TradingClient   │       │ (or SQLite dev) │
           │  · WS market data  │       └─────────────────┘
           └────────────────────┘
                     │
           ┌─────────▼──────────┐
           │ Yahoo Finance v8   │
           │ (OHLCV, options,   │
           │  metadata; rate-   │
           │  limited 30/min)   │
           └────────────────────┘
```

### 2.1 Runtime Topology
- **Single FastAPI process** on port 8000 — combines API, WebSocket
  broadcast, APScheduler jobs, Alpaca WS client, and SPA static hosting.
- **Thread model**: FastAPI event loop + `ThreadPoolExecutor` workers for
  scans, post-mortems, and overview-price parallelism.
- **Database**: Neon Postgres in production (pool_size=5, pool_pre_ping=True
  for serverless-wake tolerance); SQLite WAL for local development.
- **Static assets**: `frontend/` is mounted at `/` — `index.html` bootstraps
  a Babel-standalone-transpiled `app.js` with a cache-bust query param.

---

## 3. Data Model

Persisted in `backend/database.py` via SQLAlchemy.

### 3.1 `WatchlistStock`
The canonical watchlist. `auto_trade_enabled` acts as a per-ticker gate
that the auto-trader honours even when the global switch is on.

### 3.2 `Signal`
One row per (ticker, timeframe, generation time). Captures direction,
confidence, entry / stop / T1 / T2 / T3, reasoning text, detected patterns,
and the strategy that produced it. Backtest metadata (win rate, best
strategy, score) is blended in when available.

### 3.3 `AutoTraderConfig`
Singleton row (id=1) holding all runtime-tunable parameters:

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
| `max_concurrent_positions` | 15 | Hard cap across portfolio |
| `max_per_sector` | 5 | Soft correlation cap |
| `stop_atr_mult` | 2.0 | Default stop distance in ATR units |
| `chandelier_atr_mult` | 3.0 | Trailing stop overlay (0 = off) |
| `signal_timeframes` | "1h,4h,1d" | Eligible timeframes for entry |
| `trade_options` | **true** | Enable PUT auto-buy |
| `flatten_by_eod` | false | 15:55 ET liquidation (intraday mode) |

### 3.4 `AutoTrade`
Per-entry lifecycle. Tracks the trade from `pending` → `open` → one of
`closed_target`, `closed_stop`, `closed_reverse`, `closed_stale`,
`closed_slippage`, `closed_manual`, or `error`. Key fields:

- `entry_price` / `requested_entry` — fill vs signal
- `stop_loss` (original) / `current_stop` (mutated by trailing)
- `target1/2/3` + `level_index` (state machine cursor)
- `high_water_mark` / `low_water_mark` — chandelier calculation
- `realized_pl` — supports partial fills (T1 + T2 trims accumulate here)
- `parent_order_id`, `stop_order_id`, `tp_order_id` — broker refs
- `idempotency_key` — SHA1 of ticker|side|rounded levels|tf|conf bucket|UTC day
- `sector` — captured at entry for correlation cap
- `post_mortem` — JSON analysis populated only on losing stops

---

## 4. Trading Strategy

### 4.1 Signal Generation

`services/signal_generator.py` runs a composite rule-based evaluation per
timeframe, blending:

1. **Trend regime**: EMA20/50/200 alignment
2. **Momentum**: RSI, MACD, ROC
3. **Volatility regime**: ADX (< 20 = chop → mean-reversion bias; > 25 =
   trend → breakout bias)
4. **Structure**: pivot points (R1/S1), swing levels, support/resistance
   clusters, Fibonacci retracements and extensions, gap/fair-value-gap
   magnets, supply/demand zones
5. **Volume**: relative volume, OBV divergence
6. **Backtest blend**: strategies with ≥3 historical trades on the ticker
   get their score folded into confidence; new/chronically-losing
   strategies are down-weighted 25%

**Stop calibration** (`_calibrate_long_stop`): picks the second-tightest
candidate among ATR-distance, swing-low structural buffer, and 3×ATR ceiling
— drops the noisiest candidate to survive normal wicks.

**Targets**: collected from Fibonacci extensions (127.2 / 161.8 / 200%),
pivot levels (R1/R2/R3), fresh supply/demand zones, gap magnets, and swing
highs. If fewer than three valid levels exist, falls back to R-multiple
projections (1.5×, 2.5×, 4× risk).

### 4.2 Entry Logic (`consider_signal`)

Gate stack (short-circuits at first failure):
1. BP circuit breaker active? → skip
2. Config: `enabled=true`, `killed=false`, broker connected
3. Signal is BUY, `confidence >= threshold`
4. Daily loss limit not hit
5. `max_concurrent_positions` not reached
6. Signal is fresh (age ≤ 2× timeframe minutes, clamped 15m–4h)
7. Timeframe in `signal_timeframes`
8. Stop geometry sane (stop < entry, risk-per-share ∈ [0.1%, 10%])
9. Per-ticker auto-trade enabled
10. No existing open/pending trade on this ticker
11. Idempotency hash not seen in last 12 hours
12. Sector count < `max_per_sector`
13. **Profit-max sizing**: risk budget = `equity × max_risk_per_trade_pct`
    × `confidence_multiplier` × `kelly_multiplier`
    - Confidence multiplier: 1.0 at threshold → 1.75 at 100%
    - Kelly multiplier: 1.0 below 55% backtest win rate, ramps to 1.35 at
      100% win rate
14. Position qty = `min(risk_cap, remaining_budget, per_ticker_cap_30%,
    cash, buying_power)`
15. Submit **bracket market order**: entry (market), SL leg (stop at signal
    stop), TP leg parked 10× RPS away so only trailing stops close the trade

### 4.3 Exit State Machine (`manage_open_positions`, 60s cadence)

For each open trade the manage loop:

1. **Promote** pending → open on parent fill, capture filled_avg_price and
   leg ids. Reshape SL/TP qty on partial fills.
2. **Slippage guard**: if fill drifts > 1.0×ATR from requested entry,
   force-close (runaway gap); 0.3–1.0×ATR → shift all targets by the
   slippage and **cap** the stop below the original risk-per-share so we
   never tighten into the original chop range.
3. **SL invariant check**: if broker's stop order is missing (`canceled`
   / `replaced` / `rejected`) and position is naked-long, resubmit a fresh
   stop.
4. **Reverse-thesis check**: if a high-conviction opposing signal lands on
   a timeframe ≥ the trade's source (grace 60s), close at market.
5. **Stale-trade guard (profit-max)**: trades that haven't hit T1 after
   `8 × timeframe_minutes` get closed if price is not meaningfully
   winning (< 0.3×R above entry). Recycles capital into fresher setups.
6. **Trailing state machine** — requires `_TARGET_CONFIRM_TICKS=2`
   consecutive ticks above a target to suppress wick fakeouts:
   - **T1**: move stop to entry (break-even). **Partial trim**: sell
     `qty//3` at market, resize SL leg to remainder, realise partial P/L.
     If T1 < 0.5×ATR from entry, skip BE (too tight) and let the
     chandelier overlay handle trailing.
   - **T2**: move stop to T1. **Partial trim (profit-max)**: sell
     `qty × 0.5` (rounded) of remaining runner, capture the T2 win.
   - **T3**: move stop to T2 AND recompute next three targets from the
     current price using daily swing levels + gap magnets + ATR steps.
     Cycle repeats — we never sell into strength, only stops close.
7. **Chandelier overlay**: once `level_index ≥ 1`, also trail at
   `HWM − chandelier_atr_mult × ATR_14`. The tighter of state-machine and
   chandelier wins.
8. **Reconcile**: if parent/leg is filled on the broker side, compute
   realized P/L, set status (`closed_target` if positive P/L,
   `closed_stop` if negative), and enqueue a post-mortem for losing
   stops.

### 4.4 Option Trading

`consider_put_play(ticker)` runs after every per-ticker analysis when no
strong BUY exists. Builds a bear thesis (`services/bear_thesis`) and
selects the best PUT contract via `services/options_analyzer`:

- R:R ≥ 3:1 at T1
- Bid-ask spread < 5% of strike
- Score = R:R × IV × |delta| × liquidity
- DTE typically < 45 for theta-efficient decay

Exit conditions (whichever fires first):
- Underlying hits T1/T2/T3 → trails underlying-stop tighter, trims half
  contracts on T1 for partial profit
- Premium decay ≥ 50%
- Underlying breaches bear stop
- Reverse-thesis BUY signal on a higher timeframe

Orders are skipped if market is closed (Alpaca rejects option market
orders outside RTH with 42210000).

### 4.5 Budget Allocation (current config)

- Equity: **$98,613**
- Stock bucket: 50% of equity = **$49,306**
- Options bucket: 50% of equity = **$49,306**
- Max risk per trade: 2% = **$1,972**
- Per-ticker cap: 30% of stock bucket = **$14,792**
- Max concurrent positions: 15
- Max per sector: 5

---

## 5. Performance Design

### 5.1 Backend

**Parallel watchlist scan** (`scheduled_scan`): 4-worker ThreadPoolExecutor
across the watchlist — each worker runs `_run_analysis_for_ticker()` in
its own DB session. Scan finish time ≈ slowest ticker instead of
`sum(all tickers)`. Workers capped at 4 to stay inside the Yahoo token
bucket (30 req/min; each ticker fans out to 7 timeframes ≈ 28 rps).

**Parallel overview price fetch** (`/api/analysis/overview`): 8-worker
pool over `get_current_price` calls.

**TTL caches**:
- OHLCV data: per-timeframe TTL (300s for 5m → 86400s for 1mo), LRU
  capped at 512 entries.
- Backtest results: 1h TTL, keyed on `(ticker, AutoTraderConfig.updated_at)`
  so config edits auto-invalidate.
- Overview payload: 20s TTL, fingerprinted on watchlist membership.
- Price-fallback cache: 30s TTL (avoids Alpaca/Yahoo hammering from
  parallel manage-loop + signal-eval).
- Daily-ATR (chandelier): 5-minute TTL.

**Database locking**:
- SQLite WAL + 30s busy_timeout (covers Alpaca REST round-trips).
- Manage-loop snapshots trade IDs in a short session, then processes each
  trade in its own session so Alpaca calls don't hold the writer lock.

**Live quote stream**:
- Alpaca WebSocket connection for stock ticks.
- Stale-quote guard: ≥60s-old quotes treated as "no live price".
- Async recompute queue dedupes signal recalcs when price moves > 0.1%
  in 30s.

**Post-mortem async**: 2-thread pool processes loss-trade analyses off
the manage loop, so a wave of stop-outs doesn't stall management.

**In-flight BP reservation**: locally-tracked buying-power counter
subtracts just-submitted notional from Alpaca's reported BP (which lags
30–60s) — prevents a scan from sizing N orders against the same stale
snapshot before the circuit breaker trips.

### 5.2 Frontend

- **CDN React + Babel standalone**: fast cold start, no build pipeline.
  Cache-bust via `?v=${Date.now()}` so edits are visible without a hard
  reload.
- **Debounced chart fetch** (160ms): rapid timeframe clicks don't cascade
  full chart reloads.
- **AbortController** on in-flight fetches during ticker/timeframe
  changes.
- **Dogpile guards** on overview and trading-panel polling so a slow
  tick doesn't queue behind a backlog.
- **Stable hook identities** via `useCallback` / `useRef` so WebSocket
  subscriptions don't churn on unrelated state changes.
- **Visible-bar windowing**: `setVisibleLogicalRange` at load time shows
  a readable slice per timeframe (78 bars on 5m, 200 on 1d) — avoids the
  "11k 5-min bars squashed to 1px each" problem of `fitContent()`.

---

## 6. UI / UX

### 6.1 Design System

CSS variables in `index.html` define the palette for both themes:

| Token | Dark | Light |
|---|---|---|
| `--bg-0` | `#070a12` | `#f6f8fb` |
| `--surface` | rgba(17,24,39,0.72) | rgba(255,255,255,0.85) |
| `--text-primary` | `#e5e7eb` | `#0f172a` |
| `--accent` | `#3b82f6` | `#2563eb` |
| `--success` | `#10b981` | `#059669` |
| `--danger` | `#ef4444` | `#dc2626` |

Swapping `data-theme` on `<html>` flips the entire palette. A pre-paint
script in `index.html` applies the saved choice before React mounts so
there's no flash. Legacy Tailwind gray-palette classes are remapped
under `[data-theme="light"]` so existing markup Just Works in light mode.

### 6.2 Navigation

Two views switched via header tabs:
- **Charts & Analysis**: left watchlist sidebar + center chart/analysis
  panel with timeframe selector, signal card, timeframe alignment,
  options table, and backtest.
- **Trading**: auto-trader status + positions + recent orders.

### 6.3 Chart Controls

- Timeframe selector (5m–1mo)
- **Hide-all-indicators toggle** (profit-max feature): single checkbox
  collapses EMAs, MACD, RSI, support/resistance, supply/demand zones,
  Fib lines, and gaps/FVGs — leaving just candles + volume. Preference
  persisted to localStorage.
- Live-tick extension on the most recent bar (mutates high/low/close as
  WS prices arrive).

### 6.4 Trading Panel (redesigned)

- **Hero stats row**: Equity · Cash · Buying Power · Open Positions —
  each in a lift-on-hover surface card with tabular-nums and a
  secondary hint line.
- **Capital deployment gauge**: animated progress bar showing what
  % of equity is actively in positions.
- **Positions as cards** (1/2/3 column responsive grid): symbol + side
  pill, qty/avg, large P/L in color, hover-lift animation, highlighted
  ring when the selected ticker matches.
- **Recent orders** in a compact themed table with ghost-button Cancel
  for live orders.

### 6.5 Theme Toggle
Pill-shaped slider (dark circle → right, blue circle → left), in the
header. Stored in localStorage under key `theme`.

---

## 7. API Surface

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
- `POST /api/trading/auto/config` — update singleton config
- `POST /api/trading/kill` / `POST /api/trading/unkill`
- `POST /api/trading/auto/postmortem/{id}` — regenerate post-mortem

### Options
- `GET /api/options/puts-watch` — scan for bearish put plays
- `GET /api/options/{ticker}?timeframe=4h&side=auto|calls|puts`

### Backtest
- `POST /api/backtest/{ticker}` — evaluate all strategies on 2y daily

### Health & WS
- `GET /api/health` — subsystem heartbeat
- `WS /ws/quotes` — live stock tick broadcast

### Auth
All endpoints honour `X-API-Key` when `APP_API_KEY` env var is set (empty
= dev mode, endpoints open). All `/api/trading/*` endpoints carry auth
even on GETs because balances leak.

---

## 8. Observability

- **Rotating log file** (`backend/logs/backend.log`, 5MB × 5 files).
- **Rate-limited log formatter** deduplicates noisy Alpaca messages for
  60s windows.
- **/api/health** surfaces: scheduler_started, live_quotes_started,
  stream_stale_secs, last_scan_at, last_manage_at, realized_pnl_today,
  open_positions, auth_configured, alpaca_live.
- **Metrics counters** (`services/metrics.py`): `autotrade_event` tagged
  by event (opened, closed_*, sl_resubmitted, bp_exhausted, partial_t1,
  partial_t2, killed, unkilled, ...).
- **Nightly calibration job** (03:10 UTC): buckets closed trades by
  confidence and logs per-bucket win-rate + avg P/L — reveals
  miscalibration.

---

## 9. Safety & Risk Controls

- **Two-key live-trading gate**: `ALPACA_LIVE=1` + `I_UNDERSTAND_LIVE_RISK=yes` + `APP_API_KEY` set.
- **Persistent kill switch** (`killed=true`) survives deploys; unkill
  deliberately doesn't re-enable — that's a separate step.
- **Daily loss limit** (3% default): halts new entries when realized PnL
  drops past threshold; existing trades keep trailing.
- **Sector correlation cap** (5 default): prevents concentration in a
  single GICS sector.
- **Concurrent position cap** (15 default).
- **Idempotency dedup** (12h): prevents retry storms from re-opening the
  same signal.
- **Fat-finger guard**: rejects signals with risk-per-share outside
  [0.1%, 10%] of entry.
- **BP circuit breaker**: 30-minute pause after Alpaca rejects for
  insufficient buying power.
- **Slippage reject**: fills drifting >1.0×ATR flatten immediately.
- **SL invariant**: if broker drops the stop leg, we detect on the next
  60s tick and resubmit.
- **Reverse-thesis close** (60s grace): flatten when higher-TF opposing
  signal lands.

---

## 10. Deployment

### 10.1 Local / Cloud Workstation

```bash
./run.sh   # uvicorn on 0.0.0.0:8000 with venv auto-detect
```

### 10.2 Cloud Run (production)

1. `Dockerfile` multi-stage: slim python:3.12 base, install requirements,
   copy backend + frontend, expose 8080 (Cloud Run default), CMD runs
   uvicorn.
2. `cloudbuild.yaml` (or one-shot `gcloud run deploy --source .`) builds
   and deploys. Env vars (Alpaca keys, DATABASE_URL, APP_API_KEY) set via
   `--set-env-vars` or Secret Manager.
3. Cloud Run serves HTTPS on a stable `*.run.app` URL, auto-scales 0→N.
4. For the scheduler to run when instances hibernate, either:
   - Pin `--min-instances=1` (cheap-ish, ~$5/mo)
   - Or externalise cadence to Cloud Scheduler → HTTP POST /api/analysis/scan

### 10.3 Environment Variables

| Var | Purpose |
|---|---|
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Alpaca credentials |
| `ALPACA_LIVE` | "1" to flip from paper to live |
| `I_UNDERSTAND_LIVE_RISK` | "yes" second-key live-gate |
| `APP_API_KEY` | Optional auth; empty = dev mode open |
| `DATABASE_URL` | Postgres connection string (Neon) |
| `CORS_ALLOW_ORIGINS` | Comma-separated origins |
| `LOG_DIR` | Rotating log location |

---

## 11. Future Work

- Short-selling for SELL signals (currently long-only stocks; options
  fill the bearish lane).
- ML calibration model fed by `compute_confidence_calibration` output.
- Portfolio-heat-aware risk-per-trade (scale down when net unrealized
  drawdown is large).
- Options spreads (verticals, calendars) for defined-risk exposure.
- Per-timeframe backtest blending (currently only 2y daily).
- Push notifications on T1/T2/T3 hits via WebSocket.

---

_Last updated: strategy upgrade + full-budget deployment + Cloud Run
readiness._
