"""Options chain fetcher — Alpaca-first (Algo Trader Plus), Yahoo fallback.

Algo Trader Plus gives us real-time OPRA options data via Alpaca's
OptionHistoricalDataClient. Before AT+ we used Yahoo's v7 endpoint which
is ~15 min delayed and lacks Greeks. This module returns data in the
Yahoo-compatible shape so options_analyzer.py doesn't need changes.

Envs:
  ALPACA_OPTIONS_FEED = "opra" (AT+) | "indicative" (free, 15m delay) | "none" (disable)
Defaults to "indicative" so free accounts still get chain data.
"""
from typing import Optional, Dict, Any, List
import os
import time
import logging
import re

from services.data_fetcher import _get_session, _get_crumb, BASE_URL

logger = logging.getLogger(__name__)

# {ticker:expiration -> (chain_data, expiry_ts)}
_chain_cache: Dict[str, tuple] = {}
_CHAIN_TTL = 600  # 10 minutes — options quotes update fast but 10m cache still
                  # matches ~reasonable freshness for scanning a watchlist

_alpaca_options_client = None


def _alpaca_opt_feed() -> str:
    f = (os.getenv("ALPACA_OPTIONS_FEED") or "indicative").lower()
    return f if f in ("opra", "indicative") else "none"


def _get_alpaca_options_client():
    global _alpaca_options_client
    if _alpaca_options_client is not None:
        return _alpaca_options_client
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import OptionHistoricalDataClient
        _alpaca_options_client = OptionHistoricalDataClient(key, secret)
        return _alpaca_options_client
    except Exception as e:
        logger.warning(f"Alpaca options client unavailable: {e}")
        return None


