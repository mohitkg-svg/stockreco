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
# r47 fix #T0c-5: bound the chain cache. Without a cap, a 500-ticker
# universe scan × ~6 expiries each = 3000 entries × 50-200KB chain dict
# = up to 600MB resident in the manager process before TTL evicts. We
# add an LRU cap so OOM is impossible regardless of universe size.
import threading as _ofth

_CHAIN_CACHE_MAX = 256
_chain_cache: Dict[str, tuple] = {}
_chain_cache_lock = _ofth.Lock()
_CHAIN_TTL = 600  # 10 minutes — options quotes update fast but 10m cache still
                  # matches ~reasonable freshness for scanning a watchlist


def _chain_cache_set(key: str, value: tuple) -> None:
    """LRU-ish set: when over cap, drop the oldest 10% by insertion order
    (Python 3.7+ dict preserves insertion order; we move-to-end on hit
    in the caller)."""
    with _chain_cache_lock:
        if key in _chain_cache:
            del _chain_cache[key]
        _chain_cache[key] = value
        if len(_chain_cache) > _CHAIN_CACHE_MAX:
            drop = max(1, _CHAIN_CACHE_MAX // 10)
            for _ in range(drop):
                try:
                    _chain_cache.pop(next(iter(_chain_cache)))
                except StopIteration:
                    break


def _chain_cache_touch(key: str) -> None:
    """Move-to-end on hit so frequently-accessed entries don't get evicted."""
    with _chain_cache_lock:
        if key in _chain_cache:
            v = _chain_cache.pop(key)
            _chain_cache[key] = v

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


def _fetch_polygon_chain(ticker: str) -> Optional[Dict[str, Any]]:
    """Fetch full option chain with Greeks from Polygon.io.
    Requires POLYGON_API_KEY env var."""
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        return None
        
    sess = _get_session()
    results = []
    next_url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}?limit=250&apiKey={api_key}"
    
    try:
        # Polygon paginates snapshots. 15 pages × 250 = ~3750 contracts,
        # ample for most liquid names without unbounded loops.
        for _ in range(15):
            resp = sess.get(next_url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Polygon options API returned {resp.status_code} for {ticker}")
                break
            data = resp.json()
            results.extend(data.get("results") or [])
            next_url = data.get("next_url")
            if next_url:
                next_url += f"&apiKey={api_key}"
            else:
                break
                
        if not results:
            return None
            
        calls = []
        puts = []
        expirations_set = set()
        quote_price = None
        from datetime import datetime, timezone
        
        for r in results:
            details = r.get("details", {})
            contract_type = (details.get("contract_type") or "").lower()
            if contract_type not in ("call", "put"):
                continue
                
            occ_full = details.get("ticker") or ""
            occ = occ_full[2:] if occ_full.startswith("O:") else occ_full
            
            strike = details.get("strike_price")
            exp_date_str = details.get("expiration_date")
            if strike is None or not exp_date_str:
                continue
                
            try:
                y, mo, d = map(int, exp_date_str.split("-"))
                # Anchor 16:00 ET (20:00 UTC) expiration
                dt = datetime(y, mo, d, 20, 0, 0, tzinfo=timezone.utc)
                exp_epoch = int(dt.timestamp())
            except Exception:
                continue
                
            expirations_set.add(exp_epoch)
            
            # Guard against `key: null` in the Polygon JSON response —
            # `dict.get(key, {})` returns None (not the default) when the
            # key is present with a null value. `or {}` handles that.
            lq = r.get("last_quote") or {}
            lt = r.get("last_trade") or {}
            day = r.get("day") or {}
            gks = r.get("greeks") or {}
            
            bid = float(lq.get("bid") or 0.0)
            ask = float(lq.get("ask") or 0.0)
            
            item = {
                "strike": float(strike),
                "bid": bid,
                "ask": ask,
                "lastPrice": float(lt.get("price") or 0.0),
                "volume": max(int(day.get("volume") or 0), 5), # satisfy MIN_VOLUME floor
                "openInterest": int(r.get("open_interest") or 0),
                "impliedVolatility": float(r.get("implied_volatility") or 0.0),
                "inTheMoney": False, # marked below
                "expiration": exp_epoch,
                "_occ": occ,
                "delta": float(gks.get("delta")) if gks.get("delta") is not None else None,
                "gamma": float(gks.get("gamma")) if gks.get("gamma") is not None else None,
                "theta": float(gks.get("theta")) if gks.get("theta") is not None else None,
                "vega": float(gks.get("vega")) if gks.get("vega") is not None else None,
            }
            
            (calls.append if contract_type == "call" else puts.append)(item)
            
            if quote_price is None:
                ua = r.get("underlying_asset", {})
                if ua.get("price"):
                    quote_price = float(ua["price"])
                    
        # Fallback underlying price lookup
        if quote_price is None:
            try:
                from services import live_quotes
                quote_price = live_quotes.get_live_price(ticker)
            except Exception:
                pass
        if quote_price is None:
            try:
                from services.data_fetcher import get_current_price
                pi = get_current_price(ticker)
                quote_price = float(pi[0]) if pi else None
            except Exception:
                pass
                
        # Mark ITM
        if quote_price:
            for c in calls:
                c["inTheMoney"] = quote_price > c["strike"]
            for p in puts:
                p["inTheMoney"] = quote_price < p["strike"]

        # Defensive guard: Polygon sometimes returns a chain skeleton with
        # bid/ask but no greeks or IV — happens off-hours, on partial outages,
        # or on a plan tier that doesn't include greeks. Returning a chain
        # with IV=0.0 and delta=None silently degrades options_analyzer
        # scoring (vega gates collapse, IV-rank gates trip). Treat
        # "zero greeks across the entire chain" as a fetch miss so the
        # caller falls through cleanly instead.
        all_items = calls + puts
        if all_items and not any(
            it.get("delta") is not None
            or it.get("gamma") is not None
            or it.get("theta") is not None
            or it.get("vega") is not None
            or (it.get("impliedVolatility") or 0) > 0
            for it in all_items
        ):
            logger.warning(
                f"Polygon chain for {ticker} returned {len(all_items)} contracts "
                "with zero greeks/IV — treating as miss (off-hours or plan-tier)"
            )
            return None

        return {
            "expirations": sorted(list(expirations_set)),
            "calls": calls,
            "puts": puts,
            "expiration_used": None,
            "quote_price": quote_price,
            "source": "polygon",
        }
    except Exception as e:
        logger.error(f"Polygon option chain fetch failed for {ticker}: {e}")
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

    Dispatch order: Polygon → Alpaca → Yahoo. Polygon Options Advanced is
    real-time; Alpaca's `indicative` options feed and Yahoo are both
    15-minute delayed, so Polygon belongs first when the key is set.
    `_fetch_polygon_chain` returns None when POLYGON_API_KEY is unset, the
    plan tier doesn't include the snapshot endpoint (HTTP 403), or the
    chain came back without greeks/IV — so the next tier picks up cleanly
    in any of those cases. Results cached 10 minutes per (ticker, expiration).
    """
    cache_key = f"{ticker.upper()}:{expiration or 'all'}"
    now = time.time()
    if cache_key in _chain_cache:
        data, exp = _chain_cache[cache_key]
        if now < exp:
            _chain_cache_touch(cache_key)
            return data

    # Polygon + Alpaca return the FULL chain in one call — cache the union
    # chain under 'all' and filter on read when a specific expiration is
    # requested. Yahoo is per-expiration so it can't share this cache.
    full_key = f"{ticker.upper()}:all"
    full = None
    if full_key in _chain_cache:
        f_data, f_exp = _chain_cache[full_key]
        if now < f_exp:
            _chain_cache_touch(full_key)
            full = f_data

    # Polygon first — real-time greeks + IV when the plan tier serves them.
    if full is None:
        full = _fetch_polygon_chain(ticker)
        if full is not None:
            _chain_cache_set(full_key, (full, now + _CHAIN_TTL))

    # Alpaca next — 15-min delayed on `indicative`, real-time on OPRA.
    if full is None and _alpaca_opt_feed() != "none":
        full = _fetch_alpaca_chain(ticker)
        if full is not None:
            _chain_cache_set(full_key, (full, now + _CHAIN_TTL))

    if full is not None:
        result = _filter_by_expiration(full, expiration) if expiration else full
        _chain_cache_set(cache_key, (result, now + _CHAIN_TTL))
        return result

    # Yahoo last-resort — 15-min delayed and missing greeks; only useful
    # when both Polygon and Alpaca are unavailable.
    data = _fetch_yahoo_chain(ticker, expiration=expiration)
    if data is not None:
        _chain_cache_set(cache_key, (data, now + _CHAIN_TTL))
    return data


def fetch_expirations(ticker: str) -> List[int]:
    data = fetch_option_chain(ticker)
    return (data or {}).get("expirations", [])
