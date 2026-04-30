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
import logging
from typing import Optional, List

from models import PositionResponse, PnLReconciliationResponse

logger = logging.getLogger(__name__)
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
    pdt_enforce: Optional[bool] = None
    auto_promote_adopted: Optional[bool] = None
    # r57 schema-drift fix: these AutoTraderConfig fields existed in the DB
    # since r53-r55 but were never added to the request schema, so POST
    # /auto/config silently dropped them. Operator can now actually toggle.
    entry_1m_gate_mode: Optional[str] = Field(None, pattern="^(strict|relaxed|off)$")
    rr_min: Optional[float] = Field(None, ge=0, le=10)
    loss_pattern_mode: Optional[str] = Field(None, pattern="^(off|shadow|active)$")
    source_mute_enabled: Optional[bool] = None
    theta_adjusted_rr_enabled: Optional[bool] = None
    portfolio_kelly_enabled: Optional[bool] = None
    vol_target_annual: Optional[float] = Field(None, ge=0, le=2)
    leverage_cap: Optional[float] = Field(None, ge=0, le=10)
    book_var_99_cap_pct: Optional[float] = Field(None, ge=0, le=1)
    bracket_tif: Optional[str] = Field(None, pattern="^(day|gtc|DAY|GTC)$")
    max_correlated_open: Optional[int] = Field(None, ge=0, le=50)
    # r58: option floor configs (previously hardcoded)
    option_thesis_min_conf_aggressive: Optional[float] = Field(None, ge=0, le=100)
    option_thesis_min_conf_mult: Optional[float] = Field(None, ge=0, le=2)
    option_contract_min_score: Optional[float] = Field(None, ge=0, le=200)
    option_contract_min_score_aggressive: Optional[float] = Field(None, ge=0, le=200)


class KillSwitchRequest(BaseModel):
    reason: Optional[str] = None
    flatten: bool = True  # also close all open positions
    cancel_orders: bool = True


class UnkillRequest(BaseModel):
    reason: Optional[str] = None


class MoveStopRequest(BaseModel):
    """r53f: explicit move-stop endpoint. Replaces the prior pattern of
    POSTing `{action: "move_stop_be", new_stop: X}` to /api/trading/order
    which silently failed because OrderRequest's schema rejected the
    extra fields."""
    symbol: str
    new_stop: float = Field(..., gt=0)


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


@router.get("/equity-curve")
def equity_curve(lookback_days: int = 30):
    """r46 Tier 1 observability: persisted equity timeseries with derived
    drawdown_pct (vs rolling max). Populated by `record_equity_snapshot`
    every 5min during RTH. SPY-relative overlay included for benchmark.
    """
    from database import EquitySnapshot
    from datetime import datetime as _dt_ec, timedelta as _td_ec
    from database import SessionLocal as _SL_ec
    db = _SL_ec()
    try:
        since = _dt_ec.utcnow() - _td_ec(days=int(lookback_days))
        rows = (
            db.query(EquitySnapshot)
            .filter(EquitySnapshot.ts >= since)
            .order_by(EquitySnapshot.ts.asc())
            .all()
        )
        out = []
        peak = 0.0
        spy_anchor = None
        for r in rows:
            peak = max(peak, float(r.equity or 0))
            dd = ((peak - float(r.equity or 0)) / peak * 100.0) if peak > 0 else 0.0
            if spy_anchor is None and r.spy_close:
                spy_anchor = float(r.spy_close)
            spy_rel = (float(r.spy_close) / spy_anchor * float(rows[0].equity or 1) if (r.spy_close and spy_anchor) else None)
            out.append({
                "ts": r.ts.isoformat() + "Z",
                "equity": float(r.equity or 0),
                "cash": float(r.cash) if r.cash is not None else None,
                "buying_power": float(r.buying_power) if r.buying_power is not None else None,
                "realized_pl_today": float(r.realized_pl_today) if r.realized_pl_today is not None else None,
                "unrealized_pl": float(r.unrealized_pl) if r.unrealized_pl is not None else None,
                "open_positions": int(r.open_positions) if r.open_positions is not None else None,
                "drawdown_pct": round(dd, 3),
                "spy_close": float(r.spy_close) if r.spy_close else None,
                "spy_relative_equity": round(spy_rel, 2) if spy_rel else None,
            })
        return {"lookback_days": lookback_days, "n_snapshots": len(out), "snapshots": out}
    finally:
        db.close()


