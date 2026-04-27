"""Alpaca consolidated tape — fetch trades + compute microstructure features.

Endpoint: GET https://data.alpaca.markets/v2/stocks/{symbol}/trades
Returns every printed trade in the requested time window. Available with
Algo Trader Plus subscription (SIP feed).

Used by ml_features to enrich the feature row with order-flow microstructure
(trade-size distribution, buy/sell imbalance via tick rule, tape acceleration).

Caching:
  * `_DAY_CACHE` — full-day trade DataFrames keyed by (ticker, date). Used
    during training to avoid re-fetching the same day for many samples.
  * `_LIVE_CACHE` — last-30-min DataFrames with 60s TTL for inference path.
"""
from __future__ import annotations
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_DAY_CACHE: Dict[Tuple[str, str], pd.DataFrame] = {}
_DAY_CACHE_LOCK = threading.Lock()
_LIVE_CACHE: Dict[str, Tuple[pd.DataFrame, float]] = {}
_LIVE_TTL_SEC = 60.0
_HTTP_TIMEOUT = 20.0
# Cap how many records we'll fetch per page; Alpaca returns up to 10000.
_PAGE_LIMIT = 10000


def _client_creds():
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    feed = (os.getenv("ALPACA_DATA_FEED", "iex") or "iex").lower()
    if feed not in ("iex", "sip"):
        feed = "iex"
    return key, secret, feed


def _fetch_window(ticker: str, start_iso: str, end_iso: str) -> Optional[pd.DataFrame]:
    """Pull trades between start_iso and end_iso (RFC-3339 UTC). Paginates."""
    import httpx
    key, secret, feed = _client_creds()
    if not key or not secret:
        return None
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params: Dict[str, Any] = {
        "start": start_iso,
        "end": end_iso,
        "limit": _PAGE_LIMIT,
        "feed": feed,
    }
    rows: list = []
    page_token: Optional[str] = None
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            for _ in range(20):  # hard cap pages to avoid runaway
                if page_token:
                    params["page_token"] = page_token
                r = client.get(
                    f"https://data.alpaca.markets/v2/stocks/{ticker.upper()}/trades",
                    headers=headers, params=params,
                )
                if r.status_code != 200:
                    logger.debug(f"alpaca_tape {ticker}: HTTP {r.status_code} {r.text[:120]}")
                    return None
                d = r.json() or {}
                rows.extend(d.get("trades", []))
                page_token = d.get("next_page_token")
                if not page_token:
                    break
    except Exception as e:
        logger.debug(f"alpaca_tape fetch {ticker} failed: {e}")
        return None
    if not rows:
        return pd.DataFrame(columns=["t", "p", "s", "x"])
    df = pd.DataFrame(rows)
    # Normalize column names: t (timestamp), p (price), s (size), x (exchange)
    if "t" not in df.columns:
        return None
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df["p"] = pd.to_numeric(df["p"], errors="coerce")
    df["s"] = pd.to_numeric(df["s"], errors="coerce")
    df = df.dropna(subset=["p", "s"]).sort_values("t").reset_index(drop=True)
    return df


