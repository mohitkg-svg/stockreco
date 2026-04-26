# Backlog

Consolidated register of every deferred and rejected enhancement across
five external review passes (2026-04-25). The chronological per-pass
sections below capture the original context; the **Master deferral
register** below is the canonical short-form view used for scoping.

## Working principle (2026-04-25)

**With every new revision, scan this file's ⏸️ Deferred section before
scoping the work.** Check whether any item's revisit trigger has fired,
or whether an item naturally fits the scope of what you're about to
touch. Surface candidates explicitly when proposing the plan, with
inclusion / continued-deferral rationale. Deferred items whose rationale
has gone stale should either move to ✅ done or be re-categorized as
❌ rejected — don't let the list rot into "we'll get to it eventually".

## Master deferral register (canonical short-form, current as of r39)

### ⏸️ Deferred — multi-week, gated on data accumulation

| Item | Revisit trigger |
|---|---|
| LSTM / Transformer ML hybrid | LightGBM 90+ days at 10% live blend with measurable lift |
| SHAP / LIME interpretability | After ML scorer graduation |
| Optuna hyperparam tuning | After ML scorer graduation |
| Earnings call transcript NLP | After live data accumulates enough to validate signal |
| ML scorer graduation (shadow → 10% live blend) | ≥ 200 closed trades with shadow predictions logged + AUC > 0.60 + monotonic calibration |
| IV percentile gate for option entries | After 252-day ATM-IV history ingestion (~3-5 days work) |

### ⏸️ Deferred — multi-week, gated on operational triggers

| Item | Revisit trigger |
|---|---|
| vectorbt / PyBroker backtester rewrite | Per-ticker backtest > 10s (currently ~1s) |
| Async I/O migration (httpx.AsyncClient + await) | 95p `/api/health` latency > 2s sustained, instances trending > 2, concurrent users > 5, or scheduler thread-pool misfires |
| Full pairwise correlation matrix | Concurrent positions routinely > 20 (cap is 15) |
| Debit spreads (multi-leg, defined-risk) | ~4 weeks post-real-money once naked option behavior is well-understood live |
| AutoTraderService class encapsulation | Multi-tenancy or cross-test isolation requirements |
| Decompose `consider_signal` into Gates / Sizing / Submission helpers | After paper-trade volume builds enough that each section has dedicated unit-test coverage; safe to refactor when regressions would be caught by green-vs-broken tests, not by reading 300 lines of orchestration. r40 added section dividers (§ PRE-FLIGHT / § ENTRY GATES / § AI VETO / § BUDGET + SIZING / § ORDER SUBMIT) as the visual scaffolding; full extraction is the next step but high-risk to do directly after the audit pass that just caught silent broken code in this exact function. |

### ⏸️ Deferred — cost / budget gated

| Item | Revisit trigger |
|---|---|
| Cheddar Flow / SpotGamma options flow ($100-300/mo) | After Tier 1 free alt-data shows realized lift |
| Polygon.io Level 2 + tape ($199/mo) | Defer indefinitely; Alpaca SIP covers us |

### ⏸️ Deferred — single-user / scope-mismatched

| Item | Why deferred |
|---|---|
| JWT / OAuth | X-API-Key + rate limiter sufficient for single-user. Revisit if multi-user. |
| Redis cache | Yahoo TTL cache covers hot path on a single-user app. Revisit if data fetch becomes a bottleneck. |
| Trivy / Snyk dependency scanning | Pinned requirements + GitHub Dependabot already cover this. Revisit if compliance audit requires it. |
| Sharpe-based dynamic risk | Recent-WR < 55% trigger covers similar ground. Revisit if Sharpe and WR diverge. |
| CI/CD performance threshold gate (Sharpe ≥ 1.0 etc.) | Brittle to data flakes; would block deploys on transient yfinance hiccups. Revisit if a regression that this would have caught actually slips through. |
| Variable slippage by volatility in backtest | Flat 6bps round-trip acceptable for our liquid universe. Revisit if backtest/live divergence shows up systematically. |
| Strict type hints + mypy in CI | Widespread but not enforced. Costs more in noise than it returns at this codebase size. Revisit at 50K+ LOC. |

