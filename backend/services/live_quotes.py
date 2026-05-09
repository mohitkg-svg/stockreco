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
# r48 BACKLOG #concurrency-P0-6: lock guard for add/remove/iterate.
import threading as _lq_threading
_subscribers_lock = _lq_threading.Lock()
_subscribers: Set[Callable[[Dict[str, Any]], Awaitable[None]]] = set()

# Set of currently subscribed stock symbols on the Alpaca stream.
# r48 BACKLOG #concurrency-P1-10: lock guard for ensure/unsubscribe.
_subscribed_symbols_lock = _lq_threading.Lock()
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
# r42 fix #2.4: per-ticker last-threat-check timestamps (rate-limit the
# stop-threat fast-path to once per 5s per ticker).
_last_threat_check: Dict[str, float] = {}
# r43 fix #0.4: global single-flight gate so correlated-drawdown ticks don't
# fire N concurrent manage_open_positions runs.
import threading as _threading_lq
_threat_path_lock = _threading_lq.Lock()
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
    # r48 BACKLOG #concurrency-P0-6: atomic check+add to enforce cap.
    with _subscribers_lock:
        if len(_subscribers) >= _MAX_SUBSCRIBERS:
            logger.error(
                f"subscribe rejected: subscriber set at hard cap {_MAX_SUBSCRIBERS} — "
                "likely a leak in router cleanup"
            )
            return
        _subscribers.add(cb)


