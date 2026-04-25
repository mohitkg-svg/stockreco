# Stock Recommendations & Automated Trading

A full-stack technical-analysis and automated paper-trading platform. FastAPI + SQLAlchemy backend, React (CDN) frontend, Alpaca broker, Cloud SQL Postgres, deployed to Google Cloud Run.

> ⚠️ **This is software that places real money trades.** Read the risk-warning section below before flipping `ALPACA_LIVE=1`. You can lose your entire account in a bad day.

---

## What it does

Ingests a watchlist + dynamically-discovered scanner pool (~50 liquid US equities), runs multi-timeframe technical analysis across seven timeframes (5m → 1mo), enriches with alt-data (analyst ratings, fundamentals, insider trades, retail sentiment, institutional holdings, macro calendar), generates BUY/SELL signals with explicit entry/stop/three-target geometry, and routes them through a bracket-ordered Alpaca paper trading account.

A separate ML scorer (LightGBM, shadow mode by default) layers a P(win) prediction on top of the rules-based signal stack. Frontend shows live trade rationale, post-mortems on losers, and an in-app Claude chat widget.

See [DESIGN.md](./DESIGN.md) for full architecture, [BACKLOG.md](./BACKLOG.md) for deferred work and rationale.

---

## Quick start

### Prerequisites

- **Python 3.12** + `pip`
- **Google Cloud SDK** (`gcloud`) — for deploys
- **Alpaca Algo Trader Plus** subscription (SIP feed) — required for live quotes + tape data
- **Cloud SQL** Postgres instance — connection string in `DATABASE_URL`

### Run locally

```bash
# 1. Clone + install deps
git clone git@github.com:andyjhs1/stockrecommendations.git
cd stockrecommendations
pip install -r backend/requirements.txt

# 2. Copy env template + fill in your keys
cp .env.example backend/.env
# Edit backend/.env — see § Environment variables below

# 3. Run the backend (uvicorn picks up backend/.env automatically)
cd backend
uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# 4. Open the SPA
open http://localhost:8080
```

### Run regression tests

```bash
cd backend
DATABASE_URL="sqlite:///$(mktemp)" APP_API_KEY=test \
  python3 -m unittest tests.test_bug_scenarios tests.test_smoke -v
```

There are 78+ tests covering the bug families that have produced production losses. They run in ~3s. The deploy script (`./deploy.sh`) gates every deploy on these passing — set `SKIP_TESTS=1` to override.

---

## Deploy

### One-shot Cloud Run deploy

```bash
./deploy.sh us-central1
```

This:
1. Runs the regression test suite (refuses to deploy on failure)
2. Builds + pushes a Docker image via Cloud Build
3. Deploys to the `stockrecs` service in the named region
4. Verifies health by hitting `/api/health`

### Architecture: API service + Manager service (dual-process)

Production runs **two** Cloud Run services that share the same Cloud SQL database:

| Service | RUN_MODE | What it does |
|---|---|---|
| `stockrecs` (existing) | `api` | HTTP API, scanner scheduler, signal generation, entries, all alt-data refresh jobs |
| `stockrecs-manager` (new) | `manager` | **Only** runs the 20s position-management loop + hourly broker reconciliation |

This separates concerns so a crash in scanning, alt-data fetching, or HTTP serving cannot leave open positions unmanaged. Both services share state via the Postgres database; only the manager service registers the `manage_open_positions` scheduler job.

To deploy the manager service:

```bash
./deploy-manager.sh us-central1
```

### Environment variables

See [.env.example](./.env.example) for the full list. Required minimums to run:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Cloud SQL Postgres connection string |
| `APCA_API_KEY_ID` + `APCA_API_SECRET_KEY` | Alpaca paper-trading credentials |
| `APP_API_KEY` | Shared-secret auth on every `/api/*` endpoint |

Optional but recommended:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Enables the in-app Claude chat widget |
| `FRED_API_KEY` | Enables fetching post-release macro values from FRED |
| `ALPACA_DATA_FEED=sip` | Enables full consolidated tape (requires Algo Trader Plus) |
| `RUN_MODE=manager` | Sets a service to manager mode (default `api`) |
| `LOG_JSON=1` | Structured JSON logs for Cloud Logging (default on) |
| `SENTIMENT_BACKEND=finbert` | Use FinBERT for news sentiment (default VADER; FinBERT requires `transformers`+`torch` install) |

