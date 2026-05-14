"""Financial Modeling Prep (FMP) REST client.

Drop-in fundamentals/earnings/analyst data source for production environments
where yfinance hits Yahoo's IP block on Cloud Run egress. Uses the /stable/
API (post-Aug 2025 migration from legacy /api/v3 and /api/v4 endpoints):

  - /stable/profile?symbol={ticker}              sector/industry/beta/mktCap
  - /stable/key-metrics?symbol={ticker}&period=ttm  ROE, margins, FCF
  - /stable/ratios?symbol={ticker}&period=ttm       PE, P/B, debt/equity
  - /stable/financial-growth?symbol={ticker}     revenue/earnings growth
  - /stable/earning-calendar?symbol={ticker}     upcoming earnings
  - /stable/historical-earning-calendar?symbol={ticker} historical earnings
  - /stable/upgrades-downgrades-consensus?symbol={ticker} analyst consensus
  - /stable/price-target-consensus?symbol={ticker}  analyst target consensus
  - /stable/rss-feed?type={form}                 SEC filings poll (8-K, Form 4)

Fail-soft contract: every public function returns None / [] / False when
`FMP_API_KEY` is unset or any HTTP / JSON / timeout error fires. Callers in
fundamentals.py / earnings.py / analyst_ratings.py keep yfinance as the
fallback path. Not a hard cutover.

Caching: 6h TTL for fundamentals (only refreshes after 10-Q filings), 2h for
earnings calendar, 4h for analyst ratings, 2min for SEC RSS. Module-level dict
behind a lock; single-operator app, no cross-process invalidation.
"""
from __future__ import annotations
import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_BASE = "https://financialmodelingprep.com/stable"
_TIMEOUT_SEC = 8.0
_DEFAULT_TTL_SEC = 6 * 3600

_cache: Dict[str, "tuple[Any, float]"] = {}
_cache_lock = threading.Lock()


def _api_key() -> Optional[str]:
    key = os.getenv("FMP_API_KEY", "").strip()
    return key or None


def is_enabled() -> bool:
    return _api_key() is not None


