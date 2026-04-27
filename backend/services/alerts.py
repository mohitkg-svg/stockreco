"""Operator alerting service.

Emits alerts to:
  1. DB (Alert table) — surfaced via /api/alerts and the UI bell icon
  2. Python logger at CRITICAL/ERROR/WARNING level — grep-able in container logs
  3. Optional webhook (ALERT_WEBHOOK_URL env) — posts JSON for Slack/Discord/email relays

Critical code paths call `alert(severity, category, message, ticker=..., trade_id=...)`.
Never let alert delivery block the caller — all side effects wrapped in try/except
and the webhook is a fire-and-forget background task.
"""
from __future__ import annotations
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import httpx

from database import SessionLocal, Alert

logger = logging.getLogger(__name__)

_LEVEL_MAP = {
    "critical": logging.CRITICAL,
    "error":    logging.ERROR,
    "warning":  logging.WARNING,
    "info":     logging.INFO,
}

# Simple thread-safe de-dup: don't emit the same (category, message) alert
# more than once per 5-minute window — prevents log spam from a failing
# subsystem hammering the alerts table.
_DEDUP_WINDOW_SEC = 300
_dedup_lock = threading.Lock()
_dedup: Dict[str, float] = {}


_webhook_client: Optional[httpx.Client] = None


def _get_webhook_client() -> Optional[httpx.Client]:
    """r48 BACKLOG #perf-P3-23: reuse a single httpx.Client to avoid
    per-call TLS handshakes (~150ms each)."""
    global _webhook_client
    if _webhook_client is None:
        try:
            _webhook_client = httpx.Client(timeout=3.0)
        except Exception:
            return None
    return _webhook_client


def _post_webhook(payload: Dict[str, Any]) -> None:
    url = os.getenv("ALERT_WEBHOOK_URL")
    if not url:
        return
    client = _get_webhook_client()
    if client is None:
        return
    try:
        client.post(url, json=payload)
    except Exception as e:
        logger.debug(f"alert webhook post failed: {e}")


def alert(
    severity: str,
    category: str,
    message: str,
    ticker: Optional[str] = None,
    trade_id: Optional[int] = None,
) -> None:
    """Emit an operator alert. Safe to call from any thread — never raises."""
    severity = (severity or "warning").lower()
    if severity not in _LEVEL_MAP:
        severity = "warning"

    # De-dup
    import time as _t
    dedup_key = f"{category}|{message[:100]}|{ticker or ''}"
    now_t = _t.time()
    with _dedup_lock:
        last = _dedup.get(dedup_key, 0)
        if now_t - last < _DEDUP_WINDOW_SEC:
            return
        _dedup[dedup_key] = now_t
        # r48 BACKLOG #perf-P1.11: prune stale entries to bound the dict.
        if len(_dedup) > 5000:
            cutoff = now_t - (_DEDUP_WINDOW_SEC * 2)
            for k in list(_dedup.keys()):
                if _dedup[k] < cutoff:
                    _dedup.pop(k, None)

    prefix = f"ALERT[{severity.upper()}][{category}]"
    logger.log(_LEVEL_MAP[severity], f"{prefix} {message} (ticker={ticker} trade_id={trade_id})")

    # Persist
    try:
        db = SessionLocal()
        try:
            db.add(Alert(
                severity=severity, category=category, message=message,
                ticker=ticker, trade_id=trade_id,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"alert DB persist failed ({category}): {e}")

    # Webhook (fire & forget in a thread so we never block caller)
    try:
        payload = {
            "severity": severity,
            "category": category,
            "message": message,
            "ticker": ticker,
            "trade_id": trade_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        threading.Thread(target=_post_webhook, args=(payload,), daemon=True).start()
    except Exception:
        pass


# ---- Query helpers for router + health endpoint -------------------------------

def count_unacked(since_hours: Optional[int] = None, severity: Optional[str] = None) -> int:
    """Unacked alert count. Optionally filtered by severity + age window."""
    db = SessionLocal()
    try:
        q = db.query(Alert).filter(Alert.acked_at == None)  # noqa: E711
        if severity:
            q = q.filter(Alert.severity == severity.lower())
        if since_hours is not None:
            q = q.filter(Alert.created_at >= datetime.utcnow() - timedelta(hours=since_hours))
        return q.count()
    finally:
        db.close()


def list_recent(limit: int = 50, only_unacked: bool = False) -> List[Dict[str, Any]]:
    """Recent alerts, newest first. For the UI bell dropdown."""
    db = SessionLocal()
    try:
        q = db.query(Alert)
        if only_unacked:
            q = q.filter(Alert.acked_at == None)  # noqa: E711
        rows = q.order_by(Alert.created_at.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "severity": r.severity,
                "category": r.category,
                "message": r.message,
                "ticker": r.ticker,
                "trade_id": r.trade_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "acked_at": r.acked_at.isoformat() if r.acked_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


def ack_all() -> int:
    """Mark every unacked alert as acknowledged. Returns the count."""
    db = SessionLocal()
    try:
        rows = db.query(Alert).filter(Alert.acked_at == None).all()  # noqa: E711
        now = datetime.utcnow()
        for r in rows:
            r.acked_at = now
        db.commit()
        return len(rows)
    finally:
        db.close()


def ack_one(alert_id: int) -> bool:
    db = SessionLocal()
    try:
        r = db.query(Alert).filter(Alert.id == alert_id).first()
        if not r:
            return False
        if not r.acked_at:
            r.acked_at = datetime.utcnow()
            db.commit()
        return True
    finally:
        db.close()