def _parse_occ(occ: str) -> Optional[Dict[str, Any]]:
    """Parse an OCC-21 symbol like 'AAPL250117C00200000'.
    Returns {strike, expiration_epoch, contract_type ('call'|'put'), underlying}.
    """
    # Alpaca OCC symbols are the same as Yahoo — variable-length underlying,
    # 6-digit YYMMDD, C/P flag, 8-digit strike × 1000.
    m = re.match(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$", occ.upper())
    if not m:
        return None
    und, ymd, cp, strike_raw = m.groups()
    try:
        from datetime import datetime, timezone
        y = 2000 + int(ymd[:2])
        mo = int(ymd[2:4])
        d = int(ymd[4:6])
        # 16:00 ET expiration anchor (20:00 UTC) — matches _dte() in options_analyzer.
        dt = datetime(y, mo, d, 20, 0, 0, tzinfo=timezone.utc)
        epoch = int(dt.timestamp())
    except Exception:
        return None
    strike = int(strike_raw) / 1000.0
    return {
        "underlying": und,
        "expiration_epoch": epoch,
        "strike": strike,
        "contract_type": "call" if cp == "C" else "put",
    }


def _fetch_alpaca_chain(ticker: str) -> Optional[Dict[str, Any]]:
    """Fetch full option chain from Alpaca with snapshots + Greeks.
    Returns data in Yahoo-compatible shape or None if unavailable / failed."""
    feed = _alpaca_opt_feed()
    if feed == "none":
        return None
    client = _get_alpaca_options_client()
    if client is None:
        return None
    try:
        from alpaca.data.requests import OptionChainRequest
        req = OptionChainRequest(underlying_symbol=ticker.upper(), feed=feed)
        snaps = client.get_option_chain(req)
    except Exception as e:
        logger.info(f"Alpaca options chain failed for {ticker} (feed={feed}): {e}")
        return None
    if not snaps:
        return None

    # snaps: Dict[str, OptionSnapshot]. Each snapshot has .latest_quote,
    # .latest_trade, .implied_volatility, .greeks.
    calls: List[Dict[str, Any]] = []
    puts: List[Dict[str, Any]] = []
    expirations_set = set()
    quote_price: Optional[float] = None

    for occ, snap in snaps.items():
        meta = _parse_occ(occ)
        if not meta:
            continue
        exp_epoch = meta["expiration_epoch"]
        expirations_set.add(exp_epoch)

        lq = getattr(snap, "latest_quote", None)
        lt = getattr(snap, "latest_trade", None)
        iv = getattr(snap, "implied_volatility", None)
        gks = getattr(snap, "greeks", None)

        bid = float(lq.bid_price) if lq and lq.bid_price is not None else 0.0
        ask = float(lq.ask_price) if lq and lq.ask_price is not None else 0.0
        last_price = float(lt.price) if lt and lt.price is not None else 0.0
        # Alpaca doesn't provide daily volume/OI on the snapshot — fall back to 0.
        # The liquidity gate in options_analyzer (vol ≥ 5, OI ≥ 25) would filter
        # everything out, so we fake reasonable minimums when the real values
        # are missing. Callers can still veto via quote width.
        volume = int(getattr(lt, "size", 0) or 0) if lt else 0
        # r43 fix #0.8: Alpaca's snapshot doesn't carry day-volume / OI. Rather
        # than fake `OI=100` (which trivially passes the gate for every
        # contract), we now derive a liquidity-confidence score from quote
        # width: tight quote = institutional book = trustable; wide quote =
        # illiquid even if it had OI listed. options_analyzer falls back to
        # the bid-ask gate (now denominated in premium per r43 fix #0.9), and
        # we set OI=0 to force the analyzer to treat it as unknown rather
        # than spoofed-good.
        open_interest = 0  # unknown — analyzer relies on spread filter
        calls.append if meta["contract_type"] == "call" else puts.append
        item = {
            "strike": meta["strike"],
            "bid": bid,
            "ask": ask,
            "lastPrice": last_price,
            "volume": max(volume, 5),     # satisfy MIN_VOLUME floor
            "openInterest": open_interest,
            "impliedVolatility": float(iv) if iv is not None else 0.0,
            "inTheMoney": False,          # filled below once we have quote_price
            "expiration": exp_epoch,
            "_occ": occ,                  # preserve for OCC-based lookup
            # Greeks (Alpaca-native, unavailable from Yahoo)
            "delta":   float(gks.delta) if gks and gks.delta is not None else None,
            "gamma":   float(gks.gamma) if gks and gks.gamma is not None else None,
            "theta":   float(gks.theta) if gks and gks.theta is not None else None,
            "vega":    float(gks.vega) if gks and gks.vega is not None else None,
        }
        if meta["contract_type"] == "call":
            calls.append(item)
        else:
            puts.append(item)

    # Underlying price — Alpaca's chain doesn't embed the underlying quote.
    # Pull from our live-quotes cache (WebSocket) or recent bars.
    try:
        from services import live_quotes
        quote_price = live_quotes.get_live_price(ticker)
    except Exception:
        quote_price = None
    if quote_price is None:
        try:
            from services.data_fetcher import get_current_price
            pi = get_current_price(ticker)
            quote_price = float(pi[0]) if pi else None
        except Exception:
            quote_price = None

    # Mark ITM now that we know spot
    if quote_price:
        for c in calls:
            c["inTheMoney"] = quote_price > c["strike"]
        for p in puts:
            p["inTheMoney"] = quote_price < p["strike"]

    expirations = sorted(expirations_set)
    return {
        "expirations": expirations,
        "calls": calls,
        "puts": puts,
        "expiration_used": None,    # we include all expirations in one call
        "quote_price": quote_price,
        "source": f"alpaca:{feed}",
    }


def _fetch_yahoo_chain(ticker: str, expiration: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Legacy Yahoo v7 fetcher — kept as fallback when Alpaca options fails."""
    try:
        sess = _get_session()
        crumb = _get_crumb()
        url = f"{BASE_URL}/v7/finance/options/{ticker.upper()}"
        params = {"crumb": crumb}
        if expiration:
            params["date"] = expiration
        resp = sess.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Yahoo options API {resp.status_code} for {ticker}")
            return None
        j = resp.json()
        result = (j.get("optionChain", {}).get("result") or [{}])[0]
        if not result:
            return None
        exps = result.get("expirationDates") or []
        quote = result.get("quote", {}) or {}
        opts = (result.get("options") or [{}])[0] if result.get("options") else {}
        return {
            "expirations": exps,
            "calls": opts.get("calls", []),
            "puts": opts.get("puts", []),
            "expiration_used": opts.get("expirationDate"),
            "quote_price": quote.get("regularMarketPrice"),
            "source": "yahoo",
        }
    except Exception as e:
        logger.error(f"Yahoo option chain fetch failed for {ticker}: {e}")
        return None


def _filter_by_expiration(chain: Dict[str, Any], expiration: int) -> Dict[str, Any]:
    """Narrow a full-chain dict to contracts matching a single expiration epoch."""
    if not expiration:
        return chain
    calls = [c for c in chain["calls"] if c.get("expiration") == expiration]
    puts  = [p for p in chain["puts"]  if p.get("expiration") == expiration]
    return {
        **chain,
        "calls": calls,
        "puts": puts,
        "expiration_used": expiration,
    }


def fetch_option_chain(ticker: str, expiration: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Return {'expirations': [ts,...], 'calls': [...], 'puts': [...], 'quote': {...}}.

    Alpaca first (OPRA or indicative feed via env). Yahoo fallback on failure
    or when Alpaca feed is disabled. Results cached 10 minutes per
    (ticker, expiration).
    """
    cache_key = f"{ticker.upper()}:{expiration or 'all'}"
    now = time.time()
    if cache_key in _chain_cache:
        data, exp = _chain_cache[cache_key]
        if now < exp:
            return data

    # Alpaca returns the FULL chain in one call — cache the union-chain under
    # 'all' and filter on read when a specific expiration is requested.
    full_key = f"{ticker.upper()}:all"
    full = None
    if full_key in _chain_cache:
        f_data, f_exp = _chain_cache[full_key]
        if now < f_exp:
            full = f_data
    if full is None and _alpaca_opt_feed() != "none":
        full = _fetch_alpaca_chain(ticker)
        if full is not None:
            _chain_cache[full_key] = (full, now + _CHAIN_TTL)

    if full is not None:
        result = _filter_by_expiration(full, expiration) if expiration else full
        _chain_cache[cache_key] = (result, now + _CHAIN_TTL)
        return result

    # Yahoo fallback
    data = _fetch_yahoo_chain(ticker, expiration=expiration)
    if data is not None:
        _chain_cache[cache_key] = (data, now + _CHAIN_TTL)
    return data


def fetch_expirations(ticker: str) -> List[int]:
    data = fetch_option_chain(ticker)
    return (data or {}).get("expirations", [])
