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

def score_candidate(ticker: str, spy_r20: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Composite pre-filter score for a ticker. Returns dict with score
    and feature breakdown, or None if ticker fails liquidity / data gates.

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
        if df is None or df.empty or len(df) < 65:
            return None
        df = compute_indicators(df)
        is_partial = _is_partial_bar(df)

        # Anchor on last fully-closed bar to avoid look-ahead leaks.
        last_closed = df.iloc[-2] if is_partial and len(df) >= 2 else df.iloc[-1]
        price = float(last_closed.get("Close", 0) or 0)
        if not (PREFILTER_MIN_PRICE <= price <= PREFILTER_MAX_PRICE):
            return None

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
            return None
        if vol_20 <= 0 or avg_close_20 <= 0:
            return None
        dollar_vol_20 = avg_close_20 * vol_20
        if dollar_vol_20 < PREFILTER_MIN_DOLLAR_VOL:
            return None

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

        # 52w-high recency.
        hi_arr = hi_window.values
        hi_52w = float(hi_arr.max()) if len(hi_arr) else 0.0
        pct_from_hi = (price / hi_52w - 1.0) if hi_52w > 0 else 0.0
        try:
            argmax_rev = int(hi_arr[::-1].argmax())
            days_since_hi = max(0, len(hi_arr) - 1 - argmax_rev - argmax_rev)
        except Exception:
            days_since_hi = 999

        adx = float(last_closed.get("ADX_14", last_closed.get("adx", 0)) or 0)
        sma50 = float(last_closed.get("SMA_50", 0) or 0)
        sma200 = float(last_closed.get("SMA_200", 0) or 0)

        # Compose
        score = 0.0
        score += min(1.0, rvol / 2.0) * 25
        if rs > 0:
            score += min(1.0, rs / 0.05) * 25
        if pct_from_hi > -0.03:
            if days_since_hi <= 20:
                score += 25
            elif days_since_hi <= 60:
                score += 15
            else:
                score += 5
        elif pct_from_hi > -0.08:
            score += 5
        if price > sma50 > sma200 > 0:
            score += 15
        elif price > sma50 > 0:
            score += 8
        if dollar_vol_20 > PREFILTER_MIN_DOLLAR_VOL * 5:
            score += 10
        elif dollar_vol_20 > PREFILTER_MIN_DOLLAR_VOL * 2:
            score += 5
        # Spread-drag haircut on cheap names.
        if price < 20:
            score *= 0.85

        reason_parts = []
        if rvol >= 2.0:    reason_parts.append("RVOL surge")
        elif rvol >= 1.3:  reason_parts.append("rising volume")
        if adx >= 25:      reason_parts.append("trending")
        if rs >= 0.05:     reason_parts.append("outperform +5%")
        elif rs >= 0.02:   reason_parts.append("outperform +2%")
        if pct_from_hi >= -0.03 and days_since_hi <= 20:
            reason_parts.append("fresh 52wH breakout")

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "score": round(score, 1),
            "rvol": round(rvol, 2),
            "rs_20d": round(rs, 4),
            "adx": round(adx, 1),
            "pct_from_52w_high": round(pct_from_hi, 4),
            "reason": ", ".join(reason_parts) or "composite setup",
        }
    except Exception as e:
        logger.debug(f"scanner score {ticker}: {e}")
        return None


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

    # Skip on weekends.
    try:
        from zoneinfo import ZoneInfo as _ZI
        now_et = datetime.utcnow().replace(tzinfo=_ZI("UTC")).astimezone(_ZI("America/New_York"))
        if now_et.weekday() >= 5:
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
        return {"scanned": 0, "top_n": 0}

    spy_r20 = _spy_r20()
    start = time.time()

    # Earnings pre-filter (fail-closed on errors).
    universe = [u for u in universe if not _within_earnings_window(u["ticker"])]

    # Bulk warmup.
    try:
        from services.data_fetcher import fetch_ohlcv_bulk
        fetch_ohlcv_bulk([u["ticker"] for u in universe], timeframe="1d", batch_size=20)
    except Exception as e:
        logger.warning(f"scanner: bulk-warm failed (per-ticker fallback): {e}")

    # Score in parallel (4 workers — post-warmup is GIL-bound).
    SCAN_DEADLINE_SEC = 240.0
    scored = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="scanscore") as pool:
        futures = [pool.submit(score_candidate, u["ticker"], spy_r20) for u in universe]
        done, not_done = _fut_wait(futures, timeout=SCAN_DEADLINE_SEC)
        if not_done:
            logger.warning(f"scanner: {len(not_done)} tickers timed out at {SCAN_DEADLINE_SEC}s")
            for f in not_done:
                f.cancel()
        for f in done:
            try:
                res = f.result(timeout=0)
                if res:
                    scored.append(res)
            except Exception:
                continue

    if not scored:
        logger.info("scanner: nothing scored — skipping pool write")
        return {"scanned": 0, "top_n": 0}

    scored.sort(key=lambda r: r.get("score") or 0, reverse=True)

    # Top-quintile threshold: don't fill top-N with mediocre setups on
    # quiet days. 80th percentile cut.
    if len(scored) >= 10:
        cut_idx = max(0, int(len(scored) * 0.20) - 1)
        threshold = scored[cut_idx].get("score") or 0
        eligible = [s for s in scored if (s.get("score") or 0) >= threshold]
    else:
        eligible = scored
    top = eligible[:top_n]

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
    logger.info(
        f"scanner r57: scored {len(scored)}/{len(universe)} in {elapsed:.1f}s; "
        f"top {len(top)} persisted"
    )
    return {
        "scanned": len(scored),
        "universe_size": len(universe),
        "top_n": len(top),
        "elapsed_sec": round(elapsed, 1),
    }


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
