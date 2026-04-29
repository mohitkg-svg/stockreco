"""
Yahoo Finance data fetcher using curl_cffi (handles TLS fingerprinting).
Does not depend on yfinance — hits the v8 chart API directly.
"""
from curl_cffi import requests as cf_requests
import pandas as pd
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import time
import logging
import json

from services.config import DATA_CACHE_MAX_ENTRIES

logger = logging.getLogger(__name__)


class _BoundedTTLCache(OrderedDict):
    """LRU-bounded dict of (value, expiry_ts). Evicts oldest on overflow.

    Keeps the same `_cache[key] = (df, expiry)` / `if key in _cache` API the
    rest of data_fetcher already uses, so drop-in. The admin /clear-cache
    endpoint also still works because .clear()/len() inherit from OrderedDict.
    """
    def __init__(self, max_entries: int):
        super().__init__()
        self._max = max_entries

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._max:
            self.popitem(last=False)  # drop LRU

    def __getitem__(self, key):
        v = super().__getitem__(key)
        self.move_to_end(key)
        return v


# In-memory cache: {cache_key: (dataframe_or_dict, expiry_timestamp)}
_cache: "_BoundedTTLCache" = _BoundedTTLCache(DATA_CACHE_MAX_ENTRIES)
_session: Optional[cf_requests.Session] = None
_crumb: Optional[str] = None
_crumb_expiry: float = 0

BASE_URL = "https://query2.finance.yahoo.com"

TIMEFRAME_CONFIG = {
    "1m":  {"interval": "1m",  "range": "2d",   "ttl": 60},    # entry confirmation only
    # Range tuning: the chart UI only displays 50-80 bars by default and
    # SMA200 is the deepest indicator we plot. So we keep ~5× SMA200 worth
    # of bars (≈1000 bars) for each intraday TF — enough headroom for the
    # analysis layers (S/R swings, zones, fib, gaps), nowhere near the
    # 4,000-10,000-bar Yahoo payloads the previous "60d/2y" ranges pulled.
    # Cold-fetch latency drops 5-10x because the JSON is 5-10x smaller AND
    # every pandas-ta computation downstream runs on a fraction of the
    # rows. Yahoo limits per-interval history (5m=60d, 1h=730d) so we stay
    # under those caps.
    "5m":  {"interval": "5m",  "range": "10d",  "ttl": 300},   # ~780 bars
    "15m": {"interval": "15m", "range": "30d",  "ttl": 300},   # ~780 bars
    "30m": {"interval": "30m", "range": "60d",  "ttl": 600},   # ~780 bars
    "1h":  {"interval": "1h",  "range": "180d", "ttl": 900},   # ~1170 bars
    "4h":  {"interval": "1h",  "range": "180d", "ttl": 900},   # resampled from 1h → ~290 bars
    "1d":  {"interval": "1d",  "range": "2y",   "ttl": 3600},
    "1mo": {"interval": "1mo", "range": "10y",  "ttl": 86400},
}


def _get_session() -> cf_requests.Session:
    global _session
    if _session is None:
        _session = cf_requests.Session(impersonate="chrome")
    return _session


# ---- Yahoo rate limiter ---------------------------------------------------
# Yahoo Finance has no documented rate limit, but ad-hoc testing shows ~60
# requests/minute is the soft ceiling before they start returning 429s and
# eventually IP-ban for ~15 minutes. We keep a token bucket well below that.
import threading as _threading

_YF_RATE_PER_MIN = 30          # requests / minute soft cap
_YF_BURST = 10                 # max immediate-back-to-back requests
_yf_tokens = float(_YF_BURST)
_yf_last_refill = time.monotonic()
_yf_lock = _threading.Lock()


def _yf_acquire(timeout: float = 10.0) -> bool:
    """Block (up to `timeout` sec) until a token is available, then take one.
    Returns False on timeout — caller should treat as a fetch failure."""
    global _yf_tokens, _yf_last_refill
    refill_per_sec = _YF_RATE_PER_MIN / 60.0
    deadline = time.monotonic() + timeout
    while True:
        with _yf_lock:
            now = time.monotonic()
            elapsed = now - _yf_last_refill
            if elapsed > 0:
                _yf_tokens = min(_YF_BURST, _yf_tokens + elapsed * refill_per_sec)
                _yf_last_refill = now
            if _yf_tokens >= 1.0:
                _yf_tokens -= 1.0
                return True
            wait = (1.0 - _yf_tokens) / refill_per_sec
        if time.monotonic() + wait > deadline:
            return False
        time.sleep(min(wait, 0.5))


