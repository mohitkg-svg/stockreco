"""
Alpaca paper-trading wrapper.

Reads APCA_API_KEY_ID / APCA_API_SECRET_KEY from env. Always runs in paper mode
(paper=True) so live capital is never at risk by accident — flip the
ALPACA_LIVE=1 env var explicitly to upgrade to live trading later.

Exposes account/positions/orders queries plus order submission. For BUY/SELL
signals from the analyzer we use BRACKET orders so the broker holds the stop
and take-profit alongside the entry — no need for the app to monitor exits.
"""
from __future__ import annotations
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_client = None
_init_attempted = False


def _get_client():
    """Lazy-init the Alpaca TradingClient. Returns None if creds are missing."""
    global _client, _init_attempted
    if _client is not None or _init_attempted:
        return _client
    _init_attempted = True
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        logger.warning("APCA creds missing — paper trading disabled")
        return None
    try:
        from alpaca.trading.client import TradingClient
        paper = os.getenv("ALPACA_LIVE", "0") != "1"
        _client = TradingClient(key, secret, paper=paper)
        logger.info(f"Paper trading client ready (paper={paper})")
        return _client
    except Exception as e:
        logger.error(f"Could not init TradingClient: {e}")
        return None


def is_enabled() -> bool:
    return _get_client() is not None


_market_clock_cache: Optional[tuple] = None  # (is_open: bool, expiry_ts: float)


def is_market_open() -> bool:
    """True if US equity/options market is currently open. Cached 30s.

    Cheap gate to keep auto-trader from submitting option market orders
    outside RTH — Alpaca rejects those with code 42210000 and the retry
    storm (one submission per 15-min scan) was filling auto_trades with
    dead error rows.
    """
    import time as _t
    global _market_clock_cache
    now = _t.time()
    if _market_clock_cache and now < _market_clock_cache[1]:
        return _market_clock_cache[0]
    c = _get_client()
    if not c:
        return False
    try:
        clk = c.get_clock()
        is_open = bool(clk.is_open)
    except Exception as e:
        logger.warning(f"get_clock failed: {e}")
        return False
    _market_clock_cache = (is_open, now + 30.0)
    return is_open


def minutes_to_close() -> Optional[float]:
    """Minutes until the next regular-session close, or None if unavailable.
    Returns a negative number if market is currently closed and next_close is
    actually the *next* session's close (caller should treat that as "closed").
    """
    c = _get_client()
    if not c:
        return None
    try:
        clk = c.get_clock()
        if not clk.is_open:
            return None
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        nc = clk.next_close
        if nc.tzinfo is None:
            nc = nc.replace(tzinfo=_dt.timezone.utc)
        delta = (nc - now).total_seconds() / 60.0
        return max(0.0, delta)
    except Exception as e:
        logger.warning(f"minutes_to_close failed: {e}")
        return None


def get_account() -> Optional[Dict[str, Any]]:
    c = _get_client()
    if not c:
        return None
    try:
        a = c.get_account()
        return {
            "account_number": a.account_number,
            "status": str(a.status),
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "equity": float(a.equity),
            "portfolio_value": float(a.portfolio_value),
            "currency": a.currency,
            "pattern_day_trader": bool(a.pattern_day_trader),
            "trading_blocked": bool(a.trading_blocked),
            "paper": os.getenv("ALPACA_LIVE", "0") != "1",
        }
    except Exception as e:
        logger.error(f"get_account failed: {e}")
        return None


def get_positions() -> List[Dict[str, Any]]:
    c = _get_client()
    if not c:
        return []
    try:
        positions = c.get_all_positions()
    except Exception as e:
        logger.error(f"get_positions failed: {e}")
        return []
    out = []
    for p in positions:
        out.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": str(p.side),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price) if p.current_price else None,
            "market_value": float(p.market_value),
            "cost_basis": float(p.cost_basis),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc) * 100,
            "asset_class": str(p.asset_class),
        })
    return out


def get_orders(status: str = "all", limit: int = 50) -> List[Dict[str, Any]]:
    c = _get_client()
    if not c:
        return []
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        status_map = {
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all": QueryOrderStatus.ALL,
        }
        req = GetOrdersRequest(status=status_map.get(status, QueryOrderStatus.ALL), limit=limit)
        orders = c.get_orders(filter=req)
    except Exception as e:
        logger.error(f"get_orders failed: {e}")
        return []
    out = []
    for o in orders:
        out.append({
            "id": str(o.id),
            "client_order_id": o.client_order_id,
            "symbol": o.symbol,
            "qty": float(o.qty) if o.qty else None,
            "filled_qty": float(o.filled_qty) if o.filled_qty else 0,
            "side": str(o.side),
            "type": str(o.order_type),
            "order_class": str(o.order_class) if o.order_class else None,
            "limit_price": float(o.limit_price) if o.limit_price else None,
            "stop_price": float(o.stop_price) if o.stop_price else None,
            "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
            "status": str(o.status),
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        })
    return out