def fetch_full_day(ticker: str, day: datetime) -> Optional[pd.DataFrame]:
    """Fetch all trades on a UTC day. Cached per (ticker, day-iso) — used by
    the trainer which makes many feature calls within the same trading day."""
    key = (ticker.upper(), day.strftime("%Y-%m-%d"))
    with _DAY_CACHE_LOCK:
        cached = _DAY_CACHE.get(key)
        if cached is not None:
            return cached
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    df = _fetch_window(ticker, start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"))
    with _DAY_CACHE_LOCK:
        _DAY_CACHE[key] = df if df is not None else pd.DataFrame()
        # Bound cache size — drop oldest if over 200 entries
        if len(_DAY_CACHE) > 200:
            _DAY_CACHE.pop(next(iter(_DAY_CACHE)))
    return df


_LIVE_CACHE_MAX = 100  # r47 fix #T0h: cap to bound RSS during 500-ticker scans


def fetch_live_window(ticker: str, lookback_minutes: int = 30) -> Optional[pd.DataFrame]:
    """Last N minutes of trades for the live inference path. 60s TTL.

    r47 fix #T0h: bounded LRU. Without a cap, a 500-ticker universe scan
    × 100KB DataFrame each = 50MB RSS just for tape cache; stale entries
    past TTL still occupy memory until overwritten.
    """
    now_ts = time.time()
    key = ticker.upper()
    cached = _LIVE_CACHE.get(key)
    if cached and (now_ts - cached[1]) < _LIVE_TTL_SEC:
        return cached[0]
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=lookback_minutes)
    df = _fetch_window(ticker, start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"))
    if df is None:
        return None
    _LIVE_CACHE[key] = (df, now_ts)
    if len(_LIVE_CACHE) > _LIVE_CACHE_MAX:
        # Drop oldest 10% by insertion order
        for _ in range(max(1, _LIVE_CACHE_MAX // 10)):
            try:
                _LIVE_CACHE.pop(next(iter(_LIVE_CACHE)))
            except StopIteration:
                break
    return df


def _slice_window(df: pd.DataFrame, end_ts: datetime, lookback_minutes: int) -> pd.DataFrame:
    """Slice a trade DataFrame to (end_ts - lookback) ≤ t ≤ end_ts."""
    if df is None or df.empty:
        return df
    end_pd = pd.Timestamp(end_ts).tz_localize("UTC") if pd.Timestamp(end_ts).tzinfo is None else pd.Timestamp(end_ts).tz_convert("UTC")
    start_pd = end_pd - pd.Timedelta(minutes=lookback_minutes)
    return df[(df["t"] >= start_pd) & (df["t"] <= end_pd)]


def _tick_rule_imbalance(df: pd.DataFrame) -> Optional[float]:
    """Lee-Ready tick rule: a trade above the prior trade is a buy, below is a
    sell, equal carries the prior classification. Returns (#buys - #sells) /
    total, range [-1, +1]. Positive = net buying pressure."""
    if df is None or df.empty or len(df) < 5:
        return None
    prices = df["p"].values
    sign = 0
    buys = 0
    sells = 0
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        if diff > 0:
            sign = 1
        elif diff < 0:
            sign = -1
        if sign > 0:
            buys += 1
        elif sign < 0:
            sells += 1
    total = buys + sells
    if total == 0:
        return None
    return (buys - sells) / total


def microstructure_features(ticker: str, as_of: datetime,
                            lookback_minutes: int = 30,
                            day_df: Optional[pd.DataFrame] = None) -> Dict[str, Optional[float]]:
    """Compute microstructure features for the lookback window ending at as_of.

    Pass `day_df` (full-day trades) when training to avoid re-fetching; the
    function will slice it to the lookback window. For live inference, leave
    `day_df` None and we'll fetch the last 30 min directly.

    Returns: 6 features. None when data unavailable (LightGBM tolerates NaN).
    """
    out: Dict[str, Optional[float]] = {
        "ms_trade_count": None,
        "ms_avg_size": None,
        "ms_dollar_volume": None,
        "ms_block_trade_pct": None,
        "ms_buysell_imbalance": None,
        "ms_tape_accel": None,
    }
    df: Optional[pd.DataFrame]
    if day_df is not None:
        df = _slice_window(day_df, as_of, lookback_minutes)
    else:
        df = fetch_live_window(ticker, lookback_minutes=lookback_minutes)
    if df is None or df.empty:
        return out

    out["ms_trade_count"] = int(len(df))
    out["ms_avg_size"] = float(df["s"].mean())
    out["ms_dollar_volume"] = float((df["s"] * df["p"]).sum())
    # "Block" = single trade ≥ 10K shares (institutional-sized print)
    out["ms_block_trade_pct"] = float((df["s"] >= 10000).mean())
    out["ms_buysell_imbalance"] = _tick_rule_imbalance(df)

    # Tape acceleration: trade-rate in last 1/5 of the window vs prior 4/5.
    if len(df) >= 20:
        cut = pd.Timestamp(as_of).tz_localize("UTC") if pd.Timestamp(as_of).tzinfo is None else pd.Timestamp(as_of).tz_convert("UTC")
        split = cut - pd.Timedelta(minutes=lookback_minutes / 5)
        tail = df[df["t"] >= split]
        head = df[df["t"] < split]
        if len(head) > 0:
            tail_rate = len(tail) / max(1.0, lookback_minutes / 5)
            head_rate = len(head) / max(1.0, lookback_minutes - lookback_minutes / 5)
            if head_rate > 0:
                out["ms_tape_accel"] = tail_rate / head_rate
    return out