@router.get("/account")
def account():
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured (APCA env vars missing)")
    a = paper_trader.get_account()
    if not a:
        raise HTTPException(status_code=502, detail="Could not fetch account from Alpaca")
    return a


@router.get("/pnl-reconciliation", response_model=PnLReconciliationResponse)
def pnl_reconciliation():
    """One-stop P/L accounting: shows where every dollar of equity drift
    came from. Alpaca account equity is the truth; the bot's per-trade
    realized_pl is the bot's view; the difference is reconciliation gap
    (typically Alpaca-side closes the bot didn't observe — adopted /
    external / unmatched bracket fills).

    r52e: ops needed a single panel to answer "where did my $20k go?"
    instead of cross-referencing the position cards, the closed-trades
    ledger, and Alpaca's portfolio history.
    """
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured")
    import os
    import requests
    a = paper_trader.get_account()
    if not a:
        raise HTTPException(status_code=502, detail="Could not fetch account")
    equity = float(a.get("equity") or 0)
    last_equity = float(a.get("last_equity") or 0)

    # Starting equity from Alpaca portfolio_history.base_value (the
    # account's lifetime starting point — usually $100k for paper).
    base_value = None
    try:
        key = os.getenv("APCA_API_KEY_ID")
        sec = os.getenv("APCA_API_SECRET_KEY")
        if key and sec:
            r = requests.get(
                "https://paper-api.alpaca.markets/v2/account/portfolio/history?period=1A&timeframe=1D",
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
                timeout=8,
            )
            if r.ok:
                ph = r.json()
                base_value = float(ph.get("base_value") or 0) or None
    except Exception:
        pass
    if not base_value:
        base_value = 100000.0  # paper default

    total_drift = equity - base_value
    today_drift = equity - last_equity if last_equity else None

    # Bot DB: realized + unrealized
    from database import SessionLocal as _SL_pnl, AutoTrade as _AT_pnl
    db = _SL_pnl()
    try:
        closed = (db.query(_AT_pnl)
                  .filter(_AT_pnl.closed_at.isnot(None))
                  .all())
        realized_total = sum(float(r.realized_pl or 0) for r in closed)
        from collections import Counter
        by_status = Counter(r.status for r in closed)
        realized_by_status = {}
        for st, _ in by_status.items():
            realized_by_status[st] = {
                "count": int(by_status[st]),
                "pl": round(sum(float(r.realized_pl or 0) for r in closed if r.status == st), 2),
            }
        # Top losers / winners
        ranked = sorted(closed, key=lambda r: float(r.realized_pl or 0))
        def _row(r):
            return {
                "id": r.id, "ticker": r.ticker, "asset_type": r.asset_type,
                "symbol": r.symbol, "status": r.status,
                "realized_pl": round(float(r.realized_pl or 0), 2),
                "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            }
        top_losers = [_row(r) for r in ranked[:5] if float(r.realized_pl or 0) < 0]
        top_winners = [_row(r) for r in reversed(ranked[-5:]) if float(r.realized_pl or 0) > 0]
    finally:
        db.close()

    positions = paper_trader.get_positions() or []
    unrealized_total = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    unrealized_stocks = sum(
        float(p.get("unrealized_pl") or 0)
        for p in positions
        if "OPTION" not in (p.get("asset_class") or "").upper()
    )
    unrealized_options = sum(
        float(p.get("unrealized_pl") or 0)
        for p in positions
        if "OPTION" in (p.get("asset_class") or "").upper()
    )

    # Reconciliation gap: account drift NOT explained by bot's realized +
    # currently-open unrealized. Caused by Alpaca-side closes the bot
    # didn't capture (closed_reconciled / closed_external / pre-adoption
    # P/L on positions that came in via sync-positions).
    gap = total_drift - realized_total - unrealized_total

    return {
        "starting_equity": round(base_value, 2),
        "current_equity": round(equity, 2),
        "total_drift": round(total_drift, 2),
        "today_drift": round(today_drift, 2) if today_drift is not None else None,
        "realized_total": round(realized_total, 2),
        "realized_by_status": realized_by_status,
        "unrealized_total": round(unrealized_total, 2),
        "unrealized_stocks": round(unrealized_stocks, 2),
        "unrealized_options": round(unrealized_options, 2),
        "reconciliation_gap": round(gap, 2),
        "n_closed": len(closed),
        "n_open": len(positions),
        "top_losers": top_losers,
        "top_winners": top_winners,
    }


@router.get("/positions", response_model=List[PositionResponse])
def positions():
    """Broker positions enriched with bot-managed exit fields (current_stop,
    target1/2/3, stop_loss, opened_at, level_index, hit_t1, asset_type,
    trade_id) by joining each Alpaca position to the latest open/adopted
    AutoTrade row. r52 fix: prior code returned only Alpaca's native
    fields, so the UI position cards rendered blank Stop / Targets cells
    even though the bot tracked them."""
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured")
    rows = paper_trader.get_positions() or []
    if not rows:
        return rows
    from database import SessionLocal as _SL_pos, AutoTrade as _AT_pos
    db = _SL_pos()
    try:
        # Build a map: alpaca-symbol → AutoTrade row. Stocks: position.symbol
        # matches AutoTrade.ticker. Options: position.symbol is the OCC
        # symbol matching AutoTrade.symbol.
        syms = {(r.get("symbol") or "").upper() for r in rows}
        ats = (db.query(_AT_pos)
               .filter(_AT_pos.status.in_(["open", "adopted", "pending"]))
               .filter((_AT_pos.ticker.in_(syms)) | (_AT_pos.symbol.in_(syms)))
               .order_by(_AT_pos.opened_at.desc())
               .all())
        # Prefer the most recent row when multiple match (e.g. adopted +
        # error duplicates from prior FORCE_CLOSE_FAILED).
        by_key: dict = {}
        for a in ats:
            for k in {(a.ticker or "").upper(), (a.symbol or "").upper()}:
                if k and k not in by_key:
                    by_key[k] = a
        # r52d: for option positions, also surface the UNDERLYING ticker +
        # current spot, so the UI can compute distance-to-stop / R against
        # the underlying instead of the option premium (mixed-units bug).
        from services.data_fetcher import get_current_price as _cp_pos
        out = []
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            a = by_key.get(sym)
            if a:
                r = {
                    **r,
                    "trade_id": a.id,
                    "asset_type": a.asset_type,
                    "ticker": a.ticker,
                    "current_stop": float(a.current_stop) if a.current_stop is not None else None,
                    "stop_loss": float(a.stop_loss) if a.stop_loss is not None else None,
                    "target1": float(a.target1) if a.target1 is not None else None,
                    "target2": float(a.target2) if a.target2 is not None else None,
                    "target3": float(a.target3) if a.target3 is not None else None,
                    "level_index": a.level_index,
                    "hit_t1": a.hit_t1,
                    "opened_at": a.opened_at.isoformat() if a.opened_at else None,
                    "managed_status": a.status,
                }
                if (a.asset_type or "").lower() == "option" and a.ticker:
                    try:
                        cp = _cp_pos(a.ticker)
                        if cp and cp[0]:
                            r["underlying_symbol"] = a.ticker
                            r["underlying_price"] = float(cp[0])
                            try:
                                r["underlying_entry_price"] = float(getattr(a, "underlying_entry_price", None) or 0) or None
                            except Exception:
                                pass
                    except Exception:
                        pass
            out.append(r)
        return out
    finally:
        db.close()


@router.get("/orders")
def orders(status: str = "all", limit: int = 50):
    """r53e: enriched with `notional_usd` (cost-to-buy / size-of-sell)
    and `pl_usd` (realized P/L on filled sells, expected P/L on working
    sells when the entry side can be matched). For BUYs, notional uses
    the actual fill price when filled, else the limit/stop price as an
    estimate, else null. For SELLs, P/L is computed against the matching
    AutoTrade row's entry_price (for bot-managed trades) or against the
    current open position's avg_entry_price (for adopted/manual rows).
    """
    if not paper_trader.is_enabled():
        raise HTTPException(status_code=503, detail="Paper trading not configured")
    raw = paper_trader.get_orders(status=status, limit=limit) or []
    if not raw:
        return raw

    # Build a (symbol → entry_price) lookup from open + recently-closed
    # AutoTrade rows. For options, key on the OCC symbol. For stocks,
    # key on the ticker.
    from database import SessionLocal as _SL_o, AutoTrade as _AT_o
    from datetime import datetime as _dt_o, timedelta as _td_o
    db = _SL_o()
    entry_by_key: dict = {}
    asset_type_by_key: dict = {}
    try:
        # Open / adopted rows take priority (most recent fill).
        cutoff = _dt_o.utcnow() - _td_o(days=14)
        rows = (db.query(_AT_o)
                .filter((_AT_o.status.in_(["open", "adopted", "pending"]))
                        | (_AT_o.closed_at >= cutoff))
                .order_by(_AT_o.opened_at.desc())
                .all())
        for r in rows:
            for k in {(r.ticker or "").upper(), (r.symbol or "").upper()}:
                if k and k not in entry_by_key and r.entry_price:
                    entry_by_key[k] = float(r.entry_price)
                    asset_type_by_key[k] = (r.asset_type or "stock").lower()
    finally:
        db.close()

    # Fallback: open Alpaca positions cover adopted positions where the
    # AutoTrade row has no entry_price.
    try:
        for p in (paper_trader.get_positions() or []):
            sym = (p.get("symbol") or "").upper()
            if sym and sym not in entry_by_key and p.get("avg_entry_price"):
                entry_by_key[sym] = float(p["avg_entry_price"])
                ac = (p.get("asset_class") or "").upper()
                asset_type_by_key[sym] = "option" if "OPTION" in ac else "stock"
    except Exception:
        pass

    out = []
    for o in raw:
        sym = (o.get("symbol") or "").upper()
        side_raw = (o.get("side") or "").lower().split(".")[-1]
        is_buy = "buy" in side_raw
        is_sell = "sell" in side_raw
        atype = asset_type_by_key.get(sym, "stock")
        multiplier = 100.0 if atype == "option" else 1.0

        # Choose effective fill / working price for notional math.
        fill_px = o.get("filled_avg_price")
        limit_px = o.get("limit_price")
        stop_px = o.get("stop_price")
        eff_px = None
        for cand in (fill_px, limit_px, stop_px):
            if cand is not None:
                try:
                    eff_px = float(cand)
                    break
                except Exception:
                    continue
        # Quantity: prefer filled_qty when populated, else qty.
        qty_n = None
        try:
            qty_n = float(o.get("filled_qty") or o.get("qty") or 0)
        except Exception:
            qty_n = None

        notional = None
        if eff_px is not None and qty_n and qty_n > 0:
            notional = round(eff_px * qty_n * multiplier, 2)

        pl = None
        pl_basis = None  # the entry price we used
        if is_sell and eff_px is not None and qty_n and qty_n > 0:
            entry_lookup = entry_by_key.get(sym)
            if entry_lookup and entry_lookup > 0:
                pl = round((eff_px - entry_lookup) * qty_n * multiplier, 2)
                pl_basis = round(entry_lookup, 4)

        out.append({
            **o,
            "notional_usd": notional,
            "pl_usd": pl,
            "pl_basis_entry": pl_basis,
        })
    return out


@router.post("/move-stop")
def move_stop(req: MoveStopRequest):
    """r53f: move the trailing-stop on a bot-managed open position to a
    new level. For stocks: also replaces the broker-side STOP order so
    the protection actually moves. For options: updates the underlying
    stop tracked by the manage tick (no broker-side leg for options).

    Validates the move is in the protective direction (long → up only,
    short → down only) so a typo can't widen the stop. Operator should
    use POST /api/admin/* paths if they really need to widen.
    """
    sym = (req.symbol or "").upper()
    if not _TICKER_RE.match(sym) and not (len(sym) >= 13 and sym[:6].rstrip("0123456789").isalpha()):
        # Allow either ticker or OCC option symbol.
        raise HTTPException(status_code=400, detail="Invalid symbol")
    from database import SessionLocal as _SL_ms, AutoTrade as _AT_ms
    db = _SL_ms()
    try:
        # Match by ticker for stocks, by symbol (OCC) for options.
        row = (db.query(_AT_ms)
               .filter(_AT_ms.status.in_(["open", "adopted"]))
               .filter((_AT_ms.ticker == sym) | (_AT_ms.symbol == sym))
               .order_by(_AT_ms.opened_at.desc())
               .first())
        if not row:
            raise HTTPException(status_code=404, detail=f"No open trade for {sym}")
        new_stop = float(req.new_stop)
        cur_stop = float(row.current_stop or 0)
        side = (row.side or "buy").lower()
        is_long = "buy" in side
        # Direction check — long stops move up only; short stops move down only.
        if cur_stop > 0:
            if is_long and new_stop < cur_stop:
                raise HTTPException(
                    status_code=400,
                    detail=f"Refusing to widen long stop {cur_stop} → {new_stop} (use admin endpoint)",
                )
            if (not is_long) and new_stop > cur_stop:
                raise HTTPException(
                    status_code=400,
                    detail=f"Refusing to widen short stop {cur_stop} → {new_stop} (use admin endpoint)",
                )
        prev_stop = cur_stop
        row.current_stop = round(new_stop, 4)
        row.note = (row.note or "") + f" | MOVE_STOP: {prev_stop:.2f} → {new_stop:.2f} (operator)"
        db.commit()

        # Stock side: replace the broker SL order. Options are managed
        # via underlying-stop in the manage tick; nothing to update at
        # the broker.
        broker_result = None
        if (row.asset_type or "stock").lower() == "stock" and row.stop_order_id:
            try:
                from alpaca.trading.requests import ReplaceOrderRequest
                c = paper_trader._get_client()
                if c:
                    replaced = c.replace_order_by_id(
                        row.stop_order_id,
                        order_data=ReplaceOrderRequest(stop_price=round(new_stop, 2)),
                    )
                    broker_result = {"replaced_id": str(getattr(replaced, "id", row.stop_order_id))}
                    if replaced and getattr(replaced, "id", None):
                        row.stop_order_id = str(replaced.id)
                        db.commit()
            except Exception as e:
                logger.warning(f"move_stop {sym}: broker replace failed: {e}; submitting fresh STOP")
                # Fallback: submit a new STOP order at the new level
                try:
                    from alpaca.trading.requests import StopOrderRequest
                    from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                    c = paper_trader._get_client()
                    if c and row.qty:
                        # Cancel any working stops first
                        try:
                            paper_trader.cancel_all_orders(symbol=sym)
                        except Exception:
                            pass
                        new_o = c.submit_order(order_data=StopOrderRequest(
                            symbol=sym, qty=int(row.qty),
                            side=_OS.SELL if is_long else _OS.BUY,
                            time_in_force=_TIF.GTC,
                            stop_price=round(new_stop, 2),
                        ))
                        if new_o and getattr(new_o, "id", None):
                            row.stop_order_id = str(new_o.id)
                            db.commit()
                            broker_result = {"resubmitted_id": str(new_o.id)}
                except Exception as e2:
                    broker_result = {"error": f"replace + resubmit both failed: {e2}"}
        return {
            "ok": True,
            "trade_id": row.id,
            "symbol": sym,
            "asset_type": row.asset_type,
            "previous_stop": prev_stop,
            "new_stop": new_stop,
            "broker": broker_result or {"note": "no broker SL leg to update"},
        }
    finally:
        db.close()


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


_OCC_RE = __import__("re").compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


@router.post("/close/{symbol}")
def close_position(symbol: str):
    """Close a position; if the bot has an open AutoTrade row for this
    ticker, route through `force_close_trade` so the row + BP reservation
    + touch-count are properly reconciled. r47 fix #T0b-1: prior code only
    called paper_trader.close_position(), leaving the AutoTrade row stuck
    in `open` state — manage tick then detected the missing SL and entered
    a resubmit storm.

    r53g fix: accepts OCC option symbols (e.g.
    "BKR260515C00070000") in addition to regular tickers. Prior code
    rejected option closes with "Invalid ticker" because the ticker
    regex caps at 10 chars / no digits, while OCC symbols are 18-21
    chars. For OCC inputs, the AutoTrade query matches on `symbol`
    (the OCC) instead of `ticker` (the underlying root) so we close
    only the specific contract — not every option on the same root.
    """
    sym_u = (symbol or "").upper()
    is_occ = bool(_OCC_RE.match(sym_u))
    if not is_occ and not _TICKER_RE.match(symbol):
        raise HTTPException(status_code=400, detail="Invalid ticker or OCC option symbol")
    from database import SessionLocal, AutoTrade
    from services.execution_engine import force_close_trade
    db = SessionLocal()
    try:
        if is_occ:
            # Option close: match by exact OCC `symbol`, not by underlying.
            q = db.query(AutoTrade).filter(
                AutoTrade.symbol == sym_u,
                AutoTrade.status.in_(["pending", "open", "adopted"]),
            )
        else:
            q = db.query(AutoTrade).filter(
                AutoTrade.ticker == sym_u,
                AutoTrade.status.in_(["pending", "open", "adopted"]),
            )
        rows = q.all()
        if rows:
            summary: dict = {}
            for t in rows:
                try:
                    force_close_trade(t, db, "manual close via /close", summary,
                                      status_override="closed_manual")
                except Exception as e:
                    raise HTTPException(status_code=502, detail=f"force_close failed: {e}")
            return {"status": "closed", "count": len(rows), "summary": summary}
        # No bot-managed row: fall back to broker-only close.
        res = paper_trader.close_position(sym_u)
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        return res
    finally:
        db.close()


@router.post("/close-all")
def close_all():
    """Close every open position; reconciles AutoTrade rows for bot-managed
    positions. r47 fix #T0b-1: prior code only called paper_trader."""
    from database import SessionLocal, AutoTrade
    from services.execution_engine import force_close_trade
    db = SessionLocal()
    try:
        rows = db.query(AutoTrade).filter(
            AutoTrade.status.in_(["pending", "open", "adopted"]),
        ).all()
        managed_tickers = {r.ticker for r in rows}
        summary: dict = {}
        closed = 0
        for t in rows:
            try:
                force_close_trade(t, db, "manual close-all", summary,
                                  status_override="closed_manual")
                closed += 1
            except Exception as e:
                summary.setdefault("errors", []).append(f"{t.ticker}: {e}")
        # Catch any non-bot positions that exist at the broker (manual /
        # adopted-untracked) — flatten them too.
        try:
            res = paper_trader.close_all_positions()
            if isinstance(res, dict) and res.get("error"):
                summary.setdefault("broker_errors", []).append(res["error"])
        except Exception as e:
            summary.setdefault("broker_errors", []).append(str(e))
        return {"status": "ok", "closed_managed": closed,
                "managed_tickers": sorted(managed_tickers), "summary": summary}
    finally:
        db.close()


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


@router.get("/auto/skip-counts")
def auto_skip_counts():
    """r42 fix #1.25: counter snapshot for the UI's "rejected signals" view.
    Pairs `autotrade_skip{reason}` with `autotrade_event{event}` so the
    operator can see at a glance why the bot is sitting idle."""
    from services import metrics as _m
    return {
        "skips": _m.autotrade_skip_counts(),
        "events": _m.autotrade_event_counts(),
    }


@router.get("/auto/pdt")
def auto_pdt():
    """PDT (Pattern Day Trader) day-trade counter for the trailing 5
    business days. On Alpaca paper this is informational; on live margin
    accounts < $25k, 4+ day-trades in 5 days blocks new opens for 90d.
    Day-trade definition: open + close of same security same calendar day.
    """
    from services.risk_manager import pdt_day_trade_count
    return pdt_day_trade_count(window_business_days=5)


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
            # r53l: per-candidate scan verdict
            "last_evaluated_at": r.last_evaluated_at.isoformat() if r.last_evaluated_at else None,
            "last_stock_decision": r.last_stock_decision,
            "last_stock_reason": r.last_stock_reason,
            "last_option_decision": r.last_option_decision,
            "last_option_reason": r.last_option_reason,
            # r54: pool generation, source attribution, v2 score
            "generation": getattr(r, "generation", None),
            "pool_source": getattr(r, "pool_source", "breakout"),
            "score_v2": getattr(r, "score_v2", None),
        } for r in rows]
    finally:
        db.close()


