"""
High-Frequency Order Book Skew Ingestor (Polygon.io Shim).

Replaces the Databento L3 MBO ingestor. While Polygon does not provide true 
Level 3 Market-by-Order data, we can subscribe to their high-frequency 
equities quote stream (Q) to maintain a rolling top-of-book skew.
"""
import os
import json
import asyncio
import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Holds the latest bid/ask sizes for our macro tickers
_l1_quotes: Dict[str, Dict[str, float]] = {}

async def polygon_quote_worker():
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        return
        
    import websockets
    
    uri = "wss://socket.polygon.io/stocks"
    backoff = 5
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"action": "auth", "params": api_key}))
                auth_resp = json.loads(await ws.recv())
                if not auth_resp or auth_resp[0].get("status") != "auth_success":
                    logger.warning(f"polygon-l3-shim auth failed: {auth_resp}")
                    await asyncio.sleep(backoff)
                    continue
                    
                logger.info("Polygon high-frequency quote stream connected (Shim for L3 Skew).")
                await ws.send(json.dumps({"action": "subscribe", "params": "Q.SPY,Q.QQQ"}))
                backoff = 5
                
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        events = json.loads(msg)
                        for ev in events:
                            if ev.get("ev") == "Q":
                                sym = ev.get("sym")
                                if sym:
                                    _l1_quotes[sym] = {
                                        "bid_size": float(ev.get("bs", 0)),
                                        "ask_size": float(ev.get("as", 0)),
                                    }
                    except asyncio.TimeoutError:
                        pass
        except Exception as e:
            logger.debug(f"Polygon L3 shim worker error, reconnecting in {backoff}s: {e}")
            await asyncio.sleep(backoff)
            backoff = min(300, backoff * 2)

def get_orderbook_skew(ticker: str, levels: int = 10) -> float:
    """
    Calculates skew. Since we use Polygon L1, `levels` is ignored, 
    and we return the NBBO size imbalance.
    """
    q = _l1_quotes.get(ticker.upper())
    if not q:
        return 0.0
    bid_vol = q["bid_size"]
    ask_vol = q["ask_size"]
    total = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0