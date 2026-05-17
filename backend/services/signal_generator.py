"""Composite signal synthesis — the central rule-based engine.

Takes a single ticker's OHLCV frame at a single timeframe, runs ~20
analysis modules over it (indicators, S/R, patterns, supply/demand
zones, Fibonacci, gap detection, multi-timeframe alignment, RVOL,
volume profile, sentiment, fundamentals, analyst consensus, ML scorer),
and produces a single `signal` dict consumed by:

  * `routers/analysis.py` — surfaces to the UI
  * `services/auto_trader.consider_signal()` — the auto-trader's entry
    decision, gated on ~25 rule-based filters before submitting a
    bracket order

Public surface:
  * `generate_signal(ticker, timeframe, df)` → signal dict (see schema below)
  * `_apply_backtest_to_signal(signal, df, timeframe)` — blends per-ticker
    backtest "best-strategy" score into the confidence multiplier
  * `get_timeframe_alignment(signals)` — summary helper for the
    multi-TF alignment grid in the UI

Signal dict schema (all keys present on every return; some may be None):
  * `ticker`: uppercase symbol
  * `timeframe`: one of "5m" / "15m" / "30m" / "1h" / "4h" / "1d" / "1mo"
  * `signal_type`: "BUY" / "SELL" / "NEUTRAL"
  * `confidence`: int 0-95 (capped to leave headroom for "perfect" trades)
  * `entry`, `stop_loss`, `target1`, `target2`, `target3`: float prices,
    None on NEUTRAL signals
  * `reasoning`: newline-joined list of human-readable signal contributors
  * `patterns`: JSON-stringified list of detected pattern names
  * `strategy`: free-text label of dominant strategy (e.g.
    "Composite (multi-factor)", "MEANREV", "BREAKOUT")
  * `adx`: current ADX_14 value, surfaced for downstream regime gating
  * Auto-trader path enriches with `backtest_*` keys via
    `_apply_backtest_to_signal` before passing to consider_signal.

Behavior worth knowing (audit-rationale cross-references):
  * **Raw-evidence floor** (r40 #7): rejects signals where the winning
    side scored < 30 raw points — prevents tilt-vote-only signals
    (60/40 split with weak conviction) from clearing the threshold.
  * **Regime-aware scoring**: in chop (ADX < 20), breakout/breakdown
    bonuses are zeroed (r40 #16) so structural noise doesn't stack.
  * **Per-signal `_regime_mult` clamped to [0.7, 1.4]** (r40 #14) before
    being applied — prevents the 14+ multiplicative confidence factors
    from systematically inflating already-correlated names.
  * **T1 R:R floor 1.3** (r40 #20) — rejects entries with insufficient
    edge after costs.

NOT in this module:
  * Auto-trade entry submission (`services/auto_trader.consider_signal`)
  * Position management / trailing stops (`services/position_manager`)
  * Backtester / portfolio backtester (`services/backtester*`)
"""
from typing import Dict, Any, List, Optional
from services.indicators import extract_latest
import pandas as pd


# Tunables live in services/config.py — re-exported here under the
# legacy names so existing call sites keep working.
from services.config import (
    STOP_ATR_MULT_BY_TF as _STOP_ATR_MULTS,
    ADX_CHOP_MAX,
    ADX_TREND_MIN,
)


def _stop_atr_mult(timeframe: str) -> float:
    return _STOP_ATR_MULTS.get(timeframe, 2.0)


def _calibrate_long_stop(
    *,
    price: float,
    atr: float,
    df: pd.DataFrame,
    candidates: List[Optional[float]],
    timeframe: str,
) -> float:
    """
    Pick a long-side stop that:
      • Is the SECOND-tightest valid candidate (drops the noisiest level)
      • Sits at least cfg×ATR below price (timeframe-dependent multiplier)
      • Sits below the most recent 5-bar swing low (structural buffer)
    The two structural guards are MIN-clamps — they only WIDEN the stop, never
    tighten it. Falls back to ATR-only if no structural candidates survive.
    """
    mult = _stop_atr_mult(timeframe)
    atr_floor = price - mult * atr

    # 5-bar swing low — small buffer below to avoid stop-hunt wicks.
    # Postmortem fix H6: clamp distance to ≤ 3×ATR. After a 10% intraday
    # flush + recovery, an unclamped 5-bar low can sit 10%+ below current
    # price; the resulting risk-per-share collapses position sizing to 1
    # share or zero and silently kills the entry on volatile names.
    try:
        swing_lo_5 = float(df["Low"].iloc[-5:].min()) * 0.997
        swing_lo_5 = max(swing_lo_5, price - 3.0 * atr)
    except Exception:
        swing_lo_5 = atr_floor

    valid = [c for c in candidates if c is not None and c < price]
    valid_sorted = sorted(valid, reverse=True)  # tightest first

    if len(valid_sorted) >= 2:
        chosen = valid_sorted[1]
    elif valid_sorted:
        chosen = valid_sorted[0]
    else:
        chosen = atr_floor

    # MIN = furthest from price = most conservative
    stop = min(chosen, atr_floor, swing_lo_5)
    # Hard sanity floor — never let the stop sit ON OR ABOVE price
    stop = min(stop, price - 0.5 * atr)
    return round(stop, 2)


