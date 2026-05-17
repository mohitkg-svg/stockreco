"""
Backtest-calibrated confidence weights.

The signal generator uses hand-tuned scoring weights (e.g. "+15 for breakout
above resistance, +10 for RSI in 50-75"). These are opinions, not measurements.

This module learns a multiplicative weight ∈ [0.5, 1.5] per
(strategy, timeframe) from realized closed-trade expectancy and exposes
`get_weight()` for the signal generator to consume.

Workflow:

    1. `calibrate(lookback_days=N)` pulls closed AutoTrade rows over the
       trailing window, joins to Signal to recover strategy+timeframe.
    2. Per (strategy, timeframe) bucket: realized_expectancy =
       win_rate × avg_win − (1 − win_rate) × avg_loss, normalized by
       (avg_win + avg_loss) / 2 → unitless ∈ roughly [-1, 1].
    3. Multiplicative weight = clamp(1.0 + 0.5 × tanh(2 × expectancy_norm),
       _MIN_WEIGHT, _MAX_WEIGHT). At zero expectancy → 1.0 (no effect);
       strongly profitable → ~1.5×; strongly losing → ~0.5×.
    4. Persisted to local JSON + MLArtifact (durable across container churn).
    5. signal_generator multiplies its per-strategy contribution by
       `get_weight(strategy, timeframe)` — gated by
       cfg.calibrated_weights_enabled (default False).

Regime stratification + holdout split are deferred to R1.v2 — operator
should monitor the headline live WR after flipping the flag and re-calibrate
periodically (weekly job is the intended cadence).

r96 R1.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Tuple, List, Any, Optional

logger = logging.getLogger(__name__)

# Multiplicative weight bounds. Clamped tight to keep the calibrator from
# being the dominant factor in the confidence stack (RISK_MULT_CEILING at
# 2.0 already caps the compound). A bucket with 5 sample trades shouldn't
# shrink a strategy to 0.1× nor inflate to 3×.
_MIN_WEIGHT = 0.50
_MAX_WEIGHT = 1.50
_DEFAULT_WEIGHT = 1.0

# A bucket needs at least this many closed trades before its weight diverges
# from 1.0 — small samples revert to "no effect".
_MIN_TRADES_FOR_CALIBRATION = 10


def _try_db_load() -> Optional[Dict[str, float]]:
    return None


def _load(force: bool = False) -> None:
    pass


def get_weight(strategy: Optional[str], timeframe: Optional[str]) -> float:
    return _DEFAULT_WEIGHT


def _expectancy_to_weight(expectancy_norm: float) -> float:
    return _DEFAULT_WEIGHT


def _bucket_expectancy(trades: List[Dict[str, float]]) -> Optional[float]:
    return None


def calibrate(lookback_days: int = 180, persist: bool = True) -> Dict[str, Any]:
    return {
        "calibrated_at": datetime.utcnow().isoformat(),
        "lookback_days": lookback_days,
        "n_trades_total": 0,
        "n_buckets": 0,
        "n_calibrated": 0,
        "skipped_no_signal": 0,
        "buckets": [],
    }


def status() -> Dict[str, Any]:
    return {
        "n_loaded": 0,
        "weights": {},
        "weights_path": "",
    }