### ❌ Rejected — won't do (with rationale)

| Item | Why |
|---|---|
| Ichimoku indicator | Duplicates ADX/MA structure already present |
| Hurst exponent regime detector | ADX-based chop signal already covers it |
| Pairs / cointegration trading | System trades individual stocks; out of scope |
| Active sector rotation | Bot reacts to signals; doesn't actively rotate |
| Latency simulation in backtest | Paper bot doesn't suffer execution latency |
| Tax modeling | Out of scope |
| Priority-queue scanner | Open positions managed every 20s already; wrong problem |
| "Rip out and rebuild" the rule engine to be ML-only | Rule engine is the deterministic floor; ML is a layer on top, not a replacement |

### Pre-live decisions still in operator's hands (not engineering work)

| Decision | Gate |
|---|---|
| Flip `AI_NEWS_EXIT_MODE=shadow` (then later active) | After ≥ 1 week of `AI_ENTRY_VETO_MODE=shadow` review |
| Flip `AI_CONFIDENCE_MULT_MODE=shadow` (then later active) | Same — entry-veto first, multiplier later |
| Promote AI judge call sites from shadow to active | After reviewing ≥ 200 decisions in `ai_decision_log` |

---

# Per-pass historical context (chronological)

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
- ✅ **Portfolio-heat-aware risk-per-trade** (r35, 2026-04-25): per-trade
  risk shrinks 0.85× / 0.60× / 0.40× as live heat crosses 50% / 70% / 85%
  of the cap. The hard reject at 100% protects the book; this softens the
  approach so the last few entries before the cap are smaller probes.
- ✅ **Push notifications on T1/T2/T3 hits** (already wired in `target_hit`
  WS broadcast → `TargetHitToasts` component → browser Notification when
  tab is backgrounded). r35 added a **`trade_closed`** event covering all
  exit paths (target / stop / reverse / theta / stale) with win/loss
  styling and sticky toast.

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
- **r35 (2026-04-25)**: First pass landed — `SignalPayload` model in
  `models.py` with required-field + range validation, validates the signal
  dict at the consume boundary in `consider_signal`. Existing callers
  still pass dicts (validation only); a malformed signal logs +
  short-circuits instead of getting silently coerced to 0 downstream.
- **Remaining** (still deferred): full migration of `signal_generator.py`
  output and `auto_trader` decision-context dicts to typed models. That's
  the multi-week refactor BACKLOG was originally about — add models for
  *new* code first and migrate existing call sites incrementally.

### Full AutoTraderService class encapsulation
- **Status**: Substantially reduced in scope after the 2026-04-25
  decomposition. Most of the module state (BP reservations, circuit
  breakers, strategy/calibration caches, chandelier caches, price
  cache, replace-stop cache) now lives in the extracted modules
  (`risk_manager.py`, `execution_engine.py`, `position_manager.py`).
- **Remaining state in auto_trader.py**: `_entry_lock` (single
  threading.Lock), `_target_touch_counts` (per-trade debounce dict),
  `_post_mortem_pool` (ThreadPoolExecutor), plus ~6 constants.
- A class wrapper around two pieces of state is pure ceremony — the
  testability benefit is already delivered by `auto_trader_state.py`
  (read-view) and `reset_for_tests()` helpers in each decomposed module.
- **Do this only if** we add multi-tenancy or cross-test isolation
  requirements. Not worth the refactor cost otherwise.

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

- ✅ **Portfolio-level backtest with correlation** (r35, 2026-04-25):
  `services/portfolio_backtest.py` with caps-aware composite simulation
  was already there; r35 added (a) **stress windows** (canned date
  ranges for Aug 2024 carry / Mar 2020 COVID / Feb 2018 volmageddon /
  Q4 2018 Powell / Aug 2015 China — replays the strategy with today's
  caps over those periods), (b) **^VIX always preloaded** so regime
  tagging is never silently None, (c) **realized pair-correlation
  diagnostic** in stats (avg + max upper-triangle correlation across
  traded tickers). Endpoint: `POST /api/backtest/portfolio/run` with
  `stress_window=<key>`. Pairwise-correlation cap enforcement remains
  Tier C deferred.