def _calibrate_short_stop(
    *,
    price: float,
    atr: float,
    df: pd.DataFrame,
    candidates: List[Optional[float]],
    timeframe: str,
) -> float:
    """Mirror of _calibrate_long_stop for shorts: stop sits ABOVE price."""
    mult = _stop_atr_mult(timeframe)
    atr_ceiling = price + mult * atr
    try:
        swing_hi_5 = float(df["High"].iloc[-5:].max()) * 1.003
        # Postmortem fix H6 (mirror): clamp swing-high to ≤ 3×ATR above price
        # so a recent spike doesn't bury the short stop.
        swing_hi_5 = min(swing_hi_5, price + 3.0 * atr)
    except Exception:
        swing_hi_5 = atr_ceiling

    valid = [c for c in candidates if c is not None and c > price]
    valid_sorted = sorted(valid)  # tightest first (closest above price)

    if len(valid_sorted) >= 2:
        chosen = valid_sorted[1]
    elif valid_sorted:
        chosen = valid_sorted[0]
    else:
        chosen = atr_ceiling

    # MAX = furthest from price = most conservative
    stop = max(chosen, atr_ceiling, swing_hi_5)
    stop = max(stop, price + 0.5 * atr)
    return round(stop, 2)


def _bucket_diversity_multiplier_for(
    side: str,
    ind: Dict[str, Any],
    patterns: Optional[List[Any]] = None,
    has_breakout: bool = False,
    has_flow_zone: bool = False,
    has_fundamentals: bool = False,
    has_sentiment: bool = False,
) -> float:
    """r96 R2: return the bucket-diversity multiplier when
    cfg.signal_buckets_enabled is True. Safe-default 1.0 on any error or
    when the flag is off — never raises into the signal-gen hot path."""
    try:
        from database import SessionLocal, AutoTraderConfig
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            if not cfg or not bool(getattr(cfg, "signal_buckets_enabled", False)):
                return 1.0
        finally:
            db.close()
        from services.signal_buckets import derive_buckets_from_indicators, diversity_multiplier
        # Best-effort fundamentals/sentiment derivation from the indicator
        # dict — if these keys aren't present, treat as not-fired (conservative).
        if not has_fundamentals:
            has_fundamentals = bool(
                (ind.get("analyst_mean") and float(ind.get("analyst_mean") or 0) <= 2.5)
                or ind.get("earnings_in_3d")
                or ind.get("insider_purchase_30d")
            )
        if not has_sentiment:
            has_sentiment = bool(
                (ind.get("social_score") and abs(float(ind.get("social_score") or 0)) >= 0.5)
                or ind.get("wsb_mentions_24h")
                or ind.get("institutional_flow_5d")
            )
        bf = derive_buckets_from_indicators(
            side=side, ind=ind, pattern_hits=patterns or [],
            has_breakout=has_breakout, has_flow_zone=has_flow_zone,
            has_fundamentals=has_fundamentals, has_sentiment=has_sentiment,
        )
        return float(diversity_multiplier(bf))
    except Exception:
        return 1.0


def _calibrated_weight_for(strategy: str, timeframe: str) -> float:
    """r96 R1: return the calibrated multiplicative weight for this signal's
    (strategy, timeframe) bucket when cfg.calibrated_weights_enabled is True.
    Returns 1.0 (no effect) when the flag is off, when the bucket is
    uncalibrated, or when anything fails — safe-default-to-noop pattern.
    """
    try:
        from database import SessionLocal, AutoTraderConfig
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            if not cfg or not bool(getattr(cfg, "calibrated_weights_enabled", False)):
                return 1.0
        finally:
            db.close()
        from services.calibrated_weights import get_weight
        return float(get_weight(strategy, timeframe))
    except Exception:
        return 1.0


