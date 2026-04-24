"""Economic-release calendar API.

Read endpoints expose the upcoming/recent macro events and current blackout
status. Manual refresh endpoint regenerates the calendar window on demand.
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, Query

from routers._auth import require_api_key
from services import macro_calendar as svc

router = APIRouter(
    prefix="/api/macro",
    tags=["macro"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("/calendar")
def calendar(
    within_hours: int = Query(72, ge=1, le=24 * 14),
    min_importance: str = Query("medium", pattern="^(low|medium|high)$"),
):
    """Upcoming releases in the next `within_hours`."""
    return svc.upcoming(within_hours=within_hours, min_importance=min_importance)


@router.get("/recent")
def recent(
    within_hours: int = Query(24, ge=1, le=24 * 14),
    min_importance: str = Query("medium", pattern="^(low|medium|high)$"),
):
    """Recently-released events with their actual values (if fetched)."""
    return svc.recent(within_hours=within_hours, min_importance=min_importance)


@router.get("/blackout")
def blackout(options_strict: bool = False):
    """Whether we're currently inside a pre/post-release blackout window."""
    in_blk, ev, why = svc.is_in_blackout(options_only_strict=options_strict)
    return {"in_blackout": in_blk, "event": ev, "reason": why}


@router.post("/refresh")
def refresh(days_ahead: int = Query(60, ge=1, le=180)):
    """Re-populate the calendar window. Idempotent."""
    return svc.populate_calendar(days_ahead=days_ahead)


@router.post("/fetch-actuals")
def fetch_actuals(lookback_hours: int = Query(24, ge=1, le=168)):
    """Try to backfill `actual` values via FRED for recently-released events.
    No-op if FRED_API_KEY is not set on the server."""
    return svc.fetch_actuals_for_recent_releases(lookback_hours=lookback_hours)
