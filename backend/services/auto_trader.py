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
from contextlib import contextmanager
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
#
# r53 NOTE (Tier-1 #6): this lock is per-instance (threading.Lock). Cloud
# Run runs the api service with min=1, max=3 instances; each instance has
# its own lock object. Two instances can pass the budget+cap check
# simultaneously and both submit. The new `_pg_advisory_entry_lock`
# context manager below adds a Postgres-level advisory lock keyed on the
# ticker that serializes concurrent same-ticker entries across instances.
# On SQLite (test environment) it's a no-op since SQLite is single-writer.
_entry_lock = threading.Lock()


@contextmanager
def _pg_advisory_entry_lock(ticker: str, timeout_sec: float = 5.0):
    """r53 fix (Tier-1 #6): cross-instance entry lock via Postgres
    `pg_try_advisory_xact_lock`. Hashes ticker → 64-bit lock key; the
    lock is released on transaction commit/rollback.

    Yields True if the lock was acquired (caller proceeds), False if
    another instance currently holds it (caller skips the entry).
    On SQLite or any non-Postgres backend, yields True (no-op) since
    only one writer can be active at a time anyway.

    Use as: `with _pg_advisory_entry_lock(ticker) as acquired: ...`
    """
    db = SessionLocal()
    acquired = True  # default for non-Postgres
    try:
        try:
            dialect = db.bind.dialect.name
        except Exception:
            dialect = "unknown"
        if dialect == "postgresql":
            # 64-bit signed int from sha1 of ticker (stable across processes)
            h = hashlib.sha1(ticker.upper().encode()).digest()
            # Take the high 8 bytes, big-endian, mask to 63 bits to keep
            # within Postgres's signed bigint range.
            key = int.from_bytes(h[:8], "big") & ((1 << 63) - 1)
            try:
                from sqlalchemy import text as _sql_text
                # Try-style: returns True if acquired, False if another holder.
                got = db.execute(
                    _sql_text("SELECT pg_try_advisory_xact_lock(:k)"),
                    {"k": key},
                ).scalar()
                acquired = bool(got)
                if not acquired:
                    logger.info(
                        f"_pg_advisory_entry_lock: {ticker} held by another instance — skipping entry"
                    )
                    metrics.inc("autotrade_skip", reason="advisory_lock_held")
            except Exception as e:
                logger.warning(f"_pg_advisory_entry_lock {ticker} failed ({e}); proceeding without")
                acquired = True
        yield acquired
    finally:
        try:
            # Commit closes the txn, releasing pg_try_advisory_xact_lock
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        try:
            db.close()
        except Exception:
            pass

# BP reservation, circuit breakers, and SL-resubmit tracking now live in
# services.risk_manager. The aliases below preserve the public API used
# by other modules (routers/trading.py, main.py /api/health endpoint).
from services.risk_manager import (
    bp_breaker_active, broker_down,
    record_sl_resubmit_failure, sl_resubmit_failures_1h,
    trip_bp_breaker as _trip_bp_breaker,
    trip_broker_breaker as _trip_broker_breaker,
    clear_bp_breaker as _clear_bp_breaker,
    clear_broker_breaker as _clear_broker_breaker,
    bp_exhausted_until as _bp_exhausted_until_getter,
    broker_down_until as _broker_down_until_getter,
)

# Postmortem fix M1: local in-flight buying-power reservation. Alpaca's
# reported `buying_power` lags submitted bracket orders (pending TPs reserve
# BP that doesn't immediately show up as drawn). Without local bookkeeping,
# a watchlist scan can submit 30 orders against the same stale BP figure
# before the first 422 trips the circuit breaker. BP reservation state +
# helpers live in services.risk_manager. Aliases preserve existing call sites.
from services.risk_manager import (
    reserve_bp as _reserve_bp,
    release_bp as _release_bp,
    get_in_flight_bp as _get_in_flight_bp,
    decay_in_flight_bp_if_stale as _decay_in_flight_bp_if_stale,
)

# Per-trade consecutive-touch counters for the next price target. Required
# to suppress single-bar wick triggers — postmortems showed MRVL's T1 hit
# was a single 5m wick to 148.75 immediately followed by a print to 146.21,
# which moved the stop to BE and chopped the trade out for $0. We now
# require N>=2 consecutive manage-loop ticks above the target before
# trailing. r37: Backed by AutoTrade.target_touch_count so the debounce
# survives Cloud Run instance restarts. The in-memory dict is kept as a
# read-through cache to avoid hammering the DB on hot iterations of the
# manage loop, but writes go straight to the row + commit.
_TARGET_CONFIRM_TICKS = 2
_target_touch_counts: Dict[int, int] = {}
# r43 fix #2 / Tier 2 L6: thread-safe touch-counts. Fast-path (WS thread)
# and scheduler can both increment for the same trade row.
_target_touch_lock = threading.Lock()
# r43 fix #1.16: manage-loop reentrancy guard — prevents fast-path and
# scheduler from interleaving on the same trade rows.
_manage_lock = threading.Lock()


def _touch_get(t) -> int:
    """Read the touch counter, preferring the persisted value on the row."""
    try:
        persisted = int(getattr(t, "target_touch_count", 0) or 0)
    except Exception:
        persisted = 0
    with _target_touch_lock:
        cached = _target_touch_counts.get(t.id, 0)
    return max(persisted, cached)


def _touch_set(t, db, n: int) -> None:
    """Increment / set the touch counter on the row + cache + commit."""
    with _target_touch_lock:
        _target_touch_counts[t.id] = n
    try:
        t.target_touch_count = int(n)
        db.commit()
    except Exception as _e:
        logger.debug(f"_touch_set: persist skipped for #{t.id}: {_e}")


def _touch_clear(t, db=None) -> None:
    """Reset both cache + persisted counter (called on close, target advance,
    or when the prospective target is no longer being touched)."""
    _target_touch_counts.pop(t.id, None)
    if db is not None:
        try:
            t.target_touch_count = 0
            db.commit()
        except Exception as _e:
            logger.debug(f"_touch_clear: persist skipped for #{t.id}: {_e}")

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
_T2_PARTIAL_FRAC = 0.33  # default; trim_fraction_for_adx() adapts by trend strength


def trim_fraction_for_adx(ticker: str, level: str, default_frac: float = 0.33) -> float:
    """ADX-based dynamic trim: weak trends → bank fast, strong trends → let runners run.
    `level` ∈ {"T1","T2"}.

      * ADX ≥ 45 (parabolic / extreme trend) → trim 0% at T1 (skip the trim
        entirely; just move stop to soft-BE). Empirically the strongest
        trends are exactly when partial trims leave the most money on the
        table — the runner is the trade. Caller treats 0.0 as "no trim".
      * ADX ≥ 40 (powerful trend) → trim only 15% (leave 70% past T2)
      * ADX ≤ 25 (weak trend)     → keep default 33% (bank profit faster)
      * In between → linear interpolation between default and 15%

    Falls back to `default_frac` if ADX can't be read.
    """
    try:
        from services.position_manager import chandelier_adx
        adx = chandelier_adx(ticker)
    except Exception:
        adx = None
    if adx is None:
        return default_frac
    # Only skip the T1 trim entirely on parabolic moves — at T2 we still
    # want to bank some profit even in extreme trends (T2 is a 2:1 win;
    # never pure-runner past T2).
    if adx >= 45 and level.upper() == "T1":
        return 0.0
    if adx >= 40:
        return 0.15
    if adx <= 25:
        return default_frac
    # Linear interp 25→default, 40→15%
    span = (adx - 25.0) / (40.0 - 25.0)
    return default_frac + (0.15 - default_frac) * span
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
# r43 fix #0.3: zoneinfo-based ET evaluation — the previous hardcoded
# (13:30, 13:45) UTC was 9:30-9:45 ET only during EDT; during EST (Nov →
# mid-March) the actual opening was 14:30-14:45 UTC and the filter was
# silently off for ~4 months/year.
_OPENING_FILTER_TFS = {"5m", "15m", "30m"}
# r43 fix #0.21: closing-bell filter for the symmetric MOC-imbalance window.
_CLOSING_FILTER_TFS = {"5m", "15m", "30m"}


def _in_opening_filter_window() -> bool:
    """True iff current ET time is in 9:30-9:45 ET (DST-aware)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        return (now_et.hour, now_et.minute) >= (9, 30) and (now_et.hour, now_et.minute) < (9, 45)
    except Exception:
        # Fail closed-ish — if we can't evaluate, assume in-window to skip
        # the trade rather than risk trading the open whipsaw blind.
        return False


def _in_closing_filter_window() -> bool:
    """True iff current ET time is in 15:50-16:00 ET (last-10-min auction-print window)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        return (now_et.hour, now_et.minute) >= (15, 50) and (now_et.hour, now_et.minute) < (16, 0)
    except Exception:
        return False


def _confirm_1m_bar(ticker: str, direction: str = "BUY") -> bool:
    """Profit-audit #6: 1-min SIP bar entry confirmation.

    Before submitting a market entry, fetch recent 1-min bars and require
    them to agree with the signal direction. Prevents the "entered at
    the 5-min wick high" losses. Falls open (returns True) when 1m data
    is unavailable so we never over-filter on transient data misses.

    r55 T1 #9: gate mode is now configurable. The original "strict" mode
    (single most-recent closed bar must agree) was empirically too tight
    — pullback bars in healthy uptrends were silently blocking entries
    on high-conviction signals. New default is "relaxed": 2-of-last-3
    closed bars must agree, which filters whipsaw without blocking on a
    single contrarian print. "off" disables the gate entirely.
    """
    # Read mode from cfg (cached read, no per-call DB hit since the
    # config row is small and SQLAlchemy session-cached).
    mode = "relaxed"
    try:
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            mode = (getattr(cfg, "entry_1m_gate_mode", "relaxed") or "relaxed").lower() if cfg else "relaxed"
        finally:
            db.close()
    except Exception:
        pass
    if mode == "off":
        return True
    try:
        from services.data_fetcher import fetch_ohlcv as _fo_1m
        df1 = _fo_1m(ticker, "1m")
        if df1 is None or df1.empty or len(df1) < 2:
            return True

        def _bar_agrees(row) -> bool:
            try:
                o = float(row["Open"])
                c = float(row["Close"])
                return (c >= o) if direction == "BUY" else (c <= o)
            except Exception:
                return True

        if mode == "strict" or len(df1) < 4:
            # Last fully-closed bar (penultimate row).
            return _bar_agrees(df1.iloc[-2])
        # Relaxed: majority of last 3 closed bars must agree.
        # df1.iloc[-1] is the in-progress bar; -2/-3/-4 are last 3 closed.
        last_three = [df1.iloc[-2], df1.iloc[-3], df1.iloc[-4]]
        agree_count = sum(1 for b in last_three if _bar_agrees(b))
        return agree_count >= 2
    except Exception:
        return True

# Background thread pool for non-blocking post-mortems. Sized small — these
# are infrequent, and we don't want to fan out a hundred analyses if many
# trades close at once.
_post_mortem_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="post-mortem")
# r39 audit cleanup: bound the pending queue so a manage-loop tick that
# closes 10+ trades simultaneously (e.g. SPY 5% gap-down stops out the
# whole book) doesn't queue 10 LLM jobs. We track in-flight + queued
# count manually since ThreadPoolExecutor's queue isn't size-bounded.
_POST_MORTEM_MAX_PENDING = 5
_post_mortem_pending: int = 0
_post_mortem_lock = threading.Lock()


def _post_mortem_async(trade_id: int) -> None:
    """Re-fetch the trade by id in a fresh session and run the analysis off-loop."""
    global _post_mortem_pending
    with _post_mortem_lock:
        if _post_mortem_pending >= _POST_MORTEM_MAX_PENDING:
            logger.info(
                f"post_mortem queue full ({_post_mortem_pending}/{_POST_MORTEM_MAX_PENDING}); "
                f"dropping #{trade_id} — operator can regenerate via "
                f"POST /api/trading/auto/postmortem/{trade_id}"
            )
            metrics.inc("autotrade_event", event="post_mortem_dropped")
            return
        _post_mortem_pending += 1

    def _job():
        global _post_mortem_pending
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
            with _post_mortem_lock:
                _post_mortem_pending -= 1
    try:
        _post_mortem_pool.submit(_job)
    except Exception as e:
        logger.warning(f"could not schedule post_mortem #{trade_id}: {e}")
        with _post_mortem_lock:
            _post_mortem_pending -= 1


# Idempotency-key math lives in services.risk_math. We keep a local alias
# so existing call sites don't have to change.
from services.risk_math import signal_idempotency_key as _signal_idempotency_key  # noqa: F401

from database import SessionLocal, AutoTrade, AutoTraderConfig, Signal, WatchlistStock
from services import paper_trader, live_quotes
from services import post_mortem as post_mortem_svc
from services import metrics

# r53l: per-thread capture of the most recent autotrade_skip reason so
# consider_signal / consider_put_play / consider_call_play can persist
# their final verdict to CandidatePool.last_*_reason without rewriting
# the 58 existing `metrics.inc("autotrade_skip", ...)` call sites. The
# wrapping is non-invasive: existing call sites keep using `metrics.inc`
# unchanged.
_DECISION_TLS = __import__("threading").local()
_orig_metrics_inc = metrics.inc


def _patched_metrics_inc(name, **labels):
    if name == "autotrade_skip":
        try:
            _DECISION_TLS.last_skip_reason = labels.get("reason")
        except Exception:
            pass
    return _orig_metrics_inc(name, **labels)


metrics.inc = _patched_metrics_inc


def _gate_record(name: str, result: str, **details: Any) -> None:
    """r65 transparency: record a per-gate evaluation outcome with the
    actual computed values + threshold + plain-language formula. The
    Decision Log UI renders this so the operator can see exactly WHY
    each gate passed/failed without reading code.

    Result values:  "pass" | "fail" | "na" (didn't apply / data missing)
    Common keys:    computed, threshold, formula, ...
    """
    try:
        if not hasattr(_DECISION_TLS, "gate_log") or _DECISION_TLS.gate_log is None:
            _DECISION_TLS.gate_log = []
        entry = {"gate": name, "result": result}
        for k, v in details.items():
            if v is None:
                continue
            if isinstance(v, float):
                if abs(v) >= 1000:
                    entry[k] = round(v, 2)
                elif abs(v) >= 1:
                    entry[k] = round(v, 4)
                else:
                    entry[k] = round(v, 6)
            else:
                entry[k] = v
        _DECISION_TLS.gate_log.append(entry)
    except Exception:
        pass


def _begin_decision(ticker: str, kind: str, signal: Optional[Dict[str, Any]] = None) -> None:
    """Reset the thread-local capture state for a new evaluation.
    r63/r65: capture FULL signal levels + per-gate audit log so the
    Decision Log UI can render the complete computation trace."""
    _DECISION_TLS.ticker = (ticker or "").upper()
    _DECISION_TLS.kind = kind  # "stock" | "option"
    _DECISION_TLS.last_skip_reason = None
    _DECISION_TLS.entered = False
    _DECISION_TLS.entered_kind = None  # "call" | "put" | None
    _DECISION_TLS.entered_trade_id = None
    _DECISION_TLS.gate_log = []  # r65: per-gate computation trace
    if signal:
        # r63/r64: keep a richer signal snapshot in TLS — the persist
        # function folds this into details_json. r64: also capture
        # `reasoning` (the multi-line breakdown of which contributors
        # fired and at what weight) so the UI audit panel can show how
        # signal_generator arrived at the confidence score.
        _DECISION_TLS.signal_view = {
            "confidence": signal.get("confidence"),
            "signal_type": signal.get("signal_type"),
            "timeframe": signal.get("timeframe"),
            "strategy": signal.get("strategy"),
            "entry": signal.get("entry"),
            "stop_loss": signal.get("stop_loss"),
            "target1": signal.get("target1"),
            "target2": signal.get("target2"),
            "target3": signal.get("target3"),
            "atr": signal.get("atr"),
            "rvol": signal.get("rvol"),
            "rs_20d": signal.get("rs_20d"),
            "adx": signal.get("adx"),
            "sector": signal.get("sector"),
            # r64: confidence-breakdown text — newline-joined contributors
            # like "✅ Price above SMA200 (long-term uptrend)\n🤖 ML P(win)=0.62 (×1.05)\n..."
            "reasoning": signal.get("reasoning"),
            "patterns": signal.get("patterns"),
        }
    else:
        _DECISION_TLS.signal_view = None


def _mark_entered(option_kind: Optional[str] = None, trade_id: Optional[int] = None) -> None:
    _DECISION_TLS.entered = True
    if option_kind:
        _DECISION_TLS.entered_kind = option_kind
    if trade_id is not None:
        _DECISION_TLS.entered_trade_id = trade_id


# r53r: low-information skip reasons. The bot calls consider_signal once
# per timeframe (5m, 15m, 30m, 1h, 4h, 1d, 1mo); most timeframes
# auto-skip with one of these reasons. Without filtering, the LAST
# timeframe processed (1mo) always wins the verdict, hiding more
# meaningful gate-rejections from the configured timeframes (1h/4h/1d).
_LOW_INFO_SKIPS = {
    "tf_not_allowed",
    "below_confidence_threshold",
    "non_buy_signal",
    "missing_levels",
    "neutral_signal",
}


def _persist_decision() -> None:
    """Write the captured verdict to CandidatePool. No-op when the row
    isn't present (i.e., the ticker isn't in the candidate pool — most
    watchlist tickers won't be, and that's fine; the metric counter
    still tracked the skip aggregate).

    r53r: prioritization. consider_signal runs per-timeframe; this
    helper compares new vs existing verdict and:
      - never overwrites an "entered" verdict with anything except
        another "entered"
      - never overwrites a meaningful skip with a low-info skip
        (tf_not_allowed / below_confidence_threshold / non_buy_signal /
        missing_levels / neutral_signal)
      - otherwise writes through (latest meaningful verdict wins)
    """
    try:
        ticker = getattr(_DECISION_TLS, "ticker", None)
        kind = getattr(_DECISION_TLS, "kind", None)
        if not ticker or not kind:
            return
        entered = bool(getattr(_DECISION_TLS, "entered", False))
        last_reason = getattr(_DECISION_TLS, "last_skip_reason", None)
        entered_kind = getattr(_DECISION_TLS, "entered_kind", None)
        if entered:
            if kind == "option":
                decision = f"entered_{entered_kind or 'call'}"
            else:
                decision = "entered"
            reason = None
        elif last_reason:
            decision = "skipped"
            reason = str(last_reason)[:80]
        else:
            decision = "no_signal"
            reason = None
        # Write iff the ticker IS in the candidate pool. Use a short
        # transaction; failures are silently swallowed (this is
        # observability, not load-bearing).
        try:
            from database import SessionLocal as _SL_d, CandidatePool as _CP_d
            _db_d = _SL_d()
            try:
                row = _db_d.query(_CP_d).filter(_CP_d.ticker == ticker).first()
                if row is not None:
                    # r53r: prioritized merge. Don't overwrite meaningful
                    # verdicts with low-info skips from a different
                    # timeframe's evaluation of the same ticker.
                    existing_decision = (
                        row.last_option_decision if kind == "option"
                        else row.last_stock_decision
                    )
                    existing_reason = (
                        row.last_option_reason if kind == "option"
                        else row.last_stock_reason
                    )
                    should_write = True
                    new_is_low_info = (
                        decision == "skipped"
                        and (reason or "").split(" ")[0] in _LOW_INFO_SKIPS
                    )
                    new_is_no_signal = (decision == "no_signal")
                    existing_is_entered = (
                        existing_decision and existing_decision.startswith("entered")
                    )
                    existing_is_meaningful_skip = (
                        existing_decision == "skipped"
                        and existing_reason
                        and existing_reason.split(" ")[0] not in _LOW_INFO_SKIPS
                    )
                    # Never demote "entered" → anything other than entered.
                    if existing_is_entered and not (decision or "").startswith("entered"):
                        should_write = False
                    # Never demote a meaningful skip → low-info skip / no_signal.
                    elif existing_is_meaningful_skip and (new_is_low_info or new_is_no_signal):
                        should_write = False
                    # Never demote ANY skip → no_signal.
                    elif existing_decision == "skipped" and new_is_no_signal:
                        should_write = False
                    if should_write:
                        row.last_evaluated_at = datetime.utcnow()
                        if kind == "option":
                            row.last_option_decision = decision
                            row.last_option_reason = reason
                        else:
                            row.last_stock_decision = decision
                            row.last_stock_reason = reason
                        _db_d.commit()
            finally:
                _db_d.close()
        except Exception:
            pass
        # r58 transparency: also append to DecisionLog so the Decision
        # Transparency UI can show every per-ticker evaluation, not
        # just the latest verdict on tickers in the candidate_pool.
        # r63: build a rich `details_json` blob with signal levels +
        # market context + the full check_definition for the failing
        # gate, so the UI can render a per-row audit panel.
        try:
            import json as _json_dl
            from database import SessionLocal as _SL_dl, DecisionLog as _DL
            sig_view = getattr(_DECISION_TLS, "signal_view", None) or {}
            # Compute R:R if we have the levels (helpful for the audit UI)
            rr_net = None
            try:
                e = sig_view.get("entry")
                s = sig_view.get("stop_loss")
                t1 = sig_view.get("target1")
                if e and s and t1 and (e - s) > 0:
                    # 12 bps round-trip cost approximation matches consider_signal
                    rr_net = round((t1 - e - 0.0012 * e) / (e - s), 3)
            except Exception:
                pass
            # Resolve the human-friendly definition for the failing reason.
            check_def = None
            if reason:
                try:
                    from services.check_definitions import lookup as _ck_lookup
                    check_def = _ck_lookup(reason, source="trader")
                except Exception:
                    pass
            # Snapshot context (equity, regime, open trades) for the audit panel.
            ctx = {}
            try:
                from services import paper_trader as _pt_ctx
                from services.regime_router import classify_regime as _cr_ctx
                acct = _pt_ctx.get_account()
                if acct:
                    ctx["equity"] = float(acct.get("equity") or 0)
                    ctx["buying_power"] = float(acct.get("buying_power") or 0)
                ctx["open_trades"] = count_open_auto_trades()
                try:
                    ctx["regime"] = _cr_ctx()
                except Exception:
                    pass
            except Exception:
                pass
            # r65: per-gate audit log (computed values + thresholds + formulas)
            gate_log = list(getattr(_DECISION_TLS, "gate_log", []) or [])
            details = {
                "ticker": ticker,
                "kind": kind,
                "decision": decision,
                "reason": reason,
                "signal": {k: v for k, v in sig_view.items() if v is not None},
                "rr_net": rr_net,
                "context": ctx,
                "check_definition": check_def,
                "gate_log": gate_log,
            }
            _db_dl = _SL_dl()
            try:
                # r68-C: cache signal levels on the row so the gate-outcome
                # nightly job (services.gate_telemetry) can compute hindsight
                # P&L without re-parsing the JSON blob.
                _sig_e = sig_view.get("entry")
                _sig_s = sig_view.get("stop_loss")
                _sig_t1 = sig_view.get("target1")
                _db_dl.add(_DL(
                    ticker=ticker,
                    kind=("option_call" if kind == "option" and entered_kind == "call"
                          else "option_put" if kind == "option" and entered_kind == "put"
                          else "option" if kind == "option"
                          else "stock"),
                    decision=decision,
                    reason=reason,
                    confidence=sig_view.get("confidence"),
                    timeframe=sig_view.get("timeframe"),
                    strategy=sig_view.get("strategy"),
                    trade_id=getattr(_DECISION_TLS, "entered_trade_id", None),
                    details_json=_json_dl.dumps(details, default=str),
                    sig_entry=float(_sig_e) if isinstance(_sig_e, (int, float)) else None,
                    sig_stop=float(_sig_s) if isinstance(_sig_s, (int, float)) else None,
                    sig_target1=float(_sig_t1) if isinstance(_sig_t1, (int, float)) else None,
                ))
                _db_dl.commit()
            finally:
                _db_dl.close()
        except Exception:
            pass
    except Exception:
        pass
from services.bear_thesis import build_bear_thesis
from services.bull_thesis import build_bull_thesis
from services.options_analyzer import suggest_options_for_signal
from services.data_fetcher import get_current_price as fetch_current_price
from services.earnings import inside_earnings_window, hours_to_next_earnings
from services.alerts import alert as _raise_alert

logger = logging.getLogger(__name__)


# ---------- Config ---------------------------------------------------------

# ---------- Correlation cache (r43 fix #0.11) -----------------------------
# Rolling 30-day daily-return correlation cache. Sector cap catches
# AAPL+GOOGL+MSFT (all "Technology"), but it doesn't catch NVDA+AMD+AVGO
# in different sub-sectors that move 1:1 with semis-tape, or KO+PEP that
# move with consumer-staples sentiment. The correlation gate adds a 0.7
# threshold across *already-open* positions to prevent silent multi-trade
# correlated exposure.

_corr_cache: Dict[str, "Any"] = {}  # ticker → pd.Series of daily log-returns
_corr_cache_ts: Dict[str, float] = {}
_CORR_CACHE_TTL_SEC = 6 * 3600  # refresh twice/day
_CORR_CACHE_MAX = 256  # r52f: was unbounded; over weeks of universe scans the
                       # cache grew with every ticker ever analyzed (500+ in
                       # universe_scanner). Each entry holds a pandas Series
                       # (~30 floats); aggregate is small but contributes to
                       # the slow memory-creep that triggered r52 OOM-loop.


def _get_returns_series(ticker: str):
    """30-day daily log-returns Series for `ticker`. Cached 6h, bounded LRU."""
    import time as _tt
    now = _tt.time()
    cached = _corr_cache.get(ticker)
    if cached is not None and (now - _corr_cache_ts.get(ticker, 0)) < _CORR_CACHE_TTL_SEC:
        return cached
    try:
        import numpy as _np
        from services.data_fetcher import fetch_ohlcv as _fo
        df = _fo(ticker, "1d")
        if df is None or df.empty or len(df) < 21:
            return None
        closes = df["Close"].tail(31).astype(float)
        rets = _np.log(closes / closes.shift(1)).dropna().tail(30)
        if len(rets) < 15:
            return None
        # r52f: LRU eviction on overflow. Drop the oldest by ts before insert.
        if len(_corr_cache) >= _CORR_CACHE_MAX:
            try:
                oldest = min(_corr_cache_ts, key=_corr_cache_ts.get)
                _corr_cache.pop(oldest, None)
                _corr_cache_ts.pop(oldest, None)
            except (ValueError, KeyError):
                pass
        _corr_cache[ticker] = rets
        _corr_cache_ts[ticker] = now
        return rets
    except Exception:
        return None


def correlated_with_open(new_ticker: str, open_tickers: List[str], threshold: float = 0.70) -> List[str]:
    """Return list of open tickers whose 30d return-correlation with
    `new_ticker` exceeds `threshold`. Used by the correlation gate in
    `consider_signal` to reject piling into already-correlated exposure.

    Empty list when correlation can't be computed (data unavailable, too
    few overlapping days, etc.) — fail-open in this direction is safe
    because the sector cap is the primary defense.
    """
    if not new_ticker or not open_tickers:
        return []
    new_rets = _get_returns_series(new_ticker)
    if new_rets is None:
        return []
    correlated: List[str] = []
    for t in open_tickers:
        if t == new_ticker:
            continue
        other = _get_returns_series(t)
        if other is None:
            continue
        try:
            joined = new_rets.to_frame("a").join(other.to_frame("b"), how="inner")
            if len(joined) < 15:
                continue
            corr = float(joined["a"].corr(joined["b"]))
            if corr >= threshold:
                correlated.append(t)
        except Exception:
            continue
    return correlated


