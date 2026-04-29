"""r56 Tier-0 E3: close the score_v2 shadow loop.

Compares legacy `score` vs new `score_v2` rankings against realized
trade outcomes. The shadow column has been populated since r54 but no
analysis script exists — the third audit flagged this as the load-bearing
validation gap.

Run via:
  python -m backend.scripts.analyze_score_divergence

Output:
  - per-day top-N divergence (score_top_N vs score_v2_top_N intersection size)
  - hit-rate of trades sourced from score-only top-N vs v2-only top-N
  - decision: should universe_scoring_v2 be flipped to "active"?
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def analyze(days_back: int = 30, top_n: int = 30) -> dict:
    """Compare score vs score_v2 over the last `days_back` days of pool snapshots."""
    from backend.database import SessionLocal, CandidatePool, AutoTrade
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        # Group pool rows by generation; each generation is one scan snapshot.
        rows = (
            db.query(CandidatePool)
            .filter(CandidatePool.generated_at >= cutoff)
            .all()
        )
        by_gen: dict = defaultdict(list)
        for r in rows:
            by_gen[r.generation].append(r)
        if not by_gen:
            log.warning(f"no candidate_pool rows in last {days_back}d")
            return {"snapshots": 0}
        # For each scan: top-N by score vs top-N by score_v2; measure overlap
        overlap_pcts = []
        score_only = set()
        v2_only = set()
        for gen, gen_rows in sorted(by_gen.items()):
            valid_v2 = [r for r in gen_rows if r.score_v2 is not None]
            if len(valid_v2) < 5:
                continue
            top_score   = set(r.ticker for r in sorted(gen_rows, key=lambda r: r.score or 0, reverse=True)[:top_n])
            top_v2      = set(r.ticker for r in sorted(valid_v2, key=lambda r: r.score_v2 or 0, reverse=True)[:top_n])
            inter = top_score & top_v2
            overlap_pcts.append(100.0 * len(inter) / max(1, len(top_score)))
            score_only |= (top_score - top_v2)
            v2_only    |= (top_v2 - top_score)
        # Hit-rate of trades whose ticker appeared in only-one-ranking
        trades = (
            db.query(AutoTrade)
            .filter(AutoTrade.entry_at >= cutoff if hasattr(AutoTrade, "entry_at") else AutoTrade.created_at >= cutoff)
            .filter(AutoTrade.realized_pnl.isnot(None))
            .all()
        )
        score_only_pnls = [t.realized_pnl for t in trades if t.ticker in score_only]
        v2_only_pnls    = [t.realized_pnl for t in trades if t.ticker in v2_only]

        def _hr(xs):
            xs = [x for x in xs if x is not None]
            if not xs:
                return None, 0
            wins = sum(1 for x in xs if x > 0)
            return wins / len(xs), len(xs)

        score_hr, score_n = _hr(score_only_pnls)
        v2_hr, v2_n = _hr(v2_only_pnls)

        result = {
            "snapshots": len(by_gen),
            "avg_overlap_pct": (sum(overlap_pcts) / len(overlap_pcts)) if overlap_pcts else None,
            "score_only_n_tickers": len(score_only),
            "v2_only_n_tickers": len(v2_only),
            "score_only_trades": score_n,
            "score_only_hr": score_hr,
            "v2_only_trades": v2_n,
            "v2_only_hr": v2_hr,
        }
        # Verdict
        if v2_hr is not None and score_hr is not None:
            if v2_hr > score_hr + 0.05 and v2_n >= 10:
                result["verdict"] = "PROMOTE_V2_TO_ACTIVE"
            elif score_hr > v2_hr + 0.05 and score_n >= 10:
                result["verdict"] = "DISABLE_V2"
            else:
                result["verdict"] = "INCONCLUSIVE_KEEP_SHADOW"
        else:
            result["verdict"] = "INSUFFICIENT_DATA"
        return result
    finally:
        db.close()


if __name__ == "__main__":
    import json, sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(json.dumps(analyze(days_back=days), indent=2, default=str))