- **Vertical debit spreads** for long-call/long-put replacement — reduces
  theta drag on runner positions. Requires multi-leg Alpaca orders,
  spread-aware strike selection, spread-adjusted targets. ~3 days.
  Already in this BACKLOG's earlier section.

## External review backlog (2026-04-25, third pass)

Review covered architecture, risk, signals, execution, backtest,
observability. Graded as follows:

### ✅ Applied (commit `tier-a-b`, 2026-04-25)

- **Market-cap filter on retail sentiment multipliers** — Stocktwits + WSB
  multipliers now return neutral above $50B market cap where the tape
  already reflects retail flow.
- **Kelly cap 1.35 → 1.2** — tightened pre-live (not enough realized trades
  to trust bucket win-rates aggressively).
- **`flatten_by_eod` default-true for new configs** — overnight-gap risk
  contained during initial live phase. Existing rows keep their setting.
- **Adaptive risk sizing** — `max_risk_per_trade_pct` is multiplied by 0.5
  when VIX > 25 OR recent-30d realized win-rate < 55%; 0.75× when VIX 20-25.
- **VIX-scaled options bucket** — `option_pct_of_equity` × 0.3/0.5/0.75
  at VIX > 30 / > 25 / > 20. Stocks bucket untouched.
- **Gate-by-gate skip-reason metrics** — key rejection paths now
  `metrics.inc("autotrade_skip", reason=...)`. Operator can graph
  reject-reason frequency to spot gate miscalibration.
- **WebSocket staleness alert** — health endpoint raises a
  `stream_stale` alert when RTH quote stream is > 30s stale (deduped 5m).
- **Profit factor + per-regime stats in portfolio backtest** — each
  trade tagged with entry ADX + VIX; stats broken out by `trending` /
  `chop` / `high_vix` / `normal`. Profit factor computed from gross
  wins / |gross losses|.
- **Overnight-gap simulation** — portfolio backtest now checks whether
  the day's OPEN gapped through the stop/target and fills at OPEN price
  if so (realistic slippage for gap-down days on resting stops).

### ⏸️ Deferred — Tier C (low ROI or wrong-scope)

- **Pairwise correlation matrix beyond beta** — 1+ day of work, marginal
  over the beta-weighted heat cap we already have. Revisit if position
  count routinely exceeds 20 (currently capped at 15).
- **Separate position-manager process (Pub/Sub/Redis)** — genuine
  infrastructure complexity (~2 weeks including monitoring + deploy
  pipeline changes) for marginal resilience gain on a single-user bot.
  Current single-process + Cloud Run min-instances=1 handles the load.
  Revisit if single-process failure modes show up in practice.
- **Full async I/O migration** — still deferred with documented triggers
  (see "Async I/O migration" section above).

### ⏸️ Deferred — Tier D (review 2026-04-25, multi-week scope)

- **IV percentile gate for option entries** — sized work: ~3-5 days of work
  spread over 2-3 weeks of historical-IV ingestion. Need 252-day rolling
  history of ATM IV per ticker to compute IV-percentile. Today we use
  realized-vol % from `_iv_estimate()` which is a passable proxy but
  systematically lags actual IV regime around earnings. Once available,
  gate option entries: skip new long calls/puts when IV percentile > 80
  (option premium is overpriced; theta decay is faster than expected
  underlying move can recover). Source: yfinance options chain (free) or
  fold into the ORATS-style provider when we eventually pay for IV data.
  Revisit when option win-rate plateaus or theta-decay losses exceed
  underlying-stop losses materially.

- **ML scorer graduation from shadow → live (10% weight)** — the LightGBM
  scorer has been running in shadow mode logging predictions vs realized
  outcomes for several weeks. Graduation criteria: ≥200 closed live
  trades with shadow predictions logged; AUC > 0.60 on hold-out;
  calibration plot shows monotonic relationship between predicted prob
  and realized win-rate. Once met, blend: `final_conf = 0.9 × rule_conf
  + 0.1 × ml_prob_normalized` and gate auto-trade entries on `final_conf`.
  Risk: if rule-conf and ml-prob disagree systematically (low correlation),
  the blend can DECREASE Sharpe in some regimes — track post-graduation
  Sharpe by month for at least 90 days before increasing weight.
  Revisit when shadow log accumulates ≥200 closed trades.

