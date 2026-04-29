# Going Live with Real Money — Configuration Checklist

> **Read this entire document before flipping `ALPACA_LIVE=1`.** The settings
> below are intentionally conservative; first month live is about *not
> blowing up*, not maximizing. Every section has a "why" line — when you
> later relax a setting, you'll know what trigger to watch for.

## Pre-flight gates (must ALL be ✅ before changing any config)

- [ ] **≥ 200 closed paper trades** in the trailing 30 days, post-r40 codebase
- [ ] **Positive expectancy** on those trades (`/api/backtest/portfolio/run`
  → `expectancy > 0`)
- [ ] **Sharpe ≥ 1.0** on the same window
- [ ] **Max drawdown ≤ 15%** of starting equity (Monte Carlo p95 ≤ 20%)
- [ ] **Strategy-drawdown alert** has not fired in last 7 days
- [ ] **AI entry-veto shadow log** reviewed: ≥ 200 decisions, skip-reasons
  are concrete (not "feels frothy" / "VIX is elevated")
- [ ] **PDT day-trade counter** is 0 for past 5 business days (`/api/trading/auto/pdt`)
- [ ] **Cloud SQL backup tested**: ran `gcloud sql backups create` once,
  verified backup ID returned, restored to a test instance, dropped test
- [ ] **Manual kill drill rehearsed** in paper: hit `/api/trading/kill`,
  verified all positions flattened + DB rows updated to `closed_kill`
- [ ] **128/128 regression tests pass** AND ruff lint clean on the
  exact commit you're about to deploy
- [ ] **Cloud Run logs filter saved** for these alert categories:
  `force_close_failed`, `sl_resubmit_storm`, `strategy_drawdown`,
  `manage_loop_stuck`, `bp_breaker`, `broker_down`
- [ ] **External uptime monitor** (UptimeRobot or similar) pinging
  `/api/health` every minute and pagering on 5xx
- [ ] **Phone notifications enabled** for browser tab via the
  `target_hit` / `trade_closed` push events

---

## Cloud Run env vars (`gcloud run services update stockrecs --update-env-vars`)

```bash
# === SAFETY GATES ===
# BOTH must be set together; either alone is ignored
ALPACA_LIVE=1
I_UNDERSTAND_LIVE_RISK=yes

# === ALPACA LIVE CREDENTIALS ===
# DIFFERENT keys from paper. Get from app.alpaca.markets → API Keys → Live.
# Rotate after first month. Never share, never commit.
APCA_API_KEY_ID=<live-key-id>
APCA_API_SECRET_KEY=<live-secret>

# === HARD SECRETS ===
# Strong random; rotate quarterly. WS connections also require this.
APP_API_KEY=<32+ char hex>

# === CORS — NOT `*` IN PRODUCTION ===
# List ONLY the exact origins you use; remove `*` if currently set.
CORS_ALLOW_ORIGINS=https://stockrecs-zcm5tboivq-uc.a.run.app,https://your-frontend-host

# === DATABASE — must be Postgres on live (live boot REFUSES sqlite per r44) ===
DATABASE_URL=postgresql://...    # Cloud SQL / Neon / etc — NOT sqlite

# === DATA FEED — keep SIP if you have Algo Trader Plus ===
ALPACA_DATA_FEED=sip
ALPACA_OPTIONS_FEED=indicative
ALPACA_NEWS_STREAM=1
# r44: ALPACA_OPTIONS_STREAM defaults ON now (was off). Keep on if you have
# Algo Trader Plus (OPRA feed) — required for the marketable-limit option
# exit to actually save spread vs market orders.
ALPACA_OPTIONS_STREAM=1

# === r46: STOP-LIMIT vs STOP-MARKET ===
# 0 = legacy stop-MARKET (gap-through fills at gap price during halts /
# flash crashes — May 2010 / Aug 2024 yen unwind tail).
# 0.005 = stop-LIMIT with 50bps band: caps gap-through but risks no-fill
# on real moves (the manage tick re-evaluates and resubmits if so).
# RECOMMENDED for live: 0.005.
STOP_LIMIT_OFFSET_PCT=0.005

# === r45: ML model directory ===
# Default /tmp/ml_models is volatile on Cloud Run. Mount a persistent
# disk OR rely on the DB-mirrored artifact path (default behavior).
ML_MODEL_DIR=/tmp/ml_models

# === r44: AI cost ceilings ===
# Caps Claude bill in case of feedback bug / leaked key. Defaults are
# generous; lower if you want a tighter ceiling.
AI_DAILY_CALL_CAP=5000
CHAT_DAILY_CALL_CAP=500
AI_NEWS_EXIT_MAX_AGE_MIN=30      # ignore news > 30min old

# === r43-r46: optional universe override ===
# Default universe is Alpaca alphabetical first 500 (A-C heavy bias).
# Provide a constituent file (S&P500 / Russell 1000) to override.
STOCK_UNIVERSE_FILE=/data/sp500_constituents.txt   # one ticker per line

# === ANTHROPIC (for AI judge + chat) ===
ANTHROPIC_API_KEY=<your-key>

# === AI JUDGE MODES ===
# Promote ONLY entry_veto on day 1 (it's been in shadow longest);
# keep the other two in shadow for 2 more weeks of fresh live data.
AI_ENTRY_VETO_MODE=active           # was: shadow
AI_NEWS_EXIT_MODE=shadow            # keep shadow until reviewed live
AI_CONFIDENCE_MULT_MODE=shadow      # keep shadow until reviewed live

# === RATE LIMIT — keep defaults ===
APP_RATE_LIMIT_PER_MIN=300
APP_RATE_LIMIT_BURST=60

# === LOGGING ===
LOG_JSON=1
SENTIMENT_BACKEND=vader             # FinBERT later if shadow shows it helps
```

