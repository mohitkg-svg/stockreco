# Backlog — current open items only

**Working principle**: this file is forward-looking. Anything that's
shipped or rejected belongs in DESIGN.md §14 (changelog) or git history,
not here. Items below are genuinely open with explicit revisit triggers
or gate conditions. If you can't find a clear "what would unlock this",
move it to ❌ Rejected.

Last cleaned: 2026-04-29 (post-r56, third ground-up universe audit + Option B foundation).

---

## ⏸️ Open — gated on data accumulation

These are real engineering items, but shipping them today would do nothing
because they need ≥N closed trades / days of history first.

| Item | Revisit trigger |
|---|---|
| **ML scorer graduation** (shadow → 10% live blend) | ≥200 closed trades with shadow predictions logged + AUC > 0.60 + monotonic calibration |
| **LSTM / Transformer ML hybrid** | LightGBM 90+ days at 10% live blend with measurable lift |
| **SHAP / LIME interpretability** | After ML scorer graduation |
| **Optuna hyperparam tuning** | After ML scorer graduation |
| **Earnings call transcript NLP** | After live data accumulates enough to validate signal |
| **IV-percentile option-entry gate** | r52f shipped the data layer (`IVHistory` + nightly capture). Gate logic itself is <1 day's work but useless until ≥30 capture days have accumulated. |

## ⏸️ Open — gated on operational triggers

None of these triggers have fired yet — items are real but the conditions
that would make them ROI-positive haven't materialized.

| Item | Revisit trigger |
|---|---|
| **vectorbt / PyBroker backtester rewrite** | Per-ticker backtest > 10s (currently ~1s) |
| **Async I/O migration** (httpx.AsyncClient + await endpoints) | 95p `/api/health` latency > 2s sustained, instances trending > 2, concurrent users > 5, or scheduler thread-pool misfires |
| **Full pairwise correlation matrix** | Concurrent positions routinely > 20 (cap is 15; beta-weighted heat covers current scale) |
| **Debit spreads** (multi-leg, defined-risk) | ~4 weeks post-real-money once naked option behavior is well-understood live |
| **AutoTraderService class encapsulation** | Multi-tenancy or cross-test isolation requirements |
| **Decompose `consider_signal`** into Gates / Sizing / Submission helpers | After paper-trade volume produces dedicated unit-test coverage per section. r40 added section dividers as visual scaffolding; full extraction is the next step. |
| **Memory-leak root-cause** | Tomorrow's RTH data through the new `health.memory` observability — looking for which counter grows unboundedly. r52f bounded `_corr_cache`; r52g bounded `_rv_cache`. If RSS still climbs past 1.5 GiB on stable RTH days, dig deeper. |
| **Pydantic migration of `consider_signal` internal dicts** | Incremental — pick one signal-consumer at a time. r52f added `PositionResponse`; r52g added `PnLReconciliationResponse`. Next natural slice: AutoTraderConfig response shape. |
| **`atomic_append_note` migration of 20+ call sites** | Helper added in r53 (`execution_engine.atomic_append_note`). Migration of existing `t.note = (t.note or "") + "..."` sites is mechanical; do it incrementally. |
| **Full option-premium backtest simulation** | r53 added `strategy_scorecard.asset_type_split` so stock vs option WR is visible. Once `IVHistory` has ≥30 days of capture, wire delta+gamma+theta integration through `_simulate(asset_type='option')`. |
| **`Signal.strategy` backfill + auto-mute activation** | r53 wired `cfg.source_mute_enabled` (default off). Currently ~70% of trades have null strategy, so the mute can't see them. Backfill via `Signal.strategy = signal_meta['strategy']` migration, then flip the flag. |
| **Validate r55 sub-scanner pools have meaningfully different tickers** | r55 implemented the real PEAD / sector_rel / vol_exp logic. Breakout-vs-others overlap should drop from ~80% → ~20-40%. Watch the candidate-pool view across 5 trading days; if overlap is still >60% the heuristics need parameter tuning. |
| **Tune `entry_1m_gate_mode` after live observation** | r55 default flipped to "relaxed" (2-of-3 majority). If the false-positive rate (= entries that immediately wick out) climbs above pre-r55 baseline, flip back to "strict". |
| **Run r56 validation scripts after 30 days of data** | `backend/scripts/{analyze_score_divergence,gate_counterfactual,factor_ic_sweep}.py` close the long-standing shadow loops. Run weekly; promote/demote cfg knobs based on output. |
| **Phase 2 event detectors: NEW_HIGH, BREAKDOWN, PEAD** | r56 shipped GAP/RVOL_SURGE/SQUEEZE_RELEASE. The remaining three need per-kind data: NEW_HIGH/BREAKDOWN want intraday minute-bar streaming; PEAD wants SUE-decile from a paid earnings-surprise feed. Defer until r57+ with operator-validated thresholds. |
| **Per-event-kind strategy handlers in auto_trader** | r56's `consider_event` routes all events through `consider_signal` with a synthesized signal. Real Option B has dedicated handlers (PEAD holds longer, GAP uses tighter stops, SQUEEZE_RELEASE has wider targets). Promote once GAP/RVOL_SURGE/SQUEEZE_RELEASE accumulate ≥30 trades each. |
| **WebSocket streaming detection** | r56's event_detector polls every 2min on top-50. Real-time WS subscribe to all 1000 R1000 names would fire events within 1-3s of the trigger, not 1-2min. Defer until paper bot's edge is validated; WS adds operational burden. |
| **Quarterly Russell 1000 file refresh** | `data/russell1000.txt` is a static snapshot. Refresh from FTSE Russell or iShares IWB holdings. Auto-update via a nightly download script is future work. |