## External review backlog (2026-04-25, fourth pass — strategy + execution)

Reviewer flagged divergence between backtest and live, plus refinements
to ATR fallback, OCC consolidation, runner sizing, and restart safety.

### ✅ Applied (r37, commit `3241065`, 2026-04-25)

- ✅ **Backtest partial-exit simulation (Ghost Alpha fix)** —
  `_simulate(partial_exits=True)` now banks 33% at T1 (50% of distance
  to final target), 33% at T2 (85%), runner exits at the full target.
  Stop tightens to soft-BE at T1, full BE at T2. Closes the divergence
  where the all-in/all-out backtester systematically overstated upside
  AND drawdown vs what live actually captures. Legacy single-exit
  retained under `partial_exits=False` for sanity comparison.
- ✅ **Liquidity gate in `run_multi_strategy`** — rejects backtests
  with median 20-bar daily $-volume < $10M (matches the live
  `consider_signal` gate). Stops backtest stats from being inflated by
  spread-driven micro-cap fills the live bot wouldn't take.
- ✅ **ATR fallback chain repair** — real ATR → 14-bar median H–L range
  → stdev of 14 closes → 2% of Close → 0.01 floor. (A previous merge
  had a trailing else that silently overwrote good values with raw
  2%-of-Close.)
- ✅ **OCC parser cleanup** — removed residual inline P/C parse in
  `_manage_option_trade`; `_is_call_option` is now truly the only
  direction source. Tightens the AMKR direction-drift guard.
- ✅ **Extreme-trend skip-T1** — `trim_fraction_for_adx` returns 0.0
  at ADX ≥ 45 for the T1 site (parabolic regime — runner is the trade).
  T2 unchanged. Wired through option + stock T1 paths with 0-frac
  short-circuit.
- ✅ **Persisted `target_touch_count`** — column on `auto_trades` +
  `_touch_get / _touch_set / _touch_clear` helpers. The 2-tick debounce
  now survives Cloud Run instance cycles instead of being bypassed
  whenever an instance restarts mid-target-test.
- ✅ **Chop-regime risk halving** — `adaptive_risk_multiplier` now also
  applies 0.5× when SPY daily ADX_14 < 20. Range-bound markets chew up
  trend-following entries via false breakouts; half-size during these
  periods recovers the EV the chop chops out.

## External review backlog (2026-04-25, sixth pass — execution + concurrency)

Three small but real findings: lock contention, missing PDT enforcement,
broken option premium-vs-underlying compare.

### ✅ Applied (r41, commit `c313b02`, 2026-04-25)

- ✅ **Lock contention**: `_confirm_1m_bar` moved BEFORE `_entry_lock`
  acquisition in `consider_signal`. Slow OHLCV fetch can no longer
  hold the global entry lock.
- ✅ **PDT day-trade hard gate**: new `cfg.pdt_enforce` flag (default
  False on paper). When True, blocks new entries at ≥3 day-trades in
  trailing 5 business days — preventing the 4th from triggering a
  90-day PDT lock on margin <$25k. **Must flip to True before going
  live with margin <$25k** (LIVE_CHECKLIST.md cutover step).
- ✅ **Option underlying-vs-premium bug**: `_manage_option_trade` was
  comparing current underlying ($500) against `t.requested_entry`
  (option premium $2.00). New `auto_trades.underlying_entry_price`
  column stores the underlying at entry; manage-loop uses it for
  the spread-artifact "underlying against thesis" check.

## External review backlog (2026-04-25, fifth pass — strategy/backtest robustness)

Long review covering analysis strategy, execution, backtesting realism,
performance, and security. Triaged by ROI vs scope; deferred items have
explicit revisit conditions.

### ✅ Applied (r38, 2026-04-25)

