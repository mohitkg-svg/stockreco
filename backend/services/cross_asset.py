"""Cross-asset and regime signals — VIX term structure, credit divergence,
SKEW, sector rotation, currency, etc. r44 fix Wave 3.

All series fetch from Yahoo via the existing `data_fetcher.fetch_ohlcv`
infra. Cached 15 min in-process to avoid a feed storm. Each accessor
returns None on data unavailability — callers fail-open (no regime
adjustment) rather than blocking.
"""
from __future__ import annotations
import logging
import time
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# 15-min cache; regime signals don't need sub-second freshness.
_CACHE: Dict[str, Tuple[float, float]] = {}
_TTL_SEC = 900.0


def _cached(key: str, fetch_fn) -> Optional[float]:
    now = time.time()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _TTL_SEC:
        return cached[1]
    try:
        v = fetch_fn()
        if v is not None:
            _CACHE[key] = (now, float(v))
        return v
    except Exception as e:
        logger.debug(f"cross_asset {key} fetch failed: {e}")
        return None


def _last_close(ticker: str, tf: str = "1d") -> Optional[float]:
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(ticker, tf)
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


def _ratio(num_ticker: str, den_ticker: str) -> Optional[float]:
    n = _last_close(num_ticker)
    d = _last_close(den_ticker)
    if n is None or d is None or d == 0:
        return None
    return n / d


# ---------- VIX term structure ----------------------------------------------

def vix_term_ratio() -> Optional[float]:
    """Ratio VIX9D / VIX3M. >1.0 = backwardation = stress is now, not later.
    Empirically the cleanest "stress is *current*" signal in equity vol.
    """
    return _cached("vix_term", lambda: _ratio("^VIX9D", "^VIX3M"))


def vvix() -> Optional[float]:
    """Vol-of-vol (VVIX). >110 = vol is itself volatile (gamma-scalp regimes
    break). Used as an option-bucket multiplier modifier.
    """
    return _cached("vvix", lambda: _last_close("^VVIX"))


def skew_index() -> Optional[float]:
    """CBOE SKEW. >145 = put-buying mania (often precedes drops).
    """
    return _cached("skew", lambda: _last_close("^SKEW"))


# ---------- Credit + macro --------------------------------------------------

def hyg_spy_divergence_z() -> Optional[float]:
    """20-day-return z-score of HYG minus 20-day-return z-score of SPY.
    Negative = credit underperforming equity = risk-off precursor.
    """
    def _compute():
        from services.data_fetcher import fetch_ohlcv
        import numpy as _np
        hyg = fetch_ohlcv("HYG", "1d")
        spy = fetch_ohlcv("SPY", "1d")
        if hyg is None or spy is None or hyg.empty or spy.empty:
            return None
        hyg20 = float(hyg["Close"].iloc[-1] / hyg["Close"].iloc[-21] - 1) if len(hyg) >= 21 else None
        spy20 = float(spy["Close"].iloc[-1] / spy["Close"].iloc[-21] - 1) if len(spy) >= 21 else None
        if hyg20 is None or spy20 is None:
            return None
        # z-score the difference using a 60d window of the rolling diff
        try:
            hyg_rets = hyg["Close"].pct_change(20).dropna().tail(60)
            spy_rets = spy["Close"].pct_change(20).dropna().tail(60)
            diff_series = (hyg_rets - spy_rets).dropna()
            if len(diff_series) < 30:
                return None
            mu = float(diff_series.mean())
            sigma = float(diff_series.std()) or 1e-9
            z = (hyg20 - spy20 - mu) / sigma
            return z
        except Exception:
            return None
    return _cached("hyg_spy_div", _compute)


def yield_curve_2s10s() -> Optional[float]:
    """10Y - 2Y in percentage points. Negative = inverted (recession signal).
    Yahoo uses ^TNX (10Y) and ^IRX (3M); for 2Y use ^FVX as proxy (5Y) or
    skip if unavailable.
    """
    def _compute():
        ten = _last_close("^TNX")
        five = _last_close("^FVX")
        if ten is None or five is None:
            return None
        return ten - five
    return _cached("yc_5s10s", _compute)


# ---------- Sector / breadth ------------------------------------------------

def defensive_vs_cyclical_score() -> Optional[float]:
    """20-day return of defensive sectors mean (XLP, XLU, XLV) MINUS
    cyclical sectors mean (XLY, XLF, XLE). Positive = late-cycle / risk-off.
    """
    def _compute():
        from services.data_fetcher import fetch_ohlcv
        defs, cycs = [], []
        for sym, bucket in [("XLP", defs), ("XLU", defs), ("XLV", defs),
                             ("XLY", cycs), ("XLF", cycs), ("XLE", cycs)]:
            df = fetch_ohlcv(sym, "1d")
            if df is None or df.empty or len(df) < 21:
                continue
            r = float(df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1)
            bucket.append(r)
        if not defs or not cycs:
            return None
        return (sum(defs) / len(defs)) - (sum(cycs) / len(cycs))
    return _cached("def_vs_cyc", _compute)


