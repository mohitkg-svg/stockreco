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

# macOS Python.framework ships without root certs — point OpenSSL at certifi's bundle
# so Alpaca's wss://stream.data.alpaca.markets handshake succeeds.
CERT_BUNDLE=$(/usr/local/bin/python3.8 -c "import certifi; print(certifi.where())" 2>/dev/null)
if [ -n "$CERT_BUNDLE" ]; then
  export SSL_CERT_FILE="$CERT_BUNDLE"
  export REQUESTS_CA_BUNDLE="$CERT_BUNDLE"
fi

exec /usr/local/bin/python3.8 -m uvicorn main:app --host 127.0.0.1 --port 8000 "$@"
