"""
Gap detection for technical analysis.

Detects two related concepts:

1. **Price gap (session / overnight gap)** — bar i opens beyond the previous
   bar's range:
     • Gap up   : Open[i] > High[i-1]
     • Gap down : Open[i] < Low[i-1]
   Useful for catching earnings/news gaps, opening drives, gap-and-go setups.

2. **Fair Value Gap (FVG, ICT 3-candle imbalance)** — a 3-bar pattern where the
   middle bar's range is so impulsive that bars 1 and 3 don't overlap, leaving
   an "imbalanced" wick zone:
     • Bullish FVG : Low[i+1]  > High[i-1]   (gap zone = High[i-1] → Low[i+1])
     • Bearish FVG : High[i+1] < Low[i-1]    (gap zone = High[i+1] → Low[i-1])
   Price often returns to fill this imbalance — bullish FVGs act as support,
   bearish FVGs as resistance.

A gap is *filled* once price subsequently trades back through the entire zone.
We track the *unfilled* portion (what's left between the highest fill and the
gap edge) so partial fills still leave a usable level.

Output shape (every gap dict):
  {
    "kind"      : "price_gap" | "fvg",
    "direction" : "bull" | "bear",
    "top"       : float,   # upper edge of the gap zone
    "bottom"    : float,   # lower edge
    "mid"       : float,   # midpoint (commonly used as fill target)
    "size"      : float,   # top - bottom in dollars
    "size_pct"  : float,   # size / mid as %
    "idx"       : int,     # bar index where the gap formed
    "age_bars"  : int,     # bars since formation
    "filled"    : bool,    # fully filled?
    "fill_pct"  : float,   # 0.0 → 1.0 (how much has been filled so far)
    "name"      : str,     # human-readable label
    "description": str,
  }
"""

from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_price_gaps(df: pd.DataFrame, max_lookback: int = 200, min_pct: float = 0.003) -> List[Dict[str, Any]]:
    """
    Detect overnight / session price gaps.
    `min_pct` filters out trivial gaps smaller than 0.3% of price.
    """
    if df is None or df.empty or len(df) < 3:
        return []
    sl = df.iloc[-max_lookback:]
    O = sl["Open"].values
    H = sl["High"].values
    L = sl["Low"].values
    n = len(sl)
    gaps: List[Dict[str, Any]] = []

    for i in range(1, n):
        prev_high = H[i - 1]
        prev_low = L[i - 1]
        op = O[i]

        if op > prev_high:
            top = float(op)
            bottom = float(prev_high)
            direction = "bull"
        elif op < prev_low:
            top = float(prev_low)
            bottom = float(op)
            direction = "bear"
        else:
            continue

        size = top - bottom
        mid = (top + bottom) / 2.0
        if mid <= 0 or size / mid < min_pct:
            continue

        fill_pct, filled = _gap_fill_state(sl, i, top, bottom, direction)
        gaps.append({
            "kind": "price_gap",
            "direction": direction,
            "top": round(top, 4),
            "bottom": round(bottom, 4),
            "mid": round(mid, 4),
            "size": round(size, 4),
            "size_pct": round(size / mid * 100, 2),
            "idx": int(i),
            "age_bars": int(n - 1 - i),
            "filled": bool(filled),
            "fill_pct": round(float(fill_pct), 3),
            "name": f"{'Gap Up' if direction == 'bull' else 'Gap Down'}",
            "description": (
                f"{'Bullish' if direction=='bull' else 'Bearish'} session gap "
                f"${bottom:.2f}-${top:.2f} ({size/mid*100:.2f}% wide); "
                f"{int(fill_pct*100)}% filled"
            ),
        })
    return gaps


def detect_fair_value_gaps(df: pd.DataFrame, max_lookback: int = 200, min_pct: float = 0.0015) -> List[Dict[str, Any]]:
    """
    Detect 3-candle imbalances (ICT FVGs).
    `min_pct` is small (0.15%) because FVGs are intra-trend and often tight.
    """
    if df is None or df.empty or len(df) < 3:
        return []
    sl = df.iloc[-max_lookback:]
    H = sl["High"].values
    L = sl["Low"].values
    n = len(sl)
    fvgs: List[Dict[str, Any]] = []

    # Form at i (middle bar uses i-1 and i+1)
    for i in range(1, n - 1):
        prev_high = H[i - 1]
        prev_low = L[i - 1]
        next_high = H[i + 1]
        next_low = L[i + 1]

        # Bullish FVG: gap between previous high and next low
        if next_low > prev_high:
            top = float(next_low)
            bottom = float(prev_high)
            direction = "bull"
        # Bearish FVG: gap between previous low and next high
        elif next_high < prev_low:
            top = float(prev_low)
            bottom = float(next_high)
            direction = "bear"
        else:
            continue

        size = top - bottom
        mid = (top + bottom) / 2.0
        if mid <= 0 or size / mid < min_pct:
            continue

        # FVG is "live" once bar i+1 closes — start fill tracking at i+2
        fill_pct, filled = _gap_fill_state(sl, i + 2, top, bottom, direction)
        fvgs.append({
            "kind": "fvg",
            "direction": direction,
            "top": round(top, 4),
            "bottom": round(bottom, 4),
            "mid": round(mid, 4),
            "size": round(size, 4),
            "size_pct": round(size / mid * 100, 2),
            "idx": int(i),
            "age_bars": int(n - 1 - i),
            "filled": bool(filled),
            "fill_pct": round(float(fill_pct), 3),
            "name": f"{'Bullish' if direction=='bull' else 'Bearish'} FVG",
            "description": (
                f"{'Bullish' if direction=='bull' else 'Bearish'} fair-value gap "
                f"${bottom:.2f}-${top:.2f} (3-bar imbalance, "
                f"{int(fill_pct*100)}% filled)"
            ),
        })
    return fvgs


