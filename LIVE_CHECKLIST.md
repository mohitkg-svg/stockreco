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
  "bracket_tif": "gtc",                      // ⚠️ "day" caps weekend-gap exposure
                                             //    but positions are uncovered after RTH;
                                             //    the manage tick re-arms at 9:30 next day.
                                             //    Recommended: "day" for first month.
  "pyramid_enabled": false                   // r44: scale-in at T1 in strong trends.
                                             //      Leave OFF until you've watched
                                             //      ≥50 closed trades.
}
```

---

## r43-r46 post-deploy verification (do FIRST after the env vars deploy, before flipping `enabled=true`)

Several r43-r46 systems are silent on day 1 — they activate only after
data accumulates. Verify each is working:

| System | Activation requirement | Verification |
|---|---|---|
| EquitySnapshot (r46) | 5 min snapshots × 25 min RTH | `GET /api/trading/equity-curve?lookback_days=1` returns ≥5 snapshots |
| `account_drawdown_multiplier` (r46) | ≥5 EquitySnapshot rows | `GET /api/health` shows `account_dd_mult` non-null after ~30 min RTH |
| `crisis_mode` (r46) | Always live; True only when ≥5% multi-day DD or ≥4% session DD | `GET /api/health` shows `crisis_mode` present (False on healthy day) |
| Calibration GATE (r46) | ≥30 closed trades in any conf bucket | First days: gate is no-op (insufficient sample) |
| Per-ticker overrides (r46) | First weekly `recompute_all_profiles` run | `SELECT * FROM ticker_profiles` returns ≥1 row after weekly job |
| `MLPrediction.outcome` backfill (r45/r46) | First trade closes after model trained | `SELECT count(*) FROM ml_predictions WHERE outcome IS NOT NULL` > 0 |
| ML calibrator (r45) | Train fold w/ ≥50 OOF samples | `GET /api/ml/status` shows `calibrator_loaded: true` |
| News severity gate (r46) | News article on open position | `autotrade_event{event=news_exit}` counter increments (was always 0 pre-r46) |
| AI news-blind bug fixed (r46) | AI judge call with recent news on ticker | `prompt_summary` in AIDecisionLog includes `recent_news` array (was always `[]`) |
| Stop-LIMIT (r46) | First trade with `STOP_LIMIT_OFFSET_PCT=0.005` | Alpaca dashboard shows SL leg as STOP_LIMIT, not STOP |

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
