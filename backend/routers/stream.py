"""
Browser-facing WebSocket: /ws/quotes

Each connected client gets a per-session asyncio.Queue. The live_quotes module
broadcasts events (stock_trade, stock_quote, option_quote, signals_updated)
into every subscriber's queue, and this endpoint forwards them as JSON frames.

Clients can also POST-like messages to subscribe to specific symbols:
  {"action": "subscribe", "symbols": ["AAPL","TSLA"]}
though today the server already tracks the full watchlist, so this is mostly
a no-op for filtering on the client side.
"""
from __future__ import annotations

import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services import live_quotes
from routers._auth import verify_ws_token

router = APIRouter(tags=["stream"])
logger = logging.getLogger(__name__)


_QUEUE_MAX = 500
_COALESCE_TYPES = {"stock_quote", "stock_trade", "option_quote"}
# After this many consecutive QueueFull drops, the push() raises so the
# live_quotes broadcaster prunes us. Catches the case where a WebSocket
# silently died (network drop, browser tab closed without a clean close
# frame) but the unsubscribe() in the router's `finally` hasn't fired yet.
_DEAD_DROP_THRESHOLD = 200


@router.websocket("/ws/quotes")
async def quotes_ws(websocket: WebSocket):
    # Auth via ?token= query param (browsers can't set headers on WS).
    # Skipped when APP_API_KEY is unset (dev mode).
    token = websocket.query_params.get("token")
    if not verify_ws_token(token):
        # 1008 = policy violation — the standard close code for auth failure.
        await websocket.close(code=1008)
        return
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    # Per-symbol latest snapshot for coalescing fast-fire quote streams.
    # When the queue is near full we drop *intermediate* quote ticks for the
    # same symbol+type rather than the freshest one — bounded latency, no
    # signal loss for low-frequency events (signals_updated, snapshot, etc).
    latest: dict[tuple[str, str], dict] = {}
    drops = {"count": 0, "consecutive": 0}

    async def push(event):
        et = event.get("type") if isinstance(event, dict) else None
        sym = (event.get("symbol") or "") if isinstance(event, dict) else ""
        # For high-rate quote types: replace the in-queue copy by mutating
        # the shared dict in `latest`. The reader sees the freshest values.
        if et in _COALESCE_TYPES and sym:
            key = (et, sym)
            existing = latest.get(key)
            if existing is not None:
                existing.clear()
                existing.update(event)
                return
            latest[key] = event
        try:
            queue.put_nowait(event)
            drops["consecutive"] = 0
        except asyncio.QueueFull:
            drops["count"] += 1
            drops["consecutive"] += 1
            if drops["count"] % 100 == 1:
                logger.warning(f"quotes_ws backpressure: dropped {drops['count']} events")
            # Forget any coalesce ref for the dropped event so we don't keep mutating it.
            if et in _COALESCE_TYPES and sym:
                latest.pop((et, sym), None)
            # Subscriber appears dead — raise so live_quotes._broadcast prunes us.
            if drops["consecutive"] >= _DEAD_DROP_THRESHOLD:
                logger.warning(
                    f"quotes_ws subscriber dead (>{_DEAD_DROP_THRESHOLD} consecutive drops) — pruning"
                )
                raise RuntimeError("subscriber queue stuck")

    live_quotes.subscribe(push)

    # Send initial snapshot
    try:
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "stocks": live_quotes.all_stock_quotes(),
        }))
    except Exception:
        live_quotes.unsubscribe(push)
        return

    async def reader():
        """Drain client messages (subscribe/ping/etc.) — we mostly ignore."""
        try:
            while True:
                msg = await websocket.receive_text()
                # Client-side subscribe filter is advisory; no-op server-side.
                _ = msg
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    reader_task = asyncio.create_task(reader())

    try:
        while True:
            event = await queue.get()
            # Snapshot at send-time, then release the coalesce slot so the
            # next push for this (type, symbol) creates a fresh queue entry
            # rather than mutating an already-sent dict.
            et = event.get("type") if isinstance(event, dict) else None
            sym = (event.get("symbol") or "") if isinstance(event, dict) else ""
            payload = json.dumps(event, default=str)
            if et in _COALESCE_TYPES and sym:
                latest.pop((et, sym), None)
            await websocket.send_text(payload)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"quotes_ws closed: {e}")
    finally:
        live_quotes.unsubscribe(push)
        reader_task.cancel()
