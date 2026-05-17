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

  TEST_DB_URL="sqlite:///$(mktemp)"
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "   (Starting ephemeral Postgres container for tests)"
    # Ensure no leftover container is hanging around
    docker rm -f stockrecs-test-db >/dev/null 2>&1 || true
    if docker run --rm -d --name stockrecs-test-db -p 5432:5432 -e POSTGRES_PASSWORD=test postgres:16-alpine >/dev/null 2>&1; then
      echo "   (Waiting for Postgres to be ready...)"
      for i in {1..20}; do
        if docker exec stockrecs-test-db pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; then
          TEST_DB_URL="postgresql://postgres:test@localhost:5432/postgres"
          sleep 1
          break
        fi
        sleep 1
      done
    else
      echo "   (Failed to start Postgres container, falling back to SQLite)"
    fi
  fi

  if (cd backend && DATABASE_URL="$TEST_DB_URL" APP_API_KEY=test \
       python3 -m unittest -v tests.test_bug_scenarios tests.test_smoke); then
    echo "✅ tests passed; proceeding with deploy"
  else
    echo "❌ pre-deploy tests FAILED — aborting. Set SKIP_TESTS=1 to override."
    if command -v docker >/dev/null 2>&1; then docker stop stockrecs-test-db >/dev/null 2>&1 || true; fi
    exit 1
  fi
  if command -v docker >/dev/null 2>&1; then docker stop stockrecs-test-db >/dev/null 2>&1 || true; fi
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

# ---- Secrets Management (B27) -----------------------------------------------
# Secrets are mapped via GCP Secret Manager instead of being passed as plaintext
# env vars. Ensure these exist: gcloud secrets create apca-api-key-id ...
SECRETS="APCA_API_KEY_ID=apca-api-key-id:latest,APCA_API_SECRET_KEY=apca-api-secret-key:latest,DATABASE_URL=database-url:latest"
REMOVE_ENV_VARS="APCA_API_KEY_ID,APCA_API_SECRET_KEY,DATABASE_URL"

if [ -n "${APP_API_KEY:-}" ]; then
  SECRETS="${SECRETS},APP_API_KEY=app-api-key:latest"
  REMOVE_ENV_VARS="${REMOVE_ENV_VARS},APP_API_KEY"
fi
if [ -n "${FMP_API_KEY:-}" ]; then
  SECRETS="${SECRETS},FMP_API_KEY=fmp-api-key:latest"
  REMOVE_ENV_VARS="${REMOVE_ENV_VARS},FMP_API_KEY"
fi
if [ -n "${POLYGON_API_KEY:-}" ]; then
  SECRETS="${SECRETS},POLYGON_API_KEY=polygon-api-key:latest"
  REMOVE_ENV_VARS="${REMOVE_ENV_VARS},POLYGON_API_KEY"
fi
if [ -n "${STOCKTWITS_API_KEY:-}" ]; then
  SECRETS="${SECRETS},STOCKTWITS_API_KEY=stocktwits-api-key:latest"
  REMOVE_ENV_VARS="${REMOVE_ENV_VARS},STOCKTWITS_API_KEY"
fi

# Build env-var string for non-sensitive configuration.
# We use `--update-env-vars` below so existing Cloud Run env vars are preserved.
ENV_VARS=""

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
ENV_VARS="${ENV_VARS}${ENV_VARS:+,}CORS_ALLOW_ORIGINS=${CORS_ALLOW_ORIGINS}"
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

DEPLOY_FLAGS=()
if [ -n "$ENV_VARS" ]; then
  DEPLOY_FLAGS+=(--update-env-vars "$ENV_VARS")
fi
if [ -n "$REMOVE_ENV_VARS" ]; then
  DEPLOY_FLAGS+=(--remove-env-vars "$REMOVE_ENV_VARS")
fi

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 4Gi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 1 \
  --no-cpu-throttling \
  --timeout 300s \
  --cpu-boost \
  --add-cloudsql-instances "$CSQL_INSTANCE" \
  "${DEPLOY_FLAGS[@]}" \
  --update-secrets "$SECRETS"

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