## ⏸️ Open — cost-gated

| Item | Revisit trigger |
|---|---|
| **Cheddar Flow / SpotGamma options flow** ($100-300/mo) | After r52f IV-history shows realized lift ≠ 0 vs current realized-vol proxy |
| **Polygon.io Level 2 + tape** ($199/mo) | Defer indefinitely; Alpaca SIP covers our use case |

## 👤 Open — operator-side flips (no engineering work)

| Decision | Gate |
|---|---|
| Flip `AI_ENTRY_VETO_MODE: shadow → active` | After ≥1 week of shadow review |
| Flip `AI_NEWS_EXIT_MODE: shadow → active` | Same — entry-veto first, news-exit later |
| Flip `AI_CONFIDENCE_MULT_MODE: shadow → active` | Same path |
| Promote AI judge call sites from shadow to honored | After reviewing ≥200 decisions in `ai_decision_log` |
| Promote `cfg.loss_pattern_mode: shadow → active` | After reviewing ≥1 week of `loss_pattern_match_shadow` events in metrics. Endpoint: `GET /api/admin/loss-patterns`. |
| Promote `cfg.universe_scoring_v2: shadow → active` | After reviewing 5 trading days where score and score_v2 produce different top-N. Endpoint: candidate-pool API now returns both. |
| Enable additional universe scanners | Set `cfg.universe_scanners_enabled = "breakout,pead,sector_rel,vol_exp"` after evaluating each pool's source attribution in candidate-pool view for ≥30 trades per source. |
| Enable `cfg.universe_tod_profiles_enabled` | After validating that the bot's existing 4-cron schedule (12/14:30/17/19:30 UTC) aligns with the time-of-day profiles. |
| Re-run `POST /api/admin/backfill-realized-pl` | r53 fixed the broken sort-key in the BUY-fill matcher. The 3 rows backfilled in r52g (AAPL/SHOP/CRWV) may have matched the wrong fill; re-run idempotently and verify. |

---

## ❌ Rejected — won't do (with rationale)

Kept here so future-you doesn't re-propose these. Each one was actively
considered and declined; the reason is the load-bearing part.

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
| JWT / OAuth | X-API-Key + rate limiter sufficient for single-user. Revisit if multi-user. |
| Redis cache | Yahoo/Alpaca TTL caches cover hot path on a single-user app. |
| Trivy / Snyk dependency scanning | Pinned requirements + GitHub Dependabot already cover this. |
| Sharpe-based dynamic risk | Recent-WR < 55% trigger covers similar ground. Revisit if Sharpe and WR diverge. |
| CI/CD performance threshold gate (Sharpe ≥ 1.0 etc.) | Brittle to data flakes; would block deploys on transient yfinance hiccups. |
| Variable slippage by volatility in backtest | Flat 6bps round-trip acceptable for our liquid universe. |
| Strict type hints + mypy in CI | Costs more in noise than it returns at this codebase size. Revisit at 50K+ LOC. |
| Cheddar Flow / SpotGamma (re-listed in Open above) | Currently cost-gated; not yet rejected outright. |
| Join-the-Bid entry (bid-chase with cancel/replace) | Saves ~$20-50/trade on slippage at the cost of a fragile order state machine. The existing `limit_at_mid` captures most of the spread win at a fraction of the complexity. |
| Time-decay stop (incremental tighten each bar without T1) | Empirically weaker than fixed stops — premature tightening chops the trade out on normal noise before the thesis plays out. |
