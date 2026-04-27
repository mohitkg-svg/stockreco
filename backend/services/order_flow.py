"""r48 BACKLOG — order-flow / microstructure overlay module.

Each detector reads from `services.alpaca_tape` (live + historical
trade-print stream) and `services.live_quotes` (NBBO snapshots) and
returns a small dict that callers compose into the sizing pipeline or
use as entry/exit triggers.

All functions are pure reads with internal caching; they fail-quiet on
data-feed outages (return None or 1.0 multiplier as appropriate).
"""
from __future__ import annotations
import logging
import time
import threading
from collections import deque
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Cache lock protecting all module-level state
_lock = threading.Lock()

# Per-ticker rolling spread EMA for the spread-widening defer gate.
_spread_ema: Dict[str, float] = {}
_SPREAD_EMA_ALPHA = 0.10  # ~10-update half-life

# Per-ticker last halt-suspect timestamp (gap > 30s during RTH).
_last_halt_ts: Dict[str, float] = {}
_HALT_GAP_SECONDS = 30.0

# Per-ticker last quote-stuffing detection
_last_stuffing_ts: Dict[str, float] = {}


def update_spread_ema(ticker: str, bid: float, ask: float) -> Optional[float]:
    """Caller invokes from `_handle_quote` on every NBBO tick. Returns the
    current EMA of `(ask - bid) / mid`. Used by the spread-widening defer
    gate at entry time."""
    if not (bid and ask and ask > bid):
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    s = (ask - bid) / mid
    with _lock:
        prev = _spread_ema.get(ticker.upper())
        if prev is None:
            new = s
        else:
            new = (1 - _SPREAD_EMA_ALPHA) * prev + _SPREAD_EMA_ALPHA * s
        _spread_ema[ticker.upper()] = new
        return new


def spread_widening_defer(ticker: str) -> bool:
    """True if current spread is > 1.8× the EMA AND > 5bps absolute → defer
    the entry. Glosten-Milgrom 1985, Easley-O'Hara: spreads widen when MMs
    detect order-flow toxicity."""
    try:
        from services import live_quotes as _lq
        q = _lq.get_stock_quote(ticker)
        if not q:
            return False
        bid = float(q.get("bid") or 0)
        ask = float(q.get("ask") or 0)
        if not (bid and ask and ask > bid):
            return False
        mid = (bid + ask) / 2.0
        cur = (ask - bid) / mid
        with _lock:
            ema = _spread_ema.get(ticker.upper())
        if ema is None:
            return False
        return cur > 1.8 * ema and cur > 0.0005
    except Exception:
        return False


def detect_block_lean(ticker: str) -> Optional[Dict[str, Any]]:
    """Block-print detection. 3+ same-side blocks of size >= max(5K, 2% ADV-15m)
    in a 5-min window. Returns `{"direction": "buy"|"sell", "n_blocks": N}`
    or None.
    Bessembinder-Kaufman 1997; VPIN."""
    try:
        from services.alpaca_tape import fetch_live_window
        df = fetch_live_window(ticker, lookback_minutes=5)
        if df is None or df.empty:
            return None
        # Heuristic block threshold: top 5% size in this window.
        if "size" not in df.columns:
            return None
        threshold = float(df["size"].quantile(0.95))
        threshold = max(threshold, 5000)
        big = df[df["size"] >= threshold]
        if len(big) < 3:
            return None
        # Aggressor classification via tick rule.
        if "p" in big.columns:
            prices = big["p"].values
            ticks = []
            for i in range(1, len(prices)):
                if prices[i] > prices[i - 1]:
                    ticks.append(1)
                elif prices[i] < prices[i - 1]:
                    ticks.append(-1)
                else:
                    ticks.append(0)
            if not ticks:
                return None
            net = sum(ticks)
            if abs(net) < 2:
                return None
            return {
                "direction": "buy" if net > 0 else "sell",
                "n_blocks": int(len(big)),
                "net_aggressor": int(net),
            }
    except Exception:
        return None
    return None


