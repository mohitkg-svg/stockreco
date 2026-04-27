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

## r48 — implement EVERY r47-deferred backlog item (2026-04-27)

r47 surfaced ~150 deferred items in the "⏸️ Deferred to r48+" register
below. **r48 ships every one of them.** Highlights:

**Options (r47's biggest remaining gap)**: long-option ENTRIES routed
through `submit_option_entry_with_cross_fallback` (was market orders);
Greeks (`entry_delta/gamma/theta/vega/iv`) persisted on AutoTrade;
`portfolio_greeks` reads real values; portfolio vega/gamma/net-delta
caps gate option entries.

**Concurrency atomic-SQL**: `realized_pl` and `target_touch_count` use
SQL `UPDATE ... + :delta` instead of ORM read-modify-write. `_market_clock_cache`
single-flight via `Event`. `_subscribers`, `_subscribed_symbols`,
`_breaker_lock`, `_in_flight_bp_lock` all guard their critical sections.
`_stock_quotes` torn-write fix via whole-dict swap.

**Failure modes**: PDT 24h lockout breaker (`trip_pdt_breaker`); DB-down
60s breaker (`trip_db_down_breaker`); generic `submit_rejected` alert
ladder for sub-penny / max_position / not_tradable / fractional / etc.

**Position lifecycle**: `force_close_trade` releases BP on all paths
(slippage / news AI / reverse-thesis / time-stop).

**Memory/perf**: `_chandelier_*_cache` LRU-bounded (1000); `_alerts._dedup`
periodic prune; `httpx.Client` reused; `GZipMiddleware` registered.

**New strategy modules**:
- `services/factors.py` — A4 12-1 momentum, B9 BAB, B11 yield-curve
  defensive tilt, B12 oil regime, B10 DXY tilt, A5 real-yield, A7 FOMC
  surprise, A3 macro-surprise drift, C15 squeeze, C14 opportunistic
  insider; composite consumed in sizing.
- `services/order_flow.py` — block-print lean, sweep detector, aggressor-
  flow gate (Lee-Ready), spread-widening defer, opening drive bias,
  tape-acceleration confirm, VWAP-band fade, round-number stop-hunt
  fade, quote-stuffing score; gates wired into consider_signal.
- New strategy `_lev_etf_decay_short` (Cheng-Madhavan 2009).

**Backtest validity**: portfolio_backtest costs (12bps baseline + CS adder
+ 25bps stop slip); HAC Newey-West Sharpe (was IID-inflated ~18%); PSR
(Bailey-LdP); bootstrap permutation null (200 shuffles); alpha-decay
slope; bonferroni floor lowered + natural log; dd_score weight 1×→2×;
DEFAULT_STOP_ATR_MULT 1.5 → 2.0; sector rotation lookback 126d → 63d.

**Edge corrections**: AI envelope [0.6, 1.4] → [0.85, 1.15]; pre-FOMC
ETF stack 1.232× → 1.10×; Russell/MSCI nudge 1.05× → 1.025× AND only
for whitelisted tickers; OPEX 0.92× gated on liquid-mega-cap whitelist;
`_high52_proximity` 5-bar cooldown; `winrate_to_multiplier` smooth tanh;
`book_var_99` 1.5× → 2.33×; `kelly_risk_mult` NaN guard.

**Observability**: AI **cost ($) tracker** via `resp.usage` token math
× model price table on `/api/health`; MLPrediction.outcome backlog
counter; `db_pool_checkedout`; `pdt_locked` / `db_down` flags;
frontend error reporter at `/api/log/frontend-error`; per-fill slippage
histogram (already in r47, verified).

**Tests**: 17 new r48 regression tests pinning each fix. Full suite:
**191 passed** (was 174 in r47).

## r47 14-agent maximum-rigor audit (2026-04-27)

13 successful + 1 partial parallel agents on angles never previously
audited (concurrency-races, failure-modes, DB-integrity, lifecycle/
reconciliation, memory/perf, deep backtest-validity, edge-quantification
under realistic costs, microstructure-execution, options-pricing/Greeks,
volatility-strategy research, macro-strategy research, microstructure-flow
research, numerical edge cases, observability gaps).

**Headline pipeline-broken multipliers (all silently no-op since launch)**:
- `calibration_multiplier` read non-existent `n_trades` (col is `n`)
- `strategy_multiplier` read non-existent `trades` (key is `n`)
- `best_strategy._score()` read non-existent `avg_pl`/`oos_trades`
  (`oos_trades` is fold count, NOT trade count); MIN_OOS_TRADES floor
  never met → ranker selecting on pure-Sharpe noise of 26 strategies
- `ml_features` read wrong indicator column names (`RSI` vs `RSI_14`)
  → ALL technical features silently None
- `confidence_boost` string-matched against literal "Composite (multi-
  factor)" but persisted strategies are named "Trend Following" etc
  → boost never fired
