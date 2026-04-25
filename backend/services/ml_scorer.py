"""ML scorer — load trained model, expose predict_winrate + multiplier.

Loads the model from disk on first use (lazy). Returns P(win) ∈ [0, 1] for a
given (ticker, signal) pair. The signal_generator translates that to a
small confidence multiplier (envelope 0.88..1.12) when shadow mode is OFF.

Shadow mode (default ON via cfg.ml_scoring_enabled=False):
  * predict_winrate still runs and the prediction is logged to MLPrediction.
  * Multiplier returned is 1.0 (no effect on confidence).
After 1-2 weeks of paper data, compare logged predictions to realized
outcomes; if calibration looks good, flip ml_scoring_enabled=True.
"""
from __future__ import annotations
import logging
import os
import threading
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

_MODEL_DIR = os.environ.get("ML_MODEL_DIR", "/tmp/ml_models")
_MODEL_PATH = os.path.join(_MODEL_DIR, "model.txt")

_booster = None
_booster_loaded_at: Optional[float] = None
_load_lock = threading.Lock()


def _hydrate_from_db_if_missing() -> bool:
    """If model.txt isn't on this container's /tmp, try pulling from DB.
    Returns True if a model is available locally after the call."""
    if os.path.exists(_MODEL_PATH):
        return True
    try:
        from services.ml_trainer import _db_get  # type: ignore
        text = _db_get("model")
    except Exception:
        text = None
    if not text:
        return False
    try:
        os.makedirs(_MODEL_DIR, exist_ok=True)
        with open(_MODEL_PATH, "w") as f:
            f.write(text)
        logger.info("ml_scorer: hydrated model from DB")
        return True
    except Exception as e:
        logger.warning(f"ml_scorer: hydrate failed: {e}")
        return False


def _load_if_needed():
    global _booster, _booster_loaded_at
    if not _hydrate_from_db_if_missing():
        _booster = None
        return
    if _booster is not None and _booster_loaded_at is not None:
        try:
            mtime = os.path.getmtime(_MODEL_PATH)
            if mtime <= _booster_loaded_at:
                return
        except Exception:
            return
    with _load_lock:
        if not os.path.exists(_MODEL_PATH):
            _booster = None
            return
        try:
            import lightgbm as lgb
            _booster = lgb.Booster(model_file=_MODEL_PATH)
            _booster_loaded_at = os.path.getmtime(_MODEL_PATH)
            logger.info("ml_scorer: model loaded")
        except Exception as e:
            logger.warning(f"ml_scorer: failed to load model: {e}")
            _booster = None


def predict_winrate(ticker: str, signal: Dict[str, Any], as_of: Optional[datetime] = None) -> Optional[float]:
    """Return P(win) ∈ [0, 1], or None if model is not available."""
    _load_if_needed()
    if _booster is None:
        return None
    try:
        from services.ml_features import extract_features, feature_columns
        feat = extract_features(
            ticker, as_of or datetime.utcnow(), signal,
            include_live_only=True,
        )
        cols = feature_columns()
        x = [[feat.get(c) for c in cols]]
        import numpy as np
        x_arr = np.array(x, dtype=float)
        p = float(_booster.predict(x_arr)[0])
        return max(0.0, min(1.0, p))
    except Exception as e:
        logger.debug(f"ml_scorer.predict_winrate({ticker}) failed: {e}")
        return None


# Map P(win) → confidence multiplier. Envelope is intentionally tight (0.88..1.12)
# because (a) the model has been trained on synthetic backtest labels, not live
# trades, and (b) we want it to *tilt* the existing signal stack, not dominate it.
# Envelope values live in services.config (ML_MULT_*).
def winrate_to_multiplier(p: Optional[float]) -> float:
    from services.config import (
        ML_MULT_HIGH, ML_MULT_LIFT, ML_MULT_NEUTRAL, ML_MULT_DAMP, ML_MULT_LOW,
    )
    if p is None:
        return ML_MULT_NEUTRAL
    if p >= 0.70: return ML_MULT_HIGH
    if p >= 0.60: return ML_MULT_LIFT
    if p >= 0.45: return ML_MULT_NEUTRAL
    if p >= 0.35: return ML_MULT_DAMP
    return ML_MULT_LOW


def log_prediction(ticker: str, signal: Dict[str, Any], p: Optional[float],
                   trade_id: Optional[int] = None) -> None:
    """Persist a prediction to MLPrediction. Used for post-hoc calibration."""
    if p is None:
        return
    try:
        from database import SessionLocal, MLPrediction
        db = SessionLocal()
        try:
            row = MLPrediction(
                ticker=ticker.upper(),
                signal_type=(signal.get("signal_type") or "").upper(),
                timeframe=signal.get("timeframe") or "1d",
                predicted_winrate=float(p),
                signal_confidence=float(signal.get("confidence") or 0),
                trade_id=trade_id,
                created_at=datetime.utcnow(),
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"ml_scorer.log_prediction failed: {e}")


def score_and_apply(ticker: str, signal: Dict[str, Any], scoring_enabled: bool) -> float:
    """Single entry point used by signal_generator. Always logs prediction;
    returns 1.0 in shadow mode, real multiplier when enabled."""
    p = predict_winrate(ticker, signal)
    log_prediction(ticker, signal, p)
    if not scoring_enabled:
        return 1.0
    return winrate_to_multiplier(p)
