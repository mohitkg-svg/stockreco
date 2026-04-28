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
# r48 BACKLOG #lifecycle-P1-13: PDT 403 lockout breaker.
_pdt_lockout_until: Optional[datetime] = None
# r48 BACKLOG #failure-mode-P1-7: DB-down breaker (Postgres OperationalError).
_db_down_until: Optional[datetime] = None
# r48 BACKLOG #concurrency-P1-4: lock guard for breaker timestamps.
_breaker_lock = threading.Lock()
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


def _reset_in_flight_bp() -> None:
    """Hard-reset the BP reservation. r47 fix #T0b-2: called by kill() so
    the post-unkill instance starts from zero rather than a stale carry."""
    global _in_flight_bp_reserved, _in_flight_bp_last_seen_broker_bp, _in_flight_bp_last_check_ts
    with _in_flight_bp_lock:
        _in_flight_bp_reserved = 0.0
        _in_flight_bp_last_seen_broker_bp = None
        _in_flight_bp_last_check_ts = 0.0


def decay_in_flight_bp_if_stale() -> None:
    """Re-read Alpaca's BP every 60s. Decay reservation only when the broker
    BP has dropped by AT LEAST our last reservation amount.

    r48 BACKLOG #concurrency-P1-3: gate read+write of `_last_check_ts` is
    now inside the lock — prior code allowed two concurrent decay calls to
    both pass the 60s gate and double-fetch + double-decay.
    """
    global _in_flight_bp_reserved, _in_flight_bp_last_seen_broker_bp, _in_flight_bp_last_check_ts
    now = time.time()
    with _in_flight_bp_lock:
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
    """Trip the buying-power circuit breaker. r48 BACKLOG: lock-guarded."""
    global _bp_exhausted_until
    from datetime import timedelta
    with _breaker_lock:
        _bp_exhausted_until = datetime.utcnow() + timedelta(minutes=minutes)


def trip_broker_breaker(minutes: int = 5) -> None:
    """Trip the broker-down circuit breaker. r48 BACKLOG: lock-guarded."""
    global _broker_down_until
    from datetime import timedelta
    with _breaker_lock:
        _broker_down_until = datetime.utcnow() + timedelta(minutes=minutes)


def trip_pdt_breaker(hours: int = 24) -> None:
    """r48 BACKLOG #lifecycle-P1-13: trip the PDT lockout breaker for `hours`.
    Called from consider_signal when Alpaca returns 403 with a PDT/wash
    error string. Stops the bot from retry-storming PDT-rejected entries."""
    global _pdt_lockout_until
    from datetime import timedelta
    with _breaker_lock:
        _pdt_lockout_until = datetime.utcnow() + timedelta(hours=hours)


def trip_db_down_breaker(seconds: int = 60) -> None:
    """r48 BACKLOG #failure-mode-P1-7: pause new entries on DB connection error
    for `seconds` so a Cloud SQL micro-outage doesn't burn Yahoo/Claude credits."""
    global _db_down_until
    from datetime import timedelta
    with _breaker_lock:
        _db_down_until = datetime.utcnow() + timedelta(seconds=seconds)


def is_pdt_locked() -> bool:
    with _breaker_lock:
        return _pdt_lockout_until is not None and datetime.utcnow() < _pdt_lockout_until


def is_db_down() -> bool:
    with _breaker_lock:
        return _db_down_until is not None and datetime.utcnow() < _db_down_until


def clear_bp_breaker() -> None:
    global _bp_exhausted_until
    with _breaker_lock:
        _bp_exhausted_until = None


def clear_broker_breaker() -> None:
    global _broker_down_until
    with _breaker_lock:
        _broker_down_until = None


def clear_pdt_breaker() -> None:
    global _pdt_lockout_until
    with _breaker_lock:
        _pdt_lockout_until = None


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


