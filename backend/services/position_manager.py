"""Position-manager helpers extracted from auto_trader.py.

Owns the supporting state-machine helpers used by
`auto_trader.manage_open_positions()`:

  * Chandelier-trail math (ATR + ADX caches, adaptive mult)
  * Live price lookup with graceful fallback
  * Per-trade target recalculation after T3 breach
  * Reverse-thesis detection + scan
  * OCC symbol parsing

The main `manage_open_positions()` loop and `_manage_option_trade()` stay
in auto_trader.py — they're the orchestrator and the option-specific
state machine, respectively. This module is everything those two call
into that isn't broker/DB plumbing.

Policy: these helpers are read-only against module-level state that
belongs to auto_trader (e.g. `_target_touch_counts`); they either take
that state as a parameter or never reach for it.
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import desc

from database import AutoTrade, Signal
from services.config import PRICE_FALLBACK_TTL_SEC as _PRICE_FALLBACK_TTL

logger = logging.getLogger(__name__)

# --------------------- Chandelier helpers ----------------------------------

_chandelier_atr_cache: Dict[str, tuple] = {}
_chandelier_adx_cache: Dict[str, tuple] = {}
_CHANDELIER_ATR_TTL = 300.0
_CHANDELIER_CACHE_MAX = 1000  # r48 BACKLOG #perf-P1.9: bounded LRU cap
import threading as _pm_threading
_chandelier_cache_lock = _pm_threading.Lock()


def _bounded_cache_set(cache: Dict[str, tuple], key: str, value: tuple,
                       max_entries: int = _CHANDELIER_CACHE_MAX) -> None:
    """LRU-ish set with cap drop oldest 10% on overflow."""
    with _chandelier_cache_lock:
        if key in cache:
            del cache[key]
        cache[key] = value
        if len(cache) > max_entries:
            for _ in range(max(1, max_entries // 10)):
                try:
                    cache.pop(next(iter(cache)))
                except StopIteration:
                    break


def chandelier_atr(ticker: str) -> Optional[float]:
    """Daily ATR_14 for `ticker`. Cached 5 min — the trail is refreshed on
    every manage tick (20s), so recomputing ATR for each would burn CPU
    pointlessly since 1d bars don't change intraday."""
    now = time.time()
    cached = _chandelier_atr_cache.get(ticker.upper())
    if cached and now < cached[1]:
        return cached[0]
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty:
            return None
        ind = compute_indicators(df)
        atr = float(ind["ATR_14"].iloc[-1])
        _bounded_cache_set(_chandelier_atr_cache, ticker.upper(), (atr, now + _CHANDELIER_ATR_TTL))
        return atr
    except Exception:
        return None


def chandelier_adx(ticker: str) -> Optional[float]:
    """Daily ADX_14 — used to loosen/tighten the chandelier trail based on
    trend strength. Strong trends (ADX > 30) let winners run with a wider
    trail; chop (ADX < 20) tightens it to reduce bleed."""
    now = time.time()
    cached = _chandelier_adx_cache.get(ticker.upper())
    if cached and now < cached[1]:
        return cached[0]
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty:
            return None
        ind = compute_indicators(df)
        if "ADX_14" not in ind.columns:
            return None
        adx = float(ind["ADX_14"].iloc[-1])
        _bounded_cache_set(_chandelier_adx_cache, ticker.upper(), (adx, now + _CHANDELIER_ATR_TTL))
        return adx
    except Exception:
        return None


def adaptive_chandelier_mult(base_mult: float, ticker: str) -> float:
    """Adjust the configured chandelier multiplier based on trend strength:
      • ADX > 30  (strong trend)     → base × 1.33 (give winners room)
      • ADX < 20  (chop)             → base × 1.25 (give chop room — r39 audit)
      • 20 ≤ ADX ≤ 30 (transitional) → base (config value unchanged)
    Returns base if ADX cannot be read.

    r39 audit fix: previously chop was 0.83× (TIGHTER stop), labelled
    "cut bleed". But chop has wider absolute noise; tightening the trail
    in chop produces more whipsaws, not fewer. Six consecutive
    paper-trade losses (April 2026) all matched the chop-whipsaw pattern.
    Inverted to 1.25× — give chop room. The structural stop (signal-side)
    still bounds total loss; the chandelier just stops fighting the chop.
    """
    adx = chandelier_adx(ticker)
    if adx is None:
        return base_mult
    # r44 fix Wave 4: also key on volatility regime via ATR percentile.
    # Compressed-vol regime → tighter trail; expanding-vol → wider trail.
    vol_factor = 1.0
    try:
        from services.data_fetcher import fetch_ohlcv as _fo_v
        from services.indicators import compute_indicators as _ci_v
        df = _fo_v(ticker, "1d")
        if df is not None and not df.empty and len(df) >= 60:
            ind = _ci_v(df.tail(252))
            atr_col = next((c for c in ind.columns if c.startswith("ATR_")), None)
            if atr_col and len(ind) > 0:
                cur_atr = float(ind[atr_col].iloc[-1])
                pctile = (ind[atr_col].rank(pct=True).iloc[-1])
                # 80+ pct = high vol → 1.4× wider; <20 pct = low → 0.85× tighter.
                if pctile >= 0.80:
                    vol_factor = 1.4
                elif pctile <= 0.20:
                    vol_factor = 0.85
    except Exception:
        pass
    # r46 Tier 1: per-ticker vol_mult overlay (TSLA gets 1.6×; KO gets 0.8×).
    try:
        from services.ticker_profile import vol_mult as _tp_vm
        ticker_vol = _tp_vm(ticker, default=1.0)
    except Exception:
        ticker_vol = 1.0
    if adx > 30:
        return base_mult * 1.33 * vol_factor * ticker_vol
    if adx < 20:
        return base_mult * 1.25 * vol_factor * ticker_vol
    return base_mult * vol_factor * ticker_vol


