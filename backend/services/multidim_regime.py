"""r96 R6: multi-dimensional regime signals.

The existing regime_router classifies SPY as TREND / CHOP / HIGH_VOL using
ADX + VIX level only. The audit flagged this as one-dimensional: VIX level
alone misses term-structure stress (VIX > VIX3M = backwardation, classic
stress signal) and realized-vol spikes that haven't yet shown up in
implied vol.

This module exposes three additional indicators:

  * `vix_term_structure_ratio()` — VIX / VIX3M. >1.0 = backwardation.
  * `realized_vol_annualized(symbol="SPY", lookback=20)` — SPY 20d std × √252.
  * `spy_breadth_proxy()` — fraction of recent SPY bars where close>SMA20
    over a trailing window. Crude proxy for advance/decline (real breadth
    data costs $$$) — useful as a regime confirmation signal.

`stress_regime_active()` returns True when any of the four conditions
hold: VIX level ≥ 22, VIX term ratio ≥ 1.0, realized vol ≥ 25%, or
breadth proxy ≤ 0.35. This OR-logic intentionally errs toward
declaring stress — under cfg.multidim_regime_enabled the regime_router
treats this as HIGH_VOL.

Gated by cfg.multidim_regime_enabled (default False).
"""
from __future__ import annotations
import logging
import math
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Thresholds chosen to err toward "declare stress" (Type I over Type II)
# — false alarms cost a few skipped entries; missed regimes cost real money.
VIX_LEVEL_STRESS = 22.0
VIX_TERM_BACKWARDATION = 1.0
REALIZED_VOL_STRESS_ANNUALIZED = 0.25
BREADTH_BEAR_FLOOR = 0.35


def vix_term_structure_ratio() -> Optional[float]:
    """VIX / VIX3M. Returns None when either tape is missing. >1.0 means
    backwardation (front > back, classic stress)."""
    try:
        from services.data_fetcher import fetch_ohlcv
        v = fetch_ohlcv("^VIX", "1d")
        v3 = fetch_ohlcv("^VIX3M", "1d")
        if v is None or v.empty or v3 is None or v3.empty:
            return None
        vix_last = float(v["Close"].iloc[-1])
        v3_last = float(v3["Close"].iloc[-1])
        if v3_last <= 0:
            return None
        return vix_last / v3_last
    except Exception as e:
        logger.debug(f"vix_term_structure_ratio: {e}")
        return None


def realized_vol_annualized(symbol: str = "SPY", lookback: int = 20) -> Optional[float]:
    """Annualized stdev of daily returns over `lookback` bars. Returns
    None when insufficient data. Independent of implied vol — captures
    realized stress that hasn't yet shown up in VIX."""
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(symbol, "1d")
        if df is None or df.empty or len(df) < lookback + 1:
            return None
        tail = df["Close"].astype(float).tail(lookback + 1).tolist()
        rets = []
        for i in range(1, len(tail)):
            prev = tail[i - 1]
            if prev <= 0:
                continue
            rets.append((tail[i] - prev) / prev)
        if len(rets) < 2:
            return None
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        return (var ** 0.5) * math.sqrt(252)
    except Exception as e:
        logger.debug(f"realized_vol_annualized({symbol}): {e}")
        return None


def spy_breadth_proxy(lookback: int = 20) -> Optional[float]:
    """Fraction of the last `lookback` SPY bars where Close > SMA(20).
    Crude proxy for breadth — real advance/decline data isn't free. When
    SPY itself is sustainedly above its 20d MA, breadth is generally
    healthy; sustained below = bear breadth. Returns None on data failure.
    """
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("SPY", "1d")
        if df is None or df.empty or len(df) < lookback + 20:
            return None
        closes = df["Close"].astype(float)
        sma20 = closes.rolling(20).mean()
        tail = (closes.iloc[-lookback:] > sma20.iloc[-lookback:])
        if len(tail) == 0:
            return None
        return float(tail.sum()) / float(len(tail))
    except Exception as e:
        logger.debug(f"spy_breadth_proxy: {e}")
        return None


def stress_regime_active() -> Tuple[bool, dict]:
    """Returns (is_stress, detail_dict). is_stress=True when ANY of the
    four signals fires. detail_dict surfaces every component so the
    operator can see WHICH dimension flipped (useful for tuning)."""
    detail = {
        "vix_level": None,
        "vix_term_ratio": None,
        "realized_vol": None,
        "breadth_proxy": None,
        "flagged_by": [],
    }
    try:
        from services.market_context import current_vix
        v = current_vix()
        if v is not None:
            detail["vix_level"] = float(v)
            if detail["vix_level"] >= VIX_LEVEL_STRESS:
                detail["flagged_by"].append("vix_level")
    except Exception:
        pass
    tr = vix_term_structure_ratio()
    if tr is not None:
        detail["vix_term_ratio"] = tr
        if tr >= VIX_TERM_BACKWARDATION:
            detail["flagged_by"].append("vix_term_backwardation")
    rv = realized_vol_annualized("SPY", lookback=20)
    if rv is not None:
        detail["realized_vol"] = rv
        if rv >= REALIZED_VOL_STRESS_ANNUALIZED:
            detail["flagged_by"].append("realized_vol")
    br = spy_breadth_proxy(lookback=20)
    if br is not None:
        detail["breadth_proxy"] = br
        if br <= BREADTH_BEAR_FLOOR:
            detail["flagged_by"].append("breadth_proxy")
    is_stress = len(detail["flagged_by"]) > 0
    return (is_stress, detail)


def multidim_enabled() -> bool:
    """Cheap accessor — read cfg.multidim_regime_enabled. Default False."""
    try:
        from database import SessionLocal, AutoTraderConfig
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            return bool(getattr(cfg, "multidim_regime_enabled", False)) if cfg else False
        finally:
            db.close()
    except Exception:
        return False