def submit_bracket_order(
    symbol: str,
    qty: float,
    side: str,                # "buy" or "sell"
    entry_type: str = "market",  # "market" or "limit"
    limit_price: Optional[float] = None,
    take_profit: Optional[float] = None,
    stop_loss: Optional[float] = None,
    time_in_force: str = "day",
    client_order_id: Optional[str] = None,
    extended_hours: bool = False,  # Algo Trader Plus: trade pre/post market
) -> Dict[str, Any]:
    """
    Submit a bracket order: parent entry + take-profit + stop-loss as one unit.
    Alpaca holds the exits; if either fills, the other is auto-cancelled.

    `client_order_id` (B3 fix) — caller-provided idempotency token. Alpaca
    treats duplicate client_order_ids as the same order, so if the app
    crashes between submit and DB-commit and retries, we won't end up with
    two parent brackets on the same ticker. Alpaca restricts the string to
    [A-Za-z0-9._-]{1,48}; sanitise on the way in.

    Returns the created order dict (or {"error": "..."} on failure).
    """
    c = _get_client()
    if not c:
        return {"error": "Alpaca client not initialized"}

    from alpaca.trading.requests import (
        MarketOrderRequest, LimitOrderRequest,
        TakeProfitRequest, StopLossRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    tif_map = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "opg": TimeInForce.OPG,
        "ioc": TimeInForce.IOC,
    }
    tif = tif_map.get(time_in_force.lower(), TimeInForce.DAY)

    tp = TakeProfitRequest(limit_price=round(float(take_profit), 2)) if take_profit else None
    sl = StopLossRequest(stop_price=round(float(stop_loss), 2)) if stop_loss else None

    # Bracket only valid when both exits are present
    use_bracket = tp is not None and sl is not None
    order_class = OrderClass.BRACKET if use_bracket else OrderClass.SIMPLE

    common_kwargs = dict(
        symbol=symbol.upper(),
        qty=float(qty),
        side=side_enum,
        time_in_force=tif,
        order_class=order_class,
    )
    if client_order_id:
        import re as _re
        sanitised = _re.sub(r"[^A-Za-z0-9._-]", "", str(client_order_id))[:48]
        if sanitised:
            common_kwargs["client_order_id"] = sanitised
    if use_bracket:
        common_kwargs["take_profit"] = tp
        common_kwargs["stop_loss"] = sl

    # Extended-hours orders: Alpaca only accepts them on DAY-TIF LIMIT orders.
    # Silently downgrade to regular-session if extended_hours is True but the
    # order geometry doesn't support it — avoids rejection at submit time.
    _extended = bool(extended_hours) and entry_type.lower() == "limit" and tif == TimeInForce.DAY
    if _extended:
        common_kwargs["extended_hours"] = True

    try:
        if entry_type.lower() == "limit":
            if limit_price is None:
                return {"error": "limit_price required for limit orders"}
            req = LimitOrderRequest(limit_price=round(float(limit_price), 2), **common_kwargs)
        else:
            req = MarketOrderRequest(**common_kwargs)
        o = c.submit_order(order_data=req)
        return {
            "id": str(o.id),
            "symbol": o.symbol,
            "side": str(o.side),
            "qty": float(o.qty) if o.qty else None,
            "type": str(o.order_type),
            "order_class": str(o.order_class) if o.order_class else None,
            "limit_price": float(o.limit_price) if o.limit_price else None,
            "status": str(o.status),
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
        }
    except Exception as e:
        logger.error(f"submit_order failed: {e}")
        return {"error": str(e)}


def cancel_order(order_id: str) -> Dict[str, Any]:
    c = _get_client()
    if not c:
        return {"error": "Alpaca client not initialized"}
    try:
        c.cancel_order_by_id(order_id)
        return {"id": order_id, "status": "cancelled"}
    except Exception as e:
        return {"error": str(e)}