def _gap_fill_state(df: pd.DataFrame, start_idx: int, top: float, bottom: float, direction: str) -> Tuple[float, bool]:
    """
    Returns (fill_pct, fully_filled) by looking at all bars from start_idx onward.
    For a bull gap, fill happens as price trades DOWN through the zone (bears fill).
    For a bear gap, fill happens as price trades UP through the zone (bulls fill).
    """
    n = len(df)
    if start_idx >= n:
        return 0.0, False
    rest = df.iloc[start_idx:]
    H = rest["High"].values
    L = rest["Low"].values
    size = max(top - bottom, 1e-9)

    if direction == "bull":
        # Lowest low after gap — how deep did sellers push back into the zone?
        lowest = float(np.min(L)) if len(L) else top
        if lowest <= bottom:
            return 1.0, True
        if lowest >= top:
            return 0.0, False
        return (top - lowest) / size, False
    else:
        highest = float(np.max(H)) if len(H) else bottom
        if highest >= top:
            return 1.0, True
        if highest <= bottom:
            return 0.0, False
        return (highest - bottom) / size, False


# ---------------------------------------------------------------------------
# Aggregation helpers used by signal_generator / auto_trader / backtester
# ---------------------------------------------------------------------------

def detect_all_gaps(df: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    """All gaps (filled + unfilled) bucketed by kind."""
    return {
        "price_gaps": detect_price_gaps(df),
        "fvgs": detect_fair_value_gaps(df),
    }


def unfilled_gaps(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """All currently-unfilled gaps (price gaps + FVGs), sorted newest first."""
    out = [g for g in detect_price_gaps(df) if not g["filled"]]
    out += [g for g in detect_fair_value_gaps(df) if not g["filled"]]
    out.sort(key=lambda g: g["idx"], reverse=True)
    return out


def gap_targets_above(df: pd.DataFrame, price: float, max_n: int = 5) -> List[float]:
    """
    Unfilled bear-gap fills *above* the current price = upside fill targets.
    A bear gap above price is a magnet: bulls usually push to fill it.
    Returns a list of midpoint prices.
    """
    out = []
    for g in unfilled_gaps(df):
        if g["direction"] == "bear" and g["mid"] > price:
            # The unfilled portion's midpoint is the cleanest fill target
            unfilled_top = g["top"]
            unfilled_bottom = max(g["bottom"], price)
            mid = (unfilled_top + unfilled_bottom) / 2.0
            out.append(round(mid, 2))
    return sorted(set(out))[:max_n]


def gap_targets_below(df: pd.DataFrame, price: float, max_n: int = 5) -> List[float]:
    """Unfilled bull-gap fills *below* current price = downside fill targets."""
    out = []
    for g in unfilled_gaps(df):
        if g["direction"] == "bull" and g["mid"] < price:
            unfilled_bottom = g["bottom"]
            unfilled_top = min(g["top"], price)
            mid = (unfilled_top + unfilled_bottom) / 2.0
            out.append(round(mid, 2))
    return sorted(set(out), reverse=True)[:max_n]


def support_gaps_below(df: pd.DataFrame, price: float) -> List[Dict[str, Any]]:
    """Unfilled bullish FVGs / gap-ups *below* price act as support zones."""
    return [
        g for g in unfilled_gaps(df)
        if g["direction"] == "bull" and g["top"] < price * 1.001
    ]


def resistance_gaps_above(df: pd.DataFrame, price: float) -> List[Dict[str, Any]]:
    """Unfilled bearish FVGs / gap-downs *above* price act as resistance zones."""
    return [
        g for g in unfilled_gaps(df)
        if g["direction"] == "bear" and g["bottom"] > price * 0.999
    ]


def in_gap(df: pd.DataFrame, price: float) -> Optional[Dict[str, Any]]:
    """Return the unfilled gap (if any) that currently contains the price."""
    for g in unfilled_gaps(df):
        if g["bottom"] <= price <= g["top"]:
            return g
    return None


# ---------------------------------------------------------------------------
# Pattern-detector compatible output (for inclusion in pattern lists)
# ---------------------------------------------------------------------------

def gap_patterns(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Convert recent significant gaps into pattern-detector compatible dicts:
      {name, type, confidence, description}
    Only surfaces fresh, large, or unfilled gaps to avoid pattern-list spam.
    """
    out = []
    n = len(df) if df is not None else 0
    for g in detect_price_gaps(df):
        if g["age_bars"] > 30 and g["filled"]:
            continue
        is_bull = g["direction"] == "bull"
        conf = 60 + (10 if not g["filled"] else 0) + min(int(g["size_pct"] * 4), 20)
        out.append({
            "name": g["name"],
            "type": "bullish" if is_bull else "bearish",
            "confidence": min(conf, 90),
            "description": g["description"],
        })
    for g in detect_fair_value_gaps(df):
        if g["filled"] or g["age_bars"] > 50:
            continue
        is_bull = g["direction"] == "bull"
        conf = 55 + min(int(g["size_pct"] * 6), 25)
        out.append({
            "name": g["name"],
            "type": "bullish" if is_bull else "bearish",
            "confidence": min(conf, 85),
            "description": g["description"],
        })
    # Limit to most recent / most relevant
    out.sort(key=lambda p: p["confidence"], reverse=True)
    return out[:4]
