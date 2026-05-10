# Pre-Live Audit — Unified Summary (2026-05-10)

Five parallel audits across the codebase produced ~250 findings.
This is the deduped, prioritized list. Findings I personally verified by reading
the code are marked ✅; the rest carry the auditor's assessment.

---

## TIER 1 — LIVE BLOCKERS (must fix before flipping `ALPACA_LIVE=1`)

### Money path — confirmed real bugs

**B1. ✅ `promote_adopted_to_managed` is broken — `cfg` referenced, never defined**
File: `backend/services/auto_trader.py:1664`. `cfg.bracket_tif` is read but `cfg`
is never assigned anywhere in the 162-line function body. Every adopt→promote
attempt raises `NameError`, swallowed by the surrounding `try/except`,
returning `{"ok": False, "reason": "broker SL submit failed: ..."}`.
**Impact:** with `auto_promote_adopted=true`, every external position the bot
adopts is **naked-long forever** — no stop-loss is ever submitted. The reconciler
silently fails on every external position.
**Fix:** add `cfg = get_config(db)` after `db = SessionLocal()` (~line 1587).

**B2. ✅ Option T1 trim → `db.refresh(t)` wipes uncommitted mutations**
File: `backend/services/auto_trader.py:5306-5314`. After a successful broker trim:
`t.qty = t.qty - half`, `t.hit_t1 = True` (uncommitted ORM mutations) → then
`_atomic_append_note(...)` (commits via separate UPDATE) → then `db.refresh(t)`
which reloads `t` from DB, **reverting the qty + hit_t1 changes**.
**Impact:** broker has fewer contracts, DB still shows full qty + `hit_t1=False`.
Next manage tick re-trims. Position progressively liquidated to zero, losing the
entire runner during a strong move. Same shape at stock T1/T2/pyramid mutations
(lines 6480, 6544, 6587) where commit only fires inside an `if _new_sid:`
branch — when stop-replace fails, the trim mutation is lost.
**Fix:** `db.commit()` immediately after the trim mutation; remove the `db.refresh(t)`.

**B3. ✅ `alpaca_client.replace_order_by_id` does not exist**
File: `backend/services/auto_trader.py:6549`. Pyramid SL-resize calls
`alpaca_client.replace_order_by_id(...)`, but `replace_order_by_id` is a method
on the `TradingClient` instance (`c.replace_order_by_id(...)`), not on the
wrapper module. The other three call sites (lines 6000, 6490, 6596) get this right.
**Impact:** Pyramid SL-resize raises `AttributeError`, swallowed by surrounding
try/except → SL leg is NOT resized → pyramided shares are **naked-long** until
the next manage-tick price-trigger event.
**Fix:** Replace with `c.replace_order_by_id(t.stop_order_id, order_data=ReplaceOrderRequest(qty=int(t.qty)))`.

**B4. ✅ `_backfill_ml_outcome` argument order swapped at one call site**
File: `backend/services/auto_trader.py:6183`. Function signature is `(db, t)`;
called at 6183 as `(t, db)`. Other two call sites (5604, 6782) are correct.
**Impact:** Stop-loss close path silently fails to backfill ML outcomes →
calibration plot biased toward only TP fills → ML graduation gate (≥200 closed
predictions) under-counts.
**Fix:** Swap to `_backfill_ml_outcome(db, t)`.

**B5. ✅ Regime classifier completely broken — wrong import names**
File: `backend/services/regime_router.py:75, 83`.
- `from services.market_context import vix as _vix` — but `market_context.py`
  exports `current_vix`, not `vix`.
- `from services.indicators import adx as _adx_ind` — but `indicators.py` has
  no `adx()` function (only `compute_indicators`).
Both ImportErrors are swallowed by broad `except Exception`. `_classify_raw()`
always returns `None`.
**Impact:** TREND/CHOP/HIGH_VOL classification is dead. The `strategy_off_regime`
gate never fires — trend strategies fire in chop without challenge.
**Fix:** Replace with `from services.market_context import current_vix` and
`compute_indicators(df)["ADX_14"].iloc[-1]`.