def unsubscribe(cb: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
    with _subscribers_lock:
        _subscribers.discard(cb)


async def _broadcast(event: Dict[str, Any]) -> None:
    dead = []
    with _subscribers_lock:
        snapshot = list(_subscribers)
    for cb in snapshot:
        try:
            await cb(event)
        except Exception as e:
            logger.info(f"Subscriber dropped from broadcast set: {e}")
            dead.append(cb)
    if dead:
        with _subscribers_lock:
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
    # r48 BACKLOG #concurrency-P1-9: replace inner-dict mutation with atomic
    # whole-dict swap so a concurrent reader can't see fresh `last` paired
    # with stale `ts` (or vice versa).
    prev_dict = _stock_quotes.get(sym) or {}
    prev = prev_dict.get("last")
    new_ts = time.time()
    new_dict = {**prev_dict, "last": px, "ts": new_ts}
    _stock_quotes[sym] = new_dict   # atomic dict-pointer swap (GIL-protected)
    event = {"type": "stock_trade", "symbol": sym, "last": px, "ts": new_ts}
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

    # r42 fix #2.4: stop-threat fast-path. When the live trade tick prints
    # within 0.25% of a long auto-trade's current_stop (or above for shorts),
    # dispatch the manage tick for THIS ticker immediately rather than
    # waiting for the next scheduler firing. The check is cheap (one DB
    # query, rate-limited per ticker to once per 5s) so it can run on
    # every tick.
    # r43 fix #0.4: stop-threat fast-path now covers BOTH stocks AND options
    # (option positions track an UNDERLYING stop, so the same `current_stop`
    # field applies). Direction is derived from `side`/asset/symbol — long
    # stock + long call: trigger when px <= stop; long put: trigger when
    # px >= stop. Rate-limited per-ticker via `_last_threat_check` and
    # globally via `_threat_path_lock` so a correlated drawdown can't fire
    # 50 concurrent manage runs.
    try:
        last_threat = _last_threat_check.get(sym, 0.0)
        if time.time() - last_threat >= 5.0:
            _last_threat_check[sym] = time.time()
            from database import SessionLocal as _SL_t, AutoTrade as _AT_t
            _db_t = _SL_t()
            try:
                _rows = _db_t.query(_AT_t).filter(
                    _AT_t.ticker == sym,
                    _AT_t.status == "open"
                ).all()
                action_fired = False
                for _row in _rows:
                    is_put = (
                        _row.asset_type == "option"
                        and isinstance(_row.symbol, str)
                        and len(_row.symbol) > 12
                        and _row.symbol[-9] == "P"
                    )
                    
                    # 1. Stop-loss threat check
                    if _row.current_stop:
                        cs = float(_row.current_stop)
                        if is_put:
                            threat = px >= cs * 0.9975
                        else:
                            threat = px <= cs * 1.0025
                        if threat:
                            action_fired = True
                            break
                            
                    # 2. Target/Profit opportunity check (T1/T2/T3)
                    li = _row.level_index or 0
                    targets = [_row.target1, _row.target2, _row.target3]
                    next_target = targets[li % 3] if li < 3 else None
                    if next_target:
                        nt = float(next_target)
                        opportunity = (px <= nt) if is_put else (px >= nt)
                        if opportunity:
                            action_fired = True
                            break

                if action_fired:
                    # Global single-flight: only one fast-path manage at a time
                    # to prevent correlated drawdowns from firing N concurrent
                    # manage_open_positions runs (broker breaker risk).
                    if _threat_path_lock.acquire(blocking=False):
                        try:
                            from services import auto_trader as _at_t
                            try:
                                asyncio.get_running_loop().run_in_executor(
                                    None, _at_t.manage_open_positions
                                )
                            except RuntimeError:
                                # No running loop on this WS thread; fall back
                                # to direct synchronous call (manage already
                                # uses its own DB connections / locks).
                                _at_t.manage_open_positions()
                            logger.info(f"manage fast-path fired for {sym} @ {px} (stop/target hit)")
                        finally:
                            # Release after a brief delay so the manage loop
                            # has a chance to start without a follow-up tick
                            # immediately re-acquiring.
                            try:
                                asyncio.get_running_loop().call_later(2.0, _threat_path_lock.release)
                            except Exception:
                                _threat_path_lock.release()
            finally:
                _db_t.close()
    except Exception as _e:
        logger.debug(f"stop-threat fast-path skipped for {sym}: {_e}")


async def _handle_quote(quote) -> None:
    sym = getattr(quote, "symbol", "").upper()
    bid = float(getattr(quote, "bid_price", 0) or 0)
    ask = float(getattr(quote, "ask_price", 0) or 0)
    if not sym:
        return
    # r48 BACKLOG #concurrency-P1-9: atomic dict swap (mirror of trade handler).
    prev_dict = _stock_quotes.get(sym) or {}
    new_ts = time.time()
    new_dict = dict(prev_dict)
    if bid > 0:
        new_dict["bid"] = bid
    if ask > 0:
        new_dict["ask"] = ask
    new_dict["ts"] = new_ts
    _stock_quotes[sym] = new_dict
    # r48 BACKLOG: feed spread EMA into order_flow tracker.
    try:
        from services import order_flow as _of
        _of.update_spread_ema(sym, bid, ask)
    except Exception:
        pass
    await _broadcast({"type": "stock_quote", "symbol": sym, "bid": bid, "ask": ask, "ts": new_ts})


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
                with _subscribed_symbols_lock:
                    has_syms = bool(_subscribed_symbols)
                if has_syms:
                    break

            # r48 BACKLOG #concurrency-P1-10: snapshot under lock
            with _subscribed_symbols_lock:
                _snap = list(_subscribed_symbols)
            client.subscribe_trades(_handle_trade, *_snap)
            client.subscribe_quotes(_handle_quote, *_snap)

            logger.info(f"Alpaca stream connected, subscribed to {sorted(_snap)}")
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
    """Add tickers to the subscribed set. Called on startup + on watchlist add.
    r48 BACKLOG #concurrency-P1-10: lock guard against reconnect-snapshot race."""
    global _alpaca_client
    upper = {t.upper() for t in tickers}
    with _subscribed_symbols_lock:
        new = upper - _subscribed_symbols
        if not new:
            return
        _subscribed_symbols.update(new)
        new_snapshot = list(new)
    if _alpaca_client:
        try:
            _alpaca_client.subscribe_trades(_handle_trade, *new_snapshot)
            _alpaca_client.subscribe_quotes(_handle_quote, *new_snapshot)
            logger.info(f"Alpaca live-subscribed additional: {sorted(new_snapshot)}")
        except Exception as e:
            logger.warning(f"Could not dynamically subscribe {new_snapshot}: {e}")


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

    import json
    import websockets

    backoff = 5
    while True:
        try:
            async with websockets.connect("wss://stream.data.alpaca.markets/v1beta1/news") as ws:
                # Auth
                auth_msg = {"action": "auth", "key": key, "secret": secret}
                await ws.send(json.dumps(auth_msg))
                auth_reply = json.loads(await ws.recv())
                
                if not auth_reply or auth_reply[0].get("T") != "success":
                    logger.warning(f"news-stream auth failed: {auth_reply}")
                    await asyncio.sleep(backoff)
                    continue
                
                # Subscribe to all news
                sub_msg = {"action": "subscribe", "news": ["*"]}
                await ws.send(json.dumps(sub_msg))
                sub_reply = json.loads(await ws.recv())
                
                if not sub_reply or sub_reply[0].get("T") != "subscription":
                    logger.warning(f"news-stream subscribe failed: {sub_reply}")
                    await asyncio.sleep(backoff)
                    continue

                logger.info("news-stream: connected, subscribed to '*' via raw WebSocket")
                backoff = 5
                
                while True:
                    msg = await ws.recv()
                    events = json.loads(msg)
                    for n in events:
                        if n.get("T") == "n":
                            try:
                                from services import news as news_svc
                                item = {
                                    "id": str(n.get("id")),
                                    "headline": n.get("headline", ""),
                                    "summary": n.get("summary", ""),
                                    "source": n.get("source", ""),
                                    "author": n.get("author", ""),
                                    "url": n.get("url", ""),
                                    "created_at": n.get("created_at"),
                                    "symbols": n.get("symbols", []),
                                }
                                if not item["id"] or not item["headline"]:
                                    continue
                                result = news_svc.ingest([item])
                                if result.get("inserted"):
                                    logger.info(f"news-stream: {item['symbols'][0] if item['symbols'] else '?'}: {item['headline'][:80]}")
                                    await _broadcast({
                                        "type": "news",
                                        "symbol": (item["symbols"][0] if item["symbols"] else None),
                                        "headline": item["headline"],
                                        "created_at": item["created_at"],
                                    })
                            except Exception as e:
                                logger.debug(f"news-stream handler error: {e}")
        except Exception as e:
            logger.warning(f"news-stream error, reconnecting in {backoff}s: {e}")
            await asyncio.sleep(backoff)
            backoff = min(300, backoff * 2)


async def _handle_opt_quote(q):
    """Module-level option-quote handler so dynamic subscriptions via
    `ensure_option_symbols` use the SAME handler as the initial connect.
    r42 fix #0.3: previously dynamic subscribes used an `async def _noop`,
    so quotes for newly-opened option contracts were silently discarded —
    stop-management fell back to slow REST polling.
    """
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


async def _option_stream_worker():
    """Subscribe to option quotes for currently-held contracts via OptionDataStream.

    Requires Algo Trader Plus (OPRA feed). Gated by ALPACA_OPTIONS_STREAM env.
    Updates _option_quotes dict as ticks arrive — currently used for UI
    overlay; future enhancement is event-driven stop-loss evaluation.
    """
    global _option_stream_client
    # r43 fix #0.7: default ON when Algo-Trader-Plus credentials are available.
    # Previously defaulted off, so the entire option-quote pipeline (and the
    # marketable-limit option exit which depends on it) was silently inactive
    # for any operator who hadn't set ALPACA_OPTIONS_STREAM=1 explicitly.
    _flag = (os.getenv("ALPACA_OPTIONS_STREAM", "1") or "1").lower()
    if _flag not in ("1", "true", "on"):
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
            # r42 fix #0.3: route dynamic subscriptions to the SAME
            # handler the static subscription uses, so quotes for newly-
            # opened option contracts actually flow into _option_quotes
            # and out via WS broadcast.
            _option_stream_client.subscribe_quotes(_handle_opt_quote, *new)
            logger.info(f"option-stream: subscribed {sorted(new)}")
        except Exception as e:
            logger.warning(f"option-stream dynamic subscribe {new} failed: {e}")


def prune_option_symbols(active_symbols: List[str]) -> None:
    """r47 fix #T0e-4 / #T0h: drop OCC subscriptions for closed/expired contracts.
    Called from manage_open_positions with the set of currently-open OCC
    symbols. Without this, every weekly-expiry contract ever traded stayed
    in `_option_subscribed` forever; reconnect re-subscribed the entire pile,
    eventually triggering Alpaca subscribe-rate limits and reconnect storms."""
    global _option_stream_client
    active = {s.upper() for s in active_symbols if s}
    stale = _option_subscribed - active
    if not stale:
        return
    _option_subscribed.difference_update(stale)
    if _option_stream_client:
        try:
            _option_stream_client.unsubscribe_quotes(*stale)
            logger.info(f"option-stream: unsubscribed {len(stale)} stale OCCs")
        except Exception as e:
            logger.warning(f"option-stream unsubscribe failed: {e}")
    # Also drop cached quotes so memory follows the subscription set.
    try:
        for s in stale:
            _option_quotes.pop(s, None)
    except Exception:
        pass


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
