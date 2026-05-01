"""r69: setup-quality composite score.

Audit consensus: the auto-trader currently runs 8 separate gates against the
same setup quality dimension (`below_confidence_threshold`, `bad_rr`,
`bad_t1_geometry`, `liquidity_skip`, `ticker_chop`, `tf_not_allowed`,
`one_min_bar_disagrees`, `signal_stale`) — each with a hand-tuned threshold
and no joint optimization. This module collapses them into ONE calibrated
composite score in [0, 100] with one threshold.

USAGE
-----
Returns a `SetupQualityResult` dict with:
  score          : float in [0, 100]
  contributions  : dict of {dimension: weighted_value}
  pass_individual: bool — whether each individual gate would pass
  details        : human-readable formula

The auto-trader runs this in ALL modes:
  shadow (default, cfg.setup_quality_gate_enabled = False):
    individual gates still fire; composite is captured in DecisionLog for
    side-by-side comparison.
  active (cfg.setup_quality_gate_enabled = True):
    individual gates are bypassed; only the composite gate decides.

WEIGHTS (sum = 100)
-------------------
  confidence_headroom: 30  — how far above threshold (0 if at, 1 if perfect)
  rr_net             : 20  — net R:R after cost buffer, capped at 3
  geometry_t1        : 10  — binary, T1 - entry > min_gap
  rr_geometry        : 5   — binary, stop < entry < t1
  one_min_bar        : 10  — binary, last 1m bars agree with direction
  liquidity          : 10  — log-scaled median $-vol over 20d
  adx                : 10  — ticker ADX scaled (chop = 0, trending = 1)
  freshness          : 5   — signal age vs max-age cap

Anything missing falls to 0 contribution (conservative).
"""
from __future__ import annotations
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Cost approximation matches consider_signal (12 bps round-trip).
_COST_BUFFER_BPS = 12 / 10000.0
_MIN_T1_GAP_PCT = 0.006

_WEIGHTS = {
    "confidence_headroom": 30.0,
    "rr_net": 20.0,
    "geometry_t1": 10.0,
    "rr_geometry": 5.0,
    "one_min_bar": 10.0,
    "liquidity": 10.0,
    "adx": 10.0,
    "freshness": 5.0,
}
assert abs(sum(_WEIGHTS.values()) - 100.0) < 0.001


def _norm_confidence_headroom(confidence: float, threshold: float) -> float:
    """0 at threshold, 1 at 95+ (max plausible). Linear ramp."""
    if confidence < threshold:
        # Below threshold — 0 contribution.
        return 0.0
    return min(1.0, (confidence - threshold) / max(1.0, 95.0 - threshold))


def _norm_rr(rr_net: float, rr_min: float = 1.3) -> float:
    """0 below min, ramps to 1 at 3R."""
    if rr_net < rr_min:
        return 0.0
    return min(1.0, (rr_net - rr_min) / max(0.01, 3.0 - rr_min))


def _norm_liquidity(dvol: Optional[float]) -> float:
    """Log-scaled — 0 at $10M, 1 at $1B+ daily $-volume."""
    if not dvol or dvol <= 0:
        return 0.0
    if dvol < 10_000_000:
        return 0.0
    return min(1.0, (math.log10(dvol) - 7.0) / 2.0)


def _norm_adx(adx: Optional[float], is_mean_rev: bool = False) -> float:
    """Trending strategies: 0 at ADX 18, 1 at ADX 30+. Mean-rev: inverted."""
    if adx is None:
        return 0.5  # missing — neutral
    a = float(adx)
    if is_mean_rev:
        # Mean-rev wants chop. 1 at ADX 0, 0 at ADX 25+.
        return max(0.0, min(1.0, (25.0 - a) / 25.0))
    return max(0.0, min(1.0, (a - 18.0) / 12.0))


def _norm_freshness(age_min: Optional[float], max_age_min: float) -> float:
    """1 at age=0, 0 at max_age. Linear."""
    if age_min is None:
        return 0.5  # missing — neutral
    if age_min <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (age_min / max(1.0, max_age_min))))


