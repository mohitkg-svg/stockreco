from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db, WatchlistStock
from models import (
    BacktestResponse, BacktestStats,
    MultiStrategyBacktestResponse, StrategyResult,
)
from services.data_fetcher import fetch_ohlcv
from services.backtester import run_multi_strategy
from routers._auth import require_api_key
import logging

# Backtest is compute-heavy (2y of multi-strategy sim + yfinance fetch). Gate
# to prevent resource abuse from the open internet.
router = APIRouter(
    prefix="/api/backtest",
    tags=["backtest"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.post("/{ticker}", response_model=MultiStrategyBacktestResponse)
def backtest_ticker(ticker: str, db: Session = Depends(get_db)):
    """
    Evaluate every supported strategy on this ticker (daily data, 2y history).
    Returns results ranked best → worst so the caller can see which approach
    works for this specific stock and use the best strategy's confidence.
    """
    ticker = ticker.upper()
    existing = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
    if not existing:
        raise HTTPException(status_code=404, detail=f"{ticker} not in watchlist")

    df = fetch_ohlcv(ticker, "1d")
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No daily data for {ticker}")

    multi = run_multi_strategy(df, timeframe="1d")
    results = [
        StrategyResult(
            strategy=r["strategy"],
            description=r["description"],
            direction=r["direction"],
            confidence=r["confidence"],
            stats=BacktestStats(**r["stats"]),
            equity_curve=r["equity_curve"],
            trades=r["trades"],
        )
        for r in multi["results"]
    ]
    best = multi["best"]

    return MultiStrategyBacktestResponse(
        ticker=ticker,
        best_strategy=best["strategy"] if best else None,
        best_direction=best["direction"] if best else None,
        best_confidence=best["confidence"] if best else None,
        results=results,
    )