---

## AutoTraderConfig (`POST /api/trading/auto/config`)

Conservative first-month profile. JSON to send:

```json
{
  "enabled": false,                          // ⚠️ flip to true MANUALLY after 24h smoke

  // ── Safety gates ──
  "killed": false,                           // unset any prior kill
  "pdt_enforce": true,                       // r41: HARD GATE on margin <$25k
  "dry_run": false,                          // start true for 24h, then false

  // ── Sizing (HALVED from paper defaults) ──
  "max_risk_per_trade_pct": 0.01,            // was 0.02 — halve for first month
  "confidence_threshold": 80,                // was 75 — only highest-conf live
  "max_concurrent_positions": 5,             // was 15 — start tight
  "max_per_sector": 2,                       // was 5 — minimize correlated exposure
  "daily_loss_limit_pct": 0.02,              // was 0.03 — earlier halt

  // ── Bucket allocation ──
  "max_pct_of_equity": 0.30,                 // was 0.50 — only 30% deployed
  "stock_pct_of_equity": 0.25,               // was 0.40
  "option_pct_of_equity": 0.05,              // r39: already 0.05 — KEEP

  // ── Asset types ──
  "trade_options": false,                    // ⚠️ stocks only for first 4 weeks
  "trade_calls": false,                      // ⚠️ until ≥100 closed live stock trades
  "aggressive_options_mode": false,

  // ── Execution ──
  "entry_order_type": "limit_at_mid",        // avoid market-order slippage
  "flatten_by_eod": true,                    // no overnight exposure
  "signal_timeframes": "1h,4h,1d",           // no intraday — fewer noise trades
  "stop_atr_mult": 2.0,
  "chandelier_atr_mult": 3.0,

  // ── Universe ──
  "use_universe_scanner": false,             // ⚠️ watchlist only for known names
  "universe_top_n": 30,
  "ticker_blacklist": "VTWO,CNTA",           // recent paper losers; add as discovered
  // r54/r55 universe-scanner knobs:
  "universe_scoring_v2": "shadow",           // off | shadow | active. r55 fixed
                                             //   shrinkage + residualization +
                                             //   CHOP-inversion bugs. Promote to
                                             //   active after 5 trading days
                                             //   shadow comparison.
  "universe_scanners_enabled": "breakout",   // csv subset of {breakout,pead,
                                             //   sector_rel,vol_exp}. r55
                                             //   re-implemented sub-scanners
                                             //   with real heuristics; turn
                                             //   each on individually after
                                             //   ≥30 trades from each.
  "universe_tod_profiles_enabled": false,    // bool — TOD-aware factor weights
  "include_sector_etfs": false,              // bool — append XLK/XLF/etc to universe
  "entry_1m_gate_mode": "relaxed",           // r55 T1 #9: strict | relaxed | off.
                                             //   Default flipped from r54
                                             //   strict (= single-bar gate)
                                             //   to relaxed (2-of-3 majority).
                                             //   Watch /auto/skip-counts for
                                             //   one_min_bar_disagrees count;
                                             //   if entries-that-immediately-
                                             //   wick spike, flip to strict.

  // ── ML ──
  "ml_scoring_enabled": false,               // KEEP shadow until ≥200 live closed trades

  // ── r44/r46: NEW risk overlays (defaults; tighten if needed) ──
  "vol_target_annual": 0.12,                 // r44: portfolio realized-vol target
  "leverage_cap": 1.5,                       // r44: gross_notional / equity ceiling
  "book_var_99_cap_pct": 0.05,               // r44: heat × 1.5 cap as % of equity
  "max_correlated_open": 1,                  // r43: ρ ≥ 0.7 with N other open trades

  // ── r43/r44: existing overlay knobs ──
  "auto_promote_adopted": false,             // r41: external positions stay manual
                                             //      until you've watched the bot a week
  "rr_min": 1.3,                             // r43: net post-cost R:R floor

  // ── r46: NEW per-trade structural ──
  "bracket_tif": "day",                      // r47: default flipped from "gtc" to "day"
                                             //      (closes weekend-gap exposure).
                                             //      Manage tick re-arms next session.
  "pyramid_enabled": false,                  // r44/r47: now actually working — was
                                             //      silently dead since r44 due to
                                             //      `signal` NameError. Leave OFF
                                             //      until ≥50 closed trades.

  // ── r47: Tier P overlays (all on by default; toggle individually) ──
  "halt_detect_enabled": true,               // skip entries when WS quote stale >30s in RTH
  "iv_rank_graded_sizing": true,             // graded vs binary IV-rank veto
  "vix_spike_strategy_enabled": true,        // VIX 5σ spike → SPY long
  "spx_trend_gate_enabled": true,            // SPY < 200dSMA cuts long sizing 50%
  "credit_spread_circuit_breaker_enabled": true,  // HYG/LQD z<-2σ → veto longs
  "wash_sale_cooldown_days": 0,              // raise if running tax-sensitive account
  "option_dte0_flatten_hour_et": 15,         // force-close options at 15:00 ET on expiry

  // ── r48: BACKLOG-implementation knobs (all on by default) ──
  "factor_strategies_enabled": true,         // 12-1 momentum, BAB, yield-curve, oil, DXY, real-yield, FOMC surprise, etc.
  "flow_strategies_enabled": true,           // spread-widening defer + aggressor-flow gate
  "portfolio_max_vega_pct": 0.0005,          // 0.05% × equity per 1-vol move
  "portfolio_max_gamma_pct": 0.0002,         // 0.02% × equity
  "portfolio_max_net_delta_pct": 0.50,       // 50% of equity in net-delta
  "ai_daily_usd_cap": 20.0,                  // alert when ai_cost_today > $20
  "ml_drift_brier_alert_threshold": 0.05,    // alert when live brier vs trained > 0.05
  "index_inclusion_tickers": ""              // comma-separated tickers eligible for Russell/MSCI nudge
}
```