- `manage_open_positions` pyramid block referenced `signal` (not in
  scope) → silent NameError → r44 pyramid feature has never executed
- Schema drift: pyramid_enabled, max_correlated_open, vol_target_annual,
  leverage_cap, book_var_99_cap_pct, bracket_tif, rr_min were never
  columns

**Position lifecycle blockers**:
- `/api/trading/close{,-all}` only called broker, never updated AutoTrade row
- `kill()` cancelled foreign Alpaca orders + leaked BP reservation
- `sync_positions_from_alpaca` skipped pending → permanent phantom-pending
- SL-filled `pass`-branch never closed DB row

**DB integrity**:
- Option idempotency_key never set → multi-instance double-buy
- EquitySnapshot no UNIQUE on ts → multi-instance dupe rows
- MLPrediction outcome backfill stamped wrong trade's outcome via
  ticker+signal_type fallback
- Options chain cache no size cap → 300MB OOM in 7-14d

**Failure modes**:
- account_blocked / transfers_blocked never populated (r46 gate dead)
- News AI rate-limit was per-batch not per-hour (r46 cap dead)
- equity_snapshot UTC cron dropped close window 4 months/yr in EST
- main.py outcome backfill window mismatched auto_trader (10min vs 24h)
- No halt/LULD detection (added stale-quote heuristic)
- bracket_tif default flipped from "gtc" to "day" (weekend gap)

**Concurrency**:
- _replace_stop_cache unsynchronized + unbounded
- AI news exit thread didn't acquire _manage_lock → lost-update
- _option_subscribed never pruned on close

**Numerical**:
- Heat/sector-heat `max(0, entry-stop)` returned 0 for shorts
- slippage_aware_risk_per_share same shape → over-sized shorts 10-20×
- Adopted-short placeholder stop was 0.95×entry (wrong direction)
- Inside-bar breakout strategy was firing on noise (wrong indexing)

**Options**:
- R:R reward used intrinsic-at-EXPIRATION → biased to deep-ITM low-leverage
- DTE≤0 force-flatten added (pin-risk + assignment risk)

**Tier P new strategies (services/r47_overlays.py)**:
- A1 VIX9D/VIX3M term-regime sizing (Whaley 2009)
- A2 SKEW reversal bias (Bali-Hovakimian 2009)
- A4 VIX 5σ spike → SPY long (BTZ 2009) — new strategy in registry
- A5 IV-rank graded sizing (Goyal-Saretto 2009)
- B2 VRP filter (BTZ 2009)
- B3 VVIX anxiety gate (Park 2015)
- B6 Earnings IV-crush sidestep (Gao-Xing-Zhang 2018)
- Macro A1 SPX 200d trend gate (Faber 2007)
- Macro A6 HYG/LQD credit-spread circuit breaker (Gilchrist-Zakrajšek 2012)
- Pre-FOMC quiet-hour defer
- Donchian discipline (RVOL + ATR expansion gates)

**Tier 1 observability**:
- Per-fill slippage histogram + outlier alert (>50bps)
- APScheduler EVENT_JOB_ERROR + EVENT_JOB_MISSED listeners
- position_divergence alert from sync_positions
- AI news rate-limit fixed (was per-batch, now per-hour)

174 tests pass (was 163; 11 new r47 regression tests).

### r47 ⏸️ Deferred to r48+ — items found this pass but NOT shipped

The 14-agent audit surfaced ~250 findings. r47 implemented the highest-
leverage P0 cutover blockers + Tier P new strategies that had a clean
single-revision implementation. The items below are real findings that
were deliberately deferred — most are multi-day/multi-week, need data
ingestion, or need infrastructure (multi-leg options, point-in-time
data) that doesn't exist yet.

