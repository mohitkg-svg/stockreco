"""
Fibonacci retracement & extension levels.

Workflow:
  1. Find the most recent significant swing leg (high → low or low → high) over
     a lookback window using ATR-normalized swing detection.
  2. Compute retracements between swing_low and swing_high at standard ratios:
        23.6%, 38.2%, 50%, 61.8% (golden), 78.6%
     Retracements act as potential support (in an uptrend) or resistance
     (in a downtrend) — pullback targets.
  3. Compute extensions beyond the swing in the trend direction at:
        127.2%, 161.8%, 200%, 261.8%, 423.6%
     Extensions act as upside targets (uptrend) or downside targets
     (downtrend) for trend continuation.

The "direction" of the most recent leg is what matters for trade planning:
  - direction="up"   → swing went low→high. Retracements are support BELOW
                       current price (in the leg). Extensions project ABOVE.
  - direction="down" → swing went high→low. Retracements are resistance
                       ABOVE current price. Extensions project BELOW.
"""
from __future__ import annotations
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np


RETRACEMENT_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786]
EXTENSION_RATIOS = [1.272, 1.618, 2.0, 2.618, 4.236]
KEY_RATIOS = {0.382, 0.5, 0.618}  # "high-conviction" pullback zones
NEAR_PCT = 0.005  # within 0.5% of a fib level counts as "at" the level


def _find_recent_swing(df: pd.DataFrame, lookback: int = 90, min_window: int = 5) -> Optional[Tuple[int, int, str]]:
    """
    Identify the most recent significant swing leg.

    Returns (low_idx, high_idx, direction) where direction is "up" if low_idx < high_idx
    (price moved from low to high) or "down" if high_idx < low_idx.

    Audit fix H9: the previous implementation used absolute argmax/argmin of
    the entire lookback window, which meant a 3-month-old spike would anchor
    every subsequent fib calculation even after the structure had fully
    rotated. We now locate the most recent swing PIVOTS (3-bar confirmed
    highs/lows) and pair the latest high with the latest low, so the fib
    projection rides the current swing leg rather than stale extremes.
    Falls back to absolute extremes only when no confirmed pivots exist
    (e.g. steady-trend data with no clean pivots in the window).
    """
    if df.empty or len(df) < min_window * 2:
        return None
    window = df.tail(lookback)
    highs = window["High"].values
    lows = window["Low"].values
    n = len(highs)

    # 3-bar confirmed pivot: a high/low that's the extreme of a 7-bar
    # window centered on the bar. k=3 is the textbook minimum for "this
    # swing is resolved" — smaller k picks up noise, larger k lags too much.
    k = 3
    pivot_highs: List[int] = []
    pivot_lows: List[int] = []
    for i in range(k, n - k):
        if highs[i] == max(highs[i - k:i + k + 1]):
            pivot_highs.append(i)
        if lows[i] == min(lows[i - k:i + k + 1]):
            pivot_lows.append(i)

    base = len(df) - len(window)
    if pivot_highs and pivot_lows:
        hi_pos = pivot_highs[-1]
        lo_pos = pivot_lows[-1]
    else:
        # Fallback: absolute extremes (legacy behavior)
        hi_pos = int(highs.argmax())
        lo_pos = int(lows.argmin())

    if hi_pos == lo_pos:
        return None
    hi_idx = base + hi_pos
    lo_idx = base + lo_pos
    direction = "up" if lo_idx < hi_idx else "down"
    return lo_idx, hi_idx, direction


