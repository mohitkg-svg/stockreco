#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Load secrets (Alpaca keys, etc.) if present. backend/.env is sourced LAST
# so it wins over the repo-root .env — the root copy may contain stale keys
# from a previous paper-account reset, but backend/.env is the source of truth
# (main.py's _load_dotenv() uses setdefault and can't override env vars set
# by this shell, so order here matters).
set -a
if [ -f .env ]; then
  # shellcheck disable=SC1091
  source .env
fi
if [ -f backend/.env ]; then
  # shellcheck disable=SC1091
  source backend/.env
fi
set +a

cd backend

# Use virtual environment if present, otherwise fall back to system python3
PYTHON="${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python3}"
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  if [ -x "../.venv/bin/python3" ]; then
    PYTHON="../.venv/bin/python3"
  else
    PYTHON="python3"
  fi
fi

# Point OpenSSL at certifi's bundle if available (needed on some systems for Alpaca WSS)
CERT_BUNDLE=$("$PYTHON" -c "import certifi; print(certifi.where())" 2>/dev/null)
if [ -n "$CERT_BUNDLE" ]; then
  export SSL_CERT_FILE="$CERT_BUNDLE"
  export REQUESTS_CA_BUNDLE="$CERT_BUNDLE"
fi

exec "$PYTHON" -m uvicorn main:app --host 0.0.0.0 --port 8000 "$@"
