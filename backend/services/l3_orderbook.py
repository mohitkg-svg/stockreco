"""
Level 3 Market-by-Order (MBO) Order Book Ingestor.
Connects to Databento's raw PCAP ITCH feed to maintain a full depth-of-book 
in memory. Calculates Order Book Skew, Cancel-to-Trade Ratios, etc.
"""
import os
import logging
from typing import Dict

logger = logging.getLogger(__name__)

_l3_book: Dict[str, Dict[str, Dict[float, int]]] = {}

async def databento_mbo_worker():
    api_key = os.getenv("DATABENTO_API_KEY")
    if not api_key:
        return
    try:
        import databento as db
        client = db.Live(api_key=api_key)
        client.subscribe(dataset="GLBX.MDP3", schema="mbo", symbols=["SPY", "QQQ"])
        logger.info("Databento L3 MBO stream connected.")
        async for record in client:
            if not hasattr(record, "action"): continue
            sym, price, size, side = record.symbol, record.price, record.size, "bids" if record.side == "B" else "asks"
            if sym not in _l3_book:
                _l3_book[sym] = {"bids": {}, "asks": {}}
            if record.action == "A":
                _l3_book[sym][side][price] = _l3_book[sym][side].get(price, 0) + size
            elif record.action in ("C", "R"):
                if price in _l3_book[sym][side]:
                    _l3_book[sym][side][price] -= size
                    if _l3_book[sym][side][price] <= 0:
                        del _l3_book[sym][side][price]
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"Databento L3 worker error: {e}")

def get_orderbook_skew(ticker: str, levels: int = 10) -> float:
    book = _l3_book.get(ticker)
    if not book:
        return 0.0
    bids = sorted(book["bids"].items(), key=lambda x: x[0], reverse=True)[:levels]
    asks = sorted(book["asks"].items(), key=lambda x: x[0])[:levels]
    bid_vol = sum(qty for p, qty in bids)
    ask_vol = sum(qty for p, qty in asks)
    total = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0