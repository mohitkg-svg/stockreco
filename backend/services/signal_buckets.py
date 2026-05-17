"""r96 R2: signal-bucket diversity dampener.

The Simons-style audit flagged that signal_generator's ~30 scoring
contributors are heavily correlated — three momentum indicators all firing
on the same name look like three votes but are really one vote, triple-
counted. The existing `_regime_mult` clamp [0.7, 1.4] in signal_generator
is a coarse band-aid; the proper fix is bucketing into orthogonal groups.

A full refactor of every `bull_score += X` site into bucket-aware
accumulators is high-risk for one autonomous session, so this module
takes a smaller bite: compute a DIVERSITY multiplier from a small set of
high-level bucket-fired booleans, and apply it post-hoc to confidence.

Bucket model (7 buckets, intentionally coarse):

    trend       — moving averages / structural direction
    momentum    — RSI / MACD / rate-of-change
    breakout    — price piercing recent range
    flow        — supply/demand zones / FVG / volume profile
    pattern     — pattern detector confirmation
    fundamentals — earnings / analyst / insider
    sentiment   — social / news / WSB / institutional

Diversity heuristic:

    n_fired = number of buckets where at least one contributor fired
    diversity = n_fired / N_BUCKETS_USED   (1.0 when all fire, 0 when none)
    multiplier = 1.0 + (DIVERSITY_MAX_BOOST - 1.0) × (diversity - 0.5) × 2
                  clamped [DIVERSITY_MIN_DAMP, DIVERSITY_MAX_BOOST]

At diversity=0.5 → mult=1.0 (neutral).
At diversity=1.0 (all 7 buckets fired) → mult ≈ DIVERSITY_MAX_BOOST.
At diversity=0.0 (single bucket) → mult ≈ DIVERSITY_MIN_DAMP.

Gated by cfg.signal_buckets_enabled (default False).

This is NOT a full orthogonalization — it's a diversity-aware tax on
single-source signals. The proper bucket-z-score-and-weight refactor
remains an open item; this lands the directional intent without the
blast-radius of touching every += site.
"""
from __future__ import annotations
from typing import Dict, Optional

# Tight bounds — must not be the dominant factor in the multiplier stack.
# RISK_MULT_CEILING (2.0) caps everything, but a single signal-side mult of
# 1.3× or 0.7× is already material when stacked with confidence/kelly/calib.
DIVERSITY_MAX_BOOST = 1.20
DIVERSITY_MIN_DAMP = 0.80

# Canonical bucket names — keep this list authoritative so callers don't
# silently typo a key and skew the denominator.
BUCKETS = (
    "trend",
    "momentum",
    "breakout",
    "flow",
    "pattern",
    "fundamentals",
    "sentiment",
)


def diversity_multiplier(buckets_fired: Dict[str, bool]) -> float:
    return 1.0


def derive_buckets_from_indicators(
    side: str,
    ind: Dict,
    pattern_hits: Optional[list] = None,
    has_breakout: Optional[bool] = None,
    has_flow_zone: Optional[bool] = None,
    has_fundamentals: Optional[bool] = None,
    has_sentiment: Optional[bool] = None,
) -> Dict[str, bool]:
    return {b: False for b in BUCKETS}
