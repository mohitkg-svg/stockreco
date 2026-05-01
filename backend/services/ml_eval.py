"""r68-B: nightly ML scorer evaluation.

Computes Brier score, Expected Calibration Error (ECE), AUC, and per-bucket
hit-rate over the closed `MLPrediction` rows from the last 60 days. Persists
the result so the operator can answer "is the ML scorer ready for promotion?"
without manually pulling /api/ml/calibration each night.

PROMOTION RULE
--------------
Promotion threshold (audit-derived):
  Brier < 0.245 (vs naive 0.25)
  ECE   < 0.05
  AUC   > 0.55
  n     >= 100 closed labels

If all four hold the daily eval row sets `ready_for_promotion = True`. The
operator (NOT this code) flips `cfg.ml_scoring_enabled = True` and watches.

Runs nightly at 03:30 UTC (after the existing 03:10 calibration job).
"""
from __future__ import annotations
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Promotion thresholds — see module docstring.
_PROMOTE_BRIER_MAX = 0.245
_PROMOTE_ECE_MAX = 0.05
_PROMOTE_AUC_MIN = 0.55
_PROMOTE_N_MIN = 100


def _brier_score(preds: List[float], outcomes: List[int]) -> float:
    if not preds:
        return float("nan")
    return sum((p - y) ** 2 for p, y in zip(preds, outcomes)) / len(preds)


def _ece(preds: List[float], outcomes: List[int], n_bins: int = 10) -> float:
    """Expected Calibration Error — mean |bucket_predicted - bucket_actual|
    weighted by bucket size. Lower is better; 0 = perfect calibration."""
    if not preds:
        return float("nan")
    n = len(preds)
    buckets: List[List[Tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in zip(preds, outcomes):
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        buckets[idx].append((p, y))
    total_err = 0.0
    for b in buckets:
        if not b:
            continue
        avg_p = sum(x[0] for x in b) / len(b)
        avg_y = sum(x[1] for x in b) / len(b)
        total_err += (len(b) / n) * abs(avg_p - avg_y)
    return total_err


def _auc(preds: List[float], outcomes: List[int]) -> float:
    """Mann-Whitney AUC (probability that a random positive ranks above a
    random negative). 0.5 = random; 1.0 = perfect."""
    pos = [p for p, y in zip(preds, outcomes) if y == 1]
    neg = [p for p, y in zip(preds, outcomes) if y == 0]
    if not pos or not neg:
        return float("nan")
    wins = 0
    for p_pos in pos:
        for p_neg in neg:
            if p_pos > p_neg:
                wins += 1
            elif p_pos == p_neg:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def _bucket_hits(preds: List[float], outcomes: List[int], n_bins: int = 5) -> List[Dict]:
    """Per-bucket [predicted_range, n, actual_winrate] table."""
    if not preds:
        return []
    rows: List[Dict] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        bucket = [(p, y) for p, y in zip(preds, outcomes) if lo <= p < hi or (hi >= 1.0 and p == 1.0)]
        if not bucket:
            rows.append({
                "bucket": f"{lo:.1f}-{hi:.1f}",
                "n": 0,
                "predicted_mean": None,
                "actual_winrate": None,
            })
            continue
        rows.append({
            "bucket": f"{lo:.1f}-{hi:.1f}",
            "n": len(bucket),
            "predicted_mean": round(sum(x[0] for x in bucket) / len(bucket), 4),
            "actual_winrate": round(sum(x[1] for x in bucket) / len(bucket), 4),
        })
    return rows


def evaluate(days: int = 60) -> Dict:
    """Compute Brier/ECE/AUC/per-bucket over the last `days` of closed labels.
    Returns a result dict (also persisted into MLEvalResult)."""
    from database import SessionLocal, MLPrediction, MLEvalResult
    cutoff = datetime.utcnow() - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = db.query(MLPrediction).filter(
            MLPrediction.closed_at.isnot(None),
            MLPrediction.outcome.isnot(None),
            MLPrediction.created_at >= cutoff,
        ).all()
        preds: List[float] = []
        outcomes: List[int] = []
        for r in rows:
            try:
                p = float(r.predicted_winrate)
                y = int(r.outcome)
                if 0.0 <= p <= 1.0 and y in (0, 1):
                    preds.append(p)
                    outcomes.append(y)
            except Exception:
                continue
        n = len(preds)
        brier = _brier_score(preds, outcomes) if n else float("nan")
        ece = _ece(preds, outcomes) if n else float("nan")
        auc = _auc(preds, outcomes) if n else float("nan")
        buckets = _bucket_hits(preds, outcomes)
        ready = (
            n >= _PROMOTE_N_MIN
            and not math.isnan(brier) and brier < _PROMOTE_BRIER_MAX
            and not math.isnan(ece) and ece < _PROMOTE_ECE_MAX
            and not math.isnan(auc) and auc > _PROMOTE_AUC_MIN
        )
        result = {
            "n": n,
            "days": days,
            "brier": round(brier, 5) if not math.isnan(brier) else None,
            "ece": round(ece, 5) if not math.isnan(ece) else None,
            "auc": round(auc, 5) if not math.isnan(auc) else None,
            "buckets": buckets,
            "ready_for_promotion": bool(ready),
            "thresholds": {
                "brier_max": _PROMOTE_BRIER_MAX,
                "ece_max": _PROMOTE_ECE_MAX,
                "auc_min": _PROMOTE_AUC_MIN,
                "n_min": _PROMOTE_N_MIN,
            },
            "computed_at": datetime.utcnow().isoformat(),
        }
        # Persist
        try:
            row = MLEvalResult(
                computed_at=datetime.utcnow(),
                window_days=days,
                n=n,
                brier=result["brier"],
                ece=result["ece"],
                auc=result["auc"],
                ready_for_promotion=bool(ready),
                buckets_json=str(buckets),
            )
            db.add(row)
            db.commit()
        except Exception as _pe:
            logger.warning(f"ml_eval persist failed: {_pe}")
        logger.info(
            f"ml_eval: n={n} brier={result['brier']} ece={result['ece']} "
            f"auc={result['auc']} ready={ready}"
        )
        return result
    finally:
        db.close()


def latest_result() -> Optional[Dict]:
    """Returns the most recent persisted eval row as a dict, or None."""
    from database import SessionLocal, MLEvalResult
    db = SessionLocal()
    try:
        row = db.query(MLEvalResult).order_by(MLEvalResult.computed_at.desc()).first()
        if not row:
            return None
        return {
            "computed_at": row.computed_at.isoformat() if row.computed_at else None,
            "window_days": row.window_days,
            "n": row.n,
            "brier": row.brier,
            "ece": row.ece,
            "auc": row.auc,
            "ready_for_promotion": bool(row.ready_for_promotion),
        }
    finally:
        db.close()
