"""r57 unified scanner — collapses universe_scanner + event_detector.

Background: across r53/r54/r55/r56, four audits found ~70 bugs in this
area. The third (ground-up) audit established that the 1459-line
universe_scanner was architecturally inert (consumers read only
`ticker`), the v2 z-score stack was algebraically a no-op, the
sub-scanners overlapped 80%+ with breakout, and 100% of production
trades came from the watchlist (the scanner was dormant).

r57 is a delete pass: the 1459 + 321 lines of universe_scanner.py +
event_detector.py become this single ~280-line module. What's KEPT:

  • Static Russell 1000 universe (data/russell1000.txt)
  • Liquidity gate ($10 min price + $10M ADV)
  • Earnings fail-closed
  • One simple composite score (RVOL + RS_20d + pct_from_52w_high)
  • Three event detectors (GAP, RVOL_SURGE, SQUEEZE_RELEASE)
  • CandidatePool table writes (so existing consumers keep working)
  • CandidateEvent table writes (the event-driven path)
  • Atomic pool swap (delete-then-insert in one transaction)

What's DELETED: score_universe_v2, James-Stein shrinkage (was a no-op),
RS×pct_from_hi residualization, TOD profiles, regime weight overrides,
sub-scanner quotas (PEAD/sector_rel/vol_exp/short — all overlapped
breakout), scanner_conviction_multiplier (±15% sizing on un-validated
rank), generation-id ceremony with broken Postgres advisory lock,
mom_12_1 / rs_benchmark / _bb_width fields (computed but never read).
"""
from __future__ import annotations
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ───────────────────────── Universe loader ─────────────────────────

UNIVERSE_SIZE = 700  # Russell 1000 file currently has ~611 entries
PREFILTER_MIN_PRICE = 10.0
PREFILTER_MAX_PRICE = 2000.0
PREFILTER_MIN_DOLLAR_VOL = 10_000_000  # $10M ADV — uniform liquidity floor
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")


def _bundled_universe_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "data", "russell1000.txt"))


def _read_universe_file() -> Optional[List[str]]:
    """Read the Russell 1000 constituent file. Operator can override via
    `STOCK_UNIVERSE_FILE` env var; otherwise falls back to the bundled
    `data/russell1000.txt` (~611 names: SP500 + R1000 mid-caps)."""
    path = os.getenv("STOCK_UNIVERSE_FILE") or _bundled_universe_path()
    try:
        tickers: List[str] = []
        with open(path) as f:
            for raw in f:
                s = raw.strip().upper()
                if not s or s.startswith("#"):
                    continue
                if _TICKER_RE.match(s):
                    tickers.append(s)
        return tickers or None
    except Exception as e:
        logger.warning(f"scanner: universe file read failed ({path}): {e}")
        return None


def pull_universe(size: int = UNIVERSE_SIZE) -> List[Dict[str, Any]]:
    """Return list of {ticker, name, exchange} dicts.

    Primary path: Russell 1000 file. Defensive fallback: pull liquid
    US equities from Alpaca's /v2/assets so a missing file doesn't
    silently zero the pool (which is exactly what happened post-r57
    deploy when the Dockerfile forgot to copy data/).
    """
    tickers = _read_universe_file()
    if tickers:
        return [{"ticker": t, "name": t, "exchange": "list"} for t in tickers[:size]]
    logger.warning("scanner: universe file unavailable, falling back to Alpaca assets")
    return _pull_universe_alpaca(size)


def _pull_universe_alpaca(size: int) -> List[Dict[str, Any]]:
    """Fallback: pull tradable US equities from Alpaca. Used only when
    the Russell 1000 file is missing."""
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return []
    try:
        import httpx
        url = "https://paper-api.alpaca.markets/v2/assets"
        if os.getenv("ALPACA_LIVE", "0") == "1":
            url = "https://api.alpaca.markets/v2/assets"
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        merged: List[Dict[str, Any]] = []
        with httpx.Client(timeout=15.0) as c:
            for exch in ("NASDAQ", "NYSE"):
                r = c.get(url, headers=headers, params={
                    "status": "active", "asset_class": "us_equity", "exchange": exch,
                })
                if r.status_code == 200:
                    merged.extend(r.json())
        merged = [
            a for a in merged
            if a.get("tradable") and a.get("status") == "active"
            and a.get("class") == "us_equity"
            and a.get("symbol") and "." not in a["symbol"]
            and "/" not in a["symbol"] and len(a["symbol"]) <= 5
        ]
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for a in merged:
            s = a["symbol"]
            if s in seen:
                continue
            seen.add(s)
            out.append({"ticker": s, "name": a.get("name") or s, "exchange": a.get("exchange")})
            if len(out) >= size:
                break
        return out
    except Exception as e:
        logger.warning(f"scanner: Alpaca fallback failed: {e}")
        return []