# --------------------- Live price lookup -----------------------------------

_price_fallback_cache: Dict[str, tuple] = {}


def current_price(ticker: str, max_age_sec: Optional[float] = None) -> Optional[float]:
    """Use live WS quote first (no network), fall back to a TTL-cached
    REST/Yahoo fetch so the manage loop doesn't hammer external APIs
    every minute for tickers that aren't streaming.

    r46 fix #0.10: callers can pin a tighter freshness budget via
    `max_age_sec`. The stop-evaluation path (manage_open_positions) now
    requires a 30-second-fresh price; if WS is stale beyond that, we
    skip the cache and fetch a fresh REST price.
    """
    try:
        from services import live_quotes
        # Caller-pinned age cap, else use the module default.
        live = live_quotes.get_live_price(ticker, max_age_sec=max_age_sec)
        if live and live > 0:
            return live
    except Exception:
        pass

    # When the caller explicitly passed a tight age budget (e.g., 30s),
    # bypass the fallback cache entirely — a 30s-old REST cache cannot
    # claim freshness for a 30s-old WS query.
    bypass_cache = max_age_sec is not None and max_age_sec <= _PRICE_FALLBACK_TTL
    now = time.time()
    if not bypass_cache:
        cached = _price_fallback_cache.get(ticker.upper())
        if cached and now < cached[1]:
            return cached[0]
    try:
        from services.data_fetcher import get_current_price as fetch_current_price
        pi = fetch_current_price(ticker)
        if pi:
            px = float(pi[0])
            _bounded_cache_set(_price_fallback_cache, ticker.upper(), (px, now + _PRICE_FALLBACK_TTL))
            return px
    except Exception:
        return None
    return None


# --------------------- Target recalculation --------------------------------

def recalculate_targets(ticker: str, direction: str,
                         current_price_value: float) -> Optional[List[float]]:
    """After T3 is breached and the trend is clearly continuing, compute the
    next three targets from `current_price_value`. Uses daily swing levels
    above (long) / below (bear) price plus gap-fill magnets; falls back to
    ATR-based steps when the chart hasn't formed enough structure beyond."""
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.support_resistance import swing_levels
        from services.indicators import compute_indicators
        from services.gap_detector import gap_targets_above, gap_targets_below
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty:
            return None
        levels = swing_levels(df, window=10, max_levels=12)
        atr = None
        try:
            ind = compute_indicators(df)
            atr = float(ind["ATR_14"].iloc[-1])
        except Exception:
            atr = None
        # Better fallback than 2%-of-price: trailing 14-day median High-Low
        # range adapts to the symbol's actual realized vol.
        if not atr or atr <= 0:
            try:
                rng = (df["High"] - df["Low"]).tail(14).dropna()
                if len(rng) >= 5:
                    med = float(rng.median())
                    if med > 0:
                        atr = med
            except Exception:
                pass
        if not atr or atr <= 0:
            atr = current_price_value * 0.02

        if direction == "long":
            swing_above = {l["price"] for l in levels if l["price"] > current_price_value * 1.005}
            gap_above = set(gap_targets_above(df, current_price_value))
            above = sorted(swing_above | gap_above)
            picks = above[:3]
        else:
            swing_below = {l["price"] for l in levels if l["price"] < current_price_value * 0.995}
            gap_below = set(gap_targets_below(df, current_price_value))
            below = sorted(swing_below | gap_below, reverse=True)
            picks = below[:3]

        while len(picks) < 3:
            step = (len(picks) + 1) * 1.5 * atr
            nxt = current_price_value + step if direction == "long" else current_price_value - step
            picks.append(round(nxt, 2))

        return [round(float(p), 2) for p in picks[:3]]
    except Exception as e:
        logger.warning(f"recalculate_targets({ticker}) failed: {e}")
        return None