---

## r43-r47 post-deploy verification (do FIRST after the env vars deploy, before flipping `enabled=true`)

Several r43-r47 systems are silent on day 1 — they activate only after
data accumulates. Verify each is working:

| System | Activation requirement | Verification |
|---|---|---|
| EquitySnapshot (r46) | 5 min snapshots × 25 min RTH | `GET /api/trading/equity-curve?lookback_days=1` returns ≥5 snapshots |
| `account_drawdown_multiplier` (r46) | ≥5 EquitySnapshot rows | `GET /api/health` shows `account_dd_mult` non-null after ~30 min RTH |
| `crisis_mode` (r46) | Always live; True only when ≥5% multi-day DD or ≥4% session DD | `GET /api/health` shows `crisis_mode` present (False on healthy day) |
| Calibration GATE (r46) | ≥30 closed trades in any conf bucket | First days: gate is no-op (insufficient sample) |
| Per-ticker overrides (r46) | First weekly `recompute_all_profiles` run | `SELECT * FROM ticker_profiles` returns ≥1 row after weekly job |
| `MLPrediction.outcome` backfill (r45/r46/r47) | First trade closes after model trained | `SELECT count(*) FROM ml_predictions WHERE outcome IS NOT NULL` > 0 |
| ML calibrator (r45) | Train fold w/ ≥50 OOF samples | `GET /api/ml/status` shows `calibrator_loaded: true` |
| News severity gate (r46) | News article on open position | `autotrade_event{event=news_exit}` counter increments (was always 0 pre-r46) |
| AI news-blind bug fixed (r46) | AI judge call with recent news on ticker | `prompt_summary` in AIDecisionLog includes `recent_news` array (was always `[]`) |
| Stop-LIMIT (r46) | First trade with `STOP_LIMIT_OFFSET_PCT=0.005` | Alpaca dashboard shows SL leg as STOP_LIMIT, not STOP |
| **r47** Calibration multiplier (was dead, fixed) | ≥20 closed trades in a conf bucket | `services.risk_manager.calibration_multiplier(75)` returns ≠ 1.0 once a bucket has n≥20 |
| **r47** Strategy multiplier (was dead, fixed) | ≥20 closed trades on a strategy | Inspect `auto_trader.strategy_scorecard` output for any strat with `n>=20` |
| **r47** ML technical features (were silently None) | First inference call | Inspect `MLPrediction` rows: `features_json` should now contain non-null `tech_rsi` etc. |
| **r47** confidence_boost direction-match | Persisted best-strategy row in BUY direction | Manually call `services.best_strategy.confidence_boost(ticker, None, "BUY")` — returns 1.06 when row exists |
| **r47** EquitySnapshot UNIQUE on ts | Multi-instance Cloud Run | `SELECT ts, count(*) FROM equity_snapshots GROUP BY ts HAVING count(*) > 1` returns 0 rows |
| **r47** Slippage histogram + alert | First non-trivial fill slip | `metrics_observe('autotrade_slippage_bps')` histogram has buckets populated; alerts panel shows `slippage_outlier` if any fill >50bps |
| **r47** Scheduler EVENT_JOB_ERROR listener | Any scheduled job exception | `scheduler_job_failed` alert appears in `/api/admin/alerts` for the failing job |
| **r47** Position divergence alert | Operator manually flatten in Alpaca dashboard | Next reconcile fires `position_divergence` alert |
| **r47** SPX 200d trend gate | SPY closes below 200dSMA | `r47_overlays.spx_trend_size_factor("BUY")` returns 0.5; long sizing reduced |
| **r47** HYG/LQD credit-spread circuit breaker | HYG/LQD 60d z < -2 | New BUY entries skipped with `autotrade_skip{reason=r47_credit_cb}` |
| **r47** VIX 5σ spike strategy | Daily VIX +5σ change AND VIX≥25 | `r47_overlays.vix_spike_signal()` returns dict instead of None |
| **r47** Halt detection | WS quote stale >30s in RTH | New entries skipped with `autotrade_skip{reason=halt_suspect}` |
| **r47** News AI rate-limit (now per-hour) | 5+ articles in same hour on same ticker | Only first 3 fire AI judge; rest skipped (visible in AIDecisionLog) |
| **r47** Pyramid feature (now actually working) | T1 hit + ADX≥30 + cfg.pyramid_enabled=true | `autotrade_event{event=pyramid_t1}` increments |
| **r47** Inside-bar breakout (now correct logic) | Inside bar + parent-range break | Strategy fires only on parent-range break, not on every up-day |
| **r47** Idempotency on options | Multi-instance scan on same ticker | UNIQUE constraint blocks duplicate; `autotrade_skip{reason=idempotency_conflict}` increments |