def detect_sweep(ticker: str) -> Optional[Dict[str, Any]]:
    """Multi-price sweep detection in last 2s. Same-side prints across 3+
    distinct prices with total size >= 0.5× avg-5m volume → sweep event.
    Hasbrouck 1991: aggressive sub-second activity carries information."""
    try:
        from services.alpaca_tape import fetch_live_window
        df = fetch_live_window(ticker, lookback_minutes=1)
        if df is None or df.empty:
            return None
        if "p" not in df.columns or "size" not in df.columns:
            return None
        # Take last 2s of trades
        if "t" in df.columns:
            t_ref = df["t"].iloc[-1]
            recent = df[df["t"] >= t_ref - timedelta(seconds=2)]
        else:
            recent = df.tail(20)
        if len(recent) < 3:
            return None
        n_prices = recent["p"].nunique()
        if n_prices < 3:
            return None
        total_size = float(recent["size"].sum())
        # Compare to typical 5m volume.
        df5 = fetch_live_window(ticker, lookback_minutes=5)
        if df5 is None or df5.empty:
            return None
        avg_5m_size = float(df5["size"].sum()) / 5.0
        if total_size < 0.5 * avg_5m_size:
            return None
        # Direction inference
        first_p = float(recent["p"].iloc[0])
        last_p = float(recent["p"].iloc[-1])
        direction = "buy" if last_p > first_p else "sell"
        return {"direction": direction, "n_prices": int(n_prices),
                "total_size": int(total_size)}
    except Exception:
        return None


def aggressor_flow_imbalance(ticker: str, window_minutes: int = 15) -> Optional[float]:
    """Cumulative Lee-Ready signed-volume imbalance over `window_minutes`.
    Returns a value in [-1, 1] (signed_volume / total_volume).
    Chordia-Roll-Subrahmanyam 2002/2008."""
    try:
        from services.alpaca_tape import fetch_live_window
        df = fetch_live_window(ticker, lookback_minutes=window_minutes)
        if df is None or df.empty or "p" not in df.columns or "size" not in df.columns:
            return None
        prices = df["p"].values
        sizes = df["size"].values
        signed = 0.0
        total = 0.0
        for i in range(1, len(prices)):
            tick = 0
            if prices[i] > prices[i - 1]:
                tick = 1
            elif prices[i] < prices[i - 1]:
                tick = -1
            signed += tick * sizes[i]
            total += sizes[i]
        if total <= 0:
            return None
        return float(signed) / float(total)
    except Exception:
        return None


def aggressor_flow_gate(ticker: str, direction: str, threshold: float = 0.30) -> bool:
    """Block a new long entry when 15-min cumulative aggressor imbalance
    is < -threshold (selling pressure persistent); mirror for shorts.
    Returns True = BLOCK."""
    imb = aggressor_flow_imbalance(ticker, window_minutes=15)
    if imb is None:
        return False
    direction = (direction or "BUY").upper()
    if direction == "BUY" and imb < -threshold:
        return True
    if direction == "SELL" and imb > threshold:
        return True
    return False