**⏸️ Concurrency P1/P2 (need atomic-SQL-UPDATE refactor for hot fields)**:
| Finding | File | Defer reason |
|---|---|---|
| `_target_touch_counts` read-modify-write race vs DB column | auto_trader.py:100-131 | Convert to `UPDATE auto_trades SET target_touch_count = target_touch_count + 1` atomic SQL — refactor across all touch sites |
| `force_close_trade` `realized_pl` ORM read-modify-write | execution_engine.py:213-216 | Switch to `UPDATE ... SET realized_pl = COALESCE(realized_pl, 0) + :delta` atomic SQL |
| `update_config` row-level lost-update races | auto_trader.py:1261-1272 | `with_for_update()` on Postgres + targeted UPDATE statements |
| `kill()` vs `update_config` interleave | auto_trader.py:599 | Same atomic-update treatment |
| ML scorer model+calibrator load race | ml_scorer.py:81-134 | Atomic dir-swap or version-pinned filenames |
| `_stock_quotes` torn writes (last vs ts pair) | live_quotes.py:208-218 | Replace inner-dict mutation with whole-dict swap |
| `_subscribed_symbols` race on reconnect | live_quotes.py:367-413 | Lock guard + snapshot iteration |
| `paper_trader._market_clock_cache` thundering herd | paper_trader.py:62-75 | Single-flight `threading.Event` |
| `_subscribers` mutated/iterated unsynchronized | live_quotes.py:41,134-160 | `_subscribers_lock` |
| `_in_flight_bp_last_check_ts` unprotected gate | risk_manager.py:88-124 | Move read into the lock |
| `trip_bp_breaker` / `trip_broker_breaker` no lock | risk_manager.py:127-160 | `_breaker_lock` |
| `_post_mortem_pending` leak on submit-mid-error | auto_trader.py:280-313 | Track via Future.add_done_callback |
| `_corr_cache` torn reads (rets + ts separate writes) | auto_trader.py:344-369 | Single tuple per ticker |
| `auto_reconcile_positions` interleaves with manage tick | auto_trader.py:868-924 | Acquire `_manage_lock` in reconcile path |
| `_chandelier_atr_cache` / `_chandelier_adx_cache` / `_price_fallback_cache` unsynchronized | position_manager.py:38-86 | Per-ticker lock or single-flight |
| `_NEWS_AI_POOL` not joined on shutdown | news.py:36 | Add to lifespan teardown |
| `partial-fill cancel-remainder` doesn't release BP delta | auto_trader.py:4077-4090 | `_release_bp(unfilled * entry)` immediately |

**⏸️ DB integrity P0/P1 (multi-day or pool-tier dependency)**:
| Finding | File | Defer reason |
|---|---|---|
| Connection pool exhaustion under multi-instance Cloud Run (15 conns × 3 instances vs Cloud SQL f1-micro 25 cap) | database.py:62-66 | Operator decision: bump Cloud SQL tier to db-g1-small OR cap max_instances=1 — outside engineering scope this revision |
| No FK constraints anywhere (AutoTrade.signal_id, Alert.trade_id, etc.) | database.py | Migration risk; requires careful ON DELETE planning |
| Money fields stored as Float not Numeric — cents drift on multi-leg accumulation | database.py:139-256 | ALTER COLUMN per money column + read-path audit |
| Missing composite indexes on hot queries | database.py | Postgres CONCURRENTLY migration; non-trivial ordering |
| `consider_signal` long-running txn holds session through Alpaca + Claude | auto_trader.py:1680-2742 | Refactor into "read-gates / network-IO / write" sections |
| News cross-source dedup TOCTOU under multi-instance ingest | news.py:175-186 | DB-layer UNIQUE on (ticker, headline_prefix, ts_bucket) — schema migration |
| CandidatePool wipe-and-rebuild not atomic | universe_scanner.py:338-358 | Switch to `INSERT ... ON CONFLICT(ticker) DO UPDATE` upsert |
| AutoTraderConfig CHECK(id=1) singleton hardening | database.py:151-241 | Migration |
| `realized_pnl_today` SERIALIZABLE around daily-loss gate | auto_trader.py:551-566 | Postgres-only feature; needs Postgres-prod confirmation |
| SQLite "database is locked" silently swallowed in manage loop | auto_trader.py:4800 | Specialize OperationalError → retry-once before swallow |
| Cold-start `_in_flight_bp_reserved` lost — fresh instance over-sizes | risk_manager.py:43-46 | Persist to Memorystore/DB table; design choice |

**⏸️ Position lifecycle P1/P2 (real bugs, defensive but not catastrophic)**:
| Finding | Defer reason |
|---|---|
| Bracket parent NACK orphan (rare race) | Need broker-state probe via client_order_id on submit failure |
| Cancel-and-cross double-bracket race | Pre-cross broker query for parent-terminal status |
| PDT 403 retry storm — no breaker | Add `pdt_lockout_until` 24h cooldown breaker |
| Reverse-thesis `check_reversals_for` races manage tick | Acquire `_manage_lock` in reverse-thesis path |
| Slippage-shift force-close doesn't release BP | Add `_release_bp` to force_close_trade |
| `closed_external` doesn't compute realized_pl from broker history | Fetch last 5 SELL fills per ticker on reconcile |
| Adopted placeholder $0.95×entry doesn't reflect real risk | Use 5% notional as fixed risk for adopted rows |
| Option assignment leaves option AutoTrade row open | "option open + no broker position" → closed_external + alert |
| Realized_pl on multi-leg trim uses live snapshot, not actual fill price | Await trim fill via get_order_by_id; use filled_avg_price |
| T1 trim not idempotent on commit failure | Set hit_t1=True + commit BEFORE SL-resize round-trip |
| Promoted-adopted reverse-thesis defaults to "1d" TF | Add `source_timeframe` column |
| Stale price guard missing in `_manage_option_trade` underlying-stop | Pass `max_age_sec=30` |
| Bracket TIF=DAY orphans TP at session close | Add daily TP resubmit alongside SL invariant |
| Wash-sale enforcement (cfg knob added, no enforcement code) | r48 — compose from closed-loss tracker + cooldown gate |

