"""Institutional-holdings tracker (pragmatic 13F).

Implementation choice: instead of parsing raw SEC 13F XML with CUSIP→ticker
mapping (fragile, ~1 day of glue code), we use yfinance's
`.institutional_holders` and `.mutualfund_holders` attributes, which are
derived from the same 13F data and expose a per-holder `pctChange`
(quarter-over-quarter position change).

Signal value is intrinsically low (quarterly cadence, 45-day lag) — we
keep the multiplier tight (±3%). Primary use: tilt slightly against
tickers where institutions are actively trimming, boost where many new
funds are initiating.

Cadence: weekly (13F filings update quarterly anyway, but we refresh
weekly to keep in sync with the other fundamentals jobs).
"""
from __future__ import annotations
import logging
import math
from datetime import datetime
from typing import Optional, Dict, Any

from database import SessionLocal, InstitutionalHoldings, WatchlistStock, CandidatePool

logger = logging.getLogger(__name__)

_MULT_NEUTRAL = 1.0
_MULT_CONFIRM_STRONG = 1.03
_MULT_CONFIRM_MILD = 1.015
_MULT_CONTRA_STRONG = 0.97
_MULT_CONTRA_MILD = 0.985


def _safe(v) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _current_quarter_label(dt: Optional[datetime] = None) -> str:
    """Return "YYYYQ" for the most recent completed quarter."""
    dt = dt or datetime.utcnow()
    q = (dt.month - 1) // 3 + 1
    # If we're in Q1 still waiting for Q4's 13F filings, the last-reportable
    # quarter is Q4 of prior year.
    return f"{dt.year}Q{q}"


def _fetch_one(ticker: str) -> Optional[Dict[str, Any]]:
    """Pull institutional_holders + mutualfund_holders via yfinance.
    Returns aggregate: total_holders, weighted_pct_change_qoq, new_initiations."""
    try:
        import yfinance as yf
        yf_ticker = yf.Ticker(ticker)
        inst = yf_ticker.institutional_holders
        mfund = yf_ticker.mutualfund_holders
    except Exception as e:
        logger.debug(f"institutional: yf.Ticker({ticker}) failed: {e}")
        return None

    frames = []
    for df in (inst, mfund):
        if df is not None and hasattr(df, "to_dict") and len(df) > 0:
            frames.append(df)
    if not frames:
        return None

    total_holders = sum(len(df) for df in frames)
    top_holder_name = None
    top_holder_pct_held = None

    # institutional_holders has columns: Holder, Shares, Date Reported, % Out, Value
    # plus (sometimes) pctHeld and pctChange in newer yfinance versions.
    weighted_deltas: list = []
    new_count = 0
    for df in frames:
        cols = {c.lower(): c for c in df.columns}
        for _, row in df.iterrows():
            try:
                pct_change = None
                for key in ("pctchange", "% change", "pct change"):
                    if key in cols:
                        pct_change = _safe(row[cols[key]])
                        break
                pct_held = None
                for key in ("pctheld", "% out", "% held", "pct held"):
                    if key in cols:
                        pct_held = _safe(row[cols[key]])
                        break
                if pct_held is not None and pct_held > 0:
                    weighted_deltas.append((pct_change or 0.0, pct_held))
                    if top_holder_name is None or (pct_held > (top_holder_pct_held or 0)):
                        for key in ("holder", "fund name"):
                            if key in cols:
                                top_holder_name = str(row[cols[key]])
                                top_holder_pct_held = pct_held
                                break
                # "New position" proxy — pct_change that's exactly 1.0 or None
                # often indicates newly reported position (quirk of yfinance).
                if pct_change is not None and pct_change >= 1.0:
                    new_count += 1
            except Exception:
                continue

    weighted_delta = None
    if weighted_deltas:
        total_weight = sum(w for _, w in weighted_deltas) or 1.0
        weighted_delta = sum(d * w for d, w in weighted_deltas) / total_weight

    return {
        "ticker": ticker.upper(),
        "as_of_quarter": _current_quarter_label(),
        "total_holders": int(total_holders),
        "weighted_pct_change_qoq": round(weighted_delta, 4) if weighted_delta is not None else None,
        "new_initiation_count": int(new_count),
        "top_holder_name": top_holder_name[:200] if top_holder_name else None,
        "top_holder_pct_held": _safe(top_holder_pct_held),
    }


