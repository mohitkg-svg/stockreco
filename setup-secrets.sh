#!/usr/bin/env bash
set -euo pipefail

if [ -f backend/.env ]; then
  set -a
  source backend/.env
  set +a
fi

PROJECT="$(gcloud config get-value project 2>/dev/null)"
if [ -z "$PROJECT" ]; then
  echo "❌ No active gcloud project."
  exit 1
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)' 2>/dev/null)"
SVC_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

create_and_add_secret() {
  local secret_name="$1"
  local secret_value="$2"
  
  if ! gcloud secrets describe "$secret_name" >/dev/null 2>&1; then
    echo "Creating secret $secret_name..."
    gcloud secrets create "$secret_name" --replication-policy="automatic"
  fi
  
  echo "Adding new version to $secret_name..."
  echo -n "$secret_value" | gcloud secrets versions add "$secret_name" --data-file=-

  echo "Granting access to Cloud Run service account..."
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --member="serviceAccount:$SVC_ACCOUNT" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}

echo "Setting up secrets in project: $PROJECT"

if [ -n "${APCA_API_KEY_ID:-}" ]; then create_and_add_secret "apca-api-key-id" "$APCA_API_KEY_ID"; fi
if [ -n "${APCA_API_SECRET_KEY:-}" ]; then create_and_add_secret "apca-api-secret-key" "$APCA_API_SECRET_KEY"; fi
if [ -n "${DATABASE_URL:-}" ]; then create_and_add_secret "database-url" "$DATABASE_URL"; fi
if [ -n "${APP_API_KEY:-}" ]; then create_and_add_secret "app-api-key" "$APP_API_KEY"; fi
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then create_and_add_secret "anthropic-api-key" "$ANTHROPIC_API_KEY"; fi
if [ -n "${FMP_API_KEY:-}" ]; then create_and_add_secret "fmp-api-key" "$FMP_API_KEY"; fi
if [ -n "${POLYGON_API_KEY:-}" ]; then create_and_add_secret "polygon-api-key" "$POLYGON_API_KEY"; fi

echo "✅ Secrets initialized."