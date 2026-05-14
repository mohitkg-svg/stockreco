"""
Deterministic news gate — r92 (Item E in the pre-news-fix audit).

A rule-based news-impact layer that does NOT depend on the AI judge.
Sits alongside the AI veto / news-exit pipeline (`ai_judge.py`,
`news.py:_dispatch_ai_news_exit`) so news can still influence trades
when Claude abstains, the API key is bad, or the rate limit trips.

Two public entry points:

  • `news_entry_gate(ticker, bias, db)` — called at entry time, returns
    {action: "proceed"|"downsize"|"block", qty_mult: float, reason: str,
     alert: bool, top_news: dict|None}. Caller respects `action` and
    multiplies its computed qty by `qty_mult`.

  • `news_position_action(trade_id, ticker, bias, db, since_ts)` — called
    in the manage tick, returns {action: "hold"|"trim"|"close",
    trim_fraction: float, reason: str, alert: bool, top_news: dict|None}.

`bias` is the directional thesis: "bull" (long stock or call) or "bear"
(put). Contra-sentiment news is what triggers downsize/block/trim/close.

Thresholds (confirmed by operator 2026-05-14):
  Entry block    : severity ≥ 70 AND contra in last 2h
  Entry downsize : severity ≥ 50 AND contra in last 6h (×0.6)
  Position trim  : severity ≥ 60 AND contra in last 30m (trim 50%)
  Position close : severity ≥ 80 AND contra in last 30m
  Alert-only     : severity ≥ 40 AND no other trigger

"Contra" means:
  bull bias + sentiment_score ≤ -0.50  → contra (negative news hurts longs)
  bear bias + sentiment_score ≥ +0.50  → contra (positive news hurts shorts)

Per-trade dedup: each (trade_id, news_event_id) action only fires once
within `_DEDUP_TTL_SEC`. Prevents repeated trims on the same article
when the manage loop sees it on consecutive ticks.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# --- Thresholds (mutable for tests; do not hot-edit in prod) ---------------

ENTRY_BLOCK_SEVERITY = 70.0
ENTRY_BLOCK_HOURS = 2.0
ENTRY_DOWNSIZE_SEVERITY = 50.0
ENTRY_DOWNSIZE_HOURS = 6.0
ENTRY_DOWNSIZE_MULT = 0.6
POSITION_TRIM_SEVERITY = 60.0
POSITION_TRIM_MINUTES = 30.0
POSITION_TRIM_FRACTION = 0.5
POSITION_CLOSE_SEVERITY = 80.0
POSITION_CLOSE_MINUTES = 30.0
ALERT_ONLY_SEVERITY = 40.0
CONTRA_SCORE_THRESHOLD = 0.50  # |score| ≥ this AND opposite sign of bias

# --- Per-trade dedup -------------------------------------------------------

_DEDUP: Dict[Tuple[int, int, str], float] = {}   # (trade_id, news_id, action) → expiry_ts
_DEDUP_TTL_SEC = 6 * 3600   # 6h
_DEDUP_MAX = 4096


def _dedup_seen(trade_id: int, news_id: int, action: str) -> bool:
    now = time.time()
    key = (trade_id, news_id, action)
    exp = _DEDUP.get(key)
    if exp and exp > now:
        return True
    return False


def _dedup_mark(trade_id: int, news_id: int, action: str) -> None:
    now = time.time()
    if len(_DEDUP) >= _DEDUP_MAX:
        try:
            _DEDUP.pop(next(iter(_DEDUP)))
        except StopIteration:
            pass
    _DEDUP[(trade_id, news_id, action)] = now + _DEDUP_TTL_SEC


# --- Core ------------------------------------------------------------------

def _is_contra(bias: str, score: Optional[float]) -> bool:
    """Sentiment opposes the directional thesis."""
    if score is None:
        return False
    if bias == "bull":
        return score <= -CONTRA_SCORE_THRESHOLD
    if bias == "bear":
        return score >= CONTRA_SCORE_THRESHOLD
    return False


def _fetch_recent_news(ticker: str, db: Session, since: datetime, limit: int = 25):
    """Multi-symbol-aware fetch — same join as _build_ai_context."""
    from database import NewsEvent
    from sqlalchemy import or_ as _or
    return (
        db.query(NewsEvent)
        .filter(_or(
            NewsEvent.ticker == ticker,
            NewsEvent.symbols.like(f"%{ticker}%"),
        ))
        .filter(NewsEvent.published_at >= since)
        .order_by(NewsEvent.published_at.desc())
        .limit(limit)
        .all()
    )


def _serialize_news(n) -> Dict[str, Any]:
    return {
        "id": n.id,
        "headline": (n.headline or "")[:160],
        "sentiment_label": n.sentiment_label,
        "sentiment_score": float(n.sentiment_score) if n.sentiment_score is not None else None,
        "severity": float(n.severity) if n.severity is not None else None,
        "published_at": n.published_at.isoformat() if n.published_at else None,
    }


# --- Public: entry gate ----------------------------------------------------

def news_entry_gate(ticker: str, bias: str, db: Session) -> Dict[str, Any]:
    """Pre-entry deterministic news check. Never raises (returns proceed
    on any error). Caller multiplies its qty by `qty_mult` and respects
    `action`. If `alert=True`, caller should emit one alert before
    proceeding/blocking — alert decisions made here, alert dispatch
    happens at the call site so this module stays I/O-free.
    """
    default: Dict[str, Any] = {
        "action": "proceed", "qty_mult": 1.0,
        "reason": "", "alert": False, "top_news": None,
    }
    if bias not in ("bull", "bear"):
        return default
    try:
        # Widest window first — we read once, filter by recency in-Python.
        since = datetime.utcnow() - timedelta(hours=max(
            ENTRY_BLOCK_HOURS, ENTRY_DOWNSIZE_HOURS
        ))
        rows = _fetch_recent_news(ticker, db, since)
        if not rows:
            return default

        now = datetime.utcnow()
        block_cutoff = now - timedelta(hours=ENTRY_BLOCK_HOURS)
        downsize_cutoff = now - timedelta(hours=ENTRY_DOWNSIZE_HOURS)
        alert_cutoff = downsize_cutoff   # alerts share the wider window

        worst_contra = None
        worst_alert = None
        for n in rows:
            sev = float(n.severity or 0.0)
            contra = _is_contra(bias, float(n.sentiment_score) if n.sentiment_score is not None else None)
            pub = n.published_at or now
            # Track the worst-severity contra news for downsize/block decisions
            if contra and pub >= downsize_cutoff:
                if worst_contra is None or sev > float(worst_contra.severity or 0.0):
                    worst_contra = n
            # Track high-severity non-contra (or low-sev contra) for alert-only
            if pub >= alert_cutoff and sev >= ALERT_ONLY_SEVERITY:
                if worst_alert is None or sev > float(worst_alert.severity or 0.0):
                    worst_alert = n

        if worst_contra is not None:
            sev = float(worst_contra.severity or 0.0)
            pub = worst_contra.published_at or now
            if sev >= ENTRY_BLOCK_SEVERITY and pub >= block_cutoff:
                return {
                    "action": "block", "qty_mult": 0.0,
                    "reason": f"news_block sev={sev:.0f} sent={worst_contra.sentiment_score:+.2f}",
                    "alert": True, "top_news": _serialize_news(worst_contra),
                }
            if sev >= ENTRY_DOWNSIZE_SEVERITY:
                return {
                    "action": "downsize", "qty_mult": ENTRY_DOWNSIZE_MULT,
                    "reason": f"news_downsize sev={sev:.0f} sent={worst_contra.sentiment_score:+.2f}",
                    "alert": True, "top_news": _serialize_news(worst_contra),
                }

        if worst_alert is not None:
            return {
                "action": "proceed", "qty_mult": 1.0,
                "reason": f"news_alert sev={float(worst_alert.severity or 0):.0f}",
                "alert": True, "top_news": _serialize_news(worst_alert),
            }

        return default
    except Exception as e:
        logger.warning(f"news_entry_gate failed for {ticker} (bias={bias}): {e}")
        return default


# --- Public: position action -----------------------------------------------

def news_position_action(
    trade_id: int, ticker: str, bias: str, db: Session,
) -> Dict[str, Any]:
    """Per-tick deterministic news check for an open position. Returns one
    of hold / trim / close (or alert-only) based on FRESH (last 30m) news.

    Per-(trade_id, news_id, action) dedup prevents the manage loop from
    re-triggering on the same article on consecutive ticks. Once a trim
    fires for news#K, the same news#K can't trigger another trim for 6h.
    """
    default: Dict[str, Any] = {
        "action": "hold", "trim_fraction": 0.0,
        "reason": "", "alert": False, "top_news": None,
    }
    if bias not in ("bull", "bear"):
        return default
    try:
        widest_min = max(POSITION_TRIM_MINUTES, POSITION_CLOSE_MINUTES)
        since = datetime.utcnow() - timedelta(minutes=widest_min)
        rows = _fetch_recent_news(ticker, db, since)
        if not rows:
            return default

        # Highest-severity contra news in the window
        worst_contra = None
        worst_alert = None
        for n in rows:
            sev = float(n.severity or 0.0)
            contra = _is_contra(bias, float(n.sentiment_score) if n.sentiment_score is not None else None)
            if contra:
                if worst_contra is None or sev > float(worst_contra.severity or 0.0):
                    worst_contra = n
            if sev >= ALERT_ONLY_SEVERITY:
                if worst_alert is None or sev > float(worst_alert.severity or 0.0):
                    worst_alert = n

        if worst_contra is not None:
            sev = float(worst_contra.severity or 0.0)
            # CLOSE — highest severity
            if sev >= POSITION_CLOSE_SEVERITY:
                if _dedup_seen(trade_id, worst_contra.id, "close"):
                    return default
                _dedup_mark(trade_id, worst_contra.id, "close")
                return {
                    "action": "close", "trim_fraction": 1.0,
                    "reason": f"news_close sev={sev:.0f} sent={worst_contra.sentiment_score:+.2f}",
                    "alert": True, "top_news": _serialize_news(worst_contra),
                }
            # TRIM — moderate
            if sev >= POSITION_TRIM_SEVERITY:
                if _dedup_seen(trade_id, worst_contra.id, "trim"):
                    return default
                _dedup_mark(trade_id, worst_contra.id, "trim")
                return {
                    "action": "trim", "trim_fraction": POSITION_TRIM_FRACTION,
                    "reason": f"news_trim sev={sev:.0f} sent={worst_contra.sentiment_score:+.2f}",
                    "alert": True, "top_news": _serialize_news(worst_contra),
                }

        if worst_alert is not None:
            # Alert-only — dedup so we don't re-alert every tick
            if not _dedup_seen(trade_id, worst_alert.id, "alert"):
                _dedup_mark(trade_id, worst_alert.id, "alert")
                return {
                    "action": "hold", "trim_fraction": 0.0,
                    "reason": f"news_alert sev={float(worst_alert.severity or 0):.0f}",
                    "alert": True, "top_news": _serialize_news(worst_alert),
                }

        return default
    except Exception as e:
        logger.warning(f"news_position_action failed for trade={trade_id} {ticker}: {e}")
        return default