def dynamic_daily_loss_limit_pct(static_pct: float = 0.03) -> float:
    """r44 fix #0.11: dynamic daily-loss ceiling = max(static_pct, 3 ×
    recent_avg_daily_pnl/equity). On a great month (avg +$300/day) the
    static 3% halts at -$3,000; dynamic anchors near -$900. On a bad
    month, dynamic floors at the static value.
    Uses last 30d realized-PnL series. Floors at 1.5% to prevent runaway
    over-tightening on strong months.
    """
    try:
        from database import SessionLocal as _SL_dl, AutoTrade as _AT_dl
        from datetime import datetime as _dt_dl, timedelta as _td_dl
        from services import paper_trader as _pt_dl
        acct = _pt_dl.get_account()
        equity = float(acct["equity"]) if acct else 0.0
        if equity <= 0:
            return float(static_pct)
        db = _SL_dl()
        try:
            since = _dt_dl.utcnow() - _td_dl(days=30)
            rows = (
                db.query(_AT_dl)
                .filter(_AT_dl.status.like("closed%"),
                        _AT_dl.closed_at >= since,
                        _AT_dl.realized_pl.isnot(None))
                .all()
            )
            if len(rows) < 5:
                return float(static_pct)
            total_pl = sum(float(r.realized_pl or 0.0) for r in rows)
            avg_daily_pnl = abs(total_pl) / 30.0
            dynamic_pct = max(0.015, min(float(static_pct), 3 * avg_daily_pnl / equity))
            return float(dynamic_pct)
        finally:
            db.close()
    except Exception:
        return float(static_pct)


def session_equity_drawdown_pct() -> Optional[float]:
    """r44 fix #0.12: intra-session drawdown for auto-deleverage trigger.
    Returns peak-to-now drop as % of starting-session equity, or None on
    data unavailability.
    """
    try:
        from services import paper_trader as _pt_sed
        acct = _pt_sed.get_account()
        if not acct:
            return None
        equity = float(acct.get("equity") or 0)
        last_equity = float(acct.get("last_equity") or 0)  # alpaca = prior session close
        if last_equity <= 0:
            return None
        drop = max(0.0, (last_equity - equity) / last_equity)
        return drop
    except Exception:
        return None


def realized_portfolio_vol_annualized() -> Optional[float]:
    """r44 fix #1.1: 30d realized portfolio volatility, annualized.
    Used by `vol_target_multiplier` to scale entries when book σ exceeds
    or falls short of the target (default 12%).
    Returns None when insufficient data (cold start).
    """
    try:
        from services import paper_trader as _pt_vol
        acct = _pt_vol.get_account()
        if not acct:
            return None
        equity = float(acct.get("equity") or 0)
        if equity <= 0:
            return None
        # Use last 30d trade PnL series as a proxy for daily PnL volatility.
        # Better than nothing pre-equity-curve-feed; live should switch to
        # `get_portfolio_history` when available.
        from database import SessionLocal as _SL_v, AutoTrade as _AT_v
        from datetime import datetime as _dt_v, timedelta as _td_v
        db = _SL_v()
        try:
            since = _dt_v.utcnow() - _td_v(days=30)
            rows = db.query(_AT_v).filter(
                _AT_v.status.like("closed%"),
                _AT_v.closed_at >= since,
                _AT_v.realized_pl.isnot(None),
            ).order_by(_AT_v.closed_at.asc()).all()
            if len(rows) < 10:
                return None
            # Group by day, sum daily realized PnL.
            from collections import defaultdict as _dd
            daily = _dd(float)
            for r in rows:
                d = r.closed_at.date()
                daily[d] += float(r.realized_pl or 0.0)
            daily_pl = list(daily.values())
            if len(daily_pl) < 5:
                return None
            import statistics as _stats
            sigma_daily_pl = _stats.pstdev(daily_pl)
            sigma_daily_ret = sigma_daily_pl / equity
            return sigma_daily_ret * (252 ** 0.5)
        finally:
            db.close()
    except Exception:
        return None


def vol_target_multiplier(target_annual_vol: float = 0.12) -> float:
    """r44 fix #1.1: scale entries so book annualized vol ≈ target.
    Returns 1.0 when realized vol unknown. Clamped [0.5, 1.5] so a single
    fat-tail event doesn't double the size on the next signal.
    """
    rv = realized_portfolio_vol_annualized()
    if rv is None or rv <= 0:
        return 1.0
    raw = target_annual_vol / rv
    return float(max(0.5, min(1.5, raw)))


# r46 fix #0.6: track which DD tier we're in to fire one-shot alerts on
# crossings (don't spam the same alert every manage tick).
_dd_tier_last_alerted: Optional[str] = None