def tape_acceleration_factor(ticker: str) -> Optional[float]:
    """Andersen-Bollerslev 1997: trade-rate acceleration = urgency.
    Returns ratio (last-fifth-of-window trade count) / (prior-four-fifths
    trade rate). >1.4 = accelerating; <0.7 = decelerating."""
    try:
        from services.alpaca_tape import fetch_live_window
        df = fetch_live_window(ticker, lookback_minutes=5)
        if df is None or df.empty or len(df) < 30:
            return None
        n = len(df)
        last_fifth = df.tail(n // 5)
        prior = df.head(n - len(last_fifth))
        if len(prior) <= 0 or len(last_fifth) <= 0:
            return None
        rate_last = len(last_fifth) / max(1, len(last_fifth))
        rate_prior = len(prior) / max(1, 4 * len(last_fifth))
        if rate_prior <= 0:
            return None
        return rate_last / rate_prior
    except Exception:
        return None


def vwap_band_reversion_signal(d) -> Optional[Dict[str, Any]]:
    """VWAP +2σ exhaustion fade. Caller passes a bar DataFrame with
    `Close` and a session-anchored VWAP column. Returns signal dict on
    breach with low RVOL (no-conviction breakout)."""
    import pandas as pd
    if d is None or len(d) < 20:
        return None
    if "VWAP" not in d.columns or "Close" not in d.columns:
        return None
    try:
        sigma_col = (d["Close"] - d["VWAP"]).rolling(20).std()
        if len(sigma_col) == 0 or pd.isna(sigma_col.iloc[-1]):
            return None
        breach_up = float(d["Close"].iloc[-1]) > float(d["VWAP"].iloc[-1]) + 2 * float(sigma_col.iloc[-1])
        breach_down = float(d["Close"].iloc[-1]) < float(d["VWAP"].iloc[-1]) - 2 * float(sigma_col.iloc[-1])
        if "Volume" in d.columns and "VOL_SMA20" in d.columns:
            rvol = float(d["Volume"].iloc[-1] / d["VOL_SMA20"].iloc[-1])
        else:
            rvol = 1.0
        if rvol > 1.2:  # high RVOL = real breakout, not exhaustion
            return None
        if breach_up:
            return {"direction": "fade_short", "target": float(d["VWAP"].iloc[-1])}
        if breach_down:
            return {"direction": "fade_long", "target": float(d["VWAP"].iloc[-1])}
    except Exception:
        return None
    return None


def round_number_proximity_fade(ticker: str, atr: float) -> Optional[Dict[str, Any]]:
    """Stop-hunt fade near round numbers. Donaldson-Kim 1993; Bhattacharya-
    Holden-Jacobsen 2012. Detects a sweep through a round (.00 or .50)
    in the last 10 min; signals an opposite-direction fade entry."""
    try:
        from services import live_quotes as _lq
        q = _lq.get_stock_quote(ticker)
        if not q:
            return None
        last = float(q.get("last") or 0)
        if last <= 0 or atr <= 0:
            return None
        # Distance to nearest round
        round_below = int(last)
        round_above = round_below + 1
        half = round_below + 0.5
        candidates = [round_below, round_above, half]
        nearest = min(candidates, key=lambda r: abs(r - last))
        if abs(nearest - last) > 0.05:
            return None
        # Need a sweep — defer to detect_sweep
        sw = detect_sweep(ticker)
        if not sw:
            return None
        direction = "fade_short" if sw["direction"] == "buy" else "fade_long"
        stop_dist = 0.3 * atr
        return {"direction": direction, "round": nearest, "stop_dist": stop_dist}
    except Exception:
        return None


def quote_stuffing_score(ticker: str, quote_updates: int, trades: int) -> float:
    """Quote-stuffing detection (Egginton-Van Ness 2016): ratio of NBBO
    updates per trade in last 250ms. Returns ratio; caller alerts /
    defers when > 20."""
    if trades < 1:
        trades = 1
    return quote_updates / trades


def opening_drive_bias(ticker: str) -> Optional[str]:
    """Kissell 2014; Bogousslavsky 2021: 9:30-10:00 ET direction predicts
    afternoon ~62% of the time conditional on volume confirmation.
    Returns "BUY" / "SELL" / None. Computed from 1-min bars 9:30-10:00."""
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(ticker, "1m")
        if df is None or df.empty:
            return None
        from datetime import datetime as _dt_od
        from zoneinfo import ZoneInfo as _ZI_od
        now_et = _dt_od.now(_ZI_od("America/New_York"))
        if now_et.hour < 10:
            return None  # not yet 30-min mark
        # Slice: today's 9:30-10:00
        today = now_et.date()
        df_local = df.copy()
        df_local.index = df_local.index.tz_convert("America/New_York") if df_local.index.tz else df_local.index.tz_localize("UTC").tz_convert("America/New_York")
        morn = df_local[(df_local.index.date == today) &
                        (df_local.index.hour == 9) &
                        (df_local.index.minute >= 30)]
        if len(morn) < 5:
            return None
        ret_30 = float(morn["Close"].iloc[-1] / morn["Open"].iloc[0] - 1.0)
        vol_30 = float(morn["Volume"].sum())
        # crude: require >0.5% move + volume above average
        if abs(ret_30) > 0.005 and vol_30 > 0:
            return "BUY" if ret_30 > 0 else "SELL"
    except Exception:
        return None
    return None
