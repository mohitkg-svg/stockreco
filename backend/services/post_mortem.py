"""
Post-mortem analysis for losing auto-trades.

When a bracket-stop closes an auto-trade at a loss, we want to know *why* the
original signal failed. This module performs a deterministic, indicator-driven
review (no LLM): it re-fetches the price path the trade actually walked, then
compares conditions at entry vs at exit to surface concrete failure modes.

Returned structure (stored as JSON on AutoTrade.post_mortem):
{
  "verdict":  short label e.g. "Stop too tight (within 1×ATR)",
  "summary":  one-sentence narrative,
  "findings": [{title, body, severity: "high"|"med"|"low"}, ...],
  "lessons":  ["…", "…"],
  "price_path": [{t, o, h, l, c}, ...]  // trimmed to entry→exit window
}
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from database import AutoTrade, Signal
from services.data_fetcher import fetch_ohlcv
from services.indicators import compute_indicators
from services.support_resistance import swing_levels

logger = logging.getLogger(__name__)


# ---------- Helpers --------------------------------------------------------

def _slice_window(df: pd.DataFrame, t_start: datetime, t_end: datetime) -> pd.DataFrame:
    """Slice df to [t_start, t_end] using its DatetimeIndex (tz-aware safe)."""
    if df.empty:
        return df
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        ts = pd.Timestamp(t_start, tz="UTC").tz_convert(idx.tz)
        te = pd.Timestamp(t_end, tz="UTC").tz_convert(idx.tz)
    else:
        ts, te = pd.Timestamp(t_start), pd.Timestamp(t_end)
    return df[(idx >= ts) & (idx <= te)]


def _row_at_or_before(df: pd.DataFrame, when: datetime) -> Optional[pd.Series]:
    if df.empty:
        return None
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        target = pd.Timestamp(when, tz="UTC").tz_convert(idx.tz)
    else:
        target = pd.Timestamp(when)
    sub = df[idx <= target]
    if sub.empty:
        return df.iloc[0]
    return sub.iloc[-1]


def _safe(v) -> Optional[float]:
    try:
        f = float(v)
        if np.isnan(f) or np.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


# ---------- Core analysis --------------------------------------------------

def analyze_losing_trade(trade: AutoTrade, db: Session) -> Optional[Dict[str, Any]]:
    """
    Run the post-mortem. Returns the result dict (also stored on the row).
    Returns None if data is insufficient.
    """
    if trade.status != "closed_stop" or (trade.realized_pl or 0) >= 0:
        return None  # only losing stops trigger post-mortems

    entry_t = trade.filled_at or trade.opened_at
    exit_t = trade.closed_at or datetime.utcnow()
    if not entry_t:
        return None
    # Pad the window slightly so indicator math has lookback room
    pad = timedelta(days=1)
    window_start = entry_t - pad
    window_end = exit_t + pad

    # Pick a timeframe with enough granularity inside the trade lifetime
    duration = max(timedelta(minutes=30), exit_t - entry_t)
    if duration <= timedelta(hours=8):
        tf = "5m"
    elif duration <= timedelta(days=3):
        tf = "30m"
    elif duration <= timedelta(days=20):
        tf = "1h"
    else:
        tf = "1d"

    try:
        df = fetch_ohlcv(trade.ticker, tf)
        df = compute_indicators(df) if not df.empty else df
    except Exception as e:
        logger.warning(f"post_mortem fetch failed for {trade.ticker} {tf}: {e}")
        return None
    if df.empty or len(df) < 20:
        return None

    # Daily df gives us higher-timeframe trend context
    try:
        daily = compute_indicators(fetch_ohlcv(trade.ticker, "1d"))
    except Exception:
        daily = pd.DataFrame()

    entry_row = _row_at_or_before(df, entry_t)
    exit_row = _row_at_or_before(df, exit_t)
    if entry_row is None or exit_row is None:
        return None

    path = _slice_window(df, window_start, window_end)
    entry_px = _safe(trade.entry_price) or _safe(entry_row.get("Close"))
    stop_px = _safe(trade.stop_loss)
    target1 = _safe(trade.target1)
    if entry_px is None or stop_px is None:
        return None

    findings: List[Dict[str, Any]] = []
    lessons: List[str] = []
    verdict_candidates: List[str] = []

    # --- 1. Stop-distance vs ATR at entry ---
    atr_entry = _safe(entry_row.get("ATR_14")) if "ATR_14" in entry_row else None
    risk = entry_px - stop_px
    if atr_entry and atr_entry > 0:
        atr_mult = risk / atr_entry
        if atr_mult < 1.0:
            findings.append({
                "title": f"Stop placed {atr_mult:.2f}× ATR from entry — too tight",
                "body": (
                    f"Entry ${entry_px:.2f}, stop ${stop_px:.2f} is only "
                    f"${risk:.2f} away while one daily ATR was ${atr_entry:.2f}. "
                    "Normal noise in this name routinely exceeds the stop distance, "
                    "so the trade was statistically likely to be shaken out without "
                    "the underlying setup being invalidated."
                ),
                "severity": "high",
            })
            verdict_candidates.append("Stop too tight (sub-1×ATR)")
            lessons.append("Require stop-to-ATR ratio ≥ 1.0× before opening a position.")
        elif atr_mult < 1.5:
            findings.append({
                "title": f"Stop near 1× ATR ({atr_mult:.2f}×)",
                "body": "Stop was placed inside normal volatility. A 1.5–2.0× ATR buffer would survive routine pullbacks.",
                "severity": "med",
            })

    # --- 2. Volume / momentum confirmation at entry ---
    vol_at_entry = _safe(entry_row.get("Volume"))
    vol_avg = _safe(entry_row.get("VOL_SMA20")) if "VOL_SMA20" in entry_row else None
    if vol_at_entry and vol_avg:
        ratio = vol_at_entry / vol_avg if vol_avg > 0 else 0
        if ratio < 1.0:
            findings.append({
                "title": f"Volume at entry was below average ({ratio:.2f}× SMA20)",
                "body": (
                    "Genuine breakouts demand >1.5× average volume. Low-volume entries "
                    "tend to fade because real institutional interest is absent."
                ),
                "severity": "high",
            })
            verdict_candidates.append("No volume confirmation")
            lessons.append("Reject BUY signals without volume ≥ 1.5× SMA20 at entry.")

    # --- 3. RSI/MACD divergence at entry ---
    rsi_e = _safe(entry_row.get("RSI"))
    rsi_x = _safe(exit_row.get("RSI"))
    if rsi_e is not None:
        if rsi_e > 75:
            findings.append({
                "title": f"RSI was overbought at entry ({rsi_e:.0f})",
                "body": "Bought into late-stage strength — momentum was already exhausted, leaving little room before mean-reversion.",
                "severity": "high",
            })
            verdict_candidates.append("Overbought entry")
        if rsi_x is not None and rsi_e is not None and rsi_x < rsi_e - 10 and rsi_x < 40:
            findings.append({
                "title": f"RSI collapsed during the trade ({rsi_e:.0f} → {rsi_x:.0f})",
                "body": "Momentum unwound steadily — a weakening RSI between entry and stop is consistent with the setup losing its driver, not an isolated wick.",
                "severity": "med",
            })

    macd_e = _safe(entry_row.get("MACD_Hist"))
    if macd_e is not None and macd_e < 0:
        findings.append({
            "title": "MACD histogram was negative at entry",
            "body": "Took a long while short-term momentum was still pointed down — no upward thrust to ride.",
            "severity": "med",
        })
        verdict_candidates.append("Counter-momentum entry")

    # --- 4. Higher-timeframe trend alignment ---
    if not daily.empty:
        d_row = _row_at_or_before(daily, entry_t)
        if d_row is not None:
            close_d = _safe(d_row.get("Close"))
            sma50 = _safe(d_row.get("SMA50"))
            sma200 = _safe(d_row.get("SMA200"))
            if close_d and sma50 and sma200 and close_d < sma50 and close_d < sma200:
                findings.append({
                    "title": "Daily trend was down at entry (price below SMA50 & SMA200)",
                    "body": "Long taken against the dominant trend. Counter-trend longs need outsized confirmation; this one didn't have it.",
                    "severity": "high",
                })
                verdict_candidates.append("Against daily trend")
                lessons.append("Skip BUY signals when daily price is below both SMA50 and SMA200 unless intraday confirmation is exceptional.")
            elif close_d and sma50 and close_d < sma50:
                findings.append({
                    "title": "Daily price was below SMA50 at entry",
                    "body": "Higher-timeframe trend was weak — even if SMA200 was supportive, the medium-term holders were underwater.",
                    "severity": "med",
                })

    # --- 5. Path analysis: did the stop wick or did the trade just decay? ---
    if not path.empty and len(path) >= 3:
        max_high = float(path["High"].max())
        peak_pct = (max_high - entry_px) / entry_px * 100
        if target1 and max_high < target1 * 0.995:
            shortfall_pct = (target1 - max_high) / (target1 - entry_px) * 100 if target1 > entry_px else 0
            findings.append({
                "title": f"Never approached T1 (peak ${max_high:.2f} = {peak_pct:+.2f}% from entry)",
                "body": (
                    f"Price didn't reach even {(100 - shortfall_pct):.0f}% of the way to T1 (${target1:.2f}). "
                    "The setup never produced the expected thrust — likely the breakout structure was already broken before entry."
                ),
                "severity": "high",
            })
            verdict_candidates.append("No follow-through (never reached T1)")
        elif target1 and max_high >= target1:
            findings.append({
                "title": "Hit T1 then reversed into the stop",
                "body": (
                    "Trade reached the first target before failing. Auto-trader trails to break-even on T1, "
                    "but the trail apparently did not catch this turn — investigate the manage-loop cadence "
                    "(60s) vs how fast price reversed."
                ),
                "severity": "med",
            })
            lessons.append("Consider a tighter trail after T1 (e.g., trail by 0.5×ATR rather than fixed break-even).")

        # Detect single-bar gap-down (>2% in one bar) — likely news
        bar_returns = path["Close"].pct_change().dropna()
        if not bar_returns.empty and bar_returns.min() < -0.02:
            worst_bar = bar_returns.idxmin()
            findings.append({
                "title": f"Single-bar drop of {bar_returns.min()*100:.1f}% at {worst_bar}",
                "body": "An outsized down-bar this size during the trade is consistent with a news event or sector shock — the technical setup couldn't anticipate it.",
                "severity": "low",
            })
            verdict_candidates.append("News/event-driven loss")

    # --- 6. Original signal review (was confidence over-stated?) ---
    if trade.signal_id:
        sig = db.query(Signal).filter(Signal.id == trade.signal_id).first()
        if sig:
            findings.append({
                "title": f"Original signal was {sig.signal_type} {sig.confidence:.0f}% on {sig.timeframe} ({sig.strategy or 'composite'})",
                "body": (sig.reasoning or "(no reasoning recorded)").strip(),
                "severity": "low",
            })
            if sig.confidence >= 90:
                lessons.append(
                    f"A {sig.confidence:.0f}% confidence signal still failed — "
                    "treat very high scores with skepticism, especially when several confirmations are correlated."
                )

    # --- 7. Summary line + verdict ---
    loss = trade.realized_pl or 0.0
    pct = ((stop_px - entry_px) / entry_px * 100) if entry_px else 0
    primary = verdict_candidates[0] if verdict_candidates else "Stop hit on normal pullback"
    summary = (
        f"{trade.ticker}: closed at stop for ${loss:.2f} ({pct:+.2f}%). "
        f"Primary cause: {primary}. " + (verdict_candidates[1] if len(verdict_candidates) > 1
                                         else "")
    ).strip()

    # --- 8. Trim price path for charting (cap at ~120 points) ---
    if not path.empty:
        step = max(1, len(path) // 120)
        thinned = path.iloc[::step]
        price_path = [
            {
                "t": int(ts.timestamp()),
                "o": round(float(r["Open"]), 4),
                "h": round(float(r["High"]), 4),
                "l": round(float(r["Low"]), 4),
                "c": round(float(r["Close"]), 4),
            }
            for ts, r in thinned.iterrows()
        ]
    else:
        price_path = []

    result = {
        "verdict": primary,
        "summary": summary,
        "findings": findings,
        "lessons": list(dict.fromkeys(lessons)),  # dedupe preserve order
        "price_path": price_path,
        "timeframe_used": tf,
        "entry_price": entry_px,
        "stop_price": stop_px,
        "target1": target1,
        "generated_at": datetime.utcnow().isoformat(),
    }

    trade.post_mortem = json.dumps(result)
    db.commit()
    logger.info(f"post_mortem written for trade #{trade.id} {trade.ticker}: {primary}")
    return result
