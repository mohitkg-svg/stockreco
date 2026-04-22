"""
Bear-thesis builder for non-BUY watchlist tickers.

When the standard signal engine doesn't produce a BUY (i.e. SELL or NEUTRAL),
we still want to know if a long-PUT play makes sense. This module synthesizes a
SELL-shaped signal dict (entry / stop / target1-3 / confidence + reasoning) by:

  1. Using the latest 1d data and indicators
  2. Locating the nearest resistance above current price → that's the bear stop
  3. Locating supports below price → become T1, T2, T3 (capped at SMA200 / major lows)
  4. Scoring bearish technical conviction (RSI, MACD, EMA stack, trend, ATR room)

The output plugs straight into `options_analyzer.suggest_options_for_signal`
with direction='SELL' to find PUT contracts that meet the existing R:R filter.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import json
import logging

import pandas as pd

from services.data_fetcher import fetch_ohlcv
from services.indicators import compute_indicators
from services.support_resistance import swing_levels, pivot_points
from services.fibonacci import compute_fib_levels

logger = logging.getLogger(__name__)


def _safe(v) -> Optional[float]:
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except (TypeError, ValueError):
        return None


def build_bear_thesis(ticker: str, timeframe: str = "1d") -> Optional[Dict[str, Any]]:
    """
    Returns a SELL-shaped signal dict for `ticker` on `timeframe`, or None if
    insufficient data / structurally bullish (so puts make no sense).

    Confidence semantics here are *bear conviction*, not the same scale as the
    long-only generator — but we keep the 0-100 range so options_analyzer and
    the auto-trader can use the same threshold.
    """
    try:
        df = fetch_ohlcv(ticker, timeframe)
    except Exception as e:
        logger.warning(f"bear_thesis fetch failed for {ticker}: {e}")
        return None
    if df.empty or len(df) < 60:
        return None

    df = compute_indicators(df)
    last = df.iloc[-1]
    price = _safe(last.get("Close"))
    if not price:
        return None
    atr = _safe(last.get("ATR_14")) or price * 0.02

    rsi = _safe(last.get("RSI_14"))
    macd_hist = _safe(last.get("MACDh_12_26"))
    sma20 = _safe(last.get("SMA_20"))
    sma50 = _safe(last.get("SMA_50"))
    sma200 = _safe(last.get("SMA_200"))
    ema9 = _safe(last.get("EMA_9"))
    ema21 = _safe(last.get("EMA_21"))
    bb_upper = _safe(last.get("BBU_20"))
    bb_lower = _safe(last.get("BBL_20"))

    # ---- Score bear conviction (0-100) -----------------------------------
    conviction = 30  # baseline (most stocks aren't shortable)
    reasons: List[str] = []

    if rsi is not None:
        if rsi > 70:
            conviction += 20
            reasons.append(f"RSI overbought at {rsi:.0f} → mean-reversion target")
        elif rsi > 60:
            conviction += 8
            reasons.append(f"RSI elevated at {rsi:.0f}")
        elif rsi < 40:
            conviction += 5
            reasons.append(f"RSI weak at {rsi:.0f} → continued downside likely")

    if macd_hist is not None:
        if macd_hist < 0:
            conviction += 12
            reasons.append("MACD histogram negative — momentum down")
        else:
            conviction -= 5  # bullish momentum disqualifies short

    # Trend stack
    if sma50 and sma200 and sma50 < sma200:
        conviction += 10
        reasons.append("SMA50 < SMA200 (death-cross territory) — primary trend down")
    if sma50 and price < sma50:
        conviction += 8
        reasons.append("Price below SMA50")
    if sma200 and price < sma200:
        conviction += 8
        reasons.append("Price below SMA200 — bear-market structure")
    if ema9 and ema21 and ema9 < ema21:
        conviction += 5
        reasons.append("Short-term EMAs rolling over (EMA9 < EMA21)")

    # Bollinger position
    if bb_upper and price >= bb_upper * 0.99:
        conviction += 8
        reasons.append(f"Price at/above upper Bollinger band (${bb_upper:.2f}) — overextended")

    # Lower-high pattern in last 30 bars
    if len(df) >= 30:
        recent_highs = df["High"].iloc[-30:].rolling(5).max().dropna()
        if len(recent_highs) >= 10:
            first_half_max = recent_highs.iloc[: len(recent_highs) // 2].max()
            second_half_max = recent_highs.iloc[len(recent_highs) // 2 :].max()
            if second_half_max < first_half_max * 0.985:
                conviction += 7
                reasons.append("Lower-high structure forming on recent swing tops")

    conviction = max(0, min(95, conviction))

    if conviction < 40:
        # Stock is structurally not weak enough for a put play.
        return None

    # ---- Locate stop above price (nearest resistance) --------------------
    resistances: List[float] = []
    try:
        for lvl in swing_levels(df, window=5):
            p = lvl.get("price")
            if p and p > price:
                resistances.append(float(p))
    except Exception:
        pass

    try:
        fib = compute_fib_levels(df)
        for r in (fib.get("retracements") or []):
            p = r.get("price")
            if p and p > price:
                resistances.append(float(p))
        for r in (fib.get("extensions") or []):
            p = r.get("price")
            if p and p > price:
                resistances.append(float(p))
    except Exception:
        pass

    try:
        piv = pivot_points(df)
        for k in ("R1", "R2", "R3"):
            v = piv.get(k)
            if v and v > price:
                resistances.append(float(v))
    except Exception:
        pass

    resistances = sorted(set(round(r, 2) for r in resistances))
    # Nearest resistance, but cap stop distance at 1.5×ATR (don't pay too much theta on a wide stop)
    nearest_res = resistances[0] if resistances else (price + 1.5 * atr)
    stop = min(nearest_res, price + 1.5 * atr)
    # Ensure stop is at least 0.5×ATR away (not stuck at the same bar)
    stop = max(stop, price + 0.5 * atr)

    # ---- Locate targets below price (supports) ---------------------------
    supports: List[float] = []
    try:
        for lvl in swing_levels(df, window=5):
            p = lvl.get("price")
            if p and p < price:
                supports.append(float(p))
    except Exception:
        pass

    try:
        fib = compute_fib_levels(df)
        for r in (fib.get("retracements") or []):
            p = r.get("price")
            if p and p < price:
                supports.append(float(p))
    except Exception:
        pass

    if sma50 and sma50 < price:
        supports.append(float(sma50))
    if sma200 and sma200 < price:
        supports.append(float(sma200))

    risk = stop - price  # how far against us
    spread = max(risk * 1.0, atr * 0.7)
    supports = sorted(set(round(s, 2) for s in supports), reverse=True)

    picked: List[float] = []
    for lvl in supports:
        if not picked or (picked[-1] - lvl) >= spread:
            picked.append(lvl)
        if len(picked) == 3:
            break

    t1 = picked[0] if len(picked) >= 1 else round(price - risk * 1.5, 2)
    t2 = picked[1] if len(picked) >= 2 else round(min(price - risk * 2.5, t1 - risk), 2)
    t3 = picked[2] if len(picked) >= 3 else round(min(price - risk * 4.0, t2 - risk), 2)

    return {
        "ticker": ticker.upper(),
        "timeframe": timeframe,
        "signal_type": "SELL",   # consumed as direction by options_analyzer
        "confidence": int(conviction),
        "entry": round(price, 2),
        "stop_loss": round(stop, 2),
        "target1": float(t1),
        "target2": float(t2),
        "target3": float(t3),
        "reasoning": "Bear thesis (synthesized for put-play scan):\n• " + "\n• ".join(reasons),
        "patterns": json.dumps([]),
        "strategy": "Bear thesis (puts probe)",
        "source": "bear_thesis",
    }
