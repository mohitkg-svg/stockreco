"""
Supply & Demand zone detector.

A demand zone is the base candles preceding a strong bullish breakout — institutional
accumulation that typically acts as support on revisit. A supply zone is the mirror
image before a strong bearish breakdown.

Detection rules (pragmatic, no ML):
  1. A "strong move" candle has |close-open| > MOVE_ATR_MULT × ATR and a full-bar range
     larger than recent average.
  2. Walk back up to BASE_LOOKBACK candles of small bodies (base |close-open| < 0.5 × ATR).
     These form the zone.
  3. Zone = (min(Low), max(High)) of the base candles.
  4. Score the zone by:
        - Strength of the departure move (how many ATRs)
        - Freshness (fewer retests = stronger — each retest weakens)
        - Recency (recent zones matter more)
  5. Classify relative to current price: demand zones must sit at/below price,
     supply zones at/above.
"""
from typing import List, Dict, Any
import pandas as pd


MOVE_ATR_MULT = 1.5       # departure candle body ≥ 1.5× ATR
MAX_BASE_BODY = 0.5       # base candle body ≤ 0.5× ATR
BASE_LOOKBACK = 3         # max consolidation candles per zone
MAX_ZONES_PER_SIDE = 6    # keep top N after scoring
RETEST_TOUCH_PCT = 0.003  # a candle "retests" when it pierces within 0.3% of the zone


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def _count_retests(df: pd.DataFrame, from_idx: int, zone_low: float, zone_high: float) -> int:
    """Count candles after from_idx whose range overlaps the zone band."""
    later = df.iloc[from_idx + 1:]
    if later.empty:
        return 0
    low_trigger = zone_low * (1 - RETEST_TOUCH_PCT)
    high_trigger = zone_high * (1 + RETEST_TOUCH_PCT)
    overlap = (later["Low"] <= high_trigger) & (later["High"] >= low_trigger)
    # Count only distinct "visit" sequences (collapse consecutive touches)
    retests = 0
    in_visit = False
    for hit in overlap.values:
        if hit and not in_visit:
            retests += 1
            in_visit = True
        elif not hit:
            in_visit = False
    return retests


def detect_zones(df: pd.DataFrame, price: float) -> Dict[str, List[Dict[str, Any]]]:
    """
    Return {"demand": [zones...], "supply": [zones...]} sorted strongest first.
    Each zone dict: {low, high, score, retests, age_bars, strength_atr, type, created_at}
    """
    if df.empty or len(df) < 25:
        return {"demand": [], "supply": []}

    atr = _atr(df)
    bodies = (df["Close"] - df["Open"]).abs()
    atr_aligned = atr.reindex(df.index).ffill()

    demand: List[Dict[str, Any]] = []
    supply: List[Dict[str, Any]] = []

    n = len(df)
    for i in range(5, n):
        a = float(atr_aligned.iloc[i]) if not np.isnan(atr_aligned.iloc[i]) else 0
        if a <= 0:
            continue
        body = float(bodies.iloc[i])
        move = body / a
        if move < MOVE_ATR_MULT:
            continue

        is_bull = df["Close"].iloc[i] > df["Open"].iloc[i]

        # Walk back while base criteria hold
        base_idx_start = i - 1
        base_idx_end = i - 1
        collected = 0
        while base_idx_start >= 0 and collected < BASE_LOOKBACK:
            b = float(bodies.iloc[base_idx_start])
            if b / a <= MAX_BASE_BODY:
                base_idx_end = base_idx_start
                collected += 1
                base_idx_start -= 1
            else:
                break
        if collected == 0:
            continue

        zone_slice = df.iloc[base_idx_end:i]  # candles forming the base (exclusive of departure)
        zone_low = float(zone_slice["Low"].min())
        zone_high = float(zone_slice["High"].max())
        if zone_high <= zone_low:
            continue

        retests = _count_retests(df, i, zone_low, zone_high)
        age_bars = n - 1 - i

        # Score: departure strength (40) + freshness (25) + recency (20) + base tightness (15)
        strength_score = min(move / 3.0, 1.0) * 40
        fresh_score = max(0.0, 25 - retests * 8)
        recency_score = max(0.0, 20 - (age_bars / max(n, 1)) * 20)
        tightness = 1.0 - min((zone_high - zone_low) / (a * 2), 1.0)  # tighter = better
        tight_score = tightness * 15
        total = round(strength_score + fresh_score + recency_score + tight_score, 1)

        created_ts = int(df.index[base_idx_end].timestamp())
        zone = {
            "low": round(zone_low, 2),
            "high": round(zone_high, 2),
            "mid": round((zone_low + zone_high) / 2, 2),
            "score": total,
            "retests": retests,
            "age_bars": age_bars,
            "strength_atr": round(move, 2),
            "created_at": created_ts,
        }
        if is_bull:
            zone["type"] = "demand"
            # Only keep if below or at current price (relevant support)
            if zone_high <= price * 1.002:
                demand.append(zone)
        else:
            zone["type"] = "supply"
            if zone_low >= price * 0.998:
                supply.append(zone)

    # Merge near-duplicate zones (within 0.5% of mid)
    demand = _merge_zones(demand)
    supply = _merge_zones(supply)

    demand.sort(key=lambda z: z["score"], reverse=True)
    supply.sort(key=lambda z: z["score"], reverse=True)
    return {
        "demand": demand[:MAX_ZONES_PER_SIDE],
        "supply": supply[:MAX_ZONES_PER_SIDE],
    }


def _merge_zones(zones: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not zones:
        return zones
    zones = sorted(zones, key=lambda z: z["mid"])
    merged: List[Dict[str, Any]] = []
    for z in zones:
        if merged and abs(z["mid"] - merged[-1]["mid"]) / merged[-1]["mid"] < 0.005:
            # Merge — take union, keep higher score
            prev = merged[-1]
            prev["low"] = min(prev["low"], z["low"])
            prev["high"] = max(prev["high"], z["high"])
            prev["mid"] = round((prev["low"] + prev["high"]) / 2, 2)
            if z["score"] > prev["score"]:
                prev["score"] = z["score"]
                prev["retests"] = z["retests"]
                prev["age_bars"] = z["age_bars"]
                prev["strength_atr"] = z["strength_atr"]
        else:
            merged.append(z)
    return merged


def nearest_demand_below(zones: Dict[str, List[dict]], price: float) -> Optional[dict]:
    candidates = [z for z in zones.get("demand", []) if z["high"] < price]
    if not candidates:
        return None
    return max(candidates, key=lambda z: z["high"])  # closest below


def nearest_supply_above(zones: Dict[str, List[dict]], price: float) -> Optional[dict]:
    candidates = [z for z in zones.get("supply", []) if z["low"] > price]
    if not candidates:
        return None
    return min(candidates, key=lambda z: z["low"])  # closest above


def in_zone(zones: Dict[str, List[dict]], price: float, side: str) -> Optional[dict]:
    """Return the zone currently containing price (for the given side), if any."""
    for z in zones.get(side, []):
        if z["low"] <= price <= z["high"]:
            return z
    return None