If any of these don't activate within 1 RTH session of expected
conditions, that's a "stop the line" event — kill the bot and investigate.

---

## Cutover sequence (do these in this order)

1. **`enabled=false` deploy** with all live env vars + the JSON above.
   Verify `/api/health` → `auth_configured=true, alpaca_live=true,
   killed=false, scheduler_started=true`.

2. **24h `dry_run=true` smoke test**: scanner runs, signals are emitted,
   but no orders submit. Watch logs for any `*_gate_error` metrics.
   Check `/api/trading/auto/trades` shows pending-status DRY-RUN entries
   (not real fills).

3. **First live entry — manual approval mode**: `dry_run=false`,
   `enabled=false`. When you see a signal you'd like to take, manually
   POST `/api/trading/order` for that ticker. Confirm:
   - Bracket order lands at Alpaca (verify in Alpaca dashboard)
   - SL leg present, TP leg parked far away
   - DB row created with correct levels
   - On exit (trail or stop), `realized_pl` populates correctly

4. **After 3 manual trades exit cleanly**: flip `enabled=true`.
   The bot runs autonomously from here.

5. **Watch the first week**:
   - Hit `/api/trading/auto/pdt` daily. If count hits 3, the gate fires
     and you'll see `autotrade_skip{reason=pdt_limit}` — by design.
   - Check `/api/ai-judge/decisions?call_site=entry_veto` daily.
     If Claude is skipping trades you'd have taken, tighten the
     system prompt in `ai_judge.py` and redeploy.
   - Check `/metrics` for any `*_gate_error` counters > 0 — these
     mean a gate threw an exception (silent-fail class). Fix
     before continuing.