def _maybe_raise_dd_alert(drop_pct: float) -> None:
    """Fire a one-shot alert when DD crosses 3/5/8/10% thresholds.
    Tracks last-alerted tier to avoid spamming."""
    global _dd_tier_last_alerted
    tier = None
    severity = "info"
    if drop_pct >= 0.10:
        tier, severity = "10pct", "critical"
    elif drop_pct >= 0.08:
        tier, severity = "8pct", "critical"
    elif drop_pct >= 0.05:
        tier, severity = "5pct", "warning"
    elif drop_pct >= 0.03:
        tier, severity = "3pct", "warning"
    if tier and tier != _dd_tier_last_alerted:
        _dd_tier_last_alerted = tier
        try:
            from services.alerts import alert as _raise_dd
            _raise_dd(severity, f"drawdown_{tier}",
                      f"Account drawdown {drop_pct*100:.1f}% — entering tier {tier}")
        except Exception:
            pass
    elif drop_pct < 0.03 and _dd_tier_last_alerted is not None:
        # Recovered; reset so the next breach fires fresh.
        _dd_tier_last_alerted = None


def account_drawdown_multiplier(lookback_days: int = 60) -> float:
    """Graduated account-level drawdown control. Returns:
      ≤  3% drawdown → 1.0
      3-5%           → 0.70
      5-8%           → 0.50
      8-10%          → 0.25
      ≥ 10%          → 0.0  (caller treats as skip)

    r46 fix #0.2: now reads from the persisted EquitySnapshot table
    (populated every 5 min by `record_equity_snapshot`). Prior code
    called `paper_trader.get_portfolio_history()` which doesn't exist —
    fell through to `last_equity` (single-session DD) and the graduated
    60d tier system was effectively single-day session DD, silently
    degrading the r44 fix.
    Falls back to session-DD only when no snapshots exist (cold start).
    """
    try:
        from database import SessionLocal as _SL_dd, EquitySnapshot as _ES_dd
        from datetime import datetime as _dt_dd, timedelta as _td_dd
        db = _SL_dd()
        try:
            since = _dt_dd.utcnow() - _td_dd(days=lookback_days)
            rows = (
                db.query(_ES_dd.equity)
                .filter(_ES_dd.ts >= since)
                .order_by(_ES_dd.ts.asc())
                .all()
            )
            eq = [float(r[0]) for r in rows if r[0] is not None]
        finally:
            db.close()
        if len(eq) >= 5:
            peak = max(eq)
            cur = eq[-1]
            if peak > 0:
                drop = max(0.0, (peak - cur) / peak)
                _maybe_raise_dd_alert(drop)
                if drop >= 0.10: return 0.0
                if drop >= 0.08: return 0.25
                if drop >= 0.05: return 0.50
                if drop >= 0.03: return 0.70
                return 1.0
    except Exception as e:
        logger.debug(f"account_drawdown_multiplier (snapshot path): {e}")
    # Cold-start fallback: session DD via last_equity. Same shape as before.
    try:
        from services import paper_trader as _pt_dd
        acct = _pt_dd.get_account()
        if not acct:
            return 1.0
        equity = float(acct.get("equity") or 0)
        last_equity = float(acct.get("last_equity") or 0)
        if last_equity <= 0:
            return 1.0
        drop = max(0.0, (last_equity - equity) / last_equity)
        if drop >= 0.10: return 0.0
        if drop >= 0.08: return 0.25
        if drop >= 0.05: return 0.50
        if drop >= 0.03: return 0.70
        return 1.0
    except Exception:
        return 1.0


