# Tier-1 Fixes Applied (2026-05-10, "r82" comment tag)

All fixes verified to compile; smoke tests (51) + bug-scenarios suite (183) all
pass. Detailed audit findings in `AUDIT_SUMMARY.md`.

## Applied — money path / live-blocker bugs

| ID | File | Fix |
|---|---|---|
| **B1** | `services/auto_trader.py:1587` | Add `cfg = get_config(db)` in `promote_adopted_to_managed` (was NameError → adopted positions naked-long forever). |
| **B2** | `services/auto_trader.py:5306-5318, 6491-6505, 6565-6571, 6614-6628` | Commit `t.qty -= trim_qty` / `+= add` mutations immediately after broker trim/pyramid; remove `db.refresh(t)` that wiped them (was → progressive over-liquidation if SL-replace failed). |
| **B3** | `services/auto_trader.py:6568-6585` | Pyramid SL-resize now calls `c.replace_order_by_id(..., order_data=ReplaceOrderRequest(qty=...))` instead of nonexistent `alpaca_client.replace_order_by_id` (was AttributeError → pyramided shares naked between trim & next manage tick). |
| **B4** | `services/auto_trader.py:6193` | Swap `_backfill_ml_outcome(t, db)` → `(db, t)` (was AttributeError → SL-fill closes never backfilled ML outcomes). |
| **B5** | `services/regime_router.py:72-99` | Fix `from market_context import vix` → `current_vix`; fix `from indicators import adx` → `compute_indicators(...)["ADX_14"]` (was ImportError → regime classifier permanently dead → strategy_off_regime gate never fired). |
| **B36** | `services/signal_generator.py:1094, 1128` | Set `generated_at` on every signal dict so live-path freshness gate actually fires. |
| **B37** | `services/macro_calendar.py:44`, `r47_overlays.py:278`, `factors.py:257` | Convert `_FOMC_DATES` to `_FOMC_DATE_OBJS` (parsed `date` objects); update consumers (was iterating string set → TypeError → FOMC quiet-hour gate dead). |
| **B39** | `services/strategies.py:243-256` | Disable `_news_spike_fade` (knife-catcher with no news context, no edge). |
| **B40** | `services/strategies.py:575-587`, `best_strategy.py:58` | Filter `_vix_spike_reversion` to SPY/QQQ/IVV/VOO (docstring promise was a lie); stamp `df.attrs["ticker"]` in best_strategy caller. |
| **B41** | `services/scanner.py:198-227` | Localize naive timestamps to UTC before `tz_convert` (was → partial bar treated as closed → look-ahead leak on Alpaca-sourced data). |

## Applied — risk / broker / DB

| ID | File | Fix |
|---|---|---|
| **B13** | `services/risk_manager.py:367-381` | `vol_target_multiplier` clamp [0.5, 1.5] → [0.5, 1.0] (was scaling UP in low-vol regimes, the wrong direction). |
| **B15** | `services/risk_math.py:65-75` | `clamp_multiplier_stack` now floors at 0.0 (was: a negative upstream mult could flip qty negative → wrong-side order). |
| **B16** | `services/alpaca_client.py:140-180` | `is_market_open`: pre-init `is_open=False`; replace `'is_open' in dir()` (wrong namespace) with direct read in finally. |
| **B17** | `services/alpaca_client.py:524-535, 569-587` | Wrap `get_orders` (in `cancel_all_orders`), `cancel_order_by_id`, and `close_position` with `_safe_rest_read` timeouts (KILL flow could hang forever). |
| **B21** | `backend/database.py:1088-1136` | Add `_mig_002_idempotency_unique` (partial UNIQUE index on `auto_trades.idempotency_key`) and `_mig_003_auto_trades_status_index`. |
| **B22** | `backend/database.py:1134, 1138` | `BOOLEAN DEFAULT 0/1` → `BOOLEAN DEFAULT FALSE/TRUE` (Postgres rejects integer defaults for BOOLEAN). |
| (B86) | `backend/database.py:1118-1130` | `INSERT INTO schema_migrations` → `ON CONFLICT DO NOTHING` / `INSERT OR IGNORE` (multi-instance boot safe). |
| **B30** | `backend/routers/trading.py:206-215` | `pnl_reconciliation` now uses live API base URL when `ALPACA_LIVE=1` (was hardcoded paper-api → leaked live keys via DNS to wrong host). |
| **B32** | `backend/routers/trading.py:572-624` | `/api/trading/order` now runs server-side risk recheck (kill flag, BP/broker breaker, 25%-of-equity notional cap). |

## Applied — security / deployment

| ID | File | Fix |
|---|---|---|
| **B26** | `.gitignore`, `git rm --cached` | Add `*.db`; untrack `backend/stockapp.db` and `.bak` (12 MB live data was committed). **Note:** repo history still contains the file — separate `git filter-repo`/BFG rewrite required before the repo is shared publicly. |
| **B28** | `deploy.sh:126-141` | Remove `CORS_ALLOW_ORIGINS=*` default; require it from env; refuse `*` when `ALPACA_LIVE=1`. |
| **B34** | `services/metrics.py:171-186` | Gate `/metrics` with `Depends(require_api_key)`. **Note:** any Prometheus scraper must now send `X-API-Key`. |
| **B20** | `Dockerfile:22-27` | Drop `\|\| true` on `alpaca_websocket_patch.py` apply (silent no-op was → WS dead at runtime). |