def _backfill_ml_outcome(db: Session, t) -> None:
    """r44 fix #0.3: when an AutoTrade closes, find its corresponding
    MLPrediction row(s) and set outcome / realized_pl / closed_at. Without
    this the calibration plot and the ML graduation gate (≥200 closed-trade
    predictions) cannot evaluate.

    We match by trade_id when persisted; otherwise by (ticker, signal_type,
    created_at within ±2h of the trade's opened_at).
    """
    try:
        from database import MLPrediction
        from datetime import datetime as _dt, timedelta as _td
        outcome = 1 if (t.realized_pl is not None and t.realized_pl > 0) else 0
        rpnl = float(t.realized_pl) if t.realized_pl is not None else None
        closed_at = t.closed_at or _dt.utcnow()
        rows = []
        if t.id:
            rows = db.query(MLPrediction).filter(
                MLPrediction.trade_id == t.id,
                MLPrediction.outcome.is_(None),
            ).all()
        if not rows and t.opened_at:
            # r46 fix #0.12: widen the prediction-to-trade match window from
            # ±2h to ±24h. A trade that takes >2h to fill (limit-at-mid that
            # crosses, partial-fill chains, IOC retries) was previously
            # missing its backfill — outcome stayed NULL forever, biasing the
            # calibration plot toward fast fills.
            rows = db.query(MLPrediction).filter(
                MLPrediction.ticker == t.ticker,
                MLPrediction.signal_type == ("BUY" if (t.side or "buy") == "buy" else "SELL"),
                MLPrediction.outcome.is_(None),
                MLPrediction.created_at >= t.opened_at - _td(hours=24),
                MLPrediction.created_at <= t.opened_at + _td(hours=24),
            ).all()
        for r in rows:
            r.outcome = outcome
            r.realized_pl = rpnl
            r.closed_at = closed_at
            if r.trade_id is None and t.id:
                r.trade_id = t.id
    except Exception as e:
        logger.debug(f"_backfill_ml_outcome trade {getattr(t, 'id', None)}: {e}")


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
            "pdt_enforce": bool(getattr(cfg, "pdt_enforce", False)),
            "auto_promote_adopted": bool(getattr(cfg, "auto_promote_adopted", False)),
            # r57 schema-drift fix #2: status now exposes the r53-r56
            # config fields the operator may need to observe/toggle.
            "entry_1m_gate_mode": getattr(cfg, "entry_1m_gate_mode", "relaxed") or "relaxed",
            "rr_min": float(getattr(cfg, "rr_min", 1.3) or 1.3),
            "loss_pattern_mode": getattr(cfg, "loss_pattern_mode", "shadow") or "shadow",
            "source_mute_enabled": bool(getattr(cfg, "source_mute_enabled", False)),
            "theta_adjusted_rr_enabled": bool(getattr(cfg, "theta_adjusted_rr_enabled", True)),
            "portfolio_kelly_enabled": bool(getattr(cfg, "portfolio_kelly_enabled", True)),
            "vol_target_annual": float(getattr(cfg, "vol_target_annual", 0.12) or 0.12),
            "leverage_cap": float(getattr(cfg, "leverage_cap", 1.5) or 1.5),
            "book_var_99_cap_pct": float(getattr(cfg, "book_var_99_cap_pct", 0.05) or 0.05),
            "bracket_tif": getattr(cfg, "bracket_tif", "day") or "day",
            "max_correlated_open": int(getattr(cfg, "max_correlated_open", 1) or 1),
            # r58: option-floor configs (previously hardcoded)
            "option_thesis_min_conf_aggressive": float(getattr(cfg, "option_thesis_min_conf_aggressive", 60.0) or 60.0),
            "option_thesis_min_conf_mult": float(getattr(cfg, "option_thesis_min_conf_mult", 0.85) or 0.85),
            "option_contract_min_score": float(getattr(cfg, "option_contract_min_score", 65.0) or 65.0),
            "option_contract_min_score_aggressive": float(getattr(cfg, "option_contract_min_score_aggressive", 55.0) or 55.0),
            # r60: universe source
            "universe_source": getattr(cfg, "universe_source", "russell1000") or "russell1000",
            # r68-A: equity-snapshot freshness watchdog
            "equity_snapshot_max_age_min": float(getattr(cfg, "equity_snapshot_max_age_min", 15.0) or 15.0),
            # r69: setup-quality composite gate
            "setup_quality_min": float(getattr(cfg, "setup_quality_min", 55.0) or 55.0),
            "setup_quality_gate_enabled": bool(getattr(cfg, "setup_quality_gate_enabled", False)),
        }
    finally:
        db.close()


# ---------- Kill switch + daily loss bookkeeping --------------------------

def _session_start_utc() -> datetime:
    """Start of the current trading day in UTC, anchored to MIDNIGHT ET.

    r53 fix (Tier-0 #4): previously anchored to 9:30 ET, the regular
    session open. That meant a trade that closed during pre-market
    (e.g., 8:00 AM ET on a gap-down force-close) counted toward
    YESTERDAY's daily-loss gate. After-hours and pre-market losses then
    became momentarily invisible at 9:30 ET when the counter reset —
    the operator could absorb a -3% overnight loss and still take a
    fresh -3% in the regular session before the gate fired.

    New anchor: the most recent 00:00 America/New_York boundary. Pre-
    market, regular-session, and after-hours closes all count toward
    "today" the way a trader naturally thinks about it.

    Returned as a naive UTC datetime (matches the rest of the codebase,
    which uses datetime.utcnow()).
    """
    from datetime import timedelta as _td
    try:
        from zoneinfo import ZoneInfo
    except Exception:  # py<3.9 — should never happen in our stack
        ZoneInfo = None
    now_utc = datetime.utcnow()
    if ZoneInfo is None:
        # Fallback if zoneinfo is unavailable.
        is_edt = 3 <= now_utc.month <= 10 or (now_utc.month == 11 and now_utc.day <= 7)
        midnight_et_utc_hour = 4 if is_edt else 5  # 00:00 EDT = 04:00 UTC, 00:00 EST = 05:00 UTC
        anchor = now_utc.replace(hour=midnight_et_utc_hour, minute=0, second=0, microsecond=0)
        if now_utc < anchor:
            anchor = anchor - _td(days=1)
        return anchor
    et = ZoneInfo("America/New_York")
    # Today's 00:00 ET in ET coordinates → convert to UTC.
    now_et = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(et)
    today_midnight_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    if now_et < today_midnight_et:
        today_midnight_et = today_midnight_et - _td(days=1)
    anchor_utc = today_midnight_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return anchor_utc


