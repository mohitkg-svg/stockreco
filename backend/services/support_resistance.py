import pandas as pd
import numpy as np
from typing import List, Dict, Tuple


def pivot_points(df: pd.DataFrame) -> Dict[str, float]:
    """Compute classic pivot points from the previous period's H/L/C."""
    if df.empty or len(df) < 2:
        return {}
    prev = df.iloc[-2]
    H, L, C = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    P = (H + L + C) / 3
    R1 = 2 * P - L
    S1 = 2 * P - H
    R2 = P + (H - L)
    S2 = P - (H - L)
    R3 = H + 2 * (P - L)
    S3 = L - 2 * (H - P)
    return {"P": round(P, 2), "R1": round(R1, 2), "R2": round(R2, 2), "R3": round(R3, 2),
            "S1": round(S1, 2), "S2": round(S2, 2), "S3": round(S3, 2)}


def swing_levels(df: pd.DataFrame, window: int = 10, max_levels: int = 6) -> List[Dict]:
    """Detect significant swing high/low levels from price history."""
    if df.empty or len(df) < window * 2:
        return []

    highs = df["High"].values
    lows = df["Low"].values
    n = len(highs)
    levels = []

    for i in range(window, n - window):
        # Swing high: local max within window
        if highs[i] == max(highs[i - window:i + window + 1]):
            levels.append({"price": float(highs[i]), "type": "resistance", "idx": i, "touches": 1})
        # Swing low: local min within window
        if lows[i] == min(lows[i - window:i + window + 1]):
            levels.append({"price": float(lows[i]), "type": "support", "idx": i, "touches": 1})

    # Cluster nearby levels (within 0.5%)
    clustered = _cluster_levels(levels)

    # Sort by recency (idx) and strength, keep top N
    clustered.sort(key=lambda x: x["touches"] * 10 + x["idx"] / n, reverse=True)
    return clustered[:max_levels]


def _cluster_levels(levels: List[Dict], tolerance: float = 0.005) -> List[Dict]:
    """Merge price levels that are within tolerance% of each other."""
    if not levels:
        return []
    merged = []
    used = [False] * len(levels)
    for i, lvl in enumerate(levels):
        if used[i]:
            continue
        cluster = [lvl]
        used[i] = True
        for j, other in enumerate(levels):
            if not used[j] and lvl["type"] == other["type"]:
                if abs(lvl["price"] - other["price"]) / lvl["price"] <= tolerance:
                    cluster.append(other)
                    used[j] = True
        avg_price = np.mean([c["price"] for c in cluster])
        max_idx = max(c["idx"] for c in cluster)
        merged.append({
            "price": round(float(avg_price), 2),
            "type": cluster[0]["type"],
            "touches": len(cluster),
            "idx": max_idx,
        })
    return merged


def nearest_support_resistance(levels: List[Dict], current_price: float) -> Tuple[float, float]:
    """Return (nearest_support, nearest_resistance) relative to current price."""
    supports = [l["price"] for l in levels if l["type"] == "support" and l["price"] < current_price]
    resistances = [l["price"] for l in levels if l["type"] == "resistance" and l["price"] > current_price]
    nearest_sup = max(supports) if supports else current_price * 0.97
    nearest_res = min(resistances) if resistances else current_price * 1.03
    return nearest_sup, nearest_res


def multi_timeframe_levels(
    ticker: str,
    timeframes: List[str] = None,
    window: int = 10,
    cluster_tol: float = 0.0075,
) -> List[Dict]:
    """
    Aggregate swing levels across multiple timeframes and cluster them so a
    level confirmed by 4h+1d+1w gets weighted more heavily than one only seen
    on 5m.

    Returns list of {price, type, touches, timeframes:[...], strength}.
    Higher-timeframe levels carry more weight: weights {1mo: 4, 1d: 3, 4h: 2,
    1h: 1, 30m/15m/5m: 0.5}. `strength` = 1..5 bucketed by aggregate weight.
    """
    from services.data_fetcher import fetch_ohlcv

    timeframes = timeframes or ["1d", "4h", "1h"]
    weights = {"1mo": 4.0, "1d": 3.0, "4h": 2.0, "1h": 1.0, "30m": 0.5, "15m": 0.5, "5m": 0.5}

    raw: List[Dict] = []
    for tf in timeframes:
        try:
            df = fetch_ohlcv(ticker, tf)
        except Exception:
            continue
        if df is None or df.empty or len(df) < window * 2:
            continue
        w = weights.get(tf, 1.0)
        for lvl in swing_levels(df, window=window, max_levels=12):
            raw.append({
                "price": lvl["price"],
                "type": lvl["type"],
                "weight": w * lvl.get("touches", 1),
                "tf": tf,
            })

    # Cluster across timeframes by relative price tolerance, keeping only same-type
    if not raw:
        return []
    raw.sort(key=lambda r: r["price"])
    clusters: List[Dict] = []
    used = [False] * len(raw)
    for i, item in enumerate(raw):
        if used[i]:
            continue
        cluster = [item]
        used[i] = True
        for j in range(i + 1, len(raw)):
            if used[j] or raw[j]["type"] != item["type"]:
                continue
            if abs(raw[j]["price"] - item["price"]) / item["price"] <= cluster_tol:
                cluster.append(raw[j])
                used[j] = True
        prices = [c["price"] for c in cluster]
        weight_sum = sum(c["weight"] for c in cluster)
        tfs = sorted(set(c["tf"] for c in cluster))
        # Strength bucketed: 1 (one tf, low weight) … 5 (multi-tf high weight)
        if weight_sum >= 8:
            strength = 5
        elif weight_sum >= 5:
            strength = 4
        elif weight_sum >= 3:
            strength = 3
        elif weight_sum >= 1.5:
            strength = 2
        else:
            strength = 1
        clusters.append({
            "price": round(float(np.mean(prices)), 2),
            "type": item["type"],
            "weight": round(weight_sum, 2),
            "touches": len(cluster),
            "timeframes": tfs,
            "strength": strength,
        })
    # Sort by weight (descending)
    clusters.sort(key=lambda c: c["weight"], reverse=True)
    return clusters


def classify_levels_relative_to_price(levels: List[Dict], current_price: float) -> List[Dict]:
    """Re-classify levels as support/resistance based on current price position."""
    result = []
    for lvl in levels:
        if lvl["price"] < current_price:
            result.append({**lvl, "type": "support", "strength": min(lvl["touches"], 3)})
        else:
            result.append({**lvl, "type": "resistance", "strength": min(lvl["touches"], 3)})
    return result
