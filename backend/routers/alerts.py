"""Alert inbox — operator-facing critical-event feed."""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException, Query

from routers._auth import require_api_key
from services import alerts as alerts_svc

router = APIRouter(
    prefix="/api/alerts",
    tags=["alerts"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("")
def recent(limit: int = Query(50, ge=1, le=500), only_unacked: bool = False):
    return alerts_svc.list_recent(limit=limit, only_unacked=only_unacked)


@router.get("/count")
def count(since_hours: int = Query(24, ge=1, le=720), severity: str = Query(None)):
    return {"unacked": alerts_svc.count_unacked(since_hours=since_hours, severity=severity)}


@router.post("/ack-all")
def ack_all():
    return {"acked": alerts_svc.ack_all()}


@router.post("/{alert_id}/ack")
def ack(alert_id: int):
    if not alerts_svc.ack_one(alert_id):
        raise HTTPException(status_code=404, detail="alert not found")
    return {"acked": alert_id}
