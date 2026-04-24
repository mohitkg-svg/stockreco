"""Analyst ratings ingest + confidence multiplier.

Data source: yfinance ticker.info exposes `recommendationMean` (1=StrongBuy,
5=StrongSell), `recommendationKey`, `numberOfAnalystOpinions`, and consensus
price targets.

Refreshed 4× per day; ratings move slowly so more-frequent polling wastes
rate limits without improving signal quality.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from database import SessionLocal, AnalystRating, WatchlistStock, CandidatePool

logger = logging.getLogger(__name__)

# Multiplier envelope: strong agreement with signal direction → +10%;
# strong disagreement → -12%. Keep the penalty heavier than the boost —
# betting against consensus is the asymmetric risk.
_MULT_STRONG_AGREE = 1.10
_MULT_AGREE = 1.04
_MULT_NEUTRAL = 1.00
_MULT_DISAGREE = 0.94
_MULT_STRONG_DISAGREE = 0.88

_MIN_ANALYSTS = 3  # below this we don't trust the consensus


def _fetch_one(ticker: str) -> Optional[Dict[str, Any]]:
    """Pull rating block for a single ticker via yfinance. None on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        logger.debug(f"analyst_ratings: yf.Ticker({ticker}) failed: {e}")
        return None
    mean = info.get("recommendationMean")
    key = info.get("recommendationKey")
    count = info.get("numberOfAnalystOpinions")
    if mean is None and key is None and not count:
        return None
    return {
        "ticker": ticker.upper(),
        "mean": float(mean) if mean is not None else None,
        "key": str(key) if key else None,
        "analyst_count": int(count) if count else None,
        "target_mean": float(info.get("targetMeanPrice")) if info.get("targetMeanPrice") else None,
        "target_high": float(info.get("targetHighPrice")) if info.get("targetHighPrice") else None,
        "target_low": float(info.get("targetLowPrice")) if info.get("targetLowPrice") else None,
    }


def _upsert(row: Dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        r = db.query(AnalystRating).filter(AnalystRating.ticker == row["ticker"]).first()
        if r is None:
            r = AnalystRating(ticker=row["ticker"])
            db.add(r)
        r.mean = row.get("mean")
        r.key = row.get("key")
        r.analyst_count = row.get("analyst_count")
        r.target_mean = row.get("target_mean")
        r.target_high = row.get("target_high")
        r.target_low = row.get("target_low")
        r.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def refresh_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    row = _fetch_one(ticker)
    if row is None:
        return None
    _upsert(row)
    return row


def refresh_all() -> Dict[str, Any]:
    """Refresh ratings for watchlist + candidate-pool tickers.
    Scheduled 4×/day; costs ~1 yfinance call per ticker."""
    db = SessionLocal()
    try:
        tickers = set(s.ticker for s in db.query(WatchlistStock).all())
        tickers |= set(r.ticker for r in db.query(CandidatePool).all())
    finally:
        db.close()
    tickers = sorted(tickers)
    if not tickers:
        return {"updated": 0, "total": 0}

    ok = 0
    for t in tickers:
        try:
            if refresh_ticker(t) is not None:
                ok += 1
        except Exception as e:
            logger.debug(f"analyst_ratings: refresh {t} failed: {e}")
    logger.info(f"analyst_ratings: refreshed {ok}/{len(tickers)} tickers")
    return {"updated": ok, "total": len(tickers)}


def get_rating(ticker: str) -> Optional[Dict[str, Any]]:
    """Return latest persisted rating for `ticker`, or None."""
    db = SessionLocal()
    try:
        r = db.query(AnalystRating).filter(AnalystRating.ticker == ticker.upper()).first()
        if r is None:
            return None
        return {
            "ticker": r.ticker,
            "mean": r.mean,
            "key": r.key,
            "analyst_count": r.analyst_count,
            "target_mean": r.target_mean,
            "target_high": r.target_high,
            "target_low": r.target_low,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
    finally:
        db.close()


def rating_multiplier(ticker: str, direction: str) -> float:
    """Confidence multiplier for `direction` (BUY|SELL) based on analyst consensus.

    Mapping (mean is 1..5, 1=StrongBuy):
      BUY  + mean ≤ 2.0        → 1.10  (strong agree)
      BUY  + mean ≤ 2.5        → 1.04  (agree)
      BUY  + mean ≥ 4.0        → 0.88  (strong disagree — crowd says SELL)
      BUY  + mean ≥ 3.5        → 0.94  (disagree)
      SELL inverts the scale.
      Elsewhere (Hold zone 2.5-3.5)    → 1.00
      Coverage < 3 analysts OR no data → 1.00 (don't trust thin consensus)
    """
    r = get_rating(ticker)
    if r is None:
        return _MULT_NEUTRAL
    count = r.get("analyst_count") or 0
    mean = r.get("mean")
    if count < _MIN_ANALYSTS or mean is None:
        return _MULT_NEUTRAL
    # Stale guard: ratings older than 10 days are ignored.
    try:
        if r.get("updated_at"):
            age = datetime.utcnow() - datetime.fromisoformat(r["updated_at"])
            if age > timedelta(days=10):
                return _MULT_NEUTRAL
    except Exception:
        pass

    direction = (direction or "").upper()
    if direction == "BUY":
        if mean <= 2.0: return _MULT_STRONG_AGREE
        if mean <= 2.5: return _MULT_AGREE
        if mean >= 4.0: return _MULT_STRONG_DISAGREE
        if mean >= 3.5: return _MULT_DISAGREE
        return _MULT_NEUTRAL
    if direction == "SELL":
        if mean >= 4.0: return _MULT_STRONG_AGREE
        if mean >= 3.5: return _MULT_AGREE
        if mean <= 2.0: return _MULT_STRONG_DISAGREE
        if mean <= 2.5: return _MULT_DISAGREE
        return _MULT_NEUTRAL
    return _MULT_NEUTRAL


def rating_reason_line(ticker: str, direction: str) -> Optional[str]:
    """Human-readable bullet for the signal's reasoning[] list. None if no data."""
    r = get_rating(ticker)
    if r is None or (r.get("analyst_count") or 0) < _MIN_ANALYSTS or r.get("mean") is None:
        return None
    mean = r["mean"]
    count = r["analyst_count"]
    key = (r.get("key") or "").replace("_", " ")
    tm = r.get("target_mean")
    mult = rating_multiplier(ticker, direction)
    if mult >= _MULT_STRONG_AGREE:
        mark = "✅"
        tag = "strong agreement"
    elif mult > _MULT_NEUTRAL:
        mark = "✅"
        tag = "agreement"
    elif mult <= _MULT_STRONG_DISAGREE:
        mark = "❌"
        tag = "strong disagreement"
    elif mult < _MULT_NEUTRAL:
        mark = "⚠️"
        tag = "disagreement"
    else:
        mark = "·"
        tag = "neutral"
    tgt = f", target ${tm:.2f}" if tm else ""
    return f"{mark} Analysts: {key or 'n/a'} ({count} analysts, mean {mean:.2f}{tgt}) — {tag} with {direction}"