def cancel_all_orders(symbol: Optional[str] = None) -> Dict[str, Any]:
    """Cancel every OPEN order, optionally filtered to one ticker.

    Alpaca's `cancel_orders()` has no symbol filter, so we fetch the open
    book and cancel by id when `symbol` is given. Returns per-id results
    plus a summary so the caller can see which legs actually cancelled
    (some may already be `filled`/`cancelled` and error on re-cancel —
    those count as successful no-ops here).
    """
    c = _get_client()
    if not c:
        return {"error": "Alpaca client not initialized"}
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        orders = c.get_orders(filter=req)
    except Exception as e:
        logger.error(f"cancel_all_orders list failed: {e}")
        return {"error": str(e)}

    sym = symbol.upper() if symbol else None
    targeted = [o for o in orders if (not sym) or (o.symbol or "").upper() == sym]
    cancelled, failed = [], []
    for o in targeted:
        try:
            c.cancel_order_by_id(str(o.id))
            cancelled.append({"id": str(o.id), "symbol": o.symbol, "side": str(o.side)})
        except Exception as e:
            # Already-terminal orders throw 422 — treat as no-op, not failure.
            # Postmortem fix M2: 404 / "not found" / "does not exist" also
            # means the order is already gone (canceled by another caller or
            # filled+removed) — treat as success too.
            msg = str(e)
            msg_l = msg.lower()
            if (
                "422" in msg
                or "404" in msg
                or "already" in msg_l
                or "not found" in msg_l
                or "does not exist" in msg_l
            ):
                cancelled.append({"id": str(o.id), "symbol": o.symbol, "side": str(o.side), "note": "already terminal"})
            else:
                failed.append({"id": str(o.id), "symbol": o.symbol, "error": msg})
    return {
        "symbol": sym,
        "total_open": len(orders),
        "targeted": len(targeted),
        "cancelled": len(cancelled),
        "failed": len(failed),
        "details": {"cancelled": cancelled, "failed": failed},
    }


def close_position(symbol: str) -> Dict[str, Any]:
    c = _get_client()
    if not c:
        return {"error": "Alpaca client not initialized"}
    try:
        o = c.close_position(symbol.upper())
        return {
            "id": str(o.id),
            "symbol": o.symbol,
            "side": str(o.side),
            "qty": float(o.qty) if o.qty else None,
            "status": str(o.status),
        }
    except Exception as e:
        return {"error": str(e)}


def submit_simple_option_order(
    occ_symbol: str,
    qty: int,
    side: str = "buy",
    order_type: str = "market",
    limit_price: Optional[float] = None,
    time_in_force: str = "day",
) -> Dict[str, Any]:
    """
    Submit a single-leg long-option order using the OCC contract symbol.

    Alpaca paper supports options when account-level options trading has been
    enabled (Lvl 1+ for long calls/puts). Bracket orders aren't allowed on
    options, so we use a SIMPLE order class and our own manage loop tracks the
    underlying for stop / target exits.
    """
    c = _get_client()
    if not c:
        return {"error": "Alpaca client not initialized"}

    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    tif_map = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "opg": TimeInForce.OPG,
        "ioc": TimeInForce.IOC,
    }
    tif = tif_map.get(time_in_force.lower(), TimeInForce.DAY)
    common = dict(
        symbol=occ_symbol,
        qty=int(qty),
        side=side_enum,
        time_in_force=tif,
        order_class=OrderClass.SIMPLE,
    )
    try:
        if order_type.lower() == "limit":
            if limit_price is None:
                return {"error": "limit_price required for limit option orders"}
            req = LimitOrderRequest(limit_price=round(float(limit_price), 2), **common)
        else:
            req = MarketOrderRequest(**common)
        o = c.submit_order(order_data=req)
        return {
            "id": str(o.id),
            "symbol": o.symbol,
            "side": str(o.side),
            "qty": float(o.qty) if o.qty else None,
            "type": str(o.order_type),
            "status": str(o.status),
            "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        }
    except Exception as e:
        logger.error(f"submit_simple_option_order failed for {occ_symbol}: {e}")
        return {"error": str(e)}


def get_option_position(occ_symbol: str) -> Optional[Dict[str, Any]]:
    """Return current position for an OCC option symbol, or None if no position."""
    c = _get_client()
    if not c:
        return None
    try:
        p = c.get_open_position(occ_symbol)
    except Exception:
        return None
    return {
        "symbol": p.symbol,
        "qty": float(p.qty),
        "avg_entry_price": float(p.avg_entry_price),
        "current_price": float(p.current_price) if p.current_price else None,
        "market_value": float(p.market_value),
        "unrealized_pl": float(p.unrealized_pl),
    }


def close_all_positions(cancel_orders: bool = True) -> Dict[str, Any]:
    c = _get_client()
    if not c:
        return {"error": "Alpaca client not initialized"}
    try:
        results = c.close_all_positions(cancel_orders=cancel_orders)
        return {"closed": [str(r.symbol) for r in results] if results else []}
    except Exception as e:
        return {"error": str(e)}
