"""Risk-management helpers — empirical multipliers and sizing gates.

Extracted from auto_trader.py (2026-04-25). This module owns:
  * `strategy_multiplier()` — per-strategy empirical risk multiplier
    derived from the live-trade scorecard
  * `calibration_multiplier()` — per-confidence-bucket risk multiplier
    from the nightly calibration job
  * BP reservation + circuit-breaker state helpers

All state (caches, BP reservations, circuit-breaker timestamps) lives
here now, not in auto_trader. Callers reach in via these public helpers.

Module state is deliberately module-level (not classed) — Python modules
are already singletons and we don't need multi-tenancy. A future
AutoTraderService class can wrap this module if we ever need isolation
for tests.
"""
from __future__ import annotations
import logging
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# ---------- Caches (formerly in auto_trader) -------------------------------

_strategy_mult_cache: Dict[str, tuple] = {}   # strategy_name → (mult, expiry_ts)
_STRATEGY_CACHE_TTL = 3600
_calibration_cache: Dict[str, tuple] = {}     # bucket → (mult, expiry_ts)
_CALIBRATION_CACHE_TTL = 3600


# ---------- Buying-power reservation + circuit breakers --------------------

# Local in-flight buying-power reservation. Alpaca's reported `buying_power`
# lags submitted bracket orders (pending TPs reserve BP that doesn't
# immediately show up as drawn). Without local bookkeeping, a watchlist
# scan can submit 30 orders against the same stale BP figure before the
# first 422 trips the circuit breaker. We add `qty * entry` to this
# counter at submit time and decay it as the broker catches up.
_in_flight_bp_reserved: float = 0.0
_in_flight_bp_lock = threading.Lock()
_in_flight_bp_last_seen_broker_bp: Optional[float] = None
_in_flight_bp_last_check_ts: float = 0.0

# BP exhaustion circuit breaker (422 from Alpaca).
_bp_exhausted_until: Optional[datetime] = None
# Broker-down circuit breaker (5xx from Alpaca).
_broker_down_until: Optional[datetime] = None
# Rolling 1h count of SL-resubmit failures.
_sl_resubmit_failures: List[float] = []
_sl_resubmit_lock = threading.Lock()


def reserve_bp(amount: float) -> None:
    global _in_flight_bp_reserved
    with _in_flight_bp_lock:
        _in_flight_bp_reserved = max(0.0, _in_flight_bp_reserved + float(amount))


def release_bp(amount: float) -> None:
    global _in_flight_bp_reserved
    with _in_flight_bp_lock:
        _in_flight_bp_reserved = max(0.0, _in_flight_bp_reserved - float(amount))


def get_in_flight_bp() -> float:
    with _in_flight_bp_lock:
        return _in_flight_bp_reserved


def decay_in_flight_bp_if_stale() -> None:
    """Re-read Alpaca's BP every 60s. If the broker's number has dropped
    (i.e. they've drawn down the reserved amount), reset our counter."""
    global _in_flight_bp_reserved, _in_flight_bp_last_seen_broker_bp, _in_flight_bp_last_check_ts
    now = time.time()
    if now - _in_flight_bp_last_check_ts < 60:
        return
    _in_flight_bp_last_check_ts = now
    try:
        from services import paper_trader
        acct = paper_trader.get_account()
        if not acct:
            return
        cur_bp = float(acct.get("buying_power") or 0)
        with _in_flight_bp_lock:
            prev = _in_flight_bp_last_seen_broker_bp
            _in_flight_bp_last_seen_broker_bp = cur_bp
            # If broker BP dropped, the reservation is implicitly satisfied.
            if prev is not None and cur_bp < prev:
                _in_flight_bp_reserved = 0.0
    except Exception as e:
        logger.debug(f"decay_in_flight_bp: {e}")


def trip_bp_breaker(minutes: int = 30) -> None:
    global _bp_exhausted_until
    from datetime import timedelta
    _bp_exhausted_until = datetime.utcnow() + timedelta(minutes=minutes)


def trip_broker_breaker(minutes: int = 5) -> None:
    global _broker_down_until
    from datetime import timedelta
    _broker_down_until = datetime.utcnow() + timedelta(minutes=minutes)


def clear_bp_breaker() -> None:
    global _bp_exhausted_until
    _bp_exhausted_until = None


def clear_broker_breaker() -> None:
    global _broker_down_until
    _broker_down_until = None


def bp_breaker_active() -> bool:
    return bool(_bp_exhausted_until and datetime.utcnow() < _bp_exhausted_until)


def broker_down() -> bool:
    return bool(_broker_down_until and datetime.utcnow() < _broker_down_until)


def bp_exhausted_until() -> Optional[datetime]:
    return _bp_exhausted_until


def broker_down_until() -> Optional[datetime]:
    return _broker_down_until


def record_sl_resubmit_failure() -> None:
    now = time.time()
    with _sl_resubmit_lock:
        cutoff = now - 3600
        _sl_resubmit_failures[:] = [t for t in _sl_resubmit_failures if t > cutoff]
        _sl_resubmit_failures.append(now)


