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
    pdt_enforce: Optional[bool] = None
    auto_promote_adopted: Optional[bool] = None


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


@router.get("/positions")
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
    """Close a position; if the bot has an open AutoTrade row for this
    ticker, route through `force_close_trade` so the row + BP reservation
    + touch-count are properly reconciled. r47 fix #T0b-1: prior code only
    called paper_trader.close_position(), leaving the AutoTrade row stuck
    in `open` state — manage tick then detected the missing SL and entered
    a resubmit storm."""
    if not _TICKER_RE.match(symbol):
        raise HTTPException(status_code=400, detail="Invalid ticker")
    from database import SessionLocal, AutoTrade
    from services.execution_engine import force_close_trade
    db = SessionLocal()
    try:
        rows = db.query(AutoTrade).filter(
            AutoTrade.ticker == symbol.upper(),
            AutoTrade.status.in_(["pending", "open", "adopted"]),
        ).all()
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
        res = paper_trader.close_position(symbol)
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