def in_crisis_mode() -> bool:
    """r46 Tier 1: aggregate "crisis regime" predicate. True iff:
      * account drawdown ≥ 5% (multi-day from EquitySnapshot)
      * OR session DD ≥ 4%
      * OR (VIX > 30 AND SPY 5-day return < -5%)
      * OR `should_freeze_trading` is True

    Position-management code branches on this to tighten chandelier,
    raise T1 trim, halve time-stop, and block new entries.
    """
    try:
        # Multi-day DD from equity snapshots.
        from database import SessionLocal as _SL_cm, EquitySnapshot as _ES_cm
        from datetime import datetime as _dt_cm, timedelta as _td_cm
        db = _SL_cm()
        try:
            since = _dt_cm.utcnow() - _td_cm(days=30)
            rows = db.query(_ES_cm.equity).filter(_ES_cm.ts >= since).all()
            eq = [float(r[0]) for r in rows if r[0] is not None]
            if len(eq) >= 5:
                peak = max(eq); cur = eq[-1]
                if peak > 0 and (peak - cur) / peak >= 0.05:
                    return True
        finally:
            db.close()
    except Exception:
        pass
    try:
        sed = session_equity_drawdown_pct()
        if sed is not None and sed >= 0.04:
            return True
    except Exception:
        pass
    try:
        from services.position_manager import current_price as _cp_cm
        from services.data_fetcher import fetch_ohlcv as _fo_cm
        vix = _cp_cm("^VIX")
        spy_df = _fo_cm("SPY", "1d")
        if vix and vix > 30 and spy_df is not None and len(spy_df) >= 6:
            spy_5d = float(spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-6] - 1)
            if spy_5d < -0.05:
                return True
    except Exception:
        pass
    try:
        if should_freeze_trading() is not None:
            return True
    except Exception:
        pass
    return False


def crisis_chandelier_multiplier(base: float) -> float:
    """In crisis mode, tighten chandelier from 3× to 2× ATR (cuts trail
    distance ~33%). Outside crisis, return base unchanged."""
    try:
        if in_crisis_mode():
            return base * 0.67
    except Exception:
        pass
    return base


def crisis_t1_trim_fraction(base: float) -> float:
    """In crisis, raise T1 trim from 33% to 50%. Banks more on early wins
    when subsequent reversion risk is elevated."""
    try:
        if in_crisis_mode():
            return min(0.5, max(base, 0.5))
    except Exception:
        pass
    return base


def record_equity_snapshot() -> None:
    """r46 fix #0.2: persist current equity / cash / BP / open-pnl to
    EquitySnapshot table. Called by the scheduler every 5 minutes during
    RTH and once at EOD. Cheap (single row); 5-min cadence × 7h = 84
    rows/trading-day, manageable for years.
    """
    try:
        from database import SessionLocal as _SL_es, EquitySnapshot as _ES_es
        from services import paper_trader as _pt_es
        from datetime import datetime as _dt_es
        acct = _pt_es.get_account()
        if not acct:
            return
        equity = float(acct.get("equity") or 0)
        if equity <= 0:
            return
        cash = float(acct.get("cash") or 0)
        bp = float(acct.get("buying_power") or 0)
        rpnl = 0.0
        try:
            from services.auto_trader import realized_pnl_today as _rpnl_t
            rpnl = float(_rpnl_t() or 0)
        except Exception:
            pass
        unr = 0.0
        try:
            for p in (_pt_es.get_positions() or []):
                unr += float(p.get("unrealized_pl") or 0.0)
        except Exception:
            pass
        n_open = 0
        try:
            n_open = len(_pt_es.get_positions() or [])
        except Exception:
            pass
        spy_close = None
        try:
            from services.data_fetcher import fetch_ohlcv as _fo_es
            df_spy = _fo_es("SPY", "1d")
            if df_spy is not None and not df_spy.empty:
                spy_close = float(df_spy["Close"].iloc[-1])
        except Exception:
            pass
        db = _SL_es()
        try:
            # r47 fix #T0c-2: round timestamp to the 5-min bucket so multi-
            # instance Cloud Run deployments don't write N rows per cron tick.
            # Pre-existing bucket → skip (idempotent under multi-instance).
            now_dt = _dt_es.utcnow()
            bucket_dt = now_dt.replace(second=0, microsecond=0,
                                       minute=(now_dt.minute // 5) * 5)
            existing = db.query(_ES_es).filter(_ES_es.ts == bucket_dt).first()
            if existing is not None:
                # Refresh the row in-place — a later instance may have a
                # slightly more accurate snapshot (later within the bucket).
                existing.equity = equity
                existing.cash = cash
                existing.buying_power = bp
                existing.realized_pl_today = rpnl
                existing.unrealized_pl = unr
                existing.open_positions = n_open
                existing.spy_close = spy_close
                db.commit()
            else:
                db.add(_ES_es(
                    ts=bucket_dt,
                    equity=equity,
                    cash=cash,
                    buying_power=bp,
                    realized_pl_today=rpnl,
                    unrealized_pl=unr,
                    open_positions=n_open,
                    spy_close=spy_close,
                ))
                try:
                    db.commit()
                except Exception:
                    db.rollback()  # multi-instance race: another inserted same ts
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"record_equity_snapshot: {e}")


def slippage_aware_risk_per_share(entry: float, stop: float, atr: float, spread: float = 0.0) -> float:
    """r44 fix #1.3: risk-per-share with stop-fill slippage budget.

    r47 fix #T0f-2: prior `max(0.0, entry - stop)` returned 0 for SHORT
    setups (where stop > entry), and the qty calc divided by the residual
    slippage buffer alone — over-sizing shorts 10-20×. Use abs(...) so the
    function is direction-agnostic.
    """
    base = abs(float(entry) - float(stop))
    slip_atr = 0.10 * (atr or 0.0)
    slip_spread = 0.5 * (spread or 0.0)
    return base + max(slip_atr, slip_spread, 0.01)


def portfolio_greeks() -> Dict[str, float]:
    """r44 fix #1.5: aggregate net delta/gamma/theta/vega across the
    options book in the database. Per-contract Greeks live on AutoTrade
    rows that have asset_type='option'; we approximate with crude defaults
    when Greeks are missing (delta=0.5 for ATM, etc.).
    Returns {delta, gamma, theta, vega} all in $ terms (×100 contract).
    """
    # r48 BACKLOG #options-P0-4: read REAL persisted Greeks (entry_delta,
    # entry_gamma, entry_theta, entry_vega). Fall back to OCC-direction-aware
    # defaults only when the row is missing them (older rows pre-r48).
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    try:
        from database import SessionLocal as _SL_g, AutoTrade as _AT_g
        db = _SL_g()
        try:
            opts = db.query(_AT_g).filter(
                _AT_g.asset_type == "option",
                _AT_g.status.in_(["open", "pending"]),
            ).all()
            for t in opts:
                qty = float(t.qty or 0)
                if qty <= 0:
                    continue
                is_put = isinstance(t.symbol, str) and len(t.symbol) > 12 and t.symbol[-9] == "P"
                d = float(getattr(t, "entry_delta", None) or (-0.4 if is_put else 0.4))
                g = float(getattr(t, "entry_gamma", None) or 0.0)
                th = float(getattr(t, "entry_theta", None) or -0.05)
                v = float(getattr(t, "entry_vega", None) or 0.10)
                # All on per-contract basis × 100 multiplier
                out["delta"] += qty * 100 * d
                out["gamma"] += qty * 100 * g
                out["theta"] += qty * 100 * th  # theta is negative for long
                out["vega"] += qty * 100 * v
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"portfolio_greeks: {e}")
    return out


