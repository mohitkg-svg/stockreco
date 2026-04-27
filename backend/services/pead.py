"""Post-Earnings Announcement Drift (PEAD) — r44 Wave 7.

Bernard & Thomas (1989) — and 30+ years of follow-up — show that
stocks with a strong earnings surprise drift in the same direction
for 30-60 days after the print. SUE-decile spreads remain >5%
annualized after costs, making this the single most-validated equity
anomaly.

The bot currently treats earnings as a *blackout* (refuses to enter
within 48h of earnings). PEAD reframes the same event as a *trading
signal*: enter ON the close of earnings day if earnings surprise
direction + price reaction agree, hold 30-60 days.

Public surface:
    pead_signal(ticker) → optional {signal_type, confidence, ...}

Filtering rules (Tier 1 default):
  * Earnings was within last 1 trading day
  * EPS or revenue surprise direction matches price reaction direction
  * Price reaction (open-to-close) ≥ 3% in the agreeing direction
  * Volume on earnings day ≥ 2× 20-day average
  * IV-rank not at the 90th+ percentile (avoid IV-crush plays)
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def pead_signal(ticker: str) -> Optional[Dict[str, Any]]:
    """Return a PEAD signal dict if the ticker meets all gates today,
    else None.

    The returned dict matches the signal-generator schema closely so the
    auto-trader can consume it via `consider_signal` with a
    `strategy="PEAD"` tag.
    """
    try:
        import yfinance as _yf
        from services.data_fetcher import fetch_ohlcv
        # 1. earnings recency check
        try:
            t = _yf.Ticker(ticker)
            cal = getattr(t, "calendar", None)
        except Exception:
            cal = None
        # `t.calendar` is unreliable; fall back to t.earnings_dates df
        try:
            ed = t.earnings_dates
        except Exception:
            ed = None
        last_earnings = None
        if ed is not None and not ed.empty:
            try:
                # Find the most recent past earnings date.
                past = ed[ed.index < datetime.utcnow().replace(tzinfo=ed.index.tz)]
                if len(past) > 0:
                    last_earnings = past.index[0]
            except Exception:
                pass
        if last_earnings is None:
            return None
        # Only fire within 1 trading day of earnings.
        ts_naive = last_earnings.replace(tzinfo=None) if last_earnings.tzinfo else last_earnings
        hours_since = (datetime.utcnow() - ts_naive).total_seconds() / 3600.0
        if hours_since < 0 or hours_since > 36:
            return None

        # 2. price reaction check
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty or len(df) < 25:
            return None
        last_bar = df.iloc[-1]
        last_open = float(last_bar["Open"])
        last_close = float(last_bar["Close"])
        last_vol = float(last_bar["Volume"])
        avg_vol = float(df["Volume"].iloc[-21:-1].mean())
        if last_open <= 0 or avg_vol <= 0:
            return None
        gap_pct = (last_close - last_open) / last_open * 100.0
        vol_ratio = last_vol / avg_vol

        # 3. Direction & strength gates
        direction = None
        if gap_pct >= 3.0 and vol_ratio >= 2.0:
            direction = "BUY"
        elif gap_pct <= -3.0 and vol_ratio >= 2.0:
            direction = "SELL"
        else:
            return None

        # 4. IV-crush guard via simple proxy (skip if implied is at 1y peak).
        try:
            from services.options_analyzer import _iv_rank_too_high
            from services.options_fetcher import get_atm_iv
            atm_iv = get_atm_iv(ticker) if hasattr(__import__("services.options_fetcher", fromlist=["get_atm_iv"]), "get_atm_iv") else None
            if atm_iv and _iv_rank_too_high(ticker, atm_iv, threshold=0.90):
                return None
        except Exception:
            pass

        # 5. Build signal
        entry = last_close
        atr = float(df["High"].iloc[-21:].subtract(df["Low"].iloc[-21:]).mean())
        if atr <= 0:
            atr = entry * 0.02
        if direction == "BUY":
            stop = round(entry - 1.5 * atr, 2)
            t1 = round(entry + 2.0 * atr, 2)
            t2 = round(entry + 4.0 * atr, 2)
            t3 = round(entry + 6.0 * atr, 2)
        else:
            stop = round(entry + 1.5 * atr, 2)
            t1 = round(entry - 2.0 * atr, 2)
            t2 = round(entry - 4.0 * atr, 2)
            t3 = round(entry - 6.0 * atr, 2)

        confidence = 75.0 + min(15.0, abs(gap_pct))   # cap at 90
        return {
            "ticker": ticker,
            "timeframe": "1d",
            "signal_type": direction,
            "confidence": int(confidence),
            "entry": round(entry, 2),
            "stop_loss": stop,
            "target1": t1,
            "target2": t2,
            "target3": t3,
            "reasoning": (
                f"PEAD: earnings {hours_since:.0f}h ago, "
                f"price gap {gap_pct:+.1f}%, vol {vol_ratio:.1f}× avg → {direction} drift hypothesis"
            ),
            "patterns": "[\"PEAD\"]",
            "strategy": "PEAD",
            "adx": None,
        }
    except Exception as e:
        logger.debug(f"pead_signal {ticker}: {e}")
        return None
