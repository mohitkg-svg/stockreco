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


RETRACEMENT_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786]
EXTENSION_RATIOS = [1.272, 1.618, 2.0, 2.618, 4.236]
KEY_RATIOS = {0.382, 0.5, 0.618}  # "high-conviction" pullback zones
NEAR_PCT = 0.005  # within 0.5% of a fib level counts as "at" the level


def _find_recent_swing(df: pd.DataFrame, lookback: int = 90, min_window: int = 5) -> Optional[Tuple[int, int, str]]:
    return None


def compute_fib_levels(df: pd.DataFrame, lookback: int = 90) -> Optional[Dict[str, Any]]:
    return None


# ------------------------------------------------------------------
# Helpers for the signal generator
# ------------------------------------------------------------------
def fib_supports_below(fib: Dict[str, Any], price: float) -> List[Dict[str, Any]]:
    return []


def fib_resistances_above(fib: Dict[str, Any], price: float) -> List[Dict[str, Any]]:
    return []


def near_key_fib(fib: Dict[str, Any], price: float, pct: float = NEAR_PCT) -> Optional[Dict[str, Any]]:
    return None
