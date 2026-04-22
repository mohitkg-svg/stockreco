import pandas as pd
import numpy as np
from typing import List, Dict


def detect_patterns(df: pd.DataFrame) -> List[Dict]:
    """Detect chart patterns. Returns list of {name, type, confidence, description}."""
    if df.empty or len(df) < 30:
        return []

    patterns = []
    patterns.extend(_double_top_bottom(df))
    patterns.extend(_head_and_shoulders(df))
    patterns.extend(_triangle(df))
    patterns.extend(_flag(df))
    patterns.extend(_golden_death_cross(df))
    return patterns


def _double_top_bottom(df: pd.DataFrame, window: int = 20, tol: float = 0.02) -> List[Dict]:
    highs = df["High"].values
    lows = df["Low"].values
    n = len(highs)
    result = []
    half = window // 2

    for i in range(window, n - half):
        # Look for two peaks within tol%
        peak1_idx = np.argmax(highs[i - window:i]) + (i - window)
        peak2_idx = np.argmax(highs[i - half:i]) + (i - half)
        if peak1_idx != peak2_idx:
            p1, p2 = highs[peak1_idx], highs[peak2_idx]
            if p1 > 0 and abs(p1 - p2) / p1 < tol:
                result.append({
                    "name": "Double Top",
                    "type": "bearish",
                    "confidence": 70,
                    "description": f"Two peaks near ${p1:.2f} suggest bearish reversal",
                })
                break

        # Look for two troughs within tol%
        trough1_idx = np.argmin(lows[i - window:i]) + (i - window)
        trough2_idx = np.argmin(lows[i - half:i]) + (i - half)
        if trough1_idx != trough2_idx:
            t1, t2 = lows[trough1_idx], lows[trough2_idx]
            if t1 > 0 and abs(t1 - t2) / t1 < tol:
                result.append({
                    "name": "Double Bottom",
                    "type": "bullish",
                    "confidence": 70,
                    "description": f"Two troughs near ${t1:.2f} suggest bullish reversal",
                })
                break

    return result[:1]  # at most one double top/bottom


def _head_and_shoulders(df: pd.DataFrame) -> List[Dict]:
    """
    Audit fix H13: the old "split the last 60 bars into thirds, compare
    max of each" detector was far too loose — it would fire any time the
    middle third happened to contain a single tall bar, regardless of
    whether that bar was actually a confirmed swing pivot or whether the
    shoulders were structurally equivalent. The replacement uses 3-bar
    pivot detection and requires three consecutive swing highs (lows for
    inverse) where the middle is the head, flanked by shoulders within 5%
    of each other. Also enforces that the head sits between the two
    shoulders time-wise and that there's a valid neckline (intervening
    trough between the shoulders).
    """
    if len(df) < 60:
        return []

    window = df.tail(60)
    highs = window["High"].values
    lows = window["Low"].values
    n = len(highs)
    k = 3  # 3-bar pivot confirmation

    pivot_highs: List[int] = []
    pivot_lows: List[int] = []
    for i in range(k, n - k):
        if highs[i] == max(highs[i - k:i + k + 1]):
            pivot_highs.append(i)
        if lows[i] == min(lows[i - k:i + k + 1]):
            pivot_lows.append(i)

    # --- Head & Shoulders (bearish): last 3 pivot highs, middle tallest ---
    if len(pivot_highs) >= 3:
        l_i, m_i, r_i = pivot_highs[-3], pivot_highs[-2], pivot_highs[-1]
        lp, mp, rp = highs[l_i], highs[m_i], highs[r_i]
        # Head must clearly exceed both shoulders; shoulders within 5% of each other;
        # pivots must be time-ordered (already guaranteed by list order).
        head_tol = 0.03
        shoulder_sym = 0.05
        if (mp > lp * (1 + head_tol) and mp > rp * (1 + head_tol)
                and abs(lp - rp) / max(lp, 1e-9) < shoulder_sym):
            # Require a neckline trough between the two shoulders to rule out
            # random three-peak clusters with no intervening pullback.
            troughs_between = [i for i in pivot_lows if l_i < i < r_i]
            if troughs_between:
                return [{
                    "name": "Head & Shoulders",
                    "type": "bearish",
                    "confidence": 65,
                    "description": f"Head at ${mp:.2f}, shoulders ~${(lp + rp) / 2:.2f} — bearish reversal pattern",
                }]

    # --- Inverse Head & Shoulders (bullish): last 3 pivot lows, middle lowest ---
    if len(pivot_lows) >= 3:
        l_i, m_i, r_i = pivot_lows[-3], pivot_lows[-2], pivot_lows[-1]
        lt, mt, rt = lows[l_i], lows[m_i], lows[r_i]
        head_tol = 0.03
        shoulder_sym = 0.05
        if (mt < lt * (1 - head_tol) and mt < rt * (1 - head_tol)
                and abs(lt - rt) / max(abs(lt), 1e-9) < shoulder_sym):
            peaks_between = [i for i in pivot_highs if l_i < i < r_i]
            if peaks_between:
                return [{
                    "name": "Inverse Head & Shoulders",
                    "type": "bullish",
                    "confidence": 65,
                    "description": f"Head at ${mt:.2f}, shoulders ~${(lt + rt) / 2:.2f} — bullish reversal pattern",
                }]

    return []


