"""Broker-interaction helpers extracted from auto_trader.py.

This module owns all the Alpaca REST interactions for auto-traded
positions — bracket-order leg lookup, stop-replacement, and force-close.
Each function is narrow and side-effect-scoped (broker ± DB trade row),
making the trading state machine in position_manager easier to follow.

Policy: these functions intentionally do NOT own trade lifecycle state
(level_index, touch counts, idempotency keys). Callers in the manage
loop own that bookkeeping and pass it down.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import threading

from sqlalchemy.orm import Session

from services import paper_trader, metrics

logger = logging.getLogger(__name__)

# Idempotency cache for stop-replacements. See `replace_stop` docstring.
# r47 fix #T0e-2 / T0h: bounded LRU + threading.Lock — multi-thread
# (manage tick + WS fast-path + AI exit) all reach this dict; without a
# lock concurrent reads/writes can produce torn cache entries and double
# broker-replace calls. A bounded cap prevents unbounded growth on close
# paths that don't pop (force_close_trade etc.).
_replace_stop_cache_lock = threading.Lock()
_replace_stop_cache: Dict[str, float] = {}
_REPLACE_STOP_CACHE_MAX = 10000


def _rsc_get(key: str) -> Optional[float]:
    with _replace_stop_cache_lock:
        return _replace_stop_cache.get(key)


def _rsc_set(key: str, val: float) -> None:
    with _replace_stop_cache_lock:
        _replace_stop_cache[key] = val
        if len(_replace_stop_cache) > _REPLACE_STOP_CACHE_MAX:
            # Drop oldest 10% by insertion order
            drop = max(1, _REPLACE_STOP_CACHE_MAX // 10)
            for _ in range(drop):
                try:
                    _replace_stop_cache.pop(next(iter(_replace_stop_cache)))
                except StopIteration:
                    break


def _rsc_pop(key: str) -> None:
    with _replace_stop_cache_lock:
        _replace_stop_cache.pop(key, None)


def atomic_accumulate_realized_pl(db: Session, trade_id: int, delta: float) -> None:
    """r48 BACKLOG #concurrency-P1-11: atomic SQL accumulator for realized_pl.

    Replaces the lost-update-prone ORM read-modify-write pattern
    `t.realized_pl = (t.realized_pl or 0) + delta; db.commit()`. Two
    threads racing on the same trade row no longer wipe each other's
    contribution — the UPDATE is committed in a single round-trip with
    `realized_pl = COALESCE(realized_pl, 0) + :d`.
    """
    from database import AutoTrade as _AT_acc
    from sqlalchemy import update as _sql_update
    try:
        delta_r = round(float(delta), 2)
        db.execute(
            _sql_update(_AT_acc)
            .where(_AT_acc.id == trade_id)
            .values(realized_pl=(
                _AT_acc.__table__.c.realized_pl.op("COALESCE")(0.0) + delta_r
            ))
        )
        db.commit()
    except Exception:
        # Fallback: read-modify-write (still better than crashing)
        db.rollback()
        t = db.query(_AT_acc).filter(_AT_acc.id == trade_id).first()
        if t is not None:
            t.realized_pl = round(float(t.realized_pl or 0.0) + float(delta), 2)
            db.commit()


def atomic_append_note(db: Session, trade_id: int, suffix: str) -> None:
    """r53 fix (Tier-2 #11): atomic SQL append for `AutoTrade.note`.

    Replaces the read-modify-write pattern `t.note = (t.note or "") +
    suffix; db.commit()` used at 20+ sites. Two workers (manage tick,
    WS fast-path, AI exit) writing notes for the same trade no longer
    stomp each other's audit-trail lines.

    The suffix is appended verbatim (caller controls the separator,
    typically " | EVENT: ..."). On failure, falls through to the
    read-modify-write path.
    """
    from database import AutoTrade as _AT_n
    from sqlalchemy import update as _sql_update_n
    if not suffix:
        return
    try:
        # Use SQL string concatenation. Postgres uses ||; SQLite supports
        # both || and concat().
        from sqlalchemy import func as _func
        db.execute(
            _sql_update_n(_AT_n)
            .where(_AT_n.id == trade_id)
            .values(note=_func.coalesce(_AT_n.__table__.c.note, "") + suffix)
        )
        db.commit()
    except Exception:
        db.rollback()
        t = db.query(_AT_n).filter(_AT_n.id == trade_id).first()
        if t is not None:
            t.note = (t.note or "") + suffix
            db.commit()


def atomic_increment_target_touch(db: Session, trade_id: int) -> int:
    """r48 BACKLOG #concurrency-P1-1: atomic increment of target_touch_count.
    Returns the post-increment count."""
    from database import AutoTrade as _AT_tt
    from sqlalchemy import update as _sql_update_tt
    try:
        db.execute(
            _sql_update_tt(_AT_tt)
            .where(_AT_tt.id == trade_id)
            .values(target_touch_count=(
                _AT_tt.__table__.c.target_touch_count + 1
            ))
        )
        db.commit()
        t = db.query(_AT_tt).filter(_AT_tt.id == trade_id).first()
        return int((t.target_touch_count if t else 0) or 0)
    except Exception:
        db.rollback()
        t = db.query(_AT_tt).filter(_AT_tt.id == trade_id).first()
        if t is None:
            return 0
        n = int(t.target_touch_count or 0) + 1
        t.target_touch_count = n
        db.commit()
        return n


def get_legs(parent_id: str) -> List[Any]:
    """Return child orders (TP + SL) of a bracket parent."""
    c = paper_trader._get_client()
    if not c:
        return []
    try:
        parent = c.get_order_by_id(parent_id)
        return list(parent.legs or [])
    except Exception as e:
        logger.warning(f"could not fetch legs of {parent_id}: {e}")
        return []


def identify_legs(parent_id: str) -> Dict[str, Optional[str]]:
    """Return {'stop_id': ..., 'tp_id': ...} for a bracket parent."""
    out: Dict[str, Optional[str]] = {"stop_id": None, "tp_id": None}
    for leg in get_legs(parent_id):
        otype = str(getattr(leg, "order_type", "")).lower()
        if "stop" in otype:
            out["stop_id"] = str(leg.id)
        elif "limit" in otype:
            out["tp_id"] = str(leg.id)
    return out


def replace_stop(stop_order_id: str, new_stop: float) -> Optional[str]:
    """Move the SL child order to a new stop price.

    r42 fix #0.2: returns the NEW broker order id on success (Alpaca rotates
    the id on every replace), or None on failure. Callers MUST persist the
    returned id back to `t.stop_order_id` — without that, the next replace
    targets a now-terminal id and the broker has no working SL until the
    bracket parent regenerates one (naked-long window).

    The legacy True/False contract is preserved at the call sites by
    `bool(returned)`; existing `if replace_stop(...)` patterns continue to
    work. The new id is what makes future replaces actually land on a live
    order.
    """
    rounded = round(float(new_stop), 2)
    # Idempotency: if we already sent this exact stop price for this order
    # (and Alpaca accepted it), skip the round-trip and return the same id.
    last = _rsc_get(stop_order_id)
    if last is not None and abs(last - rounded) < 0.005:
        return stop_order_id
    c = paper_trader._get_client()
    if not c:
        logger.warning(f"replace_stop {stop_order_id}: no broker client — keeping old stop")
        return None
    try:
        from alpaca.trading.requests import ReplaceOrderRequest
        new_order = c.replace_order_by_id(
            stop_order_id,
            order_data=ReplaceOrderRequest(stop_price=rounded),
        )
        new_id = str(getattr(new_order, "id", "") or stop_order_id)
        _rsc_set(stop_order_id, rounded)
        _rsc_set(new_id, rounded)
        return new_id
    except Exception as e:
        err_lower = str(e).lower()
        if "already replaced" in err_lower:
            _rsc_set(stop_order_id, rounded)
            logger.debug(
                f"replace_stop {stop_order_id}: already-replaced (racing prior tick), "
                f"caching intent {rounded}"
            )
            return stop_order_id
        logger.error(
            f"replace_stop FAILED {stop_order_id} → {new_stop}: {e} "
            f"(broker stop unchanged, will retry next manage tick)"
        )
        return None


def replace_tp(tp_order_id: str, new_limit: float) -> Optional[str]:
    """r43 fix #0.5: replace the TP (limit-take-profit) child of a bracket.

    Mirrors `replace_stop` semantics — returns the rotated leg id on success
    so callers can persist it back to `t.tp_order_id`. Without this primitive
    the TP leg sat forever at the original `far_tp = entry + 10×risk` and
    NEVER reflected slippage shifts or T3 recalcs. A flash spike past
    `far_tp` filled at the gap-open price; the bot's "trailing-only exit"
    silently was not what the broker actually held.
    """
    rounded = round(float(new_limit), 2)
    last = _rsc_get(tp_order_id)
    if last is not None and abs(last - rounded) < 0.005:
        return tp_order_id
    c = paper_trader._get_client()
    if not c:
        logger.warning(f"replace_tp {tp_order_id}: no broker client — keeping old TP")
        return None
    try:
        from alpaca.trading.requests import ReplaceOrderRequest
        new_order = c.replace_order_by_id(
            tp_order_id,
            order_data=ReplaceOrderRequest(limit_price=rounded),
        )
        new_id = str(getattr(new_order, "id", "") or tp_order_id)
        _rsc_set(tp_order_id, rounded)
        _rsc_set(new_id, rounded)
        return new_id
    except Exception as e:
        err_lower = str(e).lower()
        if "already replaced" in err_lower:
            _rsc_set(tp_order_id, rounded)
            return tp_order_id
        logger.error(f"replace_tp FAILED {tp_order_id} → {new_limit}: {e}")
        return None


def force_close_trade(
    t: Any,   # AutoTrade
    db: Session,
    reason: str,
    summary: Dict[str, Any],
    status_override: Optional[str] = None,
    on_close: Optional[Any] = None,
) -> None:
    """Cancel any working broker orders and exit the position at market.

    `status_override` lets callers tag the reason (e.g. "closed_slippage"
    for runaway-fill rejects). Default is "closed_reverse" — distinct
    from "closed_stop"/"closed_target" so post-mortem analysis doesn't
    conflate an opposing-signal exit with a stop failure.

    `on_close` callback is fired after DB commit so callers can clean up
    their own state (e.g. target-touch counts).
    """
    # Deferred imports to avoid circulars during module load.
    from services.auto_trader import _current_price

    # r39 audit critical-5: previously, broker-call failure here returned
    # early WITHOUT updating status, raising an alert, or resubmitting an
    # SL leg. For stocks the existing bracket was canceled BEFORE the
    # close attempt, so a failure left the position naked-long with no
    # downside protection. The DB row stayed `open` and the manage loop
    # silently re-tried each tick — log spam, no escalation.
    # New behavior: raise a critical alert, attempt a fresh SL resubmit
    # to keep the position covered, mark the row `error` if everything
    # fails. The position is still naked until the operator intervenes,
    # but at least the operator is loudly informed.
    if t.asset_type == "stock":
        # r52 fix: cancel ALL open orders for the symbol, not just
        # parent_order_id. Adopted positions have no parent_order_id, but
        # the user (or a prior bracket leg) may have left a working stop
        # order that holds the qty `held_for_orders` — Alpaca then rejects
        # the close with "insufficient qty available for order
        # (requested: N, available: 0)". This blocks manual close from
        # the UI, force-stop-out, etc. Cancelling by symbol is idempotent
        # and covers parent-order-id, bracket legs, and operator orders.
        try:
            cancel_res = paper_trader.cancel_all_orders(symbol=t.ticker)
            if isinstance(cancel_res, dict) and cancel_res.get("error"):
                logger.warning(f"reverse-close cancel_all_orders {t.ticker}: {cancel_res['error']}")
        except Exception as e:
            logger.warning(f"reverse-close cancel_all_orders {t.ticker} failed: {e}")
        # r52 fix: brief settle so the broker's qty-availability bookkeeping
        # picks up the cancellations before we submit the close. Without
        # this, Alpaca's API can still report the qty as held_for_orders
        # for a few hundred ms after cancel.
        try:
            import time as _t_settle
            _t_settle.sleep(0.5)
        except Exception:
            pass
        res = paper_trader.close_position(t.ticker)
        if "error" in res:
            from services.alerts import alert as _raise_alert
            _raise_alert(
                "error", "force_close_failed",
                f"close_position {t.ticker} failed: {res['error']}; position naked-long, "
                f"trade #{t.id} marked error pending operator intervention",
                ticker=t.ticker, trade_id=t.id,
            )
            # Try to put a fresh stop back on the open position so we're
            # not unprotected while the operator sees the alert.
            try:
                if t.current_stop and t.qty:
                    from alpaca.trading.requests import StopOrderRequest
                    from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                    c = paper_trader._get_client()
                    c.submit_order(order_data=StopOrderRequest(
                        symbol=t.ticker, qty=int(t.qty), side=_OS.SELL,
                        time_in_force=_TIF.GTC, stop_price=float(t.current_stop),
                    ))
                    logger.warning(
                        f"force_close_failed: resubmitted stop for {t.ticker} "
                        f"@ {t.current_stop} after close failure"
                    )
            except Exception as _se:
                logger.error(f"force_close_failed: SL resubmit also failed: {_se}")
            t.status = "error"
            t.note = (t.note or "") + f" | FORCE_CLOSE_FAILED: {res['error']}"
            db.commit()
            return
        px = _current_price(t.ticker)
        if px and t.entry_price:
            # r48 BACKLOG #concurrency-P1-11: atomic SQL accumulator (was
            # ORM read-modify-write, racing manage tick + AI exit threads).
            atomic_accumulate_realized_pl(db, t.id, (px - t.entry_price) * t.qty)
            db.refresh(t)
    else:
        # r43 fix #2.7: use marketable-limit-with-cross-fallback on emergency
        # option closes too — saves spread on the common case while still
        # guaranteeing flatten via the 20s market-cross fallback.
        sell = paper_trader.submit_option_exit_with_cross_fallback(
            occ_symbol=t.symbol, qty=int(t.qty), side="sell",
            cross_after_seconds=20.0,
        )
        if "error" in sell:
            from services.alerts import alert as _raise_alert
            _raise_alert(
                "error", "force_close_failed",
                f"option close {t.symbol} failed: {sell['error']}; position open, "
                f"trade #{t.id} marked error pending operator intervention",
                ticker=t.ticker, trade_id=t.id,
            )
            t.status = "error"
            t.note = (t.note or "") + f" | FORCE_CLOSE_FAILED: {sell['error']}"
            db.commit()
            return
        pos = paper_trader.get_option_position(t.symbol)
        if pos and pos.get("current_price") is not None and t.entry_price:
            # r48 BACKLOG #concurrency-P1-11: atomic SQL accumulator (option side).
            atomic_accumulate_realized_pl(
                db, t.id, (pos["current_price"] - t.entry_price) * t.qty * 100
            )
            db.refresh(t)

    t.status = status_override or "closed_reverse"
    t.closed_at = datetime.utcnow()
    t.note = (t.note or "") + f" | {t.status.upper()}: {reason}"
    # r44 fix #0.3: backfill MLPrediction outcome from force_close path too.
    try:
        from services.auto_trader import _backfill_ml_outcome as _bf
        _bf(db, t)
    except Exception:
        pass
    # r48 BACKLOG #lifecycle-P1-15: release BP reservation on every force-close
    # path (slippage-reject, news AI, reverse-thesis, time-stop, etc.). Prior
    # code only released on the manage-loop reconcile path, leaking reservations.
    try:
        from services.risk_manager import _release_bp as _rb_fc
        if (t.asset_type or "stock") == "stock":
            _rb_fc(float(t.entry_price or t.requested_entry or 0)
                   * float(t.original_qty or t.qty or 0))
        else:
            _rb_fc(float(t.entry_price or t.requested_entry or 0)
                   * float(t.original_qty or t.qty or 0) * 100.0)
    except Exception:
        pass
    db.commit()
    summary["closed"] = summary.get("closed", 0) + 1
    metrics.inc("autotrade_event", event=t.status)
    logger.warning(
        f"AutoTrader {t.status.upper()} {t.ticker} ({t.asset_type}) — {reason} "
        f"PL≈${(t.realized_pl or 0):.2f}"
    )
    # Broadcast a trade_closed event so the UI can surface a toast +
    # browser notification (paired with target_hit, the closure events
    # are the other half the operator wants pushed). Non-fatal — a
    # broadcast failure must not block the close path.
    try:
        from services import live_quotes as _lq
        _lq.broadcast_event_safe({
            "type": "trade_closed",
            "trade_id": t.id,
            "ticker": t.ticker,
            "asset_type": t.asset_type,
            "status": t.status,
            "reason": reason,
            "realized_pl": round(float(t.realized_pl or 0), 2),
        })
    except Exception:
        pass
    if on_close is not None:
        try:
            on_close(t)
        except Exception as e:
            logger.debug(f"force_close_trade on_close cb failed: {e}")