def portfolio_greeks_caps_breached(equity: float, prospective_vega: float = 0.0,
                                   prospective_gamma: float = 0.0,
                                   prospective_delta: float = 0.0) -> Dict[str, bool]:
    """r48 BACKLOG #options-P0-5: check whether ADDING `prospective_*` to the
    current book would breach configured Greeks caps. Returns dict of
    `{"vega": bool, "gamma": bool, "delta": bool}` — True means cap breached.
    Caps default: vega ≤ 0.05% × equity per 1-vol move; gamma ≤ 0.02%; net
    delta ≤ 50% of equity (in $ terms)."""
    try:
        from database import SessionLocal as _SL_gc, AutoTraderConfig as _C_gc
        db = _SL_gc()
        try:
            cfg = db.query(_C_gc).filter(_C_gc.id == 1).first()
            vega_cap_pct = float(getattr(cfg, "portfolio_max_vega_pct", 0.0005) or 0.0005)
            gamma_cap_pct = float(getattr(cfg, "portfolio_max_gamma_pct", 0.0002) or 0.0002)
            delta_cap_pct = float(getattr(cfg, "portfolio_max_net_delta_pct", 0.50) or 0.50)
        finally:
            db.close()
    except Exception:
        vega_cap_pct, gamma_cap_pct, delta_cap_pct = 0.0005, 0.0002, 0.50
    g = portfolio_greeks()
    return {
        "vega": (abs(g["vega"]) + abs(prospective_vega)) > equity * vega_cap_pct,
        "gamma": (abs(g["gamma"]) + abs(prospective_gamma)) > equity * gamma_cap_pct,
        "delta": (abs(g["delta"]) + abs(prospective_delta)) > equity * delta_cap_pct,
    }


