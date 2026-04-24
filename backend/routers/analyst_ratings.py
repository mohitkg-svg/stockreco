"""Analyst ratings API — read-only consensus snapshot per ticker."""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query

from routers._auth import require_api_key
from services import analyst_ratings as svc

router = APIRouter(
    prefix="/api/analyst-ratings",
    tags=["analyst_ratings"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("/{ticker}")
def get(ticker: str):
    """Return the persisted consensus + consensus price targets for `ticker`.
    404 if no rating has been ingested yet."""
    r = svc.get_rating(ticker)
    if not r:
        raise HTTPException(status_code=404, detail=f"No analyst rating for {ticker}")
    return r


@router.post("/{ticker}/refresh")
def refresh(ticker: str):
    """Force a refresh for one ticker. Used by the UI when the operator wants
    fresh data between scheduled 4×/day refreshes."""
    row = svc.refresh_ticker(ticker)
    if row is None:
        raise HTTPException(status_code=502, detail=f"Could not fetch rating for {ticker}")
    return row


@router.post("/refresh-all")
def refresh_all():
    """Manually trigger the scheduled refresh across watchlist + candidate pool."""
    return svc.refresh_all()