def record_target_history(t: AutoTrade, reason: str, new_targets: List[float]) -> None:
    """Append a target-recalc event to the JSON audit log on the trade row."""
    try:
        existing = json.loads(t.targets_history) if t.targets_history else []
    except Exception:
        existing = []
    existing.append({
        "at": datetime.utcnow().isoformat(),
        "reason": reason,
        "targets": new_targets,
    })
    t.targets_history = json.dumps(existing)


# --------------------- OCC parser ------------------------------------------

def is_call_option(t: AutoTrade) -> bool:
    """Parse OCC symbol to detect CALL vs PUT. OCC format has the C/P
    indicator immediately before the 8-digit strike, so it's at position
    [-9] from end. AMKR260515C00075000 → 'C'."""
    sym = (getattr(t, "symbol", None) or "")
    return bool(sym) and len(sym) >= 9 and sym[-9].upper() == "C"


# --------------------- Reverse-thesis detection ----------------------------

# QUANT REVISION: Lowered to 60.0 to align with ML probability outputs (P(win)=0.60).
REVERSE_CONFIDENCE_GATE = 60.0

# Timeframe rank — higher TF carries more weight. Reverse-thesis only fires
# when the opposing signal is on a TF ≥ the original trade-source TF.
_TF_RANK = {"5m": 1, "15m": 2, "30m": 3, "1h": 4, "4h": 5, "1d": 6, "1mo": 7}


def trade_source_timeframe(t: AutoTrade, db: Session) -> str:
    """Best-effort lookup of the timeframe that produced this trade's signal."""
    if t.signal_id:
        s = db.query(Signal).filter(Signal.id == t.signal_id).first()
        if s and s.timeframe:
            return s.timeframe
    return "1d"  # safe default


def check_reversal(t: AutoTrade, db: Session) -> Optional[str]:
    """Detect a strong opposing signal that landed AFTER this trade was opened.
    Returns a reason string if we should close, else None.

      * Long stock → opposing = SELL ≥ gate
      * Long PUT   → opposing = BUY  ≥ gate (bull thesis invalidates put)
      * Long CALL  → opposing = SELL ≥ gate (bear thesis invalidates call)

    The opposing signal must be on a TF ≥ the trade's source TF — a 5m
    fakeout shouldn't be allowed to close a 1d-conviction position."""
    from datetime import timedelta as _td_grace
    opened_at = t.filled_at or t.opened_at
    if not opened_at:
        return None
    if t.asset_type == "stock":
        opposing = "SELL"
    else:
        opposing = "SELL" if is_call_option(t) else "BUY"

    # 60-second grace window — the same _run_analysis_for_ticker pass that
    # opened the trade writes signals across all other timeframes; without
    # a grace period a 1d SELL written milliseconds after the 1h BUY opens
    # would close the brand-new trade in the same heartbeat.
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
    # Opposing TF must match or EXCEED source TF. (Critical-audit fix #7.)
    src_tf = trade_source_timeframe(t, db)
    src_rank = _TF_RANK.get(src_tf, 6)
    for sig in candidates:
        if _TF_RANK.get(sig.timeframe, 0) >= src_rank:
            return (
                f"reverse-thesis {opposing} signal landed @ conf {sig.confidence:.0f} "
                f"on {sig.timeframe} (>= rank {src_rank}, src TF {src_tf}); "
                f"generated {sig.generated_at.isoformat()}"
            )
    return None


def check_reversals_for(ticker: str) -> int:
    """Run check_reversal on every open auto-trade for `ticker`. Force-close
    any trade that hits the gate. Returns count of closes triggered."""
    # Deferred import to avoid auto_trader ↔ position_manager cycle.
    from database import SessionLocal
    from services.auto_trader import _force_close_trade
    summary: Dict[str, Any] = {"closed": 0}
    db = SessionLocal()
    try:
        open_trades = db.query(AutoTrade).filter(
            AutoTrade.ticker == ticker,
            AutoTrade.status.in_(["pending", "open"]),
        ).all()
        for t in open_trades:
            try:
                reason = check_reversal(t, db)
                if reason:
                    _force_close_trade(t, db, reason, summary)
            except Exception as e:
                logger.warning(f"reversal check error on {t.ticker} #{t.id}: {e}")
        return summary["closed"]
    finally:
        db.close()


def reset_caches_for_tests() -> None:
    """Clear chandelier + price caches. Test-only."""
    _chandelier_atr_cache.clear()
    _chandelier_adx_cache.clear()
    _price_fallback_cache.clear()
