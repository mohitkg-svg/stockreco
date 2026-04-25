"""
Shared API-key authentication for all mutating (and account-revealing) routes.

Design:
- One shared secret in env (`APP_API_KEY`) — rotate by restart.
- Clients pass it via `X-API-Key` header (never as a query param — shows up in
  proxy logs and browser history).
- When `APP_API_KEY` is UNSET, auth is a no-op so local dev / tests aren't
  blocked. Production MUST set it; `main.py` bootstrap refuses to start
  `ALPACA_LIVE=1` without it.
- `hmac.compare_digest` for constant-time comparison — prevents timing attacks
  against the header secret.

Applied to: trading.py (every route), watchlist.py (mutations), admin routes.
GET-only read endpoints that expose account balances also use this — any
leak of balance/orders is equivalent to a data exfiltration.
"""
from __future__ import annotations
import hmac
import os
import threading
import time
from typing import Optional, Dict, Tuple
from fastapi import Header, HTTPException, Request


def _expected_key() -> Optional[str]:
    key = os.getenv("APP_API_KEY")
    return key.strip() if key and key.strip() else None


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """
    FastAPI dependency — raises 401 when the incoming request is missing or
    presents a wrong X-API-Key header.

    Returns None on success; attach via `dependencies=[Depends(require_api_key)]`
    on the router (preferred: single attachment on the APIRouter so individual
    route handlers don't need to know about auth).
    """
    expected = _expected_key()
    if expected is None:
        # Dev mode — no key configured, open access. Logged at boot.
        return None
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    if not hmac.compare_digest(x_api_key.strip(), expected):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return None


def auth_configured() -> bool:
    """True if APP_API_KEY is set — used by /api/health to surface config."""
    return _expected_key() is not None


# ---------- API-key rate limiter (r38) -------------------------------------
#
# Per-key token bucket — defends against a leaked X-API-Key being abused by
# a runaway script, and against a misconfigured client polling /api/* in
# tight loops. Limits are intentionally generous (we have one user) so
# normal interactive use never hits them.
#
# Configurable via env:
#   APP_RATE_LIMIT_PER_MIN    — bucket refill rate per minute (default 300)
#   APP_RATE_LIMIT_BURST      — max burst capacity (default 60)
#
# Set APP_RATE_LIMIT_PER_MIN=0 to disable entirely (e.g., load testing).

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: Dict[str, Tuple[float, float]] = {}   # key → (tokens, last_refill_ts)


def _rate_config() -> Tuple[float, float]:
    """Return (refill_per_sec, burst). Cached env reads are not worth it
    given they happen once per request and os.getenv is dict-fast."""
    per_min = float(os.getenv("APP_RATE_LIMIT_PER_MIN", "300") or 0)
    burst = float(os.getenv("APP_RATE_LIMIT_BURST", "60") or 60)
    return (per_min / 60.0 if per_min > 0 else 0.0, burst)


def _bucket_key(x_api_key: Optional[str], request: Optional[Request]) -> str:
    """Prefer the X-API-Key (already authenticated upstream) so two clients
    sharing a key share the bucket. Fall back to client IP for unauth'd
    /api/health probes."""
    if x_api_key:
        # Hash to avoid keeping plaintext keys in process memory beyond what
        # _auth already requires.
        return f"k:{hash(x_api_key) & 0xffffffff:x}"
    if request is not None and request.client:
        return f"i:{request.client.host}"
    return "anon"


def rate_limit(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency. Token-bucket per (key|ip). 429 on exhaustion.
    Disabled when APP_RATE_LIMIT_PER_MIN=0."""
    refill_per_sec, burst = _rate_config()
    if refill_per_sec <= 0 or burst <= 0:
        return None
    key = _bucket_key(x_api_key, request)
    now = time.monotonic()
    with _RATE_LOCK:
        tokens, last = _RATE_BUCKETS.get(key, (burst, now))
        # Refill since last hit.
        tokens = min(burst, tokens + (now - last) * refill_per_sec)
        if tokens < 1.0:
            # Compute retry-after for the response header.
            wait_s = (1.0 - tokens) / refill_per_sec
            _RATE_BUCKETS[key] = (tokens, now)
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": f"{int(wait_s) + 1}"},
            )
        _RATE_BUCKETS[key] = (tokens - 1.0, now)
    return None


def verify_ws_token(token: Optional[str]) -> bool:
    """Browser WebSockets can't set custom headers, so auth uses a ?token=
    query param instead. Same constant-time comparison as the header path.
    Returns True when auth passes (or is disabled in dev mode)."""
    expected = _expected_key()
    if expected is None:
        return True
    if not token:
        return False
    return hmac.compare_digest(token.strip(), expected)
