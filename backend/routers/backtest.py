from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db, WatchlistStock
from models import (
    BacktestResponse, BacktestStats,
    MultiStrategyBacktestResponse, StrategyResult,
)
from services.data_fetcher import fetch_ohlcv
from services.backtester import run_multi_strategy
from services.portfolio_backtest import run_portfolio_backtest, STRESS_WINDOWS
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


@router.post("/portfolio/run")
def backtest_portfolio(
    starting_equity: float = 100_000.0,
    risk_per_trade_pct: float = 0.02,
    max_concurrent: int = 15,
    max_per_sector: int = 5,
    max_portfolio_heat_pct: float = 0.10,
    daily_loss_limit_pct: float = 0.03,
    max_tickers: int = 50,
    lookback_days: int = 365,
    stress_window: str = "",
):
    """Portfolio-level walk-forward backtest that honours the live-trader's
    caps (concurrent positions, per-sector, beta-weighted heat, daily loss).
    Returns composite equity curve, drawdown, sharpe, cap-rejection count.

    `stress_window` (optional): one of the canned historical drawdown
    windows from `/api/backtest/portfolio/stress-windows`. When set, the
    backtest runs over that fixed date range instead of the trailing
    `lookback_days` window."""
    return run_portfolio_backtest(
        starting_equity=starting_equity,
        risk_per_trade_pct=risk_per_trade_pct,
        max_concurrent=max_concurrent,
        max_per_sector=max_per_sector,
        max_portfolio_heat_pct=max_portfolio_heat_pct,
        daily_loss_limit_pct=daily_loss_limit_pct,
        max_tickers=max_tickers,
        lookback_days=lookback_days,
        stress_window=stress_window or None,
    )


@router.get("/portfolio/stress-windows")
def list_stress_windows():
    """List the canned historical drawdown windows the portfolio backtest
    can replay. Pre-live "what if I'd been live during X" answer."""
    return {
        "windows": [
            {"key": k, "start": s, "end": e, "label": l}
            for k, (s, e, l) in STRESS_WINDOWS.items()
        ]
    }
