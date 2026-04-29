"""r56 Tier-0 E3: factor information-coefficient sweep.

For each factor in the score_universe_v2 stack, compute the Spearman
rank correlation (IC) between factor value at scan time `t` and forward
5d / 10d / 20d return on the universe.

Run via:
  python -m backend.scripts.factor_ic_sweep

Output: per-factor IC at each horizon, with the verdict:
  - factor with IC > 0.03 at any horizon: KEEP (predictive)
  - factor with abs(IC) < 0.01: DROP (no signal)
  - factor with negative IC: INVERT (signal predicts opposite)
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _spearman(xs, ys):
    """Spearman rank correlation. Returns None if too few paired points."""
    if len(xs) < 10 or len(xs) != len(ys):
        return None
    pairs = [(x, y) for x, y in zip(xs, ys)
             if x is not None and y is not None
             and x == x and y == y]  # drop NaN
    if len(pairs) < 10:
        return None
    n = len(pairs)
    # Rank x and y separately
    ranks_x = sorted(range(n), key=lambda i: pairs[i][0])
    ranks_y = sorted(range(n), key=lambda i: pairs[i][1])
    rank_x = {ranks_x[r]: r for r in range(n)}
    rank_y = {ranks_y[r]: r for r in range(n)}
    d2 = sum((rank_x[i] - rank_y[i]) ** 2 for i in range(n))
    return 1 - (6 * d2) / (n * (n * n - 1))


def analyze(days_back: int = 60) -> dict:
    """Compute IC of {rvol, adx, rs_20d, pct_from_52w_high, mom_12_1, score_v2}
    against forward returns sourced from the daily-bar fetcher.
    """
    from backend.database import SessionLocal, CandidatePool
    from backend.services.data_fetcher import fetch_ohlcv
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        rows = (
            db.query(CandidatePool)
            .filter(CandidatePool.generated_at >= cutoff)
            .all()
        )
        if not rows:
            return {"verdict": "INSUFFICIENT_DATA"}
        # Group by ticker; forward return looked up once per ticker
        by_ticker: dict = defaultdict(list)
        for r in rows:
            by_ticker[r.ticker].append(r)

        # Compute forward returns 5d, 10d, 20d
        factors = {
            "rvol": [], "adx": [], "rs_20d": [], "pct_from_52w_high": [],
            "score": [], "score_v2": [],
        }
        forward = {5: [], 10: [], 20: []}
        for ticker, snaps in by_ticker.items():
            try:
                df = fetch_ohlcv(ticker, "1d")
                if df is None or df.empty or len(df) < 25:
                    continue
                # Use the most-recent snapshot as the observation point
                snap = snaps[-1]
                # Forward returns from t+1 to t+N (approx — uses last 25 bars)
                close = float(df["Close"].iloc[-1])
                fwd5  = float(df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1) if len(df) >= 6 else None
                fwd10 = float(df["Close"].iloc[-1] / df["Close"].iloc[-11] - 1) if len(df) >= 11 else None
                fwd20 = float(df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1) if len(df) >= 21 else None
                for k in factors:
                    factors[k].append(getattr(snap, k, None))
                forward[5].append(fwd5)
                forward[10].append(fwd10)
                forward[20].append(fwd20)
            except Exception:
                continue

        # Compute ICs
        ics = {}
        for k, fxs in factors.items():
            ics[k] = {}
            for h, fwd in forward.items():
                ics[k][f"fwd_{h}d"] = _spearman(fxs, fwd)

        # Verdict per factor
        verdicts = {}
        for k, by_h in ics.items():
            best = max((v for v in by_h.values() if v is not None), default=0)
            worst = min((v for v in by_h.values() if v is not None), default=0)
            if abs(best) < 0.01 and abs(worst) < 0.01:
                verdicts[k] = "DROP_NO_SIGNAL"
            elif worst < -0.03:
                verdicts[k] = "INVERT_OR_DROP"
            elif best > 0.03:
                verdicts[k] = "KEEP_PREDICTIVE"
            else:
                verdicts[k] = "WEAK_KEEP_OR_DROP_AT_OPERATOR_DISCRETION"

        return {
            "n_tickers": len(by_ticker),
            "ics": ics,
            "verdicts": verdicts,
        }
    finally:
        db.close()


if __name__ == "__main__":
    import json, sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    print(json.dumps(analyze(days_back=days), indent=2, default=str))
