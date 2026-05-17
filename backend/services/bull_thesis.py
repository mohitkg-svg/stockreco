"""
Bull-thesis builder for long-CALL probes.

Parallel to bear_thesis.py. Synthesizes a BUY-shaped signal dict for call
plays in two cases the stock auto-trader ignores:

  A) Sub-threshold BUY setups — the signal engine produced a BUY at 65-74%
     confidence but the stock entry threshold (75%) rejected it. A call is
     a cheap way to take the 65-conf directional bet without deploying
     2% stock risk.
  B) Ticker at per-ticker stock cap — the stock slot is full but momentum
     keeps strengthening. A call adds exposure for a small premium without
     blowing past the stock-bucket cap.

Output plugs into options_analyzer.suggest_options_for_signal with
direction='BUY'.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


def _safe(v) -> Optional[float]:
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except (TypeError, ValueError):
        return None


def build_bull_thesis(ticker: str, timeframe: str = "1d") -> Optional[Dict[str, Any]]:
    """
    Returns a BUY-shaped signal dict for `ticker` on `timeframe`, or None if
    insufficient data / structurally bearish.

    Confidence semantics = bull conviction on a 0-100 scale — same shape as
    the signal generator so options_analyzer / auto_trader can use it with
    the same threshold / score gates.
    """
    try:
        df = fetch_ohlcv(ticker, timeframe)
    except Exception as e:
        logger.warning(f"bull_thesis fetch failed for {ticker}: {e}")
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

    # ---- Score bull conviction (0-100) -----------------------------------
    conviction = 30  # baseline (signals neutral)
    reasons: List[str] = []

    if rsi is not None:
        if rsi < 30:
            conviction += 20
            reasons.append(f"RSI oversold at {rsi:.0f} → mean-reversion bounce")
        elif rsi < 40:
            conviction += 8
            reasons.append(f"RSI depressed at {rsi:.0f} — room to run")
        elif rsi > 60:
            conviction += 5
            reasons.append(f"RSI strong at {rsi:.0f} → momentum continuation likely")

    if macd_hist is not None:
        if macd_hist > 0:
            conviction += 12
            reasons.append("MACD histogram positive — momentum up")
        else:
            conviction -= 5  # bearish momentum disqualifies calls

    # Trend stack
    if sma50 and sma200 and sma50 > sma200:
        conviction += 10
        reasons.append("SMA50 > SMA200 (golden-cross territory) — primary trend up")
    if sma50 and price > sma50:
        conviction += 8
        reasons.append("Price above SMA50")
    if sma200 and price > sma200:
        conviction += 8
        reasons.append("Price above SMA200 — bull-market structure")
    if ema9 and ema21 and ema9 > ema21:
        conviction += 5
        reasons.append("Short-term EMAs turning up (EMA9 > EMA21)")

    # Bollinger position — oversold bounce
    if bb_lower and price <= bb_lower * 1.01:
        conviction += 8
        reasons.append(f"Price at/below lower Bollinger band (${bb_lower:.2f}) — oversold")

    # Higher-low pattern in last 30 bars
    if len(df) >= 30:
        recent_lows = df["Low"].iloc[-30:].rolling(5).min().dropna()
        if len(recent_lows) >= 10:
            first_half_min = recent_lows.iloc[: len(recent_lows) // 2].min()
            second_half_min = recent_lows.iloc[len(recent_lows) // 2 :].min()
            if second_half_min > first_half_min * 1.015:
                conviction += 7
                reasons.append("Higher-low structure forming on recent swing bottoms")

    conviction = max(0, min(95, conviction))

    if conviction < 40:
        # Structurally not strong enough for a call play.
        return None

    # ---- Locate stop below price (nearest support) -----------------------
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
        for r in (fib.get("extensions") or []):
            p = r.get("price")
            if p and p < price:
                supports.append(float(p))
    except Exception:
        pass

    try:
        piv = pivot_points(df)
        for k in ("S1", "S2", "S3"):
            v = piv.get(k)
            if v and v < price:
                supports.append(float(v))
    except Exception:
        pass

    supports = sorted(set(round(s, 2) for s in supports), reverse=True)
    # Nearest support, but cap stop distance at 1.5×ATR (don't pay too much theta on a wide stop)
    nearest_sup = supports[0] if supports else (price - 1.5 * atr)
    stop = max(nearest_sup, price - 1.5 * atr)
    # Ensure stop is at least 0.5×ATR below (not stuck on the same bar)
    stop = min(stop, price - 0.5 * atr)

    # ---- Locate targets above price (resistances) ------------------------
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
    except Exception:
        pass

    if sma50 and sma50 > price:
        resistances.append(float(sma50))
    if sma200 and sma200 > price:
        resistances.append(float(sma200))

    risk = price - stop  # how far against us (price drops to stop)
    spread = max(risk * 1.0, atr * 0.7)
    resistances = sorted(set(round(r, 2) for r in resistances))

    picked: List[float] = []
    for lvl in resistances:
        if not picked or (lvl - picked[-1]) >= spread:
            picked.append(lvl)
        if len(picked) == 3:
            break

    t1 = picked[0] if len(picked) >= 1 else round(price + risk * 1.5, 2)
    t2 = picked[1] if len(picked) >= 2 else round(max(price + risk * 2.5, t1 + risk), 2)
    t3 = picked[2] if len(picked) >= 3 else round(max(price + risk * 4.0, t2 + risk), 2)

    return {
        "ticker": ticker.upper(),
        "timeframe": timeframe,
        "signal_type": "BUY",   # consumed as direction by options_analyzer
        "confidence": int(conviction),
        "entry": round(price, 2),
        "stop_loss": round(stop, 2),
        "target1": float(t1),
        "target2": float(t2),
        "target3": float(t3),
        "reasoning": "Bull thesis (synthesized for call-play scan):\n• " + "\n• ".join(reasons),
        "patterns": json.dumps([]),
        "strategy": "Bull thesis (calls probe)",
        "source": "bull_thesis",
    }
