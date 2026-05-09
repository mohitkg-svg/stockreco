"""
Earnings-calendar guard for the auto-trader.

Reject entries on tickers with a scheduled earnings release inside the
_EARNINGS_AVOIDANCE_HOURS window. Holding through earnings is a coin-flip +
implied-vol reset — historically an edge-destroying event unless the thesis
is specifically earnings-related (which the rule-based signal generator is
not).

Data source: yfinance `Ticker.earnings_dates` — returns a DataFrame indexed
by UTC datetime with past and upcoming reports. We cache the next upcoming
date per ticker for 12 hours (earnings dates rarely change intraday and
yfinance rate-limits aggressively).

Failure mode: if the lookup errors or returns empty, we log at DEBUG and
return None (= "unknown, don't block") rather than rejecting. Better a
rare miss than to block every trade on transient API flakes.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Block entries when next earnings is within this window.
_EARNINGS_AVOIDANCE_HOURS = 48

# Per-ticker cache: ticker -> (next_earnings_utc_ts_or_None, cache_expiry_ts).
# 12h TTL balances freshness vs API rate-limits (yfinance earnings_dates is
# slow). A None value (no upcoming earnings found) is cached too so the
# next scan doesn't re-query.
_CACHE_TTL_SEC = 12 * 3600
_earnings_cache: dict[str, tuple[Optional[float], float]] = {}


def _fetch_next_earnings_ts(ticker: str) -> Optional[float]:
    """Return the next upcoming earnings date as a UTC unix timestamp, or None.

    FMP first when configured (Cloud-Run-safe); yfinance fallback otherwise.
    """
    try:
        from services import fmp_client
        if fmp_client.is_enabled():
            ts = fmp_client.get_next_earnings_ts(ticker)
            if ts is not None:
                return ts
    except Exception as e:
        logger.debug(f"earnings: FMP next-earnings {ticker} failed: {e}")
    try:
        import yfinance as yf
        from curl_cffi import requests as _cc
        session = _cc.Session(impersonate="chrome110")
        t = yf.Ticker(ticker, session=session)
        df = t.earnings_dates  # raises on failure
        if df is None or df.empty:
            return None
        now = datetime.now(timezone.utc)
        upcoming = [idx for idx in df.index if hasattr(idx, "to_pydatetime") and idx.to_pydatetime() >= now]
        if not upcoming:
            return None
        next_dt = min(upcoming).to_pydatetime()
        # Normalize to UTC if naive
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=timezone.utc)
        return next_dt.timestamp()
    except Exception as e:
        logger.debug(f"earnings lookup failed for {ticker}: {e}")
        return None


def hours_to_next_earnings(ticker: str) -> Optional[float]:
    """Return hours until next earnings (None if unknown / > cache horizon)."""
    ticker = ticker.upper()
    now = time.time()
    cached = _earnings_cache.get(ticker)
    if cached and now < cached[1]:
        ts = cached[0]
    else:
        ts = _fetch_next_earnings_ts(ticker)
        _earnings_cache[ticker] = (ts, now + _CACHE_TTL_SEC)
    if ts is None:
        return None
    return (ts - now) / 3600


def inside_earnings_window(ticker: str, hours: int = _EARNINGS_AVOIDANCE_HOURS) -> bool:
    """True if the ticker has earnings inside the next `hours`."""
    hte = hours_to_next_earnings(ticker)
    if hte is None:
        return False
    return 0 <= hte <= hours


def recent_earnings_catalyst(ticker: str, days_back: int = 10) -> bool:
    """Ground-up Tier 2: True if the ticker had an earnings print within the
    last `days_back` days. Post-earnings momentum (PEAD) is a real factor —
    a recent catalyst strengthens a BUY setup. Inverse for SELL.

    FMP first when configured; yfinance fallback otherwise.
    """
    ticker = ticker.upper()
    try:
        from services import fmp_client
        if fmp_client.is_enabled():
            res = fmp_client.has_recent_earnings(ticker, days_back=days_back)
            if res is not None:
                return res
    except Exception as e:
        logger.debug(f"earnings: FMP recent-catalyst {ticker} failed: {e}")
    now = time.time()
    cached = _earnings_cache.get(ticker)
    if cached and now < cached[1]:
        ts = cached[0]
    else:
        ts = _fetch_next_earnings_ts(ticker)
        _earnings_cache[ticker] = (ts, now + _CACHE_TTL_SEC)
    # _fetch_next_earnings_ts only returns UPCOMING. For the "recent" check
    # we need past dates; do a second fetch without filtering.
    try:
        import yfinance as yf
        from curl_cffi import requests as _cc
        from datetime import datetime, timezone
        session = _cc.Session(impersonate="chrome110")
        t = yf.Ticker(ticker, session=session)
        df = t.earnings_dates
        if df is None or df.empty:
            return False
        cutoff = datetime.now(timezone.utc).timestamp() - (days_back * 86400)
        past_recent = [
            idx for idx in df.index
            if hasattr(idx, "timestamp")
            and idx.timestamp() < datetime.now(timezone.utc).timestamp()
            and idx.timestamp() >= cutoff
        ]
        return len(past_recent) > 0
    except Exception:
        return False
