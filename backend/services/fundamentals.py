"""Fundamentals — fetch, score, signal-generator multiplier.

Source: yfinance .info (free, already in our stack via analyst_ratings).
Cadence: weekly Sunday 04:30 UTC + on-demand. Most fundamental fields only
update once per quarter (after earnings), so weekly is plenty.

Change detection: SHA256 over the stable numeric fields. If the new fetch
hashes identical to the persisted row, we only bump last_checked_at —
no UPDATE on the data fields, so `last_changed_at` becomes a true
"fundamentals shifted on this date" timeline.

Quality score (-100..+100):
  + Profitability (25 pts): profit_margin, operating_margin, ROE
  + Growth (25 pts): revenue_growth, earnings_growth
  + Balance sheet (25 pts): debt_to_equity, current_ratio
  + Valuation (25 pts): PEG, P/E, EV/EBITDA

Multiplier envelope (signal_generator): 0.92..1.08. Asymmetric like
analyst_ratings — penalty heavier than boost since betting against junk
fundamentals on a long is the asymmetric risk.
"""
from __future__ import annotations
import hashlib
import json
import logging
import math
from datetime import datetime
from typing import Optional, Dict, Any, List

from database import SessionLocal, Fundamentals, WatchlistStock, CandidatePool

logger = logging.getLogger(__name__)

# Multiplier envelope (mirrors analyst_ratings shape).
_MULT_STRONG_GOOD = 1.08
_MULT_GOOD = 1.04
_MULT_NEUTRAL = 1.00
_MULT_BAD = 0.96
_MULT_STRONG_BAD = 0.92

# Score thresholds for the multiplier mapping.
_GOOD_THRESHOLD = 30.0
_STRONG_GOOD_THRESHOLD = 70.0
_BAD_THRESHOLD = -30.0
_STRONG_BAD_THRESHOLD = -50.0


# Fields that contribute to the change-detection hash. Excludes the
# computed quality_score (derives from these) and the timestamps.
_HASH_FIELDS = (
    "sector", "industry", "market_cap", "shares_outstanding",
    "pe_ratio", "pe_forward", "peg_ratio",
    "price_to_book", "price_to_sales", "ev_to_ebitda",
    "revenue_growth_yoy", "earnings_growth_yoy",
    "profit_margin", "operating_margin",
    "return_on_equity", "return_on_assets",
    "debt_to_equity", "current_ratio",
    "free_cash_flow", "dividend_yield",
    "beta",
    "short_pct_float", "short_ratio",
)


