"""
TWAP / VWAP Execution Engine
Slices large Kelly-sized orders into smaller child orders to minimize impact slippage.
"""
import logging
import time
from typing import Dict, Any

logger = logging.getLogger(__name__)

_active_twaps: Dict[str, Dict[str, Any]] = {}

def start_twap(ticker: str, total_qty: int, side: str, duration_minutes: int = 15) -> str:
    import uuid
    twap_id = uuid.uuid4().hex[:8]
    _active_twaps[twap_id] = {
        "ticker": ticker, "total_qty": total_qty, "filled_qty": 0,
        "side": side, "start_time": time.time(),
        "duration_sec": duration_minutes * 60, "last_slice_time": 0
    }
    logger.info(f"Started TWAP {twap_id} for {ticker} {side} {total_qty} over {duration_minutes}m")
    return twap_id

def tick_twaps():
    from services import alpaca_client
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    if not _active_twaps:
        return
    now = time.time()
    completed = []
    c = alpaca_client._get_client()
    if not c:
        return
    for twap_id, state in _active_twaps.items():
        elapsed = now - state["start_time"]
        target_qty = state["total_qty"] if elapsed >= state["duration_sec"] else int(state["total_qty"] * (elapsed / state["duration_sec"]))
        slice_qty = target_qty - state["filled_qty"]
        if slice_qty > 0:
            try:
                req = MarketOrderRequest(
                    symbol=state["ticker"], qty=slice_qty,
                    side=OrderSide.BUY if state["side"] == "buy" else OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                c.submit_order(order_data=req)
                state["filled_qty"] += slice_qty
            except Exception as e:
                logger.warning(f"TWAP {twap_id} slice failed: {e}")
        if state["filled_qty"] >= state["total_qty"]:
            completed.append(twap_id)
    for cid in completed: del _active_twaps[cid]