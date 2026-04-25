# ML Data-Source Backlog

Future enhancements to the ML model that we discussed and chose to defer.
Listed in **descending order of expected win-rate lift per dollar of cost**.

## Tier 1 — Free, high-impact (build next)

### SEC Form 4 — Insider trades
- **Source**: SEC EDGAR RSS (`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&dateb=&owner=include&count=40`)
- **Frequency**: real-time, push-style RSS
- **Cost**: $0
- **Expected lift**: +2–3% win-rate on mid/small caps where insider buying is informative
- **Features to add**:
  - `insider_buy_count_30d` — number of distinct insider purchases in last 30 days
  - `insider_buy_dollar_30d` — total $ value of insider purchases
  - `insider_net_buy_ratio` — buys / (buys + sells)
- **Risks**: noisy on mega-caps (insiders sell on schedule via 10b5-1 plans, not sentiment)

### Stocktwits API — Retail sentiment
- **Source**: `https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json`
- **Frequency**: 30-min polling
- **Cost**: $0 (rate-limited; ~200 req/hr)
- **Expected lift**: +1–2% win-rate, primarily on retail-driven names (small/mid caps + meme tickers)
- **Features to add**:
  - `st_message_count_24h` — message volume (engagement spike = squeeze risk)
  - `st_bullish_pct` — % of messages tagged Bullish (Stocktwits provides per-message sentiment)
  - `st_bullish_pct_7d_change` — sentiment trend
- **Risks**: low signal on AAPL/NVDA where the tape itself reflects retail flow already

### FINRA Short Interest
- **Source**: `https://api.finra.org/data/group/otcMarket/name/regShoDaily` (registration required, free)
- **Frequency**: bimonthly publish, daily indicative via FINRA REGT
- **Cost**: $0
- **Expected lift**: +1% baseline, +5% on names with high short interest going into a BUY signal
- **Features to add**:
  - `short_interest_pct_float` — % of float shorted
  - `days_to_cover` — short interest ÷ avg daily volume
- **Risks**: bimonthly cadence makes it stale; high SI is two-sided (squeeze risk + fundamental skepticism)

## Tier 2 — Free, moderate-impact

### r/wallstreetbets ticker mention scraper
- **Source**: Reddit JSON API (no auth) on `r/wallstreetbets/new.json`
- **Frequency**: 5-min poll
- **Cost**: $0
- **Expected lift**: +1–3% on retail-driven tickers, near-zero on the rest
- **Features to add**:
  - `wsb_mentions_24h` — count of post titles + comment top-level mentions
  - `wsb_mentions_7d_zscore` — z-score vs 30-day baseline (catches squeeze setups)
- **Risks**: easy to overfit; signal is bimodal (great for meme stocks, noise on liquid mega-caps)

### Form 13F — Quarterly institutional holdings
- **Source**: SEC EDGAR
- **Frequency**: quarterly (45 days post quarter-end)
- **Cost**: $0
- **Expected lift**: +1% headline; useful as a slow-moving regime feature
- **Features**: `inst_ownership_pct_change_qoq`, `top_10_holder_count_change`
- **Risks**: too slow-moving to be a primary signal

## Tier 3 — Paid, higher-impact

### Cheddar Flow / SpotGamma — Options flow
- **Source**: subscription APIs
- **Cost**: $100–300/mo
- **Expected lift**: +3–5% on names where institutions are positioning; lower on others
- **Features**: dark-pool prints, unusual options activity, gamma exposure levels
- **When to subscribe**: only after free additions have been deployed and shown actual realized lift

### Polygon.io — Full Level 2 + historical tape
- **Cost**: $199/mo
- **Use case**: would replace Alpaca tape. Probably not worth the cost given we already have SIP via Alpaca AT+.
- **Defer indefinitely** unless we discover Alpaca's tape latency is a problem.

## Backlog roadmap (when to revisit)

1. After v1 ML ships and runs in shadow mode for **1 week**: review `/api/ml/calibration` — if predicted vs actual win-rate buckets are well-calibrated, flip `ml_scoring_enabled=True` and start using it live.
2. **Week 2-3**: add Tier 1 free sources (Form 4, Stocktwits, FINRA SI). Retrain. Measure marginal lift.
3. **Week 4+**: if marginal lift from Tier 1 is real, evaluate whether Tier 3 paid feeds are economically justified given account size.

## Related deferred items (from DESIGN.md §15)

- **FinBERT** swap for VADER (75–80% sentiment accuracy vs ~65%)
- **Debit spreads** for defined-risk exposure
- **Portfolio-heat-aware risk-per-trade**
- **Push notifications** on T1/T2/T3 hits

## Architecture / refactor backlog

Items raised in the 2026-04-25 external code review. Right idea, deferred
because the timing is wrong (don't refactor right before / during real-money
rollout). Revisit ~4 weeks after live trading is stable.

### Encapsulate auto_trader globals into a class
- **What**: Replace module-level `_entry_lock`, `_bp_exhausted_until`, the
  caches, etc. with an `AutoTraderService` class. State becomes explicit;
  makes future testing tractable.
- **Why defer**: ~1k LOC of churn for zero functional benefit. No test suite
  exists, single FastAPI process, Cloud Run cold-starts (no hot-reload concern).
  Risk of regression > value of the refactor right now.

### Decompose auto_trader.py (3,179 LOC) into focused modules
- **Proposed split**:
  - `risk_manager.py` — budget, sector caps, multiplier stack, sizing
  - `execution_engine.py` — paper_trader interactions, order submission
  - `position_manager.py` — `manage_open_positions` state machine
  - `auto_trader.py` (slim) — orchestration only
- **Why defer**: Refactoring the highest-stakes file in the system right
  before going live with real money is the worst possible timing. Plan for
  4–6 weeks post-live once behavior is stable and well-monitored.

### Pydantic models for internal data shapes
- **What**: Replace `Dict[str, Any]` with `SignalData`, `TradeContext`, etc.
  Catches `KeyError` at construction time instead of randomly during a trade.
- **Why defer**: Mass refactor affecting most of `signal_generator`,
  `auto_trader`, `routers/trading`. Better to add models for *new* code first
  and migrate existing call sites incrementally.

### Async I/O migration (httpx.AsyncClient + await endpoints)
- **What**: Move broker / data-fetch calls to async; use `await` in routers.
- **Why defer**: ThreadPoolExecutor at 4 workers handles single-user scale
  fine. Migration is large surface area, high regression risk, marginal
  benefit until traffic is much higher. Revisit if we ever hit thread
  exhaustion symptoms.

### Priority-queue scanner (vs flat 5min watchlist scan)
- **Proposal**: scan tickers with open positions every 1m, high-conf signals
  every 5m, general watchlist every 15m.
- **Why reject**: open positions are already managed every **20 seconds** via
  `_scheduled_manage`, not via the watchlist scanner. The proposal solves a
  problem we don't have. Watchlist 5m + universe-pool 4×/day is right-sized.

## Already accepted from this review (done in commit 2026-04-25)

- ✅ Centralize cross-cutting magic numbers in `services/config.py`
  (`RISK_*`, `ML_MULT_*`, `CHAT_*`). Feature-local thresholds intentionally
  left co-located with their logic.
- ✅ Backtester: skip bars where `High < Low` or `Volume == 0` before
  simulating fills.
- ✅ Structured JSON logging to stdout (env-gated via `LOG_JSON=1`, default
  on for Cloud Run). Cloud Logging now parses each line as a structured
  record with `severity`, `logger`, `message`, and any `extra` fields.
  On-disk rotating file stays plaintext for human grep.
