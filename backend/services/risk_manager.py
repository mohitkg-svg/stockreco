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
    """Add `amount` (in dollars) to the in-flight BP reservation counter.

    Called at order-submit time before Alpaca acknowledges the bracket.
    Prevents a watchlist scan from sizing 30 orders against the same
    stale buying-power figure before the first 422 trips the BP breaker.
    Saturates at 0 to defend against a hypothetical caller passing
    negative amounts. Lock-protected; safe to call from any thread.
    """
    global _in_flight_bp_reserved
    with _in_flight_bp_lock:
        _in_flight_bp_reserved = max(0.0, _in_flight_bp_reserved + float(amount))


def release_bp(amount: float) -> None:
    """Subtract `amount` from the in-flight BP reservation counter.

    Called when an order is canceled or fails to submit. Saturates at
    0 (never goes negative). Lock-protected.
    """
    global _in_flight_bp_reserved
    with _in_flight_bp_lock:
        _in_flight_bp_reserved = max(0.0, _in_flight_bp_reserved - float(amount))


def get_in_flight_bp() -> float:
    """Read the current in-flight BP reservation. Lock-protected."""
    with _in_flight_bp_lock:
        return _in_flight_bp_reserved


def decay_in_flight_bp_if_stale() -> None:
    """Re-read Alpaca's BP every 60s. Decay reservation only when the broker
    BP has dropped by AT LEAST our last reservation amount — meaning our
    submitted bracket was likely the cause of the drop.

    r39 audit fix #24: previously zeroed the reservation any time broker
    BP dropped, including external causes (deposits, withdrawals, manual
    orders outside the bot, account-wide bracket releases). That re-
    introduced the same stale-BP bug the reservation was added to prevent.
    """
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
            # Decay only when the drop is at least our reserved amount —
            # confidence the broker drained OUR submission, not an external
            # cause. Decay by the delta, not all the way to zero, so a
            # small partial-fill draws down a proportional reservation.
            if prev is not None and cur_bp < prev and _in_flight_bp_reserved > 0:
                drop = prev - cur_bp
                if drop >= _in_flight_bp_reserved * 0.9:   # broker drop ~= our reservation
                    _in_flight_bp_reserved = 0.0
                else:
                    # Partial drain — proportional decrement, never below zero.
                    _in_flight_bp_reserved = max(0.0, _in_flight_bp_reserved - drop)
    except Exception as e:
        logger.debug(f"decay_in_flight_bp: {e}")


def trip_bp_breaker(minutes: int = 30) -> None:
    """Trip the buying-power circuit breaker for `minutes`. Called from
    `consider_signal` after an Alpaca 422 (insufficient BP) — pauses
    new entries so a tight scan loop doesn't generate retry storms
    against the broker. Default 30 min absorbs typical end-of-day BP
    constraints without manual intervention.
    """
    global _bp_exhausted_until
    from datetime import timedelta
    _bp_exhausted_until = datetime.utcnow() + timedelta(minutes=minutes)


def trip_broker_breaker(minutes: int = 5) -> None:
    """Trip the broker-down circuit breaker for `minutes`. Called from
    `consider_signal` after an Alpaca 5xx — pauses new entries until
    the broker stabilizes. Shorter window than BP breaker because 5xx
    recoveries are typically minutes, not tens of minutes.
    """
    global _broker_down_until
    from datetime import timedelta
    _broker_down_until = datetime.utcnow() + timedelta(minutes=minutes)


def clear_bp_breaker() -> None:
    """Manually clear the BP circuit breaker (admin / recovery action).
    Doesn't re-arm the auto-trader — just removes this one gate."""
    global _bp_exhausted_until
    _bp_exhausted_until = None


def clear_broker_breaker() -> None:
    """Manually clear the broker-down circuit breaker (admin action)."""
    global _broker_down_until
    _broker_down_until = None


def bp_breaker_active() -> bool:
    """True iff the BP circuit breaker is currently tripped (within its
    timer window). Read by `consider_signal` as gate #1."""
    return bool(_bp_exhausted_until and datetime.utcnow() < _bp_exhausted_until)


def broker_down() -> bool:
    """True iff the broker-down circuit breaker is tripped. Read as
    gate #2 in `consider_signal` (and surfaced on `/api/health`)."""
    return bool(_broker_down_until and datetime.utcnow() < _broker_down_until)


