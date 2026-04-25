"""AI-judge audit + control endpoints.

The judge runs in shadow mode by default — these endpoints let the operator
review what Claude WOULD have done before flipping a call site to active.
"""
from __future__ import annotations
import json
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query

from routers._auth import require_api_key
from database import SessionLocal, AIDecisionLog

router = APIRouter(
    prefix="/api/ai-judge",
    tags=["ai-judge"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("/decisions")
def list_decisions(
    call_site: Optional[str] = Query(None, pattern="^(entry_veto|news_exit|confidence_multiplier)$"),
    limit: int = Query(50, ge=1, le=500),
    only_honored: bool = Query(False),
):
    """List recent AI judge decisions, newest first.

    Filter by `call_site` to focus on one channel; `only_honored=true`
    shows just the decisions that actually changed bot behavior.
    """
    db = SessionLocal()
    try:
        q = db.query(AIDecisionLog)
        if call_site:
            q = q.filter(AIDecisionLog.call_site == call_site)
        if only_honored:
            q = q.filter(AIDecisionLog.honored.is_(True))
        rows = q.order_by(AIDecisionLog.created_at.desc()).limit(limit).all()
        out = []
        for r in rows:
            try:
                prompt = json.loads(r.prompt_summary) if r.prompt_summary else None
            except Exception:
                prompt = r.prompt_summary
            try:
                response = json.loads(r.response) if r.response else None
            except Exception:
                response = r.response
            out.append({
                "id": r.id,
                "call_site": r.call_site,
                "mode": r.mode,
                "prompt": prompt,
                "response": response,
                "latency_ms": r.latency_ms,
                "honored": bool(r.honored),
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return {"decisions": out, "count": len(out)}
    finally:
        db.close()


@router.get("/summary")
def summary():
    """Aggregate stats per call_site + mode — basic shadow-review report."""
    db = SessionLocal()
    try:
        rows = db.query(AIDecisionLog).all()
        agg: dict = {}
        for r in rows:
            key = (r.call_site, r.mode)
            b = agg.setdefault(key, {"count": 0, "honored": 0, "lat_sum": 0, "lat_n": 0})
            b["count"] += 1
            if r.honored: b["honored"] += 1
            if r.latency_ms is not None:
                b["lat_sum"] += r.latency_ms
                b["lat_n"] += 1
        return {
            "summary": [
                {
                    "call_site": cs,
                    "mode": m,
                    "count": v["count"],
                    "honored_count": v["honored"],
                    "avg_latency_ms": int(v["lat_sum"] / v["lat_n"]) if v["lat_n"] else None,
                }
                for (cs, m), v in sorted(agg.items())
            ]
        }
    finally:
        db.close()


@router.get("/modes")
def current_modes():
    """Read-only view of which call sites are off / shadow / active."""
    from services import ai_judge
    return {
        "entry_veto": ai_judge.entry_veto_mode(),
        "news_exit": ai_judge.news_exit_mode(),
        "confidence_multiplier": ai_judge.confidence_mult_mode(),
        "anthropic_configured": ai_judge._get_client() is not None,
    }