**⏸️ Memory / perf P1 (degrade over weeks; not immediate)**:
| Finding | Defer reason |
|---|---|
| Manage walltime 37s P50 / 250s P99 at 50 positions | `ThreadPoolExecutor.map(_process_trade, trade_ids)` — needs DB pool bump first |
| WS stop-threat fast-path opens fresh `SessionLocal()` per tick (~40 sessions/sec) | Cache `{ticker: current_stop}` in manage tick; fast-path reads cache |
| `_chandelier_atr_cache` / `_adx_cache` / `_price_fallback_cache` unbounded | LRU cap of 1000 each |
| `_corr_cache` holds pd.Series unbounded | LRU cap 256 |
| `alerts._dedup` unbounded over months | Periodic prune entries older than 2× DEDUP_WINDOW |
| `regime_score()` recomputed per signal | Pre-compute every 60s in scheduler, broadcast cached |
| ML scoring per-ticker microstructure adds 100s to scan | Pass shared `daily_cache` through scan-orchestration |
| News dedup query per batch | Memoize 30-min window across polls |
| `fetch_ohlcv` `df.copy()` per cache hit | Read-only view or write-on-mutate |
| `_tick_rule_imbalance` Python for-loop on 10K trades | Vectorize via numpy diff/sign |
| Kill broadcast 6.4MB JSON payload on 50-position kill-all | Pre-serialize bytes once per fan-out |
| `_recompute_task` uses asyncio default executor | Bounded executor `max_workers=4` |
| APScheduler `misfire_grace_time=60` too low for 10s manage | Bump manage misfire to 120s; add metrics counter on misfires |
| `_NEWS_AI_POOL` queue unbounded | Switch to `Queue(maxsize=20)` + drop-oldest-on-full |
| httpx per-call TLS handshake on 9 modules | Module-level reused `httpx.Client` |
| `equity_curve` no GZip middleware | Add `GZipMiddleware(minimum_size=1024)` |
| `_load_if_needed` ml_scorer DB hit per-call when calibrator missing | "checked-in-last-5min" guard |

**⏸️ Backtest validity / edge quantification (multi-week / multi-month)**:
| Finding | Defer reason |
|---|---|
| `portfolio_backtest.py` charges ZERO transaction cost (~2-5%/yr drag missing) | Wire `_apply_costs` from per-ticker backtester; re-run all stress windows |
| Universe-construction look-ahead — current Alpaca tradable list applied to past | Need point-in-time CSV per `as_of_date` (data ingestion) |
| Survivorship/look-ahead in fundamentals — current yfinance.info read for past bars | Snapshot fundamentals quarterly into versioned table OR disable in backtest |
| Insider-multiplier reads CURRENT InsiderSummary regardless of bar timestamp | Versioned InsiderSummary per `as_of_date` OR disable in non-live |
| News timestamp leakage (`updated_at` vs `created_at`) | Prefer `created_at`; +5min decision-delay floor on backtest |
| Earnings-date rescheduling leak | Versioned earnings calendar |
| No Probabilistic / Deflated Sharpe (Bailey-LdP) — current Bonferroni proxy too lax | Implement DSR with proper trial count (strategies × params × tickers) + skew/kurt moments |
| WF IS/OOS climate-mismatch test missing | Implement Combinatorial Purged CV (CPCV) + KS test on return distributions |
| Slippage too generous on small caps (~30-50 bps under-modeled) | Replace heuristic adders with Corwin-Schultz effective spread |
| Stop fills assume zero slippage past stop level | Add 25 bps stop-slippage adder |
| No alpha-decay analysis (fold-Sharpe slope) | Report fold-Sharpe slope; demote strategies with slope < -0.3 |
| No bootstrap / permutation null per strategy | Stationary bootstrap on shuffled entries → p_value |
| Sharpe annualization assumes IID trade returns (~18% inflation) | Newey-West HAC standard error |
| `calibrated_weights.py` still `NotImplementedError` | Walk-forward CV per (strategy, timeframe) |
| Multiplier independence assumed but signals correlated | PCA orthogonalization of regime score (Macro C16) |
| 14-factor signal_generator stack auto-correlated within [0.7, 1.4] clamp | Cluster-aware combine (factor count → discount) |
| AI confidence multiplier envelope [0.6, 1.4] is largest single multiplier with no backtest | Shrink to [0.85, 1.15] before active mode flip |
| Pre-FOMC drift ETF 1.232× (1.10×1.12) too aggressive post-publication | Reduce 1.10 → 1.05; drop ETF-specific 1.12 |
| Russell rebalance applied to ALL signals not just inclusion list | Gate on `cfg.index_inclusion_tickers` whitelist |
| OPEX 0.92× applied universally — small caps don't pin | Gate on `options_open_interest > 5000` |
| Insider cluster amplifier 1.12× has no OOS validation in repo | Backtest 12-cluster rule on this codebase's universe |
| Sector-rotation lookback 126d too long (mean-reversion at 6-12mo) | Switch to 63d |
| Strategy WR baseline 0.55 doesn't account for SPY uptrend base rate | Regime-adjust to 0.60 in uptrend |
| Per-bucket multiplier needs Bayesian shrinkage at small N | `Beta(11, 9)` posterior |
| Crisis chandelier 0.67× and T1 50% are hand-picked | Backtest 0.5/0.67/0.75/0.90 on crisis windows |
| `_high52_proximity` fires too often (no cooldown) | Add 5-bar cooldown after fire |
| Seven r46/r47 new strategies have no per-strategy realized-edge gate | Require `n>=30` per ticker before signal-blend contribution |
| 60/40 backtest blend arbitrary | Shrinking-toward-tech `n/(n+30)` |
| `vol_target_multiplier` reads trade-PnL series (noisy) | Switch to EquitySnapshot returns; require ≥30 snapshots |
| Confidence→sizing monotonicity broken at tight-stop boundary | Hard cap single-entry notional at 50% × equity |
| `winrate_to_multiplier` step function | Smooth `tanh` ramp |
| `regime_multiplier` clamp [0.6, 1.2] asymmetric without rationale | Document or rederive empirically |

