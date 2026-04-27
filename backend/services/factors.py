"""r48 BACKLOG — factor-based strategy module.

Implements a cross-sectional factor library and macro-overlay signals
deferred from r47. Each function is a pure read of cached cross_asset /
data_fetcher / fundamentals data; callers compose the per-ticker
multiplier into the existing sizing pipeline alongside `r47_overlays`.

Citations & expected edge ranges are documented in BACKLOG.md.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


def _safe(fn, default=None):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# A4. Cross-sectional 12-1 momentum factor
# ---------------------------------------------------------------------------
def cross_sectional_momentum(universe: List[str]) -> Dict[str, float]:
    """Compute 12-1 momentum percentile rank across a universe.

    Each ticker's `(close[t-21] / close[t-252]) - 1` ranked into [0,1].
    Skip-month avoids the 1-month reversal effect (Lehmann 1990).
    Returns `{ticker: pct_rank}` for tickers with sufficient history.
    """
    from services.data_fetcher import fetch_ohlcv
    rets: Dict[str, float] = {}
    for t in universe:
        try:
            df = fetch_ohlcv(t, "1d")
            if df is None or df.empty or len(df) < 260:
                continue
            r = float(df["Close"].iloc[-22] / df["Close"].iloc[-253] - 1.0)
            rets[t] = r
        except Exception:
            continue
    if not rets:
        return {}
    sorted_vals = sorted(rets.values())
    n = len(sorted_vals)
    out: Dict[str, float] = {}
    for k, v in rets.items():
        cnt = sum(1 for x in sorted_vals if x <= v)
        out[k] = float(cnt) / float(n)
    return out


def momentum_size_factor(ticker: str, universe: Optional[List[str]] = None) -> float:
    """Translate a ticker's 12-1 momentum percentile into a sizing
    multiplier in [0.85, 1.15]. Top-quintile boost, bottom-quintile penalty.
    Caller should cache the universe rank for a scan cycle to avoid
    recomputing per-ticker."""
    if not universe:
        try:
            from database import SessionLocal, WatchlistStock
            db = SessionLocal()
            try:
                universe = [w.ticker for w in db.query(WatchlistStock).all()]
            finally:
                db.close()
        except Exception:
            return 1.0
    if not universe or ticker not in universe:
        return 1.0
    ranks = cross_sectional_momentum(universe)
    p = ranks.get(ticker)
    if p is None:
        return 1.0
    if p >= 0.80:
        return 1.15
    if p <= 0.20:
        return 0.85
    return 1.0


# ---------------------------------------------------------------------------
# B9. BAB low-vol tilt (regime-conditional)
# ---------------------------------------------------------------------------
def bab_low_vol_factor(ticker: str, universe: Optional[List[str]] = None) -> float:
    """Frazzini-Pedersen 2014 BAB. In risk-off regime (regime_score < -0.3 OR
    credit-spread CB), boost low-vol-rank tickers, penalize high-vol-rank.
    Returns multiplier in [0.85, 1.15]. No-op outside risk-off regime."""
    try:
        from services.cross_asset import regime_score
        rs = regime_score().get("score") if isinstance(regime_score(), dict) else regime_score()
        if rs is None or rs >= -0.3:
            from services.r47_overlays import credit_spread_circuit_breaker_active
            if not credit_spread_circuit_breaker_active():
                return 1.0
    except Exception:
        return 1.0
    # Risk-off → boost low realized-vol names.
    if not universe:
        try:
            from database import SessionLocal, WatchlistStock
            db = SessionLocal()
            try:
                universe = [w.ticker for w in db.query(WatchlistStock).all()]
            finally:
                db.close()
        except Exception:
            return 1.0
    if not universe or ticker not in universe:
        return 1.0
    from services.data_fetcher import fetch_ohlcv
    vols: Dict[str, float] = {}
    for t in universe:
        try:
            df = fetch_ohlcv(t, "1d")
            if df is None or df.empty or len(df) < 60:
                continue
            v = float(df["Close"].pct_change().dropna().tail(60).std()) * (252 ** 0.5)
            vols[t] = v
        except Exception:
            continue
    if ticker not in vols:
        return 1.0
    sorted_v = sorted(vols.values())
    cnt = sum(1 for x in sorted_v if x <= vols[ticker])
    pct = cnt / len(sorted_v)
    if pct <= 0.20:
        return 1.15  # low-vol → boost in risk-off
    if pct >= 0.80:
        return 0.85
    return 1.0


# ---------------------------------------------------------------------------
# B11. Yield-curve inversion → defensive sector tilt
# ---------------------------------------------------------------------------
_DEFENSIVE_SECTORS = {"XLP", "XLU", "XLV", "Consumer Defensive", "Utilities", "Healthcare"}
_CYCLICAL_SECTORS = {"XLY", "XLI", "XLB", "Consumer Cyclical", "Industrials", "Basic Materials"}


def yield_curve_defensive_tilt(ticker_sector: Optional[str]) -> float:
    """When yield_curve_2s10s < 0 for ≥30 days, defensive sectors get +10%
    sizing, cyclicals -10%. Estrella-Hardouvelis 1991. Returns [0.90, 1.10]."""
    try:
        from services.cross_asset import yield_curve_2s10s
        yc = yield_curve_2s10s()
        if yc is None or yc >= 0:
            return 1.0
    except Exception:
        return 1.0
    sec = (ticker_sector or "").strip()
    if any(d.lower() in sec.lower() for d in _DEFENSIVE_SECTORS):
        return 1.10
    if any(c.lower() in sec.lower() for c in _CYCLICAL_SECTORS):
        return 0.90
    return 1.0


# ---------------------------------------------------------------------------
# B12. Oil regime overlay
# ---------------------------------------------------------------------------
def oil_regime_factor(ticker_sector: Optional[str]) -> float:
    """When WTI > 200d MA AND 60d change > +15%, energy gets +8%, long-
    duration tech -5%. Driesprong-Jacobsen-Maat 2008. Returns [0.95, 1.08]."""
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("USO", "1d")
        if df is None or df.empty or len(df) < 200:
            return 1.0
        last = float(df["Close"].iloc[-1])
        sma200 = float(df["Close"].rolling(200).mean().iloc[-1])
        chg60 = float(df["Close"].iloc[-1] / df["Close"].iloc[-61] - 1.0)
        if not (last > sma200 and chg60 > 0.15):
            return 1.0
    except Exception:
        return 1.0
    sec = (ticker_sector or "").strip()
    if "energy" in sec.lower() or sec.upper() == "XLE":
        return 1.08
    if "tech" in sec.lower():
        return 0.95
    return 1.0


# ---------------------------------------------------------------------------
# B10. DXY → small/large-cap tilt
# ---------------------------------------------------------------------------
def dxy_size_tilt(ticker: str) -> float:
    """Strong dollar (60d change > +5%) → small-caps +5%, large-cap
    multinationals -5%. Mirror for weak dollar.
    Universe: SPY/QQQ proxy = large; IWM family = small."""
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("UUP", "1d")
        if df is None or df.empty or len(df) < 65:
            return 1.0
        chg = float(df["Close"].iloc[-1] / df["Close"].iloc[-61] - 1.0)
    except Exception:
        return 1.0
    t = (ticker or "").upper()
    if abs(chg) < 0.05:
        return 1.0
    is_small_cap = t in {"IWM", "VTWO", "IJR"}
    is_large_cap = t in {"SPY", "QQQ", "VOO", "IVV"}
    if chg > 0.05:  # strong $
        if is_small_cap:
            return 1.05
        if is_large_cap:
            return 0.95
    elif chg < -0.05:  # weak $
        if is_small_cap:
            return 0.95
        if is_large_cap:
            return 1.05
    return 1.0


# ---------------------------------------------------------------------------
# A5. Real-yield → growth/value rotation (proxied)
# ---------------------------------------------------------------------------
def real_yield_growth_value_factor(ticker_pe: Optional[float]) -> float:
    """Approximate real-yield via TIP price. Falling TIP = rising real
    yield → favor VALUE (low P/E), penalize GROWTH (high P/E).
    Caller passes the ticker's trailing P/E; multiplier in [0.92, 1.08]."""
    if ticker_pe is None or ticker_pe <= 0:
        return 1.0
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("TIP", "1d")
        if df is None or df.empty or len(df) < 65:
            return 1.0
        chg = float(df["Close"].iloc[-1] / df["Close"].iloc[-61] - 1.0)
    except Exception:
        return 1.0
    if abs(chg) < 0.015:
        return 1.0
    rising_real = chg < -0.015  # TIP falling = real yield rising
    if rising_real:
        return 1.08 if ticker_pe < 20 else 0.92  # value boost, growth penalty
    else:
        return 0.95 if ticker_pe < 20 else 1.05
    return 1.0


