"""Read-view of auto_trader module state.

Python modules are already singletons, so full class-based encapsulation
of `auto_trader.py`'s globals would be 500+ LOC of `global X` → `self.X`
rewrites with real regression risk. This module provides the ACTUAL
benefit of encapsulation — clean API surface for monitoring + test reset
— without the refactor cost.

Usage:
    from services.auto_trader_state import state_view, reset_for_tests
    snapshot = state_view()
    snapshot["bp_breaker_active"]  # bool
    snapshot["broker_down"]        # bool
    snapshot["sl_resubmit_failures_1h"]  # int
    snapshot["in_flight_bp_reserved"]    # float

    # In tests:
    reset_for_tests()
"""
from __future__ import annotations
from typing import Dict, Any


def state_view() -> Dict[str, Any]:
    """Return a snapshot of the current auto-trader state. Read-only."""
    from services import auto_trader
    return {
        "bp_breaker_active": auto_trader.bp_breaker_active(),
        "broker_down": auto_trader.broker_down(),
        "sl_resubmit_failures_1h": auto_trader.sl_resubmit_failures_1h(),
        "in_flight_bp_reserved": float(getattr(auto_trader, "_in_flight_bp_reserved", 0.0)),
        "bp_exhausted_until": (
            getattr(auto_trader, "_bp_exhausted_until", None).isoformat()
            if getattr(auto_trader, "_bp_exhausted_until", None) else None
        ),
        "broker_down_until": (
            getattr(auto_trader, "_broker_down_until", None).isoformat()
            if getattr(auto_trader, "_broker_down_until", None) else None
        ),
    }


def reset_for_tests() -> None:
    """Clear in-memory caches + circuit breakers. Call from test setUp() to
    isolate test cases. Never call in production."""
    from services import auto_trader
    auto_trader._bp_exhausted_until = None
    auto_trader._broker_down_until = None
    with auto_trader._sl_resubmit_lock:
        auto_trader._sl_resubmit_failures.clear()
    with auto_trader._in_flight_bp_lock:
        auto_trader._in_flight_bp_reserved = 0.0
    # Caches that may accumulate test-specific entries
    for attr in ("_calibration_cache", "_strategy_cache",
                 "_chandelier_atr_cache", "_target_touch_counts",
                 "_latest_px_cache"):
        cache = getattr(auto_trader, attr, None)
        if cache is not None and hasattr(cache, "clear"):
            cache.clear()
