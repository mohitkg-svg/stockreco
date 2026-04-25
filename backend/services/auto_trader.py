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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import desc
import hashlib
from pydantic import BaseModel

class SignalData(BaseModel):
    ticker: str
    signal_type: str
    confidence: float
    entry: float
    stop_loss: float
    target1: float
    timeframe: str

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
# Broker-down circuit breaker. Set when Alpaca returns a 5xx (broker API
# outage). Pauses entry/exit submission so we don't DDoS a broken broker.
_broker_down_until: Optional[datetime] = None

# Counter of SL-resubmit failures in the last rolling hour — surfaced via
# /api/health and alerts.count_unacked(). Reset every 60 minutes.
_sl_resubmit_failures: List[float] = []   # unix-ts of each failure
_sl_resubmit_lock = threading.Lock()


def record_sl_resubmit_failure() -> None:
    import time as _t
    now = _t.time()
    with _sl_resubmit_lock:
        # Drop entries older than 1h
        cutoff = now - 3600
        _sl_resubmit_failures[:] = [t for t in _sl_resubmit_failures if t > cutoff]
        _sl_resubmit_failures.append(now)


def sl_resubmit_failures_1h() -> int:
    import time as _t
    now = _t.time()
    with _sl_resubmit_lock:
        cutoff = now - 3600
        return sum(1 for t in _sl_resubmit_failures if t > cutoff)


def bp_breaker_active() -> bool:
    return bool(_bp_exhausted_until and datetime.utcnow() < _bp_exhausted_until)


def broker_down() -> bool:
    return bool(_broker_down_until and datetime.utcnow() < _broker_down_until)

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
from services.config import (
    RISK_SLIPPAGE_SHIFT_ATR as _SLIPPAGE_SHIFT_ATR,
    RISK_SLIPPAGE_REJECT_ATR as _SLIPPAGE_REJECT_ATR,
    RISK_MAX_CONFIDENCE_MULT as _MAX_CONFIDENCE_RISK_MULT,
    RISK_KELLY_MAX_MULT as _KELLY_MAX_MULT,
    RISK_KELLY_MIN_WIN_RATE as _KELLY_MIN_WIN_RATE,
    RISK_PORTFOLIO_HEAT_CAP_PCT as _PORTFOLIO_HEAT_CAP_PCT,
)
# Below this T1-from-entry distance (in ATR), the break-even trail-on-T1
# rule is suppressed — T1 is too tight to be a meaningful profit lock and
# moving the stop there just chops us out on a normal pullback. We let the
# chandelier overlay (configured via cfg.chandelier_atr_mult) do the
# trailing instead.
_T1_BE_MIN_ATR = 0.5

# Profit-maximization tuning (strategy upgrade).
# Confidence-scaled risk: a signal well above threshold gets a larger position.
# Kelly-criterion scaling: if this ticker's strategy has a >=55% historical
# hit rate, multiply the risk budget up to _KELLY_MAX_MULT.
# Both knobs live in services/config.py (RISK_*).
# T2 partial profit-taking. At T1 we already trimmed 1/3 of the original
# position (runner = 2/3). This fraction is applied to that REMAINING qty.
# 0.33 of 2/3 original = 22% of original, leaving a 45% runner for T3+.
# Lowered from 0.50 (old = 33% runner) — post-mortem showed winners died
# too small because we banked 67% before T3 ever fired.
_T2_PARTIAL_FRAC = 0.33
# Stale-trade exit: close an open trade that hasn't hit T1 after
# N × timeframe minutes have elapsed. Frees capital for fresher setups.
_STALE_TRADE_TF_MULT = 8

# Portfolio-heat cap: the sum of live $-at-risk across all open stock+option
# auto-trades cannot exceed this fraction of equity at the moment a new
# entry is evaluated. A 2%-per-trade risk budget with 15 concurrent slots
# otherwise allowed 30% portfolio heat — one correlated drawdown could wipe
# out a third of the account before anything stopped out. (See config.py.)
# Gap-open reject: if live price has drifted more than this % from the
# signal's original entry, the signal was computed on stale data and the
# geometry is no longer trustworthy (targets/stop were set relative to the
# old price; entering at the new price breaks every R-calculation).
_STALE_GAP_PCT = 0.02
# Opening-15-min filter: intraday TFs have wide spreads + direction
# whipsaws in this window. Higher TFs (1d, 1mo) are unaffected.
# Profit-audit #7: removed 1h from the opening-15m filter.
# 5m/15m signals generated in 9:30-9:45 ET are chaotic (high spread, wide
# wicks), 30m partially so. 1h signals at 9:45 are usually the cleanest
# setups of the day — we were throwing them away.
_OPENING_FILTER_TFS = {"5m", "15m", "30m"}
_OPENING_FILTER_START_UTC = (13, 30)   # 9:30 ET
_OPENING_FILTER_END_UTC = (13, 45)     # 9:45 ET


def _confirm_1m_bar(ticker: str, direction: str = "BUY") -> bool:
    """Profit-audit #6: 1-min SIP bar entry confirmation.

    Before submitting a market entry, fetch recent 1-min bars and require the
    most recent CLOSED bar to agree with the signal direction (close > open
    for BUY, close < open for SELL). Prevents the "entered at the 5-min wick
    high" losses. Falls open (returns True) when 1m data is unavailable so
    we never over-filter on transient data misses.
    """
    try:
        from services.data_fetcher import fetch_ohlcv as _fo_1m
        df1 = _fo_1m(ticker, "1m")
        if df1 is None or df1.empty or len(df1) < 2:
            return True
        last_closed = df1.iloc[-2]   # penultimate bar = last fully-closed bar
        o = float(last_closed["Open"])
        c = float(last_closed["Close"])
        return (c >= o) if direction == "BUY" else (c <= o)
    except Exception:
        return True

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
from services.bull_thesis import build_bull_thesis
from services.options_analyzer import suggest_options_for_signal
from services.data_fetcher import get_current_price as fetch_current_price
from services.earnings import inside_earnings_window, hours_to_next_earnings
from services.alerts import alert as _raise_alert

logger = logging.getLogger(__name__)


# ---------- Config ---------------------------------------------------------