### Live trading (real money)

To switch from paper to live trading, **all four** of these must be set:

```bash
ALPACA_LIVE=1
I_UNDERSTAND_LIVE_RISK=yes
APCA_API_KEY_ID=<live-account-key>
APCA_API_SECRET_KEY=<live-account-secret>
CORS_ALLOW_ORIGINS=https://your-frontend-domain
```

The boot guard in `main.py` refuses to start without all four. This is deliberate: a single `ALPACA_LIVE=1` typo in a deploy shouldn't move real money.

---

## ⚠️ Risk warnings

This software places trades automatically. **You are responsible for every trade it makes.** Specific risks:

1. **The bot can lose money fast.** The default risk-per-trade is 2% of equity. With 15 concurrent positions, a correlated drawdown can cost 30%+ of the account in a day. Beta-weighted heat caps this at 10% but the cap is not a guarantee.
2. **Cloud Run instances can crash.** A single-instance crash during volatile markets leaves positions unmanaged until restart (~30s typical). The dual-service architecture (api + manager) reduces this risk but doesn't eliminate it.
3. **Alpaca broker can reject orders.** Buying-power exhaustion, fat-finger fills, gap-throughs, and exchange halts all happen. The bot has circuit breakers (`bp_breaker_active`, `broker_down`, daily loss limit, killed flag) but these are after-the-fact protections.
4. **Options expire.** Long calls/puts can go to zero. The `MIN_DTE=10` filter and EOD guard prevent the worst patterns but don't eliminate theta decay during slow markets.
5. **Real-time data can lag.** WebSocket disconnects, feed staleness, and Yahoo rate limits all cause stale quotes. A `stream_stale` alert fires above 30s but the bot may have already acted on stale prices.

**Recommended pre-live ritual:**
1. Run in paper for at least 4 weeks with **real-money sizing** (so realized P&L is comparable).
2. Review every loser's post-mortem (`/api/trading/auto/postmortem/{trade_id}`).
3. Verify ML calibration via `/api/ml/calibration` — predicted P(win) should track actual win-rate within ±5% per bucket.
4. Start live with `max_pct_of_equity=0.20` (20% capital deployment cap) for the first week. Scale up only after 50+ profitable closed trades.
5. Have the kill-switch URL bookmarked: `POST /api/trading/auto/kill {flatten: true}`.

---

## Repo layout

```
backend/
  main.py                  FastAPI app + scheduler bootstrap
  database.py              SQLAlchemy ORM models
  routers/                 HTTP endpoints
    trading.py             Auto-trader CRUD + rationale + kill switch
    analysis.py            Per-ticker TA + signal generation
    backtest.py            Per-ticker + portfolio backtests
    chat.py                Claude chat widget endpoint
    ml.py                  ML training + scorecard + calibration
    macro.py               Macro calendar + blackout
    fundamentals.py        Quality-score read endpoints
    social.py              Stocktwits + WSB + insiders + institutional
    analyst_ratings.py     yfinance recommendation read endpoints
    alerts.py              Operator alert inbox
    stream.py              WebSocket /ws/quotes
  services/
    auto_trader.py         Orchestration: consider_signal/put/call, manage_open_positions
    risk_manager.py        BP reservations, circuit breakers, multipliers, adaptive sizing
    execution_engine.py    Alpaca broker ops (replace_stop, force_close, leg lookup)
    position_manager.py    Chandelier helpers, price lookup, target recalc, reverse-thesis
    signal_generator.py    Composite signal computation + multiplier stack
    backtester.py          Per-ticker walk-forward backtest
    portfolio_backtest.py  Cap-aware portfolio backtest with regime stats
    fundamentals.py        yfinance-driven fundamentals + quality_score
    ml_features.py         32-feature extractor for the ML scorer
    ml_trainer.py          Walk-forward LightGBM training
    ml_scorer.py           Lazy load + predict_winrate
    macro_calendar.py      Recurrence-rule US macro calendar + blackout
    insider_trades.py      SEC Form 4 EDGAR scraper
    social_sentiment.py    Stocktwits public API
    wsb_scraper.py         Reddit r/wallstreetbets mention counter
    institutional.py       yfinance institutional_holders + 13F proxy
    analyst_ratings.py     yfinance recommendationMean
    sentiment.py           Pluggable VADER/FinBERT
    schemas.py             Pydantic models for new code
    config.py              Cross-cutting tuning knobs
    risk_math.py           Pure-function risk helpers (idempotency, Kelly)
    metrics.py             In-memory counter
    alerts.py              Alert dispatcher (DB + optional webhook)
  tests/
    test_bug_scenarios.py  Loss-driven regression suite (78+ tests)
    test_smoke.py          Idempotency + smoke tests
frontend/
  index.html               React CDN + Tailwind shell
  app.js                   3,400 LOC React SPA — chat widget, trade cards, charts
deploy.sh                  Cloud Run deploy for the api service
deploy-manager.sh          Cloud Run deploy for the manager service
DESIGN.md                  Architecture document — read this for the full picture
BACKLOG.md                 Deferred work + rationale for each defer
README.md                  This file
.env.example               Environment-variable template
```