def generate_signal(ticker: str, timeframe: str, df: pd.DataFrame) -> Dict[str, Any]:
    """Compose a directional signal from ~20 analysis inputs.

    Args:
        ticker: uppercase symbol; passed through into the signal dict.
        timeframe: one of "5m" / "15m" / "30m" / "1h" / "4h" / "1d" / "1mo".
            Affects stop-multiplier (`STOP_ATR_MULT_BY_TF`), MTF weighting,
            and which support/resistance series are computed.
        df: OHLCV frame (must have `Open / High / Low / Close / Volume`),
            indexed by timestamp. Indicators are computed inline if not
            already present. Must have ≥ 30 bars or returns NEUTRAL with
            "Insufficient data".

    Returns:
        Signal dict per the module-docstring schema. Always returns a
        valid dict — `_neutral_signal()` is the fail-open fallback for
        every error / data-quality / weak-evidence path.

    Generation flow (high level):
        1. Indicator extraction + S/R + patterns + zones + Fib + gaps + RVOL
        2. Bull/bear evidence accumulation (~50 weighted contributors)
        3. Raw-evidence floor (r40): reject if max(bull, bear) < 30
        4. Direction selection: bull > bear AND confidence ≥ 55%, or vice versa
        5. Regime-aware adjustments (chop kills breakout bonuses, r40)
        6. Multi-timeframe alignment bonus
        7. Target ladder generation (T1/T2/T3) honoring R:R ≥ 1.3 (r40)
        8. ML scorer blend (when not in shadow mode)
        9. `_regime_mult` clamp to [0.7, 1.4] before final confidence
        10. Final cap at 95 (leaves headroom for "perfect" setup recognition)

    Side effects: NONE. Pure function over the input frame.

    Failure modes — every one returns a NEUTRAL signal with a `reason`:
        * Empty / short frame → "Insufficient data"
        * Indicators didn't compute → "No indicator data"
        * No clear direction → "No clear signal conditions"
        * Mixed signals → "Mixed signals — no clear directional bias"
        * Below evidence floor (r40) → "Insufficient evidence (max raw score X < 30)"
        * Bull and bear scores tied → "Mixed signals"

    Performance: O(N × number-of-strategy-checks) per call, dominated by
    the indicator + zone computations. Typically ~30-80ms on a 500-bar
    daily frame.
    """
    if df.empty or len(df) < 30:
        return _neutral_signal(ticker, timeframe, "Insufficient data")

    ind = extract_latest(df)
    if not ind or ind.get("close") is None:
        return _neutral_signal(ticker, timeframe, "No indicator data")

    price = ind["close"]
    reasons = []
    try:
        from services.ml_scorer import predict_winrate
        prob = predict_winrate(ticker, {"signal_type": "BUY", "confidence": 50})
        if prob is None:
            return _neutral_signal(ticker, timeframe, "ML model not ready")
    except Exception as e:
        return _neutral_signal(ticker, timeframe, f"ML scoring failed: {e}")
            
    if prob > 0.55:
        signal_type = "BUY"
    elif prob < 0.45:
        signal_type = "SELL"
    else:
        return _neutral_signal(ticker, timeframe, f"ML conviction too low: P(win)={prob:.2f}")
        
    atr = float(df["High"].iloc[-14:].subtract(df["Low"].iloc[-14:]).mean()) if len(df) >= 14 else price * 0.02
    
    entry = price
    if signal_type == "BUY":
        stop_loss = round(price - 2.0 * atr, 2)
        t1 = round(price + 2.0 * atr, 2)
        t2 = round(price + 4.0 * atr, 2)
        t3 = round(price + 6.0 * atr, 2)
    else:
        stop_loss = round(price + 2.0 * atr, 2)
        t1 = round(price - 2.0 * atr, 2)
        t2 = round(price - 4.0 * atr, 2)
        t3 = round(price - 6.0 * atr, 2)

    from datetime import datetime as _dt_sg, timezone as _tz_sg
    confidence = int(prob * 100)
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "signal_type": signal_type,
        "confidence": confidence,
        "entry": entry,
        "stop_loss": stop_loss,
        "target1": round(float(t1), 2) if t1 else None,
        "target2": round(float(t2), 2) if t2 else None,
        "target3": round(float(t3), 2) if t3 else None,
        "reasoning": f"🤖 ML statistically driven entry: P(win)={prob:.2f}",
        "patterns": "[]",
        "strategy": "Pure ML",
        "adx": None,
        "generated_at": _dt_sg.now(_tz_sg.utc).isoformat(),
    }


def _neutral_signal(ticker: str, timeframe: str, reason: str) -> Dict[str, Any]:
    """Build the canonical NEUTRAL fallback signal.

    Every error / weak-evidence / data-quality path in `generate_signal`
    returns this shape. The `signal_type="NEUTRAL"` and `entry=None`
    fields combine to make the auto-trader's `consider_signal` treat
    these as non-actionable (rejected at gate 3 — non_buy_signal).

    The fixed `confidence=50` is deliberate: it represents "no opinion"
    and ensures these rows don't accidentally pass the
    `confidence ≥ threshold` gate even on misconfigured deployments.

    `reason` flows through to the UI (signal "reasoning" pane) so
    operators can see why a particular ticker was passed over.
    """
    from datetime import datetime as _dt_ns, timezone as _tz_ns
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "signal_type": "NEUTRAL",
        "confidence": 50,
        "entry": None,
        "stop_loss": None,
        "target1": None,
        "target2": None,
        "target3": None,
        "reasoning": reason,
        "patterns": "[]",
        "strategy": "Composite (multi-factor)",
        # r82: include generated_at uniformly so callers can rely on the field.
        "generated_at": _dt_ns.now(_tz_ns.utc).isoformat(),
    }


def get_timeframe_alignment(signals: List[Dict]) -> Dict[str, str]:
    """Summarize signal direction per timeframe for the alignment grid."""
    return {s["timeframe"]: s["signal_type"] for s in signals}
