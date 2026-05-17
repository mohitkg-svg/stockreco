#!/usr/bin/env bash
# Deploy the position-manager service (RUN_MODE=manager).
#
# Architecture note: the api service (deploy.sh) handles HTTP, scanning,
# signal generation, and entries. THIS service does ONLY the 20s manage
# loop + hourly broker reconciliation. Splitting these means a crash or
# rate-limit in the api service can't leave open positions unmanaged.
#
# Both services share the same Cloud SQL database. Same image; only
# RUN_MODE differs.
#
# Usage:
#   ./deploy-manager.sh [region]   # region defaults to us-central1

set -euo pipefail

REGION="${1:-us-central1}"
SERVICE="stockrecs-manager"

# Pre-deploy regression tests — same suite as the api deploy.
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
    echo "✅ tests passed; proceeding with manager deploy"
  else
    echo "❌ pre-deploy tests FAILED — aborting. Set SKIP_TESTS=1 to override."
    if command -v docker >/dev/null 2>&1; then docker stop stockrecs-test-db >/dev/null 2>&1 || true; fi
    exit 1
  fi
  if command -v docker >/dev/null 2>&1; then docker stop stockrecs-test-db >/dev/null 2>&1 || true; fi
fi

# Pick up env vars from backend/.env if present.
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

echo "→ Deploying $SERVICE (RUN_MODE=manager) to Cloud Run in $REGION"

SECRETS="APCA_API_KEY_ID=apca-api-key-id:latest,APCA_API_SECRET_KEY=apca-api-secret-key:latest,DATABASE_URL=database-url:latest"
REMOVE_ENV_VARS="APCA_API_KEY_ID,APCA_API_SECRET_KEY,DATABASE_URL"
if [ -n "${APP_API_KEY:-}" ]; then
  SECRETS="${SECRETS},APP_API_KEY=app-api-key:latest"
  REMOVE_ENV_VARS="${REMOVE_ENV_VARS},APP_API_KEY"
fi

# Manager-mode env. RUN_MODE=manager flips the lifespan to register only the
# manage loop + reconciliation.
ENV_VARS="RUN_MODE=manager"
ENV_VARS="${ENV_VARS},CORS_ALLOW_ORIGINS=*"
ENV_VARS="${ENV_VARS},ALPACA_DATA_FEED=${ALPACA_DATA_FEED:-sip}"
ENV_VARS="${ENV_VARS},LOG_JSON=${LOG_JSON:-1}"

# ALPACA_LIVE pass-through so manager can match api when you flip to live.
if [ -n "${ALPACA_LIVE:-}" ]; then
  ENV_VARS="${ENV_VARS},ALPACA_LIVE=${ALPACA_LIVE}"
fi
if [ -n "${I_UNDERSTAND_LIVE_RISK:-}" ]; then
  ENV_VARS="${ENV_VARS},I_UNDERSTAND_LIVE_RISK=${I_UNDERSTAND_LIVE_RISK}"
fi

DEPLOY_FLAGS=()
if [ -n "$ENV_VARS" ]; then
  DEPLOY_FLAGS+=(--update-env-vars "$ENV_VARS")
fi
if [ -n "$REMOVE_ENV_VARS" ]; then
  DEPLOY_FLAGS+=(--remove-env-vars "$REMOVE_ENV_VARS")
fi

# Same Cloud SQL instance as the api service.
CSQL_INSTANCE="${CSQL_INSTANCE:-$PROJECT:us-central1:stockrecs-db}"

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --platform managed \
  --no-allow-unauthenticated \
  --ingress internal \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 1 \
  --max-instances 1 \
  --timeout 300s \
  --add-cloudsql-instances "$CSQL_INSTANCE" \
  "${DEPLOY_FLAGS[@]}" \
  --update-secrets "$SECRETS"

# Manager liveness probe — health endpoint flags `degraded=True` if the
# manage loop hasn't ticked in 120s, so the probe will trip and Cloud Run
# auto-restarts the container. This is the whole point of putting the
# manager in its own service.
gcloud beta run services update "$SERVICE" --region "$REGION" \
  --liveness-probe="httpGet.path=/api/healthz,initialDelaySeconds=30,periodSeconds=30,timeoutSeconds=5,failureThreshold=3" \
  --startup-probe="httpGet.path=/api/healthz,initialDelaySeconds=10,periodSeconds=5,timeoutSeconds=5,failureThreshold=12" \
  2>/dev/null || echo "(liveness-probe flags unavailable; non-fatal)"

# The manager service is internal-ingress only — it doesn't accept public
# traffic. Its sole job is the scheduler. Health endpoint is reachable
# only from the same project for the liveness probe.

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo ""
echo "✅ Manager deployed: $URL"
echo "   (internal-only; the scheduler runs continuously inside)"
