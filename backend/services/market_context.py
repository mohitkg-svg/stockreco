"""
Market-context signals: VIX, sector momentum, and breadth.

Used by:
  • auto_trader (VIX → position-size multiplier; breadth → entry penalty)
  • signal_generator (sector → confidence tilt)

All values are cached for 15 minutes to avoid hammering Yahoo/Alpaca on
every signal eval. All accessor functions return a safe default when the
data fetch fails — the trading system stays alive rather than halting on
a transient upstream hiccup.
"""
from __future__ import annotations
import logging
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_TTL = 900  # 15 minutes
_cache: Dict[str, Tuple[float, float]] = {}   # key -> (value, expiry_ts)

# Cboe S&P 500 VIX — the canonical fear index.
_VIX_SYMBOL = "^VIX"
# SPDR Select Sector ETFs — canonical sector proxies.
_SECTOR_ETFS: Dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Health Care": "XLV",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Industrial": "XLI",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
    "Communication Services": "XLC",
}


def _cached(key: str, fetch, ttl: float = _TTL) -> Optional[float]:
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[1]:
        return hit[0] if hit[0] is not None else None
    try:
        v = fetch()
        if v is not None:
            _cache[key] = (float(v), now + ttl)
            return float(v)
    except Exception as e:
        logger.debug(f"market_context {key} fetch failed: {e}")
    # Cache None for half TTL so transient failures don't spam fetches
    _cache[key] = (None, now + ttl / 2)
    return None


# ------------------------------------------------------------------
# VIX — volatility regime
# ------------------------------------------------------------------
def current_vix() -> Optional[float]:
    """Return the most recent VIX close, cached."""
    def _fetch():
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(_VIX_SYMBOL, "1d")
        if df is None or df.empty:
            return None
        return float(df["Close"].iloc[-1])
    return _cached("vix", _fetch)


def vix_sizing_multiplier() -> float:
    """
    Multiplier on risk-per-trade budget based on VIX:
      VIX < 15   → 1.00  (calm, full size)
      15 ≤ < 20  → 0.90
      20 ≤ < 25  → 0.75
      25 ≤ < 35  → 0.55
      ≥ 35       → 0.40  (panic / crisis — risk-off)
    Unknown VIX defaults to 1.0 (fail-open).
    """
    vix = current_vix()
    if vix is None or vix <= 0:
        return 1.0
    if vix < 15:
        return 1.00
    if vix < 20:
        return 0.90
    if vix < 25:
        return 0.75
    if vix < 35:
        return 0.55
    return 0.40


# ------------------------------------------------------------------
# Sector momentum — 5-day return per sector ETF
# ------------------------------------------------------------------
def sector_momentum(sector: str) -> Optional[float]:
    """
    5-day return (decimal, e.g. 0.025 = +2.5%) of the sector ETF that best
    corresponds to `sector`. None when unknown/unavailable.
    """
    if not sector:
        return None
    etf = _SECTOR_ETFS.get(sector.strip())
    if not etf:
        return None
    key = f"sector_{etf}"

    def _fetch():
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(etf, "1d")
        if df is None or df.empty or len(df) < 6:
            return None
        closes = df["Close"].astype(float)
        return float((closes.iloc[-1] / closes.iloc[-6]) - 1.0)
    return _cached(key, _fetch)


def sector_confidence_multiplier(sector: str, direction: str = "BUY") -> float:
    """
    Tilt confidence by sector momentum:
      Leading sector + BUY signal  → ×1.08
      Lagging sector + BUY signal  → ×0.92
      Leading sector + SELL signal → ×0.92
      Lagging sector + SELL signal → ×1.08
    """
    m = sector_momentum(sector)
    if m is None:
        return 1.0
    # Classify: top/bottom-third of a reasonable 5d move band.
    threshold = 0.02
    if direction == "BUY":
        if m >= threshold:
            return 1.08
        if m <= -threshold:
            return 0.92
    else:  # SELL
        if m <= -threshold:
            return 1.08
        if m >= threshold:
            return 0.92
    return 1.0


# ------------------------------------------------------------------
# Market breadth — SPY trend as a proxy + VIX cross-check
# ------------------------------------------------------------------
def market_trend_score() -> Optional[float]:
    """
    Compact breadth score in [-1, +1]:
      +1 = strong uptrend (SPY above 50SMA, above 200SMA, VIX < 15)
       0 = neutral
      -1 = strong downtrend (below both SMAs, VIX > 25)
    None when unavailable.
    """
    def _fetch():
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("SPY", "1d")
        if df is None or df.empty or len(df) < 200:
            return None
        closes = df["Close"].astype(float)
        sma50 = closes.rolling(50).mean().iloc[-1]
        sma200 = closes.rolling(200).mean().iloc[-1]
        last = closes.iloc[-1]
        score = 0.0
        if last > sma50:
            score += 0.35
        else:
            score -= 0.35
        if last > sma200:
            score += 0.35
        else:
            score -= 0.35
        # 20-day slope of SMA50
        sma50_series = closes.rolling(50).mean()
        slope_pct = (sma50_series.iloc[-1] - sma50_series.iloc[-20]) / max(1e-6, sma50_series.iloc[-20])
        if slope_pct > 0.01:
            score += 0.15
        elif slope_pct < -0.01:
            score -= 0.15
        # VIX overlay
        v = current_vix()
        if v is not None:
            if v < 15:
                score += 0.15
            elif v > 25:
                score -= 0.15
        return max(-1.0, min(1.0, score))
    return _cached("breadth", _fetch)


def breadth_confidence_multiplier(direction: str = "BUY") -> float:
    """
    Penalize longs when the market is declining; penalize shorts when
    it's rallying. Amplify confidence when direction agrees with breadth.
      breadth ≥ +0.6  → BUY ×1.10, SELL ×0.85
      breadth ≤ −0.6  → BUY ×0.85, SELL ×1.10
      |breadth| < 0.2 → ×1.00 (neutral regime)
    """
    b = market_trend_score()
    if b is None:
        return 1.0
    if direction == "BUY":
        if b >= 0.6:
            return 1.10
        if b <= -0.6:
            return 0.85
        return 1.0 + (b * 0.10)   # linear in between
    else:
        if b <= -0.6:
            return 1.10
        if b >= 0.6:
            return 0.85
        return 1.0 - (b * 0.10)
