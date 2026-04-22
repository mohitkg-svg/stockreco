"""
Backtest-calibrated confidence weights — SKELETON.

The signal generator currently uses hand-tuned scoring weights (e.g.
"+15 for breakout above resistance, +10 for RSI in 50-75"). These are
opinions, not measurements. This module is the place to replace them with
weights *learned* from historical performance per (strategy, timeframe).

Workflow (not yet wired into runtime — that's intentional, see "Why a
skeleton" below):

    1. For each strategy in services.strategies, run backtester.run_backtest
       across the watchlist over N years of bars.
    2. Group resulting trades by (strategy, timeframe). Compute realized
       expectancy = win_rate × avg_win − (1−win_rate) × avg_loss.
    3. Normalize expectancy across strategies → weight ∈ [0, 1].
    4. Persist to disk (JSON) keyed by (strategy, timeframe).
    5. signal_generator.generate_signal() looks up the weight at scoring
       time and multiplies the strategy's contribution by it.

Why a skeleton: shipping calibrated weights without a holdout/regime split
is worse than hand-tuning — overfitting to the last 2y of bull market.
This file pins the API surface and the "TODO" so the next iteration starts
from a contract instead of a blank file.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# Default uniform weight — equivalent to "no calibration" (the current
# behavior). Override per (strategy, timeframe) by running calibrate().
_DEFAULT_WEIGHT = 1.0
_WEIGHTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "calibrated_weights.json",
)

_cache: Dict[Tuple[str, str], float] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not os.path.exists(_WEIGHTS_PATH):
        return
    try:
        with open(_WEIGHTS_PATH) as f:
            raw = json.load(f)
        for k, v in raw.items():
            try:
                strat, tf = k.split("|", 1)
                _cache[(strat, tf)] = float(v)
            except (ValueError, TypeError):
                continue
        logger.info(f"calibrated_weights: loaded {len(_cache)} entries")
    except Exception as e:
        logger.warning(f"calibrated_weights: load failed: {e}")


def get_weight(strategy: str, timeframe: str) -> float:
    """Return calibrated weight ∈ [0, 1+]. Defaults to 1.0 (no effect)."""
    _load()
    return _cache.get((strategy, timeframe), _DEFAULT_WEIGHT)


def calibrate(tickers: list[str], lookback_years: int = 2) -> Dict[Tuple[str, str], float]:
    """
    Run backtests across `tickers`, derive expectancy per (strategy, timeframe),
    write to disk, and return the weights map.

    NOT IMPLEMENTED YET — see module docstring. This stub raises so we don't
    silently use uncalibrated weights labeled as calibrated.
    """
    raise NotImplementedError(
        "calibrate() is a TODO — needs holdout split + regime stratification "
        "before it's safe to feed back into signal generation."
    )
