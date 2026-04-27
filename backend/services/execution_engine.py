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

from sqlalchemy.orm import Session

from services import paper_trader, metrics

logger = logging.getLogger(__name__)

# Idempotency cache for stop-replacements. See `replace_stop` docstring.
_replace_stop_cache: Dict[str, float] = {}


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
    last = _replace_stop_cache.get(stop_order_id)
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
        _replace_stop_cache[stop_order_id] = rounded
        # Mirror the cache entry under the new id so a follow-up no-op
        # replace is still recognized as idempotent.
        _replace_stop_cache[new_id] = rounded
        return new_id
    except Exception as e:
        err_lower = str(e).lower()
        # Alpaca briefly reports "order already replaced" when our previous
        # replace is still settling. Our intent is accepted — record it and
        # treat as success so the DB advances.
        if "already replaced" in err_lower:
            _replace_stop_cache[stop_order_id] = rounded
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
        try:
            if t.parent_order_id:
                paper_trader.cancel_order(t.parent_order_id)
        except Exception as e:
            logger.warning(f"reverse-close cancel parent failed: {e}")
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
            # r42 fix #0.1: ADD runner-leg PnL to accumulated partial-trim
            # PnL; prior `=` overwrite erased T1/T2 partials.
            existing = float(t.realized_pl or 0.0)
            t.realized_pl = round(existing + (px - t.entry_price) * t.qty, 2)
    else:
        sell = paper_trader.submit_simple_option_order(
            occ_symbol=t.symbol, qty=int(t.qty), side="sell",
            order_type="market", time_in_force="day",
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
            # r42 fix #0.1: ADD runner-leg PnL to accumulated partial PL.
            existing = float(t.realized_pl or 0.0)
            t.realized_pl = round(existing + (pos["current_price"] - t.entry_price) * t.qty * 100, 2)

    t.status = status_override or "closed_reverse"
    t.closed_at = datetime.utcnow()
    t.note = (t.note or "") + f" | {t.status.upper()}: {reason}"
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