# ───────────────────────── SPY benchmark ─────────────────────────

_spy_cache: Dict[str, Any] = {"r20": None, "ts": 0.0}


def _spy_r20() -> Optional[float]:
    """SPY 20-day return anchored to last fully-closed bar (iloc[-2]).
    Cached 5min so an intraday close refreshes promptly."""
    now = time.time()
    if now - _spy_cache["ts"] < 300 and _spy_cache["r20"] is not None:
        return _spy_cache["r20"]
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("SPY", "1d")
        if df is None or df.empty or len(df) < 22:
            return None
        r20 = float(df["Close"].iloc[-2] / df["Close"].iloc[-22] - 1)
        _spy_cache.update({"r20": r20, "ts": now})
        return r20
    except Exception as e:
        logger.warning(f"scanner: SPY fetch failed: {e}")
        return None


# ───────────────────────── Earnings filter ─────────────────────────

def _within_earnings_window(ticker: str) -> bool:
    """True when ticker has earnings within the next 48h. Fails CLOSED on
    error: yfinance hiccup → True (excluded) so we never trade INTO an
    earnings print on a flaky calendar."""
    try:
        from services.earnings import inside_earnings_window
        return bool(inside_earnings_window(ticker))
    except Exception as e:
        logger.warning(f"scanner: _within_earnings_window({ticker}) error → fail-closed: {e}")
        return True


# ───────────────────────── Bar-window helpers ─────────────────────────

def _is_partial_bar(df) -> bool:
    """True when df.index[-1] is today's intraday-incomplete bar."""
    try:
        from zoneinfo import ZoneInfo as _ZI
        last_ts = df.index[-1]
        now_et = datetime.utcnow().replace(tzinfo=_ZI("UTC")).astimezone(_ZI("America/New_York"))
        if hasattr(last_ts, "tz_convert"):
            ts_et = last_ts.tz_convert("America/New_York")
        elif hasattr(last_ts, "to_pydatetime"):
            d = last_ts.to_pydatetime()
            if d.tzinfo is None:
                d = d.replace(tzinfo=_ZI("UTC"))
            ts_et = d.astimezone(_ZI("America/New_York"))
        else:
            return False
        return (
            ts_et.date() == now_et.date()
            and (now_et.hour, now_et.minute) < (16, 5)
        )
    except Exception:
        return False


# ───────────────────────── Scoring ─────────────────────────

def _rejected(ticker: str, reason: str, **features) -> Dict[str, Any]:
    """r58 transparency: tag a rejection so run_scan can persist it
    instead of silently dropping the ticker."""
    return {"ticker": ticker, "_rejected": reason, "_features": features}