- ✅ **Monte Carlo bootstrap** in `portfolio_backtest`: 1000 paths × N
  resampled trade pnls, deterministic seed=42. p5 / p50 / p95 of
  max-drawdown and ending-equity now in stats. Headline is p95 max-
  drawdown for risk-of-ruin calibration.
- ✅ **Expectancy** (avg $/trade after costs) in stats — fills the
  "positive expectancy is necessary but not sufficient" reviewer point.
- ✅ **Strategy-drawdown trigger** in `adaptive_risk_multiplier`: 30-day
  cumulative realized-PnL drawdown ≥ 10% of equity → 0.5× sizing. Joins
  VIX/WR/ADX triggers (lowest wins).
- ✅ **API rate limiter** middleware: token-bucket per X-API-Key (or IP).
  300/min refill + 60 burst defaults. Returns 429 with Retry-After.
- ✅ **Bollinger Bands** confirmed already in `indicators.py`. No-op.

### ⏸️ Deferred — Tier E (multi-week or trigger-gated)

- **vectorbt / PyBroker backtester rewrite** — reviewer's biggest
  recommendation. Multi-week port of `_simulate` to a vectorized
  engine. Current backtester now mirrors live partial exits + has
  Monte Carlo + walk-forward already; the marginal value of a rewrite
  is execution speed, not correctness. Revisit when single-strategy
  backtest > 10s per ticker on the watchlist (currently ~1s).
- **LSTM / Transformer ML hybrid** — not gated on engineering, gated
  on data: shadow-mode log of LightGBM scorer needs ≥ 200 closed
  trades before *any* ML graduation, and a sequence model needs
  meaningfully more. Revisit when LightGBM has been live-blended at
  10% weight for 90+ days with measurable lift.
- **Full pairwise correlation matrix** — Tier C. Revisit at 20+
  concurrent positions (cap is 15).
- **Async I/O migration** — formally deferred with concrete latency /
  CPU / concurrent-user triggers; none have fired.
- **Debit spreads** — multi-leg Alpaca order machinery + spread-aware
  strikes. ~3 days work. Revisit ~4 weeks post-real-money once naked
  long-call/long-put behavior is well-understood live.
- **JWT/OAuth** — overkill for a single-user app behind X-API-Key +
  Cloud Run + IP rate limit. Revisit if multi-user.
- **Trivy / Snyk dependency scanning** — out of scope for this
  pre-live phase; Cloud Run base image and pinned requirements get
  GitHub Dependabot alerts already.
- **SHAP / LIME interpretability** — gated by ML graduation milestone.
- **Optuna hyperparameter tuning** — gated by ML graduation milestone.
- **CI/CD performance threshold gate** (e.g., fail deploy if Sharpe <
  1.0) — running portfolio backtest in deploy.sh adds 60+s to every
  deploy and is brittle to data-availability flakes. Revisit if we
  observe a regression that this would have caught.
- **Earnings call transcript NLP** — sized to 1-2 weeks for ingestion +
  embedding pipeline. High signal but hard to validate lift before
  live data accumulates.

### Closed via r36 (independent of this review pass)

- ✅ **AI judge layer** — `services/ai_judge.py` wraps three Claude
  (Haiku) call sites: entry veto, news-driven exit, sizing multiplier.
  Each independently mode-gated (off / shadow / active). Decision log
  in `ai_decision_log` table; review endpoints under `/api/ai-judge/*`.
  Fail-open guarantee — Claude unavailability never blocks live trading.
  Bounded influence — outputs clamped + enum-validated; no prices/sizes.
  Different from "ML scorer graduation" still on the Tier D list.

### Rejected — low ROI or empirically weak

- **Join-the-Bid entry** — bid-chase with cancel/replace. Saves
  ~$20-50/trade on slippage at the cost of a fragile order state machine.
  The existing `limit_at_mid` captures most of the spread win at a fraction
  of the complexity.
- **Time-decay stop** — incrementally tighten stop each bar that fails
  to hit T1. Intuitive but empirically weaker than fixed stops — premature
  tightening chops the trade out on normal noise before the thesis plays
  out. Not supported by studies.