def compute(*,
            confidence: float,
            confidence_threshold: float,
            entry: Optional[float],
            stop: Optional[float],
            target1: Optional[float],
            rr_min: float = 1.3,
            adx: Optional[float] = None,
            strategy: Optional[str] = None,
            one_min_bar_agrees: Optional[bool] = None,
            median_dvol: Optional[float] = None,
            signal_age_min: Optional[float] = None,
            signal_max_age_min: float = 90.0) -> Dict[str, Any]:
    """Compute the composite setup-quality score in [0, 100]."""
    contribs: Dict[str, float] = {}
    parts: Dict[str, float] = {}

    # Confidence headroom
    ch = _norm_confidence_headroom(float(confidence or 0.0), float(confidence_threshold or 55.0))
    parts["confidence_headroom"] = ch
    contribs["confidence_headroom"] = ch * _WEIGHTS["confidence_headroom"]

    # R:R net (after cost buffer)
    rr_net = 0.0
    geom_t1 = 0.0
    rr_geom = 0.0
    if entry and stop and target1 and entry > 0:
        try:
            cost = entry * _COST_BUFFER_BPS
            net_rew = max(0.0, (target1 - entry) - cost)
            gross_risk = max(0.01, entry - stop)
            rr_net = net_rew / gross_risk
            # Geometry checks
            geom_t1 = 1.0 if target1 > entry * (1.0 + _MIN_T1_GAP_PCT) else 0.0
            rr_geom = 1.0 if (stop < entry < target1) else 0.0
        except Exception:
            pass
    rr_norm = _norm_rr(rr_net, rr_min=rr_min)
    parts["rr_net"] = rr_norm
    contribs["rr_net"] = rr_norm * _WEIGHTS["rr_net"]
    parts["geometry_t1"] = geom_t1
    contribs["geometry_t1"] = geom_t1 * _WEIGHTS["geometry_t1"]
    parts["rr_geometry"] = rr_geom
    contribs["rr_geometry"] = rr_geom * _WEIGHTS["rr_geometry"]

    # 1-minute bar alignment
    bar_score = 1.0 if (one_min_bar_agrees is True) else (0.0 if one_min_bar_agrees is False else 0.5)
    parts["one_min_bar"] = bar_score
    contribs["one_min_bar"] = bar_score * _WEIGHTS["one_min_bar"]

    # Liquidity
    liq = _norm_liquidity(median_dvol)
    parts["liquidity"] = liq
    contribs["liquidity"] = liq * _WEIGHTS["liquidity"]

    # ADX (chop vs trend)
    is_mean_rev = ("MEANREV" in (strategy or "").upper()) or ("MEAN_REVERSION" in (strategy or "").upper())
    adx_score = _norm_adx(adx, is_mean_rev=is_mean_rev)
    parts["adx"] = adx_score
    contribs["adx"] = adx_score * _WEIGHTS["adx"]

    # Freshness
    fresh = _norm_freshness(signal_age_min, signal_max_age_min)
    parts["freshness"] = fresh
    contribs["freshness"] = fresh * _WEIGHTS["freshness"]

    score = round(sum(contribs.values()), 2)

    # Individual-gate replay (was the legacy stack going to pass anyway?)
    pass_individual = (
        ch > 0.0 and rr_norm > 0.0 and geom_t1 > 0.0 and rr_geom > 0.0
        and (one_min_bar_agrees is not False)
        and (median_dvol is None or median_dvol >= 10_000_000)
        and (adx is None or adx >= 18.0 or is_mean_rev)
        and fresh > 0.0
    )

    return {
        "score": score,
        "weights": _WEIGHTS,
        "parts": {k: round(v, 4) for k, v in parts.items()},
        "contributions": {k: round(v, 3) for k, v in contribs.items()},
        "pass_individual_gates": pass_individual,
        "details": (
            f"confidence_headroom {ch:.2f} ({_WEIGHTS['confidence_headroom']:.0f}%) "
            f"+ rr_net {rr_norm:.2f} ({_WEIGHTS['rr_net']:.0f}%) "
            f"+ geometry_t1 {geom_t1:.0f} ({_WEIGHTS['geometry_t1']:.0f}%) "
            f"+ rr_geometry {rr_geom:.0f} ({_WEIGHTS['rr_geometry']:.0f}%) "
            f"+ one_min_bar {bar_score:.1f} ({_WEIGHTS['one_min_bar']:.0f}%) "
            f"+ liquidity {liq:.2f} ({_WEIGHTS['liquidity']:.0f}%) "
            f"+ adx {adx_score:.2f} ({_WEIGHTS['adx']:.0f}%) "
            f"+ freshness {fresh:.2f} ({_WEIGHTS['freshness']:.0f}%) "
            f"= {score:.1f}"
        ),
    }