def _safe(v) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _fetch_one(ticker: str) -> Optional[Dict[str, Any]]:
    """Pull fundamentals for a single ticker via yfinance."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        logger.debug(f"fundamentals: yf.Ticker({ticker}) failed: {e}")
        return None
    if not info:
        return None
    # yfinance keys are inconsistent; pull both common spellings.
    return {
        "ticker": ticker.upper(),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": _safe(info.get("marketCap")),
        "shares_outstanding": _safe(info.get("sharesOutstanding")),
        "pe_ratio": _safe(info.get("trailingPE")),
        "pe_forward": _safe(info.get("forwardPE")),
        "peg_ratio": _safe(info.get("pegRatio") or info.get("trailingPegRatio")),
        "price_to_book": _safe(info.get("priceToBook")),
        "price_to_sales": _safe(info.get("priceToSalesTrailing12Months")),
        "ev_to_ebitda": _safe(info.get("enterpriseToEbitda")),
        "revenue_growth_yoy": _safe(info.get("revenueGrowth")),
        "earnings_growth_yoy": _safe(info.get("earningsGrowth")),
        "profit_margin": _safe(info.get("profitMargins")),
        "operating_margin": _safe(info.get("operatingMargins")),
        "return_on_equity": _safe(info.get("returnOnEquity")),
        "return_on_assets": _safe(info.get("returnOnAssets")),
        "debt_to_equity": _safe(info.get("debtToEquity")),
        "current_ratio": _safe(info.get("currentRatio")),
        "free_cash_flow": _safe(info.get("freeCashflow")),
        "dividend_yield": _safe(info.get("dividendYield")),
        "beta": _safe(info.get("beta")),
        # yfinance uses shortPercentOfFloat (decimal, 0.12 = 12%) and shortRatio (days-to-cover)
        "short_pct_float": _safe(info.get("shortPercentOfFloat")),
        "short_ratio": _safe(info.get("shortRatio")),
    }


def _hash_payload(row: Dict[str, Any]) -> str:
    """Stable SHA256 over the change-detection fields."""
    payload = {k: row.get(k) for k in _HASH_FIELDS}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_quality_score(row: Dict[str, Any]) -> float:
    """Return a -100..+100 composite quality score. Missing fields contribute 0
    so a partial dataset doesn't blow up. The score is intentionally simple:
    absolute thresholds (not sector-relative). Sector-aware scoring is a
    backlog item — needs running aggregates."""

    score = 0.0

    # ---- Profitability (max 25) -------------------------------------------
    prof = row.get("profit_margin")
    if prof is not None:
        if prof > 0.20:   score += 10
        elif prof > 0.10: score += 5
        elif prof < 0:    score -= 10
    op = row.get("operating_margin")
    if op is not None:
        if op > 0.25:   score += 5
        elif op > 0.15: score += 3
        elif op < 0:    score -= 5
    roe = row.get("return_on_equity")
    if roe is not None:
        if roe > 0.15:  score += 10
        elif roe > 0.10: score += 5
        elif roe < 0:    score -= 5

    # ---- Growth (max 25) --------------------------------------------------
    rg = row.get("revenue_growth_yoy")
    if rg is not None:
        if rg > 0.30:   score += 12
        elif rg > 0.15: score += 8
        elif rg > 0.05: score += 3
        elif rg < -0.10: score -= 10
    eg = row.get("earnings_growth_yoy")
    if eg is not None:
        if eg > 0.30:   score += 13
        elif eg > 0.15: score += 8
        elif eg > 0.05: score += 3
        elif eg < -0.20: score -= 12

    # ---- Balance sheet (max 25) ------------------------------------------
    de = row.get("debt_to_equity")
    if de is not None:
        # yfinance returns this in PERCENT form (50 = 0.5×); normalize defensively
        de_norm = de / 100 if de > 5 else de
        if de_norm < 0.30: score += 12
        elif de_norm < 1.0: score += 8
        elif de_norm > 4.0: score -= 20
        elif de_norm > 2.0: score -= 10
    cr = row.get("current_ratio")
    if cr is not None:
        if cr > 2.0:   score += 13
        elif cr > 1.5: score += 8
        elif cr < 1.0: score -= 10

    # ---- Valuation (max 25) ----------------------------------------------
    peg = row.get("peg_ratio")
    if peg is not None and peg > 0:
        if peg < 1.0:  score += 12
        elif peg < 2.0: score += 5
        elif peg > 3.0: score -= 10
    pe = row.get("pe_ratio")
    if pe is not None and pe > 0:
        if pe < 15:    score += 8
        elif pe < 25:  score += 3
        elif pe > 50:  score -= 10
    ev_eb = row.get("ev_to_ebitda")
    if ev_eb is not None and ev_eb > 0:
        if ev_eb < 12:  score += 5
        elif ev_eb > 30: score -= 8

    return max(-100.0, min(100.0, round(score, 1)))


def refresh_ticker(ticker: str, force: bool = False) -> Optional[Dict[str, Any]]:
    """Fetch fundamentals for `ticker` and upsert. Returns the persisted row
    as a dict, or None on fetch failure. If the new hash matches the old,
    only `last_checked_at` is updated (no data churn)."""
    row = _fetch_one(ticker)
    if row is None:
        return None
    new_hash = _hash_payload(row)
    quality = compute_quality_score(row)

    db = SessionLocal()
    try:
        existing = db.query(Fundamentals).filter(Fundamentals.ticker == row["ticker"]).first()
        now = datetime.utcnow()
        if existing is None:
            persisted = Fundamentals(
                **{k: row.get(k) for k in _HASH_FIELDS},
                ticker=row["ticker"],
                quality_score=quality,
                data_hash=new_hash,
                last_checked_at=now,
                last_changed_at=now,
            )
            db.add(persisted)
        elif force or existing.data_hash != new_hash:
            for k in _HASH_FIELDS:
                setattr(existing, k, row.get(k))
            existing.quality_score = quality
            existing.data_hash = new_hash
            existing.last_changed_at = now
            existing.last_checked_at = now
        else:
            # No change — only bump last_checked_at to record the verification.
            existing.last_checked_at = now
        db.commit()
        return get_fundamentals(row["ticker"])
    finally:
        db.close()


def refresh_all(max_workers: int = 4) -> Dict[str, Any]:
    """Refresh fundamentals for watchlist + candidate pool tickers in parallel.
    yfinance is rate-limited around ~30 req/min; 4 workers stays well under."""
    from concurrent.futures import ThreadPoolExecutor

    db = SessionLocal()
    try:
        tickers = set(s.ticker for s in db.query(WatchlistStock).all())
        tickers |= set(r.ticker for r in db.query(CandidatePool).all())
    finally:
        db.close()
    tickers = sorted(tickers)
    if not tickers:
        return {"checked": 0, "updated": 0, "total": 0}

    updated = 0
    checked = 0
    fetched = 0

    def _one(t: str) -> int:
        try:
            row_before = get_fundamentals(t)
            row_after = refresh_ticker(t)
            if row_after is None:
                return 0
            if row_before is None or row_before.get("data_hash") != row_after.get("data_hash"):
                return 1
        except Exception as e:
            logger.debug(f"fundamentals refresh {t}: {e}")
        return 0

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fund") as ex:
        for delta in ex.map(_one, tickers):
            checked += 1
            if delta:
                updated += 1
            fetched += 1
    logger.info(f"fundamentals: checked {checked} tickers, {updated} changed since last fetch")
    return {"checked": checked, "updated": updated, "total": len(tickers)}


def get_fundamentals(ticker: str) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        r = db.query(Fundamentals).filter(Fundamentals.ticker == ticker.upper()).first()
        if r is None:
            return None
        return {
            "ticker": r.ticker,
            "sector": r.sector, "industry": r.industry,
            "market_cap": r.market_cap, "shares_outstanding": r.shares_outstanding,
            "pe_ratio": r.pe_ratio, "pe_forward": r.pe_forward, "peg_ratio": r.peg_ratio,
            "price_to_book": r.price_to_book, "price_to_sales": r.price_to_sales,
            "ev_to_ebitda": r.ev_to_ebitda,
            "revenue_growth_yoy": r.revenue_growth_yoy,
            "earnings_growth_yoy": r.earnings_growth_yoy,
            "profit_margin": r.profit_margin, "operating_margin": r.operating_margin,
            "return_on_equity": r.return_on_equity, "return_on_assets": r.return_on_assets,
            "debt_to_equity": r.debt_to_equity, "current_ratio": r.current_ratio,
            "free_cash_flow": r.free_cash_flow, "dividend_yield": r.dividend_yield,
            "beta": r.beta,
            "short_pct_float": r.short_pct_float, "short_ratio": r.short_ratio,
            "quality_score": r.quality_score, "data_hash": r.data_hash,
            "last_checked_at": r.last_checked_at.isoformat() if r.last_checked_at else None,
            "last_changed_at": r.last_changed_at.isoformat() if r.last_changed_at else None,
        }
    finally:
        db.close()


def short_interest_multiplier(ticker: str, direction: str) -> float:
    """Short-interest signal-confidence multiplier.

    For BUY signals:
      * Very crowded short (≥ 25% of float) → 0.92 — fundamental skepticism
        is real; we should assume the shorts might be right.
      * Moderately shorted (15-25%) → 1.02 slight boost (squeeze potential
        nudges us in if a bull signal IS firing).
      * Normal (< 15%) → neutral.
    For SELL signals: inverse — already-crowded shorts mean the easy money
    has been made, penalize fresh SELLs on heavily-shorted names.
    """
    r = get_fundamentals(ticker)
    if r is None or r.get("short_pct_float") is None:
        return _MULT_NEUTRAL
    pct = float(r["short_pct_float"])
    direction = (direction or "").upper()
    if direction == "BUY":
        if pct >= 0.25: return 0.92  # deeply crowded short — respect the skepticism
        if pct >= 0.15: return 1.02  # mild squeeze tilt
        return _MULT_NEUTRAL
    if direction == "SELL":
        if pct >= 0.25: return 0.92  # already-crowded trade; late to the party
        if pct >= 0.15: return 0.96
        return _MULT_NEUTRAL
    return _MULT_NEUTRAL


def short_interest_reason_line(ticker: str, direction: str) -> Optional[str]:
    r = get_fundamentals(ticker)
    if r is None or r.get("short_pct_float") is None:
        return None
    pct = float(r["short_pct_float"])
    mult = short_interest_multiplier(ticker, direction)
    if mult < _MULT_NEUTRAL:
        mark = "⚠️"
    elif mult > _MULT_NEUTRAL:
        mark = "🔥"
    else:
        return None  # don't pollute reasoning with neutral lines
    ratio = r.get("short_ratio")
    ratio_bit = f", {ratio:.1f} days-to-cover" if ratio else ""
    return f"{mark} Short interest: {pct*100:.1f}% of float{ratio_bit} — {'crowded' if pct >= 0.25 else 'elevated'} short vs {direction}"


def beta_weight(ticker: str, default: float = 1.0,
                floor: float = 0.5, ceil: float = 2.0) -> float:
    """Return the ticker's 5y beta clamped to [floor, ceil]. Used to
    beta-weight portfolio heat — 5 high-beta tech names concentrate more
    systematic risk than 5 utilities at the same raw $-at-risk.

    Missing beta → `default` (1.0 = treat as market-weighted).
    Clamp prevents a single meme stock with beta 4 from dominating the
    heat calc (probably noisy data anyway)."""
    r = get_fundamentals(ticker)
    if r is None:
        return default
    b = r.get("beta")
    if b is None or not math.isfinite(float(b)):
        return default
    return max(floor, min(ceil, float(b)))


def quality_multiplier(ticker: str, direction: str) -> float:
    """Confidence multiplier (0.92..1.08) for `direction` ∈ {BUY, SELL}.

    BUY:
      score ≥ +70 (excellent)  → 1.08
      score ≥ +30              → 1.04
      score ≤ -50 (junk)       → 0.92
      score ≤ -30              → 0.96
      else neutral 1.00

    SELL is the mirror — junk fundamentals confirm a bearish thesis.
    """
    r = get_fundamentals(ticker)
    if r is None or r.get("quality_score") is None:
        return _MULT_NEUTRAL
    score = float(r["quality_score"])
    direction = (direction or "").upper()
    if direction == "BUY":
        if score >= _STRONG_GOOD_THRESHOLD: return _MULT_STRONG_GOOD
        if score >= _GOOD_THRESHOLD:        return _MULT_GOOD
        if score <= _STRONG_BAD_THRESHOLD:  return _MULT_STRONG_BAD
        if score <= _BAD_THRESHOLD:         return _MULT_BAD
        return _MULT_NEUTRAL
    if direction == "SELL":
        if score <= _STRONG_BAD_THRESHOLD:  return _MULT_STRONG_GOOD
        if score <= _BAD_THRESHOLD:         return _MULT_GOOD
        if score >= _STRONG_GOOD_THRESHOLD: return _MULT_STRONG_BAD
        if score >= _GOOD_THRESHOLD:        return _MULT_BAD
        return _MULT_NEUTRAL
    return _MULT_NEUTRAL


def quality_reason_line(ticker: str, direction: str) -> Optional[str]:
    r = get_fundamentals(ticker)
    if r is None or r.get("quality_score") is None:
        return None
    score = float(r["quality_score"])
    mult = quality_multiplier(ticker, direction)
    if mult >= _MULT_STRONG_GOOD:
        mark = "✅"
        tag = "strong fundamentals"
    elif mult > _MULT_NEUTRAL:
        mark = "✅"
        tag = "solid fundamentals"
    elif mult <= _MULT_STRONG_BAD:
        mark = "❌"
        tag = "weak fundamentals"
    elif mult < _MULT_NEUTRAL:
        mark = "⚠️"
        tag = "soft fundamentals"
    else:
        mark = "·"
        tag = "neutral fundamentals"
    pe = r.get("pe_ratio")
    rg = r.get("revenue_growth_yoy")
    pm = r.get("profit_margin")
    bits = []
    if pe is not None:
        bits.append(f"PE {pe:.1f}")
    if rg is not None:
        bits.append(f"rev g/y {rg*100:+.1f}%")
    if pm is not None:
        bits.append(f"margin {pm*100:.1f}%")
    detail = ", ".join(bits) if bits else "no key ratios"
    return f"{mark} Fundamentals: score {score:+.0f} ({detail}) — {tag} for {direction}"