6. **End of week 1**: if expectancy is positive and no broken alerts:
   - Raise `max_concurrent_positions` to 7
   - Raise `max_per_sector` to 3

7. **End of week 2**: if still clean:
   - Raise `max_risk_per_trade_pct` to 0.015
   - Flip `AI_NEWS_EXIT_MODE=active` (review the shadow decisions
     accumulated over the live week first)

8. **End of week 4 / 100 closed live trades**: re-evaluate options.
   - `trade_options=true` AND `trade_calls=true` IF the calibration
     plot shows positive expectancy at confidence ≥ 80
   - Otherwise stay stocks-only

---

## Settings to NEVER change without checklist

| Setting | Why locked |
|---|---|
| `ALPACA_LIVE` | Two-key gate's whole point — change requires `I_UNDERSTAND_LIVE_RISK=yes` |
| `DATABASE_URL` | r44: live mode REFUSES to boot on default sqlite — three-instance Cloud Run = three bots disagreeing |
| `pdt_enforce` (when on margin <$25k) | One missed gate = 90-day PDT lock + huge headache |
| `flatten_by_eod` | Overnight gap risk is the single largest single-trade loss vector |
| `STOP_LIMIT_OFFSET_PCT=0.005` | r46: caps flash-crash / halt-resume gap-through fills |
| `bracket_tif=day` | r46: caps weekend-gap exposure on Friday positions |
| `confidence_threshold` ≥ 75 | Below 75 the rule engine has produced losing edge in paper |
| `max_pct_of_equity` ≤ 0.50 | The 50% reserve isn't decoration — it's the recovery cushion |
| `RISK_MULT_CEILING` | Hard-coded 2.0× in services/config.py for a reason — the multiplier stack will compound past 5× without it |
| `auto_promote_adopted=false` | First month: every external position must be manually reviewed before bot management |
| `pyramid_enabled=false` | r44 scale-in is gated; review trend-detection accuracy on 50+ live trades first |
| `--memory 2Gi` (api Cloud Run) | r51 fix: 1Gi triggered OOM-kill loop every 2-5 min during RTH (live_quotes WS subs + scanner DataFrames + APScheduler peaked at ~1.1 GiB). Cron jobs registered but never fired because instance died before next 5-min boundary. Set in `deploy.sh:122`. |

---

## Daily ops (live)

