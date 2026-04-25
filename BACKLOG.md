# ML Data-Source Backlog

Future enhancements to the ML model that we discussed and chose to defer.
Listed in **descending order of expected win-rate lift per dollar of cost**.

## Tier 1 — ✅ DONE (2026-04-25)

All three free high-impact sources shipped together:

### ✅ SEC Form 4 — Insider trades
- `services/insider_trades.py` — parses EDGAR Atom feed per CIK, extracts
  Form 4 nonDerivativeTransaction codes (`P` = buy, `S` = sell), aggregates
  30d/90d buy-counts and net-buy-ratio with $ value.
- Weekly refresh Sun 04:45 UTC (SEC rate-limits 10 req/s → serial with 200ms pacing).
- `InsiderSummary` table, `/api/insider/{ticker}` + `/refresh-all` endpoints.
- `insider_multiplier(ticker, direction)` — 0.97..1.06 envelope;
  requires ≥3 transactions for signal. BUY: ≥70% buy-ratio → 1.06, ≤30% → 0.97.

### ✅ Stocktwits — Retail sentiment
- `services/social_sentiment.py` — calls public Stocktwits stream API,
  aggregates last 24h bullish/bearish tagged messages.
- 4×/day refresh (12/15/18/21 UTC).
- `SocialSentiment` table, `/api/social/sentiment/{ticker}` endpoints.
- `sentiment_multiplier(ticker, direction)` — 0.96..1.04 envelope;
  requires ≥20 messages/24h to trust lean; ≥60% tilts the multiplier.

### ✅ Short interest — via Fundamentals
- Added `short_pct_float` + `short_ratio` columns to Fundamentals (no new
  module — yfinance .info already provides both, refreshed weekly with
  the rest of the fundamentals).
- `short_interest_multiplier(ticker, direction)` — 0.92..1.02 envelope.
  BUY: ≥25% of float shorted → 0.92 (fundamental skepticism); 15-25% → 1.02
  (squeeze tilt). SELL: mirror — already-crowded shorts = 0.92 (late-to-party).

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
- **Status**: **Formally deferred** after re-evaluation on 2026-04-25.
- **Surface area**: 10+ routers, 15+ services, every yfinance / httpx /
  Alpaca SDK call. Estimated ~3 full days of code + ~1 day of regression
  hunting. Many call sites would need conditional sync/async variants
  during the migration window.
- **Benefit at current traffic**: negligible. Single-user app on Cloud Run
  with min-instances=1. ThreadPoolExecutor at 4 workers + scheduler-driven
  background jobs handle the load with ~20% CPU headroom during peak scans.
  No thread-exhaustion symptoms observed in logs.
- **Concrete trigger to revisit**: any of these would tip the trade-off:
  - `/api/health` reports sustained 95p latency > 2s
  - Cloud Run `container_instance_count` trending above 2 during market hours
  - Concurrent-user count > 5
  - Scheduler job misfires due to thread pool saturation
- **Doing it wrong costs more than doing it late.** Half-migrated async code
  (some awaited, some not) is the single most common source of "works in
  tests, deadlocks in prod" failures in Python web apps. Not worth the
  risk days before real-money trading.

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

## Strategy-review backlog (2026-04-25, second external pass)

Items from a second external review covering entry / exit / target /
backtest strategy. Graded and applied the high-ROI ones; deferring the
rest either because they're already-done (reviewer didn't know) or wrong
timing pre-real-money.

### Already applied

- ✅ **ATR-capped Soft BE** — trail-to-`entry − max(0.3R, 0.25×ATR)` at T1
  so high-volatility names aren't chopped out on noise. Had been patched
  already but left duplicate code; cleaned up.
- ✅ **Beta-weighted portfolio heat** — added `beta` column to
  `Fundamentals` (fetched weekly via yfinance), and the 10%-of-equity
  heat cap now multiplies each open trade's dollar-at-risk by
  `clamp(beta, 0.5, 2.0)`. Five high-beta tech longs now contribute more
  heat than five utilities at the same raw $-at-risk.

### Rejected as already-done

- ❌ **Volume Profile targets** — reviewer said "currently uses price
  structure (S/R)". False: `signal_generator.py:645` and `:811` already
  pull POC/VAH/VAL from `services/volume_profile.compute_volume_profile()`
  and include them in target candidates.

### Deferred — valid ideas, wrong timing

- **Portfolio-level backtest with correlation** — `backtester.py` currently
  evaluates tickers in isolation. A portfolio backtest that respects the
  sector cap + heat cap during historical periods (Aug 2024 carry-trade
  unwind, etc.) would tell us if our rules actually protect the account
  in a correlated drawdown. 2-3 days work; revisit post-real-money.
- **Vertical debit spreads** for long-call/long-put replacement — reduces
  theta drag on runner positions. Requires multi-leg Alpaca orders,
  spread-aware strike selection, spread-adjusted targets. ~3 days.
  Already in this BACKLOG's earlier section.

### Rejected — low ROI or empirically weak

- **Join-the-Bid entry** — bid-chase with cancel/replace. Saves
  ~$20-50/trade on slippage at the cost of a fragile order state machine.
  The existing `limit_at_mid` captures most of the spread win at a fraction
  of the complexity.
- **Time-decay stop** — incrementally tighten stop each bar that fails
  to hit T1. Intuitive but empirically weaker than fixed stops — premature
  tightening chops the trade out on normal noise before the thesis plays
  out. Not supported by studies.
