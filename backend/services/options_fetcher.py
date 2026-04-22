"""Yahoo Finance options chain fetcher (via curl_cffi)."""
from typing import Optional, Dict, Any, List
import time
import logging
from services.data_fetcher import _get_session, _get_crumb, BASE_URL

logger = logging.getLogger(__name__)

# {ticker: (chain_data, expiry_ts)}
_chain_cache: Dict[str, tuple] = {}
_CHAIN_TTL = 600  # 10 minutes


def fetch_option_chain(ticker: str, expiration: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Return {'expirations': [ts,...], 'calls': [...], 'puts': [...], 'quote': {...}}
    for the requested expiration (or nearest if not specified).
    """
    cache_key = f"{ticker.upper()}:{expiration or 'nearest'}"
    now = time.time()
    if cache_key in _chain_cache:
        data, exp = _chain_cache[cache_key]
        if now < exp:
            return data

    try:
        sess = _get_session()
        crumb = _get_crumb()
        url = f"{BASE_URL}/v7/finance/options/{ticker.upper()}"
        params = {"crumb": crumb}
        if expiration:
            params["date"] = expiration
        resp = sess.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Options API {resp.status_code} for {ticker}")
            return None
        j = resp.json()
        result = (j.get("optionChain", {}).get("result") or [{}])[0]
        if not result:
            return None
        exps = result.get("expirationDates") or []
        quote = result.get("quote", {}) or {}
        opts = (result.get("options") or [{}])[0] if result.get("options") else {}
        data = {
            "expirations": exps,
            "calls": opts.get("calls", []),
            "puts": opts.get("puts", []),
            "expiration_used": opts.get("expirationDate"),
            "quote_price": quote.get("regularMarketPrice"),
        }
        _chain_cache[cache_key] = (data, now + _CHAIN_TTL)
        return data
    except Exception as e:
        logger.error(f"Option chain fetch failed for {ticker}: {e}")
        return None


def fetch_expirations(ticker: str) -> List[int]:
    data = fetch_option_chain(ticker)
    return (data or {}).get("expirations", [])