def earnings_cluster_count(window_hours: int = 168) -> int:
    """r44 fix #1.6: count of open positions whose underlying has earnings
    within `window_hours`. Used by consider_signal as an aggregate-event-
    risk gate.
    """
    try:
        from database import SessionLocal as _SL_ec, AutoTrade as _AT_ec
        from services.earnings import hours_to_next_earnings as _hne
        db = _SL_ec()
        try:
            opens = db.query(_AT_ec).filter(
                _AT_ec.status.in_(["open", "pending", "adopted"]),
            ).all()
            n = 0
            for t in opens:
                try:
                    h = _hne(t.ticker)
                    if h is not None and h <= window_hours:
                        n += 1
                except Exception:
                    pass
            return n
        finally:
            db.close()
    except Exception:
        return 0


def book_var_99(equity: float) -> float:
    """99% parametric VaR.

    r48 BACKLOG #numerical-P2-20: prior `heat * 1.5` understated the 99%
    tail. For normal(0, σ), one-tailed 99% is at 2.326σ. Heat is roughly
    a 1σ stop-loss measure (assuming stops sit ~1σ wide), so the right
    multiplier is ~2.33, not 1.5. Updated.
    """
    if equity <= 0:
        return 0.0
    heat = current_portfolio_heat()
    return heat * 2.33


def book_leverage_pct(equity: float) -> float:
    """r44 fix #1.8: total notional / equity. Used to enforce a leverage
    cap (default 1.5×). Returns 0.0 on missing data.
    """
    if equity <= 0:
        return 0.0
    try:
        from services import paper_trader as _pt_lev
        positions = _pt_lev.get_positions() or []
        notional = sum(abs(float(p.get("qty") or 0) * float(p.get("current_price") or 0)) for p in positions)
        return notional / equity
    except Exception:
        return 0.0


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

    # 30-day expectancy from closed auto-trades.
    # r42 fix #1.3: previous logic used count-weighted WR ("9 small wins +
    # 1 big loss = 90% healthy"). Switch to *expectancy* (avg-PnL per trade).
    # Expectancy < 0 means the strategy is losing money on average — that
    # IS the signal to size down regardless of WR. We keep `recent_wr`
    # for the surface label (operators read WR) but the gate is on
    # expectancy.
    recent_wr = None
    expectancy = None
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
            if n >= 10:
                wins = sum(1 for t in closed if (t.realized_pl or 0) > 0)
                recent_wr = (wins / n * 100)
                pnls = [float(t.realized_pl or 0.0) for t in closed]
                expectancy = sum(pnls) / n
        finally:
            db.close()
    except Exception:
        recent_wr = None
        expectancy = None

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
    # r42 fix #1.3: gate on expectancy first (zero-pnl-bias-free), but
    # keep the WR fallback for tiny samples where avg-PnL is noisy.
    if expectancy is not None and expectancy <= 0:
        mult *= 0.5
    elif recent_wr is not None and recent_wr < 55.0:
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
    # r43 fix #1.26: when the unfloored product would be ≤ 0.25, return
    # 0 so caller treats as "skip". A 0.25× position whose stop costs
    # are still real has negative EV in adverse-stack regimes; floor-
    # then-keep-trading masks how aggressively reality is fighting us.
    if mult <= 0.25:
        return 0.0
    return mult


