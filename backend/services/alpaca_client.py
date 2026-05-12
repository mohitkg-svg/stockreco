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
import time
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


# r80: timeout + retry helper for idempotent Alpaca REST reads.
#
# alpaca-py's underlying httpx client has no public timeout setting and
# defaults to None (wait forever). A network stall on get_account() at the
# top of consider_signal would block the entire entry pipeline. Wrap the
# critical reads with a short timeout + 1 retry on transient errors so a
# bad packet doesn't paralyze the bot.
import concurrent.futures as _cf  # noqa: E402

_REST_TIMEOUT_SEC = 8.0   # r87: 5.0 → 8.0 after 2026-05-11 rehearsal showed
                          # 220 × `get_account timed out after 5.0s` in one
                          # session — Alpaca REST p99 routinely exceeds 5s.
_REST_RETRIES = 2         # r87: 1 → 2 retries (3 attempts total) so a single
                          # transient stall doesn't immediately propagate as
                          # a None and disable downstream gates.
_REST_BACKOFF_SEC = 0.4

# Persistent daemon-thread executor: a `with ThreadPoolExecutor()` block
# calls shutdown(wait=True) on exit, which would BLOCK on the timed-out
# thread and defeat the timeout. A long-lived shared executor avoids that
# trap — when future.result() times out, we abandon the future and return.
# The hung thread continues until its socket eventually errors out, but
# the caller is unblocked.
_REST_EXECUTOR = _cf.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="alpaca-rest"
)


