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
# CORS: Cloud Run URL is unknown until deploy — allow any origin by default
# (the X-API-Key gate is the real access control). Tighten after first deploy
# by setting CORS_ALLOW_ORIGINS to the exact Cloud Run URL.
ENV_VARS="${ENV_VARS},CORS_ALLOW_ORIGINS=*"
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

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 3 \
  --timeout 300s \
  --add-cloudsql-instances "$CSQL_INSTANCE" \
  --update-env-vars "$ENV_VARS"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo ""
echo "✅ Deployed: $URL"
echo "   Health:  $URL/api/health"
echo "   Open in browser: $URL"
