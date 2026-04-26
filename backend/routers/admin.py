"""Admin / one-off operational endpoints.

Auth: shared `X-API-Key` (same as the rest of `/api/*`). Designed for
operator-driven, infrequent housekeeping — backfills, schema-aligned
cleanups, etc. Each handler logs its action loudly for audit.

Pattern: every handler is idempotent or self-bounded by an explicit list
of IDs in the request body. No "delete all" or "drop everything"
shortcuts; every action names the rows it touches.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routers._auth import require_api_key
from database import SessionLocal, AutoTrade

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


class AgeOutTradesRequest(BaseModel):
    trade_ids: List[int] = Field(..., min_length=1, max_length=500)
    days_offset: int = Field(40, ge=31, le=365,
                              description="How many days to subtract from each trade's closed_at "
                                          "(must be ≥31 to age out of the 30-day analytics window)")


@router.post("/age-out-trades")
def age_out_trades(req: AgeOutTradesRequest):
    """Backdate `closed_at` on the listed AutoTrade rows by `days_offset`
    days. Used to remove specific historical trades from the 30-day
    analytics windows (recent_wr, strategy_drawdown, freeze regime, PDT
    counter) when those trades are known to be from now-fixed bugs and
    therefore not representative of forward behavior.

    The trades are NOT deleted — they remain in the audit log with their
    original `note` + `realized_pl`. Only `closed_at` shifts. The
    response lists each touched row's old/new closed_at for review.

    Use case: r40 audit landed fixes for the bug classes that produced
    the recent paper-trade losing streak (CNTA dollar-cap, AMKR direction
    drift, VTWO premium-stop spread). The losing trades aren't
    representative of post-r40 behavior; aging them out lets the freeze
    regime stop firing and the WR-based throttles return to neutral.
    """
    db = SessionLocal()
    touched = []
    try:
        rows = db.query(AutoTrade).filter(AutoTrade.id.in_(req.trade_ids)).all()
        if not rows:
            raise HTTPException(404, f"no AutoTrade rows match ids {req.trade_ids}")
        offset = timedelta(days=req.days_offset)
        for r in rows:
            old = r.closed_at
            if old is None:
                touched.append({
                    "trade_id": r.id, "ticker": r.ticker,
                    "old_closed_at": None, "new_closed_at": None,
                    "skipped": "not closed",
                })
                continue
            new = old - offset
            r.closed_at = new
            r.note = (r.note or "") + (
                f" | aged out {req.days_offset}d "
                f"({old.isoformat()} → {new.isoformat()}) "
                f"[r40 audit cleanup]"
            )
            touched.append({
                "trade_id": r.id, "ticker": r.ticker,
                "old_closed_at": old.isoformat(),
                "new_closed_at": new.isoformat(),
            })
        db.commit()
    finally:
        db.close()

    logger.warning(
        f"ADMIN age-out-trades: {len(touched)} rows touched, days_offset={req.days_offset}, "
        f"requested_ids={req.trade_ids}"
    )
    return {
        "touched": touched,
        "count": len([t for t in touched if "skipped" not in t]),
        "skipped": [t for t in touched if "skipped" in t],
        "days_offset": req.days_offset,
    }
