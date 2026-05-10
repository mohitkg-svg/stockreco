"""r53 Tier-3 C: regime-conditional signal-stack switching.

Current code applies regime adjustments to *sizing* (`adaptive_risk_multiplier`,
`regime_multiplier`), but the *signal stack itself* is universal — same
ADX/Fib/MACD rules fire in chop and trend. Empirically, breakout
strategies have negative expectancy in low-ADX regimes.

This module classifies the current SPY regime as TREND / CHOP / HIGH_VOL
and exposes `allowed_strategies(regime)` so consider_signal can
short-circuit signals from strategies that don't transfer to the
current regime.

Hysteresis: the regime classification requires the new regime to
persist for ≥3 consecutive 5-min ticks before flipping. This prevents
rapid regime-flapping at the boundaries (ADX 19→21→19) which would
disable / re-enable strategies multiple times per session.

Mode: gated by `cfg.factor_strategies_enabled` (already exists). The
`allowed_strategies()` set is empty when no regime data is available
(fail-open: don't gate when we don't know the regime).
"""
from __future__ import annotations
import logging
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Hysteresis state (process-local; shared across instances would be
# nice-to-have but not required — each instance reaches consensus
# independently within ~15 min).
_REGIME_HISTORY: list = []
_HYSTERESIS_TICKS = 3
_REGIME_CACHE: Dict[str, Tuple[str, float]] = {}  # → (regime, expiry_ts)
_REGIME_CACHE_TTL = 60.0  # 1-min recompute cap


def classify_regime() -> Optional[str]:
    """Returns one of: "TREND", "CHOP", "HIGH_VOL", or None when data is
    unavailable (fail-open).

    TREND:    SPY ADX_14 ≥ 25 AND VIX < 22
    CHOP:     SPY ADX_14 < 20 AND VIX < 22
    HIGH_VOL: VIX ≥ 22 (overrides ADX)

    Hysteresis: actual regime only flips when the new classification has
    held for `_HYSTERESIS_TICKS` consecutive calls.
    """
    import time
    cache_key = "current"
    cached = _REGIME_CACHE.get(cache_key)
    now_t = time.time()
    if cached and now_t < cached[1]:
        return cached[0]

    raw = _classify_raw()
    # Hysteresis: append + check last N
    _REGIME_HISTORY.append(raw)
    while len(_REGIME_HISTORY) > 10:
        _REGIME_HISTORY.pop(0)
    persisted: Optional[str] = None
    if len(_REGIME_HISTORY) >= _HYSTERESIS_TICKS:
        last_n = _REGIME_HISTORY[-_HYSTERESIS_TICKS:]
        if all(r == last_n[0] for r in last_n) and last_n[0] is not None:
            persisted = last_n[0]
    # If hysteresis-confirmed, that's the live regime; otherwise fall
    # through to whatever was previously cached, else raw, else None.
    chosen = persisted or (cached[0] if cached else None) or raw
    _REGIME_CACHE[cache_key] = (chosen, now_t + _REGIME_CACHE_TTL)
    return chosen


def _classify_raw() -> Optional[str]:
    """One-shot classification without hysteresis."""
    # r82: prior code did `from services.market_context import vix as _vix` and
    # `from services.indicators import adx` — neither symbol exists. Both
    # ImportErrors were swallowed by the broad except → the regime classifier
    # silently returned None on every call → TREND/CHOP/HIGH_VOL stack-switching
    # was entirely dead and `is_strategy_allowed_in_regime` always fail-opened.
    try:
        from services.market_context import current_vix as _vix
        v = _vix()
        if v is not None and v >= 22:
            return "HIGH_VOL"
    except Exception:
        v = None
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators as _ci
        df = fetch_ohlcv("SPY", "1d")
        if df is None or len(df) < 30:
            return None
        df_ind = _ci(df)
        if "ADX_14" not in df_ind.columns:
            return None
        adx_val = df_ind["ADX_14"].iloc[-1]
        if adx_val is None or (hasattr(adx_val, "__float__") is False):
            return None
        import math as _m
        spy_adx = float(adx_val)
        if _m.isnan(spy_adx):
            return None
    except Exception:
        return None
    if spy_adx >= 25:
        return "TREND"
    if spy_adx < 20:
        return "CHOP"
    # Transition zone (20 ≤ ADX < 25): keep prior regime via hysteresis.
    return None


# Strategy → regime allowlist. Empty set means "always allowed".
_STRATEGY_REGIMES = {
    # Trend-following / breakout — only in TREND.
    "BollingerBreakout": {"TREND"},
    "Breakout": {"TREND"},
    "FibExtension": {"TREND"},
    "Gap & Go": {"TREND"},
    "EMA Pullback": {"TREND"},
    "MACD Cross": {"TREND"},
    "VWAP Reclaim": {"TREND"},
    "FVG Pullback": {"TREND"},
    # Mean-reversion / range-bound — only in CHOP.
    "MeanReversion": {"CHOP"},
    "VWAPRevert": {"CHOP"},
    "S/R Bounce": {"CHOP"},
    "Bollinger Mean Revert": {"CHOP"},
    # Event-driven — works in any regime.
    "PEAD": {"TREND", "CHOP", "HIGH_VOL"},
    "Earnings Drift": {"TREND", "CHOP", "HIGH_VOL"},
    # Composite — always allowed (it's the rule-engine fallback).
    "Composite": {"TREND", "CHOP", "HIGH_VOL"},
    "Composite (multi-factor)": {"TREND", "CHOP", "HIGH_VOL"},
}


def is_strategy_allowed_in_regime(strategy_name: Optional[str], regime: Optional[str]) -> bool:
    """True when the strategy is allowed in the given regime (or when
    we don't have enough data to gate)."""
    if not strategy_name or not regime:
        return True  # fail-open
    allowed = _STRATEGY_REGIMES.get(strategy_name)
    if allowed is None:
        return True  # unknown strategy — don't gate
    return regime in allowed


def regime_status() -> dict:
    """Operator-facing summary."""
    return {
        "current_regime": classify_regime(),
        "history": _REGIME_HISTORY[-5:],
        "hysteresis_ticks": _HYSTERESIS_TICKS,
        "strategy_regimes": {k: sorted(v) for k, v in _STRATEGY_REGIMES.items()},
    }
