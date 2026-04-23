"""
Volume profile — computes POC (Point of Control), VAH, VAL from recent
OHLCV bars. These levels are high-probability magnets/reversal points and
make excellent target candidates, supplementing the existing structural
levels (fibs, pivots, swing highs/lows).

POC = price bin with the most volume over the window.
VAH/VAL = upper/lower edges of the 70% Value Area centered on POC.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


def compute_volume_profile(
    df: pd.DataFrame,
    window: int = 60,
    num_bins: int = 40,
    value_area_pct: float = 0.70,
) -> Optional[Dict[str, float]]:
    """Return {poc, vah, val} for the last `window` bars.

    Each bar's volume is distributed evenly across the price bins between its
    High and Low — approximates the Time Price Opportunity profile without
    needing tick data.
    """
    if df is None or df.empty or len(df) < window:
        return None
    d = df.iloc[-window:].copy()
    lo = float(d["Low"].min())
    hi = float(d["High"].max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, num_bins + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    vol_by_bin = np.zeros(num_bins, dtype=float)
    for _, r in d.iterrows():
        bar_lo, bar_hi, vol = float(r["Low"]), float(r["High"]), float(r.get("Volume") or 0)
        if bar_hi <= bar_lo or vol <= 0:
            continue
        # Distribute bar's volume across bins its range touches.
        lo_idx = max(0, int((bar_lo - lo) / (hi - lo) * num_bins))
        hi_idx = min(num_bins - 1, int((bar_hi - lo) / (hi - lo) * num_bins))
        n = max(1, hi_idx - lo_idx + 1)
        vol_by_bin[lo_idx:hi_idx + 1] += vol / n
    total = float(vol_by_bin.sum())
    if total <= 0:
        return None
    poc_idx = int(np.argmax(vol_by_bin))
    poc = float(centers[poc_idx])
    # Expand from POC outward until 70% of volume is contained.
    target_vol = total * value_area_pct
    lo_i = hi_i = poc_idx
    running = vol_by_bin[poc_idx]
    while running < target_vol and (lo_i > 0 or hi_i < num_bins - 1):
        left = vol_by_bin[lo_i - 1] if lo_i > 0 else -1
        right = vol_by_bin[hi_i + 1] if hi_i < num_bins - 1 else -1
        if right >= left:
            hi_i += 1
            running += vol_by_bin[hi_i]
        else:
            lo_i -= 1
            running += vol_by_bin[lo_i]
    vah = float(edges[hi_i + 1])
    val = float(edges[lo_i])
    return {"poc": round(poc, 2), "vah": round(vah, 2), "val": round(val, 2)}


def levels_above(profile: Optional[Dict[str, float]], price: float) -> List[float]:
    """Return profile levels that sit above `price` (target candidates for BUY)."""
    if not profile or price <= 0:
        return []
    out = []
    for key in ("vah", "poc", "val"):
        v = profile.get(key)
        if v and v > price * 1.005:
            out.append(float(v))
    return sorted(set(round(v, 2) for v in out))


def levels_below(profile: Optional[Dict[str, float]], price: float) -> List[float]:
    """Return profile levels that sit below `price` (target candidates for SELL)."""
    if not profile or price <= 0:
        return []
    out = []
    for key in ("val", "poc", "vah"):
        v = profile.get(key)
        if v and v < price * 0.995:
            out.append(float(v))
    return sorted(set(round(v, 2) for v in out), reverse=True)
