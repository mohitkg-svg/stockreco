"""Calendar / seasonal signals — pre-FOMC drift, day-of-week, quarter-end,
holiday weeks, etc. r44 fix Wave 3.

All accessors return a multiplier or boolean; consumers stack into the
sizing layer. Values are conservative (±5-15%) so a single seasonality
mistake doesn't blow up sizing.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.utcnow()


def is_pre_fomc_day() -> bool:
    """True iff today is the trading day BEFORE an FOMC announcement.
    Reads from the macro_calendar service when available.
    """
    try:
        from services.macro_calendar import upcoming as _macro_upcoming
        events = _macro_upcoming(48, "high")
        for ev in (events or []):
            if "FOMC" in str(ev.get("event", "")).upper():
                # If FOMC is between 1 hour and 26 hours away, today is pre-FOMC.
                ts = ev.get("release_time_utc")
                if not ts:
                    continue
                from datetime import datetime as _dt
                evt_dt = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
                hrs = (evt_dt - _dt.utcnow().replace(tzinfo=evt_dt.tzinfo)).total_seconds() / 3600
                if 1 < hrs < 26:
                    return True
    except Exception:
        pass
    return False


def is_quarter_end_window() -> bool:
    """True iff today is in the LAST 3 trading days of the calendar quarter.
    Approximated by calendar date — last 3 days of Mar, Jun, Sep, Dec.
    """
    now = _now_et()
    if now.month not in (3, 6, 9, 12):
        return False
    # Approximate "last 3 trading days" with last 5 calendar days.
    if now.day >= 26:
        return True
    return False


def is_first_4_days_of_month() -> bool:
    """True iff today is in the first 4 trading days of the month (calendar
    days 1-6 to give a small buffer for weekends).
    """
    return _now_et().day <= 6


def is_opex_day() -> bool:
    """True iff today is the third Friday of Mar/Jun/Sep/Dec (triple witching)."""
    now = _now_et()
    if now.month not in (3, 6, 9, 12):
        return False
    if now.weekday() != 4:   # Friday
        return False
    # Third Friday: day in [15, 21].
    return 15 <= now.day <= 21


def is_holiday_drift_week() -> bool:
    """True iff today is in Thanksgiving week (Mon before US Thanksgiving →
    Friday) OR Christmas-to-NY (Dec 23 → Dec 31). Empirically positive
    drift weeks; reduce trim aggressiveness.
    """
    now = _now_et()
    if now.month == 12 and now.day >= 23:
        return True
    if now.month == 11:
        # Find the 4th Thursday of November (Thanksgiving).
        first = now.replace(day=1)
        # weekday(): Mon=0, Thu=3.
        first_thu = 1 + (3 - first.weekday()) % 7
        thanksgiving = first_thu + 21
        if thanksgiving - 3 <= now.day <= thanksgiving + 1:
            return True
    return False


def pre_fomc_drift_buy_qualifying_ticker(ticker: str) -> bool:
    """r46 Tier P: gate to authorize a Pre-FOMC drift LONG entry.
    Lucca-Moench (2015): SPX +49 bps mean on day before FOMC. Restricted
    to large-cap index ETFs to avoid naming-specific noise.
    """
    if not is_pre_fomc_day():
        return False
    target_set = {"SPY", "QQQ", "IWM", "DIA", "IVV", "VOO", "VTI"}
    return ticker.upper() in target_set


def calendar_multiplier() -> float:
    """Combined calendar-effects sizing multiplier. Conservative bounds.

    Returns a value in [0.95, 1.15] depending on:
      * Pre-FOMC drift day → ×1.10
      * Quarter-end window → ×1.05
      * First 4 days of month → ×1.05
      * OPEX day → ×0.92 (volatility cluster)
      * Holiday drift week → ×1.05
    Multiple bonuses compound but the result is capped at 1.15.
    """
    m = 1.0
    if is_pre_fomc_day():
        m *= 1.10
    if is_quarter_end_window():
        m *= 1.05
    if is_first_4_days_of_month():
        m *= 1.05
    if is_opex_day():
        m *= 0.92
    if is_holiday_drift_week():
        m *= 1.05
    return float(max(0.85, min(1.15, m)))