**⏸️ Options pricing P0/P1 (real money, but mostly require Greeks persistence first)**:
| Finding | Defer reason |
|---|---|
| **Long-option entries STILL use MARKET orders** (exits are limit) | Need `submit_option_buy_marketable_limit` + cross-fallback primitive — high impact, contained |
| Greeks NOT persisted on AutoTrade — `portfolio_greeks` uses hardcoded defaults | ALTER TABLE add entry_delta/gamma/theta/vega/iv columns + plumb |
| No portfolio vega/gamma/net-delta cap | Depends on Greeks persistence |
| Theta-stop OCC parse `len >= 13` should be `>= 16` | Trivial 1-line, deferred for batching |
| R:R reward ignores theta-decay-to-target | Subtract `theta × expected_days_to_target` |
| IV-rank is realized-vol-rank (proxy) — anti-edge in 30-40% regimes | Ingest historical IV30 daily into `iv_history` table |
| `_iv_is_expensive` falls back to absolute 100% IV cap | Use sector-median IV when RV unknown |
| Premium-at-stop estimate missing gamma | Add 0.5×gamma×Δs² term (mirror of reward fix) |
| Strike-width ATR band ignores DTE — too tight for monthlies, too wide for weeklies | Scale by √DTE |
| Earnings post-print IV-crush window not gated for options (B6 partially mitigates) | Extend earnings gate to [-48h, +72h] for options |
| `managed_risk` $0.05 floor inflates R:R on cheap contracts | Require `true_managed_risk >= 0.10` AND drop premium < $0.50 |
| Bid-ask check stale (10min cached chain) at order time | Re-fetch live `live_quotes.get_option_quote(occ)` just before submit |
| Theta-stop measures underlying not contract value (misses pure IV-crush) | Add parallel premium-progress gate |
| Per-contract sizing missing gamma | Tied to gamma-corrected estimate |
| Trading-DTE vs calendar-DTE for theta scoring | `pandas_market_calendars` |
| Single-stock CAPE / quality cross-sectional rank | New module + sector aggregates |
| Multi-leg options infrastructure (vertical/calendar/iron condor/strangle/dispersion) | Multi-week rebuild of execution engine |
| Skew-aware strike selection | Track rolling 60d ATM call-IV vs put-IV |
| Term-structure-aware DTE selection | Hook to `vix_term_ratio` for monthly vs weekly bias |