**B6. Stop-evaluation reads stale WS price (`max_age_sec` not passed)**
Files: `backend/services/auto_trader.py:6286, 6317, 6333, 5210, 4270, 4779, 2961`.
`position_manager.current_price()` accepts `max_age_sec` (r46 #0.10 fix),
but every call site omits it. During a halt / WS-feed gap / weekend reconnect,
a 5-60-min-stale tick can drive a false stop trigger or false target advance.
**Fix:** Pass `max_age_sec=30.0` at every stop-eval call site; `15.0` at entry-side
gates.

**B7. `_signal_idempotency_key` called with wrong signature (option error path)**
File: `backend/services/auto_trader.py:4473-4478, 4965-4970`.
Function signature is `(signal: Dict)` but called as `(ticker, "BUY", timeframe=...)`.
Inside the function, `signal.get(...)` is called on a `str` → AttributeError.
**Impact:** Option error rows never dedupe → trade ledger fills with N×
"option submit failed" duplicates; transient broker errors retried without
idempotency.

**B8. `kill()` doesn't release option BP and skips non-bracket orders**
File: `backend/services/auto_trader.py:1142-1158, 1083-1095`.
- Line 1154 gates BP release on `asset_type == "stock"` → option positions never
  release BP on kill → reservation leaks across kill cycles.
- Lines 1083-1090 cancel only `parent_order_id`, `stop_order_id`, `tp_order_id` —
  trim sells, pyramid buys, cross-fallback re-submits not tracked → can fill
  post-kill, reopening exposure.

**B9. KILL switch reachability — depends on DB write that may be wedged**
File: `backend/services/auto_trader.py:1062-1073, 1132-1169`. `kill()` opens 4
sessions and commits before flipping any in-memory flag. If DB is the wedged
subsystem (the typical reason to kill), `kill()` itself blocks indefinitely.
**Fix:** Set a process-local `_KILLED = threading.Event()` at the **first line**
of `kill()` before any DB open. Have `consider_signal` and the manage tick check
`_KILLED.is_set()` first thing.

**B10. ✅ KILL button has 4-second deferred-undo before any action fires**
File: `frontend/app.js:6541-6575`. Pressing KILL triggers
`stageAction({ delayMs: 4000 })` — bot can still open positions for 4s, and tab
close cancels the staged setTimeout entirely (KILL never fires).
**Fix:** KILL must fire immediately with `confirm()` or modal; no `setTimeout`.

**B11. KILL button single POST with no retry / no sendBeacon**
File: `frontend/app.js:6552-6572`. Single `await api.post('/api/trading/kill')`.
On 5xx (the typical "wedged" condition), no retry, no localStorage-cached intent,
no `navigator.sendBeacon` fallback. Failure shown only as 8s toast.

### Risk / sizing — confirmed real bugs

**B12. `current_portfolio_heat` fails OPEN to 0.0 on any exception**
File: `backend/services/risk_manager.py:1147-1191`. A transient DB hiccup makes
the bot believe the book is empty and bypass the 10% portfolio-heat cap. Same
pattern in: `account_drawdown_multiplier`, `vol_target_multiplier`,
`adaptive_risk_multiplier`, `portfolio_kelly_book_throttle`,
`heat_aware_risk_multiplier`, `regime_concurrent_cap`, `crisis_chandelier_multiplier`.
One bug or transient outage in any layer silently disables the throttle.
**Fix:** Each function returns None on exception; `consider_signal` rejects
when any throttle is None. Emit metric on every exception path.

**B13. `vol_target_multiplier` allows scale-UP in low vol (clamp [0.5, 1.5])**
File: `backend/services/risk_manager.py:367-376`. Symmetric clamp means realized
vol = 6% → multiplier = 1.5× (uncapped scale-up during quiet markets, exactly
when blow-up risk peaks). Should be downside-only.
**Fix:** Clamp to `[0.5, 1.0]`.

**B14. `_beta` divisor in upstream multiplier can defeat RISK_MULT_CEILING**
File: `backend/services/auto_trader.py:3633` + `risk_math.py:68-71`. For a
β=0.4 ticker, `_upstream_mult` includes `/_beta = 2.5×` BEFORE the
`clamp_multiplier_stack` is applied. The clamp covers some multipliers but
the per-trade budget multiplication by `1/_beta` happens upstream.
**Fix:** Floor `_beta` at 0.6 before division; assert `clamped_stack <= RISK_MULT_CEILING`
inside `clamp_multiplier_stack`.

**B15. `clamp_multiplier_stack` doesn't clamp NEGATIVE multipliers**
File: `backend/services/risk_math.py:42-71`. A negative multiplier from a future
bug (e.g., cross-asset code returning -0.5) propagates: `min(-3.5, 2.0) = -3.5`
→ negative qty → would short-sell.
**Fix:** `clamped = max(0.0, min(raw, ceiling))`.

### Alpaca / broker integration — confirmed real bugs

**B16. ✅ `is_market_open()` single-flight uses `dir()` instead of `locals()`**
File: `backend/services/alpaca_client.py:168`. `'is_open' in dir()` checks
module-attribute names, not local variables. Plus an early `return False` path
exits before the `finally:` block that sets `_market_clock_inflight.set()`,
leaving subsequent callers stuck in `wait(timeout=5.0)`.
**Fix:** Initialize `is_open = False` before the try; refactor to ensure the
finally always runs.

**B17. `cancel_all_orders` and `close_position` have no timeout wrapper**
Files: `backend/services/alpaca_client.py:516-548` (`cancel_all_orders`),
`559-573` (`close_position`). Raw calls with no `_safe_rest_read` timeout.
Network stall during emergency flatten hangs forever.
**Fix:** Wrap in `_safe_rest_read` (entry-style timeout, no retry).

**B18. Same option entry-cross race exists on the EXIT side**
File: `backend/services/alpaca_client.py:875-920`
(`submit_option_exit_with_cross_fallback`). The r80 race-fix that was applied
to the entry-side wrapper was never applied to the exit wrapper. Comment says
"don't add 4s latency" but applies to a path where 2× fill is unbounded loss.

**B19. WS reconnect alert delayed ~25 min; no auto-disable**
File: `backend/services/live_quotes.py:421-433`. Backoff caps at 5 min; alert
fires after ≥5 consecutive failures (~25 min). Bot keeps trading on stale quotes
during that window.

**B20. `alpaca_websocket_patch.py` swallowed by `|| true` in Dockerfile**
File: `Dockerfile:23`. `RUN python alpaca_websocket_patch.py ... || true`.
If the patch path moves (alpaca-py upgrade), patch silently no-ops; runtime fails
on first WS connection with `unexpected keyword 'extra_headers'`.
**Fix:** Remove `|| true`; pin alpaca-py (already pinned) and add a runtime sanity
check.

### Database / migration — confirmed real bugs

**B21. ✅ `_ensure_column` for `idempotency_key` doesn't add UNIQUE constraint**
File: `backend/database.py:1140`. Model declares `unique=True, index=True`
(line 401), but `_ensure_column("auto_trades", "idempotency_key", "VARCHAR")`
omits both. For DBs where the column was added post-hoc (e.g., the existing
sqlite that's about to be migrated), the dedup invariant is **not enforced at
the DB level**. Two concurrent Cloud Run instances racing on the same signal
both insert → duplicate position.
**Fix:** Add a migration: `CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_trades_idempotency_key ON auto_trades(idempotency_key) WHERE idempotency_key IS NOT NULL;`

**B22. `_ensure_column` BOOLEAN defaults `0`/`1` will fail on Postgres**
File: `backend/database.py:1134, 1138`. `BOOLEAN DEFAULT 1` is valid SQLite,
invalid Postgres (must be `TRUE`/`FALSE`). Boot crashes on first ALTER.
**Fix:** Audit every `_ensure_column` BOOLEAN default; convert literals.

**B23. Migration script only migrates 4 tables out of ~30**
File: `backend/scripts/migrate_sqlite_to_postgres.py:42-47`. MODELS list:
`WatchlistStock, AutoTraderConfig, Signal, AutoTrade`. Missing 25+ tables
including `EquitySnapshot, ConfidenceCalibration, MLPrediction, DecisionLog,
TickerProfile, AIDecisionLog, MLArtifact, BestStrategyPerTicker, ...`
**Impact:** Post-cutover, empty `EquitySnapshot` → DD calc fails to 1.0 (no
throttle); empty `ConfidenceCalibration` → calibration_multiplier returns 1.0.

**B24. Migration script wipes destination tables before checking source has data**
File: `backend/scripts/migrate_sqlite_to_postgres.py:121-126`. If SQLite is empty
or corrupted, destination silently truncated. **Total data loss** with the wrong
env vars.
**Fix:** Refuse to wipe if dest has more rows than src; require operator confirm.

**B25. `auto_trades` lacks indexes on hot lookup columns**
File: `backend/database.py:359-440`. No index on `(status, ticker)` or `status`
alone. After 10k+ closed trades, every manage-tick does a full scan.
**Fix:** Add `CREATE INDEX ... ON auto_trades(status, ticker)`.

### Security / secrets — confirmed real bugs

**B26. ✅ `backend/stockapp.db` (12 MB) is committed to git**
Tracked across 2 commits. May contain real trades, AutoTraderConfig, equity
snapshots, broker order IDs.
**Fix:** `git rm --cached backend/stockapp.db backend/stockapp.db.bak`; add
`*.db` to `.gitignore` (currently only `*.db-shm`, `*.db-wal`, `*.db.bak` are
ignored). Rewrite history with `git filter-repo` before going live.

**B27. Secrets passed via gcloud CLI args (process-list / shell-history leak)**
File: `deploy.sh:98-138`. APCA keys, DB password, APP_API_KEY, ANTHROPIC_API_KEY,
FMP_API_KEY, POLYGON_API_KEY all concatenated into `--update-env-vars`. Visible in
`ps auxf`, shell history, audit tools.
**Fix:** Move secrets to GCP Secret Manager; use `--update-secrets`.

**B28. ✅ `CORS_ALLOW_ORIGINS=*` is the deploy.sh default — overrides any tightened value**
File: `deploy.sh:129`. Every `./deploy.sh` invocation appends `CORS_ALLOW_ORIGINS=*`,
overwriting any tightened value previously set on Cloud Run. The boot-time check
in `main.py:1049-1053` only refuses if `_cors_origins` is empty, so `["*"]`
silently passes.
**Fix:** Remove line 129; require `CORS_ALLOW_ORIGINS` from `.env`. Refuse `*`
when `_ALPACA_LIVE`.

**B29. `pickle.load` on isotonic calibrator fetched from DB**
File: `backend/services/ml_scorer.py:127-129`. The calibrator is hex-decoded from
`MLArtifact.content` and unpickled. Anyone who can write that table (compromised
admin key, SQL injection, leaked DB creds) gets arbitrary code execution in the
trading process — which holds Alpaca live keys in memory.
**Fix:** HMAC-sign artifacts and verify before unpickle, OR store as JSON
(isotonic boundaries + values).

**B30. `pnl_reconciliation` uses HARDCODED `paper-api.alpaca.markets` URL**
File: `backend/routers/trading.py:206-213`. With `ALPACA_LIVE=1`, this hits the
paper API with live keys. Live keys leak via DNS/proxy logs to wrong domain;
reconciliation reads a different account than the bot trades.
**Fix:** Use `alpaca_client.get_portfolio_history()` (knows live vs paper).

**B31. Two-key live gate checked at boot only**
File: `backend/main.py:979-1029`. After process is up, `alpaca_client._get_client()`
reads `APCA_API_KEY_ID/SECRET` directly — never re-checks `ALPACA_LIVE`.
**Fix:** Defense-in-depth re-assert in `submit_bracket_order`/`submit_market_order`.

**B32. `/api/trading/order` accepts qty/SL/TP without server-side risk recheck**
File: `backend/routers/trading.py:566-598`. Bypasses `max_pct_of_equity`,
`max_risk_per_trade_pct`, RISK_MULT_CEILING, daily-loss-halt, kill-flag.
**Fix:** Run the same risk-manager gate the auto-trader uses.

**B33. AI/Chat daily-call cap is process-local (races across instances)**
Files: `backend/routers/chat.py:22-34`, `backend/services/ai_judge.py:233-281`.
With `max-instances=2`, you hit `2× cap` per day; cold-start resets the dict
entirely.
**Fix:** Persist counter to DB with `INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count`.

**B34. ✅ `/metrics` endpoint mounted with NO auth**
File: `backend/services/metrics.py:178`. `--allow-unauthenticated` Cloud Run
exposes everything. Metrics leak trading activity volume, skip reasons, et al.
**Fix:** Gate with `Depends(require_api_key)` or move to internal-only port.

**B35. Multi-instance scheduler uses APScheduler MemoryJobStore**
File: `backend/main.py:236-246`. With `max-instances=2`, both api instances run
EVERY cron job (`scheduled_scan`, `news_poll`, `ml_outcome_backfill`, etc.).
Most aren't idempotent.
**Fix:** Use SQLAlchemyJobStore with row-locking, OR wrap each cron callable
with a Postgres advisory lock, OR pin scheduler to one instance.

### Signals / strategies — confirmed real bugs

**B36. Signal-freshness gate silently bypassed in LIVE path**
File: `backend/services/auto_trader.py:2822` + `signal_generator.py:1094-1110`.
`generate_signal()` never sets `generated_at`; `consider_signal` reads
`signal.get("generated_at")` → None → freshness check skipped.
**Fix:** Set `"generated_at": datetime.now(timezone.utc).isoformat()` on every
returned dict.

**B37. `_FOMC_DATES` is set of strings — `in_pre_fomc_quiet_hour` always False**
Files: `backend/services/r47_overlays.py:286`, `backend/services/factors.py:264`.
`datetime.combine(str, ...)` raises TypeError, swallowed → returns False forever.
The pre-FOMC quiet-hour gate is dead.
**Fix:** `_FOMC_DATES = {datetime.fromisoformat(s).date() for s in ...}`.

**B38. `extract_latest` reads partially-formed current bar (`df.iloc[-1]`)**
File: `backend/services/indicators.py:143`. On intraday timeframes the most-recent
bar is the open forming bar (not yet closed). All indicator values are computed
from in-progress prices that revise minute-by-minute.
**Impact:** Confidence flips up/down between scan ticks; macd-cross fires/unfires
on the same bar; stops/targets embed the wick.
**Fix:** Use `iloc[-2]` for the signal pipeline (keep `iloc[-1]` for the chart pane).

**B39. `_news_spike_fade` has no news-timestamp filter (knife-catcher)**
File: `backend/services/strategies.py:243-262`. Docstring claims layered filter;
no such filter exists. Strategy fades any 1.5×ATR bar with RVOL≥3 — many of
which are catalyst-driven moves that continue.
**Fix:** Disable until news-timestamp filter is wired, OR remove from `STRATEGY_FUNCS`.

**B40. `_vix_spike_reversion` fires long signal on EVERY ticker, not just SPY/QQQ**
File: `backend/services/strategies.py:582-606`. Says "filtered by signal_generator
convention" but no such filter exists in `signal_generator.py`. During a VIX spike,
every watchlist ticker fires a long.
**Fix:** Hard-code `if ticker not in {"SPY","QQQ","IVV","VOO"}: return empty`.

**B41. Alpaca returns tz-naive timestamps → look-ahead leak via `_is_partial_bar`**
Files: `backend/services/scanner.py:199-213, 338`. Naive timestamp →
`tz_convert` raises → `except: return False` → partial bar treated as closed.
`score_candidate` then anchors on `df.iloc[-1]` (partial) instead of `iloc[-2]`.
**Fix:** `if last_ts.tzinfo is None: last_ts = last_ts.tz_localize("UTC")`.

**B42. `_donchian_breakout` defaults to no-volume-gate when columns missing**
File: `backend/services/strategies.py:129-131`. `vol_ok = pd.Series(True, ...)`
when columns missing — the very names where the gates would matter most
silently disable. Same for `atr_ok`.
**Fix:** Return empty long/short series when indicator columns absent.

**B43. `_calibrate_long_stop` sanity-floor inverts on low-ATR names**
File: `backend/services/signal_generator.py:128-129`. `min(stop, price - 0.5*atr)`
with NaN-fallback ATR (= price * 0.02) silently TIGHTENS structural stops to 1%.
**Fix:** Apply only when `chosen > price - 0.5*atr` (close-to-price danger).
Mirror at `_calibrate_short_stop`.

**B44. `_macro_features` `as_of` (naive UTC) - `release_time_utc` (tz-aware)**
File: `backend/services/ml_features.py:153-185`. Naive minus aware raises
TypeError, swallowed → blackout flag always 0 in inference. Model trained with
real blackout flags never sees them in production.

### Tests / scheduler / infra — confirmed real bugs

**B45. Pre-deploy tests run with SQLite — Postgres-only bugs unscored**
File: `deploy.sh:62-71`. Tests pass with SQLite, prod is Postgres. Hides:
NULL-in-UNIQUE indexes, ON CONFLICT semantics, advisory locks (`_pg_advisory_entry_lock`),
JSON column comparisons, tz-aware vs naive datetimes.
**Fix:** Add a parallel pipeline against `postgres:16-alpine` (testcontainers).

**B46. Tests mock `alpaca_client.cancel_order`/`close_position`/`_current_price`**
File: `backend/tests/test_bug_scenarios.py:1133-1138`. Replaces every realistic
broker error (timeout, malformed response, missing field) with happy-path mocks.
Recent r80/r80b/r80c/r81 fixes all live in code paths these mocks hide.

**B47. WebSocket auth via `?token=` query param (logged in proxy logs)**
File: `backend/routers/stream.py:36-44`. `_auth.py` comment explicitly warns
"never as a query param — shows up in proxy logs", yet WS path uses exactly that.
**Fix:** Issue short-lived ephemeral WS tokens via auth-protected
`/api/ws/issue-token`.

**B48. Dockerfile runs as root**
File: `Dockerfile:1-51`. No `USER` directive.
**Fix:** `RUN useradd -m -u 1000 app && chown -R app /app` then `USER app`.

**B49. Liveness probe trips alert-emit path that itself opens DB**
File: `backend/main.py:1226-1235` + `services/alerts.py:99-111`. When manage-loop
stalls due to DB outage, `/api/health` writes an Alert row → hangs → liveness
probe times out → Cloud Run restarts → cycle repeats.
**Fix:** `/api/health` must not depend on DB writability. Move alert emission
to a separate watchdog cron.

**B50. EquitySnapshot upsert race: Postgres SAVEPOINT not used**
File: `backend/services/risk_manager.py:597-626`. Two instances both INSERT,
one wins via UNIQUE, the other catches Exception and rolls back — but the OUTER
session is now in aborted state. Subsequent ops fail with "current transaction is
aborted".
**Fix:** Wrap insert with `db.begin_nested()` SAVEPOINT, OR use
`pg_insert(...).on_conflict_do_update(...)`.

---

## TIER 2 — HIGH (fix in week 1 of live ops, before easing risk caps)

**H1.** `account_drawdown_multiplier` cold-start fallback returns 1.0 silently
on empty EquitySnapshot or `get_account()` failure (`risk_manager.py:411-472`).
Fix: bootstrap snapshots from Alpaca portfolio history during migration.

**H2.** `should_freeze_trading` consecutive-loss check has no recency filter — 5
losses spanning a full year can freeze trading (`risk_manager.py:969-978`).

**H3.** `force_close_trade` for stocks: 0.5s settle is too short for stop-leg
cancel; Alpaca takes 1-3s (`execution_engine.py:312-317`). Use
`close_position(cancel_orders=True)` or poll for empty open-orders.

**H4.** `force_close_trade` for options: 3s settle window allows multi-instance
re-entry race — flip status to `"closing"` before sleep (`execution_engine.py:388-394`).

**H5.** SL-resubmit hardcodes GTC; bypasses `bracket_tif=day` weekend safety
(`auto_trader.py:6233`).

**H6.** SL-resubmit fires every tick (60×/hr) without back-off
(`auto_trader.py:6240-6266`). Add per-trade exponential back-off; escalate to
critical alert on 5th failure.

**H7.** EOD flatten window starts at 15:55; with 60s manage cadence, last
trades flatten 15:58 (`auto_trader.py:5798`). Move to 15:50 dedicated cron.

**H8.** Reverse-thesis grace window uses `t.opened_at` for adopted positions
— treats year-old adoptions as fresh entries (`position_manager.py:316-339`).

**H9.** Bracket order with `extended_hours=True`: child SL/TP legs cannot fill
in extended hours → naked-long until 9:30 RTH (`auto_trader.py:5928-5946` /
`alpaca_client.py:415-417`). Refuse bracket entries when market closed.

**H10.** Adopted-position auto-promote bypasses risk caps (size, sector,
correlation, concurrent-position) (`auto_trader.py:1438-1492`).

**H11.** BP decay over-zeroes when operator places hedge at same time
(`risk_manager.py:131-137`). Decay only by `min(drop, _in_flight_bp_reserved)`.

**H12.** `_release_bp` post-options-trim never fires — runner reservation stays
inflated until close.

**H13.** Frontend WS reconnect-storm has no max-attempts cutoff after 1008
auth-fail (`app.js:706-732`). Rotated `APP_API_KEY` mid-session = endless storm.

**H14.** Frontend localStorage API-key persistence ("Remember me") —
XSS-exfiltratable. Drop the feature; sessionStorage only.

**H15.** Frontend `openReasoningPopup` injects unescaped `ticker`, `confidence`,
`timeframe` into popup HTML (`app.js:4877-4912`). Stored XSS executable in
operator session.

**H16.** Polling cascade: 11+ independent setIntervals, none stop on
consecutive 5xx (`app.js`). Backend brownout = self-DDoS.

**H17.** `book_var_99` is parametric normal × 2.33σ — under-states fat-tailed
risk by 30%+. Switch to historical-simulation VaR over EquitySnapshot series.

**H18.** Health probe leaks operational state (`crisis_mode`, `killed`,
`session_dd_pct`, etc.) on unauthenticated endpoint. Move rich payload behind
auth at `/api/health/full`.

**H19.** Frontend-error log endpoint is unauthenticated (`main.py:916-928`)
— fill alerts with attacker spam.

**H20.** `/api/webhooks/fmp/sec` requires API key but FMP can't send X-API-Key
— either dead, or APP_API_KEY shared with FMP. Use per-webhook signing secret.

**H21.** Confidence stack clamp doesn't constrain post-hoc multiplier
overlays (regime × backtest × Kelly) — confidence can exceed advertised cap.

**H22.** Setup-quality `_norm_freshness` returns 0.5 (neutral) when age missing
— combined with B36, every live signal gets neutral freshness without measurement.

**H23.** Pyramid SL-resize uses `replace_order_by_id` (B3) — pyramided shares
naked between trim and next manage tick.

**H24.** `_inside_bar_breakout` lacks RVOL/ATR confirmation gates that
Donchian has (`strategies.py:478-503`).

**H25.** Sector cap query may not catch NULL-sector legacy rows
(`auto_trader.py:3263-3271, 3298-3310`).

**H26.** `_open_allocations` uses entry_price for adopted positions (drift over
time) — under-counts allocation, over-opens positions.

**H27.** `chandelier_atr` cache miss not negative-cached → yfinance retried
every 60s during outage (`position_manager.py:61-80`).

**H28.** Stop-eval `slippage_aware_risk_per_share` floor at $0.01 distorts
sub-$1 / penny names. Use `max(0.01, 0.001 * entry)`.

**H29.** Test bench has no KILL-persistence test, no race-condition test
between manage_open_positions and consider_signal, no short-position sizing test.

**H30.** Manager service deploy script doesn't enforce `--ingress=internal`.

**H31.** Pre-deploy tests use `python3 -m unittest ... | tail -8` — broken
import shows "OK" from prior suite. Pipefail saves it but should use pytest
with `--strict-markers --co`.

**H32.** `gate_telemetry._hindsight_pnl_pct` is BUY-direction-only; SHORT
gate hindsight is nonsense.

**H33.** Database hot rows (`AutoTraderConfig` 50+ cols) — every config read is
the full row, write/read contention on every manage tick.

**H34.** `IDEMPOTENCY_LOOKBACK_HOURS=4` with date-stamped key is largely
redundant; cleaner if removed.

**H35.** `/api/cancel-all`, `/close-all`, `/promote-adopted`, `/sync-positions`
mutate broker state with no confirm-token. Add `{confirm: "..."}` body.

**H36.** Cloud Run pool sizing: `pool_size=5, max_overflow=3` × 3 instances =
24 slots vs 22 budget. PgBouncer is in BACKLOG, not shipped.

---

## TIER 3 — MEDIUM (fix between weeks 1-4 of live ops)

50+ findings. Highlights:

- `_FOMC_DATES` import inside hot loops (`r47_overlays.py:279`).
- `extract_latest` `safe()` allows `bool` through `float()` coercion.
- `pivot_points` re-fetches daily bars per signal (cache miss).
- Setup-quality `_norm_adx` returns 0.5 neutral when ADX missing.
- 17 `try: from services.X import Y` blocks inside `generate_signal()`.
- `_post_mortem` writes details with full signal blob (unbounded payload).
- `compute_confidence_calibration` reads ALL closed trades (no `days` cap).
- `_resample_to_4h` aligns to UTC midnight, not market session.
- `_BoundedTradeCache` LRU semantics broken (cache thrash).
- Two-decimal price rounding distorts sub-$1 names.
- `_pull_universe_alpaca` filters `"."` in symbol → drops BRK.B etc.
- `_within_earnings_window` fails-CLOSED on every yfinance hiccup.
- `score_candidate.days_since_hi` algebraic error (subtracts argmax_rev twice).
- Many `risk_manager` helpers open their own SessionLocal (pool churn).
- `record_equity_snapshot` calls `get_positions()` twice.
- `chandelier_atr_cache` survives no process restart (cold-cache hits yfinance hard).
- `cancel_order` polls without `_safe_rest_read` (160 calls during multi-position kill).
- Schedule cron job hash-collision potential.
- Schemas: `extra="allow"` lets typos persist into downstream code.
- `TradeContext.qty: float` — no validator rejects negative qty for buy.
- Alert webhook fires in unbounded thread per call (1000 alerts = 1000 threads).
- `kv()` log helper does not redact secrets.
- `_JsonFormatter` could leak `Authorization: ...` from httpx Response repr.
- Dockerfile sets `SSL_CERT_FILE=""` and `REQUESTS_CA_BUNDLE=""` — verify httpx behavior.
- `frontend/build.js` ships sourcemap to production (logic exposed).
- `ml_trainer` writes pickle to `/tmp/ml_models/` (Cloud Run wipes on cold start).
- Many more in the agent reports.

---

## TIER 4 — LOW (cosmetic / future-proofing)

20+ findings — magic numbers, unpinned dev deps, comment errors, dead code paths,
inconsistent ticker regex, etc.

---

## Summary by area

| Area | Critical | High | Medium | Low |
|---|---|---|---|---|
| Money path (auto_trader, execution, position_manager) | 12 | 10 | 8 | 5 |
| Risk (risk_manager, risk_math) | 4 | 6 | 12 | 4 |
| Alpaca / WS | 5 | 4 | 4 | 1 |
| Database / migration | 5 | 3 | 4 | 2 |
| Signals / strategies | 8 | 12 | 14 | 4 |
| Security / secrets | 8 | 5 | 4 | 2 |
| Frontend | 5 | 6 | 3 | 2 |
| Tests / scheduler / infra | 3 | 8 | 6 | 0 |

**Total: ~50 Critical, ~54 High, ~55 Medium, ~20 Low**

---

## Going-live blocker checklist

Cannot flip `ALPACA_LIVE=1` until all of these are resolved:
- [ ] B1 (promote_adopted broken)
- [ ] B2 (uncommitted trim mutations)
- [ ] B3 (replace_order_by_id wrong receiver)
- [ ] B4 (_backfill_ml_outcome arg order)
- [ ] B5 (regime classifier dead)
- [ ] B6 (stop-eval stale price)
- [ ] B7 (idempotency-key call signature)
- [ ] B8 (kill leaks option BP, misses non-bracket orders)
- [ ] B9 (kill depends on DB write)
- [ ] B10 (KILL button 4s deferred-undo)
- [ ] B11 (KILL button no retry / sendBeacon)
- [ ] B16 (is_market_open broken single-flight)
- [ ] B17 (cancel_all_orders / close_position no timeout)
- [ ] B18 (option exit cross race)
- [ ] B20 (alpaca_websocket_patch silent fail)
- [ ] B21 (idempotency_key UNIQUE not enforced)
- [ ] B22 (BOOLEAN DEFAULT 0/1 fails Postgres)
- [ ] B23 (migration script: 4 of 30 tables)
- [ ] B24 (migration wipes empty source)
- [ ] B26 (stockapp.db committed to git)
- [ ] B27 (secrets in gcloud CLI args)
- [ ] B28 (CORS=* default)
- [ ] B29 (pickle from DB)
- [ ] B30 (paper-api hardcoded in pnl_reconciliation)
- [ ] B32 (/api/trading/order bypasses risk gates)
- [ ] B33 (AI cap process-local)
- [ ] B34 (/metrics no auth)
- [ ] B35 (multi-instance scheduler)
- [ ] B36 (signal freshness gate dead in live path)
- [ ] B37 (FOMC quiet-hour dead)
- [ ] B38 (extract_latest uses partial bar)
- [ ] B39 (news_spike_fade is knife-catcher)
- [ ] B40 (vix_spike_reversion fires on every ticker)
- [ ] B41 (Alpaca tz-naive → look-ahead leak)
- [ ] B42 (donchian no-vol-gate fallback)
- [ ] B43 (calibrate_long_stop low-ATR inversion)
- [ ] B44 (macro features tz mismatch)
- [ ] B45 (tests use SQLite, prod is Postgres)
- [ ] B46 (tests mock the broker)
- [ ] B49 (health probe self-deadlock on DB outage)
- [ ] B50 (EquitySnapshot Postgres SAVEPOINT)

Plus B12-B15 (risk multiplier fail-open / scale-up / negative clamp / β divisor)
which while not strictly broken today have asymmetric blow-up risk during live.