def _triangle(df: pd.DataFrame) -> List[Dict]:
    if len(df) < 20:
        return []
    recent = df.tail(20)
    highs = recent["High"].values
    lows = recent["Low"].values

    high_slope = np.polyfit(range(len(highs)), highs, 1)[0]
    low_slope = np.polyfit(range(len(lows)), lows, 1)[0]

    if high_slope < -0.01 and abs(low_slope) < 0.005:
        return [{"name": "Descending Triangle", "type": "bearish", "confidence": 60,
                 "description": "Descending highs with flat lows — bearish continuation"}]
    if low_slope > 0.01 and abs(high_slope) < 0.005:
        return [{"name": "Ascending Triangle", "type": "bullish", "confidence": 60,
                 "description": "Ascending lows with flat highs — bullish continuation"}]
    if high_slope < -0.005 and low_slope > 0.005:
        return [{"name": "Symmetrical Triangle", "type": "neutral", "confidence": 55,
                 "description": "Converging highs and lows — breakout imminent"}]
    return []


def _flag(df: pd.DataFrame) -> List[Dict]:
    if len(df) < 25:
        return []
    pole = df.iloc[-25:-10]
    flag_body = df.iloc[-10:]

    pole_move = (pole["Close"].iloc[-1] - pole["Close"].iloc[0]) / pole["Close"].iloc[0]
    flag_range = (flag_body["High"].max() - flag_body["Low"].min()) / flag_body["Close"].mean()

    if pole_move > 0.05 and flag_range < 0.03:
        return [{"name": "Bull Flag", "type": "bullish", "confidence": 65,
                 "description": f"Sharp {pole_move:.1%} rise followed by tight consolidation — bullish continuation"}]
    if pole_move < -0.05 and flag_range < 0.03:
        return [{"name": "Bear Flag", "type": "bearish", "confidence": 65,
                 "description": f"Sharp {abs(pole_move):.1%} decline followed by tight consolidation — bearish continuation"}]
    return []


def _golden_death_cross(df: pd.DataFrame) -> List[Dict]:
    if "SMA_50" not in df.columns or "SMA_200" not in df.columns:
        return []
    if len(df) < 3:
        return []

    sma50 = df["SMA_50"].dropna()
    sma200 = df["SMA_200"].dropna()
    if len(sma50) < 2 or len(sma200) < 2:
        return []

    # Align by index
    common = sma50.index.intersection(sma200.index)
    if len(common) < 2:
        return []

    s50 = sma50[common]
    s200 = sma200[common]

    curr_above = s50.iloc[-1] > s200.iloc[-1]
    prev_above = s50.iloc[-2] > s200.iloc[-2]

    if curr_above and not prev_above:
        return [{"name": "Golden Cross", "type": "bullish", "confidence": 75,
                 "description": "SMA50 crossed above SMA200 — strong long-term bullish signal"}]
    if not curr_above and prev_above:
        return [{"name": "Death Cross", "type": "bearish", "confidence": 75,
                 "description": "SMA50 crossed below SMA200 — strong long-term bearish signal"}]
    return []