def bp_exhausted_until() -> Optional[datetime]:
    """Expiry time of the BP breaker, or None if not tripped. Surfaced
    on the operator dashboard for visibility into when the breaker
    will auto-clear."""
    return _bp_exhausted_until


def broker_down_until() -> Optional[datetime]:
    """Expiry time of the broker-down breaker, or None if not tripped."""
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
      * SPY daily ADX_14 < 20 (chop regime, reviewer feedback r37) → 0.5×
        Range-bound markets chew up trend-following entries via false
        breakouts — half-size during these periods recovers the EV that
        the chop chops out.
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

    # SPY daily ADX — chop signal when < 20.
    spy_adx = None
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators
        spy_df = fetch_ohlcv("SPY", "1d")
        if spy_df is not None and not spy_df.empty:
            ind = compute_indicators(spy_df)
            if "ADX_14" in ind.columns and len(ind) > 0:
                _adx = ind["ADX_14"].iloc[-1]
                if not (_adx is None) and _adx == _adx:  # NaN check
                    spy_adx = float(_adx)
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

    # Strategy-drawdown trigger (r38, external review pass 5): build a
    # 30-day cumulative-realized-PnL curve from closed AutoTrades; if the
    # current cum-PnL is ≥ 10% of starting equity below the trailing peak,
    # halve risk. This is the "stop the bleeding" reflex — without it, a
    # losing run keeps deploying the same size into the same broken setup.
    # Strategy-PnL (vs whole-account equity) isolates the bot's behavior
    # from manual trading or external deposits/withdrawals.
    drawdown_pct = None
    try:
        from database import SessionLocal as _SL, AutoTrade as _AT
        from datetime import datetime as _dt, timedelta as _td
        from services import paper_trader as _pt
        _acct = _pt.get_account()
        equity = float(_acct["equity"]) if _acct else 0.0
        if equity > 0:
            _db = _SL()
            try:
                since = _dt.utcnow() - _td(days=30)
                rows = (
                    _db.query(_AT)
                    .filter(_AT.status.like("closed%"),
                            _AT.closed_at >= since,
                            _AT.realized_pl.isnot(None))
                    .order_by(_AT.closed_at.asc())
                    .all()
                )
                if len(rows) >= 5:
                    cum = 0.0
                    peak = 0.0
                    for r in rows:
                        cum += float(r.realized_pl or 0.0)
                        if cum > peak:
                            peak = cum
                    # Drawdown is peak-to-trough as a % of starting equity.
                    # peak - cum is the realized $ given back from the high
                    # water mark; we normalize against equity to get a %.
                    drawdown_dollars = peak - cum
                    if drawdown_dollars > 0:
                        drawdown_pct = (drawdown_dollars / equity) * 100
            finally:
                _db.close()
    except Exception:
        drawdown_pct = None

    # r39 audit fix #17: previously each adverse regime independently
    # clamped via min() — VIX>25 AND WR<55% AND chop AND drawdown all gave
    # 0.5×, not 0.0625× as compound risk would imply. When multiple
    # regimes are simultaneously adverse, that's strictly worse than any
    # single one, so we COMPOUND the factors with a hard floor at 0.25×
    # (the lowest size we'd want to deploy on any signal).
    mult = 1.0
    if vix_level is not None and vix_level > 25:
        mult *= 0.5
    elif vix_level is not None and vix_level > 20:
        mult *= 0.75
    if recent_wr is not None and recent_wr < 55.0:
        mult *= 0.5
    if spy_adx is not None and spy_adx < 20.0:
        mult *= 0.5
    if drawdown_pct is not None and drawdown_pct >= 10.0:
        mult *= 0.5
        try:
            from services.alerts import alert as _raise
            _raise(
                "warning", "strategy_drawdown",
                f"30d strategy drawdown {drawdown_pct:.1f}% ≥ 10% — risk halved (×0.5)",
            )
        except Exception:
            pass
    # Floor at 0.25× — anything smaller is too small to recover trading
    # costs even on a winner. Below this we should freeze (see
    # should_freeze_trading), not just shrink.
    return max(mult, 0.25)