def _get_crumb() -> str:
    """Fetch a Yahoo crumb, falling back to a stale-but-recent crumb if the
    refresh fails. Without this fallback, a single rate-limited refresh
    crashes the in-flight scan run for every ticker on the watchlist."""
    global _crumb, _crumb_expiry
    now = time.time()
    if _crumb and now < _crumb_expiry:
        return _crumb
    sess = _get_session()
    try:
        sess.get("https://finance.yahoo.com", timeout=10)  # warm cookie
        resp = sess.get(f"{BASE_URL}/v1/test/getcrumb", timeout=10)
        if resp.status_code == 200 and resp.text:
            _crumb = resp.text.strip()
            _crumb_expiry = now + 3600
            return _crumb
        # Soft failure (non-200 / empty body)
        raise RuntimeError(f"crumb endpoint returned {resp.status_code}")
    except Exception as e:
        # Hard failure — if we have ANY crumb cached, extend its life by 5 min
        # and try once more next time. Yahoo crumbs typically remain valid past
        # the 1h sliding window; this avoids nuking a whole scan on one flake.
        if _crumb:
            _crumb_expiry = now + 300
            logger.warning(f"crumb refresh failed ({e}); reusing stale crumb for 5 more min")
            return _crumb
        raise RuntimeError(f"Could not obtain Yahoo Finance crumb: {e}")


def _fetch_chart(ticker: str, interval: str, range_str: str) -> pd.DataFrame:
    """Fetch OHLCV from Yahoo Finance v8 chart API."""
    if not _yf_acquire():
        logger.warning(f"yahoo rate-limit timeout for {ticker} {interval}")
        return pd.DataFrame()
    crumb = _get_crumb()
    sess = _get_session()
    url = f"{BASE_URL}/v8/finance/chart/{ticker.upper()}"
    # Intraday intervals include pre/post bars; daily+ ignore the flag.
    include_pre_post = interval not in ("1d", "1wk", "1mo")
    params = {
        "interval": interval,
        "range": range_str,
        "crumb": crumb,
        "includePrePost": "true" if include_pre_post else "false",
        "events": "div,split",
    }
    resp = sess.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        logger.error(f"Yahoo Finance returned {resp.status_code} for {ticker}")
        return pd.DataFrame()

    data = resp.json()
    try:
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        timestamps = result.get("timestamp", [])
        ohlcv = result.get("indicators", {}).get("quote", [{}])[0]
        opens = ohlcv.get("open", [])
        highs = ohlcv.get("high", [])
        lows = ohlcv.get("low", [])
        closes = ohlcv.get("close", [])
        volumes = ohlcv.get("volume", [])

        if not timestamps:
            return pd.DataFrame()

        df = pd.DataFrame({
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        }, index=pd.to_datetime(timestamps, unit="s"))
        df.index.name = "Datetime"
        df = df.dropna()
        return df
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Error parsing chart data for {ticker}: {e}")
        return pd.DataFrame()


def _cache_key(ticker: str, timeframe: str, source: str = "auto") -> str:
    """Source-aware key. The fetcher tries multiple sources per timeframe; we
    tag the cache entry with whichever source actually returned data so a later
    call asking for a *specific* source isn't silently served the other one's
    bars (which can have different bar boundaries / pre-post coverage)."""
    return f"{ticker.upper()}:{timeframe}:{source}"


def _resample_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    if df_1h.empty:
        return df_1h
    resampled = df_1h.resample("4h").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna()
    return resampled