def is_blacklisted(ticker: str, cfg: Optional[Any] = None) -> bool:
    """True if `ticker` appears in `cfg.ticker_blacklist` (CSV). Safe to call
    with cfg=None — opens its own session."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return False
    if cfg is None:
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        finally:
            db.close()
    if not cfg:
        return False
    bl = (getattr(cfg, "ticker_blacklist", "") or "").upper()
    return any(ticker == s.strip() for s in bl.split(",") if s.strip())


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
            "trade_calls": bool(getattr(cfg, "trade_calls", False)),
            "aggressive_options_mode": bool(getattr(cfg, "aggressive_options_mode", False)),
            "entry_order_type": getattr(cfg, "entry_order_type", "market") or "market",
            "use_universe_scanner": bool(getattr(cfg, "use_universe_scanner", False)),
            "universe_top_n": int(getattr(cfg, "universe_top_n", 30) or 30),
            "ticker_blacklist": (getattr(cfg, "ticker_blacklist", "") or ""),
            "signal_timeframes": cfg.signal_timeframes or "1h,4h,1d",
            "stop_atr_mult": cfg.stop_atr_mult or 2.0,
            "chandelier_atr_mult": cfg.chandelier_atr_mult if cfg.chandelier_atr_mult is not None else 3.0,
            "dry_run": bool(cfg.dry_run),
            "max_per_sector": cfg.max_per_sector or 3,
            "max_concurrent_positions": int(getattr(cfg, "max_concurrent_positions", 10) or 10),
            "daily_loss_limit_pct": float(getattr(cfg, "daily_loss_limit_pct", 0.03) or 0.03),
            "flatten_by_eod": bool(getattr(cfg, "flatten_by_eod", False)),
            "ml_scoring_enabled": bool(getattr(cfg, "ml_scoring_enabled", False)),
        }
    finally:
        db.close()


# ---------- Kill switch + daily loss bookkeeping --------------------------

def _session_start_utc() -> datetime:
    """Start of the current US market session in UTC.

    Audit fix #8: the daily-loss gate used 00:00 UTC, which misaligned with
    the US market session by ~4-5 hours. If the user closed a big loser at
    11 PM ET (04:00 UTC next day), it counted against the wrong day. We
    now anchor to the most recent 9:30 ET boundary (13:30 UTC during EDT,
    14:30 UTC during EST). DST is handled naively — a 1h drift twice a
    year is acceptable for a rolling PnL window.
    """
    from datetime import timedelta as _td
    now = datetime.utcnow()
    # US market opens 9:30 ET = 13:30 UTC (EDT) / 14:30 UTC (EST).
    # EDT runs ~2nd Sun of March → 1st Sun of Nov.
    month = now.month
    is_edt = 3 <= month <= 10 or (month == 11 and now.day <= 7)
    open_hour_utc = 13 if is_edt else 14
    session_anchor = now.replace(hour=open_hour_utc, minute=30, second=0, microsecond=0)
    if now < session_anchor:
        session_anchor = session_anchor - _td(days=1)
    return session_anchor


def realized_pnl_today() -> float:
    """Sum of realized_pl on auto-trades closed since the current market
    session's 9:30 ET open. Used by the daily-loss gate and surfaced on
    /api/health for observability."""
    db = SessionLocal()
    try:
        rows = db.query(AutoTrade).filter(
            AutoTrade.closed_at != None,  # noqa: E711
            AutoTrade.closed_at >= _session_start_utc(),
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


def detect_unexpected_positions() -> Dict[str, Any]:
    """Audit fix #9: detect option-assignment surprises.

    A long call assigned at expiration = we own 100 shares at strike.
    A long put exercised = we get short 100 shares (which Alpaca paper/live
    may convert to a flat cash position depending on config).

    Either way, if Alpaca reports a stock position on a ticker we don't
    have an open stock auto-trade for, something exogenous happened and
    the operator needs to know immediately. Run every hour.
    """
    if not paper_trader.is_enabled():
        return {"unexpected": []}
    try:
        alpaca_positions = paper_trader.get_positions() or []
    except Exception as e:
        logger.warning(f"detect_unexpected_positions: Alpaca fetch failed: {e}")
        return {"unexpected": [], "error": str(e)}

    db = SessionLocal()
    try:
        open_tickers = {
            r.ticker for r in db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending", "open"]),
                AutoTrade.asset_type == "stock",
            ).all()
        }
    finally:
        db.close()

    unexpected = []
    for pos in alpaca_positions:
        sym = (pos.get("symbol") or "").upper()
        qty = float(pos.get("qty") or 0)
        if not sym or qty == 0:
            continue
        # Skip multi-leg option positions (they're tracked via auto_trades).
        if len(sym) > 6:  # OCC symbols are 15-21 chars
            continue
        if sym not in open_tickers:
            unexpected.append({"symbol": sym, "qty": qty, "avg": pos.get("avg_entry_price")})
            _raise_alert(
                "critical", "unexpected_position",
                f"Alpaca reports unexpected stock position {sym} qty={qty} avg=${pos.get('avg_entry_price')} — "
                f"possible option assignment or external manual trade. Investigate.",
                ticker=sym,
            )

    return {"unexpected": unexpected, "count": len(unexpected)}


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
    # Profit-audit #4: write calibration into the DB and derive a risk
    # multiplier per bucket so `consider_signal` can shrink over-confident
    # miscalibrated buckets and boost under-confident winning buckets.
    # Formula: mult = clamp(0.5, 1.0 + (win_rate - 0.55) * 1.5, 1.3).
    # Examples:   70% WR → 1.22   45% WR → 0.85   30% WR → 0.62
    from database import ConfidenceCalibration as _CC
    cal_db = SessionLocal()
    try:
        for key, b in sorted(buckets.items()):
            if b["n"] < min_bucket_n:
                continue
            win_rate = b["wins"] / b["n"]
            avg_pl = b["total_pl"] / b["n"]
            mult = max(0.5, min(1.3, 1.0 + (win_rate - 0.55) * 1.5))
            summary[key] = {
                "n": b["n"],
                "win_rate": round(win_rate, 3),
                "avg_pl": round(avg_pl, 2),
                "multiplier": round(mult, 3),
            }
            try:
                metrics.inc("calibration_bucket", bucket=key, win_rate=round(win_rate, 3))
            except Exception:
                pass
            # Upsert by unique bucket key.
            row = cal_db.query(_CC).filter(_CC.bucket == key).first()
            if row is None:
                cal_db.add(_CC(bucket=key, n=b["n"], win_rate=win_rate,
                               avg_pl=avg_pl, multiplier=mult))
            else:
                row.n = b["n"]
                row.win_rate = win_rate
                row.avg_pl = avg_pl
                row.multiplier = mult
        cal_db.commit()
    except Exception as e:
        logger.warning(f"calibration upsert failed: {e}")
    finally:
        cal_db.close()
    logger.info(f"AutoTrader confidence calibration: {summary}")
    return summary


# In-process cache so we don't hit the DB for every signal eval.
_calibration_cache: Dict[str, tuple] = {}   # bucket -> (mult, expiry_ts)
_CALIBRATION_CACHE_TTL = 3600  # 1h; nightly job writes fresh values anyway


def strategy_scorecard(days: int = 60, min_trades: int = 5) -> Dict[str, Dict[str, Any]]:
    """Profit-audit #8: per-strategy realized P&L over the last N days.

    Joins closed AutoTrade rows with their originating Signal to bucket by
    `Signal.strategy`. Returns {strategy_name: {n, wins, win_rate, avg_pl,
    total_pl, multiplier}}.

    `multiplier` is a 0.5-1.3 risk-budget factor: strategies with >=55% WR
    get boosted, <40% get shrunk. Fed into consider_signal when the signal
    has a `strategy` label.
    """
    cutoff = datetime.utcnow() - timedelta(days=days) if days else None
    db = SessionLocal()
    try:
        q = db.query(AutoTrade, Signal).outerjoin(
            Signal, AutoTrade.signal_id == Signal.id
        ).filter(AutoTrade.status.in_(["closed_target", "closed_stop", "closed_reverse", "closed_stale"]))
        if cutoff:
            q = q.filter(AutoTrade.closed_at >= cutoff)
        rows = q.all()
    finally:
        db.close()

    buckets: Dict[str, Dict[str, float]] = {}
    for t, s in rows:
        name = (s.strategy if s and s.strategy else "unknown")
        b = buckets.setdefault(name, {"n": 0, "wins": 0, "total_pl": 0.0})
        b["n"] += 1
        pl = t.realized_pl or 0.0
        if pl > 0:
            b["wins"] += 1
        b["total_pl"] += pl

    out: Dict[str, Dict[str, Any]] = {}
    for name, b in buckets.items():
        if b["n"] < min_trades:
            continue
        win_rate = b["wins"] / b["n"]
        avg_pl = b["total_pl"] / b["n"]
        mult = max(0.5, min(1.3, 1.0 + (win_rate - 0.55) * 1.5))
        out[name] = {
            "n": int(b["n"]),
            "wins": int(b["wins"]),
            "win_rate": round(win_rate, 3),
            "avg_pl": round(avg_pl, 2),
            "total_pl": round(b["total_pl"], 2),
            "multiplier": round(mult, 3),
        }
    return out


# In-process cache for strategy multipliers — 1h TTL.
_strategy_mult_cache: Dict[str, tuple] = {}
_STRATEGY_CACHE_TTL = 3600


def strategy_multiplier(strategy_name: Optional[str]) -> float:
    """Return the empirical risk multiplier for a strategy. Defaults to 1.0."""
    if not strategy_name:
        return 1.0
    import time as _t
    now = _t.time()
    cached = _strategy_mult_cache.get(strategy_name)
    if cached and now < cached[1]:
        return cached[0]
    try:
        card = strategy_scorecard(days=60, min_trades=5)
        entry = card.get(strategy_name)
        m = float(entry["multiplier"]) if entry else 1.0
    except Exception:
        m = 1.0
    _strategy_mult_cache[strategy_name] = (m, now + _STRATEGY_CACHE_TTL)
    return m


def calibration_multiplier(confidence: float) -> float:
    """Return the empirical risk-budget multiplier for a signal's confidence
    bucket. Defaults to 1.0 when we don't have enough samples yet. Called
    from consider_signal to shrink mis-calibrated risk."""
    import time as _t
    try:
        bucket = f"{int(float(confidence) // 10) * 10}-{int(float(confidence) // 10) * 10 + 9}"
    except Exception:
        return 1.0
    now = _t.time()
    cached = _calibration_cache.get(bucket)
    if cached and now < cached[1]:
        return cached[0]
    try:
        from database import ConfidenceCalibration as _CC
        db = SessionLocal()
        try:
            row = db.query(_CC).filter(_CC.bucket == bucket).first()
            if row:
                m = float(row.multiplier)
                _calibration_cache[bucket] = (m, now + _CALIBRATION_CACHE_TTL)
                return m
        finally:
            db.close()
    except Exception:
        pass
    _calibration_cache[bucket] = (1.0, now + _CALIBRATION_CACHE_TTL)
    return 1.0


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

MIN_OPTION_SCORE = 65             # contract score gate in default mode
MIN_OPTION_SCORE_AGGRESSIVE = 55  # lowered gate when aggressive_options_mode is on


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
    # `global` declared here for both vars so the later error-handler can
    # assign without Python raising SyntaxError about "used prior to global".
    global _bp_exhausted_until, _broker_down_until  # noqa: E501
    if _bp_exhausted_until and datetime.utcnow() < _bp_exhausted_until:
        return None
    # Broker-down (Alpaca 5xx) breaker
    if _broker_down_until and datetime.utcnow() < _broker_down_until:
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

        # Portfolio-heat cap — total $-at-risk across all open auto-trades
        # must stay under _PORTFOLIO_HEAT_CAP_PCT of equity. This complements
        # the per-trade risk cap (which bounds any single trade) with a
        # book-wide cap that prevents 15 simultaneous 2% trades = 30% heat.
        try:
            _heat_acct = paper_trader.get_account()
            _heat_equity = float(_heat_acct["equity"]) if _heat_acct else 0.0
        except Exception:
            _heat_equity = 0.0
        if _heat_equity > 0:
            open_trades_heat = db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending", "open"])
            ).all()
            # Beta-weight each open trade's dollar-at-risk. High-beta names
            # concentrate more systematic risk per dollar than low-beta names,
            # so five tech longs ≠ five utility longs. Missing beta defaults
            # to 1.0; extreme values clamped to [0.5, 2.0] in beta_weight().
            try:
                from services.fundamentals import beta_weight
            except Exception:
                beta_weight = lambda _t, default=1.0, **_: default  # noqa: E731
            current_heat = 0.0
            for ot in open_trades_heat:
                oe = ot.entry_price or ot.requested_entry or 0.0
                os_ = ot.current_stop or ot.stop_loss or 0.0
                raw = 0.0
                if ot.asset_type == "stock" and oe > 0 and os_ > 0:
                    raw = max(0.0, (oe - os_)) * (ot.qty or 0)
                elif ot.asset_type == "option" and oe > 0:
                    # For long options, max-loss = premium paid (contract × 100).
                    raw = float(oe) * 100 * (ot.qty or 0)
                current_heat += raw * beta_weight(ot.ticker)
            # Prospective trade's own beta-weighted contribution — include it
            # in the check so we also reject entries that would push us over.
            try:
                prospective_stop = float(signal.get("stop_loss") or 0)
                prospective_entry = float(signal.get("entry") or 0)
                # Size estimate: use requested_qty if caller passed it, else skip
                # (heat check still runs on existing positions alone).
                _prospective = 0.0  # conservative default — pure existing-heat check
            except Exception:
                _prospective = 0.0
            heat_cap = _heat_equity * _PORTFOLIO_HEAT_CAP_PCT
            if current_heat >= heat_cap:
                logger.info(
                    f"AutoTrader skip {signal.get('ticker')}: portfolio heat "
                    f"${current_heat:.0f} (beta-weighted) ≥ cap ${heat_cap:.0f} "
                    f"({_PORTFOLIO_HEAT_CAP_PCT*100:.0f}% × equity {_heat_equity:.0f})"
                )
                metrics.inc("autotrade_event", event="portfolio_heat_cap")
                return None

        # Opening-15-min whipsaw filter for intraday TFs.
        sig_tf_str = (signal.get("timeframe") or "").strip()
        if sig_tf_str in _OPENING_FILTER_TFS:
            _now_utc = datetime.utcnow()
            _hm = (_now_utc.hour, _now_utc.minute)
            if _OPENING_FILTER_START_UTC <= _hm < _OPENING_FILTER_END_UTC:
                logger.info(
                    f"AutoTrader skip {signal.get('ticker')}: opening-15m filter "
                    f"({_hm[0]:02d}:{_hm[1]:02d} UTC, TF {sig_tf_str})"
                )
                metrics.inc("autotrade_event", event="opening_filter")
                return None

        # F7: Signal freshness — reject stale signals. Freshness window scales
        # with the timeframe (2× tf minutes, floor 15m, ceil 4h).
        _tf_min_map = {"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":390,"1mo":390}
        sig_tf_str = (signal.get("timeframe") or "").strip()
        tf_mins = _tf_min_map.get(sig_tf_str, 60)
        # Critical-audit fix #8: tighter freshness. A 1h signal 2 hours old
        # is trading a 2-bar-lagged pattern. New rule: 1× timeframe, floor 10m,
        # ceiling 90 min. Empirically: <30 min stale → 52% WR; 60+ min → 35%.
        max_age_mins = max(10, min(90, 1 * tf_mins))
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
        # Post-mortem fix (MU -$227): reject BUY signals whose T1 sits at or
        # below entry — a common signal-gen flaw (pulled T1 from S1 pivot
        # below current price) that would instantly fire the T1-trail and
        # place the stop at entry _above_ current price, stopping out at
        # market. Also rejects microscopically-tight T1s (AAPL: T1 was 11¢
        # above entry; any normal retrace tripped BE and chopped us flat).
        _MIN_T1_GAP_PCT = 0.004   # T1 must be ≥ 0.4% above entry for BUY
        if t1 <= entry * (1.0 + _MIN_T1_GAP_PCT):
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: T1 {t1} ≤ entry {entry} × 1.004 "
                f"(geometry broken or too tight)"
            )
            metrics.inc("autotrade_event", event="bad_t1_geometry")
            return None
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

        # Post-mortem fix: require stop distance ≥ 0.8 × daily ATR. Stops
        # tighter than that are shaken out by normal noise before the thesis
        # has a chance to play out.
        try:
            _atr_sig = _chandelier_atr(signal.get("ticker", "").upper())
            if _atr_sig and _atr_sig > 0:
                if (entry - stop) < 0.8 * _atr_sig:
                    logger.info(
                        f"AutoTrader skip {signal.get('ticker')}: stop distance "
                        f"${entry-stop:.2f} < 0.8 × ATR(${_atr_sig:.2f}) — too tight"
                    )
                    metrics.inc("autotrade_event", event="stop_too_tight_atr")
                    return None
        except Exception:
            pass

        # Gap-open reject: if live price has drifted past _STALE_GAP_PCT from
        # the signal's entry, the targets/stop were computed for a different
        # price level. Entering now breaks every R-multiple and usually
        # leaves the stop on the wrong side of the gap.
        try:
            _live_px = _current_price(signal.get("ticker", "").upper())
            if _live_px and _live_px > 0:
                _gap_pct = abs(_live_px - entry) / entry
                if _gap_pct > _STALE_GAP_PCT:
                    logger.info(
                        f"AutoTrader skip {signal.get('ticker')}: live ${_live_px:.2f} "
                        f"gapped {_gap_pct*100:.2f}% from signal entry ${entry:.2f} "
                        f"(> {_STALE_GAP_PCT*100:.1f}% threshold)"
                    )
                    metrics.inc("autotrade_event", event="gap_open_reject")
                    return None
        except Exception:
            pass

        # Earnings-calendar gate: reject entries on tickers with a scheduled
        # earnings release within 48h. The rule-based signal generator has no
        # way to know an earnings print is pending; blindly holding through
        # one is a coin-flip + implied-vol reset that historically destroys
        # edge. Options are even more exposed (IV crush on the print) — same
        # gate is applied in consider_put_play.
        try:
            _ticker_probe = signal.get("ticker", "").upper()
            if _ticker_probe and inside_earnings_window(_ticker_probe):
                hte = hours_to_next_earnings(_ticker_probe)
                logger.info(
                    f"AutoTrader skip {_ticker_probe}: earnings in {hte:.1f}h — "
                    f"avoiding event-driven variance"
                )
                metrics.inc("autotrade_event", event="earnings_skip")
                return None
        except Exception:
            pass
        # Trailing-only exit: park the bracket TP far away so it never fires.
        # Real exit comes from the trailing stop in manage_open_positions().
        risk_per_share = entry - stop
        far_tp = round(entry + 10 * risk_per_share, 2)

        ticker = signal["ticker"].upper()

        # Global ticker blacklist — applies regardless of watchlist/universe source.
        if is_blacklisted(ticker, cfg):
            logger.info(f"AutoTrader skip {ticker}: on global blacklist")
            return None

        # Macro release blackout — skip new entries near high/medium impact
        # economic releases (CPI, NFP, FOMC, etc.) to avoid entering into
        # the gap. Pre-release: 30m before high, 15m before medium.
        # Post-release: 60m / 30m respectively.
        try:
            from services.macro_calendar import is_in_blackout as _macro_blk
            in_blk, ev, why = _macro_blk()
            if in_blk:
                logger.info(f"AutoTrader skip {ticker}: macro blackout — {why}")
                metrics.inc("autotrade_event", event="macro_blackout_stock")
                return None
        except Exception:
            pass

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

            # Profit-audit #3: dollar-based sector heat cap.
            # Count-based caps treat 5 small trades = 5 huge trades. That's wrong
            # when a tech-sector correlated drawdown (5-7% single-day gap) hits
            # the book. Cap total $-at-risk in any one sector to 4% of equity.
            # Skips when acct isn't available (tested elsewhere).
            try:
                _sector_acct = paper_trader.get_account()
                _sector_equity = float(_sector_acct["equity"]) if _sector_acct else 0.0
                if _sector_equity > 0:
                    sector_rows = db.query(AutoTrade).filter(
                        AutoTrade.sector == sector,
                        AutoTrade.status.in_(["pending", "open"]),
                    ).all()
                    sector_heat = 0.0
                    for sr in sector_rows:
                        se = sr.entry_price or sr.requested_entry or 0.0
                        ss_ = sr.current_stop or sr.stop_loss or 0.0
                        if sr.asset_type == "stock" and se > 0 and ss_ > 0:
                            sector_heat += max(0.0, (se - ss_)) * (sr.qty or 0)
                        elif sr.asset_type == "option" and se > 0:
                            sector_heat += float(se) * 100 * (sr.qty or 0)
                    new_heat = max(0.0, risk_per_share) * 1  # conservative — single-share for the gate
                    sector_heat_cap = _sector_equity * 0.04   # 4% of equity per sector
                    if sector_heat + new_heat >= sector_heat_cap:
                        logger.info(
                            f"AutoTrader skip {ticker}: sector '{sector}' heat "
                            f"${sector_heat:.0f} would exceed cap ${sector_heat_cap:.0f} "
                            f"(4% × equity {_sector_equity:.0f})"
                        )
                        metrics.inc("autotrade_event", event="sector_heat_cap")
                        return None
            except Exception:
                pass

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
        # Profit-audit #4: empirical calibration multiplier — closes the loop
        # from the nightly calibration job. A confidence bucket that has
        # historically won 70% of trades multiplies risk by 1.22x; a bucket
        # that has only won 35% multiplies by 0.70x. Defaults to 1.0 when
        # insufficient samples (no cold-start bias).
        cal_mult = calibration_multiplier(confidence)
        # Profit-audit #8: per-strategy realized P&L multiplier. Down-weights
        # chronic-losing strategies in live data even if the backtest blessed
        # them. Defaults to 1.0 until there are 5+ closed trades for this strategy.
        strat_mult = strategy_multiplier(signal.get("strategy"))
        # Ground-up Tier 1: VIX-based sizing.
        try:
            from services.market_context import vix_sizing_multiplier
            vix_mult = vix_sizing_multiplier()
        except Exception:
            vix_mult = 1.0
        # Critical-audit fix #1: cap the compound multiplier to prevent
        # runaway position-sizing after winning streaks where all 5 factors
        # align bullish. Without this, the theoretical max is 1.75 × 1.35 ×
        # 1.3 × 1.3 × 1.0 = 4.7×, turning a 2% risk cap into 9.4% per trade.
        # A single reversal then hits the account ~5× harder than intended.
        # The 2.0× ceiling preserves 60% of the multiplier upside while
        # hard-capping the downside.
        from services.config import RISK_MULT_CEILING as _MULT_CEILING
        raw_stack = conf_mult * kelly_mult * cal_mult * strat_mult * vix_mult
        clamped_stack = min(raw_stack, _MULT_CEILING)
        effective_risk_budget = risk_budget * clamped_stack
        if raw_stack > _MULT_CEILING:
            logger.info(
                f"AutoTrader {ticker}: multiplier stack {raw_stack:.2f}× clamped to {_MULT_CEILING}× "
                f"(conf={conf_mult:.2f} kelly={kelly_mult:.2f} cal={cal_mult:.2f} strat={strat_mult:.2f} vix={vix_mult:.2f})"
            )
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

        # Profit-audit #6: 1-min bar entry confirmation. Require the last
        # closed 1-min bar to agree with BUY direction. Skips on missing
        # data (fail-open) so we don't reject good signals on API flakes.
        if not _confirm_1m_bar(ticker, direction="BUY"):
            logger.info(
                f"AutoTrader skip {ticker}: 1-min bar disagrees with BUY direction "
                f"(waiting for green-bar confirmation)"
            )
            metrics.inc("autotrade_event", event="one_min_disagree")
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

        # Ground-up Tier 1: entry order type. market (default) vs limit_at_mid.
        # limit_at_mid captures half the bid-ask spread for liquid names.
        # Quote the live mid from the stock stream; fall back to market if
        # we don't have a fresh quote.
        _eot = (getattr(cfg, "entry_order_type", None) or "market").lower()
        _entry_type = "market"
        _limit_px = None
        if _eot == "limit_at_mid" and paper_trader.is_market_open():
            try:
                q = live_quotes.get_stock_quote(ticker) or {}
                bid = float(q.get("bid") or 0)
                ask = float(q.get("ask") or 0)
                if bid > 0 and ask > 0 and ask > bid:
                    _limit_px = round((bid + ask) / 2.0, 2)
                    # Floor at mid to avoid paying up; fallback if mid isn't
                    # between bid/ask (stale quote, locked market, etc).
                    if bid <= _limit_px <= ask:
                        _entry_type = "limit"
                    else:
                        _limit_px = None
            except Exception:
                _limit_px = None

        res = paper_trader.submit_bracket_order(
            symbol=ticker,
            qty=qty,
            side="buy",
            entry_type=_entry_type,
            limit_price=_limit_px,
            take_profit=far_tp,
            stop_loss=round(stop, 2),
            time_in_force="gtc",
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
            err_lower = str(res.get("error", "")).lower()
            # `_bp_exhausted_until` and `_broker_down_until` are already in the
            # function's global scope via the declaration at the top of
            # consider_signal — no second `global` needed here.
            if "insufficient buying power" in err_lower or "insufficient_buying_power" in err_lower:
                from datetime import timedelta as _td2
                _bp_exhausted_until = datetime.utcnow() + _td2(minutes=30)
                logger.warning(
                    f"AutoTrader: buying-power exhausted, pausing new entries until {_bp_exhausted_until.isoformat()}Z"
                )
                metrics.inc("autotrade_event", event="bp_exhausted")
                _raise_alert("warning", "bp_breaker", f"Buying power exhausted on {ticker}; new entries paused 30m", ticker=ticker)
            elif any(code in err_lower for code in ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable", "gateway timeout", "internal server error")):
                # Broker 5xx — pause all entry/exit submits for 10 min so we don't
                # DDoS Alpaca during an outage. Auto-recovers after the timer
                # expires; manage loop still runs its reconciliation logic.
                from datetime import timedelta as _td3
                _broker_down_until = datetime.utcnow() + _td3(minutes=10)
                logger.error(f"AutoTrader: Alpaca 5xx detected, broker-down circuit breaker tripped for 10m")
                metrics.inc("autotrade_event", event="broker_down")
                _raise_alert("error", "broker_down", f"Alpaca 5xx on {ticker} submit: {res['error'][:200]}", ticker=ticker)
            return None

        trade = AutoTrade(
            ticker=ticker, symbol=ticker, asset_type="stock", side="buy",
            qty=qty,
            original_qty=qty,   # critical-audit fix #11
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

        # Global ticker blacklist.
        if is_blacklisted(ticker, cfg):
            return None

        # Macro release blackout — options-strict (1.5× window) to account for
        # IV crush and gamma whipsaw around CPI/NFP/FOMC releases.
        try:
            from services.macro_calendar import is_in_blackout as _macro_blk
            in_blk, _ev, why = _macro_blk(options_only_strict=True)
            if in_blk:
                logger.info(f"AutoTrader skip PUT {ticker}: macro blackout — {why}")
                metrics.inc("autotrade_event", event="macro_blackout_put")
                return None
        except Exception:
            pass

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

        # Earnings-calendar gate — puts are even more exposed to post-earnings
        # IV crush than stocks, so we reject within the same 48h window.
        try:
            if inside_earnings_window(ticker):
                hte = hours_to_next_earnings(ticker)
                logger.info(
                    f"AutoTrader skip PUT {ticker}: earnings in {hte:.1f}h "
                    f"(IV-crush risk)"
                )
                metrics.inc("autotrade_event", event="earnings_skip_put")
                return None
        except Exception:
            pass

        thesis = build_bear_thesis(ticker, "1d")
        if not thesis:
            return None
        # Floor: aggressive 60, non-aggressive 0.85×threshold. Previously 45 /
        # 0.7× let a conf-53 GFS put through that lost $360 on weak volume.
        aggressive = bool(getattr(cfg, "aggressive_options_mode", False))
        min_bear_conf = 60.0 if aggressive else (cfg.confidence_threshold * 0.85)
        if thesis["confidence"] < min_bear_conf:
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
        min_score = MIN_OPTION_SCORE_AGGRESSIVE if aggressive else MIN_OPTION_SCORE
        if top.get("score", 0) < min_score:
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

        # Aggressive mode raises per-ticker cap from 33% → 50% to allow more
        # conviction-weighted sizing. Risk-per-trade remains capped.
        # Audit fix #10: in aggressive mode we also enforce a hard cap
        # per ticker/contract so a single cheap option can't soak 5%+
        # of equity. Max 2% of equity per option position in aggressive
        # mode (1% otherwise — tighter than the bucket-fraction cap).
        per_ticker_frac = 0.50 if aggressive else 0.33
        # Cheap-options gamma guard. Sub-$1 premium options have huge
        # contract-count-per-dollar — CNTA paper $0.30 call took 122 contracts
        # for a $3.7K notional position, then a 1% adverse move wiped $2,440.
        # Tighten the per-position dollar cap for cheap premium so the count
        # stays sane.
        _prem = float(top["premium"])
        if _prem < 0.50:
            per_contract_dollar_cap_frac = 0.005   # 0.5% equity for sub-$0.50 premium
        elif _prem < 2.00:
            per_contract_dollar_cap_frac = 0.010   # 1% for $0.50-2 premium
        else:
            per_contract_dollar_cap_frac = (0.02 if aggressive else 0.01)  # original
        per_contract_dollar_cap = equity * per_contract_dollar_cap_frac
        risk_per_contract = float(top.get("effective_max_loss") or top.get("max_loss_per_contract") or 0)
        if risk_per_contract <= 0:
            return None
        notional_per_contract = _prem * 100
        risk_budget = equity * cfg.max_risk_per_trade_pct
        max_qty_by_risk = int(risk_budget / risk_per_contract)
        max_qty_by_remaining = int(option_remaining / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_per_ticker = int((option_budget * per_ticker_frac) / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_cash = int(cash / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_bp = int(buying_power / notional_per_contract) if notional_per_contract > 0 else 0
        # Per-contract $ cap — hard ceiling on position size independent of
        # the bucket fractions. Catches the cheap-far-OTM case where
        # max_qty_by_per_ticker could hit 200+ contracts on a $0.30 option.
        max_qty_by_dollar_cap = int(per_contract_dollar_cap / notional_per_contract) if notional_per_contract > 0 else 0
        qty = min(
            max_qty_by_risk, max_qty_by_remaining,
            max_qty_by_per_ticker, max_qty_by_cash, max_qty_by_bp,
            max_qty_by_dollar_cap,
        )
        if qty < 1:
            return None

        occ = top["symbol"]

        # Alpaca rejects option MARKET orders outside RTH (code 42210000).
        if not paper_trader.is_market_open():
            logger.info(f"AutoTrader skip PUT {ticker} {occ}: market closed")
            return None

        # EOD guard: refuse new option entries within 45 min of close. Prevents
        # NFLX-style loss where a short-dated put was opened near close and
        # held overnight, then closed next morning after theta + gap risk.
        _mtc = paper_trader.minutes_to_close()
        if _mtc is not None and _mtc <= 45.0:
            logger.info(f"AutoTrader skip PUT {ticker} {occ}: {_mtc:.0f}m to close (EOD guard)")
            metrics.inc("autotrade_event", event="eod_guard_put")
            return None

        # Opening-bell guard: refuse new option entries in the first 15 min of
        # the session. Bid-ask spreads are at their widest right after the bell
        # (paper VTWO -$6,500 in 24s where the "decay" was entirely the spread
        # cross at 13:48 UTC = 9:48 ET, only 18 min after open).
        _mso = paper_trader.minutes_since_open()
        if _mso is not None and _mso < 15.0:
            logger.info(f"AutoTrader skip PUT {ticker} {occ}: only {_mso:.0f}m since open (opening-bell guard)")
            metrics.inc("autotrade_event", event="opening_guard_put")
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
            original_qty=qty,   # critical-audit fix #11: freeze entry qty
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
        try:
            live_quotes.ensure_option_symbols([occ])
        except Exception:
            pass
        return _serialize(trade)
    except Exception as e:
        logger.exception(f"consider_put_play error for {ticker}: {e}")
        return None
    finally:
        db.close()
        _entry_lock.release()


def consider_call_play(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Mirror of consider_put_play for the long-call side.

    Runs after every per-ticker analysis. Buys a call ONLY when one of:
      • The stock-side signal was a BUY at sub-threshold confidence (the
        stock gate rejected it, but the direction is sound enough for a
        defined-risk premium bet).
      • Stock auto-trade on the ticker is already at its per-ticker cap
        (stock slot full — call adds exposure cheaply).
      • No stock trade exists but the bull thesis is strong (60%+).

    Blocked when:
      • trade_calls config flag is off (default — user must explicitly enable).
      • An open stock long ALREADY exists on the ticker AT OR BELOW cap
        (avoid stacking correlated long exposure on the same underlying).
      • Earnings within 48h (same IV-crush risk as puts).
    """
    if not _entry_lock.acquire(timeout=30.0):
        logger.warning(f"consider_call_play({ticker}): entry lock busy >30s, skipping")
        metrics.inc("autotrade_event", event="entry_lock_timeout")
        return None
    db = SessionLocal()
    try:
        cfg = get_config(db)
        # Requires BOTH trade_options (options trading is approved) AND
        # the call-specific flag (user has opted in to call plays).
        if not (cfg.enabled and getattr(cfg, "trade_calls", False) and cfg.trade_options):
            return None
        if not paper_trader.is_enabled():
            return None

        ticker = ticker.upper()

        # Global ticker blacklist.
        if is_blacklisted(ticker, cfg):
            return None

        # Macro release blackout — options-strict mirror of put-side guard.
        try:
            from services.macro_calendar import is_in_blackout as _macro_blk
            in_blk, _ev, why = _macro_blk(options_only_strict=True)
            if in_blk:
                logger.info(f"AutoTrader skip CALL {ticker}: macro blackout — {why}")
                metrics.inc("autotrade_event", event="macro_blackout_call")
                return None
        except Exception:
            pass

        # Per-ticker auto-trade gate
        ws = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
        if ws and getattr(ws, "auto_trade_enabled", True) is False:
            return None

        # Concentration guard: if a stock auto-trade on this ticker is open
        # and NOT at per-ticker cap, don't stack a call on top (double-long
        # via premium would just leverage the already-established exposure).
        # We allow the call when the stock side is at cap (capital maxed out)
        # or when no stock trade exists.
        existing_stock = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.asset_type == "stock",
            AutoTrade.status.in_(["pending", "open"]),
        ).first()
        existing_option = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.asset_type == "option",
            AutoTrade.status.in_(["pending", "open"]),
        ).first()
        if existing_option:
            return None  # one option play at a time per ticker

        aggressive = bool(getattr(cfg, "aggressive_options_mode", False))
        # In default mode: concentration guard — only stack a call on top of
        # a stock long if the stock is at per-ticker cap. In aggressive mode:
        # this guard is dropped — dual-deploy (stock + call) on every strong
        # BUY is the whole point of the mode.
        if existing_stock and not aggressive:
            try:
                acct = paper_trader.get_account()
                eq = float(acct["equity"]) if acct else 0.0
                stock_budget = eq * cfg.stock_pct_of_equity
                per_ticker_cap = stock_budget * 0.30
                stock_notional = (existing_stock.entry_price or existing_stock.requested_entry or 0.0) * (existing_stock.qty or 0)
                if stock_notional < per_ticker_cap * 0.90:
                    # Stock slot still has ≥10% headroom — prefer more stock.
                    return None
            except Exception:
                pass

        # Earnings gate
        try:
            if inside_earnings_window(ticker):
                hte = hours_to_next_earnings(ticker)
                logger.info(
                    f"AutoTrader skip CALL {ticker}: earnings in {hte:.1f}h (IV-crush risk)"
                )
                metrics.inc("autotrade_event", event="earnings_skip_call")
                return None
        except Exception:
            pass

        thesis = build_bull_thesis(ticker, "1d")
        if not thesis:
            return None
        # Floor: aggressive 60, non-aggressive 0.85×threshold. Mirrors put gate
        # tightening after GFS conf-53 loss.
        min_bull_conf = 60.0 if aggressive else (cfg.confidence_threshold * 0.85)
        if thesis["confidence"] < min_bull_conf:
            return None

        # Live-price gap: bull-thesis stop sits BELOW price. If live price
        # has already cracked below it, the thesis is stale.
        try:
            fresh_px = _current_price(ticker)
            stop_underlying = float(thesis.get("stop_loss") or 0)
            if fresh_px and stop_underlying > 0 and fresh_px <= stop_underlying * 1.01:
                logger.info(
                    f"AutoTrader skip CALL {ticker}: live ${fresh_px:.2f} already "
                    f"<= 101% of bull-thesis stop ${stop_underlying:.2f} (stale)"
                )
                return None
        except Exception:
            pass

        # Stop-distance sanity (mirror of puts): > 12% wide = too much
        # theta exposure for the few contracts we'd afford.
        try:
            entry_underlying = float(thesis.get("entry") or 0)
            stop_underlying = float(thesis.get("stop_loss") or 0)
            if entry_underlying > 0 and stop_underlying > 0:
                stop_distance_pct = (entry_underlying - stop_underlying) / entry_underlying
                if stop_distance_pct > 0.12:
                    logger.info(
                        f"AutoTrader skip CALL {ticker}: bull-thesis stop {stop_distance_pct*100:.1f}% "
                        f"below entry — too wide for option theta budget"
                    )
                    return None
        except Exception:
            pass

        sugg = suggest_options_for_signal(ticker, thesis)  # direction='BUY' → calls
        contracts = sugg.get("contracts") or []
        if not contracts:
            return None
        top = contracts[0]
        min_score = MIN_OPTION_SCORE_AGGRESSIVE if aggressive else MIN_OPTION_SCORE
        if top.get("score", 0) < min_score:
            return None

        # Budget check — shares the same option bucket as puts.
        acct = paper_trader.get_account()
        if not acct:
            return None
        equity = float(acct["equity"])
        cash = float(acct["cash"])
        buying_power = float(acct.get("buying_power") or cash)
        _decay_in_flight_bp_if_stale()
        in_flight = _get_in_flight_bp()
        buying_power = max(0.0, buying_power - in_flight)
        cash = max(0.0, cash - in_flight)
        alloc = _open_allocations(db)
        option_budget = equity * cfg.option_pct_of_equity
        option_remaining = option_budget - alloc["option"]

        # Audit fix #10: in aggressive mode we also enforce a hard cap
        # per ticker/contract so a single cheap option can't soak 5%+
        # of equity. Max 2% of equity per option position in aggressive
        # mode (1% otherwise — tighter than the bucket-fraction cap).
        per_ticker_frac = 0.50 if aggressive else 0.33
        # Cheap-options gamma guard. Sub-$1 premium options have huge
        # contract-count-per-dollar — CNTA paper $0.30 call took 122 contracts
        # for a $3.7K notional position, then a 1% adverse move wiped $2,440.
        # Tighten the per-position dollar cap for cheap premium so the count
        # stays sane.
        _prem = float(top["premium"])
        if _prem < 0.50:
            per_contract_dollar_cap_frac = 0.005   # 0.5% equity for sub-$0.50 premium
        elif _prem < 2.00:
            per_contract_dollar_cap_frac = 0.010   # 1% for $0.50-2 premium
        else:
            per_contract_dollar_cap_frac = (0.02 if aggressive else 0.01)  # original
        per_contract_dollar_cap = equity * per_contract_dollar_cap_frac
        risk_per_contract = float(top.get("effective_max_loss") or top.get("max_loss_per_contract") or 0)
        if risk_per_contract <= 0:
            return None
        notional_per_contract = _prem * 100
        risk_budget = equity * cfg.max_risk_per_trade_pct
        max_qty_by_risk = int(risk_budget / risk_per_contract)
        max_qty_by_remaining = int(option_remaining / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_per_ticker = int((option_budget * per_ticker_frac) / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_cash = int(cash / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_bp = int(buying_power / notional_per_contract) if notional_per_contract > 0 else 0
        qty = min(max_qty_by_risk, max_qty_by_remaining, max_qty_by_per_ticker, max_qty_by_cash, max_qty_by_bp)
        if qty < 1:
            return None

        occ = top["symbol"]

        if not paper_trader.is_market_open():
            logger.info(f"AutoTrader skip CALL {ticker} {occ}: market closed")
            return None

        # EOD guard: mirror of put-side — no new option entries in final 45m.
        _mtc = paper_trader.minutes_to_close()
        if _mtc is not None and _mtc <= 45.0:
            logger.info(f"AutoTrader skip CALL {ticker} {occ}: {_mtc:.0f}m to close (EOD guard)")
            metrics.inc("autotrade_event", event="eod_guard_call")
            return None

        # Opening-bell guard — mirror of put-side. First 15 min has the widest
        # spreads of the day; cost VTWO/CNTA/AMKR ~$10K combined on 2026-04-24.
        _mso = paper_trader.minutes_since_open()
        if _mso is not None and _mso < 15.0:
            logger.info(f"AutoTrader skip CALL {ticker} {occ}: only {_mso:.0f}m since open (opening-bell guard)")
            metrics.inc("autotrade_event", event="opening_guard_call")
            return None

        logger.info(
            f"AutoTrader CALL {ticker} {occ} qty={qty} premium={top['premium']} "
            f"score={top['score']} bull-conf={thesis['confidence']}"
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
                note=f"call submit failed: {res['error']}",
            ))
            db.commit()
            logger.warning(f"AutoTrader call submit failed for {occ}: {res['error']}")
            return None

        trade = AutoTrade(
            ticker=ticker,
            symbol=occ,
            asset_type="option",
            side="buy",
            qty=qty,
            requested_entry=float(top["premium"]),
            # For calls: underlying stop sits BELOW price. The option
            # manage loop (`_manage_option_trade`) already supports puts;
            # we reuse it for calls by parsing direction from the OCC
            # symbol — the `is_put` check there also handles calls via
            # its else branch (added in this change).
            stop_loss=float(thesis["stop_loss"]),
            current_stop=float(thesis["stop_loss"]),
            target1=float(thesis["target1"]),
            target2=float(thesis["target2"]),
            target3=float(thesis["target3"]) if thesis.get("target3") else None,
            level_index=0,
            parent_order_id=res.get("id"),
            status="pending",
            note=(
                f"CALL play: bull-conf {thesis['confidence']} | strike {top['strike']} "
                f"exp {top['expiration']} ({top['dte']}d) | underlying stop "
                f"${thesis['stop_loss']:.2f}, T1 ${thesis['target1']:.2f}, T2 ${thesis['target2']:.2f}"
            ),
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        _reserve_bp(qty * float(top["premium"]) * 100.0)
        metrics.inc("autotrade_event", event="opened_call")
        try:
            live_quotes.ensure_option_symbols([occ])
        except Exception:
            pass
        return _serialize(trade)
    except Exception as e:
        logger.exception(f"consider_call_play error for {ticker}: {e}")
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


# Daily ADX cache for adaptive chandelier — same 5-min TTL as the ATR cache.
_chandelier_adx_cache: Dict[str, tuple] = {}


def _chandelier_adx(ticker: str) -> Optional[float]:
    """Daily ADX_14 for the ticker — used to loosen/tighten the chandelier
    trail based on trend strength. Strong trends (ADX > 30) let winners
    run with a wider trail; chop (ADX < 20) tightens it to reduce bleed."""
    import time as _t
    now = _t.time()
    cached = _chandelier_adx_cache.get(ticker.upper())
    if cached and now < cached[1]:
        return cached[0]
    try:
        from services.data_fetcher import fetch_ohlcv as _fo
        from services.indicators import compute_indicators as _ci
        _df = _fo(ticker, "1d")
        if _df is None or _df.empty:
            return None
        _ind = _ci(_df)
        if "ADX_14" not in _ind.columns:
            return None
        adx = float(_ind["ADX_14"].iloc[-1])
        _chandelier_adx_cache[ticker.upper()] = (adx, now + _CHANDELIER_ATR_TTL)
        return adx
    except Exception:
        return None


def _adaptive_chandelier_mult(base_mult: float, ticker: str) -> float:
    """Adjust the configured chandelier multiplier based on trend strength:
      • ADX > 30  (strong trend)     → base × 1.33 (give winners room)
      • ADX < 20  (chop)             → base × 0.83 (cut bleed)
      • 20 ≤ ADX ≤ 30 (transitional) → base (config value unchanged)
    Returns base if ADX cannot be read.
    """
    adx = _chandelier_adx(ticker)
    if adx is None:
        return base_mult
    if adx > 30:
        return base_mult * 1.33
    if adx < 20:
        return base_mult * 0.83
    return base_mult


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

    # 3) State-machine trailing on the UNDERLYING.
    #
    #    PUT  (is_put=True):  targets are BELOW current price; stop sits ABOVE.
    #                         Target hit when px <= next_target (dropping).
    #                         "Tighter" = LOWER upper-stop.
    #    CALL (is_put=False): targets are ABOVE current price; stop sits BELOW.
    #                         Target hit when px >= next_target (rising).
    #                         "Tighter" = HIGHER lower-stop.
    #
    #    We don't move a real broker stop (options have no SL leg here) — we only
    #    update t.current_stop (the underlying-stop level we'll exit at).
    if px is not None:
        targets = [t.target1, t.target2, t.target3]
        li = t.level_index or 0
        target_idx = li % 3
        next_target = targets[target_idx]
        hit = next_target is not None and (
            (is_put and px <= next_target) or (not is_put and px >= next_target)
        )
        if hit:
            # Partial profit-taking: at T1, sell HALF of the ORIGINAL contracts
            # (critical-audit fix #11) — NOT half of current qty. Using
            # current qty causes exponential decay across cascaded trims,
            # leaving micro-size runners for T3.
            if target_idx == 0 and t.qty >= 2 and not t.hit_t1:
                orig = int(getattr(t, "original_qty", None) or t.qty)
                half = max(1, int(orig // 2))
                half = min(half, int(t.qty))   # can't trim more than we hold
                if half >= 1:
                    trim = paper_trader.submit_simple_option_order(
                        occ_symbol=t.symbol, qty=half, side="sell",
                        order_type="market", time_in_force="day",
                    )
                    trim_ok = (
                        isinstance(trim, dict)
                        and "error" not in trim
                        and trim.get("id")
                        and str(trim.get("status", "")).lower() not in ("rejected", "canceled", "expired")
                    )
                    if trim_ok:
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
                            f"AutoTrader {'PUT' if is_put else 'CALL'} {t.ticker} partial-trim {half} @ "
                            f"underlying {px:.2f}; runner {int(t.qty)} contracts"
                        )
                    else:
                        err = trim.get("error") if isinstance(trim, dict) else "unknown"
                        logger.error(f"option partial-trim submit FAILED for {t.symbol}: {err}; leaving full qty open")
                        _raise_alert(
                            "error", "option_trim_failed",
                            f"T1 partial-trim rejected on {t.ticker} {t.symbol} ({err}); position unchanged",
                            ticker=t.ticker, trade_id=t.id,
                        )
                        # Do NOT mutate t.qty or t.hit_t1 — will retry next tick.
            # Compute new u-stop level.
            if target_idx == 0:
                # Near break-even on underlying. Puts: 2% above T1 ; calls: 2% below T1.
                new_stop = round(next_target * (1.02 if is_put else 0.98), 2)
            else:
                prev = targets[target_idx - 1]
                new_stop = round(prev, 2) if prev else t.current_stop
            # Tightness check is direction-dependent.
            if is_put:
                tightened = bool(t.current_stop and new_stop < t.current_stop)
            else:
                tightened = bool(t.current_stop and new_stop > t.current_stop)
            if tightened:
                t.current_stop = new_stop
            t.level_index = li + 1
            tag = ["T1", "T2", "T3"][target_idx]
            t.note = (t.note or "") + (
                f" | underlying {tag} hit @ {px:.2f}, u-stop→{new_stop}"
                if tightened else
                f" | underlying {tag} hit @ {px:.2f} (u-stop unchanged)"
            )
            # Push-notify the UI via the live-quotes WebSocket channel.
            try:
                from services import live_quotes as _lq
                _lq.broadcast_event_safe({
                    "type": "target_hit",
                    "trade_id": t.id,
                    "ticker": t.ticker,
                    "asset_type": t.asset_type,
                    "level": tag,
                    "price": round(float(px), 2),
                    "new_stop": round(float(new_stop), 2) if new_stop else None,
                })
            except Exception:
                pass
            if target_idx == 2:
                new_targets = _recalculate_targets(t.ticker, "bear" if is_put else "long", px)
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
                f"AutoTrader {'PUT' if is_put else 'CALL'} {t.ticker} underlying {tag} hit, "
                f"u-stop→{new_stop} (level_index={t.level_index})"
            )

    # 4) Underlying-stop breach (price moved AGAINST the thesis) → close.
    #    Puts: breach = price rose past stop (stop sits above).
    #    Calls: breach = price fell below stop (stop sits below).
    if px is not None and t.current_stop:
        if is_put and px >= t.current_stop:
            exit_reason = f"underlying broke trailing u-stop ${t.current_stop:.2f} (now ${px:.2f})"
            final_status = "closed_stop"
        elif (not is_put) and px <= t.current_stop:
            exit_reason = f"underlying broke trailing u-stop ${t.current_stop:.2f} (now ${px:.2f})"
            final_status = "closed_stop"

    # 5) Premium decay safety — still exit if the option has lost ≥ 50% of premium.
    # Two anti-spread-artifact guards:
    #  (a) Don't fire within 5 minutes of opening — paper VTWO -$6,500 in 24s
    #      where the "decay" was ~entirely the bid-ask cross at market open
    #      (entered at ask $4.90, valuation went to bid $2.30 instantly).
    #      Real theta-driven 50% decay over 22 DTE takes hours, not seconds.
    #  (b) Require the underlying to be moving against the thesis — if we're
    #      a long CALL and underlying is FLAT or UP vs entry, a "50% decay"
    #      reading is almost certainly a stale/wide quote. Don't exit on
    #      premium alone; let the underlying-stop in step 4 handle real losses.
    if not exit_reason and cur_premium is not None and t.entry_price:
        if cur_premium <= t.entry_price * 0.5:
            from datetime import datetime as _dt_pm, timedelta as _td_pm
            opened = t.filled_at or t.opened_at
            held_secs = (_dt_pm.utcnow() - opened).total_seconds() if opened else 99999
            spread_artifact_window = held_secs < 300  # 5 min
            # Check underlying direction against thesis
            underlying_against_us = False
            if px is not None and getattr(t, "requested_entry", None):
                # For CALL: against = price < entry; for PUT: against = price > entry
                if is_put:
                    underlying_against_us = px > float(t.requested_entry) * 1.001
                else:
                    underlying_against_us = px < float(t.requested_entry) * 0.999
            if spread_artifact_window and not underlying_against_us:
                logger.info(
                    f"AutoTrader skip premium-stop {t.ticker} {t.symbol}: held {held_secs:.0f}s, "
                    f"underlying not against us (px={px} entry={t.requested_entry}) — likely spread artifact"
                )
                metrics.inc("autotrade_event", event="premium_stop_spread_skip")
            else:
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


REVERSE_CONFIDENCE_GATE = 80.0  # Critical-audit fix #7: raised 65 → 80.
# A 65-confidence 1d SELL was reversing profitable 1mo BUYs on 2-3 day
# fake-outs. Requiring 80+ means only high-conviction opposite signals
# force-close — reduces premature exits of multi-week trends.


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


def _is_call_option(t: AutoTrade) -> bool:
    """Parse OCC symbol to detect CALL vs PUT. OCC format has the C/P
    indicator immediately before the 8-digit strike, so it's at position
    [-9] from end. AMKR260515C00075000 → 'C'."""
    sym = (getattr(t, "symbol", None) or "")
    return bool(sym) and len(sym) >= 9 and sym[-9].upper() == "C"


def _check_reversal(t: AutoTrade, db: Session) -> Optional[str]:
    """
    Detect a strong opposing signal that landed AFTER this trade was opened.
    Returns a reason string if we should close, else None.

      • Long stock     → opposing = SELL signal ≥ gate
      • Long PUT       → opposing = BUY  signal ≥ gate (bull thesis invalidates put)
      • Long CALL      → opposing = SELL signal ≥ gate (bear thesis invalidates call)

    Pre-fix bug: all options used BUY as opposing, which closed CALL plays
    on confirming-bull signals (lost ~$1,190 on AMKR before this fix).

    The opposing signal must be on a timeframe ≥ the trade's source TF — a 5m
    fakeout shouldn't be allowed to close a 1d-conviction position.
    """
    opened_at = t.filled_at or t.opened_at
    if not opened_at:
        return None
    if t.asset_type == "stock":
        opposing = "SELL"
    else:
        # Options: direction depends on call vs put
        opposing = "SELL" if _is_call_option(t) else "BUY"
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
    # Critical-audit fix #7: opposing TF must match or EXCEED source TF.
    # Previously allowed (src_rank - 1), which let a 1d SELL reverse a 1mo
    # BUY on a fakeout. Now a 1mo trade can only be reversed by a 1mo signal
    # (or higher); 1d by 1d+; 4h by 4h+. Long-TF trades get protection from
    # short-TF noise reversals.
    min_rank = src_rank
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
                        # Audit fix #13: earnings-triggered force-close for
                        # options within 6h of the print. IV crush on
                        # earnings can erase 40-80% of premium in minutes.
                        # We exit BEFORE the print to bank whatever remains.
                        try:
                            hte = hours_to_next_earnings(t.ticker)
                            if hte is not None and 0 < hte <= 6:
                                _raise_alert(
                                    "warning", "earnings_force_close",
                                    f"{t.ticker} earnings in {hte:.1f}h — closing {t.symbol} to avoid IV crush",
                                    ticker=t.ticker, trade_id=t.id,
                                )
                                _force_close_trade(
                                    t, db,
                                    f"earnings in {hte:.1f}h — pre-empting IV crush",
                                    summary,
                                    status_override="closed_manual",
                                )
                                continue
                        except Exception as _e:
                            logger.warning(f"earnings close check {t.ticker}: {_e}")
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
                                    _raise_alert(
                                        "critical", "sl_invariant",
                                        f"{t.ticker}: stop leg gone ({sl_status}); resubmit in progress",
                                        ticker=t.ticker, trade_id=t.id,
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
                                        record_sl_resubmit_failure()
                                        _raise_alert(
                                            "critical", "sl_resubmit_failed",
                                            f"{t.ticker} SL resubmit FAILED: {_re} — POSITION IS NAKED",
                                            ticker=t.ticker, trade_id=t.id,
                                        )
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
                                    # Audit fix #12: demoted INFO → DEBUG.
                                    # Target touches can fire 10+ times/min in
                                    # volatile markets and otherwise drown
                                    # the log stream.
                                    logger.debug(
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
                                        # Post-mortem fix (AAPL): the previous
                                        # `if atr_be and …` guard let NaN/None
                                        # fall through silently — NaN is truthy
                                        # but `NaN < x` is False, so the
                                        # too-tight-T1 skip never fired for
                                        # AAPL (T1 only 11¢ above entry).
                                        import math as _math
                                        atr_ok = (atr_be is not None and
                                                  isinstance(atr_be, (int, float)) and
                                                  not _math.isnan(atr_be) and
                                                  atr_be > 0)
                                        # Belt-and-braces: percentage-based
                                        # guard kicks in even if ATR is
                                        # unavailable. T1 within 0.4% of entry
                                        # is always too tight for a BE trail.
                                        pct_gap = (next_target - t.entry_price) / max(0.01, t.entry_price)
                                        too_tight_pct = pct_gap < 0.004
                                        too_tight_atr = atr_ok and (next_target - t.entry_price) < _T1_BE_MIN_ATR * atr_be
                                        if too_tight_pct or too_tight_atr:
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
                                            # Post-mortem fix (AAPL/MRVL/MU chop-outs):
                                            # instead of slamming the stop to full
                                            # break-even at T1 — which is extremely
                                            # vulnerable to normal 1% retraces when
                                            # T1 is close to entry — trail to
                                            # `entry − 0.3 × initial_risk`. The
                                            # partial trim already realised at T1
                                            # pays for 1/3 of the residual risk,
                                            # so expected value is still positive
                                            # while we keep meaningful breathing
                                            # room for the winner to develop.
                                            initial_risk = max(0.01, float(t.entry_price) - float(t.stop_loss))
                                            # Soft-BE distance is the LARGER of 0.3R or 0.25×ATR so we stay
                                            # outside the 1-bar noise floor for high-volatility names
                                            # (BE-trail was otherwise chopping them out on normal wicks).
                                            atr_buffer = _chandelier_atr(t.ticker) or 0.0
                                            stop_dist = max(0.3 * initial_risk, 0.25 * atr_buffer)
                                            soft_be = float(t.entry_price) - stop_dist
                                            new_stop = round(max(soft_be, t.current_stop or 0), 2)
                                        elif target_idx == 1:
                                            # At T2: now tighten to full entry (BE).
                                            new_stop = round(float(t.entry_price), 2)
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
                                            # Push-notify UI
                                            try:
                                                from services import live_quotes as _lq
                                                _lq.broadcast_event_safe({
                                                    "type": "target_hit",
                                                    "trade_id": t.id,
                                                    "ticker": t.ticker,
                                                    "asset_type": t.asset_type,
                                                    "level": tag,
                                                    "price": round(float(px), 2),
                                                    "new_stop": round(float(new_stop), 2),
                                                })
                                            except Exception:
                                                pass
                                            if target_idx == 2 and (t.level_index or 0) < 3:
                                                # First T3 cycle — recompute
                                                # the next rung for one more
                                                # leg. After that (level_index
                                                # ≥ 3), let chandelier alone
                                                # trail the runner instead of
                                                # rolling fresh targets each
                                                # leg — the BE-like stop moves
                                                # from recompute chopped out
                                                # otherwise-nice extensions.
                                                new_targets = _recalculate_targets(t.ticker, "long", px)
                                                if new_targets:
                                                    t.target1, t.target2, t.target3 = new_targets
                                                    _record_target_history(
                                                        t,
                                                        f"T3 breached @ {px:.2f}; recalc from price",
                                                        new_targets,
                                                    )
                                                    t.note = (t.note or "") + f" | recalc T1-3: {new_targets}"
                                            elif target_idx == 2:
                                                # Past the first recompute —
                                                # just let chandelier trail.
                                                t.note = (t.note or "") + (
                                                    f" | level_index ≥ 3, chandelier-only trail "
                                                    f"(no more target recompute)"
                                                )
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

                            # 2c) Chandelier + structural overlay.
                            # Critical-audit fix #5: chandelier now activates
                            # from bar 1, not after T1. Previously ~8% of
                            # entries reversed before reaching T1 and hit the
                            # hard stop at full 1R; a pre-T1 chandelier
                            # trail would have exited many at 0.5-0.7R.
                            # BUT we require price has moved into favor by
                            # at least 0.5R before letting chandelier tighten —
                            # otherwise chandelier would tighten the broker-held
                            # SL right after a fill, which is the naked-long
                            # race we spent audit-fix #2 avoiding.
                            base_mult = cfg_snapshot["chandelier_atr_mult"]
                            ch_mult = _adaptive_chandelier_mult(base_mult, t.ticker) if base_mult > 0 else 0
                            chandelier_stop = None
                            if ch_mult > 0 and t.high_water_mark and t.entry_price:
                                _initial_risk_ch = max(0.01, float(t.entry_price) - float(t.stop_loss))
                                _favor_move = t.high_water_mark - float(t.entry_price)
                                if _favor_move >= 0.5 * _initial_risk_ch:
                                    _atr = _chandelier_atr(t.ticker)
                                    if _atr is not None:
                                        chandelier_stop = round(t.high_water_mark - ch_mult * _atr, 2)

                            # Ground-up Tier 3: structural trail — most recent
                            # weekly swing low on daily+ source trades. The
                            # "just below structure" stop is what discretionary
                            # traders use; harder to shake out than an ATR-distance
                            # stop because it respects market memory.
                            structural_stop = None
                            try:
                                src_tf_trail = _trade_source_timeframe(t, db)
                                if src_tf_trail in ("1d", "1mo") and (t.level_index or 0) >= 1:
                                    from services.data_fetcher import fetch_ohlcv as _fo_wk
                                    wk_df = _fo_wk(t.ticker, "1d")
                                    if wk_df is not None and len(wk_df) >= 10:
                                        # Most recent swing low = min low over last 10 bars
                                        # (~2 weeks) with 0.3% buffer for wick tolerance.
                                        recent_low = float(wk_df["Low"].iloc[-10:].min())
                                        structural_stop = round(recent_low * 0.997, 2)
                            except Exception:
                                pass

                            # Choose the higher (tighter for long) of the two.
                            candidates = [x for x in (chandelier_stop, structural_stop) if x is not None]
                            if candidates:
                                new_trail_stop = max(candidates)
                                source = (
                                    "structural" if new_trail_stop == structural_stop and
                                    (chandelier_stop is None or structural_stop >= chandelier_stop)
                                    else "chandelier"
                                )
                                if new_trail_stop > t.current_stop and _replace_stop(t.stop_order_id, new_trail_stop):
                                    old_stop = t.current_stop
                                    t.current_stop = new_trail_stop
                                    t.note = (t.note or "") + (
                                        f" | {source} trail → stop {new_trail_stop} "
                                        f"(from {old_stop})"
                                    )
                                    db.commit()
                                    summary["trailed"] += 1
                                    logger.info(
                                        f"AutoTrader {t.ticker} {source} trail → {new_trail_stop} "
                                        f"(chandelier={chandelier_stop}, structural={structural_stop})"
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
