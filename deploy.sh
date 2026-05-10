#!/usr/bin/env bash
# One-shot Cloud Run deploy.
#
# Prerequisites (one-time):
#   gcloud auth login
#   gcloud config set project YOUR_PROJECT_ID
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com
#
# Env vars this script expects (source your backend/.env or export manually):
#   APCA_API_KEY_ID
#   APCA_API_SECRET_KEY
#   DATABASE_URL               (Neon Postgres)
#   APP_API_KEY                (optional — if unset, endpoints are open)
#
# Usage:
#   ./deploy.sh [region]   # region defaults to us-central1

set -euo pipefail

REGION="${1:-us-central1}"
SERVICE="stockrecs"

# ---- Pre-deploy frontend build ----------------------------------------------
# r58 fix: app.compiled.js is what Cloud Run serves. Without rebuilding before
# every deploy, frontend changes in app.js never reach production. Skip with
# SKIP_FRONTEND_BUILD=1 if iterating backend-only.
if [ "${SKIP_FRONTEND_BUILD:-0}" != "1" ]; then
  echo "── Rebuilding frontend (esbuild) ──"
  if command -v node >/dev/null 2>&1 && [ -f frontend/build.js ]; then
    if (cd frontend && node build.js 2>&1 | tail -5); then
      echo "✅ frontend rebuilt"
    else
      echo "❌ frontend build failed — aborting. Set SKIP_FRONTEND_BUILD=1 to override."
      exit 1
    fi
  else
    echo "ℹ️  node or frontend/build.js missing; skipping frontend rebuild"
  fi
fi

# ---- Pre-deploy lint (ruff) -------------------------------------------------
# Conservative ruleset (syntax errors, undefined names, redefinitions only).
# Skip with SKIP_LINT=1; doesn't block if ruff isn't installed locally.
if [ "${SKIP_LINT:-0}" != "1" ]; then
  echo "── Running pre-deploy ruff lint ──"
  if command -v ruff >/dev/null 2>&1; then
    if (cd backend && ruff check . 2>&1 | tail -20); then
      echo "✅ ruff passed"
    else
      echo "❌ ruff found issues — aborting. Set SKIP_LINT=1 to override."
      echo "   To run locally: cd backend && ruff check ."
      exit 1
    fi
  else
    echo "ℹ️  ruff not installed locally; skipping (pip install -r backend/requirements-dev.txt to enable)"
  fi
fi

# ---- Pre-deploy regression tests --------------------------------------------
# Cheap (<3s) regression suite that catches the bug families surfaced in
# production losses. Skip with SKIP_TESTS=1 if you really need to.
if [ "${SKIP_TESTS:-0}" != "1" ]; then
  echo "── Running pre-deploy regression tests ──"
  if (cd backend && DATABASE_URL="sqlite:///$(mktemp)" APP_API_KEY=test \
       python3 -m unittest tests.test_bug_scenarios tests.test_smoke 2>&1 | tail -8); then
    echo "✅ tests passed; proceeding with deploy"
  else
    echo "❌ pre-deploy tests FAILED — aborting. Set SKIP_TESTS=1 to override."
    exit 1
  fi
fi

# Pick up env vars from backend/.env if present (without overwriting shell vars).
if [ -f backend/.env ]; then
  set -a
  # shellcheck disable=SC1091
  source backend/.env
  set +a
fi

: "${APCA_API_KEY_ID:?Set APCA_API_KEY_ID (in backend/.env or shell)}"
: "${APCA_API_SECRET_KEY:?Set APCA_API_SECRET_KEY}"
: "${DATABASE_URL:?Set DATABASE_URL}"

PROJECT="$(gcloud config get-value project 2>/dev/null)"
if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
  echo "No active gcloud project. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

echo "→ Deploying $SERVICE to Cloud Run in $REGION (project: $PROJECT)"

# Build env-var string. Comma-separate, escape commas in values (none expected
# for our keys) — anything hairy should move to Secret Manager.
# We use `--update-env-vars` below (not --set-env-vars) so existing Cloud
# Run env vars set via `gcloud run services update` (e.g. ALPACA_DATA_FEED)
# are preserved across deploys. --set-env-vars previously wiped them.
ENV_VARS="APCA_API_KEY_ID=${APCA_API_KEY_ID},APCA_API_SECRET_KEY=${APCA_API_SECRET_KEY},DATABASE_URL=${DATABASE_URL}"
if [ -n "${APP_API_KEY:-}" ]; then
  ENV_VARS="${ENV_VARS},APP_API_KEY=${APP_API_KEY}"
fi
# Anthropic key (chat widget + AI judge layer). Only forwarded when set
# in the local shell — so an unset local var doesn't blank the value
# already on the live service.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  ENV_VARS="${ENV_VARS},ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
fi
# Financial Modeling Prep key (fundamentals + earnings + analyst ratings +
# SEC filings poll). Same conditional pattern: unset local → preserve Cloud
# Run value across deploys; when fmp_client.is_enabled() returns False the
# yfinance fallback paths take over.
if [ -n "${FMP_API_KEY:-}" ]; then
  ENV_VARS="${ENV_VARS},FMP_API_KEY=${FMP_API_KEY}"
