"""Pure-function helpers extracted from auto_trader.py.

These are the math primitives that don't touch module globals, the
database, or external services. Extracted so they're:
  * Independently unit-testable (no fixture plumbing)
  * A natural home for the future full decomposition

Policy: NO IMPORTS of auto_trader, database, or any service with state.
Only pure math + tiny utility imports.
"""
from __future__ import annotations
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional, Tuple


def signal_idempotency_key(signal: Dict[str, Any]) -> str:
    """Deterministic dedupe hash for a signal: ticker + direction + rounded
    entry/stop/T1 + confidence-bucket + UTC date.

    Postmortem fix H2: yesterday's stale signal that happens to round to the
    same prices as today's fresh high-conviction setup was deduping the new
    entry. Including the day-stamp forces a fresh key each session;
    including the confidence bucket distinguishes a 60-conf chop signal
    from a 90-conf trend signal even when levels rounded identically.
    """
    conf_bucket = int(float(signal.get("confidence") or 0) // 10)
    day_stamp = datetime.utcnow().strftime("%Y%m%d")
    parts = "|".join([
        str(signal.get("ticker", "")).upper(),
        str(signal.get("signal_type", "")),
        f"{round(float(signal.get('entry') or 0), 2):.2f}",
        f"{round(float(signal.get('stop_loss') or 0), 2):.2f}",
        f"{round(float(signal.get('target1') or 0), 2):.2f}",
        str(signal.get("timeframe", "")),
        f"c{conf_bucket}",
        day_stamp,
    ])
    return hashlib.sha1(parts.encode()).hexdigest()[:16]


def clamp_multiplier_stack(
    confidence_mult: float,
    kelly_mult: float,
    calibration_mult: float,
    strategy_mult: float,
    vix_mult: float,
    ceiling: Optional[float] = None,
) -> Tuple[float, float, bool]:
    """Multiply the 5 factors, clamp to ceiling. Returns (raw, clamped, was_clamped).

    Critical-audit fix #1 lived at this shape — five full-at-max factors
    compounded to ~4.7× before the ceiling was added, turning a 2% risk
    cap into 9.4% per trade. Ceiling of 2.0× preserves ~60% of upside
    while hard-capping downside.
    """
    from services.config import RISK_MULT_CEILING
    if ceiling is None:
        ceiling = RISK_MULT_CEILING
    raw = confidence_mult * kelly_mult * calibration_mult * strategy_mult * vix_mult
    clamped = min(raw, ceiling)
    return raw, clamped, raw > ceiling


def confidence_risk_mult(confidence: float, threshold: float,
                          max_mult: Optional[float] = None) -> float:
    """Ramp from 1.0× at threshold to max_mult at 100% confidence. Linear."""
    from services.config import RISK_MAX_CONFIDENCE_MULT
    if max_mult is None:
        max_mult = RISK_MAX_CONFIDENCE_MULT
    if confidence <= threshold or threshold >= 100:
        return 1.0
    conf_headroom = (float(confidence) - threshold) / (100.0 - threshold)
    conf_headroom = max(0.0, min(1.0, conf_headroom))
    return 1.0 + (max_mult - 1.0) * conf_headroom


def kelly_risk_mult(historical_win_rate: Optional[float],
                     avg_reward_risk: Optional[float],
                     min_win_rate: Optional[float] = None,
                     max_mult: Optional[float] = None,
                     fractional: float = 0.25) -> float:
    """Fractional-Kelly risk multiplier. Returns 1.0 when data is missing or
    win rate is below the trust threshold.

    r42 fix #1.7: previously the function applied the *full* Kelly fraction
    (`kelly_edge` ∈ [0, 1]) directly. Empirically, full-Kelly sizing has
    drawdowns that approach the strategy's edge variance — well-known to
    be over-sized for any non-deterministic edge. We multiply by a default
    `fractional=0.25` (quarter-Kelly), which preserves most of the EV with
    a small fraction of the drawdown.

    `fractional` is exposed so tests can verify the math; production should
    leave it at the default unless the operator has clear evidence of
    why-half-Kelly-is-fine for their strategy.
    """
    from services.config import RISK_KELLY_MAX_MULT, RISK_KELLY_MIN_WIN_RATE
    if max_mult is None:
        max_mult = RISK_KELLY_MAX_MULT
    if min_win_rate is None:
        min_win_rate = RISK_KELLY_MIN_WIN_RATE
    if historical_win_rate is None or avg_reward_risk is None:
        return 1.0
    if historical_win_rate < min_win_rate:
        return 1.0
    # Kelly fraction f = W - (1 - W) / R = W - Q/R
    W = float(historical_win_rate) / 100.0
    R = max(0.1, float(avg_reward_risk))
    Q = 1 - W
    kelly_edge = max(0.0, min(1.0, W - Q / R))
    # Fractional-Kelly: scale the edge by `fractional` before mapping to
    # the multiplier headroom. This is the only change vs the prior
    # behavior — same shape, lower amplitude.
    f_edge = max(0.0, min(1.0, kelly_edge * float(fractional)))
    return 1.0 + (max_mult - 1.0) * f_edge


def position_size_by_risk(equity: float, risk_pct: float, risk_per_share: float) -> int:
    """Qty to buy so that stop-out = equity × risk_pct. Floors to 0."""
    if equity <= 0 or risk_pct <= 0 or risk_per_share <= 0:
        return 0
    budget = equity * risk_pct
    return max(0, int(budget / risk_per_share))