**⏸️ Strategy proposals not yet shipped**:
| Strategy | Defer reason |
|---|---|
| A2 Earnings-revision momentum | Need analyst-revisions historical store (current code overwrites on refresh) |
| A3 Macro-surprise drift (CPI/NFP/GDP) | Surprise→drift mapping + post-release windowed sizing |
| A4 Cross-sectional 12-1 momentum | New `services/factors.py` module with cross-sectional rank |
| A5 Real-yield → growth/value rotation | Need TIPS / 10y real yield ingestion |
| A7 FOMC hawkish/dovish surprise | Need Fed Funds futures-implied prob |
| B8 Quality cross-sectional (sector-neutral) | Rewrite quality_score against sector aggregates |
| B9 BAB low-vol tilt | New cross-sectional rank in factors.py |
| B10 DXY → small/large-cap tilt | Need DXY ingestion + intl-revenue-pct fundamentals |
| B11 Yield-curve → defensive sectors | Trivial extension of `yield_curve_2s10s` |
| B12 Oil regime overlay | New `cross_asset.oil_regime()` |
| B13 Single-stock CAPE value | Need 5y EPS series |
| C14 Opportunistic insider differentiation | Trader-frequency heuristic in insider_trades |
| C15 Squeeze setup (high SI + breakout) | Verify already in code; cooldown |
| C16 PCA orthogonalization of regime score | Multi-week — needs rolling-PCA infrastructure |
| Vol A3 Earnings long strangle | Paired-leg book-keeping in single-leg infra |
| Vol B1 Pre-FOMC calendar (long-back-only variant) | Single-leg degraded version |
| Microstructure: block-print lean, sweep detector, aggressor-flow override, spread-widening defer, opening drive bias, tape-accel confirm, halt-resume continuation/fade, VWAP reversion (band fade), ticker-personality router, VWAP-limit entry, HVN-aware stops, round-number stop-hunt fade, EOD pre-MOC continuation, earnings overshoot fade, pre-market gap reversal, quote-stuffing detector, NOPE proxy | New `services/order_flow.py` module + manage-tick hooks; multi-revision build |
| Lev-ETF decay short | Whitelist + chop-regime detector |
| GEX proxy / OPEX pin (stock-only proxy) | Need OI history quality (Alpaca currently 0) |
| Iron condor low-RV harvest | Multi-leg + short-premium broker support |

**⏸️ Observability not shipped (P1/P2 — most are 1-day each, batch in r48)**:
| Item | Implementation hint |
|---|---|
| Generic `submit_rejected` alert (PDT/wash/sub-penny/not-tradable) | One `else: _raise_alert(...)` after BP/5xx branches |
| SL-resubmit-failure threshold counter (storm precursor) | Per-trade in-memory dict; alert at 3+ in 30min |
| Data-freshness alert during RTH | Bar-age check in `data_fetcher` after fetch |
| Frontend error reporter (`/api/log/frontend-error` + window.onerror) | ~80 LOC |
| Execution latency p99 metrics (signal→submit→fill→broadcast) | Three new `Histogram` instruments |
| Daily PnL attribution digest | Cron at 22:30 UTC writing structured Alert |
| Broker-balance reconciliation alert | Hourly: expected_realized vs alpaca.cash drift |
| Self-emitted heartbeat from manage loop | Webhook payload every 5min |
| AI-veto rate alert (>50% vetoed → signal_gen too aggressive) | Nightly job |
| Zero-entries sentinel | Daily 22:00 UTC job |
| Consecutive-skip cluster alert | 30-min bucket counter per skip reason |
| **AI cost ($) tracker** — currently only call count | `resp.usage.input_tokens / output_tokens` × price table; surface on `/api/health` |
| ML drift / brier-deviation alert | Pre-train `brier_score_loss(outcomes, calibrated_probs)` vs stored |
| `MLPrediction.outcome IS NULL` backlog metric | End of `_ml_outcome_backfill` |
| Options chain & earnings cache freshness on `/api/health` | Per-ticker `last_successful_fetch_ts` |
| Idempotency-collision alert | Nightly count |
| Backtest-vs-live drift alert | Weekly job: 30d-realized-WR-by-strategy vs `calibrated_weights[strat].backtest_wr` |
| Spread/cost realism check (live vs backtest assumption) | Once SLIPPAGE_BPS histogram has data |
| Win-rate trending-down rolling alert (30-trade WR < 45%) | Nightly job |
| Correlation-spike alert (median pair-corr > 0.80) | Per manage-tick when N≥5 open |
| Drawdown attribution per-position | New `DrawdownAttribution` table |
| Equity-curve sparseness check | Frontend coverage badge |
| Deploy-event marker on equity-curve chart | `Alert(severity=info, category=deploy)` at startup |
| DB connection-pool exhaust alert | 60s healthcheck sampling `pool.checkedout()` |
| 15-min RTH low-signal-volume probe | Catches yfinance outages within minutes |

