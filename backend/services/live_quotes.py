"""
Real-time quote manager.

Connects to Alpaca's stock WebSocket feed for all watchlist tickers, keeps an
in-memory cache of the latest {bid, ask, last, ts} per symbol, and fans out
updates to both:
  1) Browser clients (via stream_hub asyncio queues — see routers/stream.py)
  2) A live-recompute scheduler that re-runs signal generation for a ticker
     when its price moves meaningfully.

Credentials: reads APCA_API_KEY_ID and APCA_API_SECRET_KEY from env.
If credentials are missing, the manager stays in a no-op state and the app
continues working with Yahoo-polled prices.

Options quotes: Alpaca's options WebSocket requires a paid data plan. We expose
`update_option_quote()` so a separate polling task (or future upgrade) can push
option prices through the same fan-out path, and `get_option_quote()` for
readers. For now the options REST chain remains the source of truth; the live
layer overlays stock quotes and whatever option ticks we receive.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# In-memory caches
# ------------------------------------------------------------------
# symbol -> {"bid": float, "ask": float, "last": float, "ts": float, "prev_close": float}
_stock_quotes: Dict[str, Dict[str, float]] = {}
# "TICKER|OCC_SYMBOL" -> {"bid", "ask", "last", "ts"}
_option_quotes: Dict[str, Dict[str, float]] = {}

# Subscribers that want every update pushed to them.
# Each subscriber is an async callable: await cb(event_dict)
_subscribers: Set[Callable[[Dict[str, Any]], Awaitable[None]]] = set()

# Set of currently subscribed stock symbols on the Alpaca stream.
_subscribed_symbols: Set[str] = set()

# Background worker task + lock
_stream_task: Optional[asyncio.Task] = None
_recompute_task: Optional[asyncio.Task] = None
# Initialized lazily in start() — must be created on the running loop, not at import time.
_recompute_queue: Optional["asyncio.Queue[str]"] = None
_last_recompute: Dict[str, float] = {}
_RECOMPUTE_MIN_INTERVAL = 30  # seconds per ticker
_RECOMPUTE_PRICE_DELTA = 0.001  # 0.1% move triggers recompute

# Reference to the live Alpaca client (set once connected)
_alpaca_client: Any = None
_loop: Optional[asyncio.AbstractEventLoop] = None


# ------------------------------------------------------------------
# Public read API
# ------------------------------------------------------------------
def get_stock_quote(ticker: str) -> Optional[Dict[str, float]]:
    return _stock_quotes.get(ticker.upper())


def get_option_quote(option_symbol: str) -> Optional[Dict[str, float]]:
    return _option_quotes.get(option_symbol.upper())


# D1: Stale-quote guard. A WS that stops receiving ticks (disconnect, stream
# error, subscription rejected) keeps the last snapshot in `_stock_quotes`
# forever — the manage loop would trail stops off that frozen price. Any
# quote older than this is treated as "no live price" so callers fall back
# to the REST price path instead of operating on a dead number.
_LIVE_QUOTE_MAX_AGE_SEC = 60.0


def get_live_price(ticker: str, *, max_age_sec: Optional[float] = None) -> Optional[float]:
    """Best-available last/mid price for a stock ticker, or None.

    Returns None if the cached quote is older than `max_age_sec` (default 60s)
    — stale ticks after a WS dropout were silently driving the stop-trail
    logic off frozen prices.
    """
    q = _stock_quotes.get(ticker.upper())
    if not q:
        return None
    age_cap = _LIVE_QUOTE_MAX_AGE_SEC if max_age_sec is None else float(max_age_sec)
    ts = q.get("ts")
    if ts and (time.time() - float(ts)) > age_cap:
        return None
    if q.get("last"):
        return q["last"]
    bid, ask = q.get("bid"), q.get("ask")
    if bid and ask:
        return (bid + ask) / 2.0
    return None


def quote_age_seconds(ticker: str) -> Optional[float]:
    """Seconds since last tick for this ticker, or None if never seen."""
    q = _stock_quotes.get(ticker.upper())
    if not q or not q.get("ts"):
        return None
    return time.time() - float(q["ts"])


def all_stock_quotes() -> Dict[str, Dict[str, float]]:
    return dict(_stock_quotes)


# ------------------------------------------------------------------
# Subscriber API (used by routers/stream.py)
# ------------------------------------------------------------------
# Hard ceiling on registered subscribers — defense against an unbounded set
# growing if router cleanup ever fails. 256 is far above realistic browser
# tabs for a single-user app; any growth past it is a bug, log loudly.
_MAX_SUBSCRIBERS = 256


def subscribe(cb: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
    if len(_subscribers) >= _MAX_SUBSCRIBERS:
        logger.error(
            f"subscribe rejected: subscriber set at hard cap {_MAX_SUBSCRIBERS} — "
            "likely a leak in router cleanup"
        )
        return
    _subscribers.add(cb)


def unsubscribe(cb: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
    _subscribers.discard(cb)


async def _broadcast(event: Dict[str, Any]) -> None:
    dead = []
    for cb in list(_subscribers):
        try:
            await cb(event)
        except Exception as e:
            # Subscriber raised — either WS died or its push() decided it's
            # stuck (see routers/stream.py:_DEAD_DROP_THRESHOLD). Either way,
            # prune. Log at INFO instead of DEBUG so leaks become visible.
            logger.info(f"Subscriber dropped from broadcast set: {e}")
            dead.append(cb)
    for cb in dead:
        _subscribers.discard(cb)


# ------------------------------------------------------------------
# Option quote ingestion (public — callable from REST pollers)
# ------------------------------------------------------------------
def update_option_quote(
    underlying: str,
    option_symbol: str,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    last: Optional[float] = None,
) -> None:
    key = option_symbol.upper()
    q = _option_quotes.setdefault(key, {})
    if bid is not None:
        q["bid"] = float(bid)
    if ask is not None:
        q["ask"] = float(ask)
    if last is not None:
        q["last"] = float(last)
    q["ts"] = time.time()
    q["underlying"] = underlying.upper()
    event = {
        "type": "option_quote",
        "underlying": underlying.upper(),
        "symbol": key,
        **q,
    }
    if _loop and _loop.is_running():
        asyncio.run_coroutine_threadsafe(_broadcast(event), _loop)


# ------------------------------------------------------------------
# Alpaca stream worker
# ------------------------------------------------------------------
async def _handle_trade(trade) -> None:
    sym = getattr(trade, "symbol", "").upper()
    px = float(getattr(trade, "price", 0) or 0)
    if not sym or px <= 0:
        return
    q = _stock_quotes.setdefault(sym, {})
    prev = q.get("last")
    q["last"] = px
    q["ts"] = time.time()
    event = {"type": "stock_trade", "symbol": sym, "last": px, "ts": q["ts"]}
    await _broadcast(event)

    # Live recompute gating
    if prev and abs(px - prev) / prev >= _RECOMPUTE_PRICE_DELTA:
        last_rc = _last_recompute.get(sym, 0)
        if time.time() - last_rc >= _RECOMPUTE_MIN_INTERVAL and _recompute_queue is not None:
            _last_recompute[sym] = time.time()
            try:
                _recompute_queue.put_nowait(sym)
            except asyncio.QueueFull:
                pass


async def _handle_quote(quote) -> None:
    sym = getattr(quote, "symbol", "").upper()
    bid = float(getattr(quote, "bid_price", 0) or 0)
    ask = float(getattr(quote, "ask_price", 0) or 0)
    if not sym:
        return
    q = _stock_quotes.setdefault(sym, {})
    if bid > 0:
        q["bid"] = bid
    if ask > 0:
        q["ask"] = ask
    q["ts"] = time.time()
    await _broadcast({"type": "stock_quote", "symbol": sym, "bid": bid, "ask": ask, "ts": q["ts"]})


async def _alpaca_worker():
    """Run Alpaca stock stream. Reconnects on failure."""
    global _alpaca_client
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        logger.warning(
            "APCA_API_KEY_ID / APCA_API_SECRET_KEY not set — live quotes disabled. "
            "Sign up for a free Alpaca paper account and export these env vars."
        )
        return

    try:
        from alpaca.data.live import StockDataStream
    except ImportError:
        logger.error("alpaca-py not installed. Run: pip install alpaca-py")
        return

    # Exponential backoff on reconnect. Fixed 10s retries hammered Alpaca
    # during DNS flakes / 429s, piling up thousands of "connection limit
    # exceeded" errors in minutes. Caps at 5 min; resets on any tick where
    # client.run() returned cleanly (connection was established long enough
    # to exit the blocking call normally).
    backoff = 10
    while True:
        try:
            client = StockDataStream(key, secret)
            _alpaca_client = client

            # We subscribe lazily as watchlist changes via ensure_symbols()
            while True:
                # Give the subscription pump a tick to register
                await asyncio.sleep(1)
                if _subscribed_symbols:
                    # alpaca-py's StockDataStream._run_forever is blocking;
                    # use its internal connection via subscribe then run
                    break

            client.subscribe_trades(_handle_trade, *list(_subscribed_symbols))
            client.subscribe_quotes(_handle_quote, *list(_subscribed_symbols))

            logger.info(f"Alpaca stream connected, subscribed to {sorted(_subscribed_symbols)}")
            # client._run_forever is sync — wrap in thread
            await asyncio.get_running_loop().run_in_executor(None, client.run)
            backoff = 10  # clean return means we actually connected
        except Exception as e:
            logger.error(f"Alpaca stream error, reconnecting in {backoff}s: {e}")
            await asyncio.sleep(backoff)
            backoff = min(300, backoff * 2)


def ensure_symbols(tickers: List[str]) -> None:
    """Add tickers to the subscribed set. Called on startup + on watchlist add."""
    global _alpaca_client
    upper = {t.upper() for t in tickers}
    new = upper - _subscribed_symbols
    if not new:
        return
    _subscribed_symbols.update(new)
    if _alpaca_client:
        try:
            _alpaca_client.subscribe_trades(_handle_trade, *new)
            _alpaca_client.subscribe_quotes(_handle_quote, *new)
            logger.info(f"Alpaca live-subscribed additional: {sorted(new)}")
        except Exception as e:
            logger.warning(f"Could not dynamically subscribe {new}: {e}")


# ------------------------------------------------------------------
# Live-recompute worker
# ------------------------------------------------------------------
async def _recompute_worker():
    """Pull tickers off the recompute queue and rerun signal generation."""
    # Lazy import to avoid circular deps
    from database import SessionLocal
    from routers.analysis import _run_analysis_for_ticker

    assert _recompute_queue is not None

    # Postmortem fix M3: SQLAlchemy Session is NOT thread-safe. Previously the
    # session was opened on the asyncio thread and handed to a worker thread
    # via run_in_executor — the asyncio loop could then issue queries on the
    # same session concurrently with the worker, producing intermittent
    # "database is locked" errors. Open the session INSIDE the worker so the
    # session lives entirely on the executor thread.
    def _do_recompute(ticker: str) -> None:
        db = SessionLocal()
        try:
            _run_analysis_for_ticker(ticker, db)
        finally:
            db.close()

    while True:
        try:
            ticker = await _recompute_queue.get()
            logger.info(f"[live-recompute] regenerating signals for {ticker}")
            await asyncio.get_running_loop().run_in_executor(None, _do_recompute, ticker)
            await _broadcast({"type": "signals_updated", "symbol": ticker, "ts": time.time()})
        except Exception as e:
            logger.error(f"Recompute worker error: {e}")
            await asyncio.sleep(2)


# ------------------------------------------------------------------
# Lifecycle hooks
# ------------------------------------------------------------------
async def start(initial_tickers: List[str]) -> None:
    global _stream_task, _recompute_task, _loop, _recompute_queue
    _loop = asyncio.get_running_loop()
    if _recompute_queue is None:
        _recompute_queue = asyncio.Queue(maxsize=128)
    ensure_symbols(initial_tickers)
    if _stream_task is None:
        _stream_task = asyncio.create_task(_alpaca_worker(), name="alpaca_stream")
    if _recompute_task is None:
        _recompute_task = asyncio.create_task(_recompute_worker(), name="live_recompute")
    logger.info(f"Live quotes started with {len(initial_tickers)} initial tickers")


async def stop() -> None:
    """Graceful shutdown — cancel tasks AND await them so the executor thread
    holding Alpaca's blocking client.run() actually exits before we return.
    Bare cancel() without await leaks the executor thread, which keeps trying
    to reconnect in the background after the FastAPI app has shut down."""
    global _stream_task, _recompute_task, _alpaca_client
    if _alpaca_client:
        try:
            await asyncio.get_running_loop().run_in_executor(None, _alpaca_client.stop)
        except Exception:
            pass
        _alpaca_client = None
    pending = [t for t in (_stream_task, _recompute_task) if t]
    for task in pending:
        task.cancel()
    if pending:
        # Bounded wait — if the executor thread is genuinely stuck, we'd
        # rather log loudly and proceed than hang shutdown forever.
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("live_quotes.stop: tasks did not finish within 5s; forcing shutdown")
    _stream_task = None
    _recompute_task = None