@router.post("/auto/universe-scan")
def auto_universe_scan():
    """Manually trigger the universe scanner (for operator testing / warm-up)."""
    from services import scanner as _sc
    return _sc.run_scan()


@router.get("/auto/candidate-events")
def auto_candidate_events(max_age_min: int = 30):
    """r57: list active (non-expired, non-consumed) candidate events.
    Surfaces the event-driven path to operator UI; without this the
    detect_events output is invisible."""
    from services import scanner as _sc
    rows = _sc.get_active_events(max_age_min=max_age_min)
    return [{
        "id": r["id"],
        "kind": r["kind"],
        "ticker": r["ticker"],
        "score": r["score"],
        "event_at": r["event_at"].isoformat() if r["event_at"] else None,
        "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
        "features": r["features"],
    } for r in rows]


@router.get("/auto/candidate-events-recent")
def auto_candidate_events_recent(limit: int = 50):
    """r57: recently-consumed events (entered, skipped, errored) — for
    operator post-mortem of the event path."""
    from database import SessionLocal, CandidateEvent
    db = SessionLocal()
    try:
        rows = (
            db.query(CandidateEvent)
            .filter(CandidateEvent.consumed_at.isnot(None))
            .order_by(CandidateEvent.consumed_at.desc())
            .limit(limit)
            .all()
        )
        return [{
            "id": r.id,
            "kind": r.kind,
            "ticker": r.ticker,
            "score": r.score,
            "event_at": r.event_at.isoformat() if r.event_at else None,
            "consumed_at": r.consumed_at.isoformat() if r.consumed_at else None,
            "consumed_decision": r.consumed_decision,
            "consumed_reason": r.consumed_reason,
        } for r in rows]
    finally:
        db.close()