def should_freeze_trading() -> Optional[str]:
    """r39 audit fix #8: hard freeze trigger on a clear losing streak.

    Returns a reason string when trading should be paused entirely, or
    None when normal sizing applies.

    Trigger: trailing-30d realized win-rate < 35% with ≥ 5 closed trades.
    The number 5 is small enough to react fast; below 35% WR we're no
    longer in "size down to find your edge", we're in "stop and figure
    out what's wrong". `adaptive_risk_multiplier` already shrinks size on
    < 55% WR; this is the next step beyond that — the engineering kill
    switch the strategy needs but the operator hasn't manually tripped.

    Caller in `consider_signal` short-circuits with
    `autotrade_skip{reason=trading_frozen}` when this returns non-None.
    """
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
            if n < 5:
                return None
            wins = sum(1 for t in closed if (t.realized_pl or 0) > 0)
            wr_pct = (wins / n) * 100
            if wr_pct < 35.0:
                return (
                    f"trailing-30d WR {wr_pct:.0f}% < 35% on {n} trades — "
                    f"trading frozen until streak ends or operator overrides "
                    f"(extend lookback / wait for closed trades to age out)"
                )
            return None
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"should_freeze_trading: {e}")
        return None


# ---------- Health monitors (operator alerts) ------------------------------

def check_low_signal_volume(min_ratio: float = 0.30,
                             min_trailing_days: int = 7) -> Optional[Dict[str, Any]]:
    """Compare today's emitted-signal count against the trailing N-day avg.
    Raise a `low_signal_volume` alert when today < min_ratio × trailing_avg.

    Both numbers come from the `signals` table (any signal_type counts —
    NEUTRAL signals are scan output and their absence indicates a real
    problem with the scan pipeline, not just market quiet).

    Returns the comparison dict for logging/telemetry, or None on error.
    Designed to be called from a daily scheduler job after market close.
    """
    try:
        from database import SessionLocal, Signal
        from datetime import datetime, timedelta
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            today_start = datetime(now.year, now.month, now.day)
            today_count = db.query(Signal).filter(
                Signal.generated_at >= today_start
            ).count()
            trail_start = today_start - timedelta(days=min_trailing_days)
            trail_count = db.query(Signal).filter(
                Signal.generated_at >= trail_start,
                Signal.generated_at < today_start,
            ).count()
            trail_avg = trail_count / max(1, min_trailing_days)
            ratio = (today_count / trail_avg) if trail_avg > 0 else None
            result = {
                "today_count": today_count,
                "trailing_days": min_trailing_days,
                "trailing_avg": round(trail_avg, 1),
                "ratio": round(ratio, 2) if ratio is not None else None,
            }
            # Alert only when we have a baseline AND today is materially below.
            # Don't alert on a fresh DB (trail_avg=0) or pre-open (still gathering).
            if (
                trail_avg >= 5            # need a meaningful baseline
                and ratio is not None
                and ratio < min_ratio
            ):
                try:
                    from services.alerts import alert as _raise
                    _raise(
                        "warning", "low_signal_volume",
                        f"Today's signal count {today_count} is {ratio*100:.0f}% "
                        f"of {min_trailing_days}-day average ({trail_avg:.1f}) "
                        f"— scanner may be degraded",
                    )
                except Exception:
                    pass
            return result
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"check_low_signal_volume: {e}")
        return None


def pdt_day_trade_count(window_business_days: int = 5) -> Dict[str, Any]:
    """Count day-trades in the trailing 5 business days.

    A "day trade" per FINRA: open + close of the same security on the
    same calendar day. We approximate from `auto_trades` by counting
    rows whose `opened_at.date() == closed_at.date()`. PDT rule (live
    margin accounts < $25k): 4+ day trades in 5 business days blocks
    new opens for 90 days.

    On paper this is informational only — Alpaca paper accounts aren't
    PDT-restricted. On live margin it becomes a hard pre-entry gate
    (not yet wired). Returns `{count, trades, threshold, would_block}`.
    """
    try:
        from database import SessionLocal, AutoTrade
        from datetime import datetime, timedelta
        db = SessionLocal()
        try:
            since = datetime.utcnow() - timedelta(days=window_business_days * 2)
            rows = (
                db.query(AutoTrade)
                .filter(AutoTrade.status.like("closed%"),
                        AutoTrade.opened_at.isnot(None),
                        AutoTrade.closed_at.isnot(None),
                        AutoTrade.closed_at >= since)
                .all()
            )
            day_trades = []
            for r in rows:
                if r.opened_at and r.closed_at and r.opened_at.date() == r.closed_at.date():
                    day_trades.append({
                        "trade_id": r.id, "ticker": r.ticker,
                        "date": r.opened_at.date().isoformat(),
                        "realized_pl": float(r.realized_pl or 0),
                    })
            count = len(day_trades)
            return {
                "count": count,
                "trades": day_trades,
                "window_days": window_business_days,
                "pdt_threshold": 4,
                "would_block_under_pdt": count >= 4,
            }
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"pdt_day_trade_count: {e}")
        return {"count": 0, "trades": [], "would_block_under_pdt": False}