def _safe_rest_read(fn, *args, timeout: float = _REST_TIMEOUT_SEC,
                    retries: int = _REST_RETRIES, **kwargs):
    """Run an idempotent Alpaca REST read with a hard timeout + retry on
    transient errors (429 / 5xx / timeout). NOT for order submits — those
    are non-idempotent and must never auto-retry from inside the SDK."""
    import time as _t
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            future = _REST_EXECUTOR.submit(fn, *args, **kwargs)
            return future.result(timeout=timeout)
        except _cf.TimeoutError as e:
            last_exc = e
            # Abandon the future: don't wait for the hung call.
            future.cancel()
            logger.warning(
                f"_safe_rest_read: {fn.__name__} timed out after {timeout}s "
                f"(attempt {attempt + 1}/{retries + 1})"
            )
        except Exception as e:
            last_exc = e
            # r80 round-2 hardening: defensive str() — some SDK exceptions
            # have broken __str__/__repr__; treat as non-transient if so.
            try:
                err_s = (str(e) or "").lower()
            except Exception:
                err_s = ""
            if any(t in err_s for t in (
                "429", "500", "502", "503", "504",
                "timeout", "timed out", "connection",
            )):
                logger.warning(
                    f"_safe_rest_read: {fn.__name__} transient error "
                    f"(attempt {attempt + 1}/{retries + 1}): {e}"
                )
            else:
                # Non-transient (auth, malformed, etc.) — don't retry.
                raise
        if attempt < retries:
            _t.sleep(_REST_BACKOFF_SEC * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    return None


_market_clock_cache: Optional[tuple] = None  # (is_open: bool, expiry_ts: float)
import threading as _pt_threading
_market_clock_lock = _pt_threading.Lock()
_market_clock_inflight = _pt_threading.Event()
_market_clock_inflight.set()  # initial state: not in-flight


def is_market_open() -> bool:
    """True if US equity/options market is currently open. Cached 30s.

    r48 BACKLOG #concurrency-P0-5: single-flight via threading.Event so
    concurrent callers after expiry don't all hit `c.get_clock()`. Lock
    guards the read+write of the cache tuple.
    """
    import time as _t
    global _market_clock_cache
    now = _t.time()
    with _market_clock_lock:
        if _market_clock_cache and now < _market_clock_cache[1]:
            return _market_clock_cache[0]
        # Decide whether THIS thread will do the broker call.
        if not _market_clock_inflight.is_set():
            # Another thread is fetching. Drop lock + wait briefly.
            pass
        else:
            _market_clock_inflight.clear()
            in_flight_owner = True
        in_flight_owner = locals().get("in_flight_owner", False)
    if not in_flight_owner:
        # Wait up to 5s for the in-flight call to finish, then re-read cache.
        _market_clock_inflight.wait(timeout=5.0)
        with _market_clock_lock:
            if _market_clock_cache and now < _market_clock_cache[1]:
                return _market_clock_cache[0]
        # Fall through: do our own fetch as a last resort.
    # r82: pre-initialize is_open so the finally-block read is safe even when
    # the early `return False` (line: `if not c`) path bypasses both branches
    # of the inner try/except. Prior code used `'is_open' in dir()` which
    # checks module attribute names, not local variables — wrong namespace.
    is_open = False
    try:
        c = _get_client()
        if not c:
            return False
        # r80 round-2: wrap with timeout. A network stall on /v2/clock
        # would otherwise hold _market_clock_inflight clear() forever
        # and block every subsequent caller's wait(timeout=5).
        clk = _safe_rest_read(c.get_clock, timeout=6.0)  # r87: 3.0 → 6.0
        is_open = bool(clk.is_open) if clk is not None else False
    except Exception as e:
        logger.warning(f"get_clock failed: {e}")
        is_open = False
    finally:
        with _market_clock_lock:
            _market_clock_cache = (is_open, _t.time() + 30.0)
        try:
            _market_clock_inflight.set()
        except Exception:
            pass
    return _market_clock_cache[0] if _market_clock_cache else False


def minutes_to_close() -> Optional[float]:
    """Minutes until the next regular-session close, or None if unavailable.
    Returns a negative number if market is currently closed and next_close is
    actually the *next* session's close (caller should treat that as "closed").
    """
    c = _get_client()
    if not c:
        return None
    try:
        # r80c: timeout-wrap. A network stall on /v2/clock used to
        # block promote_adopted + EOD-flatten paths indefinitely.
        clk = _safe_rest_read(c.get_clock, timeout=6.0)  # r87: 3.0 → 6.0
        if clk is None or not clk.is_open:
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


def minutes_since_open() -> Optional[float]:
    """Minutes since the most recent regular-session open, or None if market
    is currently closed. Used to gate options entries during the wide-spread
    opening period."""
    c = _get_client()
    if not c:
        return None
    try:
        # r80c: timeout-wrap.
        clk = _safe_rest_read(c.get_clock, timeout=6.0)  # r87: 3.0 → 6.0
        if clk is None or not clk.is_open:
            return None
        import datetime as _dt
        # If market is open, the previous open is `next_open - 24h * trading_days_back`.
        # Easier: use Alpaca's clock — when is_open=True, the session started at
        # 9:30 ET. We don't have direct prev_open, but next_close gives session end;
        # session start is next_close - 6.5h.
        nc = clk.next_close
        if nc.tzinfo is None:
            nc = nc.replace(tzinfo=_dt.timezone.utc)
        session_open = nc - _dt.timedelta(hours=6, minutes=30)
        now = _dt.datetime.now(_dt.timezone.utc)
        delta = (now - session_open).total_seconds() / 60.0
        return max(0.0, delta)
    except Exception as e:
        logger.warning(f"minutes_since_open failed: {e}")
        return None


# r87: account snapshot cache. The 2026-05-11 rehearsal hit `get_account
# timed out after 5.0s` 156 times + `get_account failed` 64 times — because
# every consider_signal preflight, every risk_manager gate, and the EquitySnapshot
# job all call get_account() independently. With 5-10 signals per scan tick and
# 5 gates per signal, that's dozens of get_account() calls per minute, each
# costing up to 8s × 3 retries on a wedged broker connection. Cache for 10s
# so per-tick gates share one round-trip. TTL kept short so values stay fresh
# for daily-loss / equity gates that care about realized PnL movement.
_account_cache: Optional[tuple] = None  # (snapshot: dict, expiry_ts: float)
_account_cache_lock = _pt_threading.Lock()
_ACCOUNT_CACHE_TTL_SEC = 10.0


def _account_cache_clear() -> None:
    """Force-clear the account cache. Call after order submits / fills when
    the caller needs an up-to-the-moment buying_power read."""
    global _account_cache
    with _account_cache_lock:
        _account_cache = None


def get_account(force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    global _account_cache
    import time as _t
    if not force_refresh:
        with _account_cache_lock:
            if _account_cache and _t.time() < _account_cache[1]:
                return dict(_account_cache[0])
    c = _get_client()
    if not c:
        return None
    try:
        # r80: timeout + retry guard.
        a = _safe_rest_read(c.get_account)
        if a is None:
            # Timed out across retries — keep returning the stale snapshot
            # if we have one, so the gates don't all fail open on a transient
            # broker stall. Caller can call force_refresh=True if they need
            # certainty.
            with _account_cache_lock:
                if _account_cache:
                    logger.warning("get_account: REST returned None, serving stale cache")
                    return dict(_account_cache[0])
            logger.error("get_account: REST returned None and no cache available")
            return None
        snap = {
            "account_number": a.account_number,
            "status": str(a.status),
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "equity": float(a.equity),
            "portfolio_value": float(a.portfolio_value),
            "currency": a.currency,
            "pattern_day_trader": bool(a.pattern_day_trader),
            "trading_blocked": bool(a.trading_blocked),
            # r47 fix #T0d-1: r46 added consider_signal pre-flight checks for
            # `account_blocked` and `transfers_blocked` — but this dict never
            # populated those keys. The .get(...) fallback always returned
            # None / False so the gate was a silent no-op. Fix: surface the
            # actual Alpaca fields.
            "account_blocked": bool(getattr(a, "account_blocked", False)),
            "transfers_blocked": bool(getattr(a, "transfers_blocked", False)),
            "day_trade_count": int(getattr(a, "daytrade_count", 0) or 0),
            "paper": os.getenv("ALPACA_LIVE", "0") != "1",
        }
        with _account_cache_lock:
            _account_cache = (snap, _t.time() + _ACCOUNT_CACHE_TTL_SEC)
        return dict(snap)
    except Exception as e:
        logger.error(f"get_account failed: {e}")
        # r87: if we have a stale cache, prefer it over None so downstream
        # gates evaluate (with a stale equity reading) rather than fail
        # open by hitting `_acct_probe is None → _equity_probe = 0`.
        with _account_cache_lock:
            if _account_cache:
                logger.warning("get_account: exception, serving stale cache")
                return dict(_account_cache[0])
        return None


def get_positions() -> List[Dict[str, Any]]:
    c = _get_client()
    if not c:
        return []
    try:
        # r80: timeout + retry guard.
        positions = _safe_rest_read(c.get_all_positions)
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
        # r80: timeout + retry guard.
        orders = _safe_rest_read(c.get_orders, filter=req)
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
    # r46 fix #0.4: optional stop-LIMIT (vs stop-MARKET) to cap flash-crash /
    # halt-resume gap fills. When STOP_LIMIT_OFFSET_PCT > 0, we use a
    # stop-LIMIT with limit_price slightly worse than the stop (0.5% by
    # default for longs). Gap-throughs leave the order unfilled — the
    # manage loop's SL-invariant check (auto_trader.py) detects the
    # missing fill and re-submits / escalates as needed.
    sl = None
    if stop_loss:
        offset_pct = float(os.getenv("STOP_LIMIT_OFFSET_PCT", "0") or "0")
        stop_price_r = round(float(stop_loss), 2)
        if offset_pct > 0:
            is_buy = (side or "buy").lower() == "buy"
            limit_price = stop_price_r * (1 - offset_pct) if is_buy else stop_price_r * (1 + offset_pct)
            sl = StopLossRequest(stop_price=stop_price_r, limit_price=round(limit_price, 2))
        else:
            sl = StopLossRequest(stop_price=stop_price_r)

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
        # r87: invalidate account snapshot cache so the next gate read picks
        # up the post-submit buying_power immediately. Without this, the
        # 10s TTL could let the next signal's pos-sizer believe we still
        # have full BP and over-leverage.
        _account_cache_clear()
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


def cancel_order(order_id: str, wait_for_terminal: bool = True, timeout_sec: float = 4.0) -> Dict[str, Any]:
    """Cancel a single order. r53b: optionally polls for terminal state
    (canceled/filled/rejected/expired) before returning, so the caller
    sees the canonical post-cancel state — eliminates the UI flash where
    the cancel toast says "success" but the next /orders fetch (within
    1-2s) still shows the order as `new` because Alpaca hadn't finished
    processing the cancel yet.

    Returns:
        On success: {"id", "status": <terminal-status-or-pending_cancel>}
        On terminal-already error: {"id", "status": "already_terminal", "note": ...}
        On other failure: {"error": <message>}
    """
    c = _get_client()
    if not c:
        return {"error": "Alpaca client not initialized"}
    try:
        c.cancel_order_by_id(order_id)
    except Exception as e:
        msg = str(e)
        msg_l = msg.lower()
        # Already-terminal / already-pending-cancel are no-ops, not errors.
        if (
            "422" in msg
            or "404" in msg
            or "already" in msg_l
            or "not found" in msg_l
            or "does not exist" in msg_l
            or "order pending cancel" in msg_l
        ):
            return {"id": order_id, "status": "already_terminal", "note": msg[:200]}
        return {"error": msg}

    if not wait_for_terminal:
        return {"id": order_id, "status": "cancel_requested"}

    # Poll for terminal status. Most cancels resolve in <1s; we wait up
    # to `timeout_sec` total with short pauses to avoid burning API calls.
    import time as _t_cw
    deadline = _t_cw.time() + timeout_sec
    final_status = "pending_cancel"
    sleep_s = 0.25
    while _t_cw.time() < deadline:
        _t_cw.sleep(sleep_s)
        sleep_s = min(sleep_s * 1.5, 1.0)  # 0.25 → 0.375 → ...
        try:
            o = c.get_order_by_id(order_id)
            s = str(getattr(o, "status", "")).lower().split(".")[-1]
            final_status = s
            if s in ("canceled", "cancelled", "filled", "rejected", "expired", "done_for_day"):
                return {"id": order_id, "status": s}
        except Exception:
            # 404 = the order is gone (Alpaca purges some terminal states fast)
            return {"id": order_id, "status": "canceled"}
    # Timed out — order is still in pending_cancel. Caller can decide
    # what to surface (we report the state we last saw).
    return {"id": order_id, "status": final_status, "note": f"timeout {timeout_sec}s waiting for terminal state"}


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
        # r82: wrap with timeout. KILL flow during a network stall used to
        # hang here forever, leaving the operator unable to flatten.
        orders = _safe_rest_read(lambda: c.get_orders(filter=req), timeout=8.0) or []
    except Exception as e:
        logger.error(f"cancel_all_orders list failed: {e}")
        return {"error": str(e)}

    sym = symbol.upper() if symbol else None
    targeted = [o for o in orders if (not sym) or (o.symbol or "").upper() == sym]
    cancelled, failed = [], []
    for o in targeted:
        try:
            # r82: bound each cancel with a short timeout so a single hung
            # leg doesn't block the rest of the kill loop.
            _safe_rest_read(lambda: c.cancel_order_by_id(str(o.id)), timeout=4.0)
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
        # r82: timeout-wrap so an emergency flatten can't hang on a wedged
        # connection. close_position is non-idempotent — no retry on timeout.
        o = _safe_rest_read(lambda: c.close_position(symbol.upper()), timeout=8.0)
        if o is None:
            return {"error": "close_position timed out"}
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
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Submit a single-leg long-option order using the OCC contract symbol.

    Alpaca paper supports options when account-level options trading has been
    enabled (Lvl 1+ for long calls/puts). Bracket orders aren't allowed on
    options, so we use a SIMPLE order class and our own manage loop tracks the
    underlying for stop / target exits.

    `client_order_id` (r85c) — caller-provided idempotency token. Same
    semantics as the stock bracket path: Alpaca treats duplicate
    client_order_ids within ~24h as the same order, so multi-instance
    scans / app retries can't double-submit. Sanitised to [A-Za-z0-9._-]{1,48}.
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
    if client_order_id:
        import re as _re
        sanitised = _re.sub(r"[^A-Za-z0-9._-]", "", str(client_order_id))[:48]
        if sanitised:
            common["client_order_id"] = sanitised
    try:
        if order_type.lower() == "limit":
            if limit_price is None:
                return {"error": "limit_price required for limit option orders"}
            req = LimitOrderRequest(limit_price=round(float(limit_price), 2), **common)
        else:
            req = MarketOrderRequest(**common)
        o = c.submit_order(order_data=req)
        # r87: invalidate account cache post-submit (see submit_bracket_order).
        _account_cache_clear()
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


def submit_option_exit_marketable_limit(
    occ_symbol: str,
    qty: int,
    side: str = "sell",
    offset_cents: float = 0.05,
    fallback_to_market: bool = True,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """r42 fix #2.2: marketable-limit option exit.

    Reads the live OPRA NBBO via `live_quotes.get_option_quote`, places a
    limit at NBBO ± offset (whichever side fills the trade), and returns the
    submitted order. Falls back to a plain market order if the quote is
    unavailable AND `fallback_to_market` is True — preserves existing
    behavior on data outages while saving the spread on the 95% common case.

    Why: market orders on illiquid weekly options eat 5-15% of premium per
    trip. Even a marketable limit at the inside ask + $0.05 cuts that
    drastically without sacrificing fill probability.
    """
    # r43 fix #0.6: post INSIDE the spread first to capture price improvement.
    # Previous version (r42) sat at bid for sells / ask+offset for buys —
    # equivalent to a market order on wide books, defeating the purpose. Now:
    # SELL at `mid - offset` (post 1 tick inside the bid), BUY at `mid +
    # offset`. Caller may follow up with a market cross if no fill in N seconds
    # (see `submit_option_exit_marketable_limit_with_cross_fallback`).
    side = side.lower()
    px: Optional[float] = None
    try:
        from services import live_quotes as _lq
        q = _lq.get_option_quote(occ_symbol)
        if q:
            bid = q.get("bid"); ask = q.get("ask")
            if bid and ask and ask > bid:
                mid = (float(bid) + float(ask)) / 2.0
                if side == "sell":
                    px = max(float(bid), mid - float(offset_cents))
                else:  # buy
                    px = min(float(ask), mid + float(offset_cents))
            elif side == "sell" and bid:
                px = float(bid)
            elif side == "buy" and ask:
                px = float(ask) + float(offset_cents)
    except Exception:
        px = None
    if px is not None and px > 0:
        return submit_simple_option_order(
            occ_symbol=occ_symbol, qty=qty, side=side,
            order_type="limit", limit_price=round(px, 2),
            time_in_force="day",
            client_order_id=client_order_id,
        )
    if fallback_to_market:
        return submit_simple_option_order(
            occ_symbol=occ_symbol, qty=qty, side=side,
            order_type="market", time_in_force="day",
            client_order_id=client_order_id,
        )
    return {"error": "no quote available for marketable-limit"}


def submit_option_entry_with_cross_fallback(
    occ_symbol: str,
    qty: int,
    cross_after_seconds: float = 30.0,
    requested_premium: Optional[float] = None,
    max_fill_vs_requested: float = 1.25,
    wide_spread_pct: float = 0.30,
    wide_spread_cross_seconds: float = 120.0,
    client_order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """r48 #BACKLOG-options-P0-2: post a marketable-LIMIT BUY inside the
    spread first, cross to market if unfilled after N seconds.

    Prior to this primitive, option ENTRIES used `submit_simple_option_order`
    with `order_type="market"` — eating the full ask on every wide OPRA
    book.

    r53 fix (Tier-0 #1): three new safeties surfaced by VTWO ($1.55 →
    $4.90, +216%) / AMKR ($6.48 → $9.50, +47%) / RMBS (~$1.55 → $15.00)
    market-cross blow-ups documented in DESIGN.md changelog:

      1. **Slippage abandon** — if the market-cross would result in a
         fill > `max_fill_vs_requested` × `requested_premium`, we
         CANCEL the limit and ABANDON the trade rather than crossing
         past 1.25× requested. Caller can retry on the next manage tick
         when the spread tightens.
      2. **Wide-spread cross deferral** — when current ask/bid spread
         exceeds `wide_spread_pct` of mid (defaults: 30%), extend the
         marketable-limit window to `wide_spread_cross_seconds` (120s)
         instead of the default 30s. Wide books are wide because nobody
         wants to take that side; crossing immediately just hands the
         market-maker the spread.
      3. **Caller passes `requested_premium`** so the abandon math has
         a reference. When None (caller hasn't migrated), we fall through
         to the legacy "cross at 30s" behavior.

    Returns the FINAL submitted order dict (market cross if it fired,
    otherwise the inside-the-spread BUY limit, or `{"error": "..."}`
    on slippage-abandon).
    """
    # r53: spread-aware cross deadline. Look up current quote; if spread
    # is unusually wide, give the limit longer to fill at mid.
    effective_cross_seconds = cross_after_seconds
    try:
        from services.live_quotes import get_option_quote as _gq
        q = _gq(occ_symbol)
        if q:
            bid = float(q.get("bid") or 0)
            ask = float(q.get("ask") or 0)
            if bid > 0 and ask > bid:
                mid = (bid + ask) / 2.0
                spread_pct = (ask - bid) / mid if mid > 0 else 0
                if spread_pct > wide_spread_pct:
                    effective_cross_seconds = max(cross_after_seconds, wide_spread_cross_seconds)
                    logger.info(
                        f"submit_option_entry: {occ_symbol} wide spread "
                        f"{spread_pct*100:.0f}% (bid={bid} ask={ask}); "
                        f"extending cross window to {effective_cross_seconds}s"
                    )
    except Exception as _e:
        logger.debug(f"submit_option_entry spread-check {occ_symbol}: {_e}")

    # r85c: cross-fallback's two submits MUST use distinct client_order_ids,
    # otherwise Alpaca dedup returns the cancelled limit when the cross
    # fires and no market order is actually placed. Use the caller-provided
    # id as-is for the inside-spread limit; suffix "-x" for any fallback /
    # market-cross attempt downstream.
    cross_id = (str(client_order_id) + "-x") if client_order_id else None
    first = submit_option_exit_marketable_limit(
        occ_symbol=occ_symbol, qty=qty, side="buy", fallback_to_market=False,
        client_order_id=client_order_id,
    )
    if "error" in first or not first.get("id"):
        # Couldn't even submit the inside-spread limit; r53: ALSO honor
        # slippage cap on the market fallback path so a stale-quote
        # bug can't sneak past the abandon gate.
        if requested_premium and requested_premium > 0:
            try:
                from services.live_quotes import get_option_quote as _gq2
                q2 = _gq2(occ_symbol)
                if q2:
                    ask2 = float(q2.get("ask") or 0)
                    if ask2 > 0 and ask2 > requested_premium * max_fill_vs_requested:
                        logger.warning(
                            f"submit_option_entry ABANDON {occ_symbol}: ask "
                            f"${ask2:.2f} > {max_fill_vs_requested}×requested "
                            f"${requested_premium:.2f} — skip rather than cross"
                        )
                        return {
                            "error": "slippage_abandon",
                            "requested_premium": requested_premium,
                            "ask": ask2,
                            "max_allowed": requested_premium * max_fill_vs_requested,
                        }
            except Exception:
                pass
        return submit_simple_option_order(
            occ_symbol=occ_symbol, qty=qty, side="buy",
            order_type="market", time_in_force="day",
            client_order_id=cross_id,
        )
    order_id = first.get("id")
    deadline = time.time() + effective_cross_seconds
    while time.time() < deadline:
        time.sleep(min(2.0, deadline - time.time()))
        try:
            c = _get_client()
            if c:
                # r80c: timeout-wrap. Without this, a 30s broker stall
                # on get_order_by_id used to block the entire deadline
                # loop, then market-cross blindly even though the limit
                # may have filled — 2× the qty.
                o = _safe_rest_read(c.get_order_by_id, order_id, timeout=2.0)
                if o is not None:
                    status = str(getattr(o, "status", "")).lower()
                    if "filled" in status:
                        return first
        except Exception:
            break
    try:
        # r80 fix: was wait_for_terminal=False (fire-and-forget) — that
        # left a race window where the limit could fill DURING the cancel
        # ACK while the market cross below also fired = 2× the qty filled.
        # Wait up to 2s for the cancel to terminalize. If the limit fills
        # before cancel completes, cancel_order returns the now-filled
        # order dict; we fall through and let the market cross logic also
        # see "filled" and skip submitting another order.
        cancel_res = cancel_order(order_id, wait_for_terminal=True, timeout_sec=2.0)
        # If cancel races with a fill, the order is now terminal-filled.
        # Re-poll once to confirm and bail out before crossing.
        try:
            c = _get_client()
            if c:
                # r80c: timeout-wrap.
                o = _safe_rest_read(c.get_order_by_id, order_id, timeout=2.0)
                if o is not None:
                    _final_status = str(getattr(o, "status", "")).lower()
                    if "filled" in _final_status:
                        logger.info(
                            f"submit_option_entry: {occ_symbol} limit filled during "
                            f"cancel race; skipping market cross."
                        )
                        return first
        except Exception:
            pass
    except Exception:
        pass
    # r53: pre-cross slippage gate. If the current ask is more than
    # max_fill_vs_requested × requested_premium, abandon rather than
    # cross. This is the load-bearing fix for VTWO/AMKR/RMBS.
    if requested_premium and requested_premium > 0:
        try:
            from services.live_quotes import get_option_quote as _gq3
            q3 = _gq3(occ_symbol)
            if q3:
                ask3 = float(q3.get("ask") or 0)
                if ask3 > 0 and ask3 > requested_premium * max_fill_vs_requested:
                    logger.warning(
                        f"submit_option_entry ABANDON {occ_symbol}: ask "
                        f"${ask3:.2f} > {max_fill_vs_requested}×requested "
                        f"${requested_premium:.2f} after {effective_cross_seconds}s "
                        f"window — skip rather than cross to market"
                    )
                    try:
                        from services.alerts import alert as _raise_alert
                        _raise_alert(
                            "warning", "option_entry_slippage_abandon",
                            f"{occ_symbol} abandoned: ask ${ask3:.2f} > "
                            f"{max_fill_vs_requested}×requested ${requested_premium:.2f}",
                            ticker=occ_symbol[:6].rstrip("0123456789"),
                        )
                    except Exception:
                        pass
                    return {
                        "error": "slippage_abandon",
                        "requested_premium": requested_premium,
                        "ask": ask3,
                        "max_allowed": requested_premium * max_fill_vs_requested,
                    }
        except Exception as _e:
            logger.debug(f"submit_option_entry post-cross check {occ_symbol}: {_e}")
    return submit_simple_option_order(
        occ_symbol=occ_symbol, qty=qty, side="buy",
        order_type="market", time_in_force="day",
        client_order_id=cross_id,
    )


def submit_option_exit_with_cross_fallback(
    occ_symbol: str,
    qty: int,
    side: str = "sell",
    cross_after_seconds: float = 20.0,
) -> Dict[str, Any]:
    """r43 fix #0.6 sibling: post inside the spread, watch for fill, cross to
    market if not filled within `cross_after_seconds`. Returns the FINAL
    submitted order dict (cross order if it fired, otherwise the inside-the-
    spread limit).

    This is the right primitive for emergency closes (force_close, news_exit,
    end-of-day flatten) where we want price improvement BUT must guarantee
    flattening before session-end.
    """
    first = submit_option_exit_marketable_limit(
        occ_symbol=occ_symbol, qty=qty, side=side, fallback_to_market=False,
    )
    if "error" in first or not first.get("id"):
        # Couldn't even submit the inside-spread limit; fall back to market.
        return submit_simple_option_order(
            occ_symbol=occ_symbol, qty=qty, side=side,
            order_type="market", time_in_force="day",
        )
    order_id = first.get("id")
    deadline = time.time() + cross_after_seconds
    while time.time() < deadline:
        time.sleep(min(2.0, deadline - time.time()))
        try:
            c = _get_client()
            if c:
                # r84: timeout-wrap the poll. Inside a tight deadline loop,
                # an unwrapped REST call could exceed `cross_after_seconds`
                # by SDK default timeout (~30s) — defeating the purpose of
                # the deadline and stalling the option exit pipeline.
                o = _safe_rest_read(c.get_order_by_id, order_id, timeout=2.0)
                status = str(getattr(o, "status", "")).lower() if o is not None else ""
                if "filled" in status:
                    return first
        except Exception:
            break
    # Not filled — cancel + market cross.
    try:
        # Internal cancel before market cross — don't add 4s latency to
        # the entry path; just fire-and-forget.
        cancel_order(order_id, wait_for_terminal=False)
    except Exception:
        pass
    return submit_simple_option_order(
        occ_symbol=occ_symbol, qty=qty, side=side,
        order_type="market", time_in_force="day",
    )


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
