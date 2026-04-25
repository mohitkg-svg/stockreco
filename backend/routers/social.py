"""Social sentiment + insider-trades API."""
from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException

from routers._auth import require_api_key
from services import social_sentiment as st
from services import insider_trades as ins

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
