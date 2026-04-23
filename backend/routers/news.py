"""
News API — phase 1 observability.

  GET  /api/news                            — recent news across watchlist
  GET  /api/news/{ticker}                   — news for one ticker
  GET  /api/news/trade/{trade_id}/context   — news during a trade's lifetime
  GET  /api/news/analysis/summary           — trade outcomes vs news sentiment
  POST /api/news/poll                       — manual trigger (admin)
"""
from __future__ import annotations
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from routers._auth import require_api_key
from services import news as news_svc

router = APIRouter(
    prefix="/api/news",
    tags=["news"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("")
def recent_news(
    limit: int = Query(50, ge=1, le=200),
    hours: int = Query(24, ge=1, le=168),
):
    """Recent news across all watchlist tickers, newest first."""
    return news_svc.list_recent(limit=limit, since_hours=hours)


@router.get("/analysis/summary")
def summary(days: int = Query(7, ge=1, le=90)):
    """Aggregate analysis: trade outcomes vs prevailing news sentiment.

    The workflow asked for: after a week of data, evaluate whether news
    sentiment aligns with trade outcomes. This returns the 2×2 matrix
    (sentiment bucket × outcome) plus per-trade details so the user can
    decide whether to wire news into auto-trader logic in phase 2.
    """
    return news_svc.summary_analysis(days=days)


@router.get("/trade/{trade_id}/context")
def trade_context(
    trade_id: int,
    before_hours: int = Query(24, ge=0, le=168),
    after_hours: int = Query(24, ge=0, le=168),
):
    """News that landed during (and bracketing) a specific trade's lifetime."""
    result = news_svc.trade_context(
        trade_id,
        window_hours_before=before_hours,
        window_hours_after=after_hours,
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/poll")
def manual_poll():
    """Admin utility — trigger a one-shot news poll (normally runs every 2min)."""
    return news_svc.poll_watchlist()


# Note: ticker route is LAST so FastAPI doesn't try to match "/analysis" or
# "/trade" as a ticker. FastAPI matches routes in declaration order.
@router.get("/{ticker}")
def ticker_news(
    ticker: str,
    limit: int = Query(25, ge=1, le=100),
    hours: int = Query(72, ge=1, le=720),
):
    """News for a single ticker, newest first."""
    return news_svc.list_for_ticker(ticker, limit=limit, since_hours=hours)