def iwm_spy_relative_strength() -> Optional[float]:
    """20-day return of IWM minus 20-day return of SPY. Positive = small-cap
    leadership (risk-on). Negative = leadership narrowing.
    """
    def _compute():
        from services.data_fetcher import fetch_ohlcv
        iwm = fetch_ohlcv("IWM", "1d")
        spy = fetch_ohlcv("SPY", "1d")
        if iwm is None or spy is None or iwm.empty or spy.empty or len(iwm) < 21 or len(spy) < 21:
            return None
        iwm20 = float(iwm["Close"].iloc[-1] / iwm["Close"].iloc[-21] - 1)
        spy20 = float(spy["Close"].iloc[-1] / spy["Close"].iloc[-21] - 1)
        return iwm20 - spy20
    return _cached("iwm_spy_rs", _compute)


def breadth_proxy_rsp_spy() -> Optional[float]:
    """Equal-weight RSP vs cap-weight SPY 20d-return diff. Negative =
    narrowing leadership (cap-weight outperforming = mega-cap-driven rally).
    """
    def _compute():
        from services.data_fetcher import fetch_ohlcv
        rsp = fetch_ohlcv("RSP", "1d")
        spy = fetch_ohlcv("SPY", "1d")
        if rsp is None or spy is None or rsp.empty or spy.empty or len(rsp) < 21 or len(spy) < 21:
            return None
        rsp20 = float(rsp["Close"].iloc[-1] / rsp["Close"].iloc[-21] - 1)
        spy20 = float(spy["Close"].iloc[-1] / spy["Close"].iloc[-21] - 1)
        return rsp20 - spy20
    return _cached("rsp_spy_breadth", _compute)


# ---------- Composite regime score ------------------------------------------

_SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]


def sector_rotation_score(sector_etf: str) -> Optional[float]:
    """r44 Wave 7 — sector rotation tilt. Rank `sector_etf` by 126-day
    return against all S&P sector ETFs. Top-3 → +0.08, bottom-3 → -0.08,
    else 0. Drives a sector_confidence_multiplier overlay.
    """
    def _compute():
        from services.data_fetcher import fetch_ohlcv
        rets: Dict[str, float] = {}
        for sym in _SECTOR_ETFS:
            df = fetch_ohlcv(sym, "1d")
            if df is None or df.empty or len(df) < 127:
                continue
            r = float(df["Close"].iloc[-1] / df["Close"].iloc[-127] - 1)
            rets[sym] = r
        if sector_etf not in rets or len(rets) < 6:
            return None
        sorted_syms = sorted(rets.keys(), key=lambda s: rets[s], reverse=True)
        rank = sorted_syms.index(sector_etf)
        if rank < 3:
            return 0.08
        if rank >= len(sorted_syms) - 3:
            return -0.08
        return 0.0
    return _cached(f"sec_rot_{sector_etf}", _compute)


def regime_score() -> Dict[str, Optional[float]]:
    """Return a dict of all regime indicators for inspection. The composite
    `score` field is a weighted combination in [-1, +1]; +1 = strongly
    risk-on, -1 = strongly risk-off.
    """
    out = {
        "vix_term": vix_term_ratio(),
        "vvix": vvix(),
        "skew": skew_index(),
        "hyg_spy": hyg_spy_divergence_z(),
        "yc_5s10s": yield_curve_2s10s(),
        "def_vs_cyc": defensive_vs_cyclical_score(),
        "iwm_spy": iwm_spy_relative_strength(),
        "breadth": breadth_proxy_rsp_spy(),
    }
    score = 0.0
    n = 0
    if out["vix_term"] is not None:
        # < 0.95 contango = risk-on (+); > 1.0 backwardation = risk-off (-)
        v = out["vix_term"]
        if v < 0.95: score += 0.3; n += 1
        elif v > 1.0: score -= 0.4; n += 1
        else: score += 0.0; n += 1
    if out["hyg_spy"] is not None:
        # Negative z-score = credit lagging = risk-off
        score -= max(-1.0, min(1.0, -out["hyg_spy"] * 0.3))
        n += 1
    if out["skew"] is not None:
        # SKEW > 145 = put-buying = risk-off
        if out["skew"] > 145: score -= 0.2
        n += 1
    if out["def_vs_cyc"] is not None:
        # Defensive outperforming = risk-off
        score -= max(-1.0, min(1.0, out["def_vs_cyc"] * 5))
        n += 1
    if out["iwm_spy"] is not None:
        score += max(-1.0, min(1.0, out["iwm_spy"] * 5))
        n += 1
    if out["breadth"] is not None:
        score += max(-1.0, min(1.0, out["breadth"] * 5))
        n += 1
    out["score"] = float(max(-1.0, min(1.0, score / max(1, n)))) if n > 0 else None
    return out


def regime_multiplier() -> float:
    """Convert composite regime score to a sizing multiplier. Clamped
    [0.6, 1.2] so the regime layer stacks safely with the per-trade
    multiplier ceiling.
    """
    rs = regime_score().get("score")
    if rs is None:
        return 1.0
    return float(max(0.6, min(1.2, 1.0 + rs * 0.2)))