**⏸️ Numerical (deferred — narrower than the dead-multiplier batch)**:
- Option mid 5¢-tick rounding for premium ≥ $3 (broker silent reject)
- Bonferroni haircut formula floor `0.7` is dead code (use natural log or lower floor)
- backtester `_simulate` `frac_remaining` float drift (round to 4 decimals)
- `_max_qty_by_gap` 2%-of-entry floor too aggressive on low-vol names
- `kelly_risk_mult` no NaN validation (NaN propagates through sizing)
- `position_size_by_risk` int() truncation (Alpaca supports fractional)
- Backtester DEFAULT_STOP_ATR_MULT=1.5 doesn't match any TF in STOP_ATR_MULT_BY_TF
- `dd_score` weight too low in `score_strategy` (50% DD scores 67/100)
- `book_var_99 = heat × 1.5` doesn't correspond to any real distribution (should be 2.33×)

These categories together represent ~150-180 deferred items. Most are
1-2 day implementations once their prerequisites are in place. Priority
for r48: **(a)** complete the options entry path (marketable-limit
entries + Greeks persistence + portfolio vega cap), **(b)** complete
observability (slippage metric ladder, AI cost tracker, ML drift,
heartbeat, generic submit-rejected), **(c)** atomic-SQL refactor for
hot fields (`realized_pl`, `target_touch_count`, BP), **(d)** Kelly
proper R-data plumb-through (currently still placebo). Multi-week
items (point-in-time data, multi-leg options, calibrated_weights
implementation, CPCV, DSR) gated on operational triggers.

## r46 13-agent maximum-spread audit (2026-04-27)

13 parallel agents on angles never previously audited. ALL Tier 0/1/P
implemented. Critical bugs verified + fixed:
- News severity gate type bug (silently dropping ALL AI news exits since r41)
- `account_drawdown_multiplier` referenced a non-existent function (multi-
  day DD silently degraded to single-session DD)
- `trading_blocked` flag never read (live account block = silent submit storm)
- Stop-LIMIT now optional (env-gated) for flash-crash protection
- Crisis playbook: kill / DD trim now run inside manage_open_positions
- DD-tier alerts on -3/-5/-8/-10% crossings
- UNIQUE on idempotency_key (multi-instance concurrent dedup)
- Per-ticker overrides (TickerProfile table + accessors)
- News cross-source dedup + per-ticker AI rate limit
- Calibration as gate (Wilson-LB < 35% rejects)
- Kelly on realized 60d edge (was backtest-only)
- Tier P: opening-reversal, last-30min-momentum, news-spike-fade,
  pre-FOMC drift, Russell/MSCI rebalance windows.

163 tests pass.

## r45 ML calibration (2026-04-27)

Closes the largest deferred item from r44: isotonic calibration on top
of the LightGBM scorer. ml_trainer fits IsotonicRegression on the
out-of-fold preds + labels (≥50 samples required) and persists the
calibrator alongside the booster. ml_scorer lazy-loads it and applies
at inference time; falls back to raw output when calibrator missing.
Brier-score raw-vs-calibrated improvement recorded in meta.json.
153 tests pass (2 new).

## r44 audit pickup (2026-04-27) — 7-agent ML/risk/regime/strategy deep-dive

Seven parallel agents on different angles than r42/r43. ALL Tier 0/1/2/3
+ Tier P proposals implemented this revision. Headline:

- **AI judge has been blind on news** (column-name bug); AI temperature=1.0
  (non-deterministic); AI no cost cap; AI no prompt-injection defense.
- **ML scorer trained on real features, served stub** features
  (None on stop_loss / target1).
- **MLPrediction.outcome had no producer** — calibration loop empty.
- **Live mode silently boots on default sqlite** if DATABASE_URL unset.
- **`flatten_by_eod` config flag was dead code**.
- **Best-strategy-of-26 selection had no Bonferroni correction**.
- **Walk-forward had purge+embargo gaps**.
- **`current_portfolio_heat` ignored in-flight BP reservations**.
- **No vol-targeting / drawdown-control / VaR / leverage cap / earnings
  cluster / portfolio Greeks / auto-deleverage layer**.
- **No cross-asset regime signals** (VIX term, HYG/SPY, SKEW, etc.).
- **No calendar / seasonality signals** (pre-FOMC, quarter-end, OPEX).
- **No PEAD trading** (earnings only as a blackout).
- **No NR7 / inside-bar / 52w-prox strategies**.
- **No insider-cluster amplification** (single multiplier ceiling).

151 tests pass.

## r43 audit pickup (2026-04-27) — strategy + execution deep-dive

A 5-agent deep-dive (stock selection, option contract selection, entry
gates, target/stop math, execution accuracy) surfaced ~130 findings;
all of Tier 0-3 implemented this revision. Highlights:

- **OCC symbol = None** for every Alpaca-fed option (verified blocking
  every option entry today). Reader now falls back to `_occ`.
- **R:R floor at consider_signal** (was only in signal_generator);
  hard 1.3R floor with 12bps cost buffer.