def realized_pnl_today() -> float:
    """Sum of realized_pl on auto-trades closed since 00:00 ET today
    (r53 — was 9:30 ET session-start, which let after-hours and
    pre-market losses momentarily evade the daily-loss gate). Used by
    the daily-loss gate and surfaced on /api/health for observability."""
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
    """Count rows that consume a concurrent-position slot.

    Includes `adopted` (r41 sync) — those represent operator-accepted
    external positions that consume capital and contribute to the
    portfolio's correlation profile, even though the bot doesn't trail/
    exit them. Excluding them would let the bot enter a new trade on
    top of an already-correlated external position past the cap.
    """
    db = SessionLocal()
    try:
        return db.query(AutoTrade).filter(
            AutoTrade.status.in_(["pending", "open", "adopted"])
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

    r53 fix (Tier-2 #12): acquire `_entry_lock` for the duration so a
    consider_signal in flight cannot pass the killed-flag check, then
    proceed past kill() into bracket-submit. Without this, the operator
    could click kill, kill flattens, and a NEW position opens because
    the in-flight signal already passed the killed check before kill()
    flipped the flag.
    """
    # r53: hold the entry lock so no in-flight consider_signal can race
    # past the killed-flag check while we're flattening. Block up to 10s
    # for in-flight to complete; if it doesn't, proceed anyway (kill
    # priority > entry).
    _kill_holds_entry_lock = _entry_lock.acquire(timeout=10.0)
    if not _kill_holds_entry_lock:
        logger.warning("kill(): entry lock not acquired in 10s; proceeding anyway")

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

    # r47 fix #T0b-5: prior code called cancel_all_orders() with NO symbol
    # filter, wiping every working order in the Alpaca account — including
    # operator/IRA/manual hedges. Now we cancel ONLY bot-owned order ids
    # tracked on AutoTrade rows (parent_order_id, stop_order_id, tp_order_id).
    if cancel_orders and paper_trader.is_enabled():
        try:
            db_c = SessionLocal()
            try:
                _bot_order_ids = set()
                for r in db_c.query(AutoTrade).filter(
                    AutoTrade.status.in_(["pending", "open", "adopted"])
                ).all():
                    for oid in (r.parent_order_id, r.stop_order_id, r.tp_order_id):
                        if oid:
                            _bot_order_ids.add(oid)
                for oid in _bot_order_ids:
                    try:
                        paper_trader.cancel_order(oid)
                        cancelled += 1
                    except Exception as ce:
                        logger.warning(f"kill(): cancel order {oid} failed: {ce}")
            finally:
                db_c.close()
        except Exception as e:
            logger.error(f"kill(): cancel_bot_orders failed: {e}")

    if flatten and paper_trader.is_enabled():
        try:
            # Only flatten POSITIONS that map to a bot AutoTrade row;
            # operator-side positions (manual scalps, hedges) are not ours
            # to flatten.
            db_p = SessionLocal()
            try:
                bot_tickers = set(
                    r.ticker for r in db_p.query(AutoTrade).filter(
                        AutoTrade.status.in_(["pending", "open", "adopted"])
                    ).all()
                )
            finally:
                db_p.close()
            for tk in bot_tickers:
                try:
                    res = paper_trader.close_position(tk)
                    if "error" not in res:
                        flattened.append(tk)
                except Exception as fe:
                    logger.warning(f"kill(): close {tk} failed: {fe}")
        except Exception as e:
            logger.error(f"kill(): bot-only flatten failed: {e}")

    # r39 audit fix #23: previously kill() flattened the broker but did NOT
    # update DB AutoTrade.status for open rows. Combined with the manage
    # loop's (now-fixed) `if True:` bug at line 2647, this meant phantom
    # `open` / `pending` rows persisted indefinitely in the dashboard
    # and counted toward `count_open_auto_trades()`, blocking the
    # concurrent cap. Now: flip every open/pending row to
    # `closed_kill` so the dashboard and counters reflect reality.
    db2 = SessionLocal()
    db_closed = 0
    try:
        from datetime import datetime as _dt_kill
        open_rows = db2.query(AutoTrade).filter(
            AutoTrade.status.in_(["pending", "open", "adopted"])
        ).all()
        # r47 fix #T0b-2: release accumulated BP reservation. Without this,
        # the in-flight reservation persisted across the kill and the next
        # post-unkill entry sized against a phantom reservation.
        try:
            from services.risk_manager import _release_bp
        except Exception:
            _release_bp = None
        for row in open_rows:
            row.status = "closed_kill"
            row.closed_at = _dt_kill.utcnow()
            row.note = (row.note or "") + f" | KILLED: {reason or 'unspecified'}"
            row.target_touch_count = 0
            _target_touch_counts.pop(row.id, None)
            db_closed += 1
            try:
                if _release_bp and (row.asset_type or "stock") == "stock":
                    _release_bp(float(row.entry_price or row.requested_entry or 0)
                                * float(row.original_qty or row.qty or 0))
            except Exception:
                pass
        db2.commit()
        # Hard-reset to defend against any pending callers still in flight.
        try:
            from services.risk_manager import _reset_in_flight_bp
            _reset_in_flight_bp()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"kill(): DB status update failed: {e}")
    finally:
        db2.close()

    logger.critical(
        f"AUTO-TRADER KILLED reason={reason!r} flattened={len(flattened)} "
        f"cancelled={cancelled} db_rows_closed={db_closed}"
    )
    metrics.inc("autotrade_event", event="killed")
    # r53 (Tier-2 #12): release the entry lock so future unkill+enable
    # cycles can proceed cleanly.
    if _kill_holds_entry_lock:
        try:
            _entry_lock.release()
        except Exception:
            pass
    return {
        "killed": True,
        "flattened": flattened,
        "cancelled": cancelled,
        "db_rows_closed": db_closed,
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
        # Include `adopted` (r41 sync) — those represent operator-accepted
        # external positions that the bot doesn't manage but DOES know
        # about, so they shouldn't refire the alert.
        open_tickers = {
            r.ticker for r in db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending", "open", "adopted"]),
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
                f"possible option assignment or external manual trade. Reconcile via "
                f"POST /api/admin/sync-positions to adopt or close the row.",
                ticker=sym,
            )

    return {"unexpected": unexpected, "count": len(unexpected)}


def sync_positions_from_alpaca() -> Dict[str, Any]:
    """Reconcile the Alpaca account against the `auto_trades` table.

    **Alpaca is the source of truth** for actual capital deployment;
    `auto_trades` is the bot's record of what IT opened. Divergence
    happens for legitimate reasons:
      * Option assignment converts a held option into shares.
      * The operator places a manual trade via the Alpaca dashboard.
      * A bracket leg fills via a path the manage loop didn't observe.
      * A position was closed externally without the bot knowing.

    Two reconciliation paths (idempotent — safe to re-run any time):

      1. **Adopt**: an Alpaca stock position with no matching open
         AutoTrade row → insert a row with `status="adopted"`. These
         rows count toward portfolio capital + heat math but are
         skipped by the manage loop (no auto-trail, no auto-exit —
         the operator handles them externally). They DO suppress
         future `unexpected_position` alerts.

      2. **Close-external**: an open AutoTrade row with no matching
         Alpaca position → mark `status="closed_external"` with note.
         The position closed via some path the bot didn't see (manual
         flatten, bracket leg fill we missed, broker-side reconciliation).

    Pending rows are not reconciled either way — they may be in flight
    at the broker. Multi-leg option symbols (>6 chars) are skipped on
    the adopt side; option positions are managed via the OCC-symbol-
    keyed AutoTrade rows.

    Returns `{adopted: [...], closed_external: [...]}` for operator review.
    """
    if not paper_trader.is_enabled():
        return {"adopted": [], "closed_external": [], "note": "broker disabled"}
    try:
        alpaca_positions = paper_trader.get_positions() or []
    except Exception as e:
        logger.warning(f"sync_positions_from_alpaca: Alpaca fetch failed: {e}")
        return {"adopted": [], "closed_external": [], "error": str(e)}

    # Build the Alpaca-side stock map: ticker → position dict.
    alpaca_stocks: Dict[str, Dict[str, Any]] = {}
    for pos in alpaca_positions:
        sym = (pos.get("symbol") or "").upper()
        qty = float(pos.get("qty") or 0)
        if not sym or qty == 0 or len(sym) > 6:
            continue
        alpaca_stocks[sym] = pos

    adopted: List[Dict[str, Any]] = []
    closed_external: List[Dict[str, Any]] = []

    db = SessionLocal()
    try:
        # Snapshot: every stock row in pending / open / adopted state.
        open_rows = db.query(AutoTrade).filter(
            AutoTrade.status.in_(["pending", "open", "adopted"]),
            AutoTrade.asset_type == "stock",
        ).all()
        db_tickers = {r.ticker: r for r in open_rows}

        # 1) ADOPT: Alpaca position has no DB row → create adopted row
        for sym, pos in alpaca_stocks.items():
            if sym in db_tickers:
                continue
            qty = float(pos.get("qty") or 0)
            avg_entry = float(pos.get("avg_entry_price") or 0)
            if avg_entry <= 0:
                continue
            # r47 fix #T0f-3: prior placeholder stop was 0.95×entry regardless
            # of side; for short positions (qty<0) the stop should be ABOVE
            # entry (1.05×). The wrong-direction stop polluted heat math
            # (current_portfolio_heat returned 0 for shorts via max(0,...)).
            _is_short = qty < 0
            _stop = round(avg_entry * (1.05 if _is_short else 0.95), 2)
            _t1 = round(avg_entry * (0.95 if _is_short else 1.05), 2)
            row = AutoTrade(
                ticker=sym,
                symbol=sym,
                asset_type="stock",
                side="buy" if not _is_short else "sell",
                qty=abs(qty),
                original_qty=abs(qty),
                requested_entry=avg_entry,
                entry_price=avg_entry,
                stop_loss=_stop,
                current_stop=_stop,
                target1=_t1,
                level_index=0,
                status="adopted",
                opened_at=datetime.utcnow(),
                filled_at=datetime.utcnow(),
                note=(
                    f"ADOPTED from Alpaca {datetime.utcnow().isoformat()}: "
                    f"external {'SHORT' if _is_short else 'LONG'} position "
                    f"(qty={qty}, avg_entry=${avg_entry:.2f}). "
                    f"Bot will NOT trail/exit this position — operator manages externally."
                ),
            )
            db.add(row)
            adopted.append({
                "ticker": sym, "qty": qty, "avg_entry": avg_entry,
            })

        # 2) CLOSE-EXTERNAL: open DB row has no Alpaca position → mark closed
        # r47 fix #T0b-3: pending rows whose parent order is terminal at the
        # broker (filled/cancelled/rejected) and have NO Alpaca position
        # should be reconciled too — prior code just skipped, leaving
        # phantom-pending rows after a crash mid-submit.
        for r in open_rows:
            if r.status == "adopted" and r.ticker in alpaca_stocks:
                continue
            if r.status == "open" and r.ticker in alpaca_stocks:
                continue
            if r.status == "pending":
                # Decide: still in flight, or terminal-with-no-position?
                _terminal = False
                try:
                    if r.parent_order_id:
                        c = paper_trader._get_client()
                        po = c.get_order_by_id(r.parent_order_id)
                        ps = str(getattr(po, "status", "") or "").lower()
                        if any(s in ps for s in ("rejected", "canceled", "cancelled", "expired")):
                            _terminal = True
                        if "filled" in ps and r.ticker not in alpaca_stocks:
                            # filled but flat at broker now → closed externally
                            _terminal = True
                except Exception:
                    # Conservative: leave pending alone if broker query failed
                    continue
                if not _terminal:
                    continue
                if r.ticker in alpaca_stocks:
                    continue
            # adopted-but-no-longer-on-alpaca → closed externally
            # open-but-no-longer-on-alpaca → closed externally
            # pending+terminal+no-broker-position → closed externally
            if r.ticker not in alpaca_stocks:
                r.status = "closed_external"
                r.closed_at = datetime.utcnow()
                r.note = (r.note or "") + (
                    f" | RECONCILED {datetime.utcnow().isoformat()}: "
                    f"Alpaca no longer reports a position for {r.ticker}; "
                    f"closed externally"
                )
                closed_external.append({"trade_id": r.id, "ticker": r.ticker})

        db.commit()
    finally:
        db.close()

    logger.warning(
        f"sync_positions_from_alpaca: adopted={len(adopted)} closed_external={len(closed_external)}"
    )
    # r47 T1 obs P1-10: surface divergence as an alert so operators don't have
    # to manually poll the sync endpoint to discover broker-vs-bot drift.
    try:
        if adopted or closed_external:
            from services.alerts import alert as _raise_alert
            n = len(adopted) + len(closed_external)
            sev = "warning" if n <= 2 else "error"
            tickers = sorted({a["ticker"] for a in adopted} |
                             {c["ticker"] for c in closed_external})
            _raise_alert(
                sev, "position_divergence",
                f"reconcile drift: adopted={len(adopted)} closed_external={len(closed_external)} "
                f"tickers={tickers}",
            )
    except Exception:
        pass
    return {"adopted": adopted, "closed_external": closed_external}


def auto_reconcile_positions() -> Dict[str, Any]:
    """Periodic Alpaca-DB reconciler — replaces `detect_unexpected_positions`
    on the scheduler.

    Behavior depends on `cfg.auto_promote_adopted`:

      * False (default, paper-safe): runs `detect_unexpected_positions`
        only — alerts on Alpaca positions with no DB row, takes no
        action. Same as the original r34 behavior. Operator runs
        `/api/admin/sync-positions` manually to resolve.

      * True (opt-in via UI / config): runs `sync_positions_from_alpaca`
        + `promote_adopted_to_managed` for each freshly-adopted ticker.
        Every external position is automatically: adopted (DB row) →
        promoted (status=open, broker SL submitted, bot-computed
        targets) → managed by the manage loop. No operator action
        required.

    Promotion failures don't abort the reconcile — the row stays
    adopted (no SL submitted, bot won't trail). Operator is alerted
    via the existing `force_close_failed`-style channel for failed
    SL submits.

    Returns a summary dict keyed on the path taken so logs surface
    which mode the job ran in.
    """
    db = SessionLocal()
    try:
        cfg = get_config(db)
        flag = bool(getattr(cfg, "auto_promote_adopted", False))
    finally:
        db.close()

    if not flag:
        return {"mode": "detect_only", **detect_unexpected_positions()}

    sync_result = sync_positions_from_alpaca()
    adopted = sync_result.get("adopted", [])
    promotions = []
    for entry in adopted:
        ticker = entry.get("ticker")
        if not ticker:
            continue
        try:
            r = promote_adopted_to_managed(ticker)
            promotions.append({"ticker": ticker, **r})
        except Exception as e:
            logger.error(f"auto_reconcile_positions: promote {ticker} failed: {e}")
            promotions.append({"ticker": ticker, "ok": False, "reason": str(e)})
    return {
        "mode": "auto_promote",
        "adopted": adopted,
        "promotions": promotions,
        "closed_external": sync_result.get("closed_external", []),
    }


def _compute_managed_levels(ticker: str, direction: str, current_price: float) -> Dict[str, Any]:
    """Derive stop / T1 / T2 / T3 for a position we already hold.

    Different problem than `signal_generator.generate_signal` (which decides
    *whether* to enter): we already hold the position, we just need
    reasonable exit levels anchored to CURRENT price.

    Approach: 1.5 × ATR stop distance (matches the live entry-side
    `STOP_ATR_MULT_BY_TF["1d"] = 2.0` minus a tightening for "we've
    already deployed capital, accept slightly tighter risk envelope").
    Targets at 1.5R / 2.5R / 4R from current — same R-multiple ladder
    the live signal_generator uses for stocks. Falls back to 2% / 3% / 5%
    / 8% percentage moves if ATR can't be computed.

    Args:
        ticker: symbol (uppercase)
        direction: "BUY" (long position) or "SELL" (short)
        current_price: live price to anchor levels on

    Returns:
        `{stop_loss, target1, target2, target3, atr, rationale}` —
        rationale string explains how levels were chosen for audit.
    """
    atr = None
    try:
        atr = _chandelier_atr(ticker)
    except Exception:
        atr = None

    if atr and atr > 0:
        risk = 1.5 * atr
        rationale = f"ATR-based: 1.5×ATR({atr:.2f})={risk:.2f} stop distance"
    else:
        risk = current_price * 0.02
        rationale = f"ATR unavailable; 2%-of-price={risk:.2f} stop distance"

    if direction == "BUY":
        stop = round(current_price - risk, 2)
        t1 = round(current_price + 1.5 * risk, 2)
        t2 = round(current_price + 2.5 * risk, 2)
        t3 = round(current_price + 4.0 * risk, 2)
    else:
        stop = round(current_price + risk, 2)
        t1 = round(current_price - 1.5 * risk, 2)
        t2 = round(current_price - 2.5 * risk, 2)
        t3 = round(current_price - 4.0 * risk, 2)

    return {
        "stop_loss": stop,
        "target1": t1,
        "target2": t2,
        "target3": t3,
        "atr": atr,
        "rationale": rationale,
    }


def promote_adopted_to_managed(ticker: str) -> Dict[str, Any]:
    """Promote an `adopted` AutoTrade row to `open` with bot-computed
    targets, submitting a real broker stop-loss order so the manage loop
    will trail / exit it like any other trade.

    Workflow:
      1. Find the adopted row for the ticker (idempotent — if already
         `open`, returns early with a note).
      2. Verify Alpaca still reports a position for it (positions can
         disappear between sync and promote; if missing, mark
         `closed_external` instead).
      3. Compute fresh stop/T1/T2/T3 from CURRENT price + ATR (not the
         original adoption entry — that's a sunk anchor; new trail
         needs to bracket today's price).
      4. Submit a real broker stop-loss order via `paper_trader._get_client()`.
      5. Update the row: `status="open"`, `current_stop`, `target1/2/3`,
         `stop_order_id`, append `note`.
      6. The manage loop's next tick will pick the row up and run the
         normal trailing/partial-exit/exit state machine on it.

    The `entry_price` field is preserved at the original adoption value
    so realized PnL is computed against the actual cost basis. The
    `requested_entry` is also preserved for audit.

    Returns: `{ok, trade_id, ticker, current_price, levels, stop_order_id}`
    on success, or `{ok: False, reason}` on failure.
    """
    if not paper_trader.is_enabled():
        return {"ok": False, "reason": "broker disabled"}

    ticker = ticker.strip().upper()
    if not ticker:
        return {"ok": False, "reason": "ticker required"}

    db = SessionLocal()
    try:
        row = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.status == "adopted",
            AutoTrade.asset_type == "stock",
        ).order_by(AutoTrade.opened_at.desc()).first()
        if row is None:
            return {"ok": False, "reason": f"no adopted stock row for {ticker}"}

        # Verify Alpaca still has the position
        try:
            alpaca_positions = paper_trader.get_positions() or []
        except Exception as e:
            return {"ok": False, "reason": f"alpaca fetch failed: {e}"}
        alpaca_pos = next(
            (p for p in alpaca_positions
             if (p.get("symbol") or "").upper() == ticker
             and float(p.get("qty") or 0) != 0),
            None,
        )
        if not alpaca_pos:
            row.status = "closed_external"
            row.closed_at = datetime.utcnow()
            row.note = (row.note or "") + (
                f" | RECONCILED at promote: Alpaca no longer reports a position for {ticker}"
            )
            db.commit()
            return {"ok": False, "reason": "no Alpaca position; row marked closed_external"}

        live_qty = abs(float(alpaca_pos.get("qty") or 0))
        if live_qty != float(row.qty or 0):
            # Alpaca qty drifted from the adoption snapshot — sync the row
            # first so the SL we submit matches the actual position size.
            row.qty = live_qty
            row.note = (row.note or "") + (
                f" | qty resync at promote: row→{live_qty} from Alpaca"
            )

        # Direction: long stock = SELL stop. (Bot only adopts long stocks today.)
        is_long = float(alpaca_pos.get("qty") or 0) > 0
        direction = "BUY" if is_long else "SELL"

        # Live price anchor — fetch fresh. A stale anchor would skew the
        # whole stop/target ladder, so we refuse the promote rather than
        # accept a degraded number. (`fetch_current_price` returns
        # `(price, change_pct)` or None.)
        current_price = None
        try:
            pi = fetch_current_price(ticker)
            if pi:
                current_price = float(pi[0])
        except Exception:
            current_price = None
        if current_price is None or current_price <= 0:
            return {"ok": False, "reason": "could not fetch live price for level computation"}

        levels = _compute_managed_levels(ticker, direction, current_price)
        new_stop = float(levels["stop_loss"])

        # Submit the broker stop-loss leg
        try:
            from alpaca.trading.requests import StopOrderRequest
            from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
            c = paper_trader._get_client()
            sell_side = _OS.SELL if is_long else _OS.BUY
            stop_res = c.submit_order(order_data=StopOrderRequest(
                symbol=ticker, qty=int(live_qty), side=sell_side,
                time_in_force=_TIF.GTC, stop_price=round(new_stop, 2),
            ))
            stop_order_id = getattr(stop_res, "id", None)
        except Exception as e:
            logger.error(f"promote_adopted: SL submit failed for {ticker}: {e}")
            return {"ok": False, "reason": f"broker SL submit failed: {e}"}

        # Promote: status open, real levels, note the transition
        row.status = "open"
        row.stop_loss = new_stop
        row.current_stop = new_stop
        row.target1 = float(levels["target1"])
        row.target2 = float(levels["target2"])
        row.target3 = float(levels["target3"])
        row.stop_order_id = str(stop_order_id) if stop_order_id else None
        row.note = (row.note or "") + (
            f" | PROMOTED-TO-MANAGED {datetime.utcnow().isoformat()}: "
            f"current=${current_price:.2f}, levels {levels['rationale']}; "
            f"stop=${new_stop:.2f}, T1=${levels['target1']:.2f}, "
            f"T2=${levels['target2']:.2f}, T3=${levels['target3']:.2f}, "
            f"stop_order_id={stop_order_id}"
        )
        db.commit()
        logger.warning(
            f"promote_adopted_to_managed: {ticker} #{row.id} promoted to open. "
            f"qty={live_qty}, current=${current_price:.2f}, "
            f"stop=${new_stop:.2f}, targets=({levels['target1']}, {levels['target2']}, {levels['target3']})"
        )
        return {
            "ok": True,
            "trade_id": row.id,
            "ticker": ticker,
            "qty": live_qty,
            "current_price": current_price,
            "levels": levels,
            "stop_order_id": str(stop_order_id) if stop_order_id else None,
        }
    finally:
        db.close()


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


# Calibration cache moved to services.risk_manager.


def strategy_scorecard(
    days: int = 60,
    min_trades: int = 5,
    asset_type: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Profit-audit #8: per-strategy realized P&L over the last N days.

    Joins closed AutoTrade rows with their originating Signal to bucket by
    `Signal.strategy`. Returns {strategy_name: {n, wins, win_rate, avg_pl,
    total_pl, multiplier, asset_type_split}}.

    r53 fix (Tier-0 #3): added `asset_type` filter so the scorecard can
    return stock-only or option-only stats. The unified WR was misleading
    because the backtester only models stocks while the live engine
    routes ~half of high-confidence signals to options. Aggregate stats
    no longer assume the two paths share an edge.

    When `asset_type` is None (default), returns the unified view but
    with a per-strategy `asset_type_split` showing the stock/option mix.

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
        if asset_type:
            q = q.filter(AutoTrade.asset_type == asset_type)
        rows = q.all()
    finally:
        db.close()

    buckets: Dict[str, Dict[str, Any]] = {}
    for t, s in rows:
        name = (s.strategy if s and s.strategy else "unknown")
        b = buckets.setdefault(
            name,
            {"n": 0, "wins": 0, "total_pl": 0.0, "n_stock": 0, "n_option": 0,
             "stock_pl": 0.0, "option_pl": 0.0, "stock_wins": 0, "option_wins": 0},
        )
        b["n"] += 1
        pl = t.realized_pl or 0.0
        if pl > 0:
            b["wins"] += 1
        b["total_pl"] += pl
        atype = (t.asset_type or "stock").lower()
        if atype == "option":
            b["n_option"] += 1
            b["option_pl"] += pl
            if pl > 0:
                b["option_wins"] += 1
        else:
            b["n_stock"] += 1
            b["stock_pl"] += pl
            if pl > 0:
                b["stock_wins"] += 1

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
            # r53: stock vs option split, since the backtester only models
            # stocks. Option WR diverging significantly from stock WR is a
            # signal that the strategy doesn't transfer to options.
            "asset_type_split": {
                "stock": {
                    "n": int(b["n_stock"]),
                    "wins": int(b["stock_wins"]),
                    "win_rate": round(b["stock_wins"] / b["n_stock"], 3) if b["n_stock"] else None,
                    "total_pl": round(b["stock_pl"], 2),
                },
                "option": {
                    "n": int(b["n_option"]),
                    "wins": int(b["option_wins"]),
                    "win_rate": round(b["option_wins"] / b["n_option"], 3) if b["n_option"] else None,
                    "total_pl": round(b["option_pl"], 2),
                },
            },
        }
    return out


# Empirical multipliers + their caches moved to services.risk_manager
from services.risk_manager import (
    strategy_multiplier,
    calibration_multiplier,
)


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


# ---------- AI judge context builder --------------------------------------

def _build_ai_context(ticker: str, db: Session) -> Dict[str, Any]:
    """Compact semantic context for the AI judge calls.

    Each section is best-effort — missing data falls through to None so a
    cold-cache or DB hiccup degrades gracefully into a less-informed
    Claude call rather than crashing the entry path.
    """
    ctx: Dict[str, Any] = {"ticker": ticker}

    # r44 fix #0.1: real columns are `symbols` (CSV) and `headline`. Prior
    # code queried NewsEvent.tickers and read n.title — both raised
    # AttributeError, swallowed by the try/except, leaving `recent_news=[]`
    # for EVERY AI judge call. The judge has been deciding entry-veto,
    # confidence-multiplier, and news-exit blind to news for the entire
    # AI-judge lifetime.
    try:
        from database import NewsEvent
        from sqlalchemy import or_ as _or
        from datetime import datetime as _dt, timedelta as _td
        cutoff = _dt.utcnow() - _td(hours=24)
        news = (
            db.query(NewsEvent)
            .filter(_or(
                NewsEvent.ticker == ticker,
                NewsEvent.symbols.like(f"%{ticker}%"),
            ))
            .filter(NewsEvent.published_at >= cutoff)
            .order_by(NewsEvent.published_at.desc())
            .limit(5).all()
        )
        ctx["recent_news"] = [
            {
                "title": n.headline,
                "sentiment": n.sentiment_label,
                "score": float(n.sentiment_score) if n.sentiment_score is not None else None,
                "published_at": n.published_at.isoformat() if n.published_at else None,
            }
            for n in news
        ]
    except Exception as _ne:
        logger.warning(f"_build_ai_context news lookup failed for {ticker}: {_ne}")
        ctx["recent_news"] = []

    # Fundamentals snapshot
    try:
        from services.fundamentals import get_fundamentals
        f = get_fundamentals(ticker) or {}
        ctx["fundamentals"] = {
            "sector": f.get("sector"),
            "industry": f.get("industry"),
            "market_cap": f.get("market_cap"),
            "pe": f.get("trailing_pe"),
            "beta": f.get("beta"),
            "short_pct_float": f.get("short_pct_float"),
        }
    except Exception:
        ctx["fundamentals"] = {}

    # Other open positions in the same sector — context for cross-trade
    # correlation concern (already enforced by sector cap, but Claude can
    # spot when the cluster is one news event away from a coordinated drawdown)
    try:
        sector = (ctx.get("fundamentals") or {}).get("sector")
        if sector:
            from services.fundamentals import get_fundamentals as _gf
            # Include adopted — AI should see externally-held sector exposure too.
            open_others = db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending", "open", "adopted"])
            ).all()
            same = []
            for ot in open_others:
                if ot.ticker == ticker: continue
                try:
                    s = (_gf(ot.ticker) or {}).get("sector")
                except Exception:
                    s = None
                if s == sector:
                    same.append(ot.ticker)
            ctx["open_positions_same_sector"] = same
    except Exception:
        ctx["open_positions_same_sector"] = []

    # Analyst rating + insider/institutional/social signals (if available)
    for src_key, accessor in [
        ("analyst_rating", "services.fundamentals.get_analyst_rating"),
        ("insider", "services.insider_trades.get_insider_summary"),
        ("social", "services.social_sentiment.get_sentiment"),
    ]:
        try:
            mod_name, fn_name = accessor.rsplit(".", 1)
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            if fn:
                ctx[src_key] = fn(ticker)
        except Exception:
            ctx[src_key] = None

    return ctx


# ---------- Budget bookkeeping --------------------------------------------

def _open_allocations(db: Session) -> Dict[str, float]:
    """Sum notional of currently-open auto trades, by asset_type.

    Includes `adopted` rows (r41 sync) — they consume capital just
    like normal open positions, even though the bot doesn't manage them.
    Excluding them would over-state available stock/option budget.
    """
    open_trades = db.query(AutoTrade).filter(
        AutoTrade.status.in_(["pending", "open", "adopted"])
    ).all()
    out = {"stock": 0.0, "option": 0.0}
    for t in open_trades:
        px = t.entry_price or t.requested_entry or 0.0
        # Options trade in 100-share contracts, premium is per share
        mult = 100.0 if t.asset_type == "option" else 1.0
        out[t.asset_type] = out.get(t.asset_type, 0.0) + px * t.qty * mult
    return out


def _safe_crisis_mode() -> bool:
    """r53m: defensive read for the SafetyBanner — never raises."""
    try:
        from services.risk_manager import in_crisis_mode as _icm
        return bool(_icm())
    except Exception:
        return False


def status_snapshot() -> Dict[str, Any]:
    """Return current budget state — used by the UI status pill.

    r42 fix #0.6/0.7: also surfaces freeze reason, BP/broker circuit-breaker
    state, PDT exposure, and adopted-position count so the dashboard can
    render unmissable safety banners. The UI was previously blind to all of
    these states — `Auto-Trader: Paused` looked identical to a manual
    pause vs a real freeze.
    """
    db = SessionLocal()
    try:
        cfg = get_config(db)
        acct = paper_trader.get_account()
        equity = float(acct["equity"]) if acct else 0.0
        alloc = _open_allocations(db)
        stock_budget = equity * cfg.stock_pct_of_equity
        # VIX-scaled option allocation: options are punished harder than stocks
        # during vol spikes (IV crush, gamma whipsaw), so we shrink the options
        # bucket when VIX elevates. Stocks bucket is left untouched.
        from services.risk_manager import (
            vix_options_bucket_multiplier as _vix_opt_mult,
            should_freeze_trading as _should_freeze,
            bp_breaker_active as _bp_active,
            broker_down as _broker_down,
            bp_exhausted_until as _bp_until,
            broker_down_until as _broker_until,
            pdt_day_trade_count as _pdt_count,
        )
        option_budget = equity * cfg.option_pct_of_equity * _vix_opt_mult()
        total_cap = equity * cfg.max_pct_of_equity
        deployed = alloc["stock"] + alloc["option"]
        freeze_reason: Optional[str] = None
        try:
            freeze_reason = _should_freeze()
        except Exception:
            freeze_reason = None
        # Adopted-positions count — surfaces the "external positions are
        # being managed by the bot only if you've promoted them" lifecycle.
        adopted_n = db.query(AutoTrade).filter(AutoTrade.status == "adopted").count()
        # PDT exposure — informational on paper, gating-relevant on live.
        try:
            pdt = _pdt_count(window_business_days=5)
        except Exception:
            pdt = {"count": 0, "would_block_under_pdt": False}
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
                AutoTrade.status.in_(["pending", "open", "adopted"])
            ).count(),
            "adopted_count": adopted_n,
            "freeze_reason": freeze_reason,
            "bp_breaker_active": bool(_bp_active()),
            "bp_breaker_until": (_bp_until().isoformat() + "Z") if _bp_until() else None,
            "broker_down": bool(_broker_down()),
            "broker_down_until": (_broker_until().isoformat() + "Z") if _broker_until() else None,
            "pdt_count": int(pdt.get("count", 0)),
            "pdt_would_block": bool(pdt.get("would_block_under_pdt", False)),
            "kill_switch": bool(getattr(cfg, "killed", False)),
            "kill_reason": getattr(cfg, "killed_reason", None),
            # r53m: surface crisis_mode here too. r53 made it a hard
            # entry gate but the SafetyBanner only read this endpoint —
            # operator could see the freeze clear but not realize the
            # bot was still blocked by crisis_mode. Now the banner
            # picks it up automatically.
            "crisis_mode": _safe_crisis_mode(),
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

MIN_OPTION_SCORE = 65             # default-mode fallback (overridden by cfg.option_contract_min_score)
MIN_OPTION_SCORE_AGGRESSIVE = 55  # aggressive-mode fallback (overridden by cfg.option_contract_min_score_aggressive)


def consider_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """r57: event-driven entry consumer.

    Called by scheduled_scan when `scanner.get_active_events()` returns
    un-consumed candidate events. Routes the event's ticker through the
    existing signal_generator → consider_signal pipeline, so the event
    benefits from the full 50-factor evidence stack and gate stack
    (crisis_mode / sector cap / correlation / book-VAR / 1m-bar gate).

    r57 fix: previously this synthesized a fake signal with `entry=None,
    stop_loss=None, target1=None` and routed it through consider_signal —
    which silently rejected every event because Pydantic-validated levels
    are required by gates that compute `(entry - stop_loss) / atr`. The
    entire event-driven path was non-functional. Now we delegate to
    signal_generator (which builds proper levels from indicators) and
    only call consider_signal when a real BUY signal materializes.

    Mark-consumed semantics: every path calls scanner.mark_consumed so
    the same event isn't re-processed on the next 2-min tick.
    """
    from services import scanner as _sc
    eid = event.get("id")
    ticker = event.get("ticker")
    kind = event.get("kind")
    if not (eid and ticker and kind):
        return None
    try:
        # Delegate to the standard per-ticker analysis path. _run_analysis_for_ticker
        # generates real signals across all configured timeframes and routes
        # any BUY signal through consider_signal — same pipeline as the
        # 5min scheduled_scan, so the event-driven path benefits from the
        # full signal_generator evidence stack and consider_signal gates.
        # The event simply means "analyze this ticker NOW, don't wait for cron".
        from routers.analysis import _run_analysis_for_ticker
        from database import SessionLocal as _SL
        db = _SL()
        try:
            _run_analysis_for_ticker(ticker, db)
        finally:
            db.close()
        # _run_analysis_for_ticker doesn't return whether an entry fired;
        # we capture the most recent decision via the thread-local skip-reason.
        try:
            reason = getattr(_DECISION_TLS, "last_skip_reason", None)
        except Exception:
            reason = None
        try:
            entered = bool(getattr(_DECISION_TLS, "entered", False))
        except Exception:
            entered = False
        decision = "entered" if entered else "skipped"
        _sc.mark_consumed(eid, decision, reason=reason)
        return {"event_id": eid, "decision": decision, "reason": reason} if entered else None
    except Exception as e:
        logger.warning(f"consider_event({ticker}, {kind}) failed: {e}")
        try:
            _sc.mark_consumed(eid, "error", reason=str(e)[:200])
        except Exception:
            pass
        return None


def consider_signal(signal: Dict[str, Any], signal_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Called after each analysis run. If the signal is strong enough and budget
    allows, submits a bracket order. Returns the AutoTrade row dict if opened,
    None otherwise.

    NOTE: only called for stock-direction BUY signals — the put-play hunt is
    invoked separately at the end of every per-ticker analysis loop via
    `consider_put_play(ticker)`.

    Structure (logical sections marked inline as section dividers):
      § PRE-FLIGHT      — circuit breakers, kill flag, freeze regime, signal validation
      § ENTRY GATES     — ~25 rule-based reject conditions (geometry, liquidity, regime, etc.)
      § AI ENTRY VETO   — Claude review, off/shadow/active mode-gated
      § BUDGET + SIZING — multiplier stack, heat-aware throttle, qty calculation
      § ORDER SUBMIT    — bracket submission, AutoTrade row insert, broadcast events

    A future revision will extract these sections into named helpers
    (BACKLOG → "Decompose consider_signal into Gates/Sizing/Submission").
    Doing it inline now is too risky given the recent r40 audit; the
    section dividers provide structural guidance until the extraction
    can be done with regression-test coverage of each block.

    Thread-safety: holds the process-wide `_entry_lock` for the duration of
    the budget/cap/idempotency checks. Two concurrent calls (same or different
    tickers) serialize so a 3rd same-sector trade can't slip past the cap.
    """
    # ════════════════════════════════════════════════════════════════════
    # § PRE-FLIGHT — circuit breakers, kill flag, freeze, signal validation
    # ════════════════════════════════════════════════════════════════════
    # r53l: capture per-thread skip-reason for the candidate pool. Begin
    # decision tracking AFTER signal validation so a malformed signal
    # doesn't poison the pool row.
    try:
        from models import SignalPayload
        SignalPayload.model_validate(signal)
    except Exception as _e:
        logger.warning(
            f"consider_signal: malformed signal rejected "
            f"(ticker={signal.get('ticker')}, err={_e})"
        )
        metrics.inc("autotrade_skip", reason="malformed_signal")
        return None
    _begin_decision(signal.get("ticker") or "", "stock", signal=signal)
    # Short-circuit if the buying-power breaker tripped recently.
    if bp_breaker_active():
        metrics.inc("autotrade_skip", reason="bp_breaker")
        return None
    # Broker-down (Alpaca 5xx) breaker
    if broker_down():
        metrics.inc("autotrade_skip", reason="broker_down")
        return None
    # r46 fix #0.3: detect Alpaca-side account block. trading_blocked / blocked
    # / account_blocked fields are surfaced in get_account but never read.
    # If Alpaca disables your account (suspected wash-trading, AML flag,
    # regulatory), the bot would just keep submitting rejected orders.
    try:
        _acct_blk = paper_trader.get_account()
        if _acct_blk and (
            _acct_blk.get("trading_blocked") or _acct_blk.get("account_blocked")
            or _acct_blk.get("transfers_blocked")
        ):
            logger.critical(f"Alpaca account flagged: trading_blocked={_acct_blk.get('trading_blocked')}, "
                            f"account_blocked={_acct_blk.get('account_blocked')}, "
                            f"transfers_blocked={_acct_blk.get('transfers_blocked')}")
            try:
                _raise_alert("critical", "account_blocked",
                             f"Alpaca reports the account is BLOCKED — refuse new entries until cleared",
                             ticker=signal.get("ticker", ""))
            except Exception:
                pass
            _trip_broker_breaker(minutes=60)
            metrics.inc("autotrade_skip", reason="account_blocked")
            return None
    except Exception as _ae:
        logger.debug(f"account_blocked check failed: {_ae}")

    # r48 BACKLOG #lifecycle-P1-13: PDT lockout pre-flight.
    try:
        from services.risk_manager import is_pdt_locked as _ipl
        if _ipl():
            logger.info("AutoTrader skip: PDT lockout active (24h)")
            metrics.inc("autotrade_skip", reason="pdt_lockout")
            return None
    except Exception:
        pass
    # r48 BACKLOG #failure-mode-P1-7: DB-down breaker pre-flight.
    try:
        from services.risk_manager import is_db_down as _idd
        if _idd():
            logger.info("AutoTrader skip: DB-down breaker active")
            metrics.inc("autotrade_skip", reason="db_down")
            return None
    except Exception:
        pass

    # Bind canonical ticker upfront for pre-lock gates and confirm 1m bar.
    ticker = (signal.get("ticker") or "").strip().upper()
    if not ticker:
        return None

    # Basic filters before heavy I/O or locking.
    sig_type = signal.get("signal_type")
    if sig_type != "BUY":
        _gate_record("non_buy_signal", "fail",
                     signal_type=sig_type,
                     formula=f"signal_type='{sig_type}' ≠ 'BUY' → reject")
        metrics.inc("autotrade_skip", reason="non_buy_signal")
        return None
    _gate_record("non_buy_signal", "pass",
                 signal_type=sig_type, formula="signal_type='BUY' → continue")

    # Profit-audit #6: 1-min bar entry confirmation.
    _bar_ok = _confirm_1m_bar(ticker, direction="BUY")
    # Capture mode for the audit so operator sees if it's strict/relaxed/off.
    try:
        _gate_db = SessionLocal()
        _gate_cfg = _gate_db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        _gate_mode = (getattr(_gate_cfg, "entry_1m_gate_mode", "relaxed") or "relaxed") if _gate_cfg else "relaxed"
        _gate_db.close()
    except Exception:
        _gate_mode = "?"
    if not _bar_ok:
        _gate_record("one_min_bar_disagrees", "fail",
                     mode=_gate_mode,
                     formula=f"mode={_gate_mode} — last 1m bars rejected the BUY direction (close < open or majority disagree)")
        metrics.inc("autotrade_event", event="one_min_disagree")
        metrics.inc("autotrade_skip", reason="one_min_bar_disagrees")
        return None
    _gate_record("one_min_bar_disagrees", "pass",
                 mode=_gate_mode,
                 formula=f"mode={_gate_mode} — last closed 1m bars agreed with BUY direction (close ≥ open)")

    # r67 fix: cheap confidence-threshold peek BEFORE the AI prefetch. The
    # AI veto round-trip is ~$0.005 and 1-2s; running it on signals that
    # will be rejected for low confidence is pure waste. Audit identified
    # this as ~$9k/yr on rejected signals at current volume.
    _conf_pre = signal.get("confidence")
    try:
        _conf_pre_f = float(_conf_pre) if _conf_pre is not None else 0.0
    except (TypeError, ValueError):
        _conf_pre_f = 0.0
    _pre_thresh = 55.0  # safe floor — actual cfg threshold is checked again post-lock
    try:
        _pre_db = SessionLocal()
        try:
            _pre_cfg = _pre_db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            if _pre_cfg and getattr(_pre_cfg, "confidence_threshold", None) is not None:
                _pre_thresh = float(_pre_cfg.confidence_threshold)
        finally:
            _pre_db.close()
    except Exception:
        pass
    if _conf_pre_f < _pre_thresh:
        # Will be rejected by post-lock confidence gate anyway. Skip AI
        # prefetch to save cost. Defer the gate_record/skip_inc to the
        # canonical block inside the lock (otherwise audit shows two records).
        _ai_veto_prefetched: Optional[Dict[str, Any]] = None
    else:
        # r42 fix #1.5: prefetch the AI veto verdict OUTSIDE the entry lock so a
        # 1-2s Claude round-trip doesn't stall every other ticker's entry path.
        _ai_veto_prefetched: Optional[Dict[str, Any]] = None
        try:
            from services import ai_judge as _aij_pf
            if _aij_pf.entry_veto_mode() != "off":
                _pf_db = SessionLocal()
                try:
                    _ai_ctx_pf = _build_ai_context(ticker, _pf_db)
                finally:
                    _pf_db.close()
                _signal_view_pf = {
                    "ticker": ticker,
                    "signal_type": signal.get("signal_type"),
                    "confidence": signal.get("confidence"),
                    "timeframe": signal.get("timeframe"),
                    "entry": signal.get("entry"),
                    "stop_loss": signal.get("stop_loss"),
                    "target1": signal.get("target1"),
                    "strategy": signal.get("strategy"),
                    "reasoning": (signal.get("reasoning") or "")[:1500],
                }
                try:
                    _ai_veto_prefetched = _aij_pf.entry_veto(_signal_view_pf, _ai_ctx_pf)
                except Exception as _pf_e:
                    logger.debug(f"ai_judge prefetch failed (will retry inside lock): {_pf_e}")
                    _ai_veto_prefetched = None
        except Exception:
            _ai_veto_prefetched = None

    if not _entry_lock.acquire(timeout=30.0):
        logger.warning(f"consider_signal({ticker}): entry lock busy >30s, skipping")
        metrics.inc("autotrade_event", event="entry_lock_timeout")
        metrics.inc("autotrade_skip", reason="entry_lock_timeout")
        return None

    # r53 fix (Tier-1 #6): acquire cross-instance Postgres advisory lock
    # AFTER the per-instance threading lock. The threading lock protects
    # against same-instance races; the advisory lock against cross-
    # instance races (Cloud Run runs up to 3 api instances). On SQLite
    # this is a no-op.
    _adv_lock_ctx = _pg_advisory_entry_lock(ticker)
    _adv_acquired = _adv_lock_ctx.__enter__()
    if not _adv_acquired:
        # Another instance is already evaluating this ticker — release
        # the threading lock and skip cleanly.
        try:
            _adv_lock_ctx.__exit__(None, None, None)
        except Exception:
            pass
        _entry_lock.release()
        return None

    db = SessionLocal()
    try:
        # `ticker` already bound pre-lock above (r41 review fix A —
        # _confirm_1m_bar moved before lock acquisition to prevent slow
        # data-API calls from stalling parallel scanner threads).

        # r68-A (r70 fix): equity-snapshot freshness watchdog. Triggers ONLY
        # when the latest snapshot is from THIS session and stale relative to
        # the recon cron's 5-min cadence — i.e., the cron is wedged mid-day.
        # Earlier r68-A naively compared against any latest row, which meant
        # every morning session blocked all entries until the first post-9:30
        # snapshot landed (yesterday's close = ~17h "stale"). The right
        # condition is: today's first snapshot exists AND its age > threshold.
        try:
            from datetime import datetime as _dt_w, timezone as _tz_w, timedelta as _td_w
            from services.paper_trader import is_market_open as _imo_w
            from zoneinfo import ZoneInfo as _ZI_w
            from database import EquitySnapshot as _ES_w
            _wd_cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            _snap_max_age_min = float(getattr(_wd_cfg, "equity_snapshot_max_age_min", 15) or 15) if _wd_cfg else 15.0
            if _imo_w():
                # Anchor "session start" to today 9:30 ET. Snapshots written
                # AFTER this point count toward the watchdog; earlier rows are
                # carry-over from prior sessions and ignored.
                _now_et = _dt_w.now(_ZI_w("America/New_York"))
                _session_start_et = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                _session_start_utc = _session_start_et.astimezone(_tz_w.utc).replace(tzinfo=None)
                _last_today = (
                    db.query(_ES_w)
                    .filter(_ES_w.ts >= _session_start_utc)
                    .order_by(_ES_w.ts.desc())
                    .first()
                )
                # Grace window: don't fire the watchdog in the first
                # 2×cadence (=10min) after market open — gives the recon cron
                # time to land its first row of the session before we declare
                # it wedged.
                _mins_since_open = (_now_et - _session_start_et).total_seconds() / 60.0
                if _last_today is None and _mins_since_open > 10.0:
                    _gate_record("stale_equity_snapshot", "fail",
                                 last_today=None,
                                 minutes_since_open=round(_mins_since_open, 1),
                                 grace_min=10.0,
                                 formula=f"no EquitySnapshot since today's open and {_mins_since_open:.1f}m elapsed (>10m grace) → recon cron wedged → reject")
                    logger.error(
                        f"AutoTrader skip {ticker}: no equity snapshot since "
                        f"session open and {_mins_since_open:.0f}m elapsed — "
                        f"recon cron wedged; fail-closed"
                    )
                    metrics.inc("autotrade_skip", reason="stale_equity_snapshot")
                    return None
                if _last_today is not None:
                    _ts = _last_today.ts
                    if _ts.tzinfo is None:
                        _ts = _ts.replace(tzinfo=_tz_w.utc)
                    _age_min = (_dt_w.now(_tz_w.utc) - _ts).total_seconds() / 60.0
                    if _age_min > _snap_max_age_min:
                        _gate_record("stale_equity_snapshot", "fail",
                                     last_snapshot_age_min=round(_age_min, 1),
                                     max_age_min=_snap_max_age_min,
                                     formula=f"latest in-session snapshot age {_age_min:.1f}m > {_snap_max_age_min:.0f}m → recon cron stalled mid-session → reject")
                        logger.error(
                            f"AutoTrader skip {ticker}: equity-snapshot wedged "
                            f"mid-session (age {_age_min:.1f}m > {_snap_max_age_min:.0f}m); fail-closed"
                        )
                        metrics.inc("autotrade_skip", reason="stale_equity_snapshot")
                        return None
        except Exception as _wd_e:
            logger.debug(f"snapshot-freshness watchdog skipped: {_wd_e}")

        # r39 audit fix #8: hard freeze on losing streak (WR < 35%, n ≥ 5).
        # Different from `adaptive_risk_multiplier` shrinking — this is the
        # full stop. Operator must intervene (kill the streak's contributing
        # trades from the lookback or wait for them to age past 30d).
        from services.risk_manager import should_freeze_trading as _freeze
        _freeze_reason = _freeze()
        if _freeze_reason:
            logger.warning(f"AutoTrader skip {ticker}: trading frozen — {_freeze_reason}")
            metrics.inc("autotrade_skip", reason="trading_frozen")
            return None
        # r53 fix (Tier-2 #13): in_crisis_mode is now a HARD ENTRY GATE.
        # Previously it only tightened chandelier and trim fractions but
        # didn't block new entries. With account at 19% multi-day DD the
        # bot was still entering new trades at full sizing.
        # Triggers: account DD ≥5%, session DD ≥4%, or VIX>30 + SPY 5d <-5%.
        try:
            from services.risk_manager import in_crisis_mode as _in_crisis
            if _in_crisis():
                logger.warning(f"AutoTrader skip {ticker}: in_crisis_mode — entries halted")
                metrics.inc("autotrade_skip", reason="crisis_mode")
                return None
        except Exception as _ce:
            logger.debug(f"in_crisis_mode check failed: {_ce}")
        cfg = get_config(db)
        if not cfg.enabled:
            metrics.inc("autotrade_skip", reason="disabled")
            return None
        # C1/G5: persistent kill flag — never re-arm silently on restart.
        if getattr(cfg, "killed", False):
            metrics.inc("autotrade_skip", reason="killed")
            return None
        # r41 review fix B: PDT day-trade hard gate. Only fires when
        # cfg.pdt_enforce is True (default False so paper account is
        # unaffected). On live margin < $25k, this prevents the 4th
        # day-trade in 5 business days from firing — which would
        # otherwise trigger a 90-day PDT lock. Threshold of 3 (not 4)
        # gives us a one-trade safety margin.
        if getattr(cfg, "pdt_enforce", False):
            try:
                from services.risk_manager import pdt_day_trade_count as _pdt
                _pdt_data = _pdt(window_business_days=5)
                if _pdt_data.get("count", 0) >= 3:
                    logger.warning(
                        f"AutoTrader skip {ticker}: PDT gate — "
                        f"{_pdt_data['count']} day-trades in last 5 business days "
                        f"(threshold 3, ≥ 4 would trigger PDT lock)"
                    )
                    metrics.inc("autotrade_skip", reason="pdt_limit")
                    return None
            except Exception as _pe:
                logger.warning(f"PDT gate check failed (falling open): {_pe}")
        if not paper_trader.is_enabled():
            metrics.inc("autotrade_skip", reason="broker_not_enabled")
            return None
        if signal.get("signal_type") != "BUY":
            metrics.inc("autotrade_skip", reason="non_buy_signal")
            return None  # long-only stock entries; puts are handled separately
        # r67 fix: NaN confidence bypassed the gate (NaN < anything → False)
        # and then propagated through the sizing stack to int(NaN) → ValueError
        # → silent reject. Reject explicitly with a named reason.
        import math as _math_nan_check
        try:
            confidence = float(signal.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = float("nan")
        if _math_nan_check.isnan(confidence) or _math_nan_check.isinf(confidence):
            _gate_record("malformed_signal", "fail",
                         field="confidence", value=str(signal.get("confidence")),
                         formula="confidence is NaN/Inf → reject (signal generator produced invalid number)")
            logger.warning(
                f"AutoTrader skip {signal.get('ticker')}: confidence is NaN/Inf "
                f"(raw={signal.get('confidence')!r}) — malformed signal"
            )
            metrics.inc("autotrade_skip", reason="malformed_signal")
            return None
        # r46 Tier 1: per-ticker confidence-threshold override.
        try:
            from services.ticker_profile import confidence_threshold as _tp_conf
            _eff_conf_thresh = _tp_conf(signal.get("ticker", ""), cfg.confidence_threshold)
        except Exception:
            _eff_conf_thresh = cfg.confidence_threshold
        if confidence < _eff_conf_thresh:
            _gate_record("below_confidence_threshold", "fail",
                         confidence=confidence, threshold=_eff_conf_thresh,
                         gap=round(confidence - _eff_conf_thresh, 1),
                         formula=f"signal confidence {confidence:.0f} < threshold {_eff_conf_thresh:.0f} → reject (gap of {confidence - _eff_conf_thresh:.0f})")
            metrics.inc("autotrade_skip", reason="below_confidence_threshold")
            return None
        _gate_record("below_confidence_threshold", "pass",
                     confidence=confidence, threshold=_eff_conf_thresh,
                     headroom=round(confidence - _eff_conf_thresh, 1),
                     formula=f"signal confidence {confidence:.0f} ≥ threshold {_eff_conf_thresh:.0f} → continue (headroom {confidence - _eff_conf_thresh:.0f})")

        # ════════════════════════════════════════════════════════════════════
        # § ENTRY GATES — rule-based reject conditions (~25 gates)
        # ════════════════════════════════════════════════════════════════════

        # C1: Daily loss limit — halt new entries once realized PnL today is
        # worse than -(daily_loss_limit_pct * equity). Existing trades keep
        # trailing; this only blocks NEW exposure.
        dll_static = float(getattr(cfg, "daily_loss_limit_pct", 0) or 0)
        if dll_static > 0:
            # r44 fix #0.11: dynamic daily-loss limit (max(static, 3×avg-daily-PnL)).
            from services.risk_manager import dynamic_daily_loss_limit_pct as _dll_dyn
            dll = _dll_dyn(static_pct=dll_static)
            _acct_probe = paper_trader.get_account()
            _equity_probe = float(_acct_probe["equity"]) if _acct_probe else 0.0
            _rpnl = realized_pnl_today()
            _unr = 0.0
            try:
                _open_pos = paper_trader.get_positions() or []
                _unr = sum(float(p.get("unrealized_pl") or 0.0) for p in _open_pos)
            except Exception:
                _unr = 0.0
            _combined = _rpnl + _unr
            _halt_threshold = -abs(dll) * _equity_probe
            if _equity_probe > 0 and _combined <= _halt_threshold:
                _gate_record("daily_loss_halt", "fail",
                             realized_today=round(_rpnl, 2), unrealized=round(_unr, 2),
                             combined=round(_combined, 2),
                             equity=round(_equity_probe, 2),
                             limit_pct=round(dll * 100, 2),
                             halt_threshold=round(_halt_threshold, 2),
                             formula=f"realized ${_rpnl:.0f} + unrealized ${_unr:.0f} = combined ${_combined:.0f} ≤ halt ${_halt_threshold:.0f} (-{dll*100:.1f}% × equity ${_equity_probe:.0f}) → reject")
                logger.warning(
                    f"AutoTrader skip {signal.get('ticker')}: daily-loss limit hit "
                    f"(combined {_combined:.2f} = realized {_rpnl:.2f} + unrealized {_unr:.2f} "
                    f"≤ -{dll*100:.2f}% × equity {_equity_probe:.0f})"
                )
                metrics.inc("autotrade_event", event="daily_loss_halt")
                metrics.inc("autotrade_skip", reason="daily_loss_halt")
                return None
            else:
                _gate_record("daily_loss_halt", "pass",
                             realized_today=round(_rpnl, 2), unrealized=round(_unr, 2),
                             combined=round(_combined, 2),
                             equity=round(_equity_probe, 2),
                             limit_pct=round(dll * 100, 2),
                             halt_threshold=round(_halt_threshold, 2),
                             headroom=round(_combined - _halt_threshold, 2),
                             formula=f"combined ${_combined:.0f} > halt ${_halt_threshold:.0f} (-{dll*100:.1f}% × equity) → continue (headroom ${_combined - _halt_threshold:.0f})")
        # r67 fix: auto-deleverage hoisted OUT of `if dll_static > 0` block.
        # Previously zeroing daily_loss_limit_pct silently disabled the 6%
        # kill switch — operator intent was "disable static cap, keep dynamic
        # protections", but the nesting killed both.
        try:
            from services.risk_manager import session_equity_drawdown_pct as _sed
            sed = _sed()
            if sed is not None:
                if sed >= 0.06:
                    _gate_record("auto_deleverage", "fail",
                                 session_dd_pct=round(sed * 100, 2), threshold_pct=6.0,
                                 formula=f"session DD {sed*100:.2f}% ≥ 6% → KILL SWITCH (flatten + cancel)")
                    logger.critical(
                        f"AUTO-DELEVERAGE: session drawdown {sed*100:.2f}% ≥ 6% — engaging kill switch"
                    )
                    try:
                        kill(reason=f"auto-deleverage session_dd={sed*100:.2f}%", flatten=True, cancel_orders=True)
                    except Exception:
                        pass
                    metrics.inc("autotrade_skip", reason="auto_deleverage")
                    return None
                if sed >= 0.04:
                    _gate_record("session_dd_4pct", "fail",
                                 session_dd_pct=round(sed * 100, 2), threshold_pct=4.0,
                                 formula=f"session DD {sed*100:.2f}% ≥ 4% → block new entries (existing trail)")
                    logger.warning(
                        f"AUTO-DELEVERAGE: session drawdown {sed*100:.2f}% ≥ 4% — blocking new entries"
                    )
                    metrics.inc("autotrade_skip", reason="session_dd_4pct")
                    return None
                _gate_record("session_dd_4pct", "pass",
                             session_dd_pct=round(sed * 100, 2), threshold_pct=4.0,
                             formula=f"session DD {sed*100:.2f}% < 4% → continue")
        except Exception as _de:
            logger.debug(f"session-DD check skipped: {_de}")

        # C1: Max concurrent positions guard.
        mcp = int(getattr(cfg, "max_concurrent_positions", 0) or 0)
        # C1: Max concurrent positions guard. Tightened in adverse regimes
        # (VIX > 25 or SPY below 200-EMA → cap // 3; VIX > 20 → cap × 2/3).
        from services.risk_manager import regime_concurrent_cap as _regime_cap
        mcp_base = int(getattr(cfg, "max_concurrent_positions", 0) or 0)
        mcp = _regime_cap(mcp_base) if mcp_base > 0 else 0
        _open_count = count_open_auto_trades()
        if mcp > 0 and _open_count >= mcp:
            _gate_record("max_concurrent_cap", "fail",
                         open_count=_open_count, cap=mcp, base_cap=mcp_base,
                         formula=f"open trades {_open_count} ≥ effective cap {mcp} (base {mcp_base}, regime-tightened) → reject")
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: max_concurrent {mcp} reached "
                f"(base {mcp_base}, regime-tightened)" if mcp != mcp_base else
                f"AutoTrader skip {signal.get('ticker')}: max_concurrent_positions {mcp} reached"
            )
            metrics.inc("autotrade_skip", reason="max_concurrent_cap")
            return None
        if mcp > 0:
            _gate_record("max_concurrent_cap", "pass",
                         open_count=_open_count, cap=mcp, base_cap=mcp_base,
                         headroom=mcp - _open_count,
                         formula=f"open trades {_open_count} < cap {mcp} → continue (headroom {mcp - _open_count})")

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
            # Include adopted — adopted positions count toward portfolio heat.
            open_trades_heat = db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending", "open", "adopted"])
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
                    # r47 fix #T0f-1: prior `max(0, oe - os_)` returned 0 for
                    # SHORTS (stop sits ABOVE entry on shorts) — adopted shorts
                    # passed the 10% portfolio-heat cap with $0 contribution.
                    # abs() makes heat direction-agnostic.
                    raw = abs(float(oe) - float(os_)) * (ot.qty or 0)
                elif ot.asset_type == "option" and oe > 0:
                    # For long options, max-loss = premium paid (contract × 100).
                    raw = float(oe) * 100 * (ot.qty or 0)
                current_heat += raw * beta_weight(ot.ticker)
            # r39 audit cleanup: removed dead `prospective_stop` /
            # `prospective_entry` computations — they were computed but
            # never used in the cap math (the comment said "conservative
            # default — pure existing-heat check"). The heat-aware risk
            # multiplier (r35) now handles the prospective contribution
            # softly via sizing throttle as we approach the cap, which
            # makes more sense than pure binary include/exclude.
            heat_cap = _heat_equity * _PORTFOLIO_HEAT_CAP_PCT
            if current_heat >= heat_cap:
                logger.info(
                    f"AutoTrader skip {signal.get('ticker')}: portfolio heat "
                    f"${current_heat:.0f} (beta-weighted) ≥ cap ${heat_cap:.0f} "
                    f"({_PORTFOLIO_HEAT_CAP_PCT*100:.0f}% × equity {_heat_equity:.0f})"
                )
                metrics.inc("autotrade_event", event="portfolio_heat_cap")
                return None

        # Opening / closing window filters for intraday TFs (r43 fix #0.3, #0.21).
        sig_tf_str = (signal.get("timeframe") or "").strip()
        if sig_tf_str in _OPENING_FILTER_TFS and _in_opening_filter_window():
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: opening-15m filter (TF {sig_tf_str})"
            )
            metrics.inc("autotrade_event", event="opening_filter")
            metrics.inc("autotrade_skip", reason="opening_filter")
            return None
        if sig_tf_str in _CLOSING_FILTER_TFS and _in_closing_filter_window():
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: closing-10m MOC-imbalance filter (TF {sig_tf_str})"
            )
            metrics.inc("autotrade_event", event="closing_filter")
            metrics.inc("autotrade_skip", reason="closing_filter")
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
                from datetime import datetime as _dt, timezone as _tz
                if isinstance(gen_at, str):
                    # r67 fix: previous code stripped tzinfo and compared to
                    # utcnow(); a TZ-naive ISO string from a non-UTC host
                    # over-stated age by the local-UTC offset. Normalize to
                    # tz-aware UTC; assume naive timestamps ARE UTC.
                    s = gen_at.replace("Z", "+00:00")
                    gen_dt = _dt.fromisoformat(s)
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=_tz.utc)
                else:
                    gen_dt = gen_at
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=_tz.utc)
                age_s = (_dt.now(_tz.utc) - gen_dt).total_seconds()
                if age_s > max_age_mins * 60:
                    logger.info(
                        f"AutoTrader skip {signal.get('ticker')}: signal age {age_s/60:.1f}m > {max_age_mins}m (stale)"
                    )
                    metrics.inc("autotrade_skip", reason="signal_stale")
                    return None
            except Exception as _ts_e:
                logger.debug(f"signal-freshness parse failed (fail-open): {_ts_e}")
        # Per-timeframe gate: don't auto-trade off 1mo/5m signals etc.
        allowed_tfs = {s.strip() for s in (cfg.signal_timeframes or "1h,4h,1d").split(",") if s.strip()}
        sig_tf = (signal.get("timeframe") or "").strip()
        if allowed_tfs and sig_tf not in allowed_tfs:
            _gate_record("tf_not_allowed", "fail",
                         timeframe=sig_tf, allowed=sorted(allowed_tfs),
                         formula=f"signal timeframe '{sig_tf}' not in cfg.signal_timeframes {sorted(allowed_tfs)} → reject")
            metrics.inc("autotrade_skip", reason="tf_not_allowed")
            return None
        _gate_record("tf_not_allowed", "pass",
                     timeframe=sig_tf, allowed=sorted(allowed_tfs),
                     formula=f"signal timeframe '{sig_tf}' is in allowlist → continue")
        entry = signal.get("entry")
        stop = signal.get("stop_loss")
        t1 = signal.get("target1")
        t2 = signal.get("target2")
        t3 = signal.get("target3")
        if not (entry and stop and t1):
            metrics.inc("autotrade_skip", reason="missing_levels")
            return None
        if stop >= entry:
            return None  # malformed signal
        # Post-mortem fix (MU -$227): reject BUY signals whose T1 sits at or
        # below entry — a common signal-gen flaw (pulled T1 from S1 pivot
        # below current price) that would instantly fire the T1-trail and
        # place the stop at entry _above_ current price, stopping out at
        # market. Also rejects microscopically-tight T1s (AAPL: T1 was 11¢
        # above entry; any normal retrace tripped BE and chopped us flat).
        # r46 Tier 1 parameter tune: raised 0.004 → 0.006 (0.6%). With the
        # 12bps round-trip cost buffer the prior 0.4% T1 left only 28bps net
        # — barely positive after a single re-fill. 0.6% cleanly covers 2×
        # round-trip cost.
        _MIN_T1_GAP_PCT = 0.006
        if t1 <= entry * (1.0 + _MIN_T1_GAP_PCT):
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: T1 {t1} ≤ entry {entry} × 1.004 "
                f"(geometry broken or too tight)"
            )
            metrics.inc("autotrade_event", event="bad_t1_geometry")
            metrics.inc("autotrade_skip", reason="bad_t1_geometry")
            return None
        # r43 fix #0.2: HARD reward:risk floor at consider_signal. The 1.3R
        # floor in signal_generator only applies to natural-target output;
        # promoted/adopted/external paths bypass it. Plus the floor must
        # account for round-trip costs — a 1.3R T1 on a $100 stock with 6bps
        # round-trip is closer to 1.15R net.
        _rr_min = float(getattr(cfg, "rr_min", 1.3) or 1.3)
        # Cost buffer: estimate 1 round-trip slippage on the reward leg.
        _cost_buffer = entry * (12 / 10000.0)   # 12 bps round-trip
        _net_reward = max(0.0, (t1 - entry) - _cost_buffer)
        _gross_risk = max(0.01, entry - stop)
        _rr_net = _net_reward / _gross_risk
        if _rr_net < _rr_min:
            _gate_record("bad_rr", "fail",
                         entry=round(entry, 4), stop=round(stop, 4), t1=round(t1, 4),
                         net_reward=round(_net_reward, 4), gross_risk=round(_gross_risk, 4),
                         cost_buffer=round(_cost_buffer, 4),
                         rr_net=round(_rr_net, 3), rr_min=round(_rr_min, 3),
                         formula=f"net R:R = (T1 ${t1:.2f} − entry ${entry:.2f} − cost ${_cost_buffer:.2f}) / (entry − stop ${stop:.2f}) = {_rr_net:.2f} < min {_rr_min:.2f} → reject")
            logger.info(
                f"AutoTrader skip {signal.get('ticker')}: net R:R {_rr_net:.2f} < {_rr_min:.2f} "
                f"after {_cost_buffer:.2f} cost buffer (entry={entry}, t1={t1}, stop={stop})"
            )
            metrics.inc("autotrade_event", event="bad_rr")
            metrics.inc("autotrade_skip", reason="bad_rr")
            return None
        _gate_record("bad_rr", "pass",
                     rr_net=round(_rr_net, 3), rr_min=round(_rr_min, 3),
                     entry=round(entry, 4), stop=round(stop, 4), t1=round(t1, 4),
                     formula=f"net R:R {_rr_net:.2f} ≥ min {_rr_min:.2f} → continue")
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
                    metrics.inc("autotrade_skip", reason="stop_too_tight_atr")
                    return None
        except Exception as _e:
            # r67 fix: was fail-open (continue trading on exception). Now
            # fail-closed — a yfinance hiccup can produce live trades against
            # stops we couldn't validate. Reject and surface the error.
            logger.warning(f"stop_too_tight_atr gate {ticker}: {_e} — fail-closed reject")
            metrics.inc("autotrade_event", event="stop_too_tight_atr_error")
            metrics.inc("autotrade_skip", reason="stop_too_tight_atr_error")
            return None

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
                    metrics.inc("autotrade_skip", reason="gap_open_reject")
                    return None
        except Exception as _e:
            # r67 fix: fail-closed. A live-price fetch failure at the open
            # is exactly when gap protection matters most.
            logger.warning(f"gap-open gate {ticker}: {_e} — fail-closed reject")
            metrics.inc("autotrade_event", event="gap_open_gate_error")
            metrics.inc("autotrade_skip", reason="gap_open_gate_error")
            return None

        # Liquidity gate: require ≥ $10M median daily $-volume over the last
        # 20 trading days. Sub-threshold names produce wide spreads + slippage
        # that quietly poisons R-multiples (a 1% slip on a 4R trade gives up
        # 0.4R/trade; over a year that's the entire edge). Threshold is
        # deliberately conservative — typical large-caps clear $100M+/day,
        # mid-caps $20–50M; we only reject true micro/small-caps.
        try:
            from services.data_fetcher import fetch_ohlcv as _liq_fo
            _liq_df = _liq_fo(ticker, "1d")
            if _liq_df is not None and not _liq_df.empty and len(_liq_df) >= 5:
                _tail = _liq_df.tail(20)
                _typ_px = (_tail["High"] + _tail["Low"] + _tail["Close"]) / 3.0
                _dvol = (_typ_px * _tail["Volume"]).median()
                if _dvol and _dvol > 0 and _dvol < 10_000_000:
                    logger.info(
                        f"AutoTrader skip {ticker}: median daily $-volume "
                        f"${_dvol/1e6:.1f}M < $10M (illiquid)"
                    )
                    metrics.inc("autotrade_event", event="illiquid_skip")
                    metrics.inc("autotrade_skip", reason="illiquid_skip")
                    return None
        except Exception as _e:
            # r67 fix: fail-closed. Illiquid names can produce 5-15bps
            # entry/exit slippage that quietly poisons R-multiples; we
            # cannot validate liquidity ⇒ do not enter.
            logger.warning(f"liquidity gate {ticker}: {_e} — fail-closed reject")
            metrics.inc("autotrade_event", event="liquidity_gate_error")
            metrics.inc("autotrade_skip", reason="liquidity_gate_error")
            return None

        # r39 audit fix #19: ticker-level ADX gate. SPY-ADX is checked in
        # adaptive_risk_multiplier (market regime), but the TICKER's own ADX
        # was never checked. CNTA / AMKR were trading on tickers with
        # sub-20 ADX while SPY was trending — the bot saw "trend regime"
        # via SPY but bought into ticker-level chop. Reject when the
        # ticker's ADX < 18; allows mean-reversion strategies to bypass
        # via signal["strategy"] containing "MEANREV" or "MEAN_REVERSION".
        try:
            _t_adx = signal.get("adx")
            _strat_name = (signal.get("strategy") or "").upper()
            _is_mean_rev = "MEANREV" in _strat_name or "MEAN_REVERSION" in _strat_name
            if _t_adx is not None and _t_adx < 18 and not _is_mean_rev:
                _gate_record("ticker_chop", "fail",
                             adx=round(_t_adx, 2), threshold=18,
                             strategy=_strat_name, is_mean_rev=_is_mean_rev,
                             formula=f"ADX {_t_adx:.1f} < 18 (chop regime) AND strategy '{_strat_name}' is not mean-reversion → reject (trend strategies don't work in chop)")
                logger.info(
                    f"AutoTrader skip {ticker}: ticker ADX {_t_adx:.1f} < 18 "
                    f"(chop) and strategy {_strat_name!r} is not mean-reversion"
                )
                metrics.inc("autotrade_skip", reason="ticker_chop")
                return None
            _gate_record("ticker_chop", "pass",
                         adx=round(_t_adx, 2) if _t_adx is not None else None,
                         threshold=18, strategy=_strat_name, is_mean_rev=_is_mean_rev,
                         formula=(f"ADX {_t_adx:.1f} ≥ 18 → continue" if _t_adx is not None and _t_adx >= 18
                                  else f"strategy '{_strat_name}' is mean-reversion (chop OK) → continue" if _is_mean_rev
                                  else "ADX unavailable → continue (gate not applied)"))
        except Exception as _e:
            logger.warning(f"ticker-ADX gate {ticker}: {_e}")

        # r69: setup-quality composite gate. Collapses 8 individual gates
        # (confidence_threshold + bad_rr + bad_t1_geometry + tf_not_allowed
        # + one_min_bar_disagrees + liquidity + ticker_chop + freshness) into
        # ONE calibrated 0-100 score with one threshold. Audit consensus:
        # "the highest-leverage refactor — collapses 8 gates into 1 with no
        # edge loss." Default mode is SHADOW (record only); operator flips
        # cfg.setup_quality_gate_enabled = True after empirical validation.
        try:
            from services.setup_quality import compute as _sq_compute
            # Compute median $-volume for liquidity contribution (already cheap;
            # data_fetcher caches the daily bars from the earlier liquidity gate).
            _sq_dvol: Optional[float] = None
            try:
                from services.data_fetcher import fetch_ohlcv as _sq_fo
                _sq_df = _sq_fo(ticker, "1d")
                if _sq_df is not None and not _sq_df.empty and len(_sq_df) >= 5:
                    _tail2 = _sq_df.tail(20)
                    _typ_px2 = (_tail2["High"] + _tail2["Low"] + _tail2["Close"]) / 3.0
                    _sq_dvol = float((_typ_px2 * _tail2["Volume"]).median() or 0.0)
            except Exception:
                _sq_dvol = None
            # Signal age in minutes (re-uses the parsed gen_at from the
            # freshness gate). Falls back to None if missing.
            _sq_age_min: Optional[float] = None
            try:
                _sq_gen_at = signal.get("generated_at")
                if _sq_gen_at:
                    from datetime import datetime as _dt_sq, timezone as _tz_sq
                    if isinstance(_sq_gen_at, str):
                        _s = _sq_gen_at.replace("Z", "+00:00")
                        _sq_gen_dt = _dt_sq.fromisoformat(_s)
                        if _sq_gen_dt.tzinfo is None:
                            _sq_gen_dt = _sq_gen_dt.replace(tzinfo=_tz_sq.utc)
                    else:
                        _sq_gen_dt = _sq_gen_at
                        if _sq_gen_dt.tzinfo is None:
                            _sq_gen_dt = _sq_gen_dt.replace(tzinfo=_tz_sq.utc)
                    _sq_age_min = (_dt_sq.now(_tz_sq.utc) - _sq_gen_dt).total_seconds() / 60.0
            except Exception:
                _sq_age_min = None
            _sq_max_age = float(max(10, min(90, 1 * tf_mins)))
            _sq_result = _sq_compute(
                confidence=confidence,
                confidence_threshold=_eff_conf_thresh,
                entry=entry, stop=stop, target1=t1,
                rr_min=_rr_min,
                adx=signal.get("adx"),
                strategy=signal.get("strategy"),
                one_min_bar_agrees=_bar_ok,
                median_dvol=_sq_dvol,
                signal_age_min=_sq_age_min,
                signal_max_age_min=_sq_max_age,
            )
            _sq_score = float(_sq_result["score"])
            _sq_min = float(getattr(cfg, "setup_quality_min", 55.0) or 55.0)
            _sq_active = bool(getattr(cfg, "setup_quality_gate_enabled", False))
            if _sq_active and _sq_score < _sq_min:
                _gate_record("setup_quality_score", "fail",
                             score=_sq_score, threshold=_sq_min,
                             parts=_sq_result["parts"],
                             contributions=_sq_result["contributions"],
                             formula=f"composite score {_sq_score:.1f} < min {_sq_min:.1f} → reject  ::  {_sq_result['details']}")
                logger.info(
                    f"AutoTrader skip {ticker}: setup_quality_score "
                    f"{_sq_score:.1f} < {_sq_min:.1f} [active mode]"
                )
                metrics.inc("autotrade_skip", reason="setup_quality_score")
                return None
            # Always record (shadow or pass-active) so operator can compare
            # composite distributions vs individual-gate verdicts.
            _gate_record("setup_quality_score", "pass" if _sq_score >= _sq_min else "shadow_below",
                         score=_sq_score, threshold=_sq_min,
                         active=_sq_active,
                         parts=_sq_result["parts"],
                         contributions=_sq_result["contributions"],
                         pass_individual=_sq_result["pass_individual_gates"],
                         formula=f"composite {_sq_score:.1f} {'≥' if _sq_score >= _sq_min else '<'} min {_sq_min:.1f} | mode={'active' if _sq_active else 'shadow'}  ::  {_sq_result['details']}")
        except Exception as _sq_e:
            logger.debug(f"setup_quality compute failed: {_sq_e}")

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
        except Exception as _e:
            # r43 fix #0.10: FAIL CLOSED on earnings-gate exception. A
            # yfinance hiccup at exactly the wrong moment was previously
            # waving the bot through into an earnings print. We now skip
            # the entry on any earnings-lookup failure — better to miss a
            # trade than to trade blind through a binary event.
            logger.warning(f"earnings gate {ticker} FAIL-CLOSED: {_e}")
            metrics.inc("autotrade_event", event="earnings_gate_error")
            metrics.inc("autotrade_skip", reason="earnings_gate_error")
            return None
        # Trailing-only exit: park the bracket TP far away so it never fires.
        # Real exit comes from the trailing stop in manage_open_positions().
        # r44 fix #1.3: slippage-aware risk_per_share — sizing assumes
        # theoretical stop fills, but real stops slip 0.10-0.40 ATR. Sized
        # 1% risk is actually 1.4-1.8% on 5-10% of trades.
        from services.risk_manager import slippage_aware_risk_per_share as _slip_rps
        _atr_for_slip = float(signal.get("atr") or 0.0)
        risk_per_share = _slip_rps(entry, stop, _atr_for_slip)
        far_tp = round(entry + 10 * risk_per_share, 2)

        # `ticker` already bound at function top (r39 fix). Global ticker
        # blacklist — applies regardless of watchlist/universe source.
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
                metrics.inc("autotrade_skip", reason="macro_blackout")
                return None
        except Exception as _e:
            # r43 fix #0.10: FAIL CLOSED on macro-blackout exception. A
            # network blip on CPI/NFP/FOMC release morning previously waved
            # the bot through into the print.
            logger.warning(f"macro blackout gate {ticker} FAIL-CLOSED: {_e}")
            metrics.inc("autotrade_event", event="macro_blackout_gate_error")
            metrics.inc("autotrade_skip", reason="macro_blackout_gate_error")
            return None

        # Per-ticker auto-trade gate
        ws = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first()
        if ws and getattr(ws, "auto_trade_enabled", True) is False:
            return None

        # r53 Tier-3 C: regime-conditional strategy switching. In CHOP,
        # turn off momentum/breakout strategies entirely; in TREND, turn
        # off mean-reversion. Fail-open when regime classification is
        # unavailable.
        try:
            from services.regime_router import (
                classify_regime as _cr,
                is_strategy_allowed_in_regime as _is_allowed,
            )
            _strat_for_regime = signal.get("strategy")
            _current_regime = _cr()
            if _strat_for_regime and _current_regime and not _is_allowed(_strat_for_regime, _current_regime):
                logger.info(
                    f"AutoTrader skip {ticker}: strategy '{_strat_for_regime}' "
                    f"not allowed in regime {_current_regime}"
                )
                metrics.inc("autotrade_skip", reason="strategy_off_regime")
                return None
        except Exception as _rr_e:
            logger.debug(f"regime_router check failed (fail-open): {_rr_e}")

        # r67: source_mute_enabled and loss_pattern_veto deleted.
        # source_mute was disabled-by-default since r53 ("pending strategy
        # backfill") — never shipped. loss_pattern_veto was permanent shadow
        # with no eval pipeline. Per r57's 14-day shadow rule and the audit
        # consensus, dormant gates are deletion candidates not maintenance
        # liabilities. The strategy_multiplier still dampens sizing for poor
        # strategies; the calibration_gate still hard-rejects on per-bucket
        # Wilson-LB.

        # One open auto-trade per ticker. Include `adopted` so the bot
        # doesn't enter a new trade on top of an externally-held position
        # in the same name.
        existing = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.status.in_(["pending", "open", "adopted"]),
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

        # Soft correlation cap: don't pile into the same sector.
        # r42 fix #2.5: when sector lookup fails OR returns empty, treat the
        # ticker as "unknown" and apply the cap against ALL "unknown"
        # tickers — previously an unmapped ticker silently bypassed the
        # cap entirely. New names that fall in the gap (the most likely to
        # be over-concentrated) were exactly the ones evading the throttle.
        try:
            from services.data_fetcher import get_ticker_info
            sector = (get_ticker_info(ticker).get("sector") or "").strip()
        except Exception:
            sector = ""
        if not sector:
            sector = "_unknown"
            try:
                from services.alerts import alert as _raise_unk
                _raise_unk("info", "sector_unknown",
                           f"Ticker {ticker} has no sector classification — counted under '_unknown' bucket",
                           ticker=ticker)
            except Exception:
                pass
        # r43 fix #0.11: pair-correlation gate. Sector cap can't catch
        # NVDA+AMD+AVGO (different sub-sectors, ρ≈0.85 with semis tape).
        # Reject when new ticker correlates ≥ 0.70 (30d daily returns)
        # with 2+ already-open tickers — prevents silent triple-counted
        # exposure on a single risk factor.
        try:
            _open_tickers = [
                row[0] for row in db.query(AutoTrade.ticker).filter(
                    AutoTrade.status.in_(["pending", "open", "adopted"])
                ).distinct().all()
                if row[0] and row[0] != ticker
            ]
            if len(_open_tickers) >= 2:
                _corr_with = correlated_with_open(ticker, _open_tickers, threshold=0.70)
                _corr_cap = int(getattr(cfg, "max_correlated_open", 1) or 1)
                if len(_corr_with) > _corr_cap:
                    logger.info(
                        f"AutoTrader skip {ticker}: ρ≥0.70 with {len(_corr_with)} open trades "
                        f"({_corr_with[:5]}); cap {_corr_cap}"
                    )
                    metrics.inc("autotrade_event", event="correlation_cap")
                    metrics.inc("autotrade_skip", reason="correlation_cap")
                    return None
        except Exception as _ce:
            logger.debug(f"correlation gate {ticker}: {_ce}")

        if sector and getattr(cfg, "max_per_sector", 3):
            # Include adopted — sector concentration is real regardless
            # of whether the bot is managing the position.
            same_sector_open = db.query(AutoTrade).filter(
                AutoTrade.sector == sector,
                AutoTrade.status.in_(["pending", "open", "adopted"]),
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
                        AutoTrade.status.in_(["pending", "open", "adopted"]),
                    ).all()
                    sector_heat = 0.0
                    for sr in sector_rows:
                        se = sr.entry_price or sr.requested_entry or 0.0
                        ss_ = sr.current_stop or sr.stop_loss or 0.0
                        if sr.asset_type == "stock" and se > 0 and ss_ > 0:
                            # r47 fix #T0f-1 (sector heat companion): same short-side fix
                            sector_heat += abs(float(se) - float(ss_)) * (sr.qty or 0)
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

        # ════════════════════════════════════════════════════════════════════
        # § AI ENTRY VETO — Claude semantic review (off/shadow/active gated)
        # ════════════════════════════════════════════════════════════════════
        # AI entry-veto layer (shadow by default; flip via AI_ENTRY_VETO_MODE
        # env var to "active" only after reviewing ≥200 shadow decisions).
        # Failure / abstain / off → proceed (rule-engine wins). Honored skips
        # log via metrics so the operator can graph veto rate.
        # r42 fix #1.5: use the prefetched verdict if we have one (most
        # common path); only fall back to a synchronous call if prefetch
        # failed and the operator has the gate enabled.
        try:
            from services import ai_judge as _aij
            mode = _aij.entry_veto_mode()
            _veto = _ai_veto_prefetched
            if _veto is None and mode != "off":
                _ai_ctx = _build_ai_context(ticker, db)
                _ai_signal_view = {
                    "ticker": ticker,
                    "signal_type": signal.get("signal_type"),
                    "confidence": signal.get("confidence"),
                    "timeframe": signal.get("timeframe"),
                    "entry": signal.get("entry"),
                    "stop_loss": signal.get("stop_loss"),
                    "target1": signal.get("target1"),
                    "strategy": signal.get("strategy"),
                    "reasoning": (signal.get("reasoning") or "")[:1500],
                }
                _veto = _aij.entry_veto(_ai_signal_view, _ai_ctx)
            if _veto and _veto.get("honored") and _veto.get("verdict") == "skip":
                logger.info(
                    f"AutoTrader skip {ticker}: AI veto — {_veto.get('reason', '')}"
                )
                metrics.inc("autotrade_skip", reason="ai_veto")
                return None
        except Exception as _e:
            # Hard guarantee: AI judge failure NEVER blocks a trade.
            logger.debug(f"ai_judge entry_veto wrapper failed: {_e}")

        # ════════════════════════════════════════════════════════════════════
        # § BUDGET + SIZING — multiplier stack, heat-aware throttle, qty calc
        # ════════════════════════════════════════════════════════════════════
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
        # Adaptive risk: halve the cap under high VIX (>25) or when recent
        # win-rate is below 55%. Catches regime changes the static cap misses.
        from services.risk_manager import (
            adaptive_risk_multiplier as _adapt_risk,
            vol_target_multiplier as _vol_tgt,
            account_drawdown_multiplier as _acct_dd,
            book_var_99 as _book_var,
            book_leverage_pct as _book_lev,
            earnings_cluster_count as _earn_cluster,
        )
        _adapt = _adapt_risk()
        if _adapt <= 0.0:
            logger.info(
                f"AutoTrader skip {ticker}: adaptive multiplier 0 (cumulative-adverse-regime)"
            )
            metrics.inc("autotrade_skip", reason="adaptive_zero")
            return None
        # r44 fix #1.2: graduated drawdown overlay. Returns 0 when DD ≥ 10%.
        _dd_mult = _acct_dd()
        if _dd_mult <= 0.0:
            logger.warning(f"AutoTrader skip {ticker}: account drawdown ≥ 10% — sizing 0")
            metrics.inc("autotrade_skip", reason="account_drawdown")
            return None
        # r44 fix #1.1: vol-target multiplier (annualized 12% target).
        _vt_mult = _vol_tgt(target_annual_vol=float(getattr(cfg, "vol_target_annual", 0.12) or 0.12))
        # r44 fix #1.6: aggregate earnings-cluster gate.
        _ec_count = _earn_cluster(window_hours=168)
        if _ec_count >= 4:
            logger.info(
                f"AutoTrader skip {ticker}: {_ec_count} open positions have earnings within 7 days"
            )
            metrics.inc("autotrade_skip", reason="earnings_cluster")
            return None
        # r44 fix #1.8: leverage cap (1.5× default).
        _lev = _book_lev(equity)
        _lev_cap = float(getattr(cfg, "leverage_cap", 1.5) or 1.5)
        if _lev >= _lev_cap:
            logger.warning(
                f"AutoTrader skip {ticker}: book leverage {_lev:.2f}× ≥ cap {_lev_cap:.2f}×"
            )
            metrics.inc("autotrade_skip", reason="leverage_cap")
            return None
        # r44 fix #1.7: book-VaR 99% sanity check (heat-derived approximation).
        _var99 = _book_var(equity)
        _var_cap_pct = float(getattr(cfg, "book_var_99_cap_pct", 0.05) or 0.05)
        _var_cap_dollars = _var_cap_pct * equity
        if _var99 >= _var_cap_dollars:
            _gate_record("book_var_99", "fail",
                         book_var_99=round(_var99, 2),
                         cap_pct=round(_var_cap_pct * 100, 2),
                         cap_dollars=round(_var_cap_dollars, 2),
                         equity=round(equity, 2),
                         formula=f"current book VaR99 (β-weighted heat × 2.33) = ${_var99:.0f} ≥ cap ${_var_cap_dollars:.0f} ({_var_cap_pct*100:.1f}% × equity ${equity:.0f}) → reject (book already at risk capacity; close existing positions to free VaR budget)")
            logger.warning(
                f"AutoTrader skip {ticker}: book VaR99 ${_var99:.0f} ≥ {_var_cap_pct*100:.1f}% × equity ${equity:.0f}"
            )
            metrics.inc("autotrade_skip", reason="book_var_99")
            return None
        _gate_record("book_var_99", "pass",
                     book_var_99=round(_var99, 2),
                     cap_pct=round(_var_cap_pct * 100, 2),
                     cap_dollars=round(_var_cap_dollars, 2),
                     equity=round(equity, 2),
                     headroom=round(_var_cap_dollars - _var99, 2),
                     formula=f"book VaR99 ${_var99:.0f} < cap ${_var_cap_dollars:.0f} ({_var_cap_pct*100:.1f}% × equity ${equity:.0f}) → continue (headroom ${_var_cap_dollars - _var99:.0f})")
        # r44 fix #1.4: beta-symmetric sizing — heat already counts beta-
        # weighted via current_portfolio_heat, but sizing didn't. A β=1.8
        # ticker consumed 1.0× risk-budget at entry but 1.8× heat
        # downstream. Now divide by beta_weight so sizing matches heat.
        try:
            from services.fundamentals import beta_weight as _bw
            _beta = max(0.5, min(2.5, float(_bw(ticker, default=1.0))))
        except Exception:
            _beta = 1.0
        # r44 Wave 3: cross-asset regime + calendar/seasonality multipliers.
        try:
            from services.cross_asset import regime_multiplier as _regime_x
            _regime_xa = _regime_x()
        except Exception:
            _regime_xa = 1.0
        try:
            from services.seasonality import (
                calendar_multiplier as _cal_m,
                pre_fomc_drift_buy_qualifying_ticker as _pfd_q,
                is_opex_day as _is_opex_d,
                opex_eligible as _opex_elig,
            )
            _cal_mult = _cal_m()
            # r48 BACKLOG #edge-F11: undo the 0.92× OPEX dampener for non-
            # OPEX-eligible tickers. The seasonality multiplier applies it
            # universally; we re-multiply by (1/0.92) for thin-options names.
            try:
                if _is_opex_d() and not _opex_elig(ticker):
                    _cal_mult *= (1.0 / 0.92)
            except Exception:
                pass
            # r46 Tier P: pre-FOMC drift extra boost on qualifying ETFs only.
            if _pfd_q(ticker):
                # r48 BACKLOG #edge-F9: drop the 1.12 ETF-specific boost.
                _cal_mult *= 1.0  # no-op; kept for symmetry
        except Exception:
            _cal_mult = 1.0
        try:
            from services.index_calendar import index_event_multiplier as _ie_m
            # r48 BACKLOG #edge-F10: pass ticker so boost only applies to
            # operator-flagged inclusion candidates, not all signals.
            _cal_mult *= _ie_m(ticker=ticker)
        except Exception:
            pass
        # r67 simplified: r47 sizing overlay (term/skew/vvix/vrp/spx) DELETED.
        # Audit consensus: 5-factor cross-asset sizing tilt cannot be validated
        # at retail trade count; the multiplier stack was theater. Kept ONLY
        # the credit-spread circuit-breaker as a hard veto on BUY direction
        # when HYG-LQD widens beyond panic thresholds.
        _r47_mult = 1.0
        try:
            if bool(getattr(cfg, "credit_spread_circuit_breaker_enabled", True)):
                from services.r47_overlays import credit_spread_circuit_breaker_active as _cscb
                if _cscb():
                    _direction = (signal.get("signal_type") or "BUY").upper()
                    if _direction == "BUY":
                        _gate_record("r47_credit_cb", "fail",
                                     direction=_direction,
                                     formula="HYG-LQD credit spread widened beyond panic threshold → veto BUY")
                        logger.info(
                            f"AutoTrader skip {ticker}: credit-spread circuit breaker active (BUY veto)"
                        )
                        metrics.inc("autotrade_skip", reason="r47_credit_cb")
                        return None
        except Exception as _r47_e:
            logger.debug(f"credit-spread CB check skipped: {_r47_e}")
        # r48 BACKLOG: factor-based composite (12-1 momentum, BAB, yield-curve,
        # oil regime, DXY, real-yield, FOMC, macro surprise, squeeze, opportunistic
        # insider). Clamped [0.6, 1.4]. Gated behind cfg.factor_strategies_enabled.
        try:
            from services.factors import factor_composite as _fc
            if bool(getattr(cfg, "factor_strategies_enabled", True)):
                _factor_mult, _ = _fc(
                    ticker,
                    sector=sector,
                    pe_ratio=signal.get("trailing_pe") or signal.get("pe"),
                    universe=None,
                )
            else:
                _factor_mult = 1.0
        except Exception:
            _factor_mult = 1.0
        # r57: scanner_conviction_multiplier removed. The ±15% nudge from
        # an un-validated v2 percentile rank wasn't moving the needle and
        # added bug surface. If/when v2 ranking proves out empirically,
        # restore via a separate validated multiplier.
        risk_budget = (equity * cfg.max_risk_per_trade_pct
                       * _adapt * _dd_mult * _vt_mult
                       * _regime_xa * _cal_mult * _r47_mult * _factor_mult) / _beta
        # Profit-max: scale risk budget with confidence headroom above threshold.
        # Signals that clear the gate by a wide margin deserve a bigger bet.
        conf_headroom = max(0.0, (confidence - cfg.confidence_threshold) / max(1.0, (100.0 - cfg.confidence_threshold)))
        conf_mult = 1.0 + (_MAX_CONFIDENCE_RISK_MULT - 1.0) * min(1.0, conf_headroom)
        # r43 fix #1.5: route through the proper fractional-Kelly helper in
        # risk_math (which now applies quarter-Kelly damping per r42 fix
        # #1.7). Previously this site had its own ad-hoc linear ramp that
        # ignored reward:risk entirely — i.e. it wasn't a Kelly fraction.
        try:
            bt_wr = float(signal.get("backtest_win_rate") or 0)
        except Exception:
            bt_wr = 0.0
        try:
            bt_avg_rr = float(signal.get("backtest_avg_reward_risk") or signal.get("avg_reward_risk") or 0)
        except Exception:
            bt_avg_rr = 0.0
        # r46 Tier 1: prefer REALIZED edge over backtest. Live drift between
        # backtest and live trades silently mis-sizes every entry. Use the
        # rolling 60d realized stats from `strategy_scorecard` when n ≥ 10
        # for this strategy; fall back to backtest values otherwise.
        try:
            _strat_name = signal.get("strategy")
            if _strat_name:
                _scard = strategy_scorecard(days=60, min_trades=10).get(_strat_name)
                if _scard:
                    _real_wr = float(_scard.get("win_rate") or 0)
                    _real_avg_pl = float(_scard.get("avg_pl") or 0)
                    if _real_wr > 0 and _real_avg_pl != 0:
                        bt_wr = _real_wr * 100.0   # convert to % expected by kelly_risk_mult
                        # avg_pl is $-per-trade; we don't have a direct R-multiple,
                        # so use the per-strategy Sharpe-like multiplier as a proxy.
                        # Keep bt_avg_rr from signal if positive, else infer.
                        if bt_avg_rr <= 0 and _real_avg_pl > 0:
                            bt_avg_rr = 1.5   # conservative default for a profitable strategy
        except Exception:
            pass
        from services.risk_math import kelly_risk_mult as _krm
        kelly_mult = _krm(
            historical_win_rate=bt_wr,
            avg_reward_risk=bt_avg_rr if bt_avg_rr > 0 else None,
            min_win_rate=_KELLY_MIN_WIN_RATE,
            max_mult=_KELLY_MAX_MULT,
        )
        # Profit-audit #4: empirical calibration multiplier — closes the loop
        # from the nightly calibration job. A confidence bucket that has
        # historically won 70% of trades multiplies risk by 1.22x; a bucket
        # that has only won 35% multiplies by 0.70x. Defaults to 1.0 when
        # insufficient samples (no cold-start bias).
        cal_mult = calibration_multiplier(confidence)
        # r46 Tier 1: calibration also gates ENTRY when bucket Wilson-LB(WR)
        # is statistically below break-even with sufficient sample size.
        # Buckets where realized WR is 25% on n=30 trades is pure capital
        # incinerator; sizing-down is necessary but insufficient.
        try:
            from database import SessionLocal as _SL_cg, ConfidenceCalibration as _CC_cg
            bucket = f"{int(float(confidence) // 10) * 10}-{int(float(confidence) // 10) * 10 + 9}"
            _db_cg = _SL_cg()
            try:
                _crow = _db_cg.query(_CC_cg).filter(_CC_cg.bucket == bucket).first()
                if _crow and getattr(_crow, "n", 0) >= 30:
                    _wr = float(_crow.win_rate or 0)
                    _n = int(_crow.n or 0)
                    # Wilson-LB at 95% confidence:
                    if _n > 0:
                        z = 1.96
                        p = max(0.0, min(1.0, _wr))
                        denom = 1 + z*z/_n
                        center = (p + z*z/(2*_n)) / denom
                        margin = (z * ((p*(1-p) + z*z/(4*_n))/_n) ** 0.5) / denom
                        wilson_lb = center - margin
                        if wilson_lb < 0.35:
                            logger.info(
                                f"AutoTrader skip {ticker}: calibration bucket {bucket} "
                                f"WR={_wr*100:.0f}% on n={_n} (Wilson-LB {wilson_lb*100:.0f}% < 35%)"
                            )
                            metrics.inc("autotrade_skip", reason="calibration_gate")
                            return None
            finally:
                _db_cg.close()
        except Exception as _ce:
            logger.debug(f"calibration gate skipped: {_ce}")
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
        # AI confidence multiplier — joins the multiplier stack and is
        # bounded by the same RISK_MULT_CEILING. Shadow mode returns 1.0
        # so this is a no-op until you flip AI_CONFIDENCE_MULT_MODE=active.
        ai_mult = 1.0
        try:
            from services import ai_judge as _aij
            if _aij.confidence_mult_mode() != "off":
                _ai_ctx = _build_ai_context(ticker, db)
                _ai_signal_view = {
                    "ticker": ticker, "signal_type": signal.get("signal_type"),
                    "confidence": signal.get("confidence"),
                    "timeframe": signal.get("timeframe"),
                    "strategy": signal.get("strategy"),
                }
                _r = _aij.confidence_multiplier(_ai_signal_view, _ai_ctx)
                ai_mult = float(_r.get("multiplier", 1.0))
        except Exception as _e:
            logger.debug(f"ai_judge confidence_multiplier wrapper failed: {_e}")

        from services.config import RISK_MULT_CEILING as _MULT_CEILING
        # r39 audit fix #13: removed dead duplicate `raw_stack` and
        # `effective_risk_budget` assignments — first lines were silently
        # overwritten by the second (without ai_mult / heat_mult).
        raw_stack = conf_mult * kelly_mult * cal_mult * strat_mult * vix_mult * ai_mult
        clamped_stack = min(raw_stack, _MULT_CEILING)
        # Heat-aware throttle: applies AFTER the multiplier-stack ceiling so
        # the heat-throttle still pulls things smaller even when other
        # factors maxed out the 2× cap. This is a downward-only adjustment
        # and not part of the upside-stack so it doesn't interact with the
        # ceiling.
        from services.risk_manager import heat_aware_risk_multiplier as _heat_mult
        heat_mult = _heat_mult(equity)
        # r53 Tier-3 E: portfolio-Kelly book throttle. Cuts the entire
        # book's risk to 40-70% nominal when 60d expectancy<0 or Sharpe<0.5.
        # Gated by cfg.portfolio_kelly_enabled (default on).
        pk_mult = 1.0
        try:
            if bool(getattr(cfg, "portfolio_kelly_enabled", True)):
                from services.risk_manager import portfolio_kelly_book_throttle as _pk
                pk_mult = float(_pk())
        except Exception:
            pk_mult = 1.0
        effective_risk_budget = risk_budget * clamped_stack * heat_mult * pk_mult
        if pk_mult < 1.0:
            logger.info(
                f"AutoTrader {ticker}: portfolio_kelly throttle {pk_mult:.2f}× "
                f"(60d expectancy/Sharpe argues for smaller book)"
            )
        if raw_stack > _MULT_CEILING:
            logger.info(
                f"AutoTrader {ticker}: multiplier stack {raw_stack:.2f}× clamped to {_MULT_CEILING}× "
                f"(conf={conf_mult:.2f} kelly={kelly_mult:.2f} cal={cal_mult:.2f} strat={strat_mult:.2f} vix={vix_mult:.2f})"
                f"(conf={conf_mult:.2f} kelly={kelly_mult:.2f} cal={cal_mult:.2f} "
                f"strat={strat_mult:.2f} vix={vix_mult:.2f} ai={ai_mult:.2f})"
            )
        if heat_mult < 1.0:
            logger.info(
                f"AutoTrader {ticker}: heat-aware throttle {heat_mult:.2f}× applied"
            )
        max_qty_by_risk = int(effective_risk_budget / risk_per_share)
        max_qty_by_remaining = int(stock_remaining / entry)
        # r46 Tier 1: per-ticker cap scales with confidence headroom. Base
        # 0.30 of stock_budget; 95-conf signal gets 0.50, 75-conf signal
        # gets 0.30. Lets ultra-high-EV signals breathe without breaking
        # diversity at average-conf.
        _conf_cap_pct = 0.30 + 0.20 * min(1.0, conf_headroom)
        max_qty_by_per_ticker = int((stock_budget * _conf_cap_pct) / entry)
        max_qty_by_cash = int(cash / entry)
        max_qty_by_bp = int(buying_power / entry)
        # r46 Tier 1: notional gap-cap. Tight 0.5%-stop on a $400 stock
        # otherwise sizes a 20% notional position; a 5% gap-down = 10× the
        # planned R loss. Cap notional × expected-overnight-gap-ATR ≤
        # 1% × equity. Uses signal['atr'] as gap-vol proxy when present.
        try:
            atr_for_gap = float(signal.get("atr") or 0.0)
            if atr_for_gap > 0 and equity > 0:
                expected_gap = max(2.0 * atr_for_gap, 0.02 * entry)   # 2×ATR or 2% floor
                notional_at_qty1 = entry
                gap_loss_at_qty1 = expected_gap
                # Solve: qty * gap_loss_per_share <= 0.01 * equity
                _max_qty_by_gap = int((0.01 * equity) / max(0.01, gap_loss_at_qty1))
            else:
                _max_qty_by_gap = 10**9
        except Exception:
            _max_qty_by_gap = 10**9
        qty = min(
            max_qty_by_risk, max_qty_by_remaining,
            max_qty_by_per_ticker, max_qty_by_cash, max_qty_by_bp,
            _max_qty_by_gap,
        )

        if qty < 1:
            return None

        # r41 review fix A: 1-min bar confirmation moved BEFORE the entry
        # lock at the top of consider_signal so a slow OHLCV fetch can't
        # stall parallel scans waiting on the lock. The post-lock duplicate
        # that used to live here was removed.

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

        # ════════════════════════════════════════════════════════════════════
        # § PRE-FOMC QUIET HOUR DEFER (r47 #TP-flow)
        # ════════════════════════════════════════════════════════════════════
        # Liquidity + slippage spike in the last 60 min before an FOMC
        # release (Lucca-Moench 2015 + post-pub microstructure work). The
        # alpha is in HOLDING through the event, not in entering at the
        # widest spread of the day. Defer non-urgent entries.
        try:
            from services.r47_overlays import in_pre_fomc_quiet_hour as _pfqh
            if _pfqh(60):
                logger.info(f"AutoTrader skip {ticker}: pre-FOMC quiet hour")
                metrics.inc("autotrade_skip", reason="pre_fomc_quiet_hour")
                return None
        except Exception:
            pass

        # ════════════════════════════════════════════════════════════════════
        # § ORDER-FLOW GATES (r48 BACKLOG)
        # ════════════════════════════════════════════════════════════════════
        try:
            from services import order_flow as _of_gate
            if bool(getattr(cfg, "flow_strategies_enabled", True)):
                # Spread-widening defer — toxicity proxy.
                if _of_gate.spread_widening_defer(ticker):
                    logger.info(f"AutoTrader skip {ticker}: spread widened > 1.8× EMA")
                    metrics.inc("autotrade_skip", reason="spread_widening")
                    return None
                # Aggressor-flow gate — persistent contra-direction pressure.
                _direction_g = (signal.get("signal_type") or "BUY").upper()
                if _of_gate.aggressor_flow_gate(ticker, _direction_g):
                    logger.info(f"AutoTrader skip {ticker}: aggressor-flow against {_direction_g}")
                    metrics.inc("autotrade_skip", reason="aggressor_flow")
                    return None
        except Exception:
            pass

        # ════════════════════════════════════════════════════════════════════
        # § HALT / LULD GUARD (r47 #T0d-5)
        # ════════════════════════════════════════════════════════════════════
        # Limit-at-mid orders submitted during a halt sit at the pre-halt
        # price. When the halt resolves at a different price (LULD limit-up
        # auction can clear 10-20% from pre-halt), our resting limit fills
        # against the new market — instant deep underwater.
        # Free heuristic without an Alpaca halt feed: stale stock quote in RTH.
        # If WS hasn't ticked in >30s during open hours for a previously-active
        # ticker, treat as halt-suspect and skip entry. The 30s threshold is
        # conservative — well above the typical 1-5s tick interval but well
        # below the 1m halt minimum.
        try:
            if (bool(getattr(cfg, "halt_detect_enabled", True))
                    and paper_trader.is_market_open()):
                _q = live_quotes.get_stock_quote(ticker) or {}
                _qts = float(_q.get("ts") or 0)
                import time as _t_halt
                if _qts > 0:
                    _stale_s = _t_halt.time() - _qts
                    if _stale_s > 30:
                        logger.warning(
                            f"AutoTrader skip {ticker}: quote stale {_stale_s:.0f}s "
                            f"during RTH (halt-suspect)"
                        )
                        metrics.inc("autotrade_skip", reason="halt_suspect")
                        return None
        except Exception:
            pass

        # ════════════════════════════════════════════════════════════════════
        # § ORDER SUBMIT — bracket submission, AutoTrade row, broadcast
        # ════════════════════════════════════════════════════════════════════
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
                # r43 Tier 2 M9: quote freshness check — bid/ask older than
                # 5s isn't trustable for limit-at-mid. Fall back to market.
                qts = float(q.get("ts") or 0)
                import time as _t_qf
                fresh = (qts > 0) and ((_t_qf.time() - qts) <= 5.0)
                if fresh and bid > 0 and ask > 0 and ask > bid:
                    _limit_px = round((bid + ask) / 2.0, 2)
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
            # r46 fix #0.7: configurable TIF. GTC keeps brackets alive across
            # weekends → exposed to Sunday-night gap events. Setting cfg.
            # bracket_tif="day" caps that exposure (positions intentionally
            # uncovered after RTH; manage tick re-evaluates).
            time_in_force=str(getattr(cfg, "bracket_tif", "gtc") or "gtc"),
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
            if "insufficient buying power" in err_lower or "insufficient_buying_power" in err_lower:
                _trip_bp_breaker(minutes=30)
                logger.warning(
                    f"AutoTrader: buying-power exhausted, pausing new entries 30m"
                )
                metrics.inc("autotrade_event", event="bp_exhausted")
                _raise_alert("warning", "bp_breaker", f"Buying power exhausted on {ticker}; new entries paused 30m", ticker=ticker)
            elif any(code in err_lower for code in ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable", "gateway timeout", "internal server error")):
                _trip_broker_breaker(minutes=10)
                logger.error(f"AutoTrader: Alpaca 5xx detected, broker-down circuit breaker tripped for 10m")
                metrics.inc("autotrade_event", event="broker_down")
                _raise_alert("error", "broker_down", f"Alpaca 5xx on {ticker} submit: {res['error'][:200]}", ticker=ticker)
            elif any(code in err_lower for code in ("403", "pattern day trader", "day-trade", "pdt", "wash trade", "wash_trade")):
                # r48 BACKLOG #lifecycle-P1-13: PDT 403 retry-storm breaker.
                from services.risk_manager import trip_pdt_breaker as _tp
                _tp(hours=24)
                logger.error(f"AutoTrader: PDT/wash 403 detected, locking out new entries 24h")
                metrics.inc("autotrade_event", event="pdt_lockout")
                _raise_alert("critical", "pdt_lockout",
                             f"PDT/wash violation on {ticker}: {res['error'][:200]} — 24h lockout", ticker=ticker)
            else:
                # r48 BACKLOG observability P0-2: generic submit_rejected alert
                # for everything that isn't BP / 5xx / PDT — sub-penny,
                # not_tradable, max_position, etc. — instead of silent error rows.
                _raise_alert("error", "submit_rejected",
                             f"{ticker} submit rejected: {res['error'][:200]}", ticker=ticker)
                metrics.inc("autotrade_event", event="submit_rejected")
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
        try:
            db.commit()
        except Exception as _ie:
            # r46 fix #0.8: IntegrityError on UNIQUE(idempotency_key) means
            # another concurrent scanner already inserted this signal. Roll
            # back, log, and skip — defensive last-line guard against
            # multi-instance race conditions.
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(f"AutoTrader skip {ticker}: idempotency conflict (concurrent insert?): {_ie}")
            metrics.inc("autotrade_skip", reason="idempotency_conflict")
            return None
        db.refresh(trade)
        # Postmortem fix M1: reserve BP locally so the next ticker in this
        # same scan loop sees a smaller available BP figure.
        _reserve_bp(qty * float(entry))
        metrics.inc("autotrade_event", event="opened")
        _mark_entered()  # r53l: candidate pool decision tracker
        # r42 fix #1.12: broadcast a trade_opened event so the UI can add
        # the new row immediately instead of waiting for the next 15s poll.
        try:
            from services import live_quotes as _lq_open
            _lq_open.broadcast_event_safe({
                "type": "trade_opened",
                "trade_id": trade.id,
                "ticker": trade.ticker,
                "asset_type": "stock",
                "qty": int(qty),
                "entry_price": float(entry),
                "stop": float(stop),
                "target1": float(t1) if t1 else None,
            })
        except Exception:
            pass
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
        # r53l: persist the captured verdict to CandidatePool for
        # operator visibility. No-op when the ticker isn't in the pool.
        try:
            _persist_decision()
        except Exception:
            pass
        # r53 (Tier-1 #6): release advisory lock + threading lock in
        # reverse acquisition order.
        try:
            _adv_lock_ctx.__exit__(None, None, None)
        except Exception:
            pass
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
    r53 (Tier-1 #6): also acquires Postgres advisory lock for cross-instance.
    """
    if not _entry_lock.acquire(timeout=30.0):
        logger.warning(f"consider_put_play({ticker}): entry lock busy >30s, skipping")
        metrics.inc("autotrade_event", event="entry_lock_timeout")
        return None
    _adv_lock_ctx_pp = _pg_advisory_entry_lock(ticker)
    _adv_acq_pp = _adv_lock_ctx_pp.__enter__()
    if not _adv_acq_pp:
        try:
            _adv_lock_ctx_pp.__exit__(None, None, None)
        except Exception:
            pass
        _entry_lock.release()
        return None
    _begin_decision(ticker, "option")  # r53l
    db = SessionLocal()
    try:
        cfg = get_config(db)
        if not cfg.enabled:
            _gate_record("disabled", "fail", formula="cfg.enabled = false → bot is paused")
            metrics.inc("autotrade_skip", reason="disabled")
            return None
        if not cfg.trade_options:
            _gate_record("trade_options_off", "fail",
                         formula="cfg.trade_options = false → put-play hunt skipped")
            metrics.inc("autotrade_skip", reason="trade_options_off")
            return None
        if not paper_trader.is_enabled():
            _gate_record("broker_not_enabled", "fail", formula="Alpaca client not initialized")
            metrics.inc("autotrade_skip", reason="broker_not_enabled")
            return None
        _gate_record("trade_options_off", "pass",
                     formula="cfg.trade_options = true → put-play hunt enabled")

        ticker = ticker.upper()

        # Global ticker blacklist.
        if is_blacklisted(ticker, cfg):
            _gate_record("ticker_blacklisted", "fail",
                         ticker=ticker, blacklist=getattr(cfg, "ticker_blacklist", ""),
                         formula=f"{ticker} is in cfg.ticker_blacklist")
            metrics.inc("autotrade_skip", reason="ticker_blacklisted")
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
        # (including adopted — externally-held position counts).
        existing = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.status.in_(["pending", "open", "adopted"]),
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
            _gate_record("no_bear_thesis", "fail",
                         formula="build_bear_thesis returned None — no viable bearish setup (insufficient breakdown evidence, support holding, or failed to find a swing-high stop)")
            # r53t: surface "no bear thesis" so the candidate pool shows
            # WHY put-play didn't fire instead of muting it as no_signal.
            metrics.inc("autotrade_skip", reason="no_bear_thesis")
            return None
        _gate_record("no_bear_thesis", "pass",
                     thesis_confidence=round(float(thesis.get("confidence", 0)), 2),
                     formula=f"bear thesis built with confidence {thesis.get('confidence', 0):.0f}")
        # r66: capture thesis as signal_view so the Decision Log audit
        # panel shows entry/stop/target levels and reasoning for option puts.
        try:
            _DECISION_TLS.signal_view = {
                "confidence": thesis.get("confidence"),
                "signal_type": "PUT",
                "timeframe": "1d",
                "strategy": "Bear Thesis (put play)",
                "entry": thesis.get("entry"),
                "stop_loss": thesis.get("stop"),
                "target1": thesis.get("target1"),
                "target2": thesis.get("target2"),
                "atr": thesis.get("atr"),
                "reasoning": thesis.get("reasoning") or thesis.get("rationale"),
            }
        except Exception:
            pass
        # r58: floor is now configurable. Previously hardcoded as
        # `60 if aggressive else 0.85 × threshold`. Operator can tune via
        # cfg.option_thesis_min_conf_aggressive / option_thesis_min_conf_mult
        # without a code deploy. Previously 45 / 0.7× let a conf-53 GFS
        # put through that lost $360 on weak volume — now operator owns
        # that risk threshold explicitly.
        aggressive = bool(getattr(cfg, "aggressive_options_mode", False))
        if aggressive:
            min_bear_conf = float(getattr(cfg, "option_thesis_min_conf_aggressive", 60.0) or 60.0)
            _floor_source = "cfg.option_thesis_min_conf_aggressive (aggressive_options_mode=true)"
        else:
            mult = float(getattr(cfg, "option_thesis_min_conf_mult", 0.85) or 0.85)
            min_bear_conf = cfg.confidence_threshold * mult
            _floor_source = f"cfg.confidence_threshold ({cfg.confidence_threshold}) × cfg.option_thesis_min_conf_mult ({mult}) = {min_bear_conf:.0f}"
        _bear_conf_val = thesis["confidence"]
        if _bear_conf_val < min_bear_conf:
            _gate_record("bear_conf_below_floor", "fail",
                         bear_thesis_conf=round(float(_bear_conf_val), 2),
                         floor=round(float(min_bear_conf), 2),
                         floor_source=_floor_source,
                         aggressive=aggressive,
                         formula=f"bear thesis confidence {_bear_conf_val:.0f} < floor {min_bear_conf:.0f} ({_floor_source}) → reject")
            metrics.inc("autotrade_skip", reason=f"bear_conf_{int(thesis['confidence'])}_below_{int(min_bear_conf)}")
            return None
        _gate_record("bear_conf_below_floor", "pass",
                     bear_thesis_conf=round(float(_bear_conf_val), 2),
                     floor=round(float(min_bear_conf), 2),
                     floor_source=_floor_source,
                     aggressive=aggressive,
                     formula=f"bear thesis confidence {_bear_conf_val:.0f} ≥ floor {min_bear_conf:.0f} → continue")

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
        # r58: min option-contract score is now configurable.
        if aggressive:
            min_score = float(getattr(cfg, "option_contract_min_score_aggressive", MIN_OPTION_SCORE_AGGRESSIVE) or MIN_OPTION_SCORE_AGGRESSIVE)
        else:
            min_score = float(getattr(cfg, "option_contract_min_score", MIN_OPTION_SCORE) or MIN_OPTION_SCORE)
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
        # VIX-scaled option allocation: options are punished harder than stocks
        # during vol spikes (IV crush, gamma whipsaw), so we shrink the options
        # bucket when VIX elevates. Stocks bucket is left untouched.
        from services.risk_manager import vix_options_bucket_multiplier as _vix_opt_mult
        option_budget = equity * cfg.option_pct_of_equity * _vix_opt_mult()
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
        # r39 audit fix #9: previously `risk_per_contract = effective_max_loss`,
        # which assumes the underlying-stop fires before premium collapses — too
        # optimistic. For naked options, the realistic worst case is full premium
        # loss (option goes to zero on a fast adverse move while the underlying-
        # stop hasn't fired yet — exactly the CNTA -$2440 / VTWO -$6500 pattern).
        # Floor at 50% of premium so sizing is bounded by realistic worst case.
        _eml = float(top.get("effective_max_loss") or top.get("max_loss_per_contract") or 0)
        if _eml <= 0:
            return None
        notional_per_contract = _prem * 100
        risk_per_contract = max(_eml, notional_per_contract * 0.5)
        # r39 audit fix #13: previously this block had two `risk_budget = ...`
        # lines; the first ignored heat-mult and was silently overwritten.
        from services.risk_manager import adaptive_risk_multiplier as _adapt_risk
        from services.risk_manager import heat_aware_risk_multiplier as _heat_mult
        # r47 Tier P (A5): graded IV-rank sizing factor on long-premium puts.
        try:
            from services.r47_overlays import iv_rank_size_factor as _ivf
            _iv_pct_p = top.get("iv_rank_pct")
            _iv_factor_p = _ivf(_iv_pct_p) if bool(getattr(cfg, "iv_rank_graded_sizing", True)) else 1.0
        except Exception:
            _iv_factor_p = 1.0
        if _iv_factor_p <= 0.001:
            logger.info(f"AutoTrader skip PUT {ticker}: IV-rank graded factor = 0 (vol expensive veto)")
            metrics.inc("autotrade_skip", reason="iv_rank_graded_veto")
            return None
        # r48 BACKLOG #options-P0-5: portfolio Greeks cap.
        try:
            from services.risk_manager import portfolio_greeks_caps_breached as _pgcb
            _prosp_vega = float(top.get("vega") or 0.10) * 100
            _prosp_gamma = float(top.get("gamma") or 0) * 100
            _prosp_delta = float(top.get("delta") or top.get("delta_estimate") or -0.4) * 100
            br = _pgcb(equity, _prosp_vega, _prosp_gamma, _prosp_delta)
            if br["vega"] or br["gamma"] or br["delta"]:
                logger.info(f"AutoTrader skip PUT {ticker}: portfolio Greeks cap breached {br}")
                metrics.inc("autotrade_skip", reason="portfolio_greeks_cap")
                return None
        except Exception:
            pass
        # B6: Earnings IV-crush sidestep — high IV + earnings within 24h.
        try:
            from services.r47_overlays import earnings_iv_crush_sidestep as _eics
            if _eics(ticker, top.get("iv_rank_pct")):
                logger.info(f"AutoTrader skip PUT {ticker}: B6 IV-crush sidestep active")
                metrics.inc("autotrade_skip", reason="iv_crush_sidestep")
                return None
        except Exception:
            pass
        risk_budget = equity * cfg.max_risk_per_trade_pct * _adapt_risk() * _heat_mult(equity) * _iv_factor_p
        max_qty_by_risk = int(risk_budget / risk_per_contract)
        max_qty_by_remaining = int(option_remaining / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_per_ticker = int((option_budget * per_ticker_frac) / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_cash = int(cash / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_bp = int(buying_power / notional_per_contract) if notional_per_contract > 0 else 0
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

        # Opening-bell guard: refuse new option entries in the first 30 min of
        # the session (r53: extended from 15→30 min). VTWO at 9:48 ET (18 min
        # post-open) and AMKR / CNTA at minute 17–18 all blew up on entry
        # slippage; the 15-min guard wasn't enough. OPRA spreads stay 200%+
        # wide for ~30 minutes post-bell on most names.
        _mso = paper_trader.minutes_since_open()
        if _mso is not None and _mso < 30.0:
            logger.info(f"AutoTrader skip PUT {ticker} {occ}: only {_mso:.0f}m since open (opening-bell guard)")
            metrics.inc("autotrade_event", event="opening_guard_put")
            return None

        logger.info(
            f"AutoTrader PUT {ticker} {occ} qty={qty} premium={top['premium']} "
            f"score={top['score']} bear-conf={thesis['confidence']}"
        )

        # r48 BACKLOG fix: marketable-limit BUY with cross fallback (was market).
        # r53 fix: pass requested_premium so the slippage-abandon gate can
        # reject fills > 1.25× requested.
        res = paper_trader.submit_option_entry_with_cross_fallback(
            occ_symbol=occ, qty=qty, cross_after_seconds=30.0,
            requested_premium=float(top["premium"]),
        )
        if "error" in res:
            # r53: slippage_abandon is a deliberate skip, not a failure —
            # the entry path proactively cancelled rather than crossing
            # past 1.25× requested premium. Don't pollute the trade
            # ledger with an "error" row; just log + skip-counter.
            if res.get("error") == "slippage_abandon":
                metrics.inc("autotrade_skip", reason="option_slippage_abandon")
                logger.info(
                    f"AutoTrader put {ticker} {occ} ABANDONED on slippage: "
                    f"requested ${res.get('requested_premium','?')} "
                    f"ask ${res.get('ask','?')} max ${res.get('max_allowed','?')}"
                )
                return None
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

        # r47 fix #T0c-1: option entries had NO idempotency_key, so multi-
        # instance Cloud Run could double-buy the same put on the same ticker.
        # Build a deterministic key tying together ticker + side + strike +
        # expiry + DTE bucket so retries / multi-instance are de-duped.
        try:
            from datetime import date as _today_d
            _occ_idem = (
                f"opt|put|{ticker}|{top.get('strike')}|"
                f"{top.get('expiration')}|{int(top.get('dte') or 0)//7}|"
                f"{_today_d.today().isoformat()}"
            )
        except Exception:
            _occ_idem = None
        trade = AutoTrade(
            ticker=ticker,
            symbol=occ,
            asset_type="option",
            side="buy",
            qty=qty,
            original_qty=qty,
            requested_entry=float(top["premium"]),
            underlying_entry_price=float(thesis.get("entry") or 0) or None,
            stop_loss=float(thesis["stop_loss"]),
            current_stop=float(thesis["stop_loss"]),
            target1=float(thesis["target1"]),
            target2=float(thesis["target2"]),
            target3=float(thesis["target3"]) if thesis.get("target3") else None,
            level_index=0,
            parent_order_id=res.get("id"),
            status="pending",
            idempotency_key=_occ_idem,
            # r48 BACKLOG: persist Greeks at entry for portfolio caps + post-mortem
            entry_delta=float(top.get("delta_estimate") or 0) or None,
            entry_gamma=float(top.get("gamma") or 0) or None,
            entry_theta=float(top.get("theta") or 0) or None,
            entry_vega=float(top.get("vega") or 0) or None,
            entry_iv=float(top.get("iv") or 0) or None,
            note=(
                f"PUT play: bear-conf {thesis['confidence']} | strike {top['strike']} "
                f"exp {top['expiration']} ({top['dte']}d) | underlying stop "
                f"${thesis['stop_loss']:.2f}, T1 ${thesis['target1']:.2f}, T2 ${thesis['target2']:.2f}"
            ),
        )
        db.add(trade)
        try:
            db.commit()
        except Exception as _ie:
            db.rollback()
            from sqlalchemy.exc import IntegrityError as _IE
            if isinstance(_ie, _IE):
                logger.warning(f"PUT idempotency_conflict {ticker} {occ}: {_occ_idem}")
                metrics.inc("autotrade_skip", reason="idempotency_conflict")
                return None
            raise
        db.refresh(trade)
        # Postmortem fix M1: reserve BP for option premium too.
        _reserve_bp(qty * float(top["premium"]) * 100.0)
        metrics.inc("autotrade_event", event="opened_put")
        _mark_entered("put")  # r53l
        try:
            live_quotes.ensure_option_symbols([occ])
        except Exception:
            pass
        # r42 fix #1.12: trade_opened broadcast for option puts.
        try:
            live_quotes.broadcast_event_safe({
                "type": "trade_opened",
                "trade_id": trade.id,
                "ticker": trade.ticker,
                "asset_type": "option",
                "symbol": occ,
                "qty": int(qty),
                "entry_price": float(top["premium"]),
            })
        except Exception:
            pass
        return _serialize(trade)
    except Exception as e:
        logger.exception(f"consider_put_play error for {ticker}: {e}")
        return None
    finally:
        db.close()
        try:
            _persist_decision()  # r53l
        except Exception:
            pass
        try:
            _adv_lock_ctx_pp.__exit__(None, None, None)
        except Exception:
            pass
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
    _adv_lock_ctx_cp = _pg_advisory_entry_lock(ticker)
    _adv_acq_cp = _adv_lock_ctx_cp.__enter__()
    if not _adv_acq_cp:
        try:
            _adv_lock_ctx_cp.__exit__(None, None, None)
        except Exception:
            pass
        _entry_lock.release()
        return None
    _begin_decision(ticker, "option")  # r53l
    db = SessionLocal()
    try:
        cfg = get_config(db)
        # Requires BOTH trade_options (options trading is approved) AND
        # the call-specific flag (user has opted in to call plays).
        if not cfg.enabled:
            _gate_record("disabled", "fail", formula="cfg.enabled = false → bot is paused")
            metrics.inc("autotrade_skip", reason="disabled")
            return None
        if not cfg.trade_options:
            _gate_record("trade_options_off", "fail",
                         formula="cfg.trade_options = false → call-play hunt skipped (puts must be enabled first)")
            metrics.inc("autotrade_skip", reason="trade_options_off")
            return None
        if not getattr(cfg, "trade_calls", False):
            _gate_record("trade_calls_off", "fail",
                         formula="cfg.trade_calls = false → call-play hunt skipped (operator hasn't enabled calls)")
            metrics.inc("autotrade_skip", reason="trade_calls_off")
            return None
        if not paper_trader.is_enabled():
            _gate_record("broker_not_enabled", "fail", formula="Alpaca client not initialized")
            metrics.inc("autotrade_skip", reason="broker_not_enabled")
            return None
        _gate_record("trade_calls_off", "pass",
                     formula="cfg.trade_options=true AND cfg.trade_calls=true → call-play hunt enabled")

        ticker = ticker.upper()

        # Global ticker blacklist.
        if is_blacklisted(ticker, cfg):
            _gate_record("ticker_blacklisted", "fail",
                         ticker=ticker, blacklist=getattr(cfg, "ticker_blacklist", ""),
                         formula=f"{ticker} is in cfg.ticker_blacklist")
            metrics.inc("autotrade_skip", reason="ticker_blacklisted")
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
            AutoTrade.status.in_(["pending", "open", "adopted"]),
        ).first()
        existing_option = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.asset_type == "option",
            AutoTrade.status.in_(["pending", "open", "adopted"]),
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
            _gate_record("no_bull_thesis", "fail",
                         formula="build_bull_thesis returned None — no viable bullish setup (insufficient breakout evidence, resistance holding, or failed swing-low stop)")
            metrics.inc("autotrade_skip", reason="no_bull_thesis")
            return None
        _gate_record("no_bull_thesis", "pass",
                     thesis_confidence=round(float(thesis.get("confidence", 0)), 2),
                     formula=f"bull thesis built with confidence {thesis.get('confidence', 0):.0f}")
        # r66: capture thesis as signal_view so the audit panel shows
        # entry/stop/target levels and reasoning for option calls.
        try:
            _DECISION_TLS.signal_view = {
                "confidence": thesis.get("confidence"),
                "signal_type": "CALL",
                "timeframe": "1d",
                "strategy": "Bull Thesis (call play)",
                "entry": thesis.get("entry"),
                "stop_loss": thesis.get("stop"),
                "target1": thesis.get("target1"),
                "target2": thesis.get("target2"),
                "atr": thesis.get("atr"),
                "reasoning": thesis.get("reasoning") or thesis.get("rationale"),
            }
        except Exception:
            pass
        # r58: floor is now configurable; mirrors put gate at line ~3653.
        if aggressive:
            min_bull_conf = float(getattr(cfg, "option_thesis_min_conf_aggressive", 60.0) or 60.0)
            _floor_source = "cfg.option_thesis_min_conf_aggressive (aggressive_options_mode=true)"
        else:
            mult = float(getattr(cfg, "option_thesis_min_conf_mult", 0.85) or 0.85)
            min_bull_conf = cfg.confidence_threshold * mult
            _floor_source = f"cfg.confidence_threshold ({cfg.confidence_threshold}) × cfg.option_thesis_min_conf_mult ({mult}) = {min_bull_conf:.0f}"
        _bull_conf_val = thesis["confidence"]
        if _bull_conf_val < min_bull_conf:
            _gate_record("bull_conf_below_floor", "fail",
                         bull_thesis_conf=round(float(_bull_conf_val), 2),
                         floor=round(float(min_bull_conf), 2),
                         floor_source=_floor_source,
                         aggressive=aggressive,
                         formula=f"bull thesis confidence {_bull_conf_val:.0f} < floor {min_bull_conf:.0f} ({_floor_source}) → reject")
            metrics.inc("autotrade_skip", reason=f"bull_conf_{int(thesis['confidence'])}_below_{int(min_bull_conf)}")
            return None
        _gate_record("bull_conf_below_floor", "pass",
                     bull_thesis_conf=round(float(_bull_conf_val), 2),
                     floor=round(float(min_bull_conf), 2),
                     formula=f"bull thesis confidence {_bull_conf_val:.0f} ≥ floor {min_bull_conf:.0f} → continue")

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
        # r58: min option-contract score is now configurable.
        if aggressive:
            min_score = float(getattr(cfg, "option_contract_min_score_aggressive", MIN_OPTION_SCORE_AGGRESSIVE) or MIN_OPTION_SCORE_AGGRESSIVE)
        else:
            min_score = float(getattr(cfg, "option_contract_min_score", MIN_OPTION_SCORE) or MIN_OPTION_SCORE)
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
        # VIX-scaled option allocation: options are punished harder than stocks
        # during vol spikes (IV crush, gamma whipsaw), so we shrink the options
        # bucket when VIX elevates. Stocks bucket is left untouched.
        from services.risk_manager import vix_options_bucket_multiplier as _vix_opt_mult
        option_budget = equity * cfg.option_pct_of_equity * _vix_opt_mult()
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
        # r39 audit fix #9: previously `risk_per_contract = effective_max_loss`,
        # which assumes the underlying-stop fires before premium collapses — too
        # optimistic. For naked options, the realistic worst case is full premium
        # loss (option goes to zero on a fast adverse move while the underlying-
        # stop hasn't fired yet — exactly the CNTA -$2440 / VTWO -$6500 pattern).
        # Floor at 50% of premium so sizing is bounded by realistic worst case.
        _eml = float(top.get("effective_max_loss") or top.get("max_loss_per_contract") or 0)
        if _eml <= 0:
            return None
        notional_per_contract = _prem * 100
        risk_per_contract = max(_eml, notional_per_contract * 0.5)
        # r39 audit fix #13: previously this block had two `risk_budget = ...`
        # lines; the first ignored heat-mult and was silently overwritten.
        from services.risk_manager import adaptive_risk_multiplier as _adapt_risk
        from services.risk_manager import heat_aware_risk_multiplier as _heat_mult
        # r47 Tier P (A5): graded IV-rank sizing on long-premium calls.
        try:
            from services.r47_overlays import iv_rank_size_factor as _ivf
            _iv_pct_c = top.get("iv_rank_pct")
            _iv_factor_c = _ivf(_iv_pct_c) if bool(getattr(cfg, "iv_rank_graded_sizing", True)) else 1.0
        except Exception:
            _iv_factor_c = 1.0
        if _iv_factor_c <= 0.001:
            logger.info(f"AutoTrader skip CALL {ticker}: IV-rank graded factor = 0 (vol expensive veto)")
            metrics.inc("autotrade_skip", reason="iv_rank_graded_veto")
            return None
        try:
            from services.r47_overlays import earnings_iv_crush_sidestep as _eics
            if _eics(ticker, top.get("iv_rank_pct")):
                logger.info(f"AutoTrader skip CALL {ticker}: B6 IV-crush sidestep active")
                metrics.inc("autotrade_skip", reason="iv_crush_sidestep")
                return None
        except Exception:
            pass
        # r48 BACKLOG #options-P0-5: portfolio Greeks cap (CALL side).
        try:
            from services.risk_manager import portfolio_greeks_caps_breached as _pgcb_c
            _prosp_vega_c = float(top.get("vega") or 0.10) * 100
            _prosp_gamma_c = float(top.get("gamma") or 0) * 100
            _prosp_delta_c = float(top.get("delta") or top.get("delta_estimate") or 0.4) * 100
            br_c = _pgcb_c(equity, _prosp_vega_c, _prosp_gamma_c, _prosp_delta_c)
            if br_c["vega"] or br_c["gamma"] or br_c["delta"]:
                logger.info(f"AutoTrader skip CALL {ticker}: portfolio Greeks cap breached {br_c}")
                metrics.inc("autotrade_skip", reason="portfolio_greeks_cap")
                return None
        except Exception:
            pass
        risk_budget = equity * cfg.max_risk_per_trade_pct * _adapt_risk() * _heat_mult(equity) * _iv_factor_c
        max_qty_by_risk = int(risk_budget / risk_per_contract)
        max_qty_by_remaining = int(option_remaining / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_per_ticker = int((option_budget * per_ticker_frac) / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_cash = int(cash / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_bp = int(buying_power / notional_per_contract) if notional_per_contract > 0 else 0
        max_qty_by_dollar_cap = int(per_contract_dollar_cap / notional_per_contract) if notional_per_contract > 0 else 0
        qty = min(
            max_qty_by_risk, max_qty_by_remaining,
            max_qty_by_per_ticker, max_qty_by_cash, max_qty_by_bp,
            max_qty_by_dollar_cap,
        )
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

        # Opening-bell guard — mirror of put-side. r53: extended 15 → 30 min.
        # First 30 min has the widest spreads; VTWO/CNTA/AMKR all blew up on
        # entry slippage at minute 17–18 post-open.
        _mso = paper_trader.minutes_since_open()
        if _mso is not None and _mso < 30.0:
            logger.info(f"AutoTrader skip CALL {ticker} {occ}: only {_mso:.0f}m since open (opening-bell guard)")
            metrics.inc("autotrade_event", event="opening_guard_call")
            return None

        logger.info(
            f"AutoTrader CALL {ticker} {occ} qty={qty} premium={top['premium']} "
            f"score={top['score']} bull-conf={thesis['confidence']}"
        )

        # r48 BACKLOG fix: marketable-limit BUY with cross fallback (was market).
        # r53 fix: pass requested_premium so the slippage-abandon gate can
        # reject fills > 1.25× requested.
        res = paper_trader.submit_option_entry_with_cross_fallback(
            occ_symbol=occ, qty=qty, cross_after_seconds=30.0,
            requested_premium=float(top["premium"]),
        )
        if "error" in res:
            if res.get("error") == "slippage_abandon":
                metrics.inc("autotrade_skip", reason="option_slippage_abandon")
                logger.info(
                    f"AutoTrader call {ticker} {occ} ABANDONED on slippage: "
                    f"requested ${res.get('requested_premium','?')} "
                    f"ask ${res.get('ask','?')} max ${res.get('max_allowed','?')}"
                )
                return None
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

        # r47 fix #T0c-1: idempotency_key for CALL plays — see PUT for rationale.
        try:
            from datetime import date as _today_d
            _occ_idem = (
                f"opt|call|{ticker}|{top.get('strike')}|"
                f"{top.get('expiration')}|{int(top.get('dte') or 0)//7}|"
                f"{_today_d.today().isoformat()}"
            )
        except Exception:
            _occ_idem = None
        trade = AutoTrade(
            ticker=ticker,
            symbol=occ,
            asset_type="option",
            side="buy",
            qty=qty,
            original_qty=qty,
            requested_entry=float(top["premium"]),
            underlying_entry_price=float(thesis.get("entry") or 0) or None,
            stop_loss=float(thesis["stop_loss"]),
            current_stop=float(thesis["stop_loss"]),
            target1=float(thesis["target1"]),
            target2=float(thesis["target2"]),
            target3=float(thesis["target3"]) if thesis.get("target3") else None,
            level_index=0,
            parent_order_id=res.get("id"),
            status="pending",
            idempotency_key=_occ_idem,
            # r48 BACKLOG: persist Greeks at entry for portfolio caps + post-mortem
            entry_delta=float(top.get("delta_estimate") or 0) or None,
            entry_gamma=float(top.get("gamma") or 0) or None,
            entry_theta=float(top.get("theta") or 0) or None,
            entry_vega=float(top.get("vega") or 0) or None,
            entry_iv=float(top.get("iv") or 0) or None,
            note=(
                f"CALL play: bull-conf {thesis['confidence']} | strike {top['strike']} "
                f"exp {top['expiration']} ({top['dte']}d) | underlying stop "
                f"${thesis['stop_loss']:.2f}, T1 ${thesis['target1']:.2f}, T2 ${thesis['target2']:.2f}"
            ),
        )
        db.add(trade)
        try:
            db.commit()
        except Exception as _ie:
            db.rollback()
            from sqlalchemy.exc import IntegrityError as _IE
            if isinstance(_ie, _IE):
                logger.warning(f"CALL idempotency_conflict {ticker} {occ}: {_occ_idem}")
                metrics.inc("autotrade_skip", reason="idempotency_conflict")
                return None
            raise
        db.refresh(trade)
        _reserve_bp(qty * float(top["premium"]) * 100.0)
        metrics.inc("autotrade_event", event="opened_call")
        _mark_entered("call")  # r53l
        try:
            live_quotes.ensure_option_symbols([occ])
        except Exception:
            pass
        # r42 fix #1.12: trade_opened broadcast for option calls.
        try:
            live_quotes.broadcast_event_safe({
                "type": "trade_opened",
                "trade_id": trade.id,
                "ticker": trade.ticker,
                "asset_type": "option",
                "symbol": occ,
                "qty": int(qty),
                "entry_price": float(top["premium"]),
            })
        except Exception:
            pass
        return _serialize(trade)
    except Exception as e:
        logger.exception(f"consider_call_play error for {ticker}: {e}")
        return None
    finally:
        db.close()
        try:
            _persist_decision()  # r53l
        except Exception:
            pass
        try:
            _adv_lock_ctx_cp.__exit__(None, None, None)
        except Exception:
            pass
        _entry_lock.release()


# ---------- Manage: trail stops, reconcile ---------------------------------

# Broker-interaction helpers moved to services.execution_engine.
# Back-compat aliases below — existing call sites don't change.
from services.execution_engine import (
    get_legs as _get_legs,
    identify_legs as _identify_legs,
    replace_stop as _replace_stop,
    replace_tp as _replace_tp,
    _replace_stop_cache,  # re-exported for any introspection use
)


# Chandelier helpers, price lookup, target recalc, and reverse-thesis
# checks moved to services.position_manager. Back-compat aliases below.
from services.position_manager import (
    chandelier_atr as _chandelier_atr,
    chandelier_adx as _chandelier_adx,
    adaptive_chandelier_mult as _adaptive_chandelier_mult,
    current_price as _current_price,
)


# Target-recalc helpers moved to services.position_manager.
from services.position_manager import (
    recalculate_targets as _recalculate_targets,
    record_target_history as _record_target_history,
)


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
    # r53d fix: see stock-side comment in manage_open_positions — strict
    # `pstatus == "filled"` never matched alpaca-py's "OrderStatus.FILLED".
    _raw = parent.status
    pstatus = (
        getattr(_raw, "value", None)
        or str(_raw).split(".")[-1]
    )
    pstatus = (pstatus or "").lower()

    # 1) Pending → open
    if t.status == "pending":
        # r39 audit fix #22: previously `"filled" in pstatus` matched both
        # "filled" AND "partially_filled". For options without a bracket SL
        # there's no SL-leg-vs-actual-qty mismatch, but `t.qty` was left at
        # the requested qty rather than the actually-filled qty — inflating
        # portfolio heat and trim sizes. Now: special-case partially_filled
        # to update `t.qty` from `filled_qty` before promoting to open
        # (matching the stock side's behavior).
        if pstatus == "partially_filled":
            try:
                filled_qty = int(getattr(parent, "filled_qty", 0) or 0)
                if filled_qty > 0:
                    t.qty = float(filled_qty)
                    t.note = (t.note or "") + (
                        f" | OPTION PARTIAL FILL: using {filled_qty} of original qty"
                    )
            except Exception as _e:
                logger.debug(f"option partial-fill qty update {t.symbol}: {_e}")
            t.entry_price = float(parent.filled_avg_price) if parent.filled_avg_price else t.requested_entry
            t.status = "open"
            t.filled_at = datetime.utcnow()
            db.commit()
            logger.info(f"AutoTrader PUT {t.ticker} {t.symbol} partially filled qty={t.qty} @ {t.entry_price}")
        elif pstatus == "filled":
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

    # r47 fix #T0g-3: DTE≤0 force-flatten. Long options held into the
    # final hour of expiry have a near-zero theta floor: OTM contracts
    # decay to 0; ITM contracts auto-exercise (Alpaca paper assigns 100
    # shares per call → bot ends Monday with an unexpected stock leg
    # blowing the stock budget and PDT counter). Detect by parsing the
    # OCC symbol's expiry and force-close before close on expiry day.
    try:
        from datetime import datetime as _dt_dte0
        from zoneinfo import ZoneInfo as _ZI_dte0
        _occ_dte = (t.symbol or "")
        if len(_occ_dte) >= 16:
            yymmdd = _occ_dte[-15:-9]
            exp = _dt_dte0.strptime(yymmdd, "%y%m%d")
            now_et = _dt_dte0.now(_ZI_dte0("America/New_York"))
            # Same calendar day as expiry, after the configured cutoff hour
            try:
                _cfg_dte0 = get_config(db)
                cutoff_hr = int(getattr(_cfg_dte0, "option_dte0_flatten_hour_et", 15) or 15)
            except Exception:
                cutoff_hr = 15
            if (exp.date() == now_et.date() and now_et.hour >= cutoff_hr):
                from services.execution_engine import force_close_trade as _fct_dte0
                logger.warning(
                    f"AutoTrader DTE0-flatten {t.ticker} {t.symbol}: expiry today, "
                    f"now {now_et.strftime('%H:%M')} ET ≥ {cutoff_hr}:00 — force-close"
                )
                _fct_dte0(
                    t, db,
                    reason=f"DTE0 force-flatten before expiry close",
                    summary=summary,
                    status_override="closed_dte0",
                )
                summary["closed"] += 1
                return
    except Exception as _dte_e:
        logger.debug(f"DTE0 check {t.symbol}: {_dte_e}")

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

    # CALL vs PUT detection — single source of truth in
    # position_manager.is_call_option (parses the OCC symbol's P/C
    # indicator at position [-9]). Reviewer flagged duplicated
    # inline parsing here as the AMKR-style direction-drift bug
    # source — the inline parse has been removed.
    is_put = not _is_call_option(t)
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
                # ADX-aware T1 trim: weak trend (ADX<25) → 50% of original,
                # strong trend (ADX>40) → 15%, parabolic (ADX≥45) → 0% (skip
                # trim entirely — let the runner run; just trail the stop).
                _t1_frac = trim_fraction_for_adx(t.ticker, "T1", default_frac=0.50)
                if _t1_frac <= 0.0:
                    half = 0   # extreme-trend short-circuit; stop trail still runs below
                else:
                    half = max(1, int(orig * _t1_frac))
                    half = min(half, int(t.qty))   # can't trim more than we hold
                if half >= 1:
                    # r51 fix: gate on RTH. Alpaca rejects option market orders
                    # outside RTH ("options market orders are only allowed during
                    # market hours"). Prior code submitted regardless and fired a
                    # noisy `option_trim_failed` alert every manage tick during
                    # extended hours — also blocking T1-confirmation state from
                    # advancing. Defer the trim to the first manage tick after
                    # the open; retry next loop. This is normal flow, not an
                    # error, so we DON'T raise an alert and we DON'T advance
                    # state — letting the runner ride past T1 with no trim
                    # until RTH resumes.
                    if not paper_trader.is_market_open():
                        logger.info(
                            f"AutoTrader {'PUT' if is_put else 'CALL'} {t.ticker} T1 hit @ "
                            f"underlying {px:.2f} but OUTSIDE RTH — deferring option trim "
                            f"to next manage tick at market open"
                        )
                        # Skip the trim block, fall through to stop-trail below.
                        half = 0
                if half >= 1:
                    # r42 fix #2.2: marketable limit on option trim — saves
                    # ~5-15% of premium vs the prior market order.
                    trim = paper_trader.submit_option_exit_marketable_limit(
                        occ_symbol=t.symbol, qty=half, side="sell",
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
                        # r51 fix: classify "outside-RTH" rejects as a deferred
                        # condition (no alert), other failures as real errors.
                        err_str = str(err).lower()
                        is_rth_reject = (
                            "market hours" in err_str
                            or "only allowed during market" in err_str
                            or "42210000" in err_str
                        )
                        if is_rth_reject:
                            logger.info(
                                f"option partial-trim deferred for {t.symbol}: outside RTH; will retry next tick"
                            )
                        else:
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
    # r53 fix (Tier-0 #2): the 50% decay threshold + spread-artifact guard
    # was firing on entry-spread reversion when the underlying had not yet
    # broken the thesis. VTWO closed -$6,500 after a +0.88% favorable
    # underlying move because t.entry_price was inflated by 216% slippage
    # (Tier-0 #1), and the premium just reverted to the bid. Two changes:
    #
    #   (a) Spread-artifact skip extended from <5min → ENTIRE <24h window.
    #       The original 5-min guard assumed real theta-driven 50% decay
    #       takes "hours, not seconds"; in practice it takes >24h on
    #       17+ DTE long options. If the underlying isn't broken, decay
    #       is virtually always spread/IV reversion, not thesis failure.
    #   (b) Add a progress-to-stop confirmation: even when underlying
    #       IS moving against us, require ≥40% of the way to the
    #       underlying stop before letting the premium-stop fire. This
    #       prevents a +0.5% adverse blip from triggering a 50% premium
    #       loss exit on a slippage-inflated entry.
    if not exit_reason and cur_premium is not None and t.entry_price:
        # r39 audit fix #12: previously a flat 50% decay threshold across all
        # hold times. A 6-min 50% decay is much more likely to be a quote-cross
        # than a 6-hour 50% decay. Scale by elapsed time:
        #   < 30 min held  → require 75% decay (strong evidence past spread)
        #   < 24h held     → require 50% decay (original threshold)
        #   ≥ 48h held     → require 40% decay (theta is real by now)
        from datetime import datetime as _dt_pm
        opened = t.filled_at or t.opened_at
        held_secs = (_dt_pm.utcnow() - opened).total_seconds() if opened else 99999
        if held_secs < 1800:        # < 30 min
            decay_threshold_pct = 0.25   # require ≥ 75% decay
        elif held_secs < 86400:     # < 24h
            decay_threshold_pct = 0.50
        elif held_secs >= 172800:   # ≥ 48h
            decay_threshold_pct = 0.60   # require ≥ 40% decay
        else:                       # 24-48h transitional
            decay_threshold_pct = 0.55
        decay_trigger_price = t.entry_price * decay_threshold_pct
        if cur_premium <= decay_trigger_price:
            # r53: extend spread-artifact window from <5min to <24h.
            # Real thesis-failure decay rarely happens within the first
            # day on 17+ DTE contracts; what we typically see in <24h
            # is spread/IV reversion of the entry premium.
            spread_artifact_window = held_secs < 86400  # 24h
            # Check underlying direction against thesis
            underlying_against_us = False
            _u_entry = getattr(t, "underlying_entry_price", None)
            if px is not None and _u_entry and _u_entry > 0:
                if is_put:
                    underlying_against_us = px > float(_u_entry) * 1.001
                else:
                    underlying_against_us = px < float(_u_entry) * 0.999
            # r53 (b): even when the underlying IS against us, require
            # meaningful progress toward the underlying stop before
            # firing the premium-stop. 0.4R (40% of the entry→stop
            # distance) is the floor.
            progress_to_stop_ok = False
            if (
                px is not None
                and _u_entry
                and _u_entry > 0
                and t.current_stop is not None
                and t.current_stop > 0
            ):
                stop_distance = abs(float(_u_entry) - float(t.current_stop))
                if stop_distance > 0:
                    move_against = (
                        max(0.0, px - float(_u_entry))
                        if is_put
                        else max(0.0, float(_u_entry) - px)
                    )
                    progress_to_stop_ok = (move_against / stop_distance) >= 0.4

            # Final firing rule: in the spread-artifact window (now <24h),
            # we only fire when BOTH (i) underlying is against us AND
            # (ii) we're ≥40% to the underlying stop. Outside the window
            # (≥24h held), the time-scaled threshold itself is enough —
            # real theta decay is not a spread artifact.
            should_fire = True
            if spread_artifact_window:
                if not underlying_against_us:
                    should_fire = False
                    logger.info(
                        f"AutoTrader skip premium-stop {t.ticker} {t.symbol}: "
                        f"held {held_secs:.0f}s, underlying not against us "
                        f"(px={px} u_entry={_u_entry}) — likely spread/IV reversion"
                    )
                elif not progress_to_stop_ok:
                    should_fire = False
                    logger.info(
                        f"AutoTrader skip premium-stop {t.ticker} {t.symbol}: "
                        f"held {held_secs:.0f}s, underlying against us but "
                        f"<0.4R progress to u-stop {t.current_stop} — likely "
                        f"slippage-inflated entry, not thesis failure"
                    )

            if not should_fire:
                metrics.inc("autotrade_event", event="premium_stop_spread_skip")
            else:
                pct_lost = (1 - cur_premium / t.entry_price) * 100
                exit_reason = (
                    f"premium decayed to ${cur_premium:.2f} ({pct_lost:.0f}% loss vs entry "
                    f"${t.entry_price:.2f}; held {held_secs/60:.0f}m, "
                    f"threshold {(1-decay_threshold_pct)*100:.0f}%)"
                )
                final_status = "closed_stop"

    # 5b) Theta stop — if the underlying has barely moved after 48h of holding,
    # the thesis is failing slowly via theta decay rather than via a clean
    # stop-out. Cut the trade rather than bleeding to expiration. Threshold:
    # < 0.2R toward target after 48h. R is measured against the underlying's
    # initial-risk distance (entry → bear/bull stop on the underlying).
    if not exit_reason and px is not None:
        # r43 fix #1.10: theta-stop now scales with DTE. Previously fixed
        # 48h hold + 0.2R progress threshold — too late for a 7-DTE option
        # (theta has already eaten 30%+ in 48h) and too early for a
        # 60-DTE that just needs more time.
        try:
            from datetime import datetime as _dt_th
            opened = t.filled_at or t.opened_at
            held_h = (_dt_th.utcnow() - opened).total_seconds() / 3600 if opened else 0
            req_entry = float(getattr(t, "requested_entry", None) or 0)
            req_stop = float(getattr(t, "stop_loss", None) or 0)
            # Pull DTE from OCC symbol (positions 6-12 yyMMdd) when possible.
            dte_now = None
            try:
                _occ = t.symbol or ""
                if len(_occ) >= 13:
                    yymmdd = _occ[-15:-9]
                    from datetime import datetime as _dt_yymmdd
                    exp = _dt_yymmdd.strptime(yymmdd, "%y%m%d")
                    dte_now = max(0.0, (exp - _dt_th.utcnow()).total_seconds() / 86400.0)
            except Exception:
                dte_now = None
            # Hold floor: 12h for ≤7-DTE, 24h for 7-30 DTE, 48h otherwise.
            if dte_now is None:
                hold_floor = 48.0
            elif dte_now <= 7:
                hold_floor = 12.0
            elif dte_now <= 30:
                hold_floor = 24.0
            else:
                hold_floor = 48.0
            # Progress floor: more permissive for shorter DTE (theta eats fast,
            # we need to stop the bleeding sooner). 0.4R for ≤7 DTE, 0.3R
            # for 7-30, 0.2R for >30.
            if dte_now is None:
                prog_floor = 0.2
            elif dte_now <= 7:
                prog_floor = 0.4
            elif dte_now <= 30:
                prog_floor = 0.3
            else:
                prog_floor = 0.2
            if held_h >= hold_floor and req_entry > 0 and req_stop > 0:
                R_under = abs(req_entry - req_stop)
                if R_under > 0:
                    progress = (req_entry - px) / R_under if is_put else (px - req_entry) / R_under
                    if progress < prog_floor:
                        exit_reason = (
                            f"theta stop: held {held_h:.0f}h DTE={dte_now}, underlying progress "
                            f"{progress:.2f}R < {prog_floor:.2f}R — thesis stalled"
                        )
                        final_status = "closed_stop"
                        metrics.inc("autotrade_event", event="theta_stop")
        except Exception:
            pass

    if exit_reason:
        # r42 fix #2.2: marketable limit on full option exit, fallback to market.
        sell = paper_trader.submit_option_exit_marketable_limit(
            occ_symbol=t.symbol, qty=int(t.qty), side="sell",
        )
        if "error" in sell:
            logger.warning(f"option sell failed for {t.symbol}: {sell['error']}")
            return
        # Capture realised P/L from the position before it disappears.
        # r42 fix #0.1: ADD the runner-leg PnL to any partial-trim PnL
        # already accumulated on this row — the prior assignment overwrote
        # T1/T2 trim profits, mis-classifying every partial-then-stopped
        # trade as a clean loss and biasing the freeze gate downward.
        try:
            existing = float(t.realized_pl or 0.0)
            if pos and pos.get("current_price") is not None and t.entry_price:
                runner_pl = round((float(pos["current_price"]) - float(t.entry_price)) * float(t.qty) * 100, 2)
                t.realized_pl = round(existing + runner_pl, 2)
            elif cur_premium is not None and t.entry_price:
                runner_pl = round((float(cur_premium) - float(t.entry_price)) * float(t.qty) * 100, 2)
                t.realized_pl = round(existing + runner_pl, 2)
            else:
                # Couldn't price the runner leg — keep any accumulated
                # partial-trim PnL rather than nulling it out.
                if existing == 0.0:
                    t.realized_pl = None
                logger.warning(f"option {t.symbol}: runner P/L unavailable (pos={bool(pos)}, cur_premium={cur_premium}); kept partial PL={existing:.2f}")
        except (TypeError, ValueError) as _e:
            logger.warning(f"option {t.symbol}: runner P/L calc skipped: {_e} (kept partial PL={float(t.realized_pl or 0):.2f})")
        t.status = final_status or "closed_manual"
        t.closed_at = datetime.utcnow()
        t.note = (t.note or "") + f" | EXIT: {exit_reason}"
        # r44 fix #0.3: backfill MLPrediction outcome on every close path.
        try:
            _backfill_ml_outcome(db, t)
        except Exception:
            pass
        db.commit()
        summary["closed"] += 1
        metrics.inc("autotrade_event", event=t.status)
        logger.info(f"AutoTrader PUT {t.ticker} {t.symbol} closed: {exit_reason} (PL ${t.realized_pl or 0:.2f})")
        # Broadcast trade_closed so the UI shows a toast + browser
        # notification on exit (companion to target_hit on TP trails).
        try:
            from services import live_quotes as _lq_close
            _lq_close.broadcast_event_safe({
                "type": "trade_closed",
                "trade_id": t.id,
                "ticker": t.ticker,
                "asset_type": t.asset_type,
                "status": t.status,
                "reason": exit_reason,
                "realized_pl": round(float(t.realized_pl or 0), 2),
            })
        except Exception:
            pass
        if t.status == "closed_stop" and (t.realized_pl or 0) < 0:
            _post_mortem_async(t.id)


# Reverse-thesis helpers moved to services.position_manager.
# Back-compat aliases below — REVERSE_CONFIDENCE_GATE is re-exported from
# position_manager so any external importer of auto_trader.REVERSE_CONFIDENCE_GATE
# still works.
from services.position_manager import (
    check_reversals_for,
    trade_source_timeframe as _trade_source_timeframe,
    is_call_option as _is_call_option,
    check_reversal as _check_reversal,
    REVERSE_CONFIDENCE_GATE,
    _TF_RANK,
)


def _force_close_trade(
    t: AutoTrade,
    db: Session,
    reason: str,
    summary: Dict[str, Any],
    status_override: Optional[str] = None,
) -> None:
    """Thin wrapper — delegates to execution_engine, passes the
    target-touch-count cleanup as a callback."""
    from services.execution_engine import force_close_trade as _ee_force_close
    _ee_force_close(
        t, db, reason, summary, status_override=status_override,
        on_close=lambda closed_t: _touch_clear(closed_t, db),
    )


def manage_open_positions() -> Dict[str, Any]:
    """
    Periodic job:
      • For pending entries — promote to 'open' once filled (capture entry_price + leg ids).
      • For open trades — when current price ≥ T1, move stop to entry (break-even).
      • Reconcile closed trades (status sync from broker).

    r43 fix #1.16: in-process lock prevents the scheduler tick and the
    stop-threat fast-path (WS thread) from interleaving on the same trade
    rows — without it, both paths could increment counters / submit
    duplicate trims / race the level_index advance.
    """
    summary = {"checked": 0, "trailed": 0, "closed": 0, "errors": 0}
    if not _manage_lock.acquire(timeout=15.0):
        # Another manage tick is in flight — skip this one rather than
        # block. Both scheduler and fast-path are best-effort retry-on-tick.
        logger.info("manage_open_positions: another tick in flight, skipping")
        summary["skipped_concurrent"] = 1
        return summary
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
                "flatten_by_eod": bool(getattr(cfg, "flatten_by_eod", False)),
                "daily_loss_limit_pct": float(getattr(cfg, "daily_loss_limit_pct", 0.03) or 0.03),
            }
            trade_ids = [
                tid for (tid,) in _bootstrap_db.query(AutoTrade.id).filter(
                    AutoTrade.status.in_(["pending", "open"])
                ).all()
            ]
        finally:
            _bootstrap_db.close()

        # r46 fix #0.5: crisis-protection checks now run INSIDE manage_open_positions,
        # not just at consider_signal entry. Before this, a flash crash with zero
        # new signals never tripped the daily-loss halt or the auto-deleverage —
        # positions kept trailing into the abyss. Now, on every manage tick:
        # check session DD; trim 33% of every losing position at -4%; engage
        # kill-switch at -6%.
        try:
            from services.risk_manager import (
                session_equity_drawdown_pct as _sed_m,
                dynamic_daily_loss_limit_pct as _dll_m,
            )
            sed_now = _sed_m()
            if sed_now is not None and sed_now >= 0.06:
                logger.critical(
                    f"manage_open_positions: session DD {sed_now*100:.2f}% ≥ 6% — engaging kill switch (no new signals required)"
                )
                try:
                    _raise_alert("critical", "auto_deleverage_kill",
                                 f"Auto-deleverage triggered: session DD {sed_now*100:.2f}%")
                except Exception:
                    pass
                try:
                    kill(reason=f"manage-tick auto-deleverage session_dd={sed_now*100:.2f}%",
                         flatten=True, cancel_orders=True)
                except Exception:
                    pass
                summary["killed"] = 1
                return summary
            if sed_now is not None and sed_now >= 0.04:
                summary["session_dd_4pct_active"] = True
                try:
                    _raise_alert("warning", "auto_deleverage_trim",
                                 f"Session DD {sed_now*100:.2f}% ≥ 4% — trimming losers, blocking new entries")
                except Exception:
                    pass
        except Exception as _crisis_e:
            logger.debug(f"manage-tick crisis check skipped: {_crisis_e}")

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

                    # r44 fix Wave 4: stocks earnings-flatten. If earnings
                    # ≤ 1h away on an open stock position, trim 50% (mirror
                    # of the options 6h flatten). Halves earnings-gap loss
                    # tail on stuck holds.
                    if t.status == "open" and t.asset_type == "stock" and t.entry_price:
                        try:
                            from services.earnings import hours_to_next_earnings as _hne_s
                            _hte_s = _hne_s(t.ticker)
                            if _hte_s is not None and _hte_s <= 1.0 and t.qty and t.qty >= 2 and not t.hit_t1:
                                _half = max(1, int(t.qty // 2))
                                from alpaca.trading.requests import MarketOrderRequest as _MOR_e
                                from alpaca.trading.enums import OrderSide as _OS_e, TimeInForce as _TIF_e
                                _trim_e = paper_trader._get_client().submit_order(order_data=_MOR_e(
                                    symbol=t.ticker, qty=_half,
                                    side=_OS_e.SELL, time_in_force=_TIF_e.DAY,
                                ))
                                if _trim_e is not None:
                                    t.qty = t.qty - _half
                                    t.note = (t.note or "") + f" | EARNINGS PRE-FLATTEN: trimmed {_half} ({_hte_s:.1f}h to print)"
                                    db.commit()
                                    metrics.inc("autotrade_event", event="earnings_pre_flatten")
                        except Exception as _ef:
                            logger.debug(f"earnings pre-flatten {t.ticker}: {_ef}")

                    # r44 fix #0.5: EOD flatten for intraday signals when
                    # cfg.flatten_by_eod is set. Fires at 15:55 ET.
                    if t.status == "open" and bool(cfg_snapshot.get("flatten_by_eod", False)):
                        try:
                            _src_tf_eod = _trade_source_timeframe(t, db)
                            if _src_tf_eod in ("5m", "15m", "30m"):
                                from zoneinfo import ZoneInfo as _ZI_eod
                                _now_et = datetime.utcnow().replace(tzinfo=_ZI_eod("UTC")).astimezone(_ZI_eod("America/New_York"))
                                if (_now_et.hour, _now_et.minute) >= (15, 55):
                                    _force_close_trade(t, db, "EOD flatten intraday", summary, status_override="closed_eod")
                                    continue
                        except Exception as _eod_e:
                            logger.debug(f"EOD flatten check {t.ticker}: {_eod_e}")

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
                    # r53d fix: alpaca-py 0.21.1 serializes OrderStatus.FILLED
                    # as "OrderStatus.FILLED" (str()), not "filled". The strict
                    # equality at line ~4886 (`pstatus == "filled"`) was
                    # silently FALSE for every fill — pending→open transitions
                    # never fired via this path. Trades only made it to "open"
                    # via promote_adopted / force_close paths. IREN trade #28
                    # spent 6 days stuck in `pending` because the parent IS
                    # filled but the bot never noticed. Use .value (canonical
                    # lowercase) when present; fall back to splitting the dot.
                    _raw = parent.status
                    pstatus = (
                        getattr(_raw, "value", None)
                        or str(_raw).split(".")[-1]
                    )
                    pstatus = (pstatus or "").lower()

                    # 1) Promote pending → open once parent filled.
                    # B5: strict "filled" match. Previous `"filled" in pstatus`
                    # also matched `"partially_filled"`, which led to DB-qty
                    # mismatch vs actual shares + SL leg sized to the ORIGINAL
                    # order quantity rather than filled_qty. Now: treat
                    # partial-fill separately and reshape SL/TP legs to the
                    # actual filled quantity, so a cancel of the unfilled
                    # remainder won't leave a mis-sized bracket.
                    # r42 fix #2.1: limit-at-mid cancel timer. A limit at mid
                    # can sit unfilled for the rest of the session if the book
                    # walks away. After 30s of "new"/"accepted" status with no
                    # fill, cancel-and-cross — submit a fresh market order at
                    # the new ask to actually take the trade. After 5min total
                    # without fill, give up and free the BP slot.
                    try:
                        if (
                            t.status == "pending"
                            and pstatus in ("new", "accepted", "pending_new")
                            and t.opened_at is not None
                        ):
                            age_s = (datetime.utcnow() - t.opened_at).total_seconds()
                            if age_s > 300:
                                logger.warning(
                                    f"AutoTrader {t.ticker} unfilled limit > 5m old; cancelling and freeing slot"
                                )
                                try:
                                    # Manage-tick cancels — fire-and-forget,
                                    # don't add 4s latency per call.
                                    paper_trader.cancel_order(t.parent_order_id, wait_for_terminal=False)
                                except Exception:
                                    pass
                                t.status = "closed_unfilled"
                                t.closed_at = datetime.utcnow()
                                t.note = (t.note or "") + " | UNFILLED: limit timed out"
                                db.commit()
                                _release_bp(float(t.qty) * float(t.requested_entry or 0))
                                metrics.inc("autotrade_event", event="closed_unfilled")
                                continue
                            elif age_s > 30:
                                # Cancel-and-cross: cancel the limit, submit
                                # a market order at the same qty + bracket.
                                logger.info(
                                    f"AutoTrader {t.ticker} limit unfilled after {age_s:.0f}s; crossing to market"
                                )
                                try:
                                    # Manage-tick cancels — fire-and-forget,
                                    # don't add 4s latency per call.
                                    paper_trader.cancel_order(t.parent_order_id, wait_for_terminal=False)
                                except Exception:
                                    pass
                                cross = paper_trader.submit_bracket_order(
                                    symbol=t.ticker, qty=int(t.qty), side="buy",
                                    entry_type="market",
                                    take_profit=t.target3 or t.target2 or t.target1,
                                    stop_loss=float(t.stop_loss),
                                    time_in_force="gtc",
                                    client_order_id=f"at-cross-{__import__('uuid').uuid4().hex[:12]}",
                                )
                                if "error" not in cross:
                                    t.parent_order_id = cross.get("id") or t.parent_order_id
                                    t.note = (t.note or "") + f" | CROSSED to market after {age_s:.0f}s unfilled"
                                    db.commit()
                                else:
                                    logger.warning(f"limit-cross resubmit failed for {t.ticker}: {cross.get('error')}")
                    except Exception as _le:
                        logger.debug(f"limit cancel-timer skipped: {_le}")
                    if t.status == "pending" and pstatus == "partially_filled":
                        filled_qty = float(getattr(parent, "filled_qty", 0) or 0)
                        if filled_qty > 0 and filled_qty < t.qty:
                            logger.warning(
                                f"AutoTrader {t.ticker} partial fill: {filled_qty}/{t.qty}; "
                                f"cancelling remainder + resizing bracket legs"
                            )
                            try:
                                paper_trader.cancel_order(t.parent_order_id, wait_for_terminal=False)
                            except Exception as e:
                                logger.warning(f"partial-fill cancel remainder failed for {t.ticker}: {e}")
                            t.qty = int(filled_qty)
                            t.note = (t.note or "") + f" | PARTIAL FILL: using {int(filled_qty)} of original qty"
                            db.commit()
                    if t.status == "pending" and pstatus == "filled":
                        # r39 audit critical-1: previously this block was wrapped
                        # in `if True:` with an `elif "canceled"...:` chained
                        # underneath — making the cancel/reject/expired branch
                        # UNREACHABLE since the elif chained off the `if True:`,
                        # not the outer condition. Effect: pending parent orders
                        # that got canceled/rejected/expired stayed in `pending`
                        # forever, blocking the concurrent-position cap.
                        # Cancel/reject/expired now handled in a separate `if`
                        # block below.
                        legs = _identify_legs(t.parent_order_id)
                        # r42 fix #2.3: reconcile DB qty with broker filled_qty
                        # at fill confirmation. If a partial fill earlier
                        # resized t.qty correctly we're already in sync; this
                        # defensive check catches the broker-side mismatch case
                        # (e.g., Alpaca returns filled status with a partial
                        # filled_qty + cancel reason) and resizes SL/TP legs.
                        try:
                            broker_filled = float(getattr(parent, "filled_qty", 0) or 0)
                            if broker_filled > 0 and abs(broker_filled - float(t.qty)) >= 0.5:
                                logger.warning(
                                    f"AutoTrader {t.ticker} fill qty mismatch: DB={t.qty} broker={broker_filled}; "
                                    f"resizing DB qty + bracket legs to broker truth"
                                )
                                t.qty = int(broker_filled)
                                # Resize SL/TP children to match the actually-held qty.
                                from alpaca.trading.requests import ReplaceOrderRequest
                                _client = paper_trader._get_client()
                                for _leg_id in (legs.get("stop_id"), legs.get("tp_id")):
                                    if _leg_id and _client:
                                        try:
                                            _client.replace_order_by_id(
                                                _leg_id,
                                                order_data=ReplaceOrderRequest(qty=int(broker_filled)),
                                            )
                                        except Exception as _re:
                                            logger.warning(f"bracket leg resize failed for {t.ticker}: {_re}")
                        except Exception as _re:
                            logger.debug(f"partial-fill reconcile skipped for {t.ticker}: {_re}")
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
                                # r47 fix #T1-2 / observability P0-1: track every
                                # fill's slippage in bps and alert on outliers.
                                # Without aggregation, slippage was the #1 silent
                                # leakage vector during prior live testing.
                                try:
                                    slip_bps = abs(slip) / req_entry * 10_000.0
                                    metrics.observe("autotrade_slippage_bps", slip_bps,
                                                    asset_type="stock")
                                    if slip_bps > 50.0:
                                        _raise_alert(
                                            "warning", "slippage_outlier",
                                            f"{t.ticker} slip {slip_bps:.0f}bps "
                                            f"({slip:+.2f}) on fill",
                                            ticker=t.ticker, trade_id=t.id,
                                        )
                                except Exception:
                                    pass
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
                                        # r43 fix #0.5: replace the broker TP
                                        # leg too. Without this, broker held
                                        # the original far_tp; bot intent and
                                        # broker reality silently diverged.
                                        if t.tp_order_id and (t.target3 or t.target2 or t.target1):
                                            _new_tp_target = float(t.target3 or t.target2 or t.target1)
                                            _new_tp_id = _replace_tp(t.tp_order_id, _new_tp_target)
                                            if _new_tp_id:
                                                t.tp_order_id = _new_tp_id
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
                                                # r42 fix #0.2: capture rotated stop_order_id.
                                                _new_id = _replace_stop(t.stop_order_id, t.stop_loss)
                                                if _new_id:
                                                    t.stop_order_id = _new_id
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
                    # r39 audit critical-1: cancel/reject/expired branch is now
                    # a separate `if`, not an unreachable `elif` chained off
                    # the (also-removed) `if True:`.
                    elif t.status == "pending" and any(
                        s in pstatus for s in ("canceled", "rejected", "expired")
                    ):
                        t.status = "closed_manual"
                        t.closed_at = datetime.utcnow()
                        t.note = (t.note or "") + f" | parent {pstatus}"
                        t.target_touch_count = 0
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
                                    # r47 fix #T0b-6: prior code `pass`'d here and
                                    # relied on the parent.legs reconcile block to
                                    # close the row. When parent.legs returns empty
                                    # (terminal bracket), the reconcile silently
                                    # skipped → row stayed `open` forever, blocking
                                    # re-entries on the ticker. Close immediately
                                    # using the SL fill data.
                                    try:
                                        _fill_px = float(getattr(sl_ord, "filled_avg_price", 0) or 0)
                                        _fill_qty = float(getattr(sl_ord, "filled_qty", 0) or t.qty or 0)
                                        if _fill_px > 0 and t.entry_price:
                                            _existing = float(t.realized_pl or 0.0)
                                            t.realized_pl = round(
                                                _existing + (_fill_px - float(t.entry_price)) * _fill_qty, 2
                                            )
                                        t.status = "closed_stop"
                                        t.closed_at = datetime.utcnow()
                                        t.note = (t.note or "") + f" | SL filled @ {_fill_px:.2f} (manage-tick reconcile)"
                                        t.target_touch_count = 0
                                        _target_touch_counts.pop(t.id, None)
                                        # Release BP and clean replace_stop cache.
                                        try:
                                            from services.risk_manager import _release_bp as _rb
                                            from services.execution_engine import _replace_stop_cache as _rsc
                                            _rb(float(t.entry_price or 0) * float(t.original_qty or t.qty or 0))
                                            _rsc.pop(t.stop_order_id, None)
                                            _rsc.pop(t.tp_order_id, None)
                                        except Exception:
                                            pass
                                        try:
                                            _backfill_ml_outcome(t, db)
                                        except Exception:
                                            pass
                                        db.commit()
                                        summary["closed"] += 1
                                        metrics.inc("autotrade_event", event="closed_stop")
                                        continue
                                    except Exception as _slf:
                                        logger.warning(f"SL-filled close path {t.ticker}: {_slf}")
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
                                        # r39 audit cleanup: escalate if rolling 1h
                                        # failure count crosses threshold. 3+ SL
                                        # resubmit failures in 1h means the broker
                                        # API is mis-behaving and the bot should be
                                        # killed by the operator. Per-occurrence
                                        # alerts are already firing above; this is
                                        # the additional one that says "this is a
                                        # pattern, not a one-off".
                                        try:
                                            _fail_count = sl_resubmit_failures_1h()
                                            if _fail_count >= 3:
                                                _raise_alert(
                                                    "critical", "sl_resubmit_storm",
                                                    f"{_fail_count} SL resubmit failures in last 1h — "
                                                    f"broker API may be impaired; consider killing "
                                                    f"the auto-trader (POST /api/trading/kill)",
                                                )
                                        except Exception:
                                            pass
                        except Exception as _e:
                            logger.warning(f"SL invariant check {t.ticker}: {_e}")

                    # Profit-max: stale-trade guard. Trades that haven't hit T1
                    # after N × timeframe minutes have had their chance — close
                    # them to recycle capital into fresher setups. Only fires
                    # for trades currently at a small loss or flat (no point
                    # closing a winning position just because T1 hasn't hit).
                    # r44 fix Wave 4: stock time-stop. Bot has theta-stop on
                    # options but no time-stop on stocks — flat-but-not-losing
                    # trades sit indefinitely consuming capital. Add: held
                    # > 4× source-TF AND 0 < pnl < 0.5R AND no T1 → close.
                    if t.status == "open" and t.entry_price and not t.hit_t1 and t.filled_at and t.asset_type == "stock":
                        try:
                            src_tf_ts = _trade_source_timeframe(t, db)
                            _ts_map = {"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440,"1mo":1440*20}
                            tf_min_ts = _ts_map.get(src_tf_ts, 240)
                            age_min_ts = (datetime.utcnow() - t.filled_at).total_seconds() / 60.0
                            if age_min_ts > 4 * tf_min_ts:
                                _px = _current_price(t.ticker)
                                _R = max(0.01, float(t.entry_price) - float(t.stop_loss or 0))
                                if _px is not None and _R > 0:
                                    _pnl_R = (_px - float(t.entry_price)) / _R
                                    if 0 < _pnl_R < 0.5:
                                        _force_close_trade(
                                            t, db,
                                            f"time-stop: held {age_min_ts/60:.1f}h with PnL {_pnl_R:.2f}R "
                                            f"(below 0.5R, capital recycle)",
                                            summary,
                                            status_override="closed_time_stop",
                                        )
                                        continue
                        except Exception as _ts_e:
                            logger.debug(f"time-stop check {t.ticker}: {_ts_e}")

                    if t.status == "open" and t.entry_price and not t.hit_t1 and t.filled_at:
                        try:
                            src_tf = _trade_source_timeframe(t, db)
                            _stale_map = {"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440,"1mo":1440*20}
                            tf_minutes = _stale_map.get(src_tf, 240)
                            age_min = (datetime.utcnow() - t.filled_at).total_seconds() / 60.0
                            max_age_min = _STALE_TRADE_TF_MULT * tf_minutes
                            if age_min > max_age_min:
                                # r39 audit fix #18: previously closed if price
                                # was less than +0.3R above entry — meaning a
                                # trade at +0.4R at hour 7 that pulled back to
                                # +0.2R at hour 8 got stale-recycled. The
                                # "winning a little" zone shouldn't qualify as
                                # stale. Now only close stale trades that are
                                # FLAT or LOSING (price at or below entry).
                                px_probe = _current_price(t.ticker)
                                entry_px = float(t.entry_price)
                                if px_probe is not None and px_probe <= entry_px:
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
                                touches = _touch_get(t) + 1
                                _touch_set(t, db, touches)
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
                                            t.target_touch_count = 0
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
                                            # r43 fix #1.7: soft-BE buffer must respect T1 distance.
                                            # Previously stop_dist = max(0.3R, 0.25×ATR) anchored to ENTRY
                                            # only — at T1=1.5R, runner trailed at entry-0.3R, riding 1.8R
                                            # underwater on noise (after a successful T1 trim!). Now we
                                            # anchor to MIN(entry-0.3R, T1-0.3R) so the runner has banked
                                            # at least 1.2R of cushion before getting stopped.
                                            atr_buffer = _chandelier_atr(t.ticker) or 0.0
                                            stop_dist = max(0.3 * initial_risk, 0.25 * atr_buffer)
                                            soft_be_entry = float(t.entry_price) - stop_dist
                                            t1_anchor = float(next_target) - stop_dist
                                            soft_be = max(soft_be_entry, t1_anchor)
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
                                            # r46 Tier 1: crisis-mode trim fraction.
                                            _t1_default = 0.33
                                            try:
                                                from services.risk_manager import crisis_t1_trim_fraction as _ct1
                                                _t1_default = _ct1(0.33)
                                            except Exception:
                                                pass
                                            _t1_frac = trim_fraction_for_adx(t.ticker, "T1", default_frac=_t1_default)
                                            # ADX≥45 → 0.0 → skip the trim
                                            # entirely. Stop still trails to
                                            # soft-BE below.
                                            trim_qty = 0 if _t1_frac <= 0.0 else max(1, int(t.qty * _t1_frac))
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
                                                    # r44 fix Wave 4: pyramid at T1 in strong trend.
                                                    # Adds 25% (of original qty) BUY at market, stop
                                                    # = entry (T1's soft-BE level). Trend-only via
                                                    # ADX≥30 AND src_tf in {1h,4h,1d}. Gated behind
                                                    # cfg.pyramid_enabled (default False) — needs
                                                    # config + telemetry rollout.
                                                    # r47 fix #T0a-7: prior code referenced `signal`
                                                    # which doesn't exist in manage scope → silent
                                                    # NameError → pyramid never executed since r44.
                                                    # Now uses live ADX + persisted source TF.
                                                    try:
                                                        _adx_now = _chandelier_adx(t.ticker) or 0.0
                                                        _src_tf_p = _trade_source_timeframe(t, db) or ""
                                                        if (
                                                            bool(getattr(cfg, "pyramid_enabled", False))
                                                            and _adx_now >= 30
                                                            and _src_tf_p in ("1h", "4h", "1d")
                                                            and t.original_qty
                                                            and (t.asset_type or "stock") == "stock"
                                                        ):
                                                            _add = max(1, int(0.25 * float(t.original_qty)))
                                                            from alpaca.trading.requests import MarketOrderRequest as _MOR_p
                                                            from alpaca.trading.enums import OrderSide as _OS_p, TimeInForce as _TIF_p
                                                            paper_trader._get_client().submit_order(order_data=_MOR_p(
                                                                symbol=t.ticker, qty=_add,
                                                                side=_OS_p.BUY, time_in_force=_TIF_p.DAY,
                                                            ))
                                                            t.qty = (t.qty or 0) + _add
                                                            # r47 fix #T0f (P1-8): resize SL leg to match new qty
                                                            # so the added shares aren't naked until next manage tick.
                                                            try:
                                                                if t.stop_order_id:
                                                                    paper_trader.replace_order_by_id(t.stop_order_id, qty=int(t.qty))
                                                            except Exception as _resize_e:
                                                                logger.warning(f"pyramid SL resize {t.ticker} failed: {_resize_e}")
                                                            t.note = (t.note or "") + f" | PYRAMID: +{_add} at T1 (ADX={_adx_now:.0f})"
                                                            metrics.inc("autotrade_event", event="pyramid_t1")
                                                    except Exception as _py_e:
                                                        logger.warning(f"pyramid {t.ticker} failed: {_py_e}")
                                        # Profit-max: T2 partial profit — trim half the remaining
                                        # runner at T2 to lock in another chunk of gains while still
                                        # leaving a runner for T3 and recalc extensions.
                                        if target_idx == 1 and t.qty >= 2:
                                            # ADX-aware T2 trim: tight in chop, loose in trends
                                            _t2_frac = trim_fraction_for_adx(t.ticker, "T2", default_frac=_T2_PARTIAL_FRAC)
                                            trim_qty = max(1, int(t.qty * _t2_frac))
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
                                        # r42 fix #0.2: replace_stop now returns rotated id.
                                        _new_sid = _replace_stop(t.stop_order_id, new_stop) if new_stop > t.current_stop else None
                                        if _new_sid:
                                            t.stop_order_id = _new_sid
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
                                                    # r43 fix #0.5: replace broker TP leg to match new T3.
                                                    if t.tp_order_id and new_targets[2]:
                                                        _new_tp = _replace_tp(t.tp_order_id, float(new_targets[2]))
                                                        if _new_tp:
                                                            t.tp_order_id = _new_tp
                                            elif target_idx == 2:
                                                # Past the first recompute —
                                                # just let chandelier trail.
                                                t.note = (t.note or "") + (
                                                    f" | level_index ≥ 3, chandelier-only trail "
                                                    f"(no more target recompute)"
                                                )
                                            _touch_clear(t, db)
                                            summary["trailed"] += 1
                                            logger.info(
                                                f"AutoTrader {t.ticker} {tag} hit, stop→{new_stop} "
                                                f"(level_index={t.level_index})"
                                            )
                            elif next_target and (t.id in _target_touch_counts or (t.target_touch_count or 0) > 0):
                                # Price fell back below the target before we
                                # got N confirmations — reset the streak.
                                _touch_clear(t, db)

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
                                # r42 fix #0.2: capture rotated id from broker replace.
                                _new_tid = _replace_stop(t.stop_order_id, new_trail_stop) if new_trail_stop > t.current_stop else None
                                if _new_tid:
                                    t.stop_order_id = _new_tid
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
                                # r42 fix #0.1: ADD runner-leg PnL to any
                                # partial-trim PnL already on this row;
                                # prior `=` assignment erased T1/T2 trims.
                                existing = float(t.realized_pl or 0.0)
                                runner_pl = round((exit_px - (t.entry_price or 0)) * t.qty, 2)
                                t.realized_pl = round(existing + runner_pl, 2)
                                if (t.realized_pl or 0) > 0:
                                    t.status = "closed_target"
                                else:
                                    t.status = "closed_stop"
                                t.closed_at = datetime.utcnow()
                                # Postmortem fix H1: clean up state-machine
                                # bookkeeping so the touch-counts dict doesn't
                                # leak by trade id over months of operation.
                                t.target_touch_count = 0
                                _target_touch_counts.pop(t.id, None)
                                # r43 fix #1.28: release in-flight BP on every
                                # close, not just `closed_unfilled`.
                                try:
                                    if t.entry_price and t.original_qty:
                                        _release_bp(float(t.entry_price) * float(t.original_qty))
                                except Exception:
                                    pass
                                # r43 fix #1.29: clean up the stop/tp cache.
                                try:
                                    if t.stop_order_id:
                                        _replace_stop_cache.pop(t.stop_order_id, None)
                                    if t.tp_order_id:
                                        _replace_stop_cache.pop(t.tp_order_id, None)
                                except Exception:
                                    pass
                                # r44 fix #0.3: backfill MLPrediction outcome.
                                # Without this the calibration loop and ML
                                # graduation gate never have data to evaluate.
                                try:
                                    _backfill_ml_outcome(db, t)
                                except Exception:
                                    pass
                                db.commit()
                                summary["closed"] += 1
                                metrics.inc("autotrade_event", event=t.status)
                                logger.info(f"AutoTrader {t.ticker} closed @ {exit_px} ({t.status}) PL={t.realized_pl:.2f}")
                                # Push trade_closed event so the UI surfaces
                                # a toast + browser notification.
                                try:
                                    from services import live_quotes as _lq_sc
                                    _lq_sc.broadcast_event_safe({
                                        "type": "trade_closed",
                                        "trade_id": t.id,
                                        "ticker": t.ticker,
                                        "asset_type": t.asset_type,
                                        "status": t.status,
                                        "reason": "broker leg fill",
                                        "realized_pl": round(float(t.realized_pl or 0), 2),
                                    })
                                except Exception:
                                    pass
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
        try:
            _manage_lock.release()
        except Exception:
            pass
        # r47 fix #T0e-4 / #T0h: prune stale OCC subscriptions for option
        # contracts that no longer have an open AutoTrade row.
        try:
            db_p = SessionLocal()
            try:
                active_occ = [
                    r.symbol for r in db_p.query(AutoTrade).filter(
                        AutoTrade.status.in_(["pending", "open"]),
                        AutoTrade.asset_type == "option",
                    ).all()
                    if r.symbol
                ]
            finally:
                db_p.close()
            live_quotes.prune_option_symbols(active_occ)
        except Exception:
            pass
    return summary