fi
# Polygon options tier.
if [ -n "${POLYGON_API_KEY:-}" ]; then
  ENV_VARS="${ENV_VARS},POLYGON_API_KEY=${POLYGON_API_KEY}"
fi
# AI judge call-site modes. Only forwarded when explicitly set, so the
# default off-everywhere stays put unless you flip the env var.
for _m in AI_ENTRY_VETO_MODE AI_NEWS_EXIT_MODE AI_CONFIDENCE_MULT_MODE; do
  if [ -n "${!_m:-}" ]; then
    ENV_VARS="${ENV_VARS},${_m}=${!_m}"
  fi
done
# CORS: r82 — was unconditionally setting CORS_ALLOW_ORIGINS=* on every
# deploy, which silently overwrote any tightened value previously set on
# Cloud Run. LIVE_CHECKLIST requires this NOT be * in prod. We now require
# CORS_ALLOW_ORIGINS to be set in backend/.env (or the shell). For LIVE
# mode we additionally refuse '*' explicitly.
if [ -z "${CORS_ALLOW_ORIGINS:-}" ]; then
  echo "❌ CORS_ALLOW_ORIGINS must be set (e.g. https://stockrecs-xxx.a.run.app)" >&2
  echo "   Add it to backend/.env or export it in your shell." >&2
  exit 1
fi
if [ "${ALPACA_LIVE:-0}" = "1" ] && [ "${CORS_ALLOW_ORIGINS}" = "*" ]; then
  echo "❌ Refusing to deploy LIVE with CORS_ALLOW_ORIGINS='*'." >&2
  exit 1
fi
ENV_VARS="${ENV_VARS},CORS_ALLOW_ORIGINS=${CORS_ALLOW_ORIGINS}"
# Algo Trader Plus feature flags — sticky so deploys don't wipe them:
#   ALPACA_DATA_FEED=sip         SIP consolidated tape (bars + live stream)
#   ALPACA_OPTIONS_FEED=indicative  Options snapshots (AT+ tier supports this)
#   ALPACA_NEWS_STREAM=1         Enable NewsDataStream when alpaca-py adds it
#   ALPACA_OPTIONS_STREAM=0      Leave OFF unless OPRA real-time is in plan
ENV_VARS="${ENV_VARS},ALPACA_DATA_FEED=${ALPACA_DATA_FEED:-sip}"
ENV_VARS="${ENV_VARS},ALPACA_OPTIONS_FEED=${ALPACA_OPTIONS_FEED:-indicative}"
ENV_VARS="${ENV_VARS},ALPACA_NEWS_STREAM=${ALPACA_NEWS_STREAM:-1}"
ENV_VARS="${ENV_VARS},ALPACA_OPTIONS_STREAM=${ALPACA_OPTIONS_STREAM:-0}"

# Cloud SQL Unix-socket mount — required for DATABASE_URL to resolve to
# the managed Postgres instance. Hardcoded here so deploys never drop the
# mount; if you move to a different instance, edit CSQL_INSTANCE below.
CSQL_INSTANCE="${CSQL_INSTANCE:-$PROJECT:us-central1:stockrecs-db}"

# Verify database tier to prevent connection pool exhaustion during cutover
# Only checks and automatically patches if going LIVE.
if gcloud sql instances describe stockrecs-db --format="value(settings.tier)" > /dev/null 2>&1; then
  DB_TIER=$(gcloud sql instances describe stockrecs-db --format="value(settings.tier)")
  if [ "$DB_TIER" = "db-f1-micro" ] && [ "${ALPACA_LIVE:-0}" = "1" ]; then
    echo "⚠️  WARNING: Upgrading Cloud SQL instance from db-f1-micro to db-g1-small."
    echo "    This is required to survive connection spikes during deployment cutovers with real money."
    gcloud sql instances patch stockrecs-db --tier=db-g1-small --quiet
  fi
fi

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 2 \
  --timeout 300s \
  --cpu-boost \
  --add-cloudsql-instances "$CSQL_INSTANCE" \
  --update-env-vars "$ENV_VARS"

# Cloud Run liveness probe — auto-restart instance if /api/healthz returns
# non-200 for 3 consecutive checks. r82 (B49): switched from /api/health to
# /api/healthz (a trivial 200-returner with NO DB / broker calls). The
# previous /api/health did 2 DB queries + broker call + alert emit, which
# could hang on a wedged DB and trigger restart loops. /api/healthz is
# unauthenticated by
# design (read-only health). Probe runs against an internal port and
# does not consume our public quota.
# These flags require gcloud beta + a recent Cloud Run version. The
# subcommand may fail on older clients — non-fatal, just warn.
gcloud beta run services update "$SERVICE" --region "$REGION" \
  --liveness-probe="httpGet.path=/api/healthz,initialDelaySeconds=20,periodSeconds=30,timeoutSeconds=5,failureThreshold=3" \
  --startup-probe="httpGet.path=/api/healthz,initialDelaySeconds=10,periodSeconds=5,timeoutSeconds=5,failureThreshold=12" \
  2>/dev/null || echo "(liveness-probe flags unavailable; install gcloud beta or update — non-fatal)"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo ""
echo "✅ Deployed: $URL"
echo "   Health:  $URL/api/health"
echo "   Open in browser: $URL"