- **Opening-filter DST bug** fixed via zoneinfo. New closing-10min filter.
- **Stop-threat fast-path now covers options too**, with global
  single-flight gate to prevent correlated-drawdown manage-storms.
- **TP leg replaced** on slippage shift + T3 recalc (was never updated;
  bot intent ≠ broker reality).
- **Marketable-limit option exit posts INSIDE the spread**, not at the
  bid. Plus a cross-fallback primitive for emergency closes.
- **Options stream defaults ON**.
- **Liquidity gate de-spoofed** (was hardcoded OI=100); spread filter
  denominated in premium not strike.
- **Macro/earnings gates fail-closed** on exception.
- **Pair-correlation gate** added (30d daily-return cache, 0.70 threshold).
- **Daily-loss halt includes UNREALIZED**.
- **Universe scanner** drops shortable filter; optional point-in-time
  override; RVOL no longer biased by intraday-partial bar.
- **Best-strategy** ranked by OOS metric (was display-confidence) +
  per-direction key bug fixed.
- **Kelly stack** routed through proper fractional-Kelly helper.
- **Calibration / strategy multipliers** gated on min-bucket-N=20.
- **Consecutive-loss freeze** (5 stops in a row).
- **Adaptive multiplier returns 0** when unfloored product ≤ 0.25 (skip vs size-tiny).
- **Soft-BE buffer respects T1 distance** (was entry-anchored only).
- **Bear-thesis stop placed 0.3% above resistance** (was AT).
- **Premium-stop binding inverted** (was always-binding regardless of underlying).
- **Theta-stop scales with DTE**.
- **IV-rank gate** vs 1y RV distribution.
- **Delta scoring asymmetric**, strike width ATR-anchored.
- **Manage-loop in-process lock**, thread-safe touch-counts.
- **Sentiment default → FinBERT auto-preferred** (was VADER).
- **Insider min-count raised 3 → 8**.
- **Stocktwits paginates 24h faithfully**.
- **Limit-at-mid quote freshness check**.
- **Idempotency 12h → 4h**.

142 tests pass.

## r42 audit pickup (2026-04-27) — items resolved this revision

The r42 multi-agent audit (BE + UI) picked up the following items from
this register and shipped fixes:

- **realized_pl overwrite on multi-leg exits** — promoted from external
  review pass 5/6 deferral. Fixed at 4 sites with `+=` accumulation +
  regression test `TestRealizedPlAccumulation`.
- **Naked-long window via stale `replace_stop` id** — promoted from r41
  audit deferral. `replace_stop` returns rotated id; all 3 callers persist
  back to `t.stop_order_id`.
- **DST handling via zoneinfo** — fix #1.8 — replaced month/day guess.
- **Count-WR vs expectancy** — fix #1.3 — freeze gate + adaptive multiplier
  now consider PnL-weighted expectancy alongside WR.
- **Walk-forward look-ahead** — fix #1.1 — indicators recomputed inside
  each fold against `df.iloc[:end]`.
- **Sortino denominator math** — fix #1.6 — RMS of `min(0,r)` against MAR=0.
- **Fractional-Kelly default** — fix #1.7 — quarter-Kelly.
- **Backtester unit consistency** — fix #1.10 — `max_drawdown_pct` is now
  percent in both backtester and portfolio_backtester.
- **Per-trade Sharpe** — fix #1.2 — alongside per-bar.
- **Liquidity-aware slippage** — fix #1.4 — dollar-vol + range + auction
  adders on top of flat baseline.
- **AI veto outside entry lock** — fix #1.5 — prefetched async; lock no
  longer holds during 1-2s Claude round-trip.
- **Strategy regime gating** — fix #1.9 — each strategy declares
  `regime ∈ {trend, chop, any}`; `all_strategies()` zeroes off-regime
  entries against ADX_14.
- **Limit-at-mid cancel timer + cancel-and-cross** — fix #2.1.
- **Marketable-limit option exits** — fix #2.2 — `submit_option_exit_marketable_limit`
  saves 5-15% premium per trip.
- **Partial-fill bracket-leg resize** — fix #2.3 — defensive
  `replace_order_by_id(qty=...)` on SL/TP children.
- **Stop-threat fast-path** — fix #2.4 — WS tick within 0.25% of stop fires
  manage immediately, rate-limited 5s/ticker.
- **Sector taxonomy strict mode** — fix #2.5 — unmapped tickers fall under
  `_unknown` bucket.
- **News-exit freshness gate** — fix #2.6 — skip Claude when headline > 30min.
- **Frontend Tier 0 (stale-price masking, alerts panel, freeze banner, PDT
  banner, SRI/CSP/pre-bundle)** + **Tier 1/2/3 polish** — full UI rebuild
  to surface safety state and remove the Babel-in-browser path.

134 tests pass.

## Master deferral register (canonical short-form, current as of r42)

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
