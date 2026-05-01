"""ML model API — train / predict / scorecard / calibration."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query

from routers._auth import require_api_key
from services import ml_trainer, ml_scorer

router = APIRouter(
    prefix="/api/ml",
    tags=["ml"],
    dependencies=[Depends(require_api_key)],
)
logger = logging.getLogger(__name__)


@router.post("/train")
def train(max_tickers: int = Query(40, ge=5, le=200)):
    """Kick training in a background thread and return immediately.
    Cloud Run's 300s request timeout is shorter than the training run, so
    the work runs detached. Poll /api/ml/status or /api/ml/scorecard."""
    return ml_trainer.train_async(max_tickers=max_tickers)


@router.get("/status")
def status():
    """Current training state (queued|collecting|training|done|error).

    r45: also surfaces whether the isotonic calibrator is loaded — when
    True, predictions are calibrated; when False, raw booster output is
    served (typically because OOF sample count was below the 50-row
    threshold at last train, or no model has been trained yet).
    """
    out = ml_trainer.get_status()
    try:
        out["calibrator_loaded"] = ml_scorer.calibrator_loaded()
    except Exception:
        out["calibrator_loaded"] = False
    return out


@router.get("/scorecard")
def scorecard():
    """Latest model meta — when trained, AUC across folds, top-20 feature
    importances. None if no model has been trained yet."""
    meta = ml_trainer.model_meta()
    if not meta:
        raise HTTPException(status_code=404, detail="no model trained yet — POST /api/ml/train")
    return meta


@router.get("/predict/{ticker}")
def predict(ticker: str, signal_type: str = Query("BUY", pattern="^(BUY|SELL)$")):
    """Single-ticker prediction probe. Useful for sanity checks. Builds a
    stub signal and runs the scorer."""
    stub = {"signal_type": signal_type, "confidence": 70, "entry": None,
            "stop_loss": None, "target1": None}
    p = ml_scorer.predict_winrate(ticker, stub)
    if p is None:
        return {"ticker": ticker.upper(), "p_win": None,
                "note": "model not loaded or features unavailable"}
    return {
        "ticker": ticker.upper(),
        "signal_type": signal_type,
        "p_win": round(p, 4),
        "multiplier_if_enabled": ml_scorer.winrate_to_multiplier(p),
    }


@router.get("/calibration")
def calibration(days: int = Query(14, ge=1, le=180)):
    """Compare predicted P(win) buckets to realized win-rate. Used to decide
    when to flip ml_scoring_enabled=True. Requires closed trades with
    backfilled outcomes."""
    from database import SessionLocal, MLPrediction
    db = SessionLocal()
    try:
        since = datetime.utcnow() - timedelta(days=days)
        rows = (
            db.query(MLPrediction)
            .filter(MLPrediction.created_at >= since,
                    MLPrediction.outcome.isnot(None))
            .all()
        )
        if not rows:
            return {"buckets": [], "n_total": 0,
                    "note": "no closed predictions yet — wait for trades to close"}
        buckets = [(0.0, 0.35), (0.35, 0.45), (0.45, 0.55),
                   (0.55, 0.65), (0.65, 0.80), (0.80, 1.01)]
        out = []
        for lo, hi in buckets:
            in_b = [r for r in rows if lo <= r.predicted_winrate < hi]
            n = len(in_b)
            if n == 0:
                out.append({"bucket": f"[{lo:.2f}, {hi:.2f})", "n": 0,
                            "predicted_mean": None, "actual_winrate": None})
                continue
            pred_mean = sum(r.predicted_winrate for r in in_b) / n
            actual = sum(1 for r in in_b if r.outcome == 1) / n
            out.append({
                "bucket": f"[{lo:.2f}, {hi:.2f})",
                "n": n,
                "predicted_mean": round(pred_mean, 3),
                "actual_winrate": round(actual, 3),
            })
        return {"buckets": out, "n_total": len(rows)}
    finally:
        db.close()


@router.get("/eval-summary")
def eval_summary():
    """r68-B: latest nightly ML eval result (Brier / ECE / AUC + ready flag).
    Run automatically at 03:30 UTC by services.ml_eval.evaluate. Returns the
    most recent persisted row from MLEvalResult, or 404 if eval has never
    run."""
    from services import ml_eval
    out = ml_eval.latest_result()
    if not out:
        raise HTTPException(status_code=404, detail="no ml_eval rows yet — wait for nightly run or POST /api/ml/eval-now")
    return out


@router.post("/eval-now")
def eval_now(days: int = Query(60, ge=7, le=365)):
    """r68-B: run the ML eval immediately (operator-triggered) instead of
    waiting for the nightly cron."""
    from services import ml_eval
    return ml_eval.evaluate(days=days)
