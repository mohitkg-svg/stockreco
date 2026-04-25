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
# News + option streaming (Algo Trader Plus)
_news_stream_task: Optional[asyncio.Task] = None
_option_stream_task: Optional[asyncio.Task] = None
_option_stream_client: Any = None
_option_subscribed: Set[str] = set()
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


def broadcast_event_safe(event: Dict[str, Any]) -> None:
    """Thread-safe broadcast for callers that aren't running in the asyncio
    loop (e.g. the scheduler thread that runs auto_trader.manage_open_positions).
    No-ops silently if the loop isn't running.
    """
    if _loop and _loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(event), _loop)
        except Exception as e:
            logger.debug(f"broadcast_event_safe failed: {e}")


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
        from alpaca.data.enums import DataFeed
    except ImportError:
        logger.error("alpaca-py not installed. Run: pip install alpaca-py")
        return

    # Feed selection:
    #   - IEX  (default, free tier): single-exchange tape, ~2% of US volume.
    #     Extended-hours sessions are near-empty on IEX, which manifests as
    #     "frozen pre-market prices" in the UI.
    #   - SIP (Algo Trader Plus): full consolidated tape across all US
    #     exchanges, including pre/post-market. This is the fix for the
    #     missing extended-hours data reported by the user.
    # Controlled via ALPACA_DATA_FEED env var so we don't hard-code paid
    # tier dependency; default stays IEX for safety if the env is missing.
    _feed_name = os.getenv("ALPACA_DATA_FEED", "iex").lower()
    _feed = DataFeed.SIP if _feed_name == "sip" else DataFeed.IEX

    # Exponential backoff on reconnect with jitter. Fixed 10s retries
    # hammered Alpaca during DNS flakes / 429s, piling up thousands of
    # "connection limit exceeded" errors in minutes. Caps at 5 min; resets
    # on any tick where client.run() returned cleanly. Jitter (0.5×..1.5×)
    # prevents thundering-herd on simultaneous reconnects across instances.
    import random as _random
    backoff = 10
    consecutive_failures = 0
    while True:
        try:
            client = StockDataStream(key, secret, feed=_feed)
            _alpaca_client = client
            logger.info(f"Alpaca stream using feed={_feed.value}")

            # We subscribe lazily as watchlist changes via ensure_symbols()
            while True:
                await asyncio.sleep(1)
                if _subscribed_symbols:
                    break

            client.subscribe_trades(_handle_trade, *list(_subscribed_symbols))
            client.subscribe_quotes(_handle_quote, *list(_subscribed_symbols))

            logger.info(f"Alpaca stream connected, subscribed to {sorted(_subscribed_symbols)}")
            await asyncio.get_running_loop().run_in_executor(None, client.run)
            backoff = 10
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            wait_s = backoff * (0.5 + _random.random())   # ±50% jitter
            logger.error(f"Alpaca stream error, reconnecting in {wait_s:.0f}s "
                         f"(attempt #{consecutive_failures}): {e}")
            # Escalate to alert when we've been failing for a while.
            # Cap-hit (~5 min backoff) AND ≥5 consecutive failures = ~25 min
            # of broken stream. Operator needs to know.
            if backoff >= 300 and consecutive_failures >= 5:
                try:
                    from services import alerts as _al
                    _al.alert(
                        severity="error",
                        category="stream_reconnect_loop",
                        message=f"Alpaca WS stuck reconnecting ({consecutive_failures} failures, {wait_s:.0f}s backoff): {str(e)[:200]}",
                    )
                except Exception:
                    pass
            await asyncio.sleep(wait_s)
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
async def _news_worker():
    """Subscribe to Alpaca news WebSocket and ingest ticks into NewsEvent.

    Replaces the 2-min REST poll with push-based ingestion. Events are
    scored with VADER + the finance lexicon (same code path as the poller),
    persisted to news_events, and broadcast to UI subscribers via the same
    fan-out as stock quotes so the frontend news panel updates live.
    """
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        logger.info("news-stream: APCA creds missing — skipping")
        return
    try:
        from alpaca.data.live import NewsDataStream  # type: ignore[attr-defined]
    except Exception:
        # alpaca-py 0.21.1 doesn't export NewsDataStream yet. REST poll path
        # (services.news.poll_watchlist, running every 2min via APScheduler)
        # keeps news ingestion working until the SDK adds the class.
        logger.info("news-stream: NewsDataStream not in this alpaca-py version — using REST poll instead")
        return

    async def _handle_news(n):
        # Alpaca's news-stream event → same shape as the REST article (id,
        # headline, summary, source, author, created_at, symbols, url).
        try:
            from services import news as news_svc
            item = {
                "id": getattr(n, "id", None),
                "headline": getattr(n, "headline", "") or "",
                "summary": getattr(n, "summary", "") or "",
                "source": getattr(n, "source", "") or "",
                "author": getattr(n, "author", "") or "",
                "url": getattr(n, "url", "") or "",
                "created_at": (getattr(n, "created_at", None).isoformat() + "Z") if getattr(n, "created_at", None) else None,
                "symbols": getattr(n, "symbols", []) or [],
            }
            if not item["id"] or not item["headline"]:
                return
            result = news_svc.ingest([item])
            if result.get("inserted"):
                logger.info(f"news-stream: {item['symbols'][0] if item['symbols'] else '?'}: {item['headline'][:80]}")
                # Broadcast to /ws/quotes subscribers so the UI can push a toast / refresh the panel.
                await _broadcast({
                    "type": "news",
                    "symbol": (item["symbols"][0] if item["symbols"] else None),
                    "headline": item["headline"],
                    "created_at": item["created_at"],
                })
        except Exception as e:
            logger.debug(f"news-stream handler error: {e}")

    backoff = 5
    while True:
        try:
            client = NewsDataStream(key, secret)
            # Subscribe to all-symbols ('*') so any watchlist change auto-covers.
            # Alpaca's news feed supports '*' as a wildcard.
            client.subscribe_news(_handle_news, "*")
            logger.info("news-stream: connected, subscribed to '*'")
            await asyncio.get_running_loop().run_in_executor(None, client.run)
            backoff = 5
        except Exception as e:
            logger.warning(f"news-stream error, reconnecting in {backoff}s: {e}")
            await asyncio.sleep(backoff)
            backoff = min(300, backoff * 2)