def score_candidate(ticker: str, spy_r20: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Composite pre-filter score for a ticker. Returns either:
      - {"ticker", "score", ...} on pass
      - {"ticker", "_rejected": "<reason>", "_features": {...}} on reject
      - None on unrecoverable error

    Score (0-100):
      • RVOL          → 25 pts  (today's vol vs 20d avg, capped at 2×)
      • RS vs SPY     → 25 pts  (capped at +5%)
      • % from 52wH   → 25 pts  (within 3% = full; recency-tiered)
      • Stack align   → 15 pts  (price > SMA50 > SMA200)
      • Liquidity     → 10 pts  (ADV > 3× floor)
      Sub-$20 spread haircut: -15%.
    """
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty:
            return _rejected(ticker, "no_ohlcv_data")
        if len(df) < 65:
            return _rejected(ticker, "insufficient_history",
                             bars_available=len(df), bars_required=65)
        df = compute_indicators(df)
        is_partial = _is_partial_bar(df)

        # Anchor on last fully-closed bar to avoid look-ahead leaks.
        last_closed = df.iloc[-2] if is_partial and len(df) >= 2 else df.iloc[-1]
        price = float(last_closed.get("Close", 0) or 0)
        if price < PREFILTER_MIN_PRICE:
            return _rejected(ticker, "price_below_floor",
                             price=round(price, 2), floor=PREFILTER_MIN_PRICE)
        if price > PREFILTER_MAX_PRICE:
            return _rejected(ticker, "price_above_ceiling",
                             price=round(price, 2), ceiling=PREFILTER_MAX_PRICE)

        # Liquidity gates use windows of fully-closed bars.
        if is_partial and len(df) >= 21:
            vol_window = df["Volume"].iloc[-21:-1]
            close_window = df["Close"].iloc[-21:-1]
            hi_window = df["High"].iloc[-min(253, len(df)):-1]
        else:
            vol_window = df["Volume"].iloc[-20:]
            close_window = df["Close"].iloc[-20:]
            hi_window = df["High"].iloc[-min(252, len(df)):]

        import math
        vol_20 = float(vol_window.mean())
        avg_close_20 = float(close_window.mean())
        if not (math.isfinite(vol_20) and math.isfinite(avg_close_20)):
            return _rejected(ticker, "nan_features")
        if vol_20 <= 0 or avg_close_20 <= 0:
            return _rejected(ticker, "non_positive_features",
                             vol_20=vol_20, avg_close_20=avg_close_20)
        dollar_vol_20 = avg_close_20 * vol_20
        if dollar_vol_20 < PREFILTER_MIN_DOLLAR_VOL:
            return _rejected(ticker, "below_dollar_volume",
                             dollar_vol_20=round(dollar_vol_20),
                             floor=PREFILTER_MIN_DOLLAR_VOL)

        # RVOL using yesterday's volume when bar is partial (today's
        # cumulative is incomplete and biases time-of-day).
        ref_vol = float(df["Volume"].iloc[-2]) if is_partial else float(last_closed.get("Volume", 0) or 0)
        rvol = (ref_vol / vol_20) if vol_20 > 0 else 1.0

        # RS vs SPY — use last fully-closed bar for both.
        if len(df) >= 22:
            rs = float(df["Close"].iloc[-2] / df["Close"].iloc[-22] - 1)
            if spy_r20 is not None:
                rs -= spy_r20
        else:
            rs = 0.0

        # 52w high AND low — direction-agnostic scoring needs both.
        # r58 Option B: scanner now picks bullish AND bearish setups; the
        # downstream consider_call_play / consider_put_play decide which
        # direction to trade. Previously the scanner was a pure BUY-side
        # screener so put plays could never source from the pool.
        hi_arr = hi_window.values
        if is_partial and len(df) >= 253:
            lo_window = df["Low"].iloc[-253:-1]
        else:
            lo_window = df["Low"].iloc[-min(252, len(df)):]
        lo_arr = lo_window.values
        hi_52w = float(hi_arr.max()) if len(hi_arr) else 0.0
        lo_52w = float(lo_arr.min()) if len(lo_arr) else 0.0
        pct_from_hi = (price / hi_52w - 1.0) if hi_52w > 0 else 0.0
        # pct_from_low is always >= 0 (price ≥ 52w low). Smaller = closer to low.
        pct_from_low = (price / lo_52w - 1.0) if lo_52w > 0 else 0.0
        try:
            argmax_rev = int(hi_arr[::-1].argmax())
            days_since_hi = max(0, len(hi_arr) - 1 - argmax_rev - argmax_rev)
        except Exception:
            days_since_hi = 999
        try:
            argmin_rev = int(lo_arr[::-1].argmin())
            days_since_lo = max(0, len(lo_arr) - 1 - argmin_rev - argmin_rev)
        except Exception:
            days_since_lo = 999

        adx = float(last_closed.get("ADX_14", last_closed.get("adx", 0)) or 0)
        sma50 = float(last_closed.get("SMA_50", 0) or 0)
        sma200 = float(last_closed.get("SMA_200", 0) or 0)

        # ─── Direction-agnostic scoring (r58 Option B) ───
        # Volume + ADX are always direction-agnostic. RS, extreme-proximity,
        # and stack-alignment switch behavior based on which direction the
        # setup is leaning. We pick the direction that produces the higher
        # composite score, expose `direction` and `dir_score`, plus the
        # winning factor breakdown.
        score = 0.0
        # 1. RVOL — always direction-agnostic (high volume = activity).
        score += min(1.0, rvol / 2.0) * 25

        # 2. RS factor — score by ABS(RS), tag direction.
        # +5% beat or -5% lag both earn full 25 points.
        score += min(1.0, abs(rs) / 0.05) * 25

        # 3. Extreme proximity — score by closeness to EITHER 52w extreme.
        # "Long" candidates press 52w highs; "short" candidates press 52w lows.
        # Pick whichever extreme the price is closer to (in % terms).
        # pct_from_hi is negative (or 0); pct_from_low is positive (or 0).
        # If |pct_from_hi| < pct_from_low → closer to high → bullish setup.
        # Else → closer to low → bearish setup.
        near_high_dist = abs(pct_from_hi)
        near_low_dist = abs(pct_from_low)
        if near_high_dist <= near_low_dist:
            extreme_dir = "long"
            extreme_dist = near_high_dist
            extreme_recency_days = days_since_hi
        else:
            extreme_dir = "short"
            extreme_dist = near_low_dist
            extreme_recency_days = days_since_lo
        # Same scoring tiers, but applied to whichever extreme is closer.
        extreme_pts = 0
        if extreme_dist < 0.03:
            if extreme_recency_days <= 20:
                extreme_pts = 25
            elif extreme_recency_days <= 60:
                extreme_pts = 15
            else:
                extreme_pts = 5
        elif extreme_dist < 0.08:
            extreme_pts = 5
        score += extreme_pts

        # 4. Stack alignment — symmetric. Bullish stack (P>SMA50>SMA200) OR
        # bearish stack (P<SMA50<SMA200) earns 15 pts; partial earns 8.
        if price > sma50 > sma200 > 0:
            score += 15
            stack_dir = "long"
        elif sma200 > sma50 > price > 0:
            score += 15
            stack_dir = "short"
        elif price > sma50 > 0:
            score += 8
            stack_dir = "long"
        elif sma50 > price > 0 and sma50 > 0:
            score += 8
            stack_dir = "short"
        else:
            stack_dir = "neutral"

        # 5. Liquidity bonus — direction-agnostic.
        if dollar_vol_20 > PREFILTER_MIN_DOLLAR_VOL * 5:
            score += 10
        elif dollar_vol_20 > PREFILTER_MIN_DOLLAR_VOL * 2:
            score += 5

        # Spread-drag haircut on cheap names (direction-agnostic).
        if price < 20:
            score *= 0.85

        # Resolve overall direction. RS sign is the strongest signal:
        # rs > 0 favors long, rs < 0 favors short. Use that as primary;
        # break ties via the extreme proximity.
        if rs >= 0.02:
            direction = "long"
        elif rs <= -0.02:
            direction = "short"
        else:
            direction = extreme_dir if extreme_pts > 0 else "neutral"

        # Build human-readable reason tags appropriate to the direction.
        reason_parts = []
        if rvol >= 2.0:    reason_parts.append("RVOL surge")
        elif rvol >= 1.3:  reason_parts.append("rising volume")
        if adx >= 25:      reason_parts.append("trending")
        if direction == "long":
            if rs >= 0.05:     reason_parts.append("outperform +5%")
            elif rs >= 0.02:   reason_parts.append("outperform +2%")
            if pct_from_hi >= -0.03 and days_since_hi <= 20:
                reason_parts.append("fresh 52wH breakout")
            elif pct_from_hi >= -0.03:
                reason_parts.append("near 52wH")
        elif direction == "short":
            if rs <= -0.05:    reason_parts.append("underperform -5%")
            elif rs <= -0.02:  reason_parts.append("underperform -2%")
            if pct_from_low <= 0.03 and days_since_lo <= 20:
                reason_parts.append("fresh 52wL breakdown")
            elif pct_from_low <= 0.03:
                reason_parts.append("near 52wL")
            if stack_dir == "short":
                reason_parts.append("bearish stack")

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "score": round(score, 1),
            "rvol": round(rvol, 2),
            "rs_20d": round(rs, 4),
            "adx": round(adx, 1),
            "pct_from_52w_high": round(pct_from_hi, 4),
            "pct_from_52w_low": round(pct_from_low, 4),
            "direction": direction,  # r58 Option B: long | short | neutral
            "reason": ", ".join(reason_parts) or "composite setup",
        }
    except Exception as e:
        logger.debug(f"scanner score {ticker}: {e}")
        return _rejected(ticker, "scoring_error", error=str(e)[:120])


# ───────────────────────── Pool persistence ─────────────────────────

def run_scan(top_n: Optional[int] = None) -> Dict[str, Any]:
    """Score the universe; persist top-N to candidate_pool atomically.

    Atomicity: DELETE existing pool + INSERT new top-N in a single
    transaction. Readers of `candidate_pool` see old pool until commit,
    then atomically see new pool. No empty-pool window. No advisory
    lock — `max_instances=1` on the cron + Postgres MVCC handles the
    rare concurrent case (worst outcome: one wasted scan, not corruption).
    """
    from database import SessionLocal, CandidatePool, AutoTraderConfig
    from concurrent.futures import ThreadPoolExecutor, wait as _fut_wait

    # r58: capture start time for ScanRun.started_at.
    start_dt = datetime.utcnow()
    start = time.time()

    # Skip on weekends.
    try:
        from zoneinfo import ZoneInfo as _ZI
        now_et = datetime.utcnow().replace(tzinfo=_ZI("UTC")).astimezone(_ZI("America/New_York"))
        if now_et.weekday() >= 5:
            _persist_scan_run(start_dt=start_dt, elapsed_sec=0.0, universe_size=0,
                              scored=0, top_n_size=0, skipped_reason="weekend",
                              rejections=[], top_picks=[])
            return {"scanned": 0, "top_n": 0, "skipped": "weekend"}
    except Exception:
        pass

    # Read top_n.
    db = SessionLocal()
    try:
        cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        if top_n is None:
            top_n = int(getattr(cfg, "universe_top_n", 30) or 30) if cfg else 30
    finally:
        db.close()

    universe = pull_universe()
    if not universe:
        logger.info("scanner: empty universe — skipping")
        _persist_scan_run(start_dt=start_dt, elapsed_sec=time.time() - start,
                          universe_size=0, scored=0, top_n_size=0,
                          skipped_reason="empty_universe", rejections=[], top_picks=[])
        return {"scanned": 0, "top_n": 0}

    spy_r20 = _spy_r20()

    # r58: keep pre-filter snapshot for the rejection log.
    universe_pre_earnings_filter = list(universe)

    # Earnings pre-filter (fail-closed on errors).
    universe = [u for u in universe if not _within_earnings_window(u["ticker"])]

    # Bulk warmup.
    try:
        from services.data_fetcher import fetch_ohlcv_bulk
        fetch_ohlcv_bulk([u["ticker"] for u in universe], timeframe="1d", batch_size=20)
    except Exception as e:
        logger.warning(f"scanner: bulk-warm failed (per-ticker fallback): {e}")

    # Score in parallel (4 workers — post-warmup is GIL-bound).
    # r58 transparency: capture rejections (tagged with `_rejected`)
    # alongside passes (`scored`) so the operator can audit "why didn't
    # AAPL make the pool?".
    SCAN_DEADLINE_SEC = 240.0
    scored: List[Dict[str, Any]] = []
    rejections: List[Dict[str, Any]] = []
    timed_out_tickers: List[str] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="scanscore") as pool:
        future_to_ticker = {
            pool.submit(score_candidate, u["ticker"], spy_r20): u["ticker"]
            for u in universe
        }
        done, not_done = _fut_wait(list(future_to_ticker.keys()), timeout=SCAN_DEADLINE_SEC)
        if not_done:
            logger.warning(f"scanner: {len(not_done)} tickers timed out at {SCAN_DEADLINE_SEC}s")
            for f in not_done:
                timed_out_tickers.append(future_to_ticker.get(f) or "?")
                f.cancel()
        for f in done:
            ticker = future_to_ticker.get(f) or "?"
            try:
                res = f.result(timeout=0)
                if res is None:
                    rejections.append({"ticker": ticker, "reason": "unknown_error", "stage": "score"})
                elif res.get("_rejected"):
                    rejections.append({
                        "ticker": ticker,
                        "reason": res["_rejected"],
                        "stage": "score",
                        "features": res.get("_features", {}),
                    })
                else:
                    scored.append(res)
            except Exception as e:
                rejections.append({"ticker": ticker, "reason": "future_exception", "stage": "score",
                                   "error": str(e)[:120]})

    # Add timed-out tickers to rejections.
    for t in timed_out_tickers:
        rejections.append({"ticker": t, "reason": "scan_timeout", "stage": "score"})

    # r58: pre-existing earnings filter rejections — record them too.
    earnings_rejected_tickers = [u["ticker"] for u in universe_pre_earnings_filter
                                 if u not in universe]
    for t in earnings_rejected_tickers:
        rejections.append({"ticker": t, "reason": "earnings_window", "stage": "earnings_filter"})

    if not scored:
        logger.info("scanner: nothing scored — skipping pool write")
        elapsed_no_scored = time.time() - start
        _persist_scan_run(start_dt=start_dt, elapsed_sec=elapsed_no_scored,
                          universe_size=len(universe), scored=0, top_n_size=0,
                          skipped_reason=None, rejections=rejections, top_picks=[])
        return {"scanned": 0, "top_n": 0}

    scored.sort(key=lambda r: r.get("score") or 0, reverse=True)

    # Top-quintile threshold: don't fill top-N with mediocre setups on
    # quiet days. 80th percentile cut.
    if len(scored) >= 10:
        cut_idx = max(0, int(len(scored) * 0.20) - 1)
        threshold = scored[cut_idx].get("score") or 0
        eligible = []
        for s in scored:
            if (s.get("score") or 0) >= threshold:
                eligible.append(s)
            else:
                rejections.append({
                    "ticker": s["ticker"], "reason": "below_top_quintile",
                    "stage": "selection",
                    "features": {"score": s["score"], "threshold": round(threshold, 1)},
                })
    else:
        eligible = scored
    top = eligible[:top_n]
    # Anyone in eligible but past top_n also rejected.
    for s in eligible[top_n:]:
        rejections.append({
            "ticker": s["ticker"], "reason": "below_top_n",
            "stage": "selection",
            "features": {"score": s["score"], "rank": eligible.index(s) + 1, "top_n": top_n},
        })

    # Atomic swap: delete old pool + insert new in one transaction.
    db = SessionLocal()
    try:
        db.query(CandidatePool).delete(synchronize_session=False)
        name_by_sym = {u["ticker"]: u.get("name") for u in universe}
        for r in top:
            db.add(CandidatePool(
                ticker=r["ticker"],
                name=name_by_sym.get(r["ticker"]) or r["ticker"],
                price=r["price"],
                score=r["score"],
                rvol=r["rvol"],
                rs_20d=r["rs_20d"],
                adx=r["adx"],
                pct_from_52w_high=r["pct_from_52w_high"],
                pct_from_52w_low=r.get("pct_from_52w_low"),
                direction=r.get("direction", "long"),
                reason=r["reason"],
                generation=1,
                pool_source="breakout",
            ))
        db.commit()
    except Exception as e:
        logger.warning(f"scanner: pool swap failed, rolling back: {e}")
        db.rollback()
        raise
    finally:
        db.close()

    elapsed = time.time() - start
    # r58: persist the full ScanRun record for the Transparency UI.
    top_picks_brief = [
        {"ticker": r["ticker"], "score": r["score"], "rvol": r["rvol"],
         "rs_20d": r["rs_20d"], "pct_from_52w_high": r["pct_from_52w_high"],
         "price": r["price"], "reason": r.get("reason")}
        for r in top
    ]
    _persist_scan_run(
        start_dt=start_dt, elapsed_sec=elapsed,
        universe_size=len(universe), scored=len(scored), top_n_size=len(top),
        skipped_reason=None, rejections=rejections, top_picks=top_picks_brief,
    )

    logger.info(
        f"scanner r58: scored {len(scored)}/{len(universe)} in {elapsed:.1f}s; "
        f"top {len(top)} persisted; {len(rejections)} rejections logged"
    )
    return {
        "scanned": len(scored),
        "universe_size": len(universe),
        "top_n": len(top),
        "elapsed_sec": round(elapsed, 1),
        "rejections": len(rejections),
    }


def _persist_scan_run(*, start_dt, elapsed_sec, universe_size, scored, top_n_size,
                      skipped_reason, rejections, top_picks):
    """Persist a ScanRun row with rejections + top_picks JSON blobs."""
    from database import SessionLocal, ScanRun
    db = SessionLocal()
    try:
        db.add(ScanRun(
            started_at=start_dt,
            completed_at=datetime.utcnow(),
            universe_size=universe_size,
            scored=scored,
            top_n_size=top_n_size,
            elapsed_sec=round(elapsed_sec, 1),
            skipped_reason=skipped_reason,
            rejections=json.dumps(rejections) if rejections else None,
            top_picks=json.dumps(top_picks) if top_picks else None,
        ))
        db.commit()
    except Exception as e:
        logger.warning(f"scanner: ScanRun persist failed: {e}")
        db.rollback()
    finally:
        db.close()


def get_candidate_tickers() -> List[str]:
    """Return current pool tickers in score-desc order, excluding blacklist."""
    return [m["ticker"] for m in get_candidate_meta()]


def get_candidate_meta() -> List[Dict[str, Any]]:
    """Return rich metadata for each pool ticker (operator UI + analytics)."""
    from database import SessionLocal, CandidatePool, AutoTraderConfig
    db = SessionLocal()
    try:
        cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        rows = db.query(CandidatePool).order_by(CandidatePool.score.desc()).all()
        bl_csv = (getattr(cfg, "ticker_blacklist", "") or "").upper() if cfg else ""
        blacklist = {s.strip() for s in bl_csv.split(",") if s.strip()}
        out = []
        for r in rows:
            if r.ticker.upper() in blacklist:
                continue
            out.append({
                "ticker": r.ticker,
                "score": r.score,
                "rvol": r.rvol,
                "rs_20d": r.rs_20d,
                "adx": r.adx,
                "pct_from_52w_high": r.pct_from_52w_high,
                "last_evaluated_at": r.last_evaluated_at,
            })
        return out
    finally:
        db.close()


# ───────────────────────── Event detection ─────────────────────────

_DWELL_MIN = {"GAP": 60, "RVOL_SURGE": 30, "SQUEEZE_RELEASE": 60}
_TTL_MIN = {"GAP": 30, "RVOL_SURGE": 20, "SQUEEZE_RELEASE": 60}


def _detect_gap(df) -> Optional[Dict[str, Any]]:
    """GAP: today's open vs prev close exceeds 2× ATR-20."""
    if df is None or df.empty or len(df) < 22:
        return None
    try:
        prev_close = float(df["Close"].iloc[-2])
        today_open = float(df["Open"].iloc[-1])
        if prev_close <= 0:
            return None
        gap = (today_open / prev_close) - 1.0
        rets = df["Close"].pct_change().iloc[-20:]
        atr = float(rets.std()) if len(rets) > 1 else 0.0
        if atr <= 0 or abs(gap) / atr < 2.0:
            return None
        side = "long" if gap > 0 else "short"
        return {
            "score": min(100.0, 50.0 * abs(gap) / atr),
            "side": side,
            "features": {"gap_pct": gap, "atr": atr, "prev_close": prev_close, "today_open": today_open},
        }
    except Exception:
        return None


def _detect_rvol_surge(df) -> Optional[Dict[str, Any]]:
    """RVOL_SURGE: today's volume ≥ 2× 20d avg, with directional bias from
    the last fully-closed bar."""
    if df is None or df.empty or len(df) < 22:
        return None
    try:
        today_v = float(df["Volume"].iloc[-1])
        avg_v = float(df["Volume"].iloc[-21:-1].mean())
        if avg_v <= 0:
            return None
        rvol = today_v / avg_v
        if rvol < 2.0:
            return None
        last2 = df["Close"].iloc[-2:]
        side = "long" if float(last2.iloc[-1]) >= float(last2.iloc[0]) else "short"
        return {
            "score": min(100.0, 30.0 * rvol),
            "side": side,
            "features": {"rvol": rvol, "today_vol": today_v, "avg_20d_vol": avg_v},
        }
    except Exception:
        return None


def _detect_squeeze_release(df) -> Optional[Dict[str, Any]]:
    """SQUEEZE_RELEASE: BB-width has expanded ≥1.4× a compressed prior
    baseline, with RVOL ≥ 1.3 confirmation."""
    if df is None or df.empty or len(df) < 60:
        return None
    try:
        closes = df["Close"].astype(float)
        cur = closes.iloc[-20:]
        prior = closes.iloc[-40:-20]
        cur_mean = float(cur.mean()); cur_std = float(cur.std(ddof=0))
        prior_mean = float(prior.mean()); prior_std = float(prior.std(ddof=0))
        bw_cur = (4.0 * cur_std / cur_mean) if cur_mean > 0 else 0.0
        bw_prior = (4.0 * prior_std / prior_mean) if prior_mean > 0 else 0.0
        if bw_prior <= 0 or bw_cur / bw_prior < 1.4:
            return None
        today_v = float(df["Volume"].iloc[-1])
        avg_v = float(df["Volume"].iloc[-21:-1].mean())
        if avg_v <= 0 or today_v / avg_v < 1.3:
            return None
        return {
            "score": min(100.0, 25.0 * bw_cur / bw_prior),
            "side": "long",  # squeeze releases tend up; reversal handled separately
            "features": {"bb_width": bw_cur, "bb_width_prior": bw_prior, "expansion_ratio": bw_cur / bw_prior},
        }
    except Exception:
        return None


_DETECTORS = {
    "GAP": _detect_gap,
    "RVOL_SURGE": _detect_rvol_surge,
    "SQUEEZE_RELEASE": _detect_squeeze_release,
}


def _recently_emitted(db, ticker: str, kind: str) -> bool:
    from database import CandidateEvent
    cutoff = datetime.utcnow() - timedelta(minutes=_DWELL_MIN.get(kind, 30))
    return db.query(CandidateEvent).filter(
        CandidateEvent.kind == kind,
        CandidateEvent.ticker == ticker,
        CandidateEvent.event_at >= cutoff,
    ).first() is not None


def _emit(db, ticker: str, kind: str, res: Dict[str, Any]) -> None:
    from database import CandidateEvent
    ttl_min = _TTL_MIN.get(kind, 30)
    db.add(CandidateEvent(
        kind=kind,
        ticker=ticker,
        event_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=ttl_min),
        score=round(res["score"], 1),
        features=json.dumps({
            "side": res["side"],
            **{k: (round(v, 6) if isinstance(v, (int, float)) else v)
               for k, v in res.get("features", {}).items()},
        }),
    ))


def detect_events(top_k: int = 50) -> Dict[str, Any]:
    """Sweep the candidate pool's top-K for setup events. Writes to
    candidate_events. Called every 2 min during RTH by the cron."""
    from database import SessionLocal
    from services.data_fetcher import fetch_ohlcv
    pool = get_candidate_meta()[:top_k]
    if not pool:
        return {"events_emitted": 0, "tickers_scanned": 0, "elapsed_sec": 0.0}
    counts: Dict[str, int] = {}
    start = time.time()
    db = SessionLocal()
    try:
        for entry in pool:
            ticker = entry["ticker"]
            try:
                df = fetch_ohlcv(ticker, "1d")
                for kind, fn in _DETECTORS.items():
                    if _recently_emitted(db, ticker, kind):
                        continue
                    res = fn(df)
                    if res is None:
                        continue
                    _emit(db, ticker, kind, res)
                    counts[kind] = counts.get(kind, 0) + 1
            except Exception as e:
                logger.debug(f"scanner.detect_events: {ticker} skipped: {e}")
                continue
        db.commit()
    except Exception as e:
        logger.warning(f"scanner.detect_events: commit failed: {e}")
        db.rollback()
    finally:
        db.close()
    elapsed = time.time() - start
    total = sum(counts.values())
    if total:
        logger.info(f"scanner.detect_events: scanned {len(pool)} in {elapsed:.1f}s; emitted {total} ({counts})")
    return {
        "tickers_scanned": len(pool),
        "events_emitted": total,
        "by_kind": counts,
        "elapsed_sec": round(elapsed, 1),
    }


def get_active_events(max_age_min: int = 30) -> List[Dict[str, Any]]:
    """Return live (non-expired, non-consumed) events for consider_event."""
    from database import SessionLocal, CandidateEvent
    cutoff = datetime.utcnow() - timedelta(minutes=max_age_min)
    db = SessionLocal()
    try:
        rows = (
            db.query(CandidateEvent)
            .filter(CandidateEvent.consumed_at.is_(None))
            .filter(CandidateEvent.event_at >= cutoff)
            .filter(
                (CandidateEvent.expires_at.is_(None))
                | (CandidateEvent.expires_at >= datetime.utcnow())
            )
            .order_by(CandidateEvent.score.desc())
            .all()
        )
        return [{
            "id": r.id,
            "kind": r.kind,
            "ticker": r.ticker,
            "score": r.score,
            "event_at": r.event_at,
            "expires_at": r.expires_at,
            "features": json.loads(r.features) if r.features else {},
        } for r in rows]
    finally:
        db.close()


def mark_consumed(event_id: int, decision: str, reason: Optional[str] = None) -> None:
    """Mark an event as acted-on so the next detect_events tick won't re-emit
    and consider_event won't re-process it."""
    from database import SessionLocal, CandidateEvent
    db = SessionLocal()
    try:
        row = db.query(CandidateEvent).filter(CandidateEvent.id == event_id).first()
        if row is None:
            return
        row.consumed_at = datetime.utcnow()
        row.consumed_decision = decision
        row.consumed_reason = reason
        db.commit()
    except Exception as e:
        logger.warning(f"scanner.mark_consumed({event_id}) failed: {e}")
        db.rollback()
    finally:
        db.close()
