# syntax=docker/dockerfile:1.6
# ---------- Builder ----------
FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./requirements.txt
RUN pip install --prefix=/install -r requirements.txt

# Apply the alpaca-py / websockets 14 compatibility patch at build time so
# runtime boot is clean. alpaca-py 0.21.1 uses the legacy `extra_headers`
# kwarg; websockets >=14 renamed it to `additional_headers`. The patch
# shipped in the repo under `.venv/.../alpaca/common/websocket.py` is
# vendored into the site-packages install.
COPY alpaca_websocket_patch.py ./alpaca_websocket_patch.py
RUN python alpaca_websocket_patch.py /install/lib/python3.12/site-packages/alpaca/common/websocket.py || true

# ---------- Runtime ----------
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /install /usr/local
COPY backend ./backend
COPY frontend ./frontend

# Alpaca certs land in certifi — override SSL bundle path at runtime.
ENV SSL_CERT_FILE="" REQUESTS_CA_BUNDLE=""

EXPOSE 8080

# Cloud Run injects $PORT (default 8080). Bind to 0.0.0.0 so the managed
# proxy can forward traffic. Single worker — the app uses APScheduler +
# in-memory caches that must not be duplicated across workers.
WORKDIR /app/backend
CMD exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