@router.post("/auto/manage-now")
def auto_manage_now():
    """Manually trigger the trail/reconcile pass — mostly for testing."""
    return auto_trader.manage_open_positions()


@router.post("/auto/scan-now")
def auto_scan_now():
    """r53p: manually fire the full watchlist + candidate-pool scan
    (same code path as the 5-minute `scheduled_scan` cron). Runs
    consider_signal / consider_put_play / consider_call_play across
    every ticker. Operator-driven; cron continues independently.

    Returns the scheduled_scan summary (tickers scanned, entries opened,
    skip-counter deltas).
    """
    logger.warning("ADMIN /auto/scan-now invoked")
    import time as _t_sn
    from main import scheduled_scan as _ss, _app_health as _ah
    started = _t_sn.time()
    try:
        _ss()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"scan failed: {e}")
    elapsed = round(_t_sn.time() - started, 2)
    return {
        "ok": True,
        "elapsed_sec": elapsed,
        "last_scan_at": _ah.get("last_scan_at"),
    }


@router.get("/auto/schedule")
def auto_schedule():
    """r53p: report the scheduler's job inventory + next-fire times so
    operators can see when each cron next runs without shelling into
    the container.
    """
    try:
        from main import scheduler as _sched
        jobs = []
        for j in _sched.get_jobs():
            try:
                nxt = j.next_run_time
                jobs.append({
                    "id": j.id,
                    "name": j.name or j.id,
                    "next_run": nxt.isoformat() if nxt else None,
                    "trigger": str(j.trigger),
                    "max_instances": j.max_instances,
                    "coalesce": j.coalesce,
                })
            except Exception:
                pass
        # Sort by next_run for readability
        jobs.sort(key=lambda x: x.get("next_run") or "9")
        return {"jobs": jobs, "n": len(jobs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"schedule introspection failed: {e}")


@router.post("/auto/postmortem/{trade_id}")
def auto_regen_postmortem(trade_id: int):
    """Re-run the loss post-mortem for a closed trade (useful after editing rules)."""
    res = auto_trader.regenerate_post_mortem(trade_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Trade not found, not closed at a stop, or insufficient data")
    return res


@router.get("/auto/rationale/{trade_id}")
def auto_trade_rationale(trade_id: int):
    """Aggregated 'why was this trade made?' view.

    Pulls together:
      • Origin — was the ticker on the watchlist or surfaced by the scanner?
        (and the scanner score breakdown if so)
      • Signal — the originating Signal row's reasoning bullets, confidence,
        timeframe, strategy
      • Backtest — best_strategy_per_ticker row (winning strategy + OOS metrics)
      • Fundamentals — composite quality_score + headline ratios
      • Analyst rating — consensus + price target premium
      • Macro context — high-importance events within ±48h of opened_at

    Frontend renders this in a single expander on the trade card.
    """
    from database import (
        SessionLocal, AutoTrade, Signal, WatchlistStock, CandidatePool,
        BestStrategyPerTicker, Fundamentals, AnalystRating, MacroEvent,
    )
    from datetime import timedelta

    db = SessionLocal()
    try:
        t = db.query(AutoTrade).filter(AutoTrade.id == trade_id).first()
        if not t:
            raise HTTPException(status_code=404, detail=f"trade {trade_id} not found")

        ticker = t.ticker
        # --- Origin ---
        wl = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
        cand = db.query(CandidatePool).filter(CandidatePool.ticker == ticker).first()
        if wl and cand:
            origin = "watchlist+pool"
        elif wl:
            origin = "watchlist"
        elif cand:
            origin = "scanner"
        else:
            origin = "unknown"
        scanner = None
        if cand:
            scanner = {
                "score": cand.score, "rvol": cand.rvol,
                "rs_20d": cand.rs_20d, "rs_60d": cand.rs_60d,
                "adx": cand.adx, "pct_from_52w_high": cand.pct_from_52w_high,
                "reason": cand.reason, "price": cand.price,
                "generated_at": cand.generated_at.isoformat() if cand.generated_at else None,
            }

        # --- Signal ---
        signal = None
        if t.signal_id:
            sig = db.query(Signal).filter(Signal.id == t.signal_id).first()
            if sig:
                # reasoning is stored as a newline-separated string; split into bullets
                reasoning_lines: list = []
                if sig.reasoning:
                    reasoning_lines = [ln for ln in sig.reasoning.split("\n") if ln.strip()]
                signal = {
                    "signal_type": sig.signal_type,
                    "confidence": sig.confidence,
                    "timeframe": sig.timeframe,
                    "strategy": getattr(sig, "strategy", None),
                    "entry": sig.entry, "stop_loss": sig.stop_loss,
                    "target1": sig.target1, "target2": sig.target2, "target3": sig.target3,
                    "reasoning_lines": reasoning_lines,
                    "generated_at": sig.generated_at.isoformat() if sig.generated_at else None,
                }

        # --- Backtest evidence ---
        bs = db.query(BestStrategyPerTicker).filter(BestStrategyPerTicker.ticker == ticker).first()
        backtest = None
        if bs:
            backtest = {
                "winning_strategy": bs.strategy,
                "winning_direction": bs.direction,
                "confidence": bs.confidence,
                "oos_trades": bs.oos_trades,
                "win_rate": bs.win_rate,
                "avg_pl": bs.avg_pl,
                "updated_at": bs.updated_at.isoformat() if bs.updated_at else None,
            }

        # --- Fundamentals ---
        f = db.query(Fundamentals).filter(Fundamentals.ticker == ticker).first()
        fundamentals = None
        if f:
            fundamentals = {
                "quality_score": f.quality_score,
                "sector": f.sector, "industry": f.industry,
                "pe_ratio": f.pe_ratio, "peg_ratio": f.peg_ratio,
                "revenue_growth_yoy": f.revenue_growth_yoy,
                "earnings_growth_yoy": f.earnings_growth_yoy,
                "profit_margin": f.profit_margin,
                "return_on_equity": f.return_on_equity,
                "debt_to_equity": f.debt_to_equity,
                "current_ratio": f.current_ratio,
                "last_changed_at": f.last_changed_at.isoformat() if f.last_changed_at else None,
            }

        # --- Analyst rating ---
        ar = db.query(AnalystRating).filter(AnalystRating.ticker == ticker).first()
        analyst = None
        if ar:
            target_premium = None
            cur_px = t.entry_price or t.requested_entry
            if ar.target_mean and cur_px:
                target_premium = (ar.target_mean - cur_px) / cur_px
            analyst = {
                "mean": ar.mean, "key": ar.key, "analyst_count": ar.analyst_count,
                "target_mean": ar.target_mean,
                "target_high": ar.target_high, "target_low": ar.target_low,
                "target_premium_vs_entry": target_premium,
                "updated_at": ar.updated_at.isoformat() if ar.updated_at else None,
            }

        # --- Macro context (events within ±48h of opened_at) ---
        macro_events: list = []
        if t.opened_at:
            window_start = t.opened_at - timedelta(hours=48)
            window_end = t.opened_at + timedelta(hours=48)
            evs = (
                db.query(MacroEvent)
                .filter(
                    MacroEvent.release_time_utc >= window_start,
                    MacroEvent.release_time_utc <= window_end,
                    MacroEvent.importance.in_(["high", "medium"]),
                )
                .order_by(MacroEvent.release_time_utc.asc()).all()
            )
            for ev in evs:
                macro_events.append({
                    "event_key": ev.event_key,
                    "event_name": ev.event_name,
                    "importance": ev.importance,
                    "release_time_utc": ev.release_time_utc.isoformat() if ev.release_time_utc else None,
                    "consensus": ev.consensus,
                    "actual": ev.actual,
                    "surprise_pct": ev.surprise_pct,
                    "minutes_relative_to_open": (
                        int((ev.release_time_utc - t.opened_at).total_seconds() / 60)
                        if (ev.release_time_utc and t.opened_at) else None
                    ),
                })

        return {
            "trade_id": t.id, "ticker": ticker, "asset_type": t.asset_type,
            "side": t.side, "qty": t.qty,
            "entry_price": t.entry_price, "stop_loss": t.stop_loss,
            "target1": t.target1, "target2": t.target2, "target3": t.target3,
            "status": t.status, "note": t.note,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "origin": origin,
            "scanner": scanner,
            "signal": signal,
            "backtest": backtest,
            "fundamentals": fundamentals,
            "analyst": analyst,
            "macro_context": macro_events,
        }
    finally:
        db.close()


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
