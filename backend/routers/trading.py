"""
Paper-trading REST endpoints (Alpaca).

GET    /api/trading/account              -> {cash, equity, buying_power, ...}
GET    /api/trading/positions            -> [{symbol, qty, avg_entry_price, ...}]
GET    /api/trading/orders?status=open   -> [{id, symbol, side, status, ...}]
POST   /api/trading/order                -> submit a (bracket) order
DELETE /api/trading/orders/{id}          -> cancel a working order
POST   /api/trading/close/{symbol}       -> market-close a single position
POST   /api/trading/close-all            -> close every position + cancel orders
"""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from services import paper_trader, auto_trader
from routers._auth import require_api_key

# All trading endpoints require X-API-Key auth (when APP_API_KEY is set).
# Attached at router-level so every GET/POST/DELETE inherits it. GETs are
# included because /account and /positions leak balances and open positions —
# equivalent to exfiltrating trading data.
router = APIRouter(
    prefix="/api/trading",
    tags=["trading"],
    dependencies=[Depends(require_api_key)],
)


class AutoTraderConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    confidence_threshold: Optional[float] = Field(None, ge=0, le=100)
    max_pct_of_equity: Optional[float] = Field(None, gt=0, le=1)
    stock_pct_of_equity: Optional[float] = Field(None, ge=0, le=1)
    option_pct_of_equity: Optional[float] = Field(None, ge=0, le=1)
    max_risk_per_trade_pct: Optional[float] = Field(None, gt=0, le=0.1)
    trade_options: Optional[bool] = None
    trade_calls: Optional[bool] = None
    aggressive_options_mode: Optional[bool] = None
    entry_order_type: Optional[str] = Field(None, pattern="^(market|limit_at_mid)$")
    use_universe_scanner: Optional[bool] = None
    universe_top_n: Optional[int] = Field(None, ge=5, le=200)
    ticker_blacklist: Optional[str] = Field(None, max_length=500)
    daily_loss_limit_pct: Optional[float] = Field(None, ge=0, le=0.5)
    max_concurrent_positions: Optional[int] = Field(None, ge=0, le=100)
    max_per_sector: Optional[int] = Field(None, ge=0, le=50)
    flatten_by_eod: Optional[bool] = None
    signal_timeframes: Optional[str] = None
    stop_atr_mult: Optional[float] = Field(None, gt=0, le=10)
    chandelier_atr_mult: Optional[float] = Field(None, ge=0, le=10)
    dry_run: Optional[bool] = None
    ml_scoring_enabled: Optional[bool] = None


class KillSwitchRequest(BaseModel):
    reason: Optional[str] = None
    flatten: bool = True  # also close all open positions
    cancel_orders: bool = True


class UnkillRequest(BaseModel):
    reason: Optional[str] = None


class OrderRequest(BaseModel):
    symbol: str
    qty: float = Field(..., gt=0)
    side: str = Field(..., pattern="^(buy|sell|BUY|SELL)$")
    entry_type: str = Field("market", pattern="^(market|limit|MARKET|LIMIT)$")
    limit_price: Optional[float] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    time_in_force: str = Field("day", pattern="^(day|gtc|opg|ioc|DAY|GTC|OPG|IOC)$")
    # Algo Trader Plus: allow pre-market (4-9:30 ET) + after-hours (16-20 ET)
    # fills. Alpaca only honours this on DAY-TIF LIMIT orders — silently
    # downgrades otherwise. Default "auto" flips true when market is closed
    # AND it's a limit order.
    extended_hours: Optional[str] = Field("auto", pattern="^(auto|true|false|on|off|AUTO|TRUE|FALSE|ON|OFF)$")


@router.get("/account")
def account():
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured (APCA env vars missing)")
    a = paper_trader.get_account()
    if not a:
        raise HTTPException(status_code=502, detail="Could not fetch account from Alpaca")
    return a


@router.get("/positions")
def positions():
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured")
    return paper_trader.get_positions()


@router.get("/orders")
def orders(status: str = "all", limit: int = 50):
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured")
    return paper_trader.get_orders(status=status, limit=limit)


@router.post("/order")
def submit_order(req: OrderRequest):
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured")
    # Resolve extended_hours (auto = true only during pre/post-market and
    # only on DAY-TIF limit orders — paper_trader does the final legality
    # check too).
    _eh_in = (req.extended_hours or "auto").lower()
    if _eh_in in ("true", "on"):
        extended_hours = True
    elif _eh_in in ("false", "off"):
        extended_hours = False
    else:  # auto
        extended_hours = (
            req.entry_type.lower() == "limit"
            and req.time_in_force.lower() == "day"
            and not paper_trader.is_market_open()
        )

    res = paper_trader.submit_bracket_order(
        symbol=req.symbol,
        qty=req.qty,
        side=req.side,
        entry_type=req.entry_type,
        limit_price=req.limit_price,
        take_profit=req.take_profit,
        stop_loss=req.stop_loss,
        time_in_force=req.time_in_force,
        extended_hours=extended_hours,
    )
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


