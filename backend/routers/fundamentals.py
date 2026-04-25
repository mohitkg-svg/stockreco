"""Fundamentals API — ticker snapshot + manual refresh."""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException

from routers._auth import require_api_key
from services import fundamentals as svc

router = APIRouter(
    prefix="/api/fundamentals",
    tags=["fundamentals"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("/{ticker}")
def get(ticker: str):
    """Persisted fundamentals for `ticker` + computed quality score.
    404 if no row has been fetched yet."""
    r = svc.get_fundamentals(ticker)
    if not r:
        raise HTTPException(status_code=404, detail=f"No fundamentals for {ticker} yet")
    return r


@router.post("/{ticker}/refresh")
def refresh(ticker: str, force: bool = False):
    """Force a re-fetch for one ticker. `force=True` bypasses hash short-circuit
    and rewrites the row even if data is unchanged (use sparingly)."""
    r = svc.refresh_ticker(ticker, force=force)
    if r is None:
        raise HTTPException(status_code=502, detail=f"Could not fetch fundamentals for {ticker}")
    return r


@router.post("/refresh-all")
def refresh_all():
    """Manually trigger the scheduled refresh across watchlist + candidate pool.
    Returns counts: total checked, how many had a hash change."""
    return svc.refresh_all()
