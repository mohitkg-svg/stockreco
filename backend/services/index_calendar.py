"""r46 Tier P: index-rebalance / inclusion calendar overlays.

Russell reconstitution (late June, MSCI quarterly, FTSE quarterly): names
moving into a major index drift +6-9% from announcement to effective
(Madhavan 2003, Cai-Houge 2008). Names moving OUT often drop equally.

This module provides a coarse calendar of known windows and a hook that
signal_generator can read to bias confidence. The actual constituent
changes are announced by S&P/Russell on specific dates that we don't
auto-scrape (would require a press-release watcher); for now we surface
the WINDOWS so the operator can manually curate `cfg.index_inclusion_tickers`
and the bot recognizes them as elevated-edge names during the window.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.utcnow()


def is_russell_reconstitution_window() -> bool:
    """True iff today is in the Russell reconstitution window
    (typically last Friday of June + 1 trading week prior)."""
    now = _now_et()
    if now.month != 6:
        return False
    # Last Friday of June.
    last_day = 30
    last_dt = now.replace(day=last_day)
    while last_dt.weekday() != 4:   # Friday
        last_dt -= timedelta(days=1)
    # Window: 7 days before through 1 day after.
    delta_days = (now.date() - last_dt.date()).days
    return -7 <= delta_days <= 1


def is_msci_quarterly_window() -> bool:
    """MSCI quarterly rebalance: end of Feb, May, Aug, Nov.
    Window: last 3 trading days of those months."""
    now = _now_et()
    if now.month not in (2, 5, 8, 11):
        return False
    # Approximate "last 3 trading days" with last 5 calendar days.
    return now.day >= 26


def is_in_index_event_window() -> bool:
    """Aggregate: any active index-rebalance window."""
    return is_russell_reconstitution_window() or is_msci_quarterly_window()


def index_event_multiplier(ticker: Optional[str] = None) -> float:
    """Sizing nudge during active index-rebalance windows.

    r48 BACKLOG #edge-F10: prior code applied 1.05× to ALL signals during
    the window — but the academic effect (Madhavan 2003, Cai-Houge 2008)
    is for SPECIFIC names being added/dropped, not the whole universe.
    Now: only fire the boost for tickers present in
    `cfg.index_inclusion_tickers` (comma-separated CSV); also reduced
    magnitude to 1.025 (post-publication effect compressed ~50%).
    """
    if not is_in_index_event_window():
        return 1.0
    if ticker is None:
        return 1.0
    try:
        from database import SessionLocal as _SL_ie, AutoTraderConfig as _C_ie
        db = _SL_ie()
        try:
            cfg = db.query(_C_ie).filter(_C_ie.id == 1).first()
            inclusion = (getattr(cfg, "index_inclusion_tickers", None) or "")
        finally:
            db.close()
    except Exception:
        return 1.0
    inc_list = {t.strip().upper() for t in inclusion.split(",") if t.strip()}
    if ticker.upper() in inc_list:
        return 1.025
    return 1.0
