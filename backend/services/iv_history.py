"""r52f: ATM-IV nightly capture.

Pulls the closest-to-30d and closest-to-60d ATM IV per watchlist ticker
and writes a daily snapshot to the `iv_history` table. The IV-percentile
option-entry gate (deferred since r41) reads back from this table once
≥252 trading days of history have accumulated per ticker.

Source: yfinance Ticker.option_chain(expiry). For each ticker we pick:
  * The expiry closest to today + 30 calendar days
  * The expiry closest to today + 60 calendar days
At each expiry we average the implied_volatility on the call + put with
strike closest to the underlying's last close — that's the standard ATM
straddle IV. Free, requires no extra data subscription.

Cadence: nightly 04:30 UTC (after option markets close, before
universe_scan kicks off pre-market). Idempotent — UNIQUE(ticker, ts)
upsert on the date bucket.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _atm_iv_for_target_dte(yf_ticker, underlying_close: float, target_days: int) -> Optional[float]:
    """Return ATM (call+put avg) IV for the expiry closest to target_days
    from now. Returns None if no expiry within ±15 days of target or
    yfinance returns empty."""
    try:
        expiries = list(yf_ticker.options or [])
    except Exception:
        return None
    if not expiries:
        return None
    today = datetime.utcnow().date()
    target_date = today + timedelta(days=target_days)
    # Pick closest expiry to target
    best_exp = None
    best_diff = 9999
    for e_str in expiries:
        try:
            e_d = datetime.strptime(e_str, "%Y-%m-%d").date()
            diff = abs((e_d - target_date).days)
            if diff < best_diff:
                best_diff = diff
                best_exp = e_str
        except Exception:
            continue
    if best_exp is None or best_diff > 15:
        return None
    try:
        chain = yf_ticker.option_chain(best_exp)
    except Exception:
        return None
    iv_calls = None
    iv_puts = None
    try:
        calls = chain.calls
        if calls is not None and not calls.empty:
            calls = calls.copy()
            calls["abs_diff"] = (calls["strike"] - underlying_close).abs()
            row = calls.sort_values("abs_diff").iloc[0]
            iv = row.get("impliedVolatility")
            if iv and iv > 0:
                iv_calls = float(iv)
    except Exception:
        pass
    try:
        puts = chain.puts
        if puts is not None and not puts.empty:
            puts = puts.copy()
            puts["abs_diff"] = (puts["strike"] - underlying_close).abs()
            row = puts.sort_values("abs_diff").iloc[0]
            iv = row.get("impliedVolatility")
            if iv and iv > 0:
                iv_puts = float(iv)
    except Exception:
        pass
    if iv_calls is None and iv_puts is None:
        return None
    if iv_calls is None:
        return iv_puts
    if iv_puts is None:
        return iv_calls
    return (iv_calls + iv_puts) / 2.0


def capture_one(ticker: str) -> Optional[Tuple[float, float, Optional[float]]]:
    """Capture ATM IV30 / IV60 for one ticker. Returns
    (iv30, iv60, underlying_close) or None on failure."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        h = t.history(period="5d", interval="1d")
        if h is None or h.empty:
            return None
        close = float(h["Close"].iloc[-1])
        iv30 = _atm_iv_for_target_dte(t, close, 30)
        iv60 = _atm_iv_for_target_dte(t, close, 60)
        if iv30 is None and iv60 is None:
            return None
        return (iv30, iv60, close)
    except Exception as e:
        logger.debug(f"iv_history.capture_one {ticker}: {e}")
        return None


def capture_all_watchlist() -> dict:
    """Cron entry point — capture ATM IV for every watchlist ticker.
    Idempotent: UNIQUE(ticker, ts_date) upsert via merge.
    """
    from database import SessionLocal, WatchlistStock, IVHistory
    db = SessionLocal()
    captured = 0
    skipped = 0
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        tickers = [s.ticker for s in db.query(WatchlistStock).all()]
        for ticker in tickers:
            try:
                # Skip if today's row already exists (idempotent)
                existing = (db.query(IVHistory)
                            .filter(IVHistory.ticker == ticker)
                            .filter(IVHistory.ts == today)
                            .first())
                if existing is not None:
                    skipped += 1
                    continue
                result = capture_one(ticker)
                if result is None:
                    skipped += 1
                    continue
                iv30, iv60, close = result
                term_skew = None
                if iv30 and iv60 and iv30 > 0:
                    term_skew = (iv60 / iv30) - 1.0
                db.add(IVHistory(
                    ticker=ticker, ts=today,
                    atm_iv30=iv30, atm_iv60=iv60,
                    term_iv_skew=term_skew,
                    underlying_close=close,
                ))
                db.commit()
                captured += 1
            except Exception as e:
                logger.debug(f"iv_history.capture_all_watchlist {ticker}: {e}")
                db.rollback()
                skipped += 1
        logger.info(f"iv_history: captured {captured}, skipped {skipped}")
        return {"captured": captured, "skipped": skipped, "total": len(tickers)}
    finally:
        db.close()


def iv_percentile(ticker: str, lookback_days: int = 252) -> Optional[float]:
    """Read-side: 0-100 percentile rank of today's IV30 within the
    rolling lookback window. Returns None if <30 data points (not enough
    history for a meaningful rank — fail-open per the gate's policy).
    """
    from database import SessionLocal, IVHistory
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=lookback_days)
        rows = (db.query(IVHistory)
                .filter(IVHistory.ticker == ticker)
                .filter(IVHistory.ts >= cutoff)
                .filter(IVHistory.atm_iv30.isnot(None))
                .order_by(IVHistory.ts.desc())
                .all())
        if len(rows) < 30:
            return None
        latest = rows[0].atm_iv30
        history = [r.atm_iv30 for r in rows[1:] if r.atm_iv30 is not None]
        if not history or latest is None:
            return None
        below = sum(1 for h in history if h < latest)
        return round((below / len(history)) * 100.0, 1)
    finally:
        db.close()