---

## Useful endpoints

- `GET /api/health` — liveness + degradation flags + alert count
- `GET /api/trading/auto/status` — config + budget + open positions
- `POST /api/trading/auto/config` — update any config field
- `POST /api/trading/auto/kill` — emergency flatten + cancel all
- `GET /api/trading/auto/rationale/{trade_id}` — full "why this trade?" view
- `GET /api/trading/auto/pdt` — PDT day-trade count for trailing 5 business days
- `GET /api/ml/calibration` — predicted vs realized win-rate buckets
- `GET /api/ai-judge/decisions` — Claude entry-veto / news-exit / sizing-mult audit log
- `GET /api/ai-judge/summary` — aggregate stats per call site + mode
- `GET /api/ai-judge/modes` — current mode (off / shadow / active) per call site
- `POST /api/backtest/portfolio/run` — book-level walk-forward with cap enforcement
- `GET /api/backtest/portfolio/stress-windows` — list canned historical drawdown periods
- `GET /api/macro/blackout` — am I currently in a pre/post-release window?
- `GET /metrics` — Prometheus-format internal counters

---

## Database backup + restore

Cloud SQL Postgres takes automatic daily backups (7-day retention by default).
The instance also has point-in-time recovery (PITR) enabled — you can restore
to any second within the retention window.

### One-shot manual backup (before risky migrations)

```bash
# List the instance name
gcloud sql instances list

# Trigger an on-demand backup
gcloud sql backups create \
  --instance=stockrecs-db \
  --description="pre-migration $(date +%Y%m%d)"

# List backups + IDs
gcloud sql backups list --instance=stockrecs-db
```

### Restore to a new instance (test recovery before doing it live)

```bash
gcloud sql backups restore <BACKUP_ID> \
  --restore-instance=stockrecs-db-restore \
  --backup-instance=stockrecs-db
```

### Local dump (for off-cloud archive)

```bash
# Read DATABASE_URL from .env, then:
pg_dump "$DATABASE_URL" --no-owner --no-acl --clean --if-exists \
  --file="stockrecs-$(date +%Y%m%d).sql"
```

Restore the local dump into a fresh Postgres with `psql "$DATABASE_URL" < dump.sql`.

### What's actually in the DB

- `auto_trades` — every entry, fill, exit (the audit trail of the bot's behavior)
- `signals` — every emitted signal, including NEUTRAL
- `ai_decision_log` — every Claude judge call
- `news_events` — ingested Alpaca + scraped articles
- `alerts` — operator alert inbox

`auto_trades` is the most important table to preserve. The rest can be regenerated.

Every `/api/*` endpoint requires the `X-API-Key: <APP_API_KEY>` header.

---

## Acknowledgements

Built collaboratively with Claude over several sessions. Real-money trading discipline contributed by getting paper-traded losses critiqued post-mortem and the codified into [tests/test_bug_scenarios.py](./backend/tests/test_bug_scenarios.py) so the same bug never costs money twice.