---

## Applied — KILL switch chain (B9/B10/B11)

| ID | File | Fix |
|---|---|---|
| **B9** | `services/auto_trader.py` | Added `_KILLED = threading.Event()` and `set_killed_flag()` / `clear_killed_flag()` / `hydrate_killed_flag_from_db()`. `kill()` now sets the in-memory flag as the FIRST line, before any DB session opens — so a wedged DB cannot prevent KILL from blocking new entries. DB persistence becomes best-effort (still happens for audit trail + restart). `consider_signal`, `consider_event`, `consider_put_play`, `consider_call_play` check `is_killed_in_memory()` first thing. `unkill()` clears the in-memory flag AFTER the DB row is updated (crash-safe order). `main.py` lifespan calls `hydrate_killed_flag_from_db()` at boot; if DB unreachable, fail-CLOSED (assumes killed). |
| **B10** | `frontend/app.js` | KILL button replaced 4-second `stageAction` deferred-undo with a synchronous `confirm()` dialog. Fires immediately on OK. Pre-write of `pushNotification` and `localStorage.killIntent` BEFORE the network request so a tab close mid-fire is recoverable. |
| **B11** | `frontend/app.js` + `routers/_auth.py` + `routers/trading.py` + `main.py` | KILL POST now retries 3× with backoff (250ms, 500ms, 750ms) under a 6s AbortController timeout per attempt. Final fallback: `navigator.sendBeacon('/api/trading/kill?_k=...')` so the request commits even on tab unload. New `kill_router` sub-router uses `require_api_key_kill` which accepts `?_k=...` query-param fallback (sendBeacon can't set headers). On mount, the frontend replays any persisted `killIntent` from localStorage so a prior-session interrupted KILL re-fires. |

## Applied — migration script (B23/B24)

| ID | File | Fix |
|---|---|---|
| **B23** | `backend/scripts/migrate_sqlite_to_postgres.py` | MODELS list expanded from 4 to 26 (every ORM model defined in `database.py`). Specifically restores: `EquitySnapshot` (drives DD multiplier), `ConfidenceCalibration`, `MLPrediction`, `DecisionLog`, `TickerProfile`, `BestStrategyPerTicker`, `AIDecisionLog`, `Alert`, `MLArtifact`, `MLEvalResult`, `Fundamentals`, `AnalystRating`, `MacroEvent`, `IVHistory`, `NewsEvent`, `WSBMention`, `SocialSentiment`, `InstitutionalHoldings`, `InsiderSummary`, `ScanRun`, `CandidatePool`, `CandidateEvent`. |
| **B24** | `backend/scripts/migrate_sqlite_to_postgres.py` | Pre-flight summary that prints source vs destination row counts BEFORE wiping. Refuses to wipe any table where dest > src + 10 (guards against URLs swapped). Requires `--confirm I_UNDERSTAND_DESTRUCTIVE` to proceed past dry-run mode. Echoes both source/dest hostnames (redacted) so operator can verify. |

## Applied — DB-backed AI cap (B33)

| ID | File | Fix |
|---|---|---|
| **B33** | `database.py` (new model `AICallBudget`), `services/ai_call_budget.py` (new), `services/ai_judge.py:_ai_budget_check`, `routers/chat.py:_chat_budget_check` | New `ai_call_budget` table with PK `(date, channel)`. Atomic single-statement increment: `INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count` (Postgres + modern SQLite both supported). Replaces per-process in-memory counters that could be exceeded by Nx with multi-instance Cloud Run + cold restarts. Fail-OPEN on DB error (a cost cap should not refuse every AI call during a DB blip). |

## Applied — Multi-instance scheduler (B35)

| ID | File | Fix |
|---|---|---|
| **B35** | `backend/main.py` | Added `_with_singleton_lock(job_id)` decorator that wraps any callable with a Postgres `pg_try_advisory_lock(hash(job_id))` — the lock acquires for the duration of the call and releases at the end. If another instance holds the lock, the wrapper logs at DEBUG and returns None. No-op on SQLite. Monkey-patched `scheduler.add_job` so EVERY cron callable is auto-wrapped (avoids editing 25 individual call sites). Opt-out via `_singleton=False` kwarg. |

## Applied — Health probe (B49)

| ID | File | Fix |
|---|---|---|
| **B49** | `backend/main.py`, `deploy.sh`, `deploy-manager.sh` | New `GET /api/healthz` returns `{"ok": true}` with NO DB or broker calls (microsecond response). Deploy scripts now point Cloud Run liveness + startup probes at `/api/healthz` instead of `/api/health`. Removed the alert-emit calls from the `/api/health` handler (stream_stale + manage_loop_stuck) and moved them into a new `_health_watchdog_tick` cron that runs every 60s. Prevents the prior failure mode where a wedged DB caused alert inserts to hang the probe handler past its 5s timeout, triggering Cloud Run liveness restart loops. |

---

## NOT applied — needs your decision (architectural / multi-file refactors)

These would meaningfully change live behavior. I recommend discussing the
approach before I touch them.

### B9 + B10 + B11 — KILL switch reachability (CRITICAL)
The KILL endpoint depends on a DB write (`cfg.killed=True`); the frontend
button uses a 4-second `stageAction` deferred-undo and a single non-retried
POST. If DB is the wedged subsystem (the typical reason to kill), kill itself
can hang.

**Proposed:** add a process-local `_KILLED = threading.Event()` set at the
*first line* of `kill()` (before any DB open); `consider_signal` /
`manage_open_positions` check it first. Frontend: replace `stageAction({delayMs:4000})`
with a single `confirm()`; retry POST 3× + fallback to `navigator.sendBeacon`;
cache an "intent to kill" in localStorage so reload re-fires.

### B23 + B24 — SQLite → Postgres migration script
The script in `backend/scripts/migrate_sqlite_to_postgres.py` only migrates 4
of ~30 tables (missing EquitySnapshot, ConfidenceCalibration, MLPrediction,
DecisionLog, etc.); silently truncates destination tables before checking source
has data. Cutover with this script → empty `EquitySnapshot` → DD throttle dead;
or, if env vars are wrong, total data loss.

**Proposed:** rewrite to migrate every ORM model in `models.py`; refuse to wipe
if dest has more rows than src; require operator confirmation of source/dest
hostnames.

### B29 — pickle from DB (RCE risk)
`services/ml_scorer.py:127-129` unpickles a calibrator hex-decoded from
`MLArtifact.content`. Any DB-write path = arbitrary code execution in the
trading process.

**Proposed:** HMAC-sign artifacts and verify before unpickle; or store
calibrator as JSON (isotonic boundaries + values).

### B33 — AI / chat daily-call cap is process-local
With `max-instances=2`, each instance has its own counter; cold-start resets it.
Effective cap is `2 × N × cold_start_count`. Anthropic billing risk.

**Proposed:** `AICallBudget(date PRIMARY KEY, count, cost_usd)` table;
`INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count`.

### B35 — Multi-instance scheduler races
APScheduler uses MemoryJobStore; with `max-instances=2`, BOTH api instances
run every cron job (scheduled_scan, news_poll, ml_outcome_backfill, etc.).

**Proposed:** SQLAlchemyJobStore with row-locking, OR pg_advisory_lock wrapper
on every cron callable, OR pin api to `max-instances=1`.

### B45 + B46 — Test infrastructure (SQLite + mocked broker)
Tests run on SQLite; production is Postgres. Tests mock `alpaca_client.cancel_order`,
`close_position`, `_current_price`. The recent r80/r80b/r80c/r81 fixes all live
in code paths these mocks hide.

**Proposed:** add a parallel pipeline against `postgres:16-alpine` via
testcontainers; integration test mode that runs against actual paper Alpaca
nightly.

### B49 — Health probe self-deadlock
`/api/health` writes Alert rows on stale streams; if the underlying issue is
the DB itself being unresponsive, the alert insert hangs → liveness probe times
out → Cloud Run restarts → cycle repeats.

**Proposed:** `/api/health` becomes pure read (no DB writes); separate watchdog
cron handles alert emission.

### B27 — Secrets in gcloud CLI args
`deploy.sh` concatenates APCA keys + DB password + APP_API_KEY +
ANTHROPIC_API_KEY into `--update-env-vars`. They appear in `ps auxf` and shell
history.

**Proposed:** move to GCP Secret Manager; `--update-secrets=APCA_API_KEY_ID=apca-id:latest`.

### B6, B7, B8, B12, B14, B18, B19, B25 (and all of Tier 2/3)
Many more findings worth addressing before easing risk caps, but each one
deserves a dedicated commit + test rather than a batch edit. Recommend taking
them in groups of 3-5 over the next week of paper-trading.

---

## Files changed

```
M  .gitignore
M  Dockerfile
M  deploy.sh
M  backend/database.py
M  backend/routers/trading.py
M  backend/services/alpaca_client.py
M  backend/services/auto_trader.py
M  backend/services/best_strategy.py
M  backend/services/factors.py
M  backend/services/macro_calendar.py
M  backend/services/metrics.py
M  backend/services/r47_overlays.py
M  backend/services/regime_router.py
M  backend/services/risk_manager.py
M  backend/services/risk_math.py
M  backend/services/scanner.py
M  backend/services/signal_generator.py
M  backend/services/strategies.py
D  backend/stockapp.db        (untracked — file remains on disk)
D  backend/stockapp.db.bak    (untracked — file remains on disk)
+  AUDIT_FULL.md, AUDIT_SUMMARY.md, AUDIT_FIXES_APPLIED.md
```

## Verification

- `python3 -m unittest tests.test_smoke` — 51 tests OK
- `python3 -m unittest tests.test_bug_scenarios` — 183 tests OK
- `python3 -m py_compile` on all 15 modified Python files — clean
