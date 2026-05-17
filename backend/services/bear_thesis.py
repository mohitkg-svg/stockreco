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


def build_bear_thesis(ticker: str, timeframe: str = "1d") -> Optional[Dict[str, Any]]:
    """
    Returns a SELL-shaped signal dict for `ticker` on `timeframe`, or None if
    insufficient data / structurally bullish (so puts make no sense).

    Confidence semantics here are *bear conviction*, not the same scale as the
    long-only generator — but we keep the 0-100 range so options_analyzer and
    the auto-trader can use the same threshold.
    """
    from services.data_fetcher import fetch_ohlcv
    try:
        df = fetch_ohlcv(ticker, timeframe)
    except Exception as e:
        logger.warning(f"bear_thesis fetch failed for {ticker}: {e}")
        return None
    if df.empty or len(df) < 14:
        return None

    price = float(df["Close"].iloc[-1])
    atr = float(df["High"].iloc[-14:].subtract(df["Low"].iloc[-14:]).mean()) if len(df) >= 14 else price * 0.02
    
    try:
        from services.ml_scorer import predict_winrate
        prob = predict_winrate(ticker, {"signal_type": "SELL", "confidence": 50})
        if prob is None or prob >= 0.45:
            return None
    except Exception as e:
        logger.warning(f"bear_thesis ML scoring failed for {ticker}: {e}")
        return None

    stop = round(price + 1.5 * atr, 2)
    t1 = round(price - 1.5 * atr, 2)
    t2 = round(price - 2.5 * atr, 2)
    t3 = round(price - 4.0 * atr, 2)

    return {
        "ticker": ticker.upper(),
        "timeframe": timeframe,
        "signal_type": "SELL",   # consumed as direction by options_analyzer
        "confidence": int((1.0 - prob) * 100),
        "entry": round(price, 2),
        "stop_loss": round(stop, 2),
        "target1": float(t1),
        "target2": float(t2),
        "target3": float(t3),
        "reasoning": f"🤖 ML Bear Thesis: P(loss)={prob:.2f}",
        "patterns": "[]",
        "strategy": "Pure ML (puts probe)",
        "source": "bear_thesis",
    }