def sl_resubmit_failures_1h() -> int:
    now = time.time()
    with _sl_resubmit_lock:
        cutoff = now - 3600
        return sum(1 for t in _sl_resubmit_failures if t > cutoff)


def adaptive_risk_multiplier() -> float:
    """Tighten the max-risk-per-trade envelope under adverse conditions.

    Returns a multiplier to apply to cfg.max_risk_per_trade_pct:
      * VIX > 25 or recent-30d realized win-rate < 55% → 0.5× (halve risk)
      * VIX > 20 (elevated but not extreme) → 0.75×
      * Otherwise 1.0×

    Missing data defaults to 1.0 (no tightening) — erring on the operator's
    already-set cap rather than over-interpreting noisy inputs.
    """
    # VIX level
    vix_level = None
    try:
        from services.position_manager import current_price
        px = current_price("^VIX")
        if px and px > 0:
            vix_level = px
    except Exception:
        pass

    # 30-day realized win rate from closed auto-trades
    try:
        from database import SessionLocal, AutoTrade
        from datetime import datetime, timedelta
        db = SessionLocal()
        try:
            since = datetime.utcnow() - timedelta(days=30)
            closed = db.query(AutoTrade).filter(
                AutoTrade.status.like("closed%"),
                AutoTrade.closed_at >= since,
                AutoTrade.realized_pl.isnot(None),
            ).all()
            n = len(closed)
            wins = sum(1 for t in closed if (t.realized_pl or 0) > 0)
            recent_wr = (wins / n * 100) if n >= 10 else None
        finally:
            db.close()
    except Exception:
        recent_wr = None

    mult = 1.0
    if vix_level is not None and vix_level > 25:
        mult = min(mult, 0.5)
    elif vix_level is not None and vix_level > 20:
        mult = min(mult, 0.75)
    if recent_wr is not None and recent_wr < 55.0:
        mult = min(mult, 0.5)
    return mult


def vix_options_bucket_multiplier() -> float:
    """Scale `option_pct_of_equity` by VIX regime. High VIX = gamma/vega
    exposure is costlier, so we de-allocate from options.

      * VIX > 30 → 0.3× (strongly reduce)
      * VIX > 25 → 0.5×
      * VIX > 20 → 0.75×
      * else    → 1.0×
    """
    try:
        from services.position_manager import current_price
        px = current_price("^VIX")
    except Exception:
        return 1.0
    if px is None or px <= 0:
        return 1.0
    if px > 30: return 0.3
    if px > 25: return 0.5
    if px > 20: return 0.75
    return 1.0


def reset_for_tests() -> None:
    """Clear every cache + circuit-breaker. Use only in tests."""
    global _in_flight_bp_reserved, _bp_exhausted_until, _broker_down_until
    _in_flight_bp_reserved = 0.0
    _bp_exhausted_until = None
    _broker_down_until = None
    _strategy_mult_cache.clear()
    _calibration_cache.clear()
    with _sl_resubmit_lock:
        _sl_resubmit_failures.clear()


# ---------- Empirical multipliers ------------------------------------------

def strategy_multiplier(strategy_name: Optional[str]) -> float:
    """Empirical risk multiplier for a strategy, 1.0 when not enough data.

    Derived from `strategy_scorecard()` which live-reads closed trades in
    the last 60 days. Cached 1h (nightly job refreshes upstream).
    """
    if not strategy_name:
        return 1.0
    now = time.time()
    cached = _strategy_mult_cache.get(strategy_name)
    if cached and now < cached[1]:
        return cached[0]
    try:
        # Delayed import to avoid auto_trader ↔ risk_manager circular at module load
        from services.auto_trader import strategy_scorecard
        card = strategy_scorecard(days=60, min_trades=5)
        entry = card.get(strategy_name)
        m = float(entry["multiplier"]) if entry else 1.0
    except Exception:
        m = 1.0
    _strategy_mult_cache[strategy_name] = (m, now + _STRATEGY_CACHE_TTL)
    return m


def calibration_multiplier(confidence: float) -> float:
    """Per-confidence-bucket empirical multiplier. Defaults to 1.0 when
    the bucket has no data yet. Nightly job writes fresh values."""
    try:
        bucket = f"{int(float(confidence) // 10) * 10}-{int(float(confidence) // 10) * 10 + 9}"
    except Exception:
        return 1.0
    now = time.time()
    cached = _calibration_cache.get(bucket)
    if cached and now < cached[1]:
        return cached[0]
    try:
        from database import ConfidenceCalibration, SessionLocal
        db = SessionLocal()
        try:
            row = db.query(ConfidenceCalibration).filter(ConfidenceCalibration.bucket == bucket).first()
            if row:
                m = float(row.multiplier)
                _calibration_cache[bucket] = (m, now + _CALIBRATION_CACHE_TTL)
                return m
        finally:
            db.close()
    except Exception:
        pass
    _calibration_cache[bucket] = (1.0, now + _CALIBRATION_CACHE_TTL)
    return 1.0
