"""Social sentiment + insider-trades API."""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException

from routers._auth import require_api_key
from services import social_sentiment as st
from services import insider_trades as ins
from services import wsb_scraper as wsb
from services import institutional as inst

router = APIRouter(
    prefix="/api",
    tags=["social"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("/social/sentiment/{ticker}")
def get_sentiment(ticker: str):
    r = st.get_sentiment(ticker)
    if not r:
        raise HTTPException(status_code=404, detail=f"No sentiment for {ticker} yet")
    return r


@router.post("/social/sentiment/{ticker}/refresh")
def refresh_sentiment(ticker: str):
    r = st.refresh_ticker(ticker)
    if r is None:
        raise HTTPException(status_code=502, detail=f"Could not fetch sentiment for {ticker}")
    return r


@router.post("/social/sentiment/refresh-all")
def refresh_sentiment_all():
    return st.refresh_all()


@router.get("/insider/{ticker}")
def get_insider(ticker: str):
    r = ins.get_insider(ticker)
    if not r:
        raise HTTPException(status_code=404, detail=f"No insider rollup for {ticker} yet")
    return r


@router.post("/insider/{ticker}/refresh")
def refresh_insider(ticker: str):
    r = ins.refresh_ticker(ticker)
    if r is None:
        raise HTTPException(status_code=502, detail=f"Could not fetch insider data for {ticker}")
    return r


@router.post("/insider/refresh-all")
def refresh_insider_all():
    return ins.refresh_all()


@router.get("/wsb/{ticker}")
def get_wsb(ticker: str):
    r = wsb.get_mentions(ticker)
    if not r:
        raise HTTPException(status_code=404, detail=f"No WSB mentions recorded for {ticker}")
    return r


@router.post("/wsb/refresh")
def refresh_wsb():
    """Pull the latest r/wallstreetbets posts + comments and recount mentions
    across the watchlist + candidate pool."""
    return wsb.refresh_once()


@router.get("/institutional/{ticker}")
def get_institutional(ticker: str):
    r = inst.get_holdings(ticker)
    if not r:
        raise HTTPException(status_code=404, detail=f"No institutional holdings for {ticker} yet")
    return r


@router.post("/institutional/{ticker}/refresh")
def refresh_institutional_one(ticker: str):
    r = inst.refresh_ticker(ticker)
    if r is None:
        raise HTTPException(status_code=502, detail=f"Could not fetch institutional for {ticker}")
    return r


@router.post("/institutional/refresh-all")
def refresh_institutional_all():
    return inst.refresh_all()