def should_freeze_trading() -> Optional[str]:
    """Hard freeze trigger on a clear losing streak.

    Triggers (any one of):
      * WR < 35% AND ≥ 5 closed trades
      * expectancy ≤ 0 AND ≥ 10 closed trades AND sum_PnL ≤ -2× |worst trade|
      * r43 fix #1.20: 5+ consecutive losing trades (most recent N closed trades).
    """
    try:
        from database import SessionLocal, AutoTrade
        from datetime import datetime, timedelta
        from sqlalchemy import desc as _desc
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
            pnls = [float(t.realized_pl or 0.0) for t in closed]
            sum_pl = sum(pnls)
            expectancy = sum_pl / n
            worst = min(pnls) if pnls else 0.0
            if wr_pct < 35.0:
                return (
                    f"trailing-30d WR {wr_pct:.0f}% < 35% on {n} trades — "
                    f"trading frozen (extend lookback / wait for closed trades to age out)"
                )
            if n >= 10 and expectancy <= 0 and sum_pl <= -2.0 * abs(worst):
                return (
                    f"trailing-30d expectancy ${expectancy:.2f}/trade ≤ 0, "
                    f"sum=${sum_pl:.2f}, worst=${worst:.2f} — losses span beyond "
                    f"a single fat-tail; trading frozen pending edge review"
                )
            # r43 fix #1.20: consecutive-loss circuit breaker. 5 stops in
            # a row triggers a freeze regardless of 30d stats — it's the
            # "human would stop trading" reflex.
            recent = (
                db.query(AutoTrade)
                .filter(
                    AutoTrade.status.like("closed%"),
                    AutoTrade.realized_pl.isnot(None),
                )
                .order_by(_desc(AutoTrade.closed_at))
                .limit(5)
                .all()
            )
            if len(recent) >= 5 and all((r.realized_pl or 0) <= 0 for r in recent):
                return (
                    f"5 consecutive losing trades — trading frozen until next "
                    f"manual override or until at least one closed-target ages in"
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
                    # r47 fix #T0f-1: shorts have stop ABOVE entry; max(0,...)
                    # was zeroing them out and letting adopted shorts pass the
                    # 10% portfolio-heat cap with $0 contribution.
                    raw = abs(float(oe) - float(os_)) * (ot.qty or 0)
                elif ot.asset_type == "option" and oe > 0:
                    raw = float(oe) * 100 * (ot.qty or 0)
                total += raw * beta_weight(ot.ticker)
            # r44 fix #0.10: include in-flight BP reservations as a
            # conservative heat add. Without this, two parallel scanners
            # could both compute heat=8% (cap=10%) and each reserve a
            # 1.5% trade — actual post-fill heat 11%, breaching the cap.
            # Use a 5% stop-distance proxy: in_flight_bp / 20 ≈ heat-equiv.
            try:
                in_flight = float(get_in_flight_bp() or 0.0)
                total += max(0.0, in_flight / 20.0)
            except Exception:
                pass
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

def strategy_multiplier(
    strategy_name: Optional[str],
    asset_type: Optional[str] = None,
) -> float:
    """Empirical risk multiplier for a strategy, 1.0 when not enough data.

    Derived from `strategy_scorecard()` which live-reads closed trades in
    the last 60 days. Cached 1h (nightly job refreshes upstream).

    r53 fix (Tier-0 #3): added optional `asset_type` filter so option
    entries don't get sized with stock-flow data. The backtester only
    models stocks; live option trades have ~0% WR vs stock ~36% WR on
    the same strategies. Mixing them dilutes the option penalty.
    Callers (consider_call_play, consider_put_play) should pass
    asset_type="option" so the multiplier reads ONLY past option
    realized stats.
    """
    if not strategy_name:
        return 1.0
    cache_key = f"{strategy_name}|{asset_type or 'any'}"
    now = time.time()
    cached = _strategy_mult_cache.get(cache_key)
    if cached and now < cached[1]:
        return cached[0]
    try:
        # r43 fix #1.6: enforce min-bucket-N at the call site. With <20
        # closed trades the multiplier (which can swing 0.5×→1.5×) is
        # noise; we return 1.0 until we have a meaningful sample.
        from services.auto_trader import strategy_scorecard
        card = strategy_scorecard(days=60, min_trades=5, asset_type=asset_type)
        entry = card.get(strategy_name)
        if entry and (entry.get("n") or entry.get("trades") or 0) >= 20:
            m = float(entry["multiplier"])
        else:
            m = 1.0
    except Exception:
        m = 1.0
    _strategy_mult_cache[cache_key] = (m, now + _STRATEGY_CACHE_TTL)
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
            # r43 fix #1.6: require ≥20 trades in the bucket before deviating
            # from 1.0; below that, a single big winner can permanently
            # 1.5× every signal in that bucket for 60 days.
            if row and (getattr(row, "n", 0) or 0) >= 20:
                m = float(row.multiplier)
                _calibration_cache[bucket] = (m, now + _CALIBRATION_CACHE_TTL)
                return m
        finally:
            db.close()
    except Exception:
        pass
    _calibration_cache[bucket] = (1.0, now + _CALIBRATION_CACHE_TTL)
    return 1.0