def _cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        value, expiry = hit
        if time.time() >= expiry:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: Any, ttl_sec: float) -> None:
    with _cache_lock:
        # Cap cache to ~2k entries to avoid unbounded growth on long-running instances.
        if len(_cache) >= 2000:
            for k in list(_cache.keys())[:200]:
                _cache.pop(k, None)
        _cache[key] = (value, time.time() + ttl_sec)


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _get(url: str, params: Optional[Dict[str, Any]] = None,
         ttl_sec: float = _DEFAULT_TTL_SEC) -> Optional[Any]:
    """GET with cache, single retry on 429/5xx, JSON decode. None on any failure."""
    key = _api_key()
    if key is None:
        return None
    p = dict(params or {})
    p["apikey"] = key
    cache_key = url + "?" + "&".join(
        f"{k}={v}" for k, v in sorted(p.items()) if k != "apikey"
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        import requests
    except Exception:
        return None
    for attempt in range(2):
        try:
            r = requests.get(url, params=p, timeout=_TIMEOUT_SEC)
            if r.status_code == 200:
                data = r.json()
                _cache_put(cache_key, data, ttl_sec)
                return data
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(1.0)
                continue
            logger.warning(f"FMP {url} returned HTTP {r.status_code}")
            return None
        except Exception as e:
            if attempt == 0:
                continue
            logger.debug(f"FMP {url} request failed: {e}")
            return None
    return None


def _first_dict(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


# ---- Fundamentals --------------------------------------------------------

def get_profile(ticker: str) -> Optional[Dict[str, Any]]:
    """Company profile: sector/industry/beta/mktCap. 24h TTL — rarely changes."""
    return _first_dict(_get(
        f"{_BASE}/profile",
        params={"symbol": ticker.upper()},
        ttl_sec=24 * 3600,
    ))


def get_key_metrics_ttm(ticker: str) -> Optional[Dict[str, Any]]:
    return _first_dict(_get(
        f"{_BASE}/key-metrics",
        params={"symbol": ticker.upper(), "period": "ttm"},
    ))


def get_ratios_ttm(ticker: str) -> Optional[Dict[str, Any]]:
    return _first_dict(_get(
        f"{_BASE}/ratios",
        params={"symbol": ticker.upper(), "period": "ttm"},
    ))


def get_financial_growth(ticker: str) -> Optional[Dict[str, Any]]:
    return _first_dict(_get(
        f"{_BASE}/financial-growth",
        params={"symbol": ticker.upper(), "period": "annual", "limit": 1},
    ))


def get_short_interest(ticker: str) -> Optional[Dict[str, Any]]:
    return _first_dict(_get(
        f"{_BASE}/short-interest",
        params={"symbol": ticker.upper(), "limit": 1},
    ))


def get_fundamentals(ticker: str) -> Optional[Dict[str, Any]]:
    """Composite fundamentals fetch matching services.fundamentals._fetch_one()
    output shape. Returns None if profile fetch fails (caller falls back)."""
    profile = get_profile(ticker)
    if profile is None:
        return None
    km = get_key_metrics_ttm(ticker) or {}
    ratios = get_ratios_ttm(ticker) or {}
    growth = get_financial_growth(ticker) or {}
    short = get_short_interest(ticker) or {}
    return {
        "ticker": ticker.upper(),
        "sector": profile.get("sector") or None,
        "industry": profile.get("industry") or None,
        "market_cap": _safe_float(profile.get("mktCap") or profile.get("marketCap")),
        # FMP doesn't expose float shares cleanly on the profile endpoint;
        # leave shares_outstanding null and let yfinance fill it on next refresh.
        "shares_outstanding": None,
        "pe_ratio": _safe_float(
            ratios.get("priceToEarningsRatio") or profile.get("pe")
        ),
        "pe_forward": None,
        "peg_ratio": _safe_float(ratios.get("priceToEarningsGrowthRatio")),
        "price_to_book": _safe_float(ratios.get("priceToBookRatio")),
        "price_to_sales": _safe_float(ratios.get("priceToSalesRatio")),
        "ev_to_ebitda": _safe_float(km.get("evToEBITDA")),
        "revenue_growth_yoy": _safe_float(growth.get("revenueGrowth")),
        "earnings_growth_yoy": _safe_float(
            growth.get("epsgrowth") or growth.get("netIncomeGrowth")
        ),
        "profit_margin": _safe_float(
            ratios.get("netProfitMargin") or ratios.get("bottomLineProfitMargin")
        ),
        "operating_margin": _safe_float(ratios.get("operatingProfitMargin")),
        "return_on_equity": _safe_float(km.get("returnOnEquity")),
        "return_on_assets": _safe_float(km.get("returnOnAssets")),
        "debt_to_equity": _safe_float(ratios.get("debtToEquityRatio")),
        "current_ratio": _safe_float(
            km.get("currentRatio") or ratios.get("currentRatio")
        ),
        "free_cash_flow": _safe_float(ratios.get("freeCashFlowPerShare")),
        "dividend_yield": _safe_float(ratios.get("dividendYield")),
        "beta": _safe_float(profile.get("beta")),
        "short_pct_float": _safe_float(short.get("shortPercentOfFloat")),
        "short_ratio": _safe_float(short.get("daysToCover")),
    }


# ---- Earnings ------------------------------------------------------------

def _parse_iso(date_str: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_next_earnings_ts(ticker: str) -> Optional[float]:
    """Next upcoming earnings timestamp (UTC unix seconds), or None.

    The /stable/earnings-calendar endpoint caps at ~4000 results per request
    and doesn't support a symbol filter, so we chunk into 30-day windows to
    avoid missing large-cap tickers buried past the cap.
    """
    sym = ticker.upper()
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    upcoming_ts: Optional[float] = None
    # Search up to 180 days out in 30-day chunks
    for offset in range(0, 180, 30):
        chunk_start = (now + timedelta(days=offset)).date()
        chunk_end = (now + timedelta(days=offset + 30)).date()
        data = _get(
            f"{_BASE}/earnings-calendar",
            params={"from": chunk_start.isoformat(), "to": chunk_end.isoformat()},
            ttl_sec=2 * 3600,
        )
        if not isinstance(data, list):
            continue
        for r in data:
            if not isinstance(r, dict) or (r.get("symbol") or "").upper() != sym:
                continue
            dt = _parse_iso(r.get("date") or "")
            if dt is None:
                continue
            ts = dt.timestamp()
            if ts < now_ts:
                continue
            if upcoming_ts is None or ts < upcoming_ts:
                upcoming_ts = ts
        # Found it — no need to check further windows
        if upcoming_ts is not None:
            break
    return upcoming_ts


def has_recent_earnings(ticker: str, days_back: int = 10) -> Optional[bool]:
    """True if ticker had an earnings print in the last `days_back` days.
    None on fetch failure (caller falls back); False if confirmed no event."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days_back)).date()
    end = now.date()
    data = _get(
        f"{_BASE}/earnings-calendar",
        params={"from": start.isoformat(), "to": end.isoformat()},
        ttl_sec=2 * 3600,
    )
    if not isinstance(data, list):
        return None
    sym = ticker.upper()
    cutoff_ts = (now - timedelta(days=days_back)).timestamp()
    now_ts = now.timestamp()
    for r in data:
        if not isinstance(r, dict) or (r.get("symbol") or "").upper() != sym:
            continue
        dt = _parse_iso(r.get("date") or "")
        if dt is None:
            continue
        ts = dt.timestamp()
        if cutoff_ts <= ts <= now_ts:
            return True
    return False


# ---- Analyst ratings -----------------------------------------------------

def get_analyst_consensus(ticker: str) -> Optional[Dict[str, Any]]:
    """Return analyst rating shape compatible with analyst_ratings._fetch_one().

    Uses /stable/price-target-consensus for target prices and
    /stable/price-target-summary for analyst count. The legacy
    upgrades-downgrades-consensus endpoint is no longer available on the
    stable API.
    """
    tgt = _first_dict(_get(
        f"{_BASE}/price-target-consensus",
        params={"symbol": ticker.upper()},
        ttl_sec=4 * 3600,
    ))
    if tgt is None:
        return None
    target_mean = _safe_float(tgt.get("targetConsensus"))
    target_high = _safe_float(tgt.get("targetHigh"))
    target_low = _safe_float(tgt.get("targetLow"))
    target_median = _safe_float(tgt.get("targetMedian"))
    # Get analyst count from price-target-summary
    summary = _first_dict(_get(
        f"{_BASE}/price-target-summary",
        params={"symbol": ticker.upper()},
        ttl_sec=4 * 3600,
    ))
    count = int((summary or {}).get("lastYearCount") or 0)
    if count == 0 and target_mean is None:
        return None
    # r92: /stable/ does NOT expose rating buckets. Derive a 1..5 consensus
    # mean from the analyst-target upside (target_mean / current_price - 1).
    # Higher upside → lower mean (1=StrongBuy). Bands chosen to match the
    # rating_multiplier zones in analyst_ratings.py:
    #   upside ≥ +30%        → 1.0  (StrongBuy)   → BUY ×1.10
    #   +15% ≤ upside < +30% → 2.0  (Buy)         → BUY ×1.10 / ×1.04 boundary
    #   -10% ≤ upside < +15% → 3.0  (Hold)        → NEUTRAL
    #   -25% ≤ upside < -10% → 4.0  (Sell)        → BUY ×0.88 (disagree)
    #   upside < -25%        → 5.0  (StrongSell)  → BUY ×0.88 (strong disagree)
    mean: Optional[float] = None
    key: Optional[str] = None
    try:
        if target_mean is not None and target_mean > 0:
            from services.data_fetcher import get_current_price  # local: avoid cycle
            cp = get_current_price(ticker.upper())
            price = float(cp[0]) if cp and cp[0] else None
            if price and price > 0:
                upside = (target_mean / price) - 1.0
                if upside >= 0.30:
                    mean, key = 1.0, "strong_buy"
                elif upside >= 0.15:
                    mean, key = 2.0, "buy"
                elif upside >= -0.10:
                    mean, key = 3.0, "hold"
                elif upside >= -0.25:
                    mean, key = 4.0, "sell"
                else:
                    mean, key = 5.0, "strong_sell"
    except Exception as e:
        logger.warning(f"fmp.get_analyst_consensus: mean-from-upside failed for {ticker}: {e}")
        mean, key = None, None
    return {
        "ticker": ticker.upper(),
        "mean": mean,
        "key": key,
        "analyst_count": count or None,
        "target_mean": target_mean,
        "target_high": target_high,
        "target_low": target_low,
    }


# ---- SEC filings poll ----------------------------------------------------

# Process-local seen-set so the cron poll doesn't re-insert the same filing
# every 5 min. Keyed on FMP's filing `link` (unique per filing). Cap size so
# it doesn't grow unbounded across a multi-day uptime window.
_seen_filings_lock = threading.Lock()
_seen_filings: List[str] = []
_SEEN_FILINGS_CAP = 2000


def _mark_seen(link: str) -> bool:
    """Return True if `link` was newly added; False if already seen."""
    with _seen_filings_lock:
        if link in _seen_filings:
            return False
        _seen_filings.append(link)
        if len(_seen_filings) > _SEEN_FILINGS_CAP:
            del _seen_filings[: len(_seen_filings) - _SEEN_FILINGS_CAP]
        return True


def get_recent_sec_filings(form_type: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Recent SEC filings of `form_type` (e.g. '8-K', '4', '10-Q'). [] on failure.

    NOTE: The SEC RSS feed endpoint is not available on the /stable/ API.
    This function attempts the call but gracefully returns [] if unavailable.
    The primary SEC filing source is the direct EDGAR scraper in insider_trades.py.
    """
    data = _get(
        f"{_BASE}/rss-feed",
        params={"type": form_type, "limit": limit, "page": 0},
        ttl_sec=120,
    )
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _form_to_event_kind(form_type: str) -> Optional[str]:
    """Map a SEC form to a CandidateEvent.kind. Anything else → None (skipped)."""
    ft = (form_type or "").upper()
    if "4" in ft:
        return "INSIDER_BUY"
    if "8" in ft:
        return "PEAD"
    return None


def poll_sec_filings_into_events() -> Dict[str, int]:
    """Poll FMP RSS for fresh 8-K and Form 4 filings, insert as CandidateEvent rows.

    Idempotent via the process-local `_seen_filings` set keyed on the FMP
    filing link. Complements the push webhook in main.py:fmp_sec_webhook —
    if the push misses an event (network blip, FMP outage), the next poll
    catches it within 5 min.
    """
    if not is_enabled():
        return {"checked": 0, "inserted": 0, "skipped": 0}
    inserted = 0
    skipped = 0
    checked = 0
    rows_to_insert: List[Dict[str, Any]] = []
    for form_type in ("8-K", "4"):
        kind = _form_to_event_kind(form_type)
        if kind is None:
            continue
        for r in get_recent_sec_filings(form_type, limit=50):
            checked += 1
            link = r.get("link") or r.get("finalLink") or ""
            ticker = (r.get("symbol") or r.get("ticker") or "").upper()
            if not link or not ticker:
                skipped += 1
                continue
            if not _mark_seen(link):
                skipped += 1
                continue
            rows_to_insert.append({
                "kind": kind, "ticker": ticker,
                "score": 80.0,
                "features": json.dumps({
                    "source": "fmp_rss",
                    "form": form_type,
                    "link": link,
                    "accepted_date": r.get("acceptedDate"),
                    "title": r.get("title"),
                }),
            })
    if not rows_to_insert:
        return {"checked": checked, "inserted": 0, "skipped": skipped}
    try:
        from database import SessionLocal, CandidateEvent
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            ttl = now + timedelta(minutes=60)
            for r in rows_to_insert:
                db.add(CandidateEvent(
                    kind=r["kind"], ticker=r["ticker"], score=r["score"],
                    features=r["features"], expires_at=ttl,
                ))
                inserted += 1
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"fmp poll_sec_filings DB insert failed: {e}")
        return {"checked": checked, "inserted": 0, "skipped": skipped}
    if inserted:
        logger.info(f"fmp_sec_poll: inserted {inserted} CandidateEvent rows ({skipped} dups)")
    return {"checked": checked, "inserted": inserted, "skipped": skipped}
