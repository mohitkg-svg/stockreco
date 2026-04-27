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
# r45 ML calibration: isotonic calibrator persisted alongside the booster.
_CALIB_PATH = os.path.join(_MODEL_DIR, "calibrator.pkl")

_booster = None
_booster_loaded_at: Optional[float] = None
_calibrator = None
_calibrator_loaded_at: Optional[float] = None
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


def _hydrate_calibrator_from_db() -> bool:
    """r45: pull the pickled isotonic calibrator from DB if not on disk.
    Stored as hex-encoded bytes in MLArtifact.content."""
    if os.path.exists(_CALIB_PATH):
        return True
    try:
        from services.ml_trainer import _db_get  # type: ignore
        hex_content = _db_get("calibrator")
    except Exception:
        hex_content = None
    if not hex_content:
        return False
    try:
        raw = bytes.fromhex(hex_content)
        os.makedirs(_MODEL_DIR, exist_ok=True)
        with open(_CALIB_PATH, "wb") as f:
            f.write(raw)
        logger.info("ml_scorer: hydrated calibrator from DB")
        return True
    except Exception as e:
        logger.warning(f"ml_scorer: calibrator hydrate failed: {e}")
        return False


def _load_if_needed():
    global _booster, _booster_loaded_at, _calibrator, _calibrator_loaded_at
    if not _hydrate_from_db_if_missing():
        _booster = None
        return
    # Reload booster if file mtime is newer than what we have.
    needs_booster_reload = (_booster is None) or (_booster_loaded_at is None)
    if not needs_booster_reload:
        try:
            mtime = os.path.getmtime(_MODEL_PATH)
            needs_booster_reload = mtime > _booster_loaded_at
        except Exception:
            needs_booster_reload = False
    if needs_booster_reload:
        with _load_lock:
            if not os.path.exists(_MODEL_PATH):
                _booster = None
            else:
                try:
                    import lightgbm as lgb
                    _booster = lgb.Booster(model_file=_MODEL_PATH)
                    _booster_loaded_at = os.path.getmtime(_MODEL_PATH)
                    logger.info("ml_scorer: model loaded")
                except Exception as e:
                    logger.warning(f"ml_scorer: failed to load model: {e}")
                    _booster = None
    # r45: load (or refresh) the isotonic calibrator alongside the booster.
    # If no calibrator file/DB entry exists, leave _calibrator=None — the
    # inference path will fall back to raw booster output. This is the right
    # behavior on a fresh install (model trained but calibrator not yet fit
    # because OOF sample count was below the 50-row threshold).
    _hydrate_calibrator_from_db()
    if not os.path.exists(_CALIB_PATH):
        _calibrator = None
        _calibrator_loaded_at = None
        return
    needs_calib_reload = (_calibrator is None) or (_calibrator_loaded_at is None)
    if not needs_calib_reload:
        try:
            mtime_c = os.path.getmtime(_CALIB_PATH)
            needs_calib_reload = mtime_c > _calibrator_loaded_at
        except Exception:
            needs_calib_reload = False
    if needs_calib_reload:
        with _load_lock:
            try:
                import pickle as _pickle
                with open(_CALIB_PATH, "rb") as f:
                    _calibrator = _pickle.load(f)
                _calibrator_loaded_at = os.path.getmtime(_CALIB_PATH)
                logger.info("ml_scorer: isotonic calibrator loaded")
            except Exception as e:
                logger.warning(f"ml_scorer: failed to load calibrator: {e}")
                _calibrator = None


def predict_winrate(ticker: str, signal: Dict[str, Any], as_of: Optional[datetime] = None) -> Optional[float]:
    """Return calibrated P(win) ∈ [0, 1], or None if model is not available.

    r45: when an isotonic calibrator is loaded, we apply it to the raw
    booster output. LightGBM tree outputs are systematically over-confident
    at the tails (predicted 0.85 → actual ~0.65-0.70 on small samples);
    isotonic learns the monotonic mapping that closes that gap.
    Falls back to raw output when calibrator is unavailable.
    """
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
        raw = float(_booster.predict(x_arr)[0])
        if _calibrator is not None:
            try:
                calibrated = float(_calibrator.transform([raw])[0])
                return max(0.0, min(1.0, calibrated))
            except Exception as e:
                logger.debug(f"ml_scorer: calibrator transform failed, using raw: {e}")
        return max(0.0, min(1.0, raw))
    except Exception as e:
        logger.debug(f"ml_scorer.predict_winrate({ticker}) failed: {e}")
        return None


def predict_winrate_raw_and_calibrated(ticker: str, signal: Dict[str, Any],
                                        as_of: Optional[datetime] = None
                                        ) -> Optional[Dict[str, Optional[float]]]:
    """r45: returns both raw and calibrated P(win) in a single call.
    Useful for the calibration-validation UI where the operator wants to
    see how much the calibrator is shifting predictions.
    Returns None if model isn't loaded.
    """
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
        raw = float(_booster.predict(x_arr)[0])
        cal = None
        if _calibrator is not None:
            try:
                cal = float(_calibrator.transform([raw])[0])
            except Exception:
                cal = None
        return {"raw": max(0.0, min(1.0, raw)), "calibrated": (max(0.0, min(1.0, cal)) if cal is not None else None)}
    except Exception as e:
        logger.debug(f"predict_winrate_raw_and_calibrated({ticker}) failed: {e}")
        return None


def calibrator_loaded() -> bool:
    """Observability accessor — does the scorer currently have a calibrator
    loaded? Surface in /api/ml/status so operators see whether calibrated
    or raw probabilities are being used at inference time.
    """
    return _calibrator is not None


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
