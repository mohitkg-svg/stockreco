"""r68-C: gate-outcome hindsight telemetry.

Forcing function for the audit's central recommendation: every gate must
justify itself with measured outcomes. For each rejected DecisionLog row
older than 5 trading days, fetch forward bars and compute the realized P&L
the trade WOULD have produced over a 5-day horizon. Aggregate per `reason`
to identify gates that systematically filter winners (positive mean PnL on
their rejected signals).

Hindsight P&L formula
---------------------
For a signal with (entry, stop_loss, target1):
  Walk forward up to 5 trading days from decision time using daily bars.
  - If High[i] >= target1 BEFORE Low[i] <= stop_loss, exit at target1.
  - If Low[i]  <= stop_loss BEFORE High[i] >= target1, exit at stop_loss.
  - If neither hit by day 5, exit at Close[5].
  Return = (exit - entry) / entry × 100, in percent.

Aggregation
-----------
GET /api/auto/gate-outcomes returns: per gate name, n_rejected, mean_pnl_pct,
median_pnl_pct, win_rate. Operator reads "if mean > 0, this gate cost money
on its rejected signals — consider deletion."

Schedule
--------
Runs nightly at 04:00 UTC. Walks DecisionLog where:
  - decision = "skipped"
  - reason   = (any non-null)
  - hindsight_computed_at IS NULL
  - ts >= 7 days ago AND ts <= 5 days ago (mature enough)
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_HORIZON_TRADING_DAYS = 5


def _hindsight_pnl_pct(ticker: str, decision_ts: datetime, entry: float,
                        stop: Optional[float], target1: Optional[float]) -> Optional[float]:
    """Walk forward up to 5 trading days. Returns realized % return if the
    trade had been entered at `entry` with `stop` and `target1`. Stop-then-
    target priority intra-day is the standard worst-case assumption."""
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(ticker.upper(), "1d")
        if df is None or df.empty:
            return None
        # Find the bar at-or-after decision_ts. Use date-only since 1d bars.
        decision_date = decision_ts.date()
        # Filter to bars strictly AFTER the decision date (T+1 onward).
        try:
            mask = df.index.date > decision_date
        except Exception:
            return None
        forward = df[mask].head(_HORIZON_TRADING_DAYS)
        if forward.empty:
            return None
        for _, bar in forward.iterrows():
            try:
                hi = float(bar["High"])
                lo = float(bar["Low"])
                cl = float(bar["Close"])
            except Exception:
                continue
            if stop is not None and lo <= stop:
                # Worst-case priority: stop hit first.
                return (stop - entry) / entry * 100.0
            if target1 is not None and hi >= target1:
                return (target1 - entry) / entry * 100.0
        # No level hit — exit at last close.
        try:
            last_close = float(forward.iloc[-1]["Close"])
            return (last_close - entry) / entry * 100.0
        except Exception:
            return None
    except Exception as e:
        logger.debug(f"hindsight pnl {ticker}: {e}")
        return None


def recompute(max_rows: int = 500) -> Dict:
    """Walk DecisionLog rows that are mature (>=5 trading days old, <7d for
    bound) and have no hindsight yet. Compute, persist, return summary."""
    from database import SessionLocal, DecisionLog
    db = SessionLocal()
    try:
        # Bound: ts between 7d and 5d ago. 5d gives forward bars; 7d caps
        # backfill cost (older rows can be backfilled separately if needed).
        upper = datetime.utcnow() - timedelta(days=5)
        lower = datetime.utcnow() - timedelta(days=14)
        rows = (
            db.query(DecisionLog)
            .filter(
                DecisionLog.decision == "skipped",
                DecisionLog.reason.isnot(None),
                DecisionLog.hindsight_computed_at.is_(None),
                DecisionLog.ts >= lower,
                DecisionLog.ts <= upper,
                DecisionLog.sig_entry.isnot(None),
            )
            .order_by(DecisionLog.ts.desc())
            .limit(max_rows)
            .all()
        )
        n_done = 0
        n_skipped = 0
        for r in rows:
            try:
                entry = float(r.sig_entry) if r.sig_entry else None
                if not entry or entry <= 0:
                    n_skipped += 1
                    continue
                stop = float(r.sig_stop) if r.sig_stop else None
                t1 = float(r.sig_target1) if r.sig_target1 else None
                pnl = _hindsight_pnl_pct(r.ticker, r.ts, entry, stop, t1)
                r.hindsight_computed_at = datetime.utcnow()
                if pnl is not None:
                    r.hindsight_pnl_5d_pct = round(float(pnl), 4)
                    n_done += 1
                else:
                    n_skipped += 1
            except Exception as _re:
                logger.debug(f"hindsight row {r.id}: {_re}")
                n_skipped += 1
        db.commit()
        logger.info(f"gate_telemetry: computed {n_done} hindsight, skipped {n_skipped}")
        return {"computed": n_done, "skipped": n_skipped, "examined": len(rows)}
    finally:
        db.close()


def aggregate_by_gate(days: int = 30) -> List[Dict]:
    """Per-gate aggregation: n, mean/median pnl, win rate. Operator's deletion
    decision tool — gates with mean > 0 over n>=10 are filtering winners."""
    from database import SessionLocal, DecisionLog
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        rows = (
            db.query(DecisionLog)
            .filter(
                DecisionLog.decision == "skipped",
                DecisionLog.reason.isnot(None),
                DecisionLog.hindsight_pnl_5d_pct.isnot(None),
                DecisionLog.ts >= cutoff,
            )
            .all()
        )
        bucket: Dict[str, List[float]] = {}
        for r in rows:
            bucket.setdefault(r.reason, []).append(float(r.hindsight_pnl_5d_pct))
        out: List[Dict] = []
        for reason, pnls in bucket.items():
            n = len(pnls)
            if n == 0:
                continue
            mean = sum(pnls) / n
            srt = sorted(pnls)
            mid = n // 2
            median = (srt[mid - 1] + srt[mid]) / 2 if (n % 2 == 0) else srt[mid]
            win_rate = sum(1 for p in pnls if p > 0) / n
            out.append({
                "reason": reason,
                "n": n,
                "mean_pnl_pct": round(mean, 3),
                "median_pnl_pct": round(median, 3),
                "win_rate": round(win_rate, 3),
                "verdict": "deletion_candidate" if (n >= 10 and mean > 0) else (
                    "marginal" if (n >= 10 and abs(mean) < 0.5) else "ok"),
            })
        out.sort(key=lambda d: (-d["mean_pnl_pct"], -d["n"]))
        return out
    finally:
        db.close()
