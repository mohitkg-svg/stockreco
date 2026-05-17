"""Polygon.io historical data fetchers."""
import os
import httpx
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)
_client: Optional[httpx.Client] = None

def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=5.0)
    return _client

def get_historical_obi(ticker: str, as_of: datetime) -> Optional[float]:
    """Fetch historical NBBO from Polygon to calculate Order Book Imbalance (OBI).
    Requires POLYGON_API_KEY. Fetches the closest quote prior to `as_of`."""
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        return None
        
    ts_ns = int(as_of.timestamp() * 1e9)
    url = f"https://api.polygon.io/v3/quotes/{ticker.upper()}"
    params = {
        "timestamp.lte": ts_ns,
        "order": "desc",
        "limit": 1,
        "apiKey": api_key
    }
    
    try:
        c = _get_client()
        r = c.get(url, params=params)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                q = results[0]
                # Polygon option/stock sizes are strictly in lots of 100
                bid_size = float(q.get("bid_size", 0))
                ask_size = float(q.get("ask_size", 0))
                total = bid_size + ask_size
                if total > 0:
                    return (bid_size - ask_size) / total
    except Exception as e:
        logger.debug(f"Polygon historical OBI fetch failed for {ticker}: {e}")
        
    return None