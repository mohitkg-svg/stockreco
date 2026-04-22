from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from typing import List
from database import get_db, WatchlistStock
from models import AddTickerRequest, WatchlistItem
from services.data_fetcher import get_ticker_info, get_current_price
from services import live_quotes
from routers._auth import require_api_key
import logging

# Watchlist mutations indirectly drive auto-trades (add a ticker → scanner
# picks up signals → auto-trader opens positions). Gate everything.
router = APIRouter(
    prefix="/api/watchlist",
    tags=["watchlist"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.get("", response_model=List[WatchlistItem])
def get_watchlist(db: Session = Depends(get_db)):
    stocks = db.query(WatchlistStock).all()
    result = []
    for stock in stocks:
        price_info = get_current_price(stock.ticker)
        item = WatchlistItem(
            ticker=stock.ticker,
            name=stock.name,
            added_at=stock.added_at,
            price=price_info[0] if price_info else None,
            change_pct=price_info[1] if price_info else None,
            auto_trade_enabled=bool(getattr(stock, "auto_trade_enabled", True)),
        )
        result.append(item)
    return result


@router.patch("/{ticker}/auto-trade")
def set_auto_trade(ticker: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Toggle the per-ticker auto-trade gate. Body: {"enabled": true|false}."""
    ticker = ticker.upper()
    stock = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
    if not stock:
        raise HTTPException(status_code=404, detail=f"{ticker} not in watchlist")
    enabled = bool(payload.get("enabled"))
    stock.auto_trade_enabled = enabled
    db.commit()
    return {"ticker": ticker, "auto_trade_enabled": enabled}


@router.post("", response_model=WatchlistItem)
def add_to_watchlist(req: AddTickerRequest, db: Session = Depends(get_db)):
    ticker = req.ticker.upper().strip()
    existing = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"{ticker} is already in watchlist")

    info = get_ticker_info(ticker)
    if not info.get("name"):
        raise HTTPException(status_code=404, detail=f"Ticker {ticker} not found")

    stock = WatchlistStock(ticker=ticker, name=info.get("name", ticker))
    db.add(stock)
    db.commit()
    db.refresh(stock)

    try:
        live_quotes.ensure_symbols([ticker])
    except Exception:
        pass

    price_info = get_current_price(ticker)
    return WatchlistItem(
        ticker=stock.ticker,
        name=stock.name,
        added_at=stock.added_at,
        price=price_info[0] if price_info else None,
        change_pct=price_info[1] if price_info else None,
    )


@router.delete("/{ticker}")
def remove_from_watchlist(ticker: str, db: Session = Depends(get_db)):
    ticker = ticker.upper()
    stock = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
    if not stock:
        raise HTTPException(status_code=404, detail=f"{ticker} not in watchlist")
    db.delete(stock)
    db.commit()
    return {"message": f"{ticker} removed from watchlist"}