def compute_fib_levels(df: pd.DataFrame, lookback: int = 90) -> Optional[Dict[str, Any]]:
    """
    Return Fibonacci retracement + extension levels for the most recent swing leg.

    Output:
        {
            "swing_low": float, "swing_high": float,
            "swing_low_ts": int (unix), "swing_high_ts": int,
            "direction": "up" | "down",
            "leg_size": float,
            "retracements": [{"ratio": 0.382, "price": ..., "label": "38.2%"}, ...],
            "extensions":   [{"ratio": 1.618, "price": ..., "label": "161.8%"}, ...],
        }

    Pricing convention:
      - Retracement at ratio r is measured from the END of the leg back toward the start.
        For up-leg: swing_high - r * (swing_high - swing_low)
        For down-leg: swing_low + r * (swing_high - swing_low)
      - Extension at ratio e projects beyond the END of the leg in trend direction.
        For up-leg: swing_low + e * (swing_high - swing_low)
        For down-leg: swing_high - e * (swing_high - swing_low)
    """
    swing = _find_recent_swing(df, lookback=lookback)
    if not swing:
        return None
    lo_idx, hi_idx, direction = swing
    swing_low = float(df.iloc[lo_idx]["Low"])
    swing_high = float(df.iloc[hi_idx]["High"])
    leg = swing_high - swing_low
    if leg <= 0:
        return None

    retracements = []
    extensions = []
    if direction == "up":
        for r in RETRACEMENT_RATIOS:
            retracements.append({
                "ratio": r,
                "price": round(swing_high - r * leg, 2),
                "label": f"{r * 100:.1f}%",
            })
        for e in EXTENSION_RATIOS:
            extensions.append({
                "ratio": e,
                "price": round(swing_low + e * leg, 2),
                "label": f"{e * 100:.1f}%",
            })
    else:  # down
        for r in RETRACEMENT_RATIOS:
            retracements.append({
                "ratio": r,
                "price": round(swing_low + r * leg, 2),
                "label": f"{r * 100:.1f}%",
            })
        for e in EXTENSION_RATIOS:
            extensions.append({
                "ratio": e,
                "price": round(swing_high - e * leg, 2),
                "label": f"{e * 100:.1f}%",
            })

    def _ts(idx: int) -> int:
        try:
            return int(df.index[idx].timestamp())
        except Exception:
            return 0

    return {
        "swing_low": round(swing_low, 2),
        "swing_high": round(swing_high, 2),
        "swing_low_ts": _ts(lo_idx),
        "swing_high_ts": _ts(hi_idx),
        "direction": direction,
        "leg_size": round(leg, 2),
        "retracements": retracements,
        "extensions": extensions,
    }


# ------------------------------------------------------------------
# Helpers for the signal generator
# ------------------------------------------------------------------
def fib_supports_below(fib: Dict[str, Any], price: float) -> List[Dict[str, Any]]:
    """Retracement / extension levels sitting BELOW price (act as support for longs)."""
    if not fib:
        return []
    levels = []
    for r in fib.get("retracements", []):
        if r["price"] < price:
            levels.append({**r, "kind": "retracement"})
    for e in fib.get("extensions", []):
        if e["price"] < price:
            levels.append({**e, "kind": "extension"})
    levels.sort(key=lambda x: x["price"], reverse=True)  # nearest below first
    return levels


def fib_resistances_above(fib: Dict[str, Any], price: float) -> List[Dict[str, Any]]:
    """Retracement / extension levels sitting ABOVE price (act as resistance / targets for longs)."""
    if not fib:
        return []
    levels = []
    for r in fib.get("retracements", []):
        if r["price"] > price:
            levels.append({**r, "kind": "retracement"})
    for e in fib.get("extensions", []):
        if e["price"] > price:
            levels.append({**e, "kind": "extension"})
    levels.sort(key=lambda x: x["price"])  # nearest above first
    return levels


def near_key_fib(fib: Dict[str, Any], price: float, pct: float = NEAR_PCT) -> Optional[Dict[str, Any]]:
    """If price is within `pct` of a key retracement (38.2/50/61.8), return that level."""
    if not fib:
        return None
    for r in fib.get("retracements", []):
        if r["ratio"] in KEY_RATIOS:
            if abs(price - r["price"]) / price <= pct:
                return r
    return None