async def _option_stream_worker():
    """Subscribe to option quotes for currently-held contracts via OptionDataStream.

    Requires Algo Trader Plus (OPRA feed). Gated by ALPACA_OPTIONS_STREAM env.
    Updates _option_quotes dict as ticks arrive — currently used for UI
    overlay; future enhancement is event-driven stop-loss evaluation.
    """
    global _option_stream_client
    if (os.getenv("ALPACA_OPTIONS_STREAM", "0") or "0").lower() not in ("1", "true", "on"):
        return
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return
    try:
        from alpaca.data.live import OptionDataStream
    except Exception as e:
        logger.warning(f"option-stream: OptionDataStream unavailable ({e})")
        return

    async def _handle_opt_quote(q):
        sym = getattr(q, "symbol", None)
        if not sym:
            return
        data = {
            "symbol": sym,
            "bid": float(q.bid_price) if getattr(q, "bid_price", None) is not None else None,
            "ask": float(q.ask_price) if getattr(q, "ask_price", None) is not None else None,
            "ts": time.time(),
        }
        _option_quotes[sym] = data
        await _broadcast({"type": "option_quote", **data})

    backoff = 5
    while True:
        try:
            client = OptionDataStream(key, secret)
            _option_stream_client = client
            # Wait for a subscription before connecting; the manage loop
            # calls ensure_option_symbols() when option auto-trades open.
            # Audit fix #4: we re-subscribe to the FULL _option_subscribed set
            # on every (re)connect so reconnect events don't silently drop
            # coverage of still-open option positions.
            while not _option_subscribed:
                await asyncio.sleep(5)
            snapshot = list(_option_subscribed)
            client.subscribe_quotes(_handle_opt_quote, *snapshot)
            logger.info(f"option-stream: connected (feed=OPRA), subscribed to {len(snapshot)} OCC symbols: {snapshot[:5]}{'…' if len(snapshot) > 5 else ''}")
            await asyncio.get_running_loop().run_in_executor(None, client.run)
            backoff = 5
        except Exception as e:
            logger.warning(f"option-stream error, reconnecting in {backoff}s: {e}")
            await asyncio.sleep(backoff)
            backoff = min(300, backoff * 2)


def ensure_option_symbols(occ_symbols: List[str]) -> None:
    """Dynamically add OCC option symbols to the option stream subscription.
    Called by auto_trader when an option trade opens."""
    global _option_stream_client
    upper = {s.upper() for s in occ_symbols if s}
    new = upper - _option_subscribed
    if not new:
        return
    _option_subscribed.update(new)
    if _option_stream_client:
        try:
            async def _noop(_q): pass
            _option_stream_client.subscribe_quotes(_noop, *new)
            logger.info(f"option-stream: subscribed {sorted(new)}")
        except Exception as e:
            logger.warning(f"option-stream dynamic subscribe {new} failed: {e}")


async def start(initial_tickers: List[str]) -> None:
    global _stream_task, _recompute_task, _loop, _recompute_queue
    global _news_stream_task, _option_stream_task
    _loop = asyncio.get_running_loop()
    if _recompute_queue is None:
        _recompute_queue = asyncio.Queue(maxsize=128)
    ensure_symbols(initial_tickers)
    if _stream_task is None:
        _stream_task = asyncio.create_task(_alpaca_worker(), name="alpaca_stream")
    if _recompute_task is None:
        _recompute_task = asyncio.create_task(_recompute_worker(), name="live_recompute")
    # Algo Trader Plus streams — default News on (free), Options off (needs OPRA).
    if _news_stream_task is None and (os.getenv("ALPACA_NEWS_STREAM", "1") or "1").lower() in ("1", "true", "on"):
        _news_stream_task = asyncio.create_task(_news_worker(), name="alpaca_news_stream")
    if _option_stream_task is None:
        _option_stream_task = asyncio.create_task(_option_stream_worker(), name="alpaca_option_stream")
    logger.info(f"Live quotes started with {len(initial_tickers)} initial tickers")


async def stop() -> None:
    """Graceful shutdown — cancel tasks AND await them so the executor thread
    holding Alpaca's blocking client.run() actually exits before we return.
    Bare cancel() without await leaks the executor thread, which keeps trying
    to reconnect in the background after the FastAPI app has shut down."""
    global _stream_task, _recompute_task, _alpaca_client
    global _news_stream_task, _option_stream_task, _option_stream_client
    if _alpaca_client:
        try:
            await asyncio.get_running_loop().run_in_executor(None, _alpaca_client.stop)
        except Exception:
            pass
        _alpaca_client = None
    if _option_stream_client:
        try:
            await asyncio.get_running_loop().run_in_executor(None, _option_stream_client.stop)
        except Exception:
            pass
        _option_stream_client = None
    pending = [t for t in (_stream_task, _recompute_task, _news_stream_task, _option_stream_task) if t]
    for task in pending:
        task.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("live_quotes.stop: tasks did not finish within 5s; forcing shutdown")
    _stream_task = None
    _recompute_task = None
    _news_stream_task = None
    _option_stream_task = None