def _upsert(row: Dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        r = db.query(InstitutionalHoldings).filter(
            InstitutionalHoldings.ticker == row["ticker"]
        ).first()
        if r is None:
            r = InstitutionalHoldings(ticker=row["ticker"])
            db.add(r)
        for k, v in row.items():
            if k != "ticker":
                setattr(r, k, v)
        r.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def refresh_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    row = _fetch_one(ticker)
    if row is None:
        return None
    _upsert(row)
    return get_holdings(ticker)


def refresh_all() -> Dict[str, Any]:
    db = SessionLocal()
    try:
        tickers = set(s.ticker for s in db.query(WatchlistStock).all())
        tickers |= set(r.ticker for r in db.query(CandidatePool).all())
    finally:
        db.close()
    tickers = sorted(tickers)
    if not tickers:
        return {"checked": 0, "total": 0}
    ok = 0
    for t in tickers:
        try:
            if refresh_ticker(t) is not None:
                ok += 1
        except Exception as e:
            logger.debug(f"institutional {t}: {e}")
    logger.info(f"institutional: refreshed {ok}/{len(tickers)} tickers")
    return {"checked": ok, "total": len(tickers)}


def get_holdings(ticker: str) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        r = db.query(InstitutionalHoldings).filter(
            InstitutionalHoldings.ticker == ticker.upper()
        ).first()
        if r is None:
            return None
        return {
            "ticker": r.ticker,
            "as_of_quarter": r.as_of_quarter,
            "total_holders": r.total_holders,
            "weighted_pct_change_qoq": r.weighted_pct_change_qoq,
            "new_initiation_count": r.new_initiation_count,
            "top_holder_name": r.top_holder_name,
            "top_holder_pct_held": r.top_holder_pct_held,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
    finally:
        db.close()


def institutional_multiplier(ticker: str, direction: str) -> float:
    """±3% envelope for institutional accumulation vs distribution.

    BUY: weighted_pct_change_qoq > +10% → 1.03 (net accumulation)
         between +3% and +10% → 1.015
         < -10% → 0.97 (net distribution)
    SELL: mirror.
    """
    r = get_holdings(ticker)
    if r is None or r.get("weighted_pct_change_qoq") is None:
        return _MULT_NEUTRAL
    delta = float(r["weighted_pct_change_qoq"])
    direction = (direction or "").upper()
    if direction == "BUY":
        if delta >= 0.10:  return _MULT_CONFIRM_STRONG
        if delta >= 0.03:  return _MULT_CONFIRM_MILD
        if delta <= -0.10: return _MULT_CONTRA_STRONG
        if delta <= -0.03: return _MULT_CONTRA_MILD
        return _MULT_NEUTRAL
    if direction == "SELL":
        if delta <= -0.10: return _MULT_CONFIRM_STRONG
        if delta <= -0.03: return _MULT_CONFIRM_MILD
        if delta >= 0.10:  return _MULT_CONTRA_STRONG
        if delta >= 0.03:  return _MULT_CONTRA_MILD
        return _MULT_NEUTRAL
    return _MULT_NEUTRAL


def institutional_reason_line(ticker: str, direction: str) -> Optional[str]:
    r = get_holdings(ticker)
    if r is None or r.get("weighted_pct_change_qoq") is None:
        return None
    mult = institutional_multiplier(ticker, direction)
    if mult == _MULT_NEUTRAL:
        return None
    delta = float(r["weighted_pct_change_qoq"])
    new_cnt = r.get("new_initiation_count") or 0
    mark = "🏦✅" if mult > _MULT_NEUTRAL else "🏦⚠️"
    sign = "accumulating" if delta > 0 else "distributing"
    new_bit = f", {new_cnt} new init" if new_cnt > 0 else ""
    return f"{mark} Institutions ({r.get('as_of_quarter', '?')}): weighted {delta*100:+.1f}% QoQ ({sign}){new_bit} — {'confirms' if mult > _MULT_NEUTRAL else 'contradicts'} {direction}"
