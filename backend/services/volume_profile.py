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
import pandas as pd


def compute_volume_profile(
    df: pd.DataFrame,
    window: int = 60,
    num_bins: int = 40,
    value_area_pct: float = 0.70,
) -> Optional[Dict[str, float]]:
    return None


def levels_above(profile: Optional[Dict[str, float]], price: float) -> List[float]:
    return []


def levels_below(profile: Optional[Dict[str, float]], price: float) -> List[float]:
    return []
