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

# === DATA FEED — keep SIP if you have Algo Trader Plus ===
ALPACA_DATA_FEED=sip
ALPACA_OPTIONS_FEED=indicative
ALPACA_NEWS_STREAM=1
ALPACA_OPTIONS_STREAM=0

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
  "ml_scoring_enabled": false                // KEEP shadow until ≥200 live closed trades
}
```

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
| `pdt_enforce` (when on margin <$25k) | One missed gate = 90-day PDT lock + huge headache |
| `flatten_by_eod` | Overnight gap risk is the single largest single-trade loss vector |
| `confidence_threshold` ≥ 75 | Below 75 the rule engine has produced losing edge in paper |
| `max_pct_of_equity` ≤ 0.50 | The 50% reserve isn't decoration — it's the recovery cushion |
| `RISK_MULT_CEILING` | Hard-coded 2.0× in services/config.py for a reason — the multiplier stack will compound past 5× without it |

---

## Daily ops (live)

**Pre-market (08:30 ET)**:
- `/api/health` returns 200, `degraded=false`, `last_manage_at` < 60s
- `/api/trading/auto/pdt` count < 3
- `/api/trading/auto/status` shows expected open positions
- Check overnight alerts: `/api/alerts?unacked=true`
- Check `/api/macro/blackout` — note any windows that will fire today

**During RTH**: 
- Phone push notifications handle target hits / trade closes
- Spot-check `last_manage_at` if the manager service hasn't pinged in 5+ min

**Post-close (16:30 ET)**:
- Review the day's closed trades for any with `closed_news_ai`,
  `closed_slippage`, `closed_kill`, or empty `realized_pl`
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
