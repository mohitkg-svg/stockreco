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
ENV_VARS="APCA_API_KEY_ID=${APCA_API_KEY_ID},APCA_API_SECRET_KEY=${APCA_API_SECRET_KEY},DATABASE_URL=${DATABASE_URL}"
if [ -n "${APP_API_KEY:-}" ]; then
  ENV_VARS="${ENV_VARS},APP_API_KEY=${APP_API_KEY}"
fi
# CORS: Cloud Run URL is unknown until deploy — allow any origin by default
# (the X-API-Key gate is the real access control). Tighten after first deploy
# by setting CORS_ALLOW_ORIGINS to the exact Cloud Run URL.
ENV_VARS="${ENV_VARS},CORS_ALLOW_ORIGINS=*"

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
  --set-env-vars "$ENV_VARS"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo ""
echo "✅ Deployed: $URL"
echo "   Health:  $URL/api/health"
echo "   Open in browser: $URL"