# ------------------------------------------------------------------
# Alpaca historical bars (preferred when credentials are present)
# ------------------------------------------------------------------
_ALPACA_TF = {
    "1m":  ("1Min",  2),      # Profit-audit #6: 1-min SIP bars for entry confirmation
    "5m":  ("5Min",  10),
    "15m": ("15Min", 30),
    "30m": ("30Min", 60),
    "1h":  ("1Hour", 180),
    "4h":  ("1Hour", 180),    # resampled
    "1d":  ("1Day",  730),
    "1mo": ("1Month", 3650),
}
_alpaca_bars_client = None


def _get_alpaca_bars_client():
    global _alpaca_bars_client
    if _alpaca_bars_client is not None:
        return _alpaca_bars_client
    import os
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        _alpaca_bars_client = StockHistoricalDataClient(key, secret)
        return _alpaca_bars_client
    except Exception as e:
        logger.warning(f"Alpaca bars client unavailable: {e}")
        return None


def _fetch_alpaca_bars(ticker: str, timeframe: str) -> pd.DataFrame:
    """Fetch historical OHLCV from Alpaca. Returns empty DF on failure.

    Feed selection mirrors the live quote stream via ALPACA_DATA_FEED env:
      • SIP (Algo Trader Plus): full consolidated tape, INCLUDES pre/post
        market bars. Preferred when available.
      • IEX (free tier): single-exchange, regular hours only (near-empty
        during extended hours).
    """
    cfg = _ALPACA_TF.get(timeframe)
    if not cfg:
        return pd.DataFrame()
    client = _get_alpaca_bars_client()
    if client is None:
        return pd.DataFrame()
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from datetime import timedelta, datetime as _dt
        import os as _os

        atf_str, days = cfg
        # Map string → TimeFrame enum
        tf_map = {
            "1Min":  TimeFrame(1, TimeFrameUnit.Minute),
            "5Min":  TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "30Min": TimeFrame(30, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
            "1Day":  TimeFrame(1, TimeFrameUnit.Day),
            "1Month": TimeFrame(1, TimeFrameUnit.Month),
        }
        atf = tf_map[atf_str]
        end = _dt.utcnow()
        start = end - timedelta(days=days)
        feed = (_os.getenv("ALPACA_DATA_FEED", "iex") or "iex").lower()
        if feed not in ("iex", "sip"):
            feed = "iex"
        # r54 Tier-0 #3: explicit Adjustment.ALL so split/dividend events
        # don't show as ~50% gaps that nuke ADX/RS/RVOL for the affected
        # ticker for ~14 days post-split. Without this, Alpaca SIP defaults
        # to RAW (unadjusted) which silently corrupts the score.
        adjustment = None
        try:
            from alpaca.data.enums import Adjustment as _Adj
            adjustment = _Adj.ALL
        except Exception:
            pass
        kwargs = dict(symbol_or_symbols=ticker.upper(), timeframe=atf,
                      start=start, end=end, feed=feed)
        if adjustment is not None:
            kwargs["adjustment"] = adjustment
        req = StockBarsRequest(**kwargs)
        bars = client.get_stock_bars(req)
        df_raw = bars.df
        if df_raw is None or df_raw.empty:
            return pd.DataFrame()
        # Multi-index (symbol, timestamp) → drop symbol level
        if isinstance(df_raw.index, pd.MultiIndex):
            df_raw = df_raw.xs(ticker.upper(), level=0)
        df = pd.DataFrame({
            "Open":   df_raw["open"],
            "High":   df_raw["high"],
            "Low":    df_raw["low"],
            "Close":  df_raw["close"],
            "Volume": df_raw["volume"],
        })
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.index.name = "Datetime"
        return df.dropna()
    except Exception as e:
        logger.warning(f"Alpaca bars fetch failed for {ticker} {timeframe}: {e}")
        return pd.DataFrame()


_INTRADAY_TFS = {"5m", "15m", "30m", "1h", "4h"}


def fetch_ohlcv_bulk(tickers: List[str], timeframe: str = "1d", batch_size: int = 20) -> Dict[str, pd.DataFrame]:
    """r54 Tier-1 #8: bulk-fetch daily bars for many tickers in fewer
    Alpaca API round-trips. The Alpaca SDK supports
    `symbol_or_symbols=[a, b, c, ...]` per request — switching from 1
    ticker/call to 20 tickers/call cuts the universe scanner walltime
    from ~60-90s to ~6-12s without burning Yahoo rate limits or
    saturating the Alpaca quota.

    Returns a dict {ticker: DataFrame}. Tickers with no data are
    omitted. Caches each ticker's df in the module-level _cache the
    same way fetch_ohlcv does, so subsequent single-ticker calls hit
    the cache and skip the per-ticker round-trip.
    """
    cfg = _ALPACA_TF.get(timeframe)
    if not cfg:
        return {}
    client = _get_alpaca_bars_client()
    if client is None:
        return {}
    out: Dict[str, pd.DataFrame] = {}
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from datetime import timedelta as _td_b, datetime as _dt_b
        import os as _os_b

        atf_str, days = cfg
        tf_map = {
            "1Min":  TimeFrame(1, TimeFrameUnit.Minute),
            "5Min":  TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "30Min": TimeFrame(30, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
            "1Day":  TimeFrame(1, TimeFrameUnit.Day),
            "1Month": TimeFrame(1, TimeFrameUnit.Month),
        }
        atf = tf_map[atf_str]
        end = _dt_b.utcnow()
        start = end - _td_b(days=days)
        feed = (_os_b.getenv("ALPACA_DATA_FEED", "iex") or "iex").lower()
        if feed not in ("iex", "sip"):
            feed = "iex"
        adjustment = None
        try:
            from alpaca.data.enums import Adjustment as _Adj
            adjustment = _Adj.ALL
        except Exception:
            pass
        # Process in batches.
        upper = [t.upper() for t in tickers if t]
        now_ts = time.time()
        for i in range(0, len(upper), batch_size):
            batch = upper[i:i + batch_size]
            kwargs = dict(symbol_or_symbols=batch, timeframe=atf,
                          start=start, end=end, feed=feed)
            if adjustment is not None:
                kwargs["adjustment"] = adjustment
            try:
                req = StockBarsRequest(**kwargs)
                bars = client.get_stock_bars(req)
                df_raw = bars.df
                if df_raw is None or df_raw.empty:
                    continue
                # alpaca-py returns a multi-index df (symbol, timestamp).
                # Split per-ticker.
                for t in batch:
                    try:
                        df_t = df_raw.loc[t] if t in df_raw.index.get_level_values(0) else None
                        if df_t is None or df_t.empty:
                            continue
                        # Normalize to the same shape fetch_ohlcv returns.
                        df_t = df_t.copy()
                        df_t.columns = [c.title() if c.lower() in ("open", "high", "low", "close", "volume") else c for c in df_t.columns]
                        out[t] = df_t
                        # Cache it so subsequent fetch_ohlcv hits hit the cache.
                        try:
                            key = _cache_key(t, timeframe)
                            _cache[key] = (df_t.copy(), now_ts + cfg["ttl"] if isinstance(cfg, dict) else now_ts + 3600)
                        except Exception:
                            pass
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"fetch_ohlcv_bulk batch {i}: {e}")
                continue
    except Exception as e:
        logger.warning(f"fetch_ohlcv_bulk failed: {e}")
    return out


def fetch_ohlcv(ticker: str, timeframe: str) -> pd.DataFrame:
    """
    Fetch OHLCV data with caching.

    Source priority depends on the timeframe:
      • Intraday (5m/15m/30m/1h/4h): Yahoo first — its consolidated feed
        includes pre-market (4–9:30 ET) and after-hours (16:00–20:00 ET)
        bars, so charts reflect extended-hours price action. Alpaca is the
        fallback (free IEX feed is RTH-only and won't show pre/post bars).
      • Daily / monthly: Alpaca first (matches our live tick stream and
        bracket-order fills), Yahoo fallback.
    """
    key = _cache_key(ticker, timeframe)
    now = time.time()
    if key in _cache:
        df, expiry = _cache[key]
        if now < expiry:
            return df.copy()

    cfg = TIMEFRAME_CONFIG.get(timeframe)
    if cfg is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    intraday = timeframe in _INTRADAY_TFS
    # With SIP feed (Algo Trader Plus), Alpaca has extended-hours bars AND
    # better latency + no Yahoo rate limit. Use it as primary for all
    # timeframes. Fall back to Yahoo on API errors.
    # With IEX (free tier), Alpaca intraday bars are RTH-only — Yahoo's
    # consolidated feed is richer, so fall back to old priority.
    import os as _os_dp
    _sip_on = (_os_dp.getenv("ALPACA_DATA_FEED", "iex") or "iex").lower() == "sip"
    if _sip_on:
        sources = ["alpaca", "yahoo"]
    else:
        sources = ["yahoo", "alpaca"] if intraday else ["alpaca", "yahoo"]

    from services import metrics as _metrics
    _TRANSIENT_NET_HINTS = (
        "could not resolve host",
        "name or service not known",
        "connection reset",
        "connection aborted",
        "temporary failure in name resolution",
        "nodename nor servname",
    )
    for i, src in enumerate(sources):
        is_last = (i == len(sources) - 1)
        try:
            if src == "alpaca":
                df = _fetch_alpaca_bars(ticker, timeframe)
            else:
                fetch_interval = "1h" if timeframe == "4h" else cfg["interval"]
                df = _fetch_chart(ticker, fetch_interval, cfg["range"])
            if df is None or df.empty:
                _metrics.inc("data_fetch", source=src, outcome="empty")
                continue
            if timeframe == "4h":
                df = _resample_to_4h(df)
            df = df.dropna()
            _cache[key] = (df.copy(), now + cfg["ttl"])
            _metrics.inc("data_fetch", source=src, outcome="ok")
            return df
        except Exception as e:
            # Transient network flakes with a fallback source still to try
            # are noise — demote to debug. Log at warning only if we're out
            # of fallbacks or the error looks substantive.
            err_lower = str(e).lower()
            is_transient = any(h in err_lower for h in _TRANSIENT_NET_HINTS)
            if is_transient and not is_last:
                logger.debug(f"{src} fetch failed for {ticker} {timeframe}: {e} (trying fallback)")
            elif is_transient and is_last:
                # All sources failed with transient net errors (DNS flap, TLS
                # reset). Next scan in 15min will retry — no operator action
                # needed, so INFO not WARNING.
                logger.info(f"{src} fetch transient-fail for {ticker} {timeframe}: {e}")
            else:
                logger.warning(f"{src} fetch failed for {ticker} {timeframe}: {e}")
            _metrics.inc("data_fetch", source=src, outcome="error")
            continue

    return pd.DataFrame()


def get_ticker_info(ticker: str) -> dict:
    """Get basic ticker info via Yahoo Finance quoteSummary."""
    key = f"{ticker.upper()}:info"
    now = time.time()
    if key in _cache:
        info, expiry = _cache[key]
        if now < expiry:
            return info

    try:
        if not _yf_acquire():
            logger.warning(f"yahoo rate-limit timeout for ticker_info {ticker}")
            return {"name": ticker.upper()}
        crumb = _get_crumb()
        sess = _get_session()
        url = f"{BASE_URL}/v10/finance/quoteSummary/{ticker.upper()}"
        params = {"modules": "price,summaryProfile", "crumb": crumb}
        resp = sess.get(url, params=params, timeout=15)
        data = resp.json()
        block = data.get("quoteSummary", {}).get("result", [{}])[0]
        price_data = block.get("price", {}) or {}
        profile = block.get("summaryProfile", {}) or {}
        result = {
            "name": price_data.get("longName") or price_data.get("shortName", ticker),
            # C3 fix: actually extract sector (was hardcoded ""). Without this,
            # the auto-trader's per-sector cap is a no-op because every trade
            # has sector="" and max_per_sector never gates. yfinance returns
            # sector on the summaryProfile module (already in our params list).
            "sector": (profile.get("sector") or "").strip(),
            "industry": (profile.get("industry") or "").strip(),
            "currency": price_data.get("currency", "USD"),
        }
        _cache[key] = (result, now + 3600)
        return result
    except Exception as e:
        logger.warning(f"Could not fetch info for {ticker}: {e}")
        # Try fallback: get name from chart meta
        try:
            df = fetch_ohlcv(ticker, "1d")
            if not df.empty:
                return {"name": ticker.upper()}
        except Exception:
            pass
        return {"name": ticker.upper()}


def _alpaca_latest_trade(ticker: str) -> Optional[float]:
    """REST fallback for a real-time last trade price via Alpaca's snapshot
    endpoint. Used when the WS quote cache is empty / stale (e.g. right
    after container boot, extended-hours with sparse prints, or during WS
    reconnect). 10s result cache so we don't hit Alpaca for every
    watchlist row during an overview build."""
    import os as _os_lt, time as _t_lt, httpx as _httpx_lt
    now = _t_lt.time()
    cached = _latest_trade_cache.get(ticker.upper())
    if cached and now < cached[1]:
        return cached[0]
    key = _os_lt.getenv("APCA_API_KEY_ID")
    secret = _os_lt.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return None
    feed = (_os_lt.getenv("ALPACA_DATA_FEED", "iex") or "iex").lower()
    if feed not in ("iex", "sip"):
        feed = "iex"
    try:
        with _httpx_lt.Client(timeout=5.0) as client:
            r = client.get(
                f"https://data.alpaca.markets/v2/stocks/{ticker.upper()}/snapshot",
                headers={
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                },
                params={"feed": feed},
            )
        if r.status_code != 200:
            return None
        d = r.json() or {}
        lt = (d.get("latestTrade") or {}).get("p")
        if lt:
            px = float(lt)
            _latest_trade_cache[ticker.upper()] = (px, now + 10)
            return px
    except Exception:
        return None
    return None


# r44 fix #0.13: bounded TTL cache. Was unbounded — slow memory leak as the
# universe scanner adds tickers across days. OOM in a long-running Cloud
# Run instance is a real risk.
class _BoundedTradeCache:
    def __init__(self, max_entries: int = 2000):
        self._d: Dict[str, tuple] = {}
        self._max = max_entries

    def __setitem__(self, k: str, v: tuple) -> None:
        if len(self._d) >= self._max:
            # Drop oldest 10% (LRU-by-insertion approximation; expiry-based
            # would be more correct but pricier per-write).
            cutoff = self._max // 10
            for old_key in list(self._d.keys())[:cutoff]:
                self._d.pop(old_key, None)
        self._d[k] = v

    def __getitem__(self, k: str) -> tuple:
        return self._d[k]

    def get(self, k: str, default=None):
        return self._d.get(k, default)

    def __contains__(self, k: str) -> bool:
        return k in self._d

    def pop(self, k: str, default=None):
        return self._d.pop(k, default)


_latest_trade_cache = _BoundedTradeCache(max_entries=2000)


def get_current_price(ticker: str) -> Optional[Tuple[float, float]]:
    """Return (current_price, change_pct).

    Price priority:
      1. WebSocket live quote (sub-second, populated by StockDataStream)
      2. Alpaca REST snapshot latestTrade (fresh, covers ext-hours)
      3. Last daily bar close (fallback; regular-session close)
    change_pct is computed vs the previous daily close from OHLCV data so
    it stays consistent across the 3 price sources.
    """
    try:
        df = fetch_ohlcv(ticker, "1d")
        if df.empty or len(df) < 2:
            return None
        prev = float(df.iloc[-2]["Close"])
        latest = float(df.iloc[-1]["Close"])  # daily-bar fallback

        # 1) Prefer live WS quote — populated as SIP ticks arrive.
        live = None
        try:
            from services.live_quotes import get_live_price  # local import avoids cycle
            live = get_live_price(ticker)
        except Exception:
            live = None
        if live and live > 0:
            latest = live
        else:
            # 2) WS cache empty or stale — hit Alpaca snapshot REST for the
            #    most recent trade. Keeps watchlist consistent with chart.
            lt = _alpaca_latest_trade(ticker)
            if lt and lt > 0:
                latest = lt

        change_pct = ((latest - prev) / prev) * 100
        return round(latest, 2), round(change_pct, 2)
    except Exception:
        return None


def invalidate_cache(ticker: str):
    keys_to_remove = [k for k in _cache if k.startswith(ticker.upper() + ":")]
    for k in keys_to_remove:
        del _cache[k]
