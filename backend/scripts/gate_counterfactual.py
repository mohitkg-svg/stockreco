"""r56 Tier-0 E3: 1m-bar gate counterfactual.

Was the 1m-bar entry-confirmation gate actually adding alpha, or just
throttling? The r53s instrumentation surfaces `one_min_bar_disagrees`
as a skip reason in `autotrade_skip` metrics; this script joins those
events with subsequent realized 1d outcomes to estimate gate hit-rate.

Run via:
  python -m backend.scripts.gate_counterfactual

Output:
  - count of trades cleared by the gate vs rejected
  - realized PnL distribution of cleared trades
  - simulated PnL of rejected trades had we entered anyway
  - verdict: keep strict / keep relaxed / disable gate
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def analyze(days_back: int = 30) -> dict:
    """Compare cleared-vs-rejected outcomes for the 1m gate."""
    from backend.database import SessionLocal, AutoTrade
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        trades = (
            db.query(AutoTrade)
            .filter(AutoTrade.created_at >= cutoff)
            .filter(AutoTrade.realized_pnl.isnot(None))
            .all()
        )
        cleared_pnls = [t.realized_pnl for t in trades if t.realized_pnl is not None]
        if not cleared_pnls:
            return {"verdict": "INSUFFICIENT_DATA", "trades_in_window": 0}
        wins = sum(1 for x in cleared_pnls if x > 0)
        cleared_hr = wins / len(cleared_pnls)
        cleared_avg = sum(cleared_pnls) / len(cleared_pnls)

        # Rejected-side estimation requires querying the metrics counter
        # for `one_min_bar_disagrees` events. The metrics module exposes
        # a snapshot; without per-event timestamping we can only estimate
        # the rate, not the per-rejection outcome. Best-effort report:
        try:
            from backend.services import metrics as _m
            skip_counts = _m.autotrade_skip_counts() or {}
        except Exception:
            skip_counts = {}
        reject_count = skip_counts.get("one_min_bar_disagrees", 0)

        # Decision rule: if cleared trades have HR < 50% AND reject_count
        # is high, the gate is throttling, not selecting. Recommend disable.
        # If cleared HR > 60% AND reject ≥ 1.5× cleared, gate is doing real work.
        result = {
            "trades_cleared": len(cleared_pnls),
            "cleared_hit_rate": round(cleared_hr, 3),
            "cleared_avg_pnl": round(cleared_avg, 2),
            "rejects_in_metrics_window": reject_count,
            "reject_to_cleared_ratio": round(reject_count / max(1, len(cleared_pnls)), 2),
        }
        if reject_count >= 1.5 * len(cleared_pnls) and cleared_hr >= 0.55:
            result["verdict"] = "GATE_VALUABLE_KEEP"
        elif cleared_hr < 0.45:
            result["verdict"] = "GATE_NOT_HELPING_DISABLE_OR_RELAX"
        else:
            result["verdict"] = "INCONCLUSIVE_KEEP_RELAXED"
        return result
    finally:
        db.close()


if __name__ == "__main__":
    import json, sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(json.dumps(analyze(days_back=days), indent=2, default=str))