_ORDER_ID_RE = __import__("re").compile(r"^[a-fA-F0-9\-]{8,64}$")


@router.delete("/orders/{order_id}")
def cancel_order(order_id: str):
    # Defence-in-depth: Alpaca order IDs are UUIDs, but the SDK has been known to
    # accept arbitrary strings unchanged. A simple regex gate kills any chance
    # of injection-y characters reaching the broker layer.
    if not _ORDER_ID_RE.match(order_id):
        raise HTTPException(status_code=400, detail="Invalid order ID format")
    res = paper_trader.cancel_order(order_id)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


_TICKER_RE = __import__("re").compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")


@router.post("/orders/cancel-all")
def cancel_all_orders(symbol: Optional[str] = None):
    """Cancel every OPEN order on Alpaca, optionally filtered to one ticker.

    Wipes bracket TP/SL legs left dangling from old runs so the blotter stops
    showing stale buy/sell entries. Does NOT close filled positions — use
    /close/{symbol} or /close-all for that.
    """
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured")
    if symbol is not None and not _TICKER_RE.match(symbol):
        raise HTTPException(status_code=400, detail="Invalid ticker")
    res = paper_trader.cancel_all_orders(symbol=symbol)
    if "error" in res:
        raise HTTPException(status_code=502, detail=res["error"])
    return res


@router.post("/close/{symbol}")
def close_position(symbol: str):
    res = paper_trader.close_position(symbol)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/close-all")
def close_all():
    res = paper_trader.close_all_positions()
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


# -------- Auto-trader --------

@router.get("/auto/status")
def auto_status():
    return auto_trader.status_snapshot()


@router.post("/auto/config")
def auto_config(req: AutoTraderConfigRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    return auto_trader.update_config(**updates)


@router.get("/auto/trades")
def auto_trades(limit: int = 50):
    return auto_trader.list_trades(limit=limit)


@router.get("/auto/calibration")
def auto_calibration(min_bucket_n: int = 5):
    """Confidence-bucket calibration — observed win-rate and the risk
    multiplier being applied to each bucket. Closes the loop from the
    nightly job."""
    return auto_trader.compute_confidence_calibration(min_bucket_n=min_bucket_n)


@router.get("/auto/strategy-scorecard")
def auto_strategy_scorecard(days: int = 60, min_trades: int = 5):
    """Per-strategy realized P&L from live trades (profit-audit #8).
    Shows which strategies are carrying the book and which are dragging."""
    return auto_trader.strategy_scorecard(days=days, min_trades=min_trades)


@router.get("/auto/candidate-pool")
def auto_candidate_pool(limit: int = 50):
    """Current universe-scanner pool: top-N tickers ranked by composite score.
    Auto-trader treats this as an extension of the watchlist when
    use_universe_scanner=True."""
    from database import SessionLocal, CandidatePool
    db = SessionLocal()
    try:
        rows = (
            db.query(CandidatePool)
            .order_by(CandidatePool.score.desc())
            .limit(limit).all()
        )
        return [{
            "ticker": r.ticker, "name": r.name,
            "score": r.score, "price": r.price,
            "rvol": r.rvol, "rs_20d": r.rs_20d, "rs_60d": r.rs_60d,
            "adx": r.adx, "pct_from_52w_high": r.pct_from_52w_high,
            "reason": r.reason,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        } for r in rows]
    finally:
        db.close()


@router.post("/auto/universe-scan")
def auto_universe_scan():
    """Manually trigger the universe scanner (for operator testing / warm-up)."""
    from services import universe_scanner as _us
    return _us.run_scan()


@router.post("/auto/manage-now")
def auto_manage_now():
    """Manually trigger the trail/reconcile pass — mostly for testing."""
    return auto_trader.manage_open_positions()


@router.post("/auto/postmortem/{trade_id}")
def auto_regen_postmortem(trade_id: int):
    """Re-run the loss post-mortem for a closed trade (useful after editing rules)."""
    res = auto_trader.regenerate_post_mortem(trade_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Trade not found, not closed at a stop, or insufficient data")
    return res


# -------- Kill switch --------

@router.post("/kill")
def kill_switch(req: KillSwitchRequest):
    """
    Emergency halt — disables auto-trader AND (by default) flattens every
    open position + cancels every working order. The kill state is persisted
    in AutoTraderConfig so a process restart does NOT silently re-arm.

    Response shape:
      {"killed": true, "flattened": [...], "cancelled": N, "reason": "..."}
    """
    res = auto_trader.kill(reason=req.reason, flatten=req.flatten, cancel_orders=req.cancel_orders)
    return res


@router.post("/unkill")
def unkill_switch(req: UnkillRequest):
    """
    Clear the kill flag so the auto-trader can be re-enabled. Does NOT set
    enabled=True by itself — that's a separate deliberate step via
    /auto/config so re-arming is a two-step process.
    """
    return auto_trader.unkill(reason=req.reason)
