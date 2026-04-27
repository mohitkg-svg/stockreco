"""r46 Tier 1: per-ticker profile accessors with cached reads + global
config fallback. Every accessor returns a sensible default when no row
exists or DB lookup fails — so the call sites never have to handle a
None case at the cost of forking their logic.
"""
from __future__ import annotations
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 5-min in-process cache. Per-ticker overrides change at most weekly
# (alongside best-strategy recompute) so the TTL is generous.
_CACHE: Dict[str, tuple] = {}
_TTL_SEC = 300.0


def _get_row(ticker: str) -> Optional[Dict[str, Any]]:
    if not ticker:
        return None
    key = ticker.upper()
    now = time.time()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _TTL_SEC:
        return cached[1]
    try:
        from database import SessionLocal, TickerProfile
        db = SessionLocal()
        try:
            row = db.query(TickerProfile).filter(TickerProfile.ticker == key).first()
            if row is None:
                _CACHE[key] = (now, None)
                return None
            d = {
                "realized_vol_30d": row.realized_vol_30d,
                "vol_mult": row.vol_mult or 1.0,
                "beta_60d_realized": row.beta_60d_realized,
                "confidence_threshold_override": row.confidence_threshold_override,
                "median_chain_spread_pct": row.median_chain_spread_pct,
                "min_rr_override": row.min_rr_override,
                "min_dte_override": row.min_dte_override,
                "trend_persistence_score": row.trend_persistence_score,
                "chandelier_mult_override": row.chandelier_mult_override,
                "has_earnings_calendar": (row.has_earnings_calendar
                                            if row.has_earnings_calendar is not None
                                            else True),
                "correlation_cluster_id": row.correlation_cluster_id,
                "news_count_p50_30d": row.news_count_p50_30d,
                "median_winning_hold_bars": row.median_winning_hold_bars,
            }
            _CACHE[key] = (now, d)
            return d
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"ticker_profile {ticker}: {e}")
        return None


def vol_mult(ticker: str, default: float = 1.0) -> float:
    """Per-ticker ATR-multiplier scaler. Clamped [0.7, 1.6] in the seeder."""
    r = _get_row(ticker)
    if r and r.get("vol_mult"):
        return float(r["vol_mult"])
    return float(default)


def confidence_threshold(ticker: str, default: float) -> float:
    """Per-ticker confidence threshold override; falls back to global config."""
    r = _get_row(ticker)
    if r and r.get("confidence_threshold_override") is not None:
        return float(r["confidence_threshold_override"])
    return float(default)


def min_rr(ticker: str, default: float = 2.0) -> float:
    r = _get_row(ticker)
    if r and r.get("min_rr_override") is not None:
        return float(r["min_rr_override"])
    return float(default)


def min_dte(ticker: str, default: int = 10) -> int:
    r = _get_row(ticker)
    if r and r.get("min_dte_override") is not None:
        return int(r["min_dte_override"])
    return int(default)


def chandelier_mult(ticker: str, default: float) -> float:
    r = _get_row(ticker)
    if r and r.get("chandelier_mult_override") is not None:
        return float(r["chandelier_mult_override"])
    return float(default)


def has_earnings(ticker: str) -> bool:
    """If we know a ticker has no earnings calendar (e.g. ETF, commodity),
    consider_signal can short-circuit the earnings lookup."""
    r = _get_row(ticker)
    if r:
        return bool(r.get("has_earnings_calendar", True))
    return True


def beta(ticker: str, default: float = 1.0) -> float:
    """60-day realized beta vs SPY; preferred over yfinance's stale 5y beta."""
    r = _get_row(ticker)
    if r and r.get("beta_60d_realized") is not None:
        return float(r["beta_60d_realized"])
    return float(default)


def news_count_baseline(ticker: str, default: float = 5.0) -> float:
    r = _get_row(ticker)
    if r and r.get("news_count_p50_30d") is not None:
        return float(r["news_count_p50_30d"])
    return float(default)


def correlation_cluster_id(ticker: str) -> Optional[str]:
    r = _get_row(ticker)
    return r.get("correlation_cluster_id") if r else None


def upsert(ticker: str, **fields) -> None:
    """Write helper used by the recompute job. Invalidates the cache."""
    if not ticker:
        return
    try:
        from database import SessionLocal, TickerProfile
        from datetime import datetime
        db = SessionLocal()
        try:
            row = db.query(TickerProfile).filter(TickerProfile.ticker == ticker.upper()).first()
            if row is None:
                row = TickerProfile(ticker=ticker.upper())
                db.add(row)
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            row.updated_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()
        _CACHE.pop(ticker.upper(), None)
    except Exception as e:
        logger.debug(f"ticker_profile.upsert {ticker}: {e}")


def recompute_all_profiles(tickers: Optional[list] = None) -> Dict[str, int]:
    """Weekly job: refresh realized vol, beta, news baseline for known
    tickers. Cheap; reuses the same OHLCV fetch the best-strategy job
    already does.
    """
    from database import SessionLocal, WatchlistStock, CandidatePool
    if tickers is None:
        db = SessionLocal()
        try:
            t = set(s.ticker for s in db.query(WatchlistStock).all())
            t |= set(r.ticker for r in db.query(CandidatePool).all())
        finally:
            db.close()
        tickers = sorted(t)
    n = 0
    for tk in tickers:
        try:
            from services.data_fetcher import fetch_ohlcv
            import numpy as _np
            df = fetch_ohlcv(tk, "1d")
            if df is None or df.empty or len(df) < 65:
                continue
            closes = df["Close"].astype(float)
            rets = _np.log(closes / closes.shift(1)).dropna()
            rv30 = float(rets.tail(30).std()) if len(rets) >= 30 else None
            # 60-day beta vs SPY
            beta_v = None
            try:
                spy_df = fetch_ohlcv("SPY", "1d")
                if spy_df is not None and not spy_df.empty:
                    spy_rets = _np.log(spy_df["Close"].astype(float) / spy_df["Close"].astype(float).shift(1)).dropna()
                    joined = rets.tail(60).to_frame("a").join(spy_rets.tail(60).to_frame("b"), how="inner")
                    if len(joined) >= 30:
                        cov = float(joined["a"].cov(joined["b"]))
                        var = float(joined["b"].var())
                        if var > 0:
                            beta_v = cov / var
            except Exception:
                pass
            # vol_mult: ratio of this ticker's RV30 vs the universe median (clamped)
            vm = 1.0
            try:
                if rv30:
                    universe_median_rv = 0.018  # rough SPX/QQQ benchmark; refined elsewhere
                    raw = rv30 / universe_median_rv
                    vm = float(max(0.7, min(1.6, raw)))
            except Exception:
                pass
            upsert(tk,
                   realized_vol_30d=rv30,
                   vol_mult=vm,
                   beta_60d_realized=beta_v)
            n += 1
        except Exception as e:
            logger.debug(f"recompute_all_profiles {tk}: {e}")
    logger.info(f"ticker_profile: refreshed {n} tickers")
    return {"refreshed": n}
