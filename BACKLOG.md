# Backlog — current open items only

**Working principle**: this file is forward-looking. Anything that's
shipped or rejected belongs in DESIGN.md §14 (changelog) or git history,
not here. Items below are genuinely open with explicit revisit triggers
or gate conditions. If you can't find a clear "what would unlock this",
move it to ❌ Rejected.

Last cleaned: 2026-04-28 (post-r53).

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
