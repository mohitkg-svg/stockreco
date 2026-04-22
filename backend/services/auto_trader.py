"""
Automated trading on top of the signal engine + Alpaca paper account.

Rules (configurable in DB row AutoTraderConfig.id=1):
  • At most 50% of account equity is ever deployed.
  • Of equity: 40% may be in stock positions, 10% in options.
  • Every entry has a hard stop-loss (the bracket SL leg held by Alpaca).
  • Trailing exit (state machine, no profit-taking):
      - At T1 cross → stop moves to entry (break-even).
      - At T2 cross → stop moves to T1.
      - At T3 cross → stop moves to T2 AND we recompute the next 3 targets
        from the current price (using swing levels + ATR). The cycle repeats
        as long as the move continues. We never sell into strength — only
        a stop hit closes the trade.
  • Risk per trade is capped at `max_risk_per_trade_pct` of equity (default 2%).
  • Confidence threshold gate (default 75) — weak signals are ignored.
  • Only BUY signals are auto-traded for now (long-only). SELL signals close
    existing auto-trade longs but do not open shorts.

This service is invoked from two places:
  1. After every analysis run (`consider_signal`) — opens new positions.
  2. From a 60s scheduler job (`manage_open_positions`) — trails stops to
     break-even at T1 and reconciles status.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import desc
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

# Process-wide entry lock — serializes consider_signal / consider_put_play so
# the budget + sector-cap + idempotency checks can't race against a parallel
# entry on a *different* ticker (e.g. two tickers refreshed simultaneously,
# both seeing 2 open trades in the same sector and both opening a 3rd). The
# manage loop does NOT take this lock — exits are independent of caps.
_entry_lock = threading.Lock()

# Buying-power circuit breaker. Set to a future UTC datetime when Alpaca
# rejects an order for insufficient buying power — consider_signal then
# short-circuits until we're past this timestamp. Prevents the retry storm
# where every 15-min scan resubmits the same doomed orders for every
# qualifying ticker.
_bp_exhausted_until: Optional[datetime] = None

# Postmortem fix M1: local in-flight buying-power reservation. Alpaca's
# reported `buying_power` lags submitted bracket orders (pending TPs reserve
# BP that doesn't immediately show up as drawn). Without local bookkeeping,
# a watchlist scan can submit 30 orders against the same stale BP figure
# before the first 422 trips the circuit breaker. We add `qty * entry` to
# this counter at submit time and decay it on the next account refresh
# (the next manage tick or next consider_signal call) by re-reading
# Alpaca's BP — anything the broker has now drawn down is implicitly
# reflected, so we reset to zero when the gap closes.
_in_flight_bp_reserved: float = 0.0
_in_flight_bp_lock = threading.Lock()


def _reserve_bp(amount: float) -> None:
    global _in_flight_bp_reserved
    with _in_flight_bp_lock:
        _in_flight_bp_reserved = max(0.0, _in_flight_bp_reserved + float(amount))


def _release_bp(amount: float) -> None:
    global _in_flight_bp_reserved
    with _in_flight_bp_lock:
        _in_flight_bp_reserved = max(0.0, _in_flight_bp_reserved - float(amount))


def _get_in_flight_bp() -> float:
    with _in_flight_bp_lock:
        return _in_flight_bp_reserved


def _decay_in_flight_bp_if_stale() -> None:
    """Reset the in-flight reservation periodically — Alpaca's reported BP
    eventually reflects the submitted orders, at which point our local
    counter is double-counting. We zero it after _BP_RESERVATION_TTL_SEC.
    """
    global _in_flight_bp_reserved, _in_flight_bp_last_reset
    import time as _t
    now = _t.time()
    with _in_flight_bp_lock:
        if now - _in_flight_bp_last_reset > _BP_RESERVATION_TTL_SEC:
            _in_flight_bp_reserved = 0.0
            _in_flight_bp_last_reset = now


_BP_RESERVATION_TTL_SEC = 60.0  # Alpaca usually reflects submitted BP within 30-60s
import time as _t_mod
_in_flight_bp_last_reset: float = _t_mod.time()

# Per-trade consecutive-touch counters for the next price target. Required
# to suppress single-bar wick triggers — postmortems showed MRVL's T1 hit
# was a single 5m wick to 148.75 immediately followed by a print to 146.21,
# which moved the stop to BE and chopped the trade out for $0. We now
# require N>=2 consecutive manage-loop ticks above the target before
# trailing. Module-level (lost on restart, that's fine — it's a debounce).
_TARGET_CONFIRM_TICKS = 2
_target_touch_counts: Dict[int, int] = {}

# Slippage thresholds (multiples of daily ATR_14). Fills that drift up to
# ±0.3×ATR are normal market-order behaviour. ±0.3-1.0×ATR shifts targets
# proportionally so distances stay intact. >1.0×ATR is a runaway gap-up
# fill — flatten immediately rather than auto-trailing into a chop-out.
_SLIPPAGE_SHIFT_ATR = 0.3
_SLIPPAGE_REJECT_ATR = 1.0
# Below this T1-from-entry distance (in ATR), the break-even trail-on-T1
# rule is suppressed — T1 is too tight to be a meaningful profit lock and
# moving the stop there just chops us out on a normal pullback. We let the
# chandelier overlay (configured via cfg.chandelier_atr_mult) do the
# trailing instead.
_T1_BE_MIN_ATR = 0.5

# Profit-maximization tuning (strategy upgrade).
# Confidence-scaled risk: a signal well above threshold gets a larger
# position. Risk multiplier ramps linearly from 1.0x at the threshold to
# _MAX_CONFIDENCE_RISK_MULT at 100% confidence.
_MAX_CONFIDENCE_RISK_MULT = 1.75
# Backtest-win-rate-aware scaling: if this ticker's strategy has a >=55%
# historical hit rate, multiply the risk budget up to _KELLY_MAX_MULT.
_KELLY_MAX_MULT = 1.35
_KELLY_MIN_WIN_RATE = 55.0
# T2 partial profit-taking: after T1 trim, if price pushes through T2,
# trim half the remaining runner so 50% of the T2 qty banks the win.
_T2_PARTIAL_FRAC = 0.5
# Stale-trade exit: close an open trade that hasn't hit T1 after
# N × timeframe minutes have elapsed. Frees capital for fresher setups.
_STALE_TRADE_TF_MULT = 8

# Background thread pool for non-blocking post-mortems. Sized small — these
# are infrequent, and we don't want to fan out a hundred analyses if many
# trades close at once.
_post_mortem_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="post-mortem")


def _post_mortem_async(trade_id: int) -> None:
    """Re-fetch the trade by id in a fresh session and run the analysis off-loop."""
    def _job():
        from database import SessionLocal as _SL, AutoTrade as _AT
        s = _SL()
        try:
            row = s.query(_AT).filter(_AT.id == trade_id).first()
            if row:
                post_mortem_svc.analyze_losing_trade(row, s)
        except Exception as e:
            logger.warning(f"async post_mortem #{trade_id} failed: {e}")
        finally:
            s.close()
    try:
        _post_mortem_pool.submit(_job)
    except Exception as e:
        logger.warning(f"could not schedule post_mortem #{trade_id}: {e}")


def _signal_idempotency_key(signal: Dict[str, Any]) -> str:
    """
    Deterministic dedupe hash for a signal: ticker + direction + rounded
    entry/stop/T1 + confidence-bucket + UTC date. Postmortem fix H2: yesterday's
    stale signal that happens to round to the same prices as today's fresh
    high-conviction setup was deduping the new entry. Including the day-stamp
    forces a fresh key each session; including the confidence bucket
    distinguishes a 60-conf chop signal from a 90-conf trend signal even when
    levels rounded identically.
    """
    conf_bucket = int(float(signal.get("confidence") or 0) // 10)
    day_stamp = datetime.utcnow().strftime("%Y%m%d")
    parts = "|".join([
        str(signal.get("ticker", "")).upper(),
        str(signal.get("signal_type", "")),
        f"{round(float(signal.get('entry') or 0), 2):.2f}",
        f"{round(float(signal.get('stop_loss') or 0), 2):.2f}",
        f"{round(float(signal.get('target1') or 0), 2):.2f}",
        str(signal.get("timeframe", "")),
        f"c{conf_bucket}",
        day_stamp,
    ])
    return hashlib.sha1(parts.encode()).hexdigest()[:16]

from database import SessionLocal, AutoTrade, AutoTraderConfig, Signal, WatchlistStock
from services import paper_trader, live_quotes
from services import post_mortem as post_mortem_svc
from services import metrics
from services.bear_thesis import build_bear_thesis
from services.options_analyzer import suggest_options_for_signal
from services.data_fetcher import get_current_price as fetch_current_price

logger = logging.getLogger(__name__)


# ---------- Config ---------------------------------------------------------

def get_config(db: Session) -> AutoTraderConfig:
    cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
    if not cfg:
        cfg = AutoTraderConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def get_config_dict() -> Dict[str, Any]:
    db = SessionLocal()
    try:
        cfg = get_config(db)
        return {
            "enabled": cfg.enabled,
            "confidence_threshold": cfg.confidence_threshold,
            "max_pct_of_equity": cfg.max_pct_of_equity,
            "stock_pct_of_equity": cfg.stock_pct_of_equity,
            "option_pct_of_equity": cfg.option_pct_of_equity,
            "max_risk_per_trade_pct": cfg.max_risk_per_trade_pct,
            "trade_options": cfg.trade_options,
            "signal_timeframes": cfg.signal_timeframes or "1h,4h,1d",
            "stop_atr_mult": cfg.stop_atr_mult or 2.0,
            "chandelier_atr_mult": cfg.chandelier_atr_mult if cfg.chandelier_atr_mult is not None else 3.0,
            "dry_run": bool(cfg.dry_run),
            "max_per_sector": cfg.max_per_sector or 3,
        }
    finally:
        db.close()


# ---------- Kill switch + daily loss bookkeeping --------------------------

def realized_pnl_today() -> float:
    """Sum of realized_pl on auto-trades closed since 00:00 UTC today.

    Used by the daily-loss gate and surfaced on /api/health for observability.
    Kept as UTC for simplicity — the gate fires against today's paper P/L
    rather than a calendar-session P/L, which is close enough for our purposes
    and doesn't require market-calendar logic.
    """
    db = SessionLocal()
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = db.query(AutoTrade).filter(
            AutoTrade.closed_at != None,  # noqa: E711
            AutoTrade.closed_at >= today_start,
        ).all()
        return float(sum((r.realized_pl or 0.0) for r in rows))
    except Exception as e:
        logger.warning(f"realized_pnl_today failed: {e}")
        return 0.0
    finally:
        db.close()


def count_open_auto_trades() -> int:
    db = SessionLocal()
    try:
        return db.query(AutoTrade).filter(
            AutoTrade.status.in_(["pending", "open"])
        ).count()
    finally:
        db.close()


def kill(reason: Optional[str] = None, flatten: bool = True, cancel_orders: bool = True) -> Dict[str, Any]:
    """Emergency halt: disable auto-trader, flip the persistent kill flag,
    optionally cancel every working order + flatten every open position.

    The kill flag is persisted in AutoTraderConfig.killed so a process
    restart does NOT silently re-arm the bot. unkill() clears the flag but
    deliberately does NOT re-enable — re-arming requires a separate
    /auto/config {enabled:true} step.
    """
    db = SessionLocal()
    flattened: List[str] = []
    cancelled = 0
    try:
        cfg = get_config(db)
        cfg.enabled = False
        cfg.killed = True
        cfg.killed_at = datetime.utcnow()
        cfg.killed_reason = (reason or "unspecified")[:255]
        db.commit()
    finally:
        db.close()

    if cancel_orders and paper_trader.is_enabled():
        try:
            r = paper_trader.cancel_all_orders()
            cancelled = int(r.get("cancelled") or 0)
        except Exception as e:
            logger.error(f"kill(): cancel_all_orders failed: {e}")

    if flatten and paper_trader.is_enabled():
        try:
            r = paper_trader.close_all_positions(cancel_orders=False)
            flattened = list(r.get("closed") or [])
        except Exception as e:
            logger.error(f"kill(): close_all_positions failed: {e}")

    logger.critical(
        f"AUTO-TRADER KILLED reason={reason!r} flattened={len(flattened)} cancelled={cancelled}"
    )
    metrics.inc("autotrade_event", event="killed")
    return {
        "killed": True,
        "flattened": flattened,
        "cancelled": cancelled,
        "reason": reason,
    }


def unkill(reason: Optional[str] = None) -> Dict[str, Any]:
    """Clear the persistent kill flag. Does NOT set enabled=True — that's a
    deliberate second step via /auto/config to prevent accidental re-arming.
    """
    db = SessionLocal()
    try:
        cfg = get_config(db)
        cfg.killed = False
        cfg.killed_at = None
        cfg.killed_reason = None
        db.commit()
    finally:
        db.close()
    logger.warning(f"AUTO-TRADER UNKILLED reason={reason!r} (enabled still False — re-arm via /auto/config)")
    metrics.inc("autotrade_event", event="unkilled")
    return {"killed": False, "enabled": False, "reason": reason}


def compute_confidence_calibration(min_bucket_n: int = 5) -> Dict[str, Any]:
    """F1: Bucket closed auto-trades by signal-confidence and compute realized
    win-rate + avg PnL per bucket. Exposes whether our confidence score has
    any predictive power — e.g. if the 80-89 bucket has a LOWER win-rate
    than 60-69, the scoring is miscalibrated and the thresholds need a
    retune. Called nightly from the scheduler; results are logged + stored
    as a metrics gauge so /api/health surfaces them.
    """
    db = SessionLocal()
    try:
        rows = db.query(AutoTrade, Signal).outerjoin(
            Signal, AutoTrade.signal_id == Signal.id
        ).filter(
            AutoTrade.status.in_(["closed_target", "closed_stop"]),
            AutoTrade.realized_pl != None,  # noqa: E711
        ).all()
    finally:
        db.close()

    buckets: Dict[str, Dict[str, float]] = {}
    for (t, s) in rows:
        conf = float(getattr(s, "confidence", 0) or 0) if s else 0.0
        if conf <= 0:
            continue
        key = f"{int(conf // 10) * 10}-{int(conf // 10) * 10 + 9}"
        b = buckets.setdefault(key, {"n": 0, "wins": 0, "total_pl": 0.0})
        b["n"] += 1
        if (t.realized_pl or 0) > 0:
            b["wins"] += 1
        b["total_pl"] += float(t.realized_pl or 0)

    summary = {}
    for key, b in sorted(buckets.items()):
        if b["n"] < min_bucket_n:
            continue
        win_rate = b["wins"] / b["n"]
        avg_pl = b["total_pl"] / b["n"]
        summary[key] = {
            "n": b["n"],
            "win_rate": round(win_rate, 3),
            "avg_pl": round(avg_pl, 2),
        }
        try:
            metrics.inc("calibration_bucket", bucket=key, win_rate=round(win_rate, 3))
        except Exception:
            pass
    logger.info(f"AutoTrader confidence calibration: {summary}")
    return summary


def update_config(**kwargs) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        cfg = get_config(db)
        for k, v in kwargs.items():
            if hasattr(cfg, k) and v is not None:
                setattr(cfg, k, v)
        db.commit()
        db.refresh(cfg)
    finally:
        db.close()
    return get_config_dict()


# ---------- Budget bookkeeping --------------------------------------------

def _open_allocations(db: Session) -> Dict[str, float]:
    """Sum notional of currently-open auto trades, by asset_type."""
    open_trades = db.query(AutoTrade).filter(
        AutoTrade.status.in_(["pending", "open"])
    ).all()
    out = {"stock": 0.0, "option": 0.0}
    for t in open_trades:
        px = t.entry_price or t.requested_entry or 0.0
        # Options trade in 100-share contracts, premium is per share
        mult = 100.0 if t.asset_type == "option" else 1.0
        out[t.asset_type] = out.get(t.asset_type, 0.0) + px * t.qty * mult
    return out


def status_snapshot() -> Dict[str, Any]:
    """Return current budget state — used by the UI status pill."""
    db = SessionLocal()
    try:
        cfg = get_config(db)
        acct = paper_trader.get_account()
        equity = float(acct["equity"]) if acct else 0.0
        alloc = _open_allocations(db)
        stock_budget = equity * cfg.stock_pct_of_equity
        option_budget = equity * cfg.option_pct_of_equity
        total_cap = equity * cfg.max_pct_of_equity
        deployed = alloc["stock"] + alloc["option"]
        return {
            "enabled": cfg.enabled,
            "broker_connected": acct is not None,
            "equity": equity,
            "total_cap": total_cap,
            "deployed": deployed,
            "stock_budget": stock_budget,
            "stock_used": alloc["stock"],
            "stock_remaining": max(0.0, stock_budget - alloc["stock"]),
            "option_budget": option_budget,
            "option_used": alloc["option"],
            "option_remaining": max(0.0, option_budget - alloc["option"]),
            "config": get_config_dict(),
            "open_trades": db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending", "open"])
            ).count(),
        }
    finally:
        db.close()


def list_trades(limit: int = 50) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        rows = db.query(AutoTrade).order_by(desc(AutoTrade.opened_at)).limit(limit).all()
        return [_serialize(t) for t in rows]
    finally:
        db.close()


def _serialize(t: AutoTrade) -> Dict[str, Any]:
    import json as _json
    pm = None
    if t.post_mortem:
        try:
            pm = _json.loads(t.post_mortem)
        except Exception:
            pm = None
    th = None
    if t.targets_history:
        try:
            th = _json.loads(t.targets_history)
        except Exception:
            th = None
    return {
        "id": t.id,
        "ticker": t.ticker,
        "symbol": t.symbol,
        "asset_type": t.asset_type,
        "side": t.side,
        "qty": t.qty,
        "entry_price": t.entry_price,
        "requested_entry": t.requested_entry,
        "stop_loss": t.stop_loss,
        "current_stop": t.current_stop,
        "target1": t.target1,
        "target2": t.target2,
        "target3": t.target3,
        "level_index": t.level_index or 0,
        "targets_history": th,
        "hit_t1": t.hit_t1,
        "status": t.status,
        "note": t.note,
        "parent_order_id": t.parent_order_id,
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "realized_pl": t.realized_pl,
        "post_mortem": pm,
        "has_post_mortem": pm is not None,
    }


def regenerate_post_mortem(trade_id: int) -> Optional[Dict[str, Any]]:
    """Force-rerun the post-mortem for a closed losing trade — useful after rule tweaks."""
    db = SessionLocal()
    try:
        t = db.query(AutoTrade).filter(AutoTrade.id == trade_id).first()
        if not t:
            return None
        return post_mortem_svc.analyze_losing_trade(t, db)
    finally:
        db.close()


# ---------- Entry: react to a fresh signal --------------------------------

MIN_OPTION_SCORE = 65   # contract score gate before we'll auto-buy a put


def consider_signal(signal: Dict[str, Any], signal_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Called after each analysis run. If the signal is strong enough and budget
    allows, submits a bracket order. Returns the AutoTrade row dict if opened,
    None otherwise.

    NOTE: only called for stock-direction BUY signals — the put-play hunt is
    invoked separately at the end of every per-ticker analysis loop via
    `consider_put_play(ticker)`.

    Thread-safety: holds the process-wide `_entry_lock` for the duration of
    the budget/cap/idempotency checks. Two concurrent calls (same or different
    tickers) serialize so a 3rd same-sector trade can't slip past the cap.
    """
    # Short-circuit if the buying-power breaker tripped recently.
    global _bp_exhausted_until
    if _bp_exhausted_until and datetime.utcnow() < _bp_exhausted_until:
        return None
    if not _entry_lock.acquire(timeout=30.0):
        logger.warning(f"consider_signal({signal.get('ticker')}): entry lock busy >30s, skipping")
        metrics.inc("autotrade_event", event="entry_lock_timeout")
        return None
    db = SessionLocal()
    try:
        cfg = get_config(db)
        if not cfg.enabled:
            return None
        # C1/G5: persistent kill flag — never re-arm silently on restart.
        if getattr(cfg, "killed", False):
            return None
        if not paper_trader.is_enabled():
            return None
        if signal.get("signal_type") != "BUY":
            return None  # long-only stock entries; puts are handled separately
        confidence = float(signal.get("confidence") or 0)
        if confidence < cfg.confidence_threshold:
            return None

        # C1: Daily loss limit — halt new entries once realized PnL today is
        # worse than -(daily_loss_limit_pct * equity). Existing trades keep
        # trailing; this only blocks NEW exposure.
        dll = float(getattr(cfg, "daily_loss_limit_pct", 0) or 0)
        if dll > 0:
            _acct_probe = paper_trader.get_account()
            _equity_probe = float(_acct_probe["equity"]) if _acct_probe else 0.0
            _rpnl = realized_pnl_today()
            if _equity_probe > 0 and _rpnl <= -abs(dll) * _equity_probe:
                logger.warning(
                    f"AutoTrader skip {signal.get('ticker')}: daily-loss limit hit "
                    f"(realized {_rpnl:.2f} ≤ -{dll*100:.1f}% × equity {_equity_probe:.0f})"
                )
                metrics.inc("autotrade_event", event="daily_loss_halt")
                return None

        # C1: Max concurrent positions guard.
        mcp = int(getattr(cfg, "max_concurrent_positions", 0) or 0)
        if mcp > 0 and count_open_auto_trades() >= mcp:
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: max_concurrent_positions {mcp} reached"
            )
            return None

        # F7: Signal freshness — reject stale signals. Freshness window scales
        # with the timeframe (2× tf minutes, floor 15m, ceil 4h).
        _tf_min_map = {"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":390,"1mo":390}
        sig_tf_str = (signal.get("timeframe") or "").strip()
        tf_mins = _tf_min_map.get(sig_tf_str, 60)
        max_age_mins = max(15, min(240, 2 * tf_mins))
        gen_at = signal.get("generated_at")
        if gen_at:
            try:
                from datetime import datetime as _dt
                if isinstance(gen_at, str):
                    gen_dt = _dt.fromisoformat(gen_at.replace("Z", "+00:00"))
                else:
                    gen_dt = gen_at
                age_s = (_dt.utcnow() - gen_dt.replace(tzinfo=None)).total_seconds()
                if age_s > max_age_mins * 60:
                    logger.info(
                        f"AutoTrader skip {signal.get('ticker')}: signal age {age_s/60:.1f}m > {max_age_mins}m (stale)"
                    )
                    return None
            except Exception:
                pass
        # Per-timeframe gate: don't auto-trade off 1mo/5m signals etc.
        allowed_tfs = {s.strip() for s in (cfg.signal_timeframes or "1h,4h,1d").split(",") if s.strip()}
        sig_tf = (signal.get("timeframe") or "").strip()
        if allowed_tfs and sig_tf not in allowed_tfs:
            return None
        entry = signal.get("entry")
        stop = signal.get("stop_loss")
        t1 = signal.get("target1")
        t2 = signal.get("target2")
        t3 = signal.get("target3")
        if not (entry and stop and t1):
            return None
        if stop >= entry:
            return None  # malformed signal
        # C10: Fat-finger guard — risk-per-share outside [0.1%, 10%] of entry
        # almost always indicates a bad level (stop on wrong side of a gap,
        # or a stop so wide the trade is a lottery ticket). Reject loudly
        # rather than deploying real capital against garbage geometry.
        _rps_pct = (entry - stop) / entry if entry > 0 else 0
        if _rps_pct < 0.001 or _rps_pct > 0.10:
            # INFO not WARNING: this is a normal reject path (the guard doing
            # its job), not an anomaly worth drawing the eye in the log.
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: fat-finger guard "
                f"(risk-per-share {_rps_pct*100:.2f}% of entry, expected 0.1%-10%)"
            )
            metrics.inc("autotrade_event", event="fat_finger_reject")
            return None
        # Trailing-only exit: park the bracket TP far away so it never fires.
        # Real exit comes from the trailing stop in manage_open_positions().
        risk_per_share = entry - stop
        far_tp = round(entry + 10 * risk_per_share, 2)

        ticker = signal["ticker"].upper()

        # Per-ticker auto-trade gate
        ws = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
        if ws and getattr(ws, "auto_trade_enabled", True) is False:
            return None

        # One open auto-trade per ticker.
        existing = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.status.in_(["pending", "open"]),
        ).first()
        if existing:
            return None

        # Idempotency: don't re-open the same signal we already trade-row'd in
        # the recent past (covers retries, scheduler races, refresh-spam).
        idem = _signal_idempotency_key(signal)
        from datetime import timedelta as _td
        from services.config import IDEMPOTENCY_LOOKBACK_HOURS as _IDEM_HRS
        recent_dup = db.query(AutoTrade).filter(
            AutoTrade.idempotency_key == idem,
            AutoTrade.opened_at > datetime.utcnow() - _td(hours=_IDEM_HRS),
        ).first()
        if recent_dup:
            logger.info(f"AutoTrader skip {ticker}: idempotent dup of trade #{recent_dup.id}")
            return None

        # Soft correlation cap: don't pile into the same sector
        try:
            from services.data_fetcher import get_ticker_info
            sector = (get_ticker_info(ticker).get("sector") or "").strip()
        except Exception:
            sector = ""
        if sector and getattr(cfg, "max_per_sector", 3):
            same_sector_open = db.query(AutoTrade).filter(
                AutoTrade.sector == sector,
                AutoTrade.status.in_(["pending", "open"]),
            ).count()
            if same_sector_open >= cfg.max_per_sector:
                logger.info(
                    f"AutoTrader skip {ticker}: {same_sector_open} open trades already in sector "
                    f"'{sector}' (cap {cfg.max_per_sector})"
                )
                return None

        # Check budget
        acct = paper_trader.get_account()
        if not acct:
            return None
        equity = float(acct["equity"])
        cash = float(acct["cash"])
        buying_power = float(acct.get("buying_power") or cash)
        # Postmortem fix M1: subtract our locally-tracked in-flight reservation
        # so the same scan can't size 30 orders against the same stale BP
        # snapshot before Alpaca reflects the first one.
        _decay_in_flight_bp_if_stale()
        in_flight = _get_in_flight_bp()
        buying_power = max(0.0, buying_power - in_flight)
        cash = max(0.0, cash - in_flight)
        alloc = _open_allocations(db)
        stock_budget = equity * cfg.stock_pct_of_equity
        stock_remaining = stock_budget - alloc["stock"]

        # Position sizing: cap by (a) risk-per-trade, (b) remaining stock budget,
        # (c) per-ticker cap of 25% of stock budget, (d) cash, (e) buying power.
        # Buying-power cap catches margin-account cases where cash > BP because
        # other queued bracket orders have reserved BP — without it Alpaca
        # rejects the submit with code 40310000 and we log noise.
        if risk_per_share <= 0:
            return None
        risk_budget = equity * cfg.max_risk_per_trade_pct
        # Profit-max: scale risk budget with confidence headroom above threshold.
        # Signals that clear the gate by a wide margin deserve a bigger bet.
        conf_headroom = max(0.0, (confidence - cfg.confidence_threshold) / max(1.0, (100.0 - cfg.confidence_threshold)))
        conf_mult = 1.0 + (_MAX_CONFIDENCE_RISK_MULT - 1.0) * min(1.0, conf_headroom)
        # Kelly-lite: if backtest win-rate is strong, boost further.
        try:
            bt_wr = float(signal.get("backtest_win_rate") or 0)
        except Exception:
            bt_wr = 0.0
        if bt_wr >= _KELLY_MIN_WIN_RATE:
            kelly_edge = min(1.0, (bt_wr - _KELLY_MIN_WIN_RATE) / (100.0 - _KELLY_MIN_WIN_RATE))
            kelly_mult = 1.0 + (_KELLY_MAX_MULT - 1.0) * kelly_edge
        else:
            kelly_mult = 1.0
        effective_risk_budget = risk_budget * conf_mult * kelly_mult
        max_qty_by_risk = int(effective_risk_budget / risk_per_share)
        max_qty_by_remaining = int(stock_remaining / entry)
        # Profit-max: per-ticker cap raised from 25% → 30% of stock budget.
        # With a 10-position concurrent cap, this lets a strong conviction
        # trade meaningfully out-size weaker ones without starving diversity.
        max_qty_by_per_ticker = int((stock_budget * 0.30) / entry)
        max_qty_by_cash = int(cash / entry)
        max_qty_by_bp = int(buying_power / entry)
        qty = min(
            max_qty_by_risk, max_qty_by_remaining,
            max_qty_by_per_ticker, max_qty_by_cash, max_qty_by_bp,
        )

        if qty < 1:
            return None

        logger.info(
            f"AutoTrader opening {ticker} qty={qty} entry≈{entry} stop={stop} "
            f"T1={t1} T2={t2} T3={t3} (conf {confidence}, risk/share {risk_per_share:.2f}, "
            f"far_tp={far_tp})"
        )

        # Dry-run: record the intended trade but don't actually submit.
        if getattr(cfg, "dry_run", False):
            db.add(AutoTrade(
                ticker=ticker, symbol=ticker, asset_type="stock", side="buy",
                qty=qty, requested_entry=entry, stop_loss=stop, current_stop=stop,
                target1=t1, target2=t2, target3=t3, level_index=0,
                signal_id=signal_id, sector=sector or None, idempotency_key=idem,
                status="closed_manual",
                note=f"DRY-RUN simulated entry @ {entry} (would risk ${risk_per_share*qty:.2f})",
                closed_at=datetime.utcnow(),
            ))
            db.commit()
            logger.info(f"AutoTrader DRY-RUN {ticker} qty={qty} entry≈{entry} (no broker submit)")
            return None

        res = paper_trader.submit_bracket_order(
            symbol=ticker,
            qty=qty,
            side="buy",
            entry_type="market",
            take_profit=far_tp,
            stop_loss=round(stop, 2),
            time_in_force="gtc",
            # Use a UUID so Alpaca never rejects a retry as a duplicate
            # client_order_id (cancelled/filled IDs cannot be reused). DB-level
            # idempotency is handled by the idem hash above — the Alpaca ID
            # just needs to be unique per submission attempt.
            client_order_id=f"at-{__import__('uuid').uuid4().hex[:16]}",
        )
        if "error" in res:
            # Stamp the idempotency_key on error rows too — without it, every
            # 15-min scan regenerates the same signal, fails the same submit,
            # and inserts another error row (retry storm filling the table +
            # Alpaca log). With the key set, the dedupe query above short-
            # circuits future attempts until the lookback window expires.
            db.add(AutoTrade(
                ticker=ticker, symbol=ticker, asset_type="stock", side="buy",
                qty=qty, requested_entry=entry, stop_loss=stop, current_stop=stop,
                target1=t1, target2=t2, target3=t3, level_index=0,
                signal_id=signal_id, sector=sector or None, idempotency_key=idem,
                status="error", note=f"submit failed: {res['error']}",
            ))
            db.commit()
            logger.warning(f"AutoTrader submit failed for {ticker}: {res['error']}")
            # Buying-power circuit breaker: if Alpaca says we're tapped out,
            # skip ALL consider_signal calls for the next 30 min. Otherwise
            # the next tick re-evaluates every ticker and submits N more
            # doomed orders before anything closes.
            err_lower = str(res.get("error", "")).lower()
            if "insufficient buying power" in err_lower or "insufficient_buying_power" in err_lower:
                from datetime import timedelta as _td2
                _bp_exhausted_until = datetime.utcnow() + _td2(minutes=30)
                logger.warning(
                    f"AutoTrader: buying-power exhausted, pausing new entries until {_bp_exhausted_until.isoformat()}Z"
                )
                metrics.inc("autotrade_event", event="bp_exhausted")
            return None

        trade = AutoTrade(
            ticker=ticker, symbol=ticker, asset_type="stock", side="buy",
            qty=qty,
            requested_entry=entry,
            stop_loss=stop,
            current_stop=stop,
            target1=t1,
            target2=t2,
            target3=t3,
            level_index=0,
            signal_id=signal_id,
            parent_order_id=res.get("id"),
            status="pending",
            sector=sector or None,
            idempotency_key=idem,
            high_water_mark=entry,
            note=f"opened from signal conf {confidence:.0f} ({signal.get('timeframe')})",
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        # Postmortem fix M1: reserve BP locally so the next ticker in this
        # same scan loop sees a smaller available BP figure.
        _reserve_bp(qty * float(entry))
        metrics.inc("autotrade_event", event="opened")
        return _serialize(trade)
    except Exception as e:
        from sqlalchemy.exc import OperationalError as _OE
        if isinstance(e, _OE) and "database is locked" in str(e).lower():
            # Transient: the 30s busy_timeout elapsed while another writer
            # (typically manage_open_positions) held the SQLite lock. No
            # data lost — the next 15-min scan re-evaluates this signal.
            # Demote to warning so the log stays clean.
            logger.warning(f"consider_signal: SQLite lock timeout (will retry next scan): {e}")
        else:
            logger.exception(f"consider_signal error: {e}")
        return None
    finally:
        db.close()
        _entry_lock.release()


# ---------- Entry: put-play hunt for non-BUY tickers -----------------------

def consider_put_play(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Synthesizes a bear thesis for `ticker`, picks the best PUT contract, and —
    if it clears the score gate and the option budget has room — buys 1+
    contracts at market. Position is tracked as an AutoTrade with
    asset_type='option'.

    Thread-safety: shares `_entry_lock` with consider_signal so option budget
    checks don't race against stock-side entries draining the same equity.
    """
    if not _entry_lock.acquire(timeout=30.0):
        logger.warning(f"consider_put_play({ticker}): entry lock busy >30s, skipping")
        metrics.inc("autotrade_event", event="entry_lock_timeout")
        return None
    db = SessionLocal()
    try:
        cfg = get_config(db)
        if not (cfg.enabled and cfg.trade_options):
            return None
        if not paper_trader.is_enabled():
            return None

        ticker = ticker.upper()

        # Per-ticker auto-trade gate
        ws = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
        if ws and getattr(ws, "auto_trade_enabled", True) is False:
            return None

        # Don't double-trade an underlying we already have a long stock auto-trade on
        existing = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.status.in_(["pending", "open"]),
        ).first()
        if existing:
            return None

        thesis = build_bear_thesis(ticker, "1d")
        if not thesis:
            return None
        if thesis["confidence"] < cfg.confidence_threshold * 0.7:
            # Bear conviction scale runs cooler than long conviction; require ~70% of threshold
            return None

        # Postmortem fix H4: bear-thesis is computed off cached daily data
        # (up to 1h TTL). If the underlying ripped through the bear-thesis
        # stop in the interim, the put we're about to buy will get exited
        # at premium-decay loss on the very next manage tick. Reject early.
        try:
            fresh_px = _current_price(ticker)
            stop_underlying = float(thesis.get("stop_loss") or 0)
            if fresh_px and stop_underlying > 0 and fresh_px >= stop_underlying * 0.99:
                logger.info(
                    f"AutoTrader skip PUT {ticker}: live price ${fresh_px:.2f} already "
                    f">= 99% of bear-thesis stop ${stop_underlying:.2f} (stale thesis)"
                )
                return None
        except Exception:
            pass

        # Sanity-check the underlying stop distance. The bear-thesis stop sits
        # ABOVE current price; if it's > 12% wide, the put is being held
        # against an irrationally large adverse move before we'd cut, which
        # blows past the spirit of `max_risk_per_trade_pct` (the dollar cap is
        # met because qty shrinks, but theta+vega will eat the few contracts
        # we hold long before the stop fires).
        try:
            entry_underlying = float(thesis.get("entry") or 0)
            stop_underlying = float(thesis.get("stop_loss") or 0)
            if entry_underlying > 0 and stop_underlying > 0:
                stop_distance_pct = (stop_underlying - entry_underlying) / entry_underlying
                if stop_distance_pct > 0.12:
                    logger.info(
                        f"AutoTrader skip PUT {ticker}: bear-thesis stop {stop_distance_pct*100:.1f}% "
                        f"above entry — too wide for option theta budget"
                    )
                    return None
        except Exception:
            pass

        sugg = suggest_options_for_signal(ticker, thesis)
        contracts = sugg.get("contracts") or []
        if not contracts:
            return None
        top = contracts[0]
        if top.get("score", 0) < MIN_OPTION_SCORE:
            return None

        # Budget check
        acct = paper_trader.get_account()
        if not acct:
            return None
        equity = float(acct["equity"])
        cash = float(acct["cash"])
        buying_power = float(acct.get("buying_power") or cash)
        # Postmortem fix M1
        _decay_in_flight_bp_if_stale()
        in_flight = _get_in_flight_bp()
        buying_power = max(0.0, buying_power - in_flight)
        cash = max(0.0, cash - in_flight)
        alloc = _open_allocations(db)
        option_budget = equity * cfg.option_pct_of_equity
        option_remaining = option_budget - alloc["option"]

        # Sizing rules:
        #   • Risk per contract = effective_max_loss (already in $, not per-share)
        #   • Max risk per trade ≤ max_risk_per_trade_pct of equity
        #   • Notional (premium*100*qty) ≤ remaining option bucket
        #   • Per-ticker option cap = 33% of option budget
        #   • Buying-power cap — reject at-sizing if BP already eaten by other
        #     pending submits (prevents Alpaca 40310000 at submit time).
        risk_per_contract = float(top.get("effective_max_loss") or top.get("max_loss_per_contract") or 0)
        if risk_per_contract <= 0:
            return None
        notional_per_contract = float(top["premium"]) * 100
        risk_budget = equity * cfg.max_risk_per_trade_pct
        max_qty_by_risk = int(risk_budget / risk_per_contract)
        max_qty_by_remaining = int(option_remaining / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_per_ticker = int((option_budget * 0.33) / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_cash = int(cash / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_bp = int(buying_power / notional_per_contract) if notional_per_contract > 0 else 0
        qty = min(
            max_qty_by_risk, max_qty_by_remaining,
            max_qty_by_per_ticker, max_qty_by_cash, max_qty_by_bp,
        )
        if qty < 1:
            return None

        occ = top["symbol"]

        # Alpaca rejects option MARKET orders outside RTH (code 42210000).
        # Skip early instead of submitting, logging, and writing an error row
        # on every 15-min scan while the market is closed.
        if not paper_trader.is_market_open():
            logger.info(f"AutoTrader skip PUT {ticker} {occ}: market closed")
            return None

        logger.info(
            f"AutoTrader PUT {ticker} {occ} qty={qty} premium={top['premium']} "
            f"score={top['score']} bear-conf={thesis['confidence']}"
        )

        res = paper_trader.submit_simple_option_order(
            occ_symbol=occ, qty=qty, side="buy",
            order_type="market", time_in_force="day",
        )
        if "error" in res:
            db.add(AutoTrade(
                ticker=ticker, symbol=occ, asset_type="option", side="buy",
                qty=qty, requested_entry=top["premium"],
                stop_loss=top["effective_stop_premium"],
                current_stop=top["effective_stop_premium"],
                target1=thesis["target1"], target2=thesis["target2"],
                target3=thesis.get("target3"), level_index=0,
                status="error",
                note=f"option submit failed: {res['error']}",
            ))
            db.commit()
            logger.warning(f"AutoTrader put submit failed for {occ}: {res['error']}")
            return None

        trade = AutoTrade(
            ticker=ticker,
            symbol=occ,
            asset_type="option",
            side="buy",
            qty=qty,
            requested_entry=float(top["premium"]),       # premium per share
            # NOTE: for option trades the broker has no SL leg. We track the
            # UNDERLYING stop level here (initialised to the bear-thesis stop) so
            # the state-machine trail in _manage_option_trade can mutate it.
            stop_loss=float(thesis["stop_loss"]),
            current_stop=float(thesis["stop_loss"]),
            target1=float(thesis["target1"]),            # underlying levels — manage loop
            target2=float(thesis["target2"]),            # uses these to trail stops
            target3=float(thesis["target3"]) if thesis.get("target3") else None,
            level_index=0,
            parent_order_id=res.get("id"),
            status="pending",
            note=(
                f"PUT play: bear-conf {thesis['confidence']} | strike {top['strike']} "
                f"exp {top['expiration']} ({top['dte']}d) | underlying stop "
                f"${thesis['stop_loss']:.2f}, T1 ${thesis['target1']:.2f}, T2 ${thesis['target2']:.2f}"
            ),
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        # Postmortem fix M1: reserve BP for option premium too.
        _reserve_bp(qty * float(top["premium"]) * 100.0)
        metrics.inc("autotrade_event", event="opened_put")
        return _serialize(trade)
    except Exception as e:
        logger.exception(f"consider_put_play error for {ticker}: {e}")
        return None
    finally:
        db.close()
        _entry_lock.release()


# ---------- Manage: trail stops, reconcile ---------------------------------

def _get_legs(parent_id: str) -> List[Any]:
    """Return child orders (TP + SL) of a bracket parent."""
    c = paper_trader._get_client()
    if not c:
        return []
    try:
        parent = c.get_order_by_id(parent_id)
        return list(parent.legs or [])
    except Exception as e:
        logger.warning(f"could not fetch legs of {parent_id}: {e}")
        return []


def _identify_legs(parent_id: str) -> Dict[str, Optional[str]]:
    """Return {'stop_id': ..., 'tp_id': ...} for a bracket parent."""
    out: Dict[str, Optional[str]] = {"stop_id": None, "tp_id": None}
    for leg in _get_legs(parent_id):
        otype = str(getattr(leg, "order_type", "")).lower()
        if "stop" in otype:
            out["stop_id"] = str(leg.id)
        elif "limit" in otype:
            out["tp_id"] = str(leg.id)
    return out


# Last successfully-requested stop price per broker order id. Stops the
# manage loop from calling Alpaca with an identical stop price twice in
# consecutive ticks (race produces "order already replaced" / 42210000).
_replace_stop_cache: Dict[str, float] = {}


def _replace_stop(stop_order_id: str, new_stop: float) -> bool:
    """Move the SL child order to a new stop price.

    Returns True only if the broker acknowledged the replacement. Callers
    MUST guard their DB mutation behind this return value (current pattern:
    `if new_stop > t.current_stop and _replace_stop(...): t.current_stop = ...`)
    so the database can never carry a tighter stop than the broker actually
    holds. A False return is logged loudly because every failure means the
    next manage tick will re-attempt — silent drift is a money bug.
    """
    rounded = round(float(new_stop), 2)
    # Idempotency: if we already sent this exact stop price for this order
    # (and Alpaca accepted it), skip the round-trip. Avoids the "order
    # already replaced" race when two manage ticks compute the same target.
    last = _replace_stop_cache.get(stop_order_id)
    if last is not None and abs(last - rounded) < 0.005:
        return True
    c = paper_trader._get_client()
    if not c:
        logger.warning(f"replace_stop {stop_order_id}: no broker client — keeping old stop")
        return False
    try:
        from alpaca.trading.requests import ReplaceOrderRequest
        c.replace_order_by_id(
            stop_order_id,
            order_data=ReplaceOrderRequest(stop_price=rounded),
        )
        _replace_stop_cache[stop_order_id] = rounded
        return True
    except Exception as e:
        err_lower = str(e).lower()
        # Alpaca briefly reports "order already replaced" when our previous
        # replace is still settling. Our intent is accepted — record it and
        # treat as success so the DB advances; next tick will re-sync if
        # the broker ends up on a different price.
        if "already replaced" in err_lower:
            _replace_stop_cache[stop_order_id] = rounded
            logger.debug(
                f"replace_stop {stop_order_id}: already-replaced (racing prior tick), "
                f"caching intent {rounded}"
            )
            return True
        # Loud — DB will not advance, broker keeps prior stop. Manage loop
        # will retry every 60s until success.
        logger.error(
            f"replace_stop FAILED {stop_order_id} → {new_stop}: {e} "
            f"(broker stop unchanged, will retry next manage tick)"
        )
        return False


from services.config import PRICE_FALLBACK_TTL_SEC as _PRICE_FALLBACK_TTL
_price_fallback_cache: Dict[str, tuple] = {}  # ticker -> (price, expiry_ts)

# Daily-ATR cache for the chandelier overlay. ATR_14 on 1d bars only changes
# meaningfully once per session — recomputing every 60s tick across N open
# trades wastes CPU + 1d OHLCV fetches. 5-minute TTL is plenty.
_chandelier_atr_cache: Dict[str, tuple] = {}  # ticker -> (atr, expiry_ts)
_CHANDELIER_ATR_TTL = 300.0


def _chandelier_atr(ticker: str) -> Optional[float]:
    import time as _t
    now = _t.time()
    cached = _chandelier_atr_cache.get(ticker.upper())
    if cached and now < cached[1]:
        return cached[0]
    try:
        from services.data_fetcher import fetch_ohlcv as _fo
        from services.indicators import compute_indicators as _ci
        _df = _fo(ticker, "1d")
        if _df is None or _df.empty:
            return None
        _ind = _ci(_df)
        atr = float(_ind["ATR_14"].iloc[-1])
        _chandelier_atr_cache[ticker.upper()] = (atr, now + _CHANDELIER_ATR_TTL)
        return atr
    except Exception:
        return None


def _current_price(ticker: str) -> Optional[float]:
    """
    Use live WS quote first (no network), fall back to a 30-second-memoized
    Yahoo fetch so the manage loop doesn't hammer external APIs every minute
    for tickers that aren't streaming.
    """
    try:
        live = live_quotes.get_live_price(ticker)
        if live and live > 0:
            return live
    except Exception:
        pass

    import time as _t
    now = _t.time()
    cached = _price_fallback_cache.get(ticker.upper())
    if cached and now < cached[1]:
        return cached[0]
    try:
        pi = fetch_current_price(ticker)
        if pi:
            px = float(pi[0])
            _price_fallback_cache[ticker.upper()] = (px, now + _PRICE_FALLBACK_TTL)
            return px
    except Exception:
        return None
    return None


def _recalculate_targets(ticker: str, direction: str, current_price: float) -> Optional[List[float]]:
    """
    After T3 is breached and the trend is clearly continuing, compute the next
    three targets from `current_price`. Uses recent daily swing levels above
    (long) or below (bear) current price; falls back to ATR-based steps when
    the chart hasn't formed enough structure beyond.
    Returns [t1, t2, t3] or None on failure.
    """
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.support_resistance import swing_levels
        from services.indicators import compute_indicators
        from services.gap_detector import gap_targets_above, gap_targets_below
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty:
            return None
        levels = swing_levels(df, window=10, max_levels=12)
        # ATR fallback step
        atr = None
        try:
            ind = compute_indicators(df)
            atr = float(ind["ATR_14"].iloc[-1])
        except Exception:
            atr = current_price * 0.02
        if not atr or atr <= 0:
            atr = current_price * 0.02

        # Gap-fill levels are high-probability magnets — fold them into the candidate pool.
        if direction == "long":
            swing_above = {l["price"] for l in levels if l["price"] > current_price * 1.005}
            gap_above = set(gap_targets_above(df, current_price))
            above = sorted(swing_above | gap_above)
            picks = above[:3]
        else:
            swing_below = {l["price"] for l in levels if l["price"] < current_price * 0.995}
            gap_below = set(gap_targets_below(df, current_price))
            below = sorted(swing_below | gap_below, reverse=True)
            picks = below[:3]

        # Top up with ATR-stepped projections if we didn't get 3 swing levels
        while len(picks) < 3:
            step = (len(picks) + 1) * 1.5 * atr
            nxt = current_price + step if direction == "long" else current_price - step
            picks.append(round(nxt, 2))

        return [round(float(p), 2) for p in picks[:3]]
    except Exception as e:
        logger.warning(f"_recalculate_targets({ticker}) failed: {e}")
        return None


def _record_target_history(t: AutoTrade, reason: str, new_targets: List[float]) -> None:
    """Append a target-recalc event to the JSON audit log on the trade row."""
    import json as _json
    try:
        existing = _json.loads(t.targets_history) if t.targets_history else []
    except Exception:
        existing = []
    existing.append({
        "at": datetime.utcnow().isoformat(),
        "reason": reason,
        "targets": new_targets,
    })
    t.targets_history = _json.dumps(existing)


def _manage_option_trade(t: AutoTrade, c, db: Session, summary: Dict[str, Any]) -> None:
    """
    Manage a single option auto-trade:
      • Promote pending→open once the buy order fills.
      • Exit conditions (whichever fires first → market sell-to-close):
          – Underlying hits T2 (full profit target)            → status closed_target
          – Underlying hits T1 (first target, lock the win)    → status closed_target
          – Underlying breaches the bear stop (thesis broken)  → status closed_stop
          – Premium decays ≥ 50% from entry (premium stop)     → status closed_stop
      • Reconcile if the option position has gone to zero (already exited).
    """
    parent = c.get_order_by_id(t.parent_order_id)
    pstatus = str(parent.status).lower()

    # 1) Pending → open
    if t.status == "pending":
        if "filled" in pstatus:
            t.entry_price = float(parent.filled_avg_price) if parent.filled_avg_price else t.requested_entry
            t.status = "open"
            t.filled_at = datetime.utcnow()
            db.commit()
            logger.info(f"AutoTrader PUT {t.ticker} {t.symbol} filled @ {t.entry_price}")
        elif any(s in pstatus for s in ("canceled", "rejected", "expired")):
            t.status = "closed_manual"
            t.closed_at = datetime.utcnow()
            t.note = (t.note or "") + f" | option parent {pstatus}"
            db.commit()
            summary["closed"] += 1
        return

    if t.status != "open":
        return

    # 2) Pull underlying & current option price
    px = _current_price(t.ticker)
    if px is None:
        # Fallback to a quick fetch if WS hasn't seen this ticker recently
        try:
            pi = fetch_current_price(t.ticker)
            if pi:
                px = pi[0]
        except Exception:
            pass

    pos = paper_trader.get_option_position(t.symbol)
    cur_premium = pos["current_price"] if pos and pos.get("current_price") else None

    # Parse the option direction from the OCC symbol (8th-from-end char: 'C' or 'P')
    is_put = False
    try:
        # OCC: AAPL250117P00270000  → ...P0027... ; 'P' is the 9th-from-end char
        is_put = "P" in t.symbol[-9:-8] or "P" in t.symbol[-15:-14]
    except Exception:
        pass

    exit_reason = None
    final_status = None

    # 3) State-machine trailing on the UNDERLYING (mirrors stock logic, inverted for puts).
    #    For puts: targets are BELOW current price, "current_stop" is the underlying stop ABOVE.
    #    Crossing T1 (price drops below T1) → tighten underlying stop to entry underlying;
    #    Crossing T2 → underlying stop moves to T1; Crossing T3 → moves to T2 + recalc.
    #    We don't move a real broker stop (options have no SL leg here) — we only
    #    update t.current_stop (the underlying-stop level we'll exit at).
    if px is not None and is_put:
        # Parse entry-underlying from note ("underlying stop $X" + thesis records),
        # but for trailing we just need the current underlying stop in t.current_stop
        # (initialised to the bear-thesis underlying stop when we record it below).
        targets = [t.target1, t.target2, t.target3]
        li = t.level_index or 0
        target_idx = li % 3
        next_target = targets[target_idx]
        if next_target and px <= next_target:
            # Partial profit-taking for options: at T1, sell HALF the contracts
            # to lock in profit (theta-decay risk on remaining halves is now
            # paid for by the realized half). Only trims once.
            if target_idx == 0 and t.qty >= 2 and not t.hit_t1:
                half = int(t.qty // 2)
                if half >= 1:
                    trim = paper_trader.submit_simple_option_order(
                        occ_symbol=t.symbol, qty=half, side="sell",
                        order_type="market", time_in_force="day",
                    )
                    if "error" not in trim:
                        # Capture partial realised P/L
                        if cur_premium is not None and t.entry_price:
                            partial_pl = (cur_premium - t.entry_price) * half * 100
                            t.realized_pl = (t.realized_pl or 0) + partial_pl
                        t.qty = t.qty - half
                        t.hit_t1 = True
                        t.note = (t.note or "") + (
                            f" | PARTIAL: trimmed {half} contracts at T1 (px={px:.2f}); "
                            f"runner = {int(t.qty)} contracts"
                        )
                        logger.info(
                            f"AutoTrader PUT {t.ticker} partial-trim {half} @ underlying {px:.2f}; "
                            f"runner {int(t.qty)} contracts"
                        )
            if target_idx == 0:
                new_stop = round(next_target * 1.02, 2)  # near break-even on underlying
            else:
                prev = targets[target_idx - 1]
                new_stop = round(prev, 2) if prev else t.current_stop
            # For a put, tighter = LOWER upper-stop. Only commit if it's tighter.
            tightened = bool(t.current_stop and new_stop < t.current_stop)
            if tightened:
                t.current_stop = new_stop
            t.level_index = li + 1
            tag = ["T1", "T2", "T3"][target_idx]
            t.note = (t.note or "") + (
                f" | underlying {tag} hit @ {px:.2f}, u-stop→{new_stop}"
                if tightened else
                f" | underlying {tag} hit @ {px:.2f} (u-stop unchanged)"
            )
            if target_idx == 2:
                new_targets = _recalculate_targets(t.ticker, "bear", px)
                if new_targets:
                    t.target1, t.target2, t.target3 = new_targets
                    _record_target_history(
                        t,
                        f"underlying T3 breached @ {px:.2f}; recalc",
                        new_targets,
                    )
                    t.note = (t.note or "") + f" | recalc T1-3: {new_targets}"
            db.commit()
            summary["trailed"] += 1
            logger.info(
                f"AutoTrader PUT {t.ticker} underlying {tag} hit, u-stop→{new_stop} "
                f"(level_index={t.level_index})"
            )

    # 4) Underlying-stop breach (price moved AGAINST the put thesis) → close
    if px is not None and is_put and t.current_stop and px >= t.current_stop:
        exit_reason = f"underlying broke trailing u-stop ${t.current_stop:.2f} (now ${px:.2f})"
        final_status = "closed_stop"

    # 5) Premium decay safety — still exit if the option has lost ≥ 50% of premium
    if not exit_reason and cur_premium is not None and t.entry_price:
        if cur_premium <= t.entry_price * 0.5:
            exit_reason = f"premium decayed to ${cur_premium:.2f} (≥50% of entry ${t.entry_price:.2f})"
            final_status = "closed_stop"

    if exit_reason:
        sell = paper_trader.submit_simple_option_order(
            occ_symbol=t.symbol, qty=int(t.qty), side="sell",
            order_type="market", time_in_force="day",
        )
        if "error" in sell:
            logger.warning(f"option sell failed for {t.symbol}: {sell['error']}")
            return
        # Capture realised P/L from the position before it disappears.
        # Guard against pos=None (option-position lookup can fail) AND against
        # NaN entry_price (rare flake in fill report). Round to 2dp so float
        # noise doesn't flip closed_target ↔ closed_stop downstream.
        try:
            if pos and pos.get("current_price") is not None and t.entry_price:
                t.realized_pl = round((float(pos["current_price"]) - float(t.entry_price)) * float(t.qty) * 100, 2)
            elif cur_premium is not None and t.entry_price:
                t.realized_pl = round((float(cur_premium) - float(t.entry_price)) * float(t.qty) * 100, 2)
            else:
                t.realized_pl = None
                logger.warning(f"option {t.symbol}: closing without P/L (pos={bool(pos)}, cur_premium={cur_premium})")
        except (TypeError, ValueError) as _e:
            t.realized_pl = None
            logger.warning(f"option {t.symbol}: P/L calc skipped: {_e}")
        t.status = final_status or "closed_manual"
        t.closed_at = datetime.utcnow()
        t.note = (t.note or "") + f" | EXIT: {exit_reason}"
        db.commit()
        summary["closed"] += 1
        metrics.inc("autotrade_event", event=t.status)
        logger.info(f"AutoTrader PUT {t.ticker} {t.symbol} closed: {exit_reason} (PL ${t.realized_pl or 0:.2f})")
        if t.status == "closed_stop" and (t.realized_pl or 0) < 0:
            _post_mortem_async(t.id)


REVERSE_CONFIDENCE_GATE = 65.0  # min confidence for an opposing signal to force-close


def check_reversals_for(ticker: str) -> int:
    """
    Evaluate reverse-thesis closure for any open auto-trade on `ticker`. Called
    immediately after a fresh analysis run for that ticker so we can react
    inside the same heartbeat instead of waiting for the next 60s manage tick.
    Returns count of trades closed.
    """
    db = SessionLocal()
    summary = {"closed": 0}
    try:
        cfg = get_config(db)
        if not cfg.enabled or not paper_trader.is_enabled():
            return 0
        trades = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker.upper(),
            AutoTrade.status == "open",
        ).all()
        for t in trades:
            try:
                rev = _check_reversal(t, db)
                if rev:
                    _force_close_trade(t, db, rev, summary)
            except Exception as e:
                logger.warning(f"reversal check error on {t.ticker} #{t.id}: {e}")
        return summary["closed"]
    finally:
        db.close()


# Timeframe rank — higher TF carries more weight. Reverse-thesis only fires
# when the opposing signal is on a TF ≥ the original trade-source TF (a 5m
# blip shouldn't yank a position opened off a 1d signal).
_TF_RANK = {"5m": 1, "15m": 2, "30m": 3, "1h": 4, "4h": 5, "1d": 6, "1mo": 7}


def _trade_source_timeframe(t: AutoTrade, db: Session) -> str:
    """Best-effort lookup of the timeframe that produced this trade's signal."""
    if t.signal_id:
        s = db.query(Signal).filter(Signal.id == t.signal_id).first()
        if s and s.timeframe:
            return s.timeframe
    return "1d"  # safe default


def _check_reversal(t: AutoTrade, db: Session) -> Optional[str]:
    """
    Detect a strong opposing signal that landed AFTER this trade was opened.
    Returns a reason string if we should close, else None.

      • Long stock     → opposing = SELL signal ≥ gate
      • Long put       → opposing = BUY  signal ≥ gate (bull thesis invalidates short)

    The opposing signal must be on a timeframe ≥ the trade's source TF — a 5m
    fakeout shouldn't be allowed to close a 1d-conviction position.
    """
    opened_at = t.filled_at or t.opened_at
    if not opened_at:
        return None
    opposing = "SELL" if t.asset_type == "stock" else "BUY"
    src_tf = _trade_source_timeframe(t, db)
    src_rank = _TF_RANK.get(src_tf, 6)

    # Postmortem fix C3: 60-second grace window. The same `_run_analysis_for_ticker`
    # pass that opened the trade also writes signals across all other timeframes;
    # without a grace period a 1d SELL written milliseconds after the 1h BUY
    # opens would close the brand-new trade in the same heartbeat. 60s is short
    # enough that a real reversal still acts in the next manage tick and long
    # enough to clear out same-pass writes (a multi-TF analyze takes 5-15s).
    from datetime import timedelta as _td_grace
    earliest_valid = opened_at + _td_grace(seconds=60)

    candidates = (
        db.query(Signal)
        .filter(
            Signal.ticker == t.ticker,
            Signal.signal_type == opposing,
            Signal.generated_at > earliest_valid,
            Signal.confidence >= REVERSE_CONFIDENCE_GATE,
        )
        .order_by(desc(Signal.generated_at))
        .all()
    )
    # Postmortem fix M5: a 1d opposing signal CAN reverse a 1mo-source trade.
    # The strict `>=` rule made 1mo trades effectively un-reverse-able because
    # 1mo signals barely change month-to-month. Allow the opposing TF to be
    # one rank below the source TF.
    min_rank = max(1, src_rank - 1)
    for sig in candidates:
        if _TF_RANK.get(sig.timeframe, 0) >= min_rank:
            return (
                f"reverse-thesis {opposing} signal landed @ conf {sig.confidence:.0f} "
                f"on {sig.timeframe} (>= rank {min_rank}, src TF {src_tf}); "
                f"generated {sig.generated_at.isoformat()}"
            )
    return None


def _force_close_trade(
    t: AutoTrade,
    db: Session,
    reason: str,
    summary: Dict[str, Any],
    status_override: Optional[str] = None,
) -> None:
    """Cancel any working broker orders and exit the position at market.

    `status_override` lets callers tag the reason (e.g. "closed_slippage" for
    runaway-fill rejects). Default is "closed_reverse" — see C1 fix.
    """
    if t.asset_type == "stock":
        # Cancel parent bracket (which also cancels its TP/SL legs in Alpaca),
        # then market-close the position.
        try:
            if t.parent_order_id:
                paper_trader.cancel_order(t.parent_order_id)
        except Exception as e:
            logger.warning(f"reverse-close cancel parent failed: {e}")
        res = paper_trader.close_position(t.ticker)
        if "error" in res:
            logger.warning(f"reverse-close {t.ticker} failed: {res['error']}")
            return
        # Realised P/L from current price snapshot
        px = _current_price(t.ticker)
        if px and t.entry_price:
            t.realized_pl = (px - t.entry_price) * t.qty
    else:
        # Option: market sell-to-close
        sell = paper_trader.submit_simple_option_order(
            occ_symbol=t.symbol, qty=int(t.qty), side="sell",
            order_type="market", time_in_force="day",
        )
        if "error" in sell:
            logger.warning(f"reverse-close option {t.symbol} failed: {sell['error']}")
            return
        pos = paper_trader.get_option_position(t.symbol)
        if pos and pos.get("current_price") is not None and t.entry_price:
            t.realized_pl = (pos["current_price"] - t.entry_price) * t.qty * 100

    # Distinct status for reverse-closes — postmortem fix C1: a reverse-close
    # is fundamentally different from "stop hit" or "target hit"; conflating
    # them mis-attributes win/loss stats AND triggers post-mortems on trades
    # that were never given a chance to test their stop. closed_reverse is
    # never post-mortem'd because the close was caused by an exogenous
    # opposing signal, not a stop failure.
    t.status = status_override or "closed_reverse"
    t.closed_at = datetime.utcnow()
    t.note = (t.note or "") + f" | {t.status.upper()}: {reason}"
    # Clean up state-machine bookkeeping (postmortem fix H1).
    _target_touch_counts.pop(t.id, None)
    db.commit()
    summary["closed"] += 1
    metrics.inc("autotrade_event", event=t.status)
    logger.warning(
        f"AutoTrader {t.status.upper()} {t.ticker} ({t.asset_type}) — {reason} "
        f"PL≈${(t.realized_pl or 0):.2f}"
    )


def manage_open_positions() -> Dict[str, Any]:
    """
    Periodic job:
      • For pending entries — promote to 'open' once filled (capture entry_price + leg ids).
      • For open trades — when current price ≥ T1, move stop to entry (break-even).
      • Reconcile closed trades (status sync from broker).
    """
    summary = {"checked": 0, "trailed": 0, "closed": 0, "errors": 0}
    _manage_ctx = metrics.timer("manage")
    _manage_ctx.__enter__()
    # Snapshot config + active trade IDs in a SHORT session, then release the
    # write lock immediately. Each trade is then processed in its own session
    # below, so the Alpaca REST round-trips inside the loop don't hold the
    # SQLite writer lock — that was the source of the "database is locked"
    # contention with the scan thread's INSERT INTO auto_trades.
    try:
        _bootstrap_db = SessionLocal()
        try:
            cfg = get_config(_bootstrap_db)
            if not cfg.enabled or not paper_trader.is_enabled():
                return summary
            # Snapshot only the fields we read inside the loop — detached from session.
            cfg_snapshot = {
                "chandelier_atr_mult": float(getattr(cfg, "chandelier_atr_mult", 0) or 0),
            }
            trade_ids = [
                tid for (tid,) in _bootstrap_db.query(AutoTrade.id).filter(
                    AutoTrade.status.in_(["pending", "open"])
                ).all()
            ]
        finally:
            _bootstrap_db.close()

        c = paper_trader._get_client()
        for trade_id in trade_ids:
            db = SessionLocal()
            try:
                t = db.query(AutoTrade).filter(AutoTrade.id == trade_id).first()
                if t is None:
                    continue
                summary["checked"] += 1
                try:
                    if not t.parent_order_id:
                        continue

                    # 0) Reverse-thesis check — fires for both stocks and options.
                    #    Only acts on trades that have actually filled ("open"), so
                    #    a brand-new pending entry isn't yanked before it fills.
                    if t.status == "open":
                        rev = _check_reversal(t, db)
                        if rev:
                            _force_close_trade(t, db, rev, summary)
                            continue

                    # ===== Option auto-trades take a separate path =====
                    if t.asset_type == "option":
                        _manage_option_trade(t, c, db, summary)
                        continue

                    parent = c.get_order_by_id(t.parent_order_id)
                    pstatus = str(parent.status).lower()

                    # 1) Promote pending → open once parent filled.
                    # B5: strict "filled" match. Previous `"filled" in pstatus`
                    # also matched `"partially_filled"`, which led to DB-qty
                    # mismatch vs actual shares + SL leg sized to the ORIGINAL
                    # order quantity rather than filled_qty. Now: treat
                    # partial-fill separately and reshape SL/TP legs to the
                    # actual filled quantity, so a cancel of the unfilled
                    # remainder won't leave a mis-sized bracket.
                    if t.status == "pending" and pstatus == "partially_filled":
                        filled_qty = float(getattr(parent, "filled_qty", 0) or 0)
                        if filled_qty > 0 and filled_qty < t.qty:
                            logger.warning(
                                f"AutoTrader {t.ticker} partial fill: {filled_qty}/{t.qty}; "
                                f"cancelling remainder + resizing bracket legs"
                            )
                            try:
                                paper_trader.cancel_order(t.parent_order_id)
                            except Exception as e:
                                logger.warning(f"partial-fill cancel remainder failed for {t.ticker}: {e}")
                            t.qty = int(filled_qty)
                            t.note = (t.note or "") + f" | PARTIAL FILL: using {int(filled_qty)} of original qty"
                            db.commit()
                    if t.status == "pending" and pstatus == "filled":
                        if True:
                            legs = _identify_legs(t.parent_order_id)
                            t.entry_price = float(parent.filled_avg_price) if parent.filled_avg_price else t.requested_entry
                            t.stop_order_id = legs.get("stop_id")
                            t.tp_order_id = legs.get("tp_id")
                            t.status = "open"
                            t.filled_at = datetime.utcnow()
                            db.commit()
                            logger.info(f"AutoTrader {t.ticker} filled @ {t.entry_price}")

                            # ----- Slippage check (postmortem fix #1) -----
                            # If the actual fill drifted materially from the
                            # requested entry, the pre-computed targets are
                            # stale relative to "true" risk. Postmortems showed
                            # MU gapped through T1 on the open and the trail
                            # locked us into a chop-out at BE before the move
                            # had any room.
                            try:
                                req_entry = float(t.requested_entry or 0)
                                if req_entry > 0 and t.asset_type == "stock":
                                    slip = float(t.entry_price) - req_entry
                                    atr = _chandelier_atr(t.ticker)
                                    if atr and atr > 0:
                                        atr_units = abs(slip) / atr
                                        if atr_units > _SLIPPAGE_REJECT_ATR:
                                            # Runaway gap fill — flatten now.
                                            logger.warning(
                                                f"AutoTrader {t.ticker} slippage {slip:+.2f} "
                                                f"= {atr_units:.2f}×ATR > {_SLIPPAGE_REJECT_ATR}; "
                                                f"force-closing"
                                            )
                                            _force_close_trade(
                                                t, db,
                                                f"slippage {atr_units:.2f}×ATR exceeds reject threshold",
                                                summary,
                                                status_override="closed_slippage",
                                            )
                                            continue
                                        elif atr_units > _SLIPPAGE_SHIFT_ATR:
                                            # Shift targets + stop so distance-
                                            # from-entry is preserved — but
                                            # CAP the stop so we never tighten
                                            # below the original risk-per-share
                                            # (postmortem fix C2). On positive
                                            # slippage, naively shifting the
                                            # stop UP shrinks the cushion and
                                            # puts us inside the chop range
                                            # the original stop was sized to
                                            # absorb.
                                            old_t = (t.target1, t.target2, t.target3, t.stop_loss)
                                            if t.target1: t.target1 = round(float(t.target1) + slip, 2)
                                            if t.target2: t.target2 = round(float(t.target2) + slip, 2)
                                            if t.target3: t.target3 = round(float(t.target3) + slip, 2)
                                            if t.stop_loss:
                                                # Original risk-per-share from the SIGNAL.
                                                orig_risk = max(0.0, req_entry - float(t.stop_loss))
                                                shifted_stop = float(t.stop_loss) + slip
                                                # Long stops sit BELOW entry; the
                                                # furthest-down (loosest) of the
                                                # two preserves the cushion.
                                                # min() = lower price = looser
                                                # for a long stop.
                                                if orig_risk > 0:
                                                    cap_stop = float(t.entry_price) - orig_risk
                                                    new_stop = min(shifted_stop, cap_stop)
                                                else:
                                                    new_stop = shifted_stop
                                                t.stop_loss = round(new_stop, 2)
                                                if t.stop_order_id and t.stop_loss > 0:
                                                    if _replace_stop(t.stop_order_id, t.stop_loss):
                                                        t.current_stop = t.stop_loss
                                            t.note = (t.note or "") + (
                                                f" | slippage {slip:+.2f} ({atr_units:.2f}×ATR) "
                                                f"shifted T1-3+stop from {old_t} to "
                                                f"({t.target1},{t.target2},{t.target3},{t.stop_loss})"
                                            )
                                            _record_target_history(
                                                t,
                                                f"slippage shift {slip:+.2f} ({atr_units:.2f}×ATR)",
                                                [t.target1 or 0, t.target2 or 0, t.target3 or 0],
                                            )
                                            db.commit()
                                            logger.info(
                                                f"AutoTrader {t.ticker} slippage-shift {slip:+.2f} "
                                                f"({atr_units:.2f}×ATR); new T1-3 = "
                                                f"{t.target1}/{t.target2}/{t.target3}"
                                            )
                            except Exception as e:
                                logger.warning(f"slippage check {t.ticker} failed: {e}")
                        elif "canceled" in pstatus or "rejected" in pstatus or "expired" in pstatus:
                            t.status = "closed_manual"
                            t.closed_at = datetime.utcnow()
                            t.note = (t.note or "") + f" | parent {pstatus}"
                            _target_touch_counts.pop(t.id, None)
                            db.commit()
                            summary["closed"] += 1
                            metrics.inc("autotrade_event", event="closed_manual")
                        continue

                    # B1: SL leg invariant — confirm the broker still holds
                    # our stop order. A dangling bracket (TP filled elsewhere,
                    # manual leg cancel, or Alpaca housekeeping) leaves us
                    # naked-long with no downside protection. If the SL is
                    # missing, resubmit a fresh stop for the current position
                    # so we're never unprotected for more than one manage tick.
                    if t.status == "open" and t.entry_price and t.stop_order_id:
                        try:
                            sl_ord = c.get_order_by_id(t.stop_order_id)
                            sl_status = str(getattr(sl_ord, "status", "")).lower()
                            if sl_status in ("canceled", "filled", "rejected", "expired", "replaced"):
                                if sl_status == "filled":
                                    pass  # stop actually fired — reconcile block below will close
                                else:
                                    # Gone but not filled → we're naked-long.
                                    logger.error(
                                        f"AutoTrader {t.ticker} SL INVARIANT VIOLATION: "
                                        f"stop_order_id {t.stop_order_id} status={sl_status} — resubmitting"
                                    )
                                    from alpaca.trading.requests import StopOrderRequest
                                    from alpaca.trading.enums import OrderSide, TimeInForce
                                    new_stop_price = round(float(t.current_stop or t.stop_loss), 2)
                                    try:
                                        new_sl = c.submit_order(order_data=StopOrderRequest(
                                            symbol=t.ticker,
                                            qty=int(t.qty),
                                            side=OrderSide.SELL,
                                            time_in_force=TimeInForce.GTC,
                                            stop_price=new_stop_price,
                                        ))
                                        t.stop_order_id = str(new_sl.id)
                                        t.note = (t.note or "") + f" | SL invariant: resubmitted stop @ {new_stop_price}"
                                        db.commit()
                                        metrics.inc("autotrade_event", event="sl_resubmitted")
                                    except Exception as _re:
                                        logger.error(f"AutoTrader {t.ticker} SL resubmit FAILED: {_re}")
                        except Exception as _e:
                            logger.warning(f"SL invariant check {t.ticker}: {_e}")

                    # Profit-max: stale-trade guard. Trades that haven't hit T1
                    # after N × timeframe minutes have had their chance — close
                    # them to recycle capital into fresher setups. Only fires
                    # for trades currently at a small loss or flat (no point
                    # closing a winning position just because T1 hasn't hit).
                    if t.status == "open" and t.entry_price and not t.hit_t1 and t.filled_at:
                        try:
                            src_tf = _trade_source_timeframe(t, db)
                            _stale_map = {"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440,"1mo":1440*20}
                            tf_minutes = _stale_map.get(src_tf, 240)
                            age_min = (datetime.utcnow() - t.filled_at).total_seconds() / 60.0
                            max_age_min = _STALE_TRADE_TF_MULT * tf_minutes
                            if age_min > max_age_min:
                                # Only close if the trade is not meaningfully
                                # winning (price below 0.3×R above entry).
                                px_probe = _current_price(t.ticker)
                                entry_px = float(t.entry_price)
                                rps = max(0.01, entry_px - float(t.stop_loss or entry_px))
                                if px_probe is not None and (px_probe - entry_px) < 0.3 * rps:
                                    _force_close_trade(
                                        t, db,
                                        f"stale trade: open {age_min/60:.1f}h without T1 "
                                        f"(max {max_age_min/60:.1f}h for {src_tf})",
                                        summary,
                                        status_override="closed_stale",
                                    )
                                    continue
                        except Exception as _e:
                            logger.warning(f"stale-trade check {t.ticker}: {_e}")

                    # 2) Open trade: state-machine trailing stop (long)
                    if t.status == "open" and t.entry_price and t.stop_order_id:
                        px = _current_price(t.ticker)
                        if px is not None:
                            if not t.high_water_mark or px > t.high_water_mark:
                                t.high_water_mark = px
                        if px is not None:
                            targets = [t.target1, t.target2, t.target3]
                            li = t.level_index or 0
                            target_idx = li % 3
                            next_target = targets[target_idx]
                            if next_target and px >= next_target:
                                # T1 confirmation (postmortem fix #4): require
                                # N consecutive manage-loop ticks above target
                                # so a single 5m wick (MRVL: 148.75 spike then
                                # immediate 146.21 print) doesn't move the stop
                                # to BE and chop us out flat.
                                touches = _target_touch_counts.get(t.id, 0) + 1
                                _target_touch_counts[t.id] = touches
                                if touches < _TARGET_CONFIRM_TICKS:
                                    logger.info(
                                        f"AutoTrader {t.ticker} {['T1','T2','T3'][target_idx]} "
                                        f"touch {touches}/{_TARGET_CONFIRM_TICKS} @ {px:.2f} — awaiting confirmation"
                                    )
                                else:
                                    # Postmortem fix #2: if T1 is too tight (less
                                    # than _T1_BE_MIN_ATR × ATR from entry), the
                                    # break-even move is meaningless — a normal
                                    # pullback chops us out for $0. Skip the BE
                                    # but still advance level_index so the
                                    # chandelier overlay takes over.
                                    skip_stop_move = False
                                    if target_idx == 0:
                                        atr_be = _chandelier_atr(t.ticker)
                                        if atr_be and (next_target - t.entry_price) < _T1_BE_MIN_ATR * atr_be:
                                            skip_stop_move = True
                                            logger.info(
                                                f"AutoTrader {t.ticker} T1 too tight "
                                                f"({(next_target - t.entry_price):.2f} < "
                                                f"{_T1_BE_MIN_ATR}×ATR({atr_be:.2f})) — skipping BE, "
                                                f"chandelier will trail"
                                            )
                                            t.level_index = li + 1
                                            t.hit_t1 = True
                                            t.note = (t.note or "") + (
                                                f" | T1 hit @ {px:.2f}, BE skipped (T1 too tight); "
                                                f"chandelier active"
                                            )
                                            _target_touch_counts.pop(t.id, None)
                                            db.commit()

                                    if not skip_stop_move:
                                        if target_idx == 0:
                                            new_stop = round(t.entry_price, 2)
                                        else:
                                            prev = targets[target_idx - 1]
                                            new_stop = round(prev, 2) if prev else t.current_stop
                                        # F3: Partial profit-taking on T1 for
                                        # stocks — sell qty//3 at market to
                                        # lock in realized gains, then resize
                                        # the SL leg to the remainder. Only
                                        # once per trade (guarded by hit_t1).
                                        if target_idx == 0 and not t.hit_t1 and t.qty >= 3:
                                            trim_qty = int(t.qty // 3)
                                            if trim_qty >= 1:
                                                try:
                                                    from alpaca.trading.requests import MarketOrderRequest
                                                    from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                                                    trim_res = c.submit_order(order_data=MarketOrderRequest(
                                                        symbol=t.ticker, qty=trim_qty,
                                                        side=_OS.SELL, time_in_force=_TIF.DAY,
                                                    ))
                                                    trim_ok = trim_res is not None
                                                except Exception as _te:
                                                    logger.warning(f"F3 partial-trim submit failed for {t.ticker}: {_te}")
                                                    trim_ok = False
                                                if trim_ok:
                                                    realized_partial = (px - float(t.entry_price)) * trim_qty
                                                    t.realized_pl = (t.realized_pl or 0.0) + round(realized_partial, 2)
                                                    t.qty = t.qty - trim_qty
                                                    t.note = (t.note or "") + (
                                                        f" | PARTIAL: trimmed {trim_qty} shares at T1 "
                                                        f"(px={px:.2f}, +${realized_partial:.2f}); "
                                                        f"runner = {t.qty} shares"
                                                    )
                                                    # Resize SL leg to remaining qty.
                                                    try:
                                                        from alpaca.trading.requests import ReplaceOrderRequest
                                                        c.replace_order_by_id(
                                                            t.stop_order_id,
                                                            order_data=ReplaceOrderRequest(qty=int(t.qty)),
                                                        )
                                                    except Exception as _re:
                                                        logger.warning(f"F3 SL-qty resize failed for {t.ticker}: {_re}")
                                                    metrics.inc("autotrade_event", event="partial_t1")
                                        # Profit-max: T2 partial profit — trim half the remaining
                                        # runner at T2 to lock in another chunk of gains while still
                                        # leaving a runner for T3 and recalc extensions.
                                        if target_idx == 1 and t.qty >= 2:
                                            trim_qty = max(1, int(t.qty * _T2_PARTIAL_FRAC))
                                            if trim_qty < t.qty:
                                                try:
                                                    from alpaca.trading.requests import MarketOrderRequest
                                                    from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                                                    trim_res = c.submit_order(order_data=MarketOrderRequest(
                                                        symbol=t.ticker, qty=trim_qty,
                                                        side=_OS.SELL, time_in_force=_TIF.DAY,
                                                    ))
                                                    trim_ok = trim_res is not None
                                                except Exception as _te:
                                                    logger.warning(f"T2 partial-trim submit failed for {t.ticker}: {_te}")
                                                    trim_ok = False
                                                if trim_ok:
                                                    realized_partial = (px - float(t.entry_price)) * trim_qty
                                                    t.realized_pl = (t.realized_pl or 0.0) + round(realized_partial, 2)
                                                    t.qty = t.qty - trim_qty
                                                    t.note = (t.note or "") + (
                                                        f" | PARTIAL: trimmed {trim_qty} shares at T2 "
                                                        f"(px={px:.2f}, +${realized_partial:.2f}); "
                                                        f"runner = {t.qty} shares"
                                                    )
                                                    try:
                                                        from alpaca.trading.requests import ReplaceOrderRequest
                                                        c.replace_order_by_id(
                                                            t.stop_order_id,
                                                            order_data=ReplaceOrderRequest(qty=int(t.qty)),
                                                        )
                                                    except Exception as _re:
                                                        logger.warning(f"T2 SL-qty resize failed for {t.ticker}: {_re}")
                                                    metrics.inc("autotrade_event", event="partial_t2")
                                        if new_stop > t.current_stop and _replace_stop(t.stop_order_id, new_stop):
                                            t.current_stop = new_stop
                                            t.level_index = li + 1
                                            if target_idx == 0:
                                                t.hit_t1 = True
                                            tag = ["T1", "T2", "T3"][target_idx]
                                            t.note = (t.note or "") + f" | {tag} hit @ {px:.2f}, stop→{new_stop}"
                                            if target_idx == 2:
                                                new_targets = _recalculate_targets(t.ticker, "long", px)
                                                if new_targets:
                                                    t.target1, t.target2, t.target3 = new_targets
                                                    _record_target_history(
                                                        t,
                                                        f"T3 breached @ {px:.2f}; recalc from price",
                                                        new_targets,
                                                    )
                                                    t.note = (t.note or "") + f" | recalc T1-3: {new_targets}"
                                            _target_touch_counts.pop(t.id, None)
                                            db.commit()
                                            summary["trailed"] += 1
                                            logger.info(
                                                f"AutoTrader {t.ticker} {tag} hit, stop→{new_stop} "
                                                f"(level_index={t.level_index})"
                                            )
                            elif next_target and t.id in _target_touch_counts:
                                # Price fell back below the target before we
                                # got N confirmations — reset the streak.
                                _target_touch_counts.pop(t.id, None)

                            # 2c) Chandelier-exit overlay
                            ch_mult = cfg_snapshot["chandelier_atr_mult"]
                            if ch_mult > 0 and (t.level_index or 0) >= 1 and t.high_water_mark:
                                _atr = _chandelier_atr(t.ticker)
                                if _atr is not None:
                                    chandelier_stop = round(t.high_water_mark - ch_mult * _atr, 2)
                                    if chandelier_stop > t.current_stop and _replace_stop(t.stop_order_id, chandelier_stop):
                                        old_stop = t.current_stop
                                        t.current_stop = chandelier_stop
                                        t.note = (t.note or "") + (
                                            f" | chandelier trail HWM ${t.high_water_mark:.2f} "
                                            f"-{ch_mult:.1f}×ATR(${_atr:.2f}) → stop {chandelier_stop} "
                                            f"(from {old_stop})"
                                        )
                                        db.commit()
                                        summary["trailed"] += 1
                                        logger.info(
                                            f"AutoTrader {t.ticker} chandelier trail → {chandelier_stop} "
                                            f"(HWM={t.high_water_mark}, ATR={_atr:.2f})"
                                        )

                    # 3) Reconcile: if parent or both legs closed, close the trade
                    if t.status == "open":
                        legs = list(parent.legs or [])
                        leg_states = [str(l.status).lower() for l in legs]
                        filled_legs = [s for s in leg_states if "filled" in s]
                        if filled_legs and legs:
                            exit_leg = next((l for l in legs if "filled" in str(l.status).lower()), None)
                            exit_px = float(exit_leg.filled_avg_price) if exit_leg and exit_leg.filled_avg_price else None
                            if exit_px is not None and t.entry_price is not None:
                                t.realized_pl = round((exit_px - (t.entry_price or 0)) * t.qty, 2)
                                if (t.realized_pl or 0) > 0:
                                    t.status = "closed_target"
                                else:
                                    t.status = "closed_stop"
                                t.closed_at = datetime.utcnow()
                                # Postmortem fix H1: clean up state-machine
                                # bookkeeping so the touch-counts dict doesn't
                                # leak by trade id over months of operation.
                                _target_touch_counts.pop(t.id, None)
                                db.commit()
                                summary["closed"] += 1
                                metrics.inc("autotrade_event", event=t.status)
                                logger.info(f"AutoTrader {t.ticker} closed @ {exit_px} ({t.status}) PL={t.realized_pl:.2f}")
                                if t.status == "closed_stop" and (t.realized_pl or 0) < 0:
                                    _post_mortem_async(t.id)
                except (ConnectionError, ConnectionResetError, TimeoutError) as e:
                    # Transient network blip against Alpaca (TLS reset, socket
                    # timeout). The next manage tick (60s later) will retry.
                    # WARNING without traceback — traceback adds ~15 lines of
                    # noise for a known-transient failure mode.
                    summary["errors"] += 1
                    logger.warning(f"manage transient net error for {t.ticker}: {e}")
                except Exception as e:
                    # Unwrap requests/urllib3 ConnectionError which inherits from
                    # IOError, not ConnectionError — check str-repr as fallback.
                    _es = str(e).lower()
                    if any(s in _es for s in ("connection aborted", "connection reset",
                                              "connection refused", "read timed out",
                                              "temporary failure in name resolution")):
                        summary["errors"] += 1
                        logger.warning(f"manage transient net error for {t.ticker}: {e}")
                    else:
                        summary["errors"] += 1
                        # logger.exception so the traceback lands in the log file —
                        # critical for diagnosing the rare manage-loop failure.
                        logger.exception(f"manage error for {t.ticker}: {e}")
            finally:
                db.close()
    finally:
        try:
            _manage_ctx.__exit__(None, None, None)
        except Exception:
            pass
    return summary
