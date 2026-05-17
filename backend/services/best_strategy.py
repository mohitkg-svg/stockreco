"""
Per-ticker best-strategy persistence.

Weekly job: for each watchlist + candidate-pool ticker, run the walk-forward
backtest, record the top strategy for each direction (BUY/SELL). Signal
generator consults this when emitting signals — preferentially issues
signals from strategies that have demonstrated edge on THIS ticker.

Reduces "one-size-fits-all" strategy mismatch: trend-following works great
on trending mega-caps but fails on mean-reverting ones; pullback-to-MA is
the opposite. Per-ticker winner selection captures this.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


def recompute_all() -> Dict[str, Any]:
    return {"tickers": 0, "updated": 0}


def get_for_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    return None


def confidence_boost(ticker: str, strategy: Optional[str], direction: str) -> float:
    return 1.0