**Pre-market (08:30 ET)**:
- `/api/health` returns 200, `degraded=false`, `last_manage_at` < 60s
- `/api/health` shows `crisis_mode=false`, `session_dd_pct` < 2.0, `account_dd_mult >= 0.7` (r46)
- `/api/trading/auto/pdt` count < 3
- `/api/trading/auto/status` shows expected open positions; `freeze_reason=null` (r42)
- Check overnight alerts: `/api/alerts?unacked=true` — including any
  `drawdown_3pct/5pct/8pct/10pct` alerts (r46 DD-tier alerts)
- Check `/api/macro/blackout` — note any windows that will fire today
- Check `/api/trading/equity-curve?lookback_days=7` — 7-day chart should
  not show step-changes that don't match expected fills (r46)

**During RTH**: 
- Phone push notifications handle target hits / trade closes
- Spot-check `last_manage_at` if the manager service hasn't pinged in 5+ min

**Equity-curve bootstrap** (after fresh deploy or OOM/outage):
```bash
# Manually fire one snapshot so the chart and account_drawdown_multiplier
# have data to read instead of waiting for the cron's next 5-min boundary
curl -X POST -H "X-API-Key: $APP_API_KEY" \
  https://stockrecs-zcm5tboivq-uc.a.run.app/api/admin/record-equity-snapshot
# Returns {ok, latest_ts, latest_equity, total_rows}
```

**Post-close (16:30 ET)**:
- Review the day's closed trades for any with `closed_news_ai`,
  `closed_slippage`, `closed_kill`, `closed_eod`, `closed_time_stop`,
  or empty `realized_pl`
- r46: check `autotrade_skip{reason=...}` distribution — heavy
  `calibration_gate` / `correlation_cap` / `account_drawdown` / 
  `book_var_99` / `leverage_cap` / `idempotency_conflict` indicates
  one of those gates is too tight or a real risk-event tripped
- r46: check `/api/trading/equity-curve?lookback_days=1` — confirm 5-min
  snapshots accumulated through the session
- Skim `/api/ai-judge/decisions` for any honored skips —
  did Claude reject a trade that would have won? Tighten prompts if so
- Check `/metrics` for `force_close_failed` count > 0 — every one
  of those means a position was naked-long until you intervened

**Weekly (Sunday)**:
- Run `/api/backtest/portfolio/run` with last week's data,
  compare expectancy/Sharpe to live realized
- Take a manual Cloud SQL backup before any config changes
- Audit the AI judge log: pull `/api/ai-judge/summary` and
  validate the honored-vs-shadow ratio matches expectations

---

## Emergency procedures

### Bot is running away (multiple unwanted entries)
```bash
# 1. Hit kill via the API (preserves audit trail)
curl -X POST -H "X-API-Key: $APP_API_KEY" \
  https://stockrecs-zcm5tboivq-uc.a.run.app/api/trading/kill \
  -d '{"reason":"runaway entries","flatten":true,"cancel_orders":true}'

# 2. If API is wedged, kill via gcloud
gcloud run services update stockrecs --region=us-central1 \
  --update-env-vars="ALPACA_LIVE=0"

# 3. Manually flatten via Alpaca dashboard if both above fail
```

### Position naked-long after `force_close_failed` alert
```bash
# 1. Inspect the trade
curl -H "X-API-Key: $APP_API_KEY" \
  https://stockrecs-zcm5tboivq-uc.a.run.app/api/trading/auto/trades?limit=10

# 2. Manually submit a stop via Alpaca dashboard or:
curl -X POST -H "X-API-Key: $APP_API_KEY" \
  https://stockrecs-zcm5tboivq-uc.a.run.app/api/trading/close/<TICKER>

# 3. Mark the AutoTrade row done via SQL or admin endpoint
```

### Unwind a config mistake
```bash
# Before any risky config change, save current state:
curl -H "X-API-Key: $APP_API_KEY" \
  https://stockrecs-zcm5tboivq-uc.a.run.app/api/trading/auto/status > config-backup-$(date +%Y%m%d).json

# To restore: POST the relevant fields from the backup back to /auto/config
```

---

## Final note — when in doubt, kill

The kill switch is persistent. It survives restarts. It's two-step to
re-arm. It's the cheapest safety mechanism we have. **Use it any time
something feels off** — investigate after, not during. A 24-hour kill
costs nothing; a runaway day costs the account.
