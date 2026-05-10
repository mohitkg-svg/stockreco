"""US economic-release calendar + blackout-window helper.

Populates `macro_events` with high-importance US releases over a rolling
60-day window. Generates events from deterministic recurrence rules
(NFP/CPI/PPI/PCE etc.) plus a hardcoded FOMC schedule.

Auto-trader uses `is_in_blackout()` to refuse new entries within ±N minutes
of high-importance releases — the goal is to avoid entering positions right
into a 1%-2% S&P gap that has nothing to do with our setup.

Realistic latency expectations:
  * Blackout windows are pre-scheduled, so they fire on time.
  * Fetching the *actual* number after release uses FRED — typically
    delayed 1-5 minutes. Set FRED_API_KEY env var to enable.
  * Sub-second release-time price action is unavailable on free feeds;
    that requires Bloomberg Terminal / Reuters / Polygon Indicators.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from database import SessionLocal, MacroEvent

logger = logging.getLogger(__name__)

# Blackout half-windows by importance.
_PRE_BLACKOUT_HIGH_MIN = 30
_POST_BLACKOUT_HIGH_MIN = 60
_PRE_BLACKOUT_MED_MIN = 15
_POST_BLACKOUT_MED_MIN = 30

# Hardcoded FOMC statement dates (UTC = 14:00 ET = 18:00 UTC during EDT,
# 19:00 UTC during EST). Update when Fed publishes the next year's schedule.
_FOMC_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
]
_FOMC_2027 = [
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16",
    "2027-07-28", "2027-09-22", "2027-11-03", "2027-12-15",
]
_FOMC_DATES = set(_FOMC_2026 + _FOMC_2027)

# r82: a parallel set of date objects for callers that compare against
# datetime.date values directly. The string-set above is kept because at
# least one consumer (macro_calendar.py itself, line 154) does
# `if d.isoformat() in _FOMC_DATES`. Both forms now coexist.
from datetime import date as _date_fomc
_FOMC_DATE_OBJS = {_date_fomc.fromisoformat(s) for s in _FOMC_DATES}

# FRED series IDs for fetching actuals after release. None = no FRED fetch.
_FRED_SERIES = {
    "CPI": "CPIAUCSL",        # CPI All Urban
    "PPI": "PPIACO",
    "NFP": "PAYEMS",          # Total nonfarm
    "PCE": "PCEPI",
    "GDP_ADV": "GDPC1",
    "RETAIL": "RSAFS",
    "ISM_MFG": None,          # ISM is paid; no FRED equivalent for headline PMI
    "MICH_PRELIM": "UMCSENT",
    "MICH_FINAL": "UMCSENT",
    "FOMC": None,             # Rate decision is qualitative
}


def _is_edt(d) -> bool:
    """US Eastern is EDT (UTC-4) from 2nd Sun of March until 1st Sun of November."""
    import calendar
    if d.month < 3 or d.month > 11:
        return False
    if 4 <= d.month <= 10:
        return True
    if d.month == 3:
        first_sun = next(day for day in range(1, 8) if calendar.weekday(d.year, 3, day) == 6)
        return d.day >= first_sun + 7
    # November
    first_sun = next(day for day in range(1, 8) if calendar.weekday(d.year, 11, day) == 6)
    return d.day < first_sun


def _et_to_utc(d, hh_et, mm_et) -> datetime:
    offset = 4 if _is_edt(d) else 5
    return datetime(d.year, d.month, d.day, hh_et + offset, mm_et, tzinfo=timezone.utc)


def _add_event(events: List[Dict[str, Any]], key: str, name: str,
               release_utc: datetime, importance: str) -> None:
    events.append({
        "event_key": key,
        "event_name": name,
        "country": "US",
        "importance": importance,
        "release_time_utc": release_utc.replace(tzinfo=None),
        "fred_series_id": _FRED_SERIES.get(key),
    })


def populate_calendar(days_ahead: int = 60) -> Dict[str, Any]:
    """Generate event rows for the next `days_ahead` days using recurrence
    rules + the FOMC list. Idempotent: re-running adds nothing if the same
    (event_key, release_time_utc) row already exists."""
    import calendar as _cal

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    events: List[Dict[str, Any]] = []

    d = now.date()
    while d <= end.date():
        wd = d.weekday()  # Mon=0 .. Sun=6
        # NFP: 1st Friday of month, 08:30 ET, high
        if wd == 4 and d.day <= 7:
            _add_event(events, "NFP", "Nonfarm Payrolls", _et_to_utc(d, 8, 30), "high")
        # CPI: 2nd Wednesday of month, 08:30 ET (BLS publishes exact dates;
        # this rule lands within ±1 day in practice), high
        if wd == 2 and 8 <= d.day <= 14:
            _add_event(events, "CPI", "Consumer Price Index", _et_to_utc(d, 8, 30), "high")
        # PPI: day after CPI, 08:30 ET
        if wd == 3 and 9 <= d.day <= 15:
            _add_event(events, "PPI", "Producer Price Index", _et_to_utc(d, 8, 30), "high")
        # PCE: last Friday of month, 08:30 ET, high
        if wd == 4 and (d + timedelta(days=7)).month != d.month:
            _add_event(events, "PCE", "Personal Consumption Expenditures",
                       _et_to_utc(d, 8, 30), "high")
        # ISM Mfg: first business day of month, 10:00 ET, medium
        if d.day <= 3 and wd < 5:
            month_start = d.replace(day=1)
            first_bday = month_start
            while first_bday.weekday() >= 5:
                first_bday += timedelta(days=1)
            if d == first_bday:
                _add_event(events, "ISM_MFG", "ISM Manufacturing PMI",
                           _et_to_utc(d, 10, 0), "medium")
        # U-Mich Sentiment: preliminary on 2nd Friday, 10:00 ET, medium
        if wd == 4 and 8 <= d.day <= 14:
            _add_event(events, "MICH_PRELIM", "U-Mich Consumer Sentiment (Prelim)",
                       _et_to_utc(d, 10, 0), "medium")
        # U-Mich Sentiment Final: 4th Friday, 10:00 ET, medium
        if wd == 4 and 22 <= d.day <= 28:
            _add_event(events, "MICH_FINAL", "U-Mich Consumer Sentiment (Final)",
                       _et_to_utc(d, 10, 0), "medium")
        # GDP advance estimate: 28th of Jan/Apr/Jul/Oct (nearest weekday), 08:30 ET
        if d.month in (1, 4, 7, 10):
            target = d.replace(day=28)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            if d == target:
                _add_event(events, "GDP_ADV", "GDP Advance Estimate",
                           _et_to_utc(d, 8, 30), "high")
        # Retail Sales: ~15th of month, 08:30 ET, medium
        if d.day == 15 or (d.day in (16, 17) and d.replace(day=15).weekday() >= 5):
            target = d.replace(day=15)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            if d == target:
                _add_event(events, "RETAIL", "Retail Sales",
                           _et_to_utc(d, 8, 30), "medium")
        # FOMC: hardcoded list, 14:00 ET, high
        if d.isoformat() in _FOMC_DATES:
            _add_event(events, "FOMC", "FOMC Rate Decision",
                       _et_to_utc(d, 14, 0), "high")
        d += timedelta(days=1)

    # Upsert
    db = SessionLocal()
    added = 0
    try:
        for ev in events:
            existing = db.query(MacroEvent).filter(
                MacroEvent.event_key == ev["event_key"],
                MacroEvent.release_time_utc == ev["release_time_utc"],
            ).first()
            if existing:
                continue
            db.add(MacroEvent(**ev))
            added += 1
        db.commit()
    finally:
        db.close()
    logger.info(f"macro_calendar: populated {added} new events ({len(events)} in window)")
    return {"added": added, "in_window": len(events)}


def upcoming(within_hours: int = 24, min_importance: str = "medium") -> List[Dict[str, Any]]:
    """Events releasing in the next `within_hours`. Sorted ascending."""
    levels = {"low": 0, "medium": 1, "high": 2}
    floor = levels.get(min_importance, 1)
    now = datetime.utcnow()
    horizon = now + timedelta(hours=within_hours)
    db = SessionLocal()
    try:
        rows = (
            db.query(MacroEvent)
            .filter(MacroEvent.release_time_utc >= now,
                    MacroEvent.release_time_utc <= horizon)
            .order_by(MacroEvent.release_time_utc.asc()).all()
        )
        return [_serialize(r) for r in rows if levels.get(r.importance, 0) >= floor]
    finally:
        db.close()


def recent(within_hours: int = 6, min_importance: str = "medium") -> List[Dict[str, Any]]:
    """Recently-released events in the last `within_hours`. Sorted descending."""
    levels = {"low": 0, "medium": 1, "high": 2}
    floor = levels.get(min_importance, 1)
    now = datetime.utcnow()
    floor_t = now - timedelta(hours=within_hours)
    db = SessionLocal()
    try:
        rows = (
            db.query(MacroEvent)
            .filter(MacroEvent.release_time_utc <= now,
                    MacroEvent.release_time_utc >= floor_t)
            .order_by(MacroEvent.release_time_utc.desc()).all()
        )
        return [_serialize(r) for r in rows if levels.get(r.importance, 0) >= floor]
    finally:
        db.close()


def is_in_blackout(now_utc: Optional[datetime] = None,
                   options_only_strict: bool = False) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """True if we're inside a pre-release or post-release window for any
    high/medium importance event. Returns (in_blackout, event_dict, reason).

    Pre-release: 30m before high-importance, 15m before medium.
    Post-release: 60m after high-importance, 30m after medium.

    `options_only_strict` widens windows by 50% — options react harder to
    macro shocks than stocks (gamma, IV crush) so we hold off longer.
    """
    now = now_utc or datetime.utcnow()
    db = SessionLocal()
    try:
        # Look for any event whose blackout window covers `now`.
        floor = now - timedelta(hours=2)   # widest plausible post-window
        ceil = now + timedelta(hours=1)    # widest plausible pre-window
        rows = (
            db.query(MacroEvent)
            .filter(MacroEvent.release_time_utc >= floor,
                    MacroEvent.release_time_utc <= ceil)
            .all()
        )
        for r in rows:
            if r.importance == "high":
                pre_min = _PRE_BLACKOUT_HIGH_MIN
                post_min = _POST_BLACKOUT_HIGH_MIN
            elif r.importance == "medium":
                pre_min = _PRE_BLACKOUT_MED_MIN
                post_min = _POST_BLACKOUT_MED_MIN
            else:
                continue
            if options_only_strict:
                pre_min = int(pre_min * 1.5)
                post_min = int(post_min * 1.5)
            window_start = r.release_time_utc - timedelta(minutes=pre_min)
            window_end = r.release_time_utc + timedelta(minutes=post_min)
            if window_start <= now <= window_end:
                phase = "pre-release" if now < r.release_time_utc else "post-release"
                mins_to = int((r.release_time_utc - now).total_seconds() / 60)
                if phase == "pre-release":
                    reason = f"{phase} blackout: {r.event_key} ({r.importance}) in {mins_to}m"
                else:
                    reason = f"{phase} cooldown: {r.event_key} released {-mins_to}m ago"
                return True, _serialize(r), reason
        return False, None, ""
    finally:
        db.close()


def fetch_actuals_for_recent_releases(lookback_hours: int = 24) -> Dict[str, Any]:
    """For events that have released in the last N hours and have no `actual`
    populated yet, try fetching the value from FRED. Requires FRED_API_KEY
    env var; no-op (and logs a debug line) if not set."""
    api_key = os.getenv("FRED_API_KEY", "").strip()
    now = datetime.utcnow()
    floor = now - timedelta(hours=lookback_hours)
    db = SessionLocal()
    fetched = 0
    try:
        rows = (
            db.query(MacroEvent)
            .filter(MacroEvent.release_time_utc >= floor,
                    MacroEvent.release_time_utc <= now,
                    MacroEvent.actual.is_(None))
            .all()
        )
        if not rows:
            return {"checked": 0, "fetched": 0}
        if not api_key:
            logger.debug("macro_calendar: FRED_API_KEY not set; skipping actuals fetch")
            return {"checked": len(rows), "fetched": 0,
                    "note": "set FRED_API_KEY to enable post-release fetches"}
        import httpx
        for r in rows:
            if not r.fred_series_id:
                continue
            try:
                url = "https://api.stlouisfed.org/fred/series/observations"
                params = {
                    "series_id": r.fred_series_id,
                    "api_key": api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                }
                resp = httpx.get(url, params=params, timeout=15.0)
                if resp.status_code != 200:
                    continue
                obs = resp.json().get("observations", [])
                if not obs:
                    continue
                val = obs[0].get("value")
                if val is None or val == ".":
                    continue
                actual = float(val)
                r.actual = actual
                r.released_at = datetime.utcnow()
                if r.consensus and r.consensus != 0:
                    r.surprise_pct = round(((actual - r.consensus) / abs(r.consensus)) * 100, 3)
                fetched += 1
            except Exception as e:
                logger.debug(f"macro_calendar: FRED fetch {r.fred_series_id} failed: {e}")
        db.commit()
    finally:
        db.close()
    if fetched:
        logger.info(f"macro_calendar: fetched {fetched} actuals from FRED")
    return {"checked": len(rows), "fetched": fetched}


def _serialize(r: MacroEvent) -> Dict[str, Any]:
    return {
        "id": r.id,
        "event_key": r.event_key,
        "event_name": r.event_name,
        "country": r.country,
        "importance": r.importance,
        "release_time_utc": r.release_time_utc.isoformat() if r.release_time_utc else None,
        "consensus": r.consensus,
        "actual": r.actual,
        "unit": r.unit,
        "surprise_pct": r.surprise_pct,
        "released_at": r.released_at.isoformat() if r.released_at else None,
        "fred_series_id": r.fred_series_id,
        "note": r.note,
    }