def regime_concurrent_cap(base_cap: int) -> int:
    """Tighten max_concurrent_positions in adverse regimes — reviewer's
    "don't trade chop" filter. Returns the EFFECTIVE cap to use for the
    portfolio-heat / concurrent-positions check.

      * VIX > 25 OR SPY below 200-EMA → base // 3 (typically 5)
      * VIX > 20 → base × 2/3 (typically 10)
      * else → base unchanged

    Risk envelope and bucket-sizing already shrink under volatility
    (adaptive_risk_multiplier + vix_options_bucket_multiplier); this
    layer additionally limits the *number* of concurrent ideas — fewer
    positions to manage when regime is hostile.
    """
    if base_cap <= 0:
        return base_cap
    try:
        from services.position_manager import current_price
        vix = current_price("^VIX")
    except Exception:
        vix = None

    spy_below_200 = False
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators
        spy_df = fetch_ohlcv("SPY", "1d")
        if spy_df is not None and not spy_df.empty:
            ind = compute_indicators(spy_df)
            close = float(ind["Close"].iloc[-1])
            if "EMA_200" in ind.columns:
                ema200 = float(ind["EMA_200"].iloc[-1])
                spy_below_200 = close < ema200
    except Exception:
        pass

    if (vix is not None and vix > 25) or spy_below_200:
        return max(3, base_cap // 3)
    if vix is not None and vix > 20:
        return max(5, (base_cap * 2) // 3)
    return base_cap


def current_portfolio_heat() -> float:
    """Beta-weighted dollar-at-risk across all open + pending auto trades.
    Returns 0.0 on any DB / fundamentals lookup failure (errs on the
    "no throttling, just use default sizing" side rather than over-shrinking
    entries on a transient hiccup). Reads only — no writes."""
    try:
        from database import SessionLocal, AutoTrade
        try:
            from services.fundamentals import beta_weight
        except Exception:
            beta_weight = lambda _t, default=1.0, **_: default  # noqa: E731
        db = SessionLocal()
        try:
            open_trades = db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending", "open"])
            ).all()
            total = 0.0
            for ot in open_trades:
                oe = ot.entry_price or ot.requested_entry or 0.0
                os_ = ot.current_stop or ot.stop_loss or 0.0
                raw = 0.0
                if ot.asset_type == "stock" and oe > 0 and os_ > 0:
                    raw = max(0.0, (oe - os_)) * (ot.qty or 0)
                elif ot.asset_type == "option" and oe > 0:
                    raw = float(oe) * 100 * (ot.qty or 0)
                total += raw * beta_weight(ot.ticker)
            return total
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"current_portfolio_heat: {e}")
        return 0.0


def heat_aware_risk_multiplier(equity: float) -> float:
    """Throttle per-trade risk as live portfolio heat approaches the cap.

    The hard heat-cap reject in `consider_signal` (at 100% of cap) protects
    the *book*, but without this throttle the 14th simultaneous trade is
    still sized at full 2% — splatting a fresh full-size position right at
    95% heat usage. This makes the last few entries before the cap smaller
    probes:

      ≤ 50% heat used  → 1.00× (plenty of room)
      50–70%           → 0.85×
      70–85%           → 0.60×
      85–100%          → 0.40× (last quarter — small probes only)

    Returns 1.0 on missing data / equity ≤ 0 (no-op).
    """
    if equity <= 0:
        return 1.0
    try:
        from services.config import RISK_PORTFOLIO_HEAT_CAP_PCT as _CAP_PCT
    except Exception:
        _CAP_PCT = 0.10
    cap = equity * _CAP_PCT
    if cap <= 0:
        return 1.0
    heat = current_portfolio_heat()
    if heat <= 0:
        return 1.0
    used = heat / cap
    if used <= 0.50:
        return 1.0
    if used <= 0.70:
        return 0.85
    if used <= 0.85:
        return 0.60
    return 0.40


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