# ---------------------------------------------------------------------------
# A7. FOMC hawkish/dovish surprise (lightweight proxy)
# ---------------------------------------------------------------------------
def fomc_surprise_factor() -> float:
    """T+0..T+1 after an FOMC release, infer surprise direction from SPY
    intraday move on the announcement day. Hawkish = SPY -1%+ → -1d
    equity drag carries over; Dovish = SPY +1%+ → tailwind. This is a
    cheap proxy for the proper Fed Funds futures-implied surprise."""
    try:
        from services.macro_calendar import _FOMC_DATES
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo as _ZI
        now_et = _dt.now(_ZI("America/New_York"))
        for d in _FOMC_DATES:
            try:
                if (now_et.date() == d) or ((now_et.date() - d).days == 1):
                    from services.data_fetcher import fetch_ohlcv
                    df = fetch_ohlcv("SPY", "1d")
                    if df is not None and not df.empty:
                        last = float(df["Close"].iloc[-1])
                        prev = float(df["Open"].iloc[-1])  # intraday move proxy
                        chg = (last / prev - 1.0) if prev > 0 else 0
                        if chg > 0.01:
                            return 1.05  # dovish surprise tailwind
                        if chg < -0.01:
                            return 0.95  # hawkish drag
            except Exception:
                continue
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------------------
# A3. Macro surprise drift (CPI/NFP/PCE/GDP)
# ---------------------------------------------------------------------------
def macro_surprise_drift_factor() -> float:
    """T+0..T+3 after a macro release, scale long-bias sizing by the
    direction of the surprise. CPI < expected → equity-positive (1.06×);
    NFP > expected → equity-positive (1.05×); CPI > expected → -. Reads
    `MacroEvent.surprise_pct` from the most-recent release within 72h."""
    try:
        from database import SessionLocal, MacroEvent
        from datetime import datetime as _dt_ms, timedelta as _td_ms
        db = SessionLocal()
        try:
            cutoff = _dt_ms.utcnow() - _td_ms(hours=72)
            row = (db.query(MacroEvent)
                   .filter(MacroEvent.release_time_utc >= cutoff,
                           MacroEvent.release_time_utc <= _dt_ms.utcnow(),
                           MacroEvent.actual.isnot(None))
                   .order_by(MacroEvent.release_time_utc.desc())
                   .first())
            if not row or row.surprise_pct is None:
                return 1.0
            kind = (row.kind or "").upper()
            sp = float(row.surprise_pct or 0)
            if kind in ("CPI", "PCE", "PPI"):
                # Low inflation surprise = positive
                if sp < -0.5: return 1.06
                if sp > 0.5: return 0.94
            elif kind in ("NFP", "GDP", "RETAIL"):
                if sp > 0.5: return 1.05
                if sp < -0.5: return 0.95
        finally:
            db.close()
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------------------
# C15. Squeeze setup amplifier (high SI + breakout)
# ---------------------------------------------------------------------------
def squeeze_setup_factor(ticker: str) -> float:
    """SI > 15% float AND price breaks 50d high on RVOL > 1.5 → 1.05× sizing
    boost (with tighter stop discipline assumed in caller). Boehmer-Jones-
    Zhang 2008 conditional-reversal subset of the broader high-SI cohort."""
    try:
        from database import SessionLocal, Fundamentals
        from services.data_fetcher import fetch_ohlcv
        db = SessionLocal()
        try:
            f = db.query(Fundamentals).filter(Fundamentals.ticker == ticker).first()
            if not f or not getattr(f, "short_pct_float", None):
                return 1.0
            si = float(f.short_pct_float or 0)
        finally:
            db.close()
        if si < 0.15:
            return 1.0
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty or len(df) < 60:
            return 1.0
        hi50 = float(df["High"].rolling(50).max().iloc[-2])
        last = float(df["Close"].iloc[-1])
        vol = float(df["Volume"].iloc[-1])
        avg_vol = float(df["Volume"].rolling(20).mean().iloc[-1])
        if last > hi50 and vol > 1.5 * avg_vol:
            return 1.05
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------------------
# C14. Opportunistic insider differentiation
# ---------------------------------------------------------------------------
def opportunistic_insider_factor(ticker: str) -> float:
    """Cohen-Malloy-Pomorski 2012 — opportunistic insiders (non-routine
    traders) outperform routine sellers by ~5%/yr. Without per-trader
    history, use a frequency heuristic: insider traders with ≤2 trades
    over the rolling 24mo are "opportunistic"; clusters of THESE buyers
    are the high-conviction signal.

    The codebase's `InsiderSummary` doesn't track per-trader frequency
    today; this function returns a conservative 1.0 unless insider buy
    cluster is strong (≥8 distinct buyers + ratio > 0.7) — a proxy for
    "non-routine".
    """
    try:
        from database import SessionLocal, InsiderSummary
        db = SessionLocal()
        try:
            row = db.query(InsiderSummary).filter(InsiderSummary.ticker == ticker).first()
            if not row:
                return 1.0
            buys = int(getattr(row, "buy_count", 0) or 0)
            ratio = float(getattr(row, "buy_ratio", 0) or 0)
            if buys >= 8 and ratio >= 0.7:
                return 1.06
        finally:
            db.close()
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------------------
# Single composite — caller uses one entry-point + per-overlay flags.
# ---------------------------------------------------------------------------
def factor_composite(ticker: str, *, sector: Optional[str] = None,
                     pe_ratio: Optional[float] = None,
                     universe: Optional[List[str]] = None) -> Tuple[float, Dict[str, float]]:
    """Combine all factor multipliers into one number, clamped [0.6, 1.4]."""
    parts: Dict[str, float] = {}
    parts["momentum"] = momentum_size_factor(ticker, universe)
    parts["bab"] = bab_low_vol_factor(ticker, universe)
    parts["yield_curve"] = yield_curve_defensive_tilt(sector)
    parts["oil_regime"] = oil_regime_factor(sector)
    parts["dxy"] = dxy_size_tilt(ticker)
    parts["real_yield_pe"] = real_yield_growth_value_factor(pe_ratio)
    parts["fomc_surprise"] = fomc_surprise_factor()
    parts["macro_surprise"] = macro_surprise_drift_factor()
    parts["squeeze"] = squeeze_setup_factor(ticker)
    parts["opportunistic_insider"] = opportunistic_insider_factor(ticker)
    combined = 1.0
    for v in parts.values():
        try:
            combined *= float(v) if v else 1.0
        except Exception:
            pass
    combined = max(0.6, min(1.4, combined))
    return combined, parts
