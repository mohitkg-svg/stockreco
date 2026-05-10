# Comprehensive Pre-Live Audit — 2026-05-10

Five parallel audit agents ran across the entire codebase. Total: ~250 findings.
This file holds the raw reports; see `AUDIT_SUMMARY.md` for the deduped/prioritized list.

Audit scopes:
1. Money path — auto_trader, execution, position_manager, regime_router
2. Risk + Alpaca + DB — risk_manager, risk_math, alpaca_client, live_quotes, database, models, migration
3. API surface + secrets — main.py, routers/, deploy.sh, Dockerfile, .env, .gitignore
4. Signals + strategies — signal_generator, strategies, scanner, indicators, setup_quality, ml_*
5. Frontend + tests + scheduler + alerts/metrics/schemas — app.js, tests, schedulers, alerts, log_utils

(Full reports preserved in chat history; the synthesis below is in AUDIT_SUMMARY.md)
