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
from typing import Optional
from fastapi import Header, HTTPException


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
