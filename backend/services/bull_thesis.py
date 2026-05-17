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
from typing import Any, Dict, Optional
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
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(ticker, timeframe)
    except Exception as e:
        logger.warning(f"bull_thesis fetch failed for {ticker}: {e}")
        return None
    if df.empty or len(df) < 14:
        return None

    price = float(df["Close"].iloc[-1])
    atr = float(df["High"].iloc[-14:].subtract(df["Low"].iloc[-14:]).mean()) if len(df) >= 14 else price * 0.02
    
    try:
        from services.ml_scorer import predict_winrate
        prob = predict_winrate(ticker, {"signal_type": "BUY", "confidence": 50})
        if prob is None or prob <= 0.55:
            return None
    except Exception as e:
        logger.warning(f"bull_thesis ML scoring failed for {ticker}: {e}")
        return None

    stop = round(price - 1.5 * atr, 2)
    t1 = round(price + 1.5 * atr, 2)
    t2 = round(price + 2.5 * atr, 2)
    t3 = round(price + 4.0 * atr, 2)

    return {
        "ticker": ticker.upper(),
        "timeframe": timeframe,
        "signal_type": "BUY",   # consumed as direction by options_analyzer
        "confidence": int(prob * 100),
        "entry": round(price, 2),
        "stop_loss": round(stop, 2),
        "target1": float(t1),
        "target2": float(t2),
        "target3": float(t3),
        "reasoning": f"🤖 ML Bull Thesis: P(win)={prob:.2f}",
        "patterns": "[]",
        "strategy": "Pure ML (calls probe)",
        "source": "bull_thesis",
    }
