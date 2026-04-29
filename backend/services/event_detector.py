"""r56 Tier-3 Option B: event-driven candidate detector.

Replaces the cron-based "score 500, take top-N" universe-scanner model
with an event-shaped detector: continuously (every 1-2 min during RTH)
scan the universe for discrete setup events — GAP, RVOL_SURGE,
NEW_HIGH, SQUEEZE_RELEASE, PEAD, BREAKDOWN — and emit a CandidateEvent
row when any fires. Threshold-based, not top-N: quiet days produce 0-3
events, active days 30-100. The pre-existing concurrent-position /
correlation / book-VAR caps in auto_trader throttle from the consumer
side.

Cadence:
  - Universe rebuild: nightly via universe_scanner.run_scan() (existing).
  - Event detection: every 1-2 min during RTH via this module.
  - Event consumption: auto_trader.consider_event() (called from the
    scheduled_scan loop).

Compared to universe_scanner:
  - Drops cross-sectional z-score / shrinkage / regime weights / TOD
    profiles entirely. Each event kind has its own threshold-based check.
  - Each kind tags pool_source so downstream strategy can adapt
    (PEAD holds longer, GAP uses tighter stops, etc.).
  - Liveness via expires_at: stale events (>30 min old) skip on consume.
  - Coexists with universe_scanner during the transition; both populate
    decision-tier inputs to auto_trader.

Phase 1 (this revision): GAP, RVOL_SURGE, SQUEEZE_RELEASE detectors —
the three most operationally-tractable kinds with daily-bar data. PEAD
and NEW_HIGH/BREAKDOWN follow once minute-bar streaming is wired (r57+).
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-kind dwell windows: don't re-emit the same (kind, ticker) within N min.
_DWELL_MIN = {
    "GAP":             60,    # gap is a once-a-day event
    "RVOL_SURGE":      30,    # vol surges can re-fire on second leg
    "SQUEEZE_RELEASE": 60,    # band-width expansion is multi-bar
    "NEW_HIGH":        45,
    "PEAD":         24 * 60,  # PEAD is a daily-cadence event
    "BREAKDOWN":       45,
}

# TTL: how long an event remains actionable before staleness.
_EVENT_TTL_MIN = {
    "GAP":             30,
    "RVOL_SURGE":      20,
    "SQUEEZE_RELEASE": 60,
    "NEW_HIGH":        30,
    "PEAD":         5 * 60,
    "BREAKDOWN":       30,
}


def _recently_emitted(db, ticker: str, kind: str) -> bool:
    """True if (kind, ticker) was emitted within the dwell window."""
    from database import CandidateEvent
    dwell = timedelta(minutes=_DWELL_MIN.get(kind, 30))
    cutoff = datetime.utcnow() - dwell
    row = (
        db.query(CandidateEvent)
        .filter(CandidateEvent.kind == kind)
        .filter(CandidateEvent.ticker == ticker)
        .filter(CandidateEvent.event_at >= cutoff)
        .first()
    )
    return row is not None


def _emit(db, ticker: str, kind: str, score: float, features: Dict[str, Any]) -> None:
    """Append a CandidateEvent row. Does NOT commit (caller batches)."""
    from database import CandidateEvent
    ttl = _EVENT_TTL_MIN.get(kind, 30)
    db.add(CandidateEvent(
        kind=kind,
        ticker=ticker,
        event_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=ttl),
        score=round(score, 1),
        features=json.dumps({k: (round(v, 4) if isinstance(v, (int, float)) else v)
                             for k, v in features.items()}),
    ))


def _detect_gap(df, ticker: str) -> Optional[Dict[str, Any]]:
    """GAP event: today's open vs prev close exceeds 2× ATR-20.

    Requires daily bar with the partial-day open already known. Detects
    early-RTH gap setups within the first 30-60 minutes after open.
    """
    if df is None or df.empty or len(df) < 22:
        return None
    try:
        prev_close = float(df["Close"].iloc[-2])
        today_open = float(df["Open"].iloc[-1])
        if prev_close <= 0:
            return None
        gap_pct = (today_open / prev_close) - 1.0
        # ATR proxy: stdev of daily returns last 20 bars.
        rets = df["Close"].pct_change().iloc[-20:]
        atr = float(rets.std()) if len(rets) > 1 else 0.01
        if atr <= 0:
            return None
        gap_in_atrs = abs(gap_pct) / atr
        if gap_in_atrs < 2.0:
            return None
        score = min(100.0, 50.0 * gap_in_atrs)
        return {
            "score": score,
            "features": {"gap_pct": gap_pct, "atr_20": atr,
                         "gap_in_atrs": gap_in_atrs, "prev_close": prev_close,
                         "today_open": today_open},
        }
    except Exception:
        return None


def _detect_rvol_surge(df, ticker: str) -> Optional[Dict[str, Any]]:
    """RVOL_SURGE event: today's volume already ≥ 2× 20d-avg by current
    time-of-session.

    Cheap proxy — uses daily bar volume vs 20d average. A more accurate
    detector would use intraday volume vs same-time-of-day average; that's
    a phase-2 improvement requiring 1m streaming.
    """
    if df is None or df.empty or len(df) < 22:
        return None
    try:
        today_vol = float(df["Volume"].iloc[-1])
        avg_20 = float(df["Volume"].iloc[-21:-1].mean())
        if avg_20 <= 0:
            return None
        rvol = today_vol / avg_20
        if rvol < 2.0:
            return None
        score = min(100.0, 30.0 * rvol)
        return {
            "score": score,
            "features": {"rvol_today": rvol, "today_vol": today_vol, "avg_20d_vol": avg_20},
        }
    except Exception:
        return None


def _detect_squeeze_release(df, ticker: str) -> Optional[Dict[str, Any]]:
    """SQUEEZE_RELEASE event: BB-width has expanded ≥1.4× a compressed
    prior baseline AND today's RVOL ≥ 1.3.

    "Compressed prior" = prior BB-width below the 30th percentile of the
    rolling 60-day BB-width distribution.
    """
    if df is None or df.empty or len(df) < 60:
        return None
    try:
        closes = df["Close"].astype(float)
        # Current 20d BB-width
        cur = closes.iloc[-20:]
        cur_mean = float(cur.mean())
        cur_std = float(cur.std(ddof=0))
        bb_width = (4.0 * cur_std / cur_mean) if cur_mean > 0 else 0.0
        # Prior 20d BB-width (bars t-40 .. t-20)
        prior = closes.iloc[-40:-20]
        prior_mean = float(prior.mean())
        prior_std = float(prior.std(ddof=0))
        bb_width_prior = (4.0 * prior_std / prior_mean) if prior_mean > 0 else 0.0
        if bb_width_prior <= 0 or bb_width <= 0:
            return None
        # Was prior compressed? Compare to 60d distribution.
        bb_widths_60 = []
        for i in range(40, 60):
            window = closes.iloc[-i:-(i - 20)] if i > 20 else closes.iloc[-i:]
            if len(window) < 20:
                continue
            m = float(window.mean()); s = float(window.std(ddof=0))
            bb_widths_60.append((4.0 * s / m) if m > 0 else 0.0)
        if not bb_widths_60:
            return None
        bb_widths_60.sort()
        p30 = bb_widths_60[int(len(bb_widths_60) * 0.30)]
        if bb_width_prior > p30:
            return None  # prior wasn't compressed
        # Expansion check
        expansion_ratio = bb_width / bb_width_prior
        if expansion_ratio < 1.4:
            return None
        # RVOL confirmation
        today_vol = float(df["Volume"].iloc[-1])
        avg_20 = float(df["Volume"].iloc[-21:-1].mean())
        rvol = (today_vol / avg_20) if avg_20 > 0 else 0.0
        if rvol < 1.3:
            return None
        score = min(100.0, 25.0 * expansion_ratio + 20.0 * (rvol - 1.3))
        return {
            "score": score,
            "features": {"bb_width": bb_width, "bb_width_prior": bb_width_prior,
                         "expansion_ratio": expansion_ratio, "rvol": rvol},
        }
    except Exception:
        return None


_DETECTORS = {
    "GAP":             _detect_gap,
    "RVOL_SURGE":      _detect_rvol_surge,
    "SQUEEZE_RELEASE": _detect_squeeze_release,
}


def detect_events(top_k: int = 50) -> Dict[str, Any]:
    """Sweep the candidate pool's top-K tickers for setup events. Writes
    detected events to candidate_events; consumer (auto_trader) reads
    fresh events on its next pass.

    `top_k` bounds how many tickers we scan per cycle — set conservatively
    because this runs every 1-2 min. Defaults to top 50 by composite score
    from the legacy candidate_pool. Future: rebuild from a Russell 1000
    file with intraday-streaming scoring.
    """
    from database import SessionLocal
    from services.universe_scanner import get_candidate_meta
    from services.data_fetcher import fetch_ohlcv
    start = time.time()
    pool = get_candidate_meta()[:top_k]
    if not pool:
        logger.info("event_detector: candidate pool empty — nothing to scan")
        return {"events_emitted": 0, "tickers_scanned": 0, "elapsed_sec": 0.0}
    counts: Dict[str, int] = {}
    db = SessionLocal()
    try:
        for entry in pool:
            ticker = entry["ticker"]
            try:
                df = fetch_ohlcv(ticker, "1d")
                for kind, fn in _DETECTORS.items():
                    if _recently_emitted(db, ticker, kind):
                        continue
                    res = fn(df, ticker)
                    if res is None:
                        continue
                    _emit(db, ticker, kind, res["score"], res["features"])
                    counts[kind] = counts.get(kind, 0) + 1
            except Exception as e:
                logger.debug(f"event_detector: {ticker} skipped: {e}")
                continue
        db.commit()
    except Exception as e:
        logger.warning(f"event_detector: commit failed, rolling back: {e}")
        db.rollback()
    finally:
        db.close()
    elapsed = time.time() - start
    total = sum(counts.values())
    logger.info(
        f"event_detector: scanned {len(pool)} tickers in {elapsed:.1f}s; "
        f"emitted {total} events ({counts})"
    )
    return {
        "tickers_scanned": len(pool),
        "events_emitted": total,
        "by_kind": counts,
        "elapsed_sec": round(elapsed, 1),
    }


def get_active_events(max_age_min: int = 30) -> List[Dict[str, Any]]:
    """Return live (non-expired, non-consumed) events for the consumer.

    Used by the auto_trader's scheduled_scan to prioritize event-driven
    candidates over pool-rank candidates. Sorted by score desc.
    """
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
    """Mark an event as acted-on. Called by auto_trader.consider_event."""
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
        logger.warning(f"mark_consumed({event_id}) failed: {e}")
        db.rollback()
    finally:
        db.close()
