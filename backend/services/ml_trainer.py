"""ML trainer — synthesize labeled examples from historical signals + train.

For each ticker in (watchlist ∪ candidate pool):
  * walk historical daily bars
  * at each bar, run generate_signal() on the bars-up-to-this-point
  * if a signal fires, extract features and look forward to determine label:
    BUY win = high crosses target1 within N bars BEFORE low crosses stop_loss
    BUY loss = low crosses stop_loss first
    Neither (open) → drop row
  * mirror for SELL

Walk-forward: 4 chronological folds. Train on folds 0..k-1, score on fold k.
Mean test AUC across folds is the model's headline metric.

Output: (model.txt, feature_importance.json, scorecard.json) persisted to
disk + DB. The scorer loads model.txt at startup.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_MODEL_DIR = os.environ.get("ML_MODEL_DIR", "/tmp/ml_models")
_MODEL_PATH = os.path.join(_MODEL_DIR, "model.txt")
_META_PATH = os.path.join(_MODEL_DIR, "meta.json")
_STATUS_PATH = os.path.join(_MODEL_DIR, "status.json")
# r45: isotonic calibrator persisted alongside the booster so inference-time
# P(win) is properly calibrated to actual win-rate (LightGBM tree outputs
# are systematically over-confident at the tails — well-known result).
_CALIB_PATH = os.path.join(_MODEL_DIR, "calibrator.pkl")


def _db_put(name: str, content: str, is_binary: bool = False) -> None:
    """Upsert a row in ml_artifacts. Survives container churn."""
    from database import SessionLocal, MLArtifact
    db = SessionLocal()
    try:
        row = db.query(MLArtifact).filter(MLArtifact.name == name).first()
        if row is None:
            row = MLArtifact(name=name, content=content, is_binary=is_binary)
            db.add(row)
        else:
            row.content = content
            row.is_binary = is_binary
            row.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _db_get(name: str) -> Optional[str]:
    from database import SessionLocal, MLArtifact
    db = SessionLocal()
    try:
        row = db.query(MLArtifact).filter(MLArtifact.name == name).first()
        return row.content if row else None
    finally:
        db.close()

# Label horizon: how many daily bars to look forward to determine win/loss.
_LABEL_HORIZON_BARS = 10
# Minimum daily bars of history before we'll generate signals (warm-up).
_WARMUP_BARS = 220
# Sample stride: skip every N bars between signal evaluations to avoid
# correlated samples (consecutive days look almost identical).
_SAMPLE_STRIDE = 2
# Walk-forward folds.
_FOLDS = 4
# Min positive examples needed; below this we don't train.
_MIN_TOTAL_SAMPLES = 200


def _label_forward_return(future: pd.DataFrame, entry_close: float) -> Optional[float]:
    """Target variable: continuous N-bar forward return (regression)."""
    if future.empty:
        return None
    # Calculate pure forward return percentage
    ret = (float(future["Close"].iloc[-1]) - entry_close) / entry_close
    return float(ret)


def _read_drop_target_geometry_flag() -> bool:
    """r96 F1: read the ml_features_drop_target_geometry flag from cfg.
    Default False so absence/error preserves prior leaky behavior — operator
    must explicitly flip the bit to engage the leak fix."""
    try:
        from database import SessionLocal, AutoTraderConfig
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            return bool(getattr(cfg, "ml_features_drop_target_geometry", False)) if cfg else False
        finally:
            db.close()
    except Exception:
        return False


def _collect_samples_for_ticker(ticker: str, daily_cache: Dict[str, pd.DataFrame],
                                 drop_target_geometry: bool = False) -> List[Dict[str, Any]]:
    """Walk historical daily bars, generate signals, label outcomes, return rows."""
    from services.data_fetcher import fetch_ohlcv
    from services.indicators import compute_indicators
    from services.signal_generator import generate_signal
    from services.ml_features import extract_features

    rows: List[Dict[str, Any]] = []
    try:
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty or len(df) < _WARMUP_BARS + _LABEL_HORIZON_BARS:
            return rows
        df = compute_indicators(df).copy()
    except Exception as e:
        logger.debug(f"ml_trainer: prep {ticker} failed: {e}")
        return rows

    daily_cache.setdefault(ticker, df)

    # Walk forward in time: at each bar i, signal generated using df[:i+1],
    # then look at df[i+1:i+1+horizon] for win/loss.
    n = len(df)
    last_safe = n - _LABEL_HORIZON_BARS - 1
    for i in range(_WARMUP_BARS, last_safe + 1, _SAMPLE_STRIDE):
        sliced = df.iloc[:i + 1]
        
        entry = float(sliced["Close"].iloc[-1])
        future = df.iloc[i + 1: i + 1 + _LABEL_HORIZON_BARS]
        label = _label_forward_return(future, entry)
        
        if label is None:
            continue
        as_of_ts = df.index[i].to_pydatetime() if hasattr(df.index[i], "to_pydatetime") else df.index[i]
        # Pull full-day tape once per (ticker, day) — many samples across the
        # same day reuse the same 16K-trade DataFrame from cache.
        try:
            from services.alpaca_tape import fetch_full_day
            tape_day_df = fetch_full_day(ticker, as_of_ts)
        except Exception:
            tape_day_df = None
        try:
            feat = extract_features(
                ticker, as_of_ts, {},
                daily_df=sliced,
                daily_cache=daily_cache,
                include_live_only=False,
                tape_day_df=tape_day_df,
                drop_target_geometry=drop_target_geometry,
            )
        except Exception as e:
            logger.debug(f"ml_trainer: features {ticker}@{i}: {e}")
            continue
        feat["__label"] = float(label)
        feat["__ticker"] = ticker
        feat["__ts"] = pd.Timestamp(as_of_ts).isoformat()
        rows.append(feat)
    return rows


def _read_use_live_outcomes_flag() -> bool:
    """r96 F8: gate for ingesting realized MLPrediction outcomes as labeled
    training rows. Default False — operator opts in once feature capture
    has been collecting for a while (otherwise n_live is too small to help)."""
    try:
        from database import SessionLocal, AutoTraderConfig
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            return bool(getattr(cfg, "ml_trainer_use_live_outcomes", False)) if cfg else False
        finally:
            db.close()
    except Exception:
        return False


def _collect_live_outcome_samples() -> List[Dict[str, Any]]:
    """r96 F8: pull closed MLPrediction rows with features_json + outcome and
    convert them to training samples. Empty list when flag is off or no rows
    qualify."""
    import json as _json_lo
    from database import SessionLocal, MLPrediction
    from services.ml_features import feature_columns
    rows: List[Dict[str, Any]] = []
    db = SessionLocal()
    try:
        preds = (
            db.query(MLPrediction)
            .filter(
                MLPrediction.outcome.isnot(None),
                MLPrediction.features_json.isnot(None),
            )
            .all()
        )
        cols = set(feature_columns())
        for r in preds:
            try:
                feat = _json_lo.loads(r.features_json or "{}")
            except Exception:
                continue
            if not isinstance(feat, dict):
                continue
            # Keep only known feature columns, drop anything unexpected so the
            # row shape matches feature_columns().
            sample = {k: feat.get(k) for k in cols}
            sample["__label"] = 0.05 if int(r.outcome) == 1 else -0.05
            sample["__ticker"] = r.ticker
            sample["__ts"] = (r.created_at.isoformat() if r.created_at else "")
            sample["__source"] = "live"
            rows.append(sample)
    finally:
        db.close()
    return rows


def collect_samples(tickers: Optional[List[str]] = None, max_tickers: int = 80) -> pd.DataFrame:
    """Build labeled DataFrame across watchlist + candidate pool tickers.

    r96 F8: when cfg.ml_trainer_use_live_outcomes is True, closed MLPrediction
    rows (live realized win/loss outcomes captured at scoring time) are
    appended to the synthetic backtest rows — closes the feedback loop from
    live trades back into the next training round.
    """
    from database import SessionLocal, WatchlistStock, CandidatePool
    if tickers is None:
        # r96 R4: when survivorship_filter_enabled is True, include delisted
        # tickers so ML training labels aren't biased toward survivor names.
        try:
            from services.survivorship import list_universe, survivorship_enabled
            include_delisted = survivorship_enabled()
            tickers = list_universe(include_delisted=include_delisted)[:max_tickers]
        except Exception:
            db = SessionLocal()
            try:
                t = set(s.ticker for s in db.query(WatchlistStock).all())
                t |= set(r.ticker for r in db.query(CandidatePool).all())
            finally:
                db.close()
            tickers = sorted(t)[:max_tickers]
    drop_target_geometry = _read_drop_target_geometry_flag()
    if drop_target_geometry:
        logger.info("ml_trainer: F1 label-leak fix engaged — sig_stop_pct/sig_t1_pct features dropped")
    daily_cache: Dict[str, pd.DataFrame] = {}
    all_rows: List[Dict[str, Any]] = []
    for tk in tickers:
        try:
            rows = _collect_samples_for_ticker(tk, daily_cache, drop_target_geometry=drop_target_geometry)
            all_rows.extend(rows)
        except Exception as e:
            logger.debug(f"ml_trainer: ticker {tk} skipped: {e}")
    # r96 F8: append live realized outcomes if enabled.
    if _read_use_live_outcomes_flag():
        try:
            live_rows = _collect_live_outcome_samples()
            if live_rows:
                logger.info(
                    f"ml_trainer: F8 ingested {len(live_rows)} live MLPrediction "
                    f"outcomes alongside {len(all_rows)} synthetic backtest rows"
                )
                all_rows.extend(live_rows)
        except Exception as e:
            logger.warning(f"ml_trainer: F8 live-outcome ingest failed: {e}")
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows).sort_values("__ts").reset_index(drop=True)

    # Cross-sectional z-score normalization
    from services.ml_features import feature_columns
    cols = [c for c in feature_columns() if c not in ("macro_in_blackout", "analyst_count")]
    df[cols] = df.groupby("__ts")[cols].transform(lambda x: (x - x.mean()) / (x.std() + 1e-9))

    logger.info(f"ml_trainer: collected {len(df)} samples across {df['__ticker'].nunique()} tickers")
    return df


def train(samples: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """Train LightGBM walk-forward across 4 folds. Returns scorecard dict and
    persists model + meta to disk. No-op (returns error dict) if training data
    is too thin."""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score, accuracy_score

    if samples is None:
        samples = collect_samples()
    if samples is None or len(samples) < _MIN_TOTAL_SAMPLES:
        return {"trained": False, "reason": f"too few samples ({len(samples) if samples is not None else 0} < {_MIN_TOTAL_SAMPLES})"}

    from services.ml_features import feature_columns
    cols = feature_columns()
    X = samples[cols].astype(float).values
    y = samples["__label"].astype(float).values

    # r44 fix #0.16: walk-forward by TIME boundaries, not row index. Same
    # `__ts` boundary across all tickers prevents cross-ticker leakage:
    # AAPL@2024-Q2 train + MSFT@2024-Q2 test would otherwise be
    # interleaved-by-row, leaking near-future on the OTHER ticker.
    # Also adds a 5-day forward EMBARGO between train end and test start
    # to prevent same-ticker label leakage from neighboring bars (a 5d
    # label horizon means same-day samples can predict each other).
    EMBARGO_DAYS = 5
    timestamps = pd.to_datetime(samples["__ts"]).reset_index(drop=True)
    # r96 F5: read cfg.ml_strict_time_folds. When True, refuse to train on
    # too-thin data instead of falling back to row-based folds — the row
    # fallback reintroduces interleaved-by-row leakage exactly when the
    # sample is small, which is the worst time for it. Default False keeps
    # prior behavior (fallback) until operator opts in.
    _strict_time_folds = False
    try:
        from database import SessionLocal as _SL_strict, AutoTraderConfig as _ATC_strict
        _dbs = _SL_strict()
        try:
            _cfg_strict = _dbs.query(_ATC_strict).filter(_ATC_strict.id == 1).first()
            _strict_time_folds = bool(getattr(_cfg_strict, "ml_strict_time_folds", False)) if _cfg_strict else False
        finally:
            _dbs.close()
    except Exception:
        _strict_time_folds = False
    if len(timestamps) < (_FOLDS + 1) * 30:
        if _strict_time_folds:
            # r96 F5 strict mode: refuse to train rather than mask leakage.
            return {
                "trained": False,
                "reason": (
                    f"ml_strict_time_folds=True and sample size {len(timestamps)} "
                    f"< {(_FOLDS + 1) * 30} required for time-based folds. "
                    f"Row-based fallback would leak; refusing to train."
                ),
            }
        # Fall back to row-based folds when sample is too thin for time splits.
        # WARNING: this path reintroduces interleaved-by-row leakage; flip
        # cfg.ml_strict_time_folds=True to forbid it once enough samples exist.
        logger.warning(
            f"ml_trainer: sample size {len(timestamps)} < "
            f"{(_FOLDS + 1) * 30} — using ROW-BASED folds (leakage risk). "
            f"Flip cfg.ml_strict_time_folds=True to refuse instead."
        )
        n = len(samples)
        fold_size = n // (_FOLDS + 1)
        fold_iter = []
        for k in range(1, _FOLDS + 1):
            train_end = k * fold_size
            test_end = (k + 1) * fold_size
            fold_iter.append((slice(0, train_end), slice(train_end, test_end), k))
    else:
        # Time-based: split timestamps into _FOLDS+1 quantile boundaries.
        ts_sorted = timestamps.sort_values()
        bounds = [
            ts_sorted.iloc[int(i * len(ts_sorted) / (_FOLDS + 1))]
            for i in range(1, _FOLDS + 2)
        ]
        fold_iter = []
        for k in range(1, _FOLDS + 1):
            train_cutoff = bounds[k - 1]
            embargo_cutoff = train_cutoff + pd.Timedelta(days=EMBARGO_DAYS)
            test_end_ts = bounds[k]
            train_mask = (timestamps <= train_cutoff).to_numpy()
            test_mask = ((timestamps > embargo_cutoff) & (timestamps <= test_end_ts)).to_numpy()
            fold_iter.append((train_mask, test_mask, k))

    fold_metrics: List[Dict[str, Any]] = []
    # r45: collect out-of-fold (preds, labels) across every fold so we
    # can fit a single isotonic calibrator on truly held-out predictions.
    oof_preds: List[float] = []
    oof_labels: List[int] = []
    for tr_idx, te_idx, k in fold_iter:
        X_tr = X[tr_idx]
        y_tr = y[tr_idx]
        X_te = X[te_idx]
        y_te = y[te_idx]
        if len(X_te) < 30 or len(set(y_tr)) < 2 or len(set(y_te)) < 2:
            continue
        booster = lgb.train(
            params={
                "objective": "regression",
                "metric": "rmse",
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_data_in_leaf": 20,
                "feature_fraction": 0.85,
                "bagging_fraction": 0.85,
                "bagging_freq": 4,
                "verbose": -1,
            },
            train_set=lgb.Dataset(X_tr, label=y_tr, feature_name=cols),
            num_boost_round=200,
        )
        pred = booster.predict(X_te)
        
        # Convert continuous labels to binary for classification metrics and Isotonic Mapping
        y_te_bin = (y_te > 0).astype(int)
        y_tr_bin = (y_tr > 0).astype(int)
        
        try:
            auc = roc_auc_score(y_te_bin, pred)
        except Exception:
            auc = None
        acc = accuracy_score(y_te_bin, (pred > 0.0).astype(int))
        fold_metrics.append({
            "fold": k, "n_train": len(X_tr), "n_test": len(X_te),
            "auc": round(auc, 4) if auc is not None else None,
            "accuracy": round(acc, 4),
            "pos_rate_train": round(float(np.mean(y_tr_bin)), 3),
            "pos_rate_test": round(float(np.mean(y_te_bin)), 3),
        })
        oof_preds.extend(pred.tolist())
        oof_labels.extend(y_te_bin.tolist())
    if not fold_metrics:
        return {"trained": False, "reason": "no usable folds"}
    aucs = [m["auc"] for m in fold_metrics if m.get("auc") is not None]
    mean_auc = round(float(np.mean(aucs)), 4) if aucs else None

    # Final model on ALL samples (after fold validation passes the bar)
    final_booster = lgb.train(
        params={
            "objective": "regression",
            "metric": "rmse",
            "learning_rate": 0.05,
            "num_leaves": 31,
            "min_data_in_leaf": 20,
            "feature_fraction": 0.85,
            "bagging_fraction": 0.85,
            "bagging_freq": 4,
            "verbose": -1,
        },
        train_set=lgb.Dataset(X, label=y, feature_name=cols),
        num_boost_round=300,
    )

    # Persist
    os.makedirs(_MODEL_DIR, exist_ok=True)
    final_booster.save_model(_MODEL_PATH)
    # Mirror to DB so the artifact survives container churn / scale events.
    try:
        with open(_MODEL_PATH) as f:
            _db_put("model", f.read(), is_binary=False)
    except Exception as e:
        logger.warning(f"ml_trainer: db model persist failed: {e}")

    # r45 ML calibration: fit an isotonic regression on the out-of-fold
    # predictions vs realized labels. LightGBM tree outputs are systematically
    # over-confident at the tails — predicted P(win)=0.85 corresponds to an
    # ACTUAL win-rate around 0.65-0.70 on small samples. Isotonic learns the
    # monotonic mapping from raw output to calibrated probability without
    # assuming a parametric shape. Persist the fitted calibrator next to the
    # booster; ml_scorer applies it at inference time.
    calib_meta: Dict[str, Any] = {
        "fitted": False,
        "n_oof_samples": len(oof_preds),
    }
    if len(oof_preds) >= 50 and len(set(oof_labels)) >= 2:
        try:
            from sklearn.isotonic import IsotonicRegression
            import pickle as _pickle
            calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            calibrator.fit(np.array(oof_preds), np.array(oof_labels))
            # Sanity-check the calibrator on the OOF set itself.
            try:
                from sklearn.metrics import brier_score_loss as _brier
                raw_brier = _brier(oof_labels, oof_preds)
                calib_brier = _brier(oof_labels, calibrator.transform(oof_preds))
                calib_meta["raw_brier"] = round(float(raw_brier), 4)
                calib_meta["calibrated_brier"] = round(float(calib_brier), 4)
                calib_meta["brier_improvement"] = round(float(raw_brier - calib_brier), 4)
            except Exception:
                pass
            with open(_CALIB_PATH, "wb") as f:
                _pickle.dump(calibrator, f)
            try:
                # DB mirror — pickle bytes hex-encoded (MLArtifact.content is
                # a String column, not BLOB; hex keeps it text-safe).
                with open(_CALIB_PATH, "rb") as f:
                    _db_put("calibrator", f.read().hex(), is_binary=True)
            except Exception as e:
                logger.warning(f"ml_trainer: db calibrator persist failed: {e}")
            calib_meta["fitted"] = True
            logger.info(
                f"ml_trainer: isotonic calibrator fitted on {len(oof_preds)} OOF samples "
                f"(brier raw={calib_meta.get('raw_brier')}, calibrated={calib_meta.get('calibrated_brier')})"
            )
        except Exception as e:
            logger.warning(f"ml_trainer: calibrator fit failed: {e}")
            calib_meta["error"] = str(e)[:200]
    else:
        calib_meta["reason"] = "too few OOF samples or single-class"
        # Remove any stale calibrator file/DB entry so old calibrators don't
        # silently apply to a freshly-trained model with insufficient data.
        try:
            if os.path.exists(_CALIB_PATH):
                os.remove(_CALIB_PATH)
        except Exception:
            pass
        try:
            from database import SessionLocal as _SL_calib, MLArtifact as _MA_calib
            _db = _SL_calib()
            try:
                row = _db.query(_MA_calib).filter(_MA_calib.name == "calibrator").first()
                if row:
                    _db.delete(row)
                    _db.commit()
            finally:
                _db.close()
        except Exception:
            pass

    importance = dict(zip(cols, [int(v) for v in final_booster.feature_importance(importance_type="gain")]))
    importance_sorted = sorted(importance.items(), key=lambda kv: -kv[1])
    meta = {
        "trained_at": datetime.utcnow().isoformat(),
        "n_samples": int(n),
        "n_features": len(cols),
        "feature_columns": cols,
        "mean_auc_oof": mean_auc,
        "fold_metrics": fold_metrics,
        "feature_importance_top20": importance_sorted[:20],
        "tickers": int(samples["__ticker"].nunique()),
        "horizon_bars": _LABEL_HORIZON_BARS,
        "stride": _SAMPLE_STRIDE,
        "calibrator": calib_meta,
    }
    with open(_META_PATH, "w") as f:
        json.dump(meta, f, indent=2)
    try:
        _db_put("meta", json.dumps(meta), is_binary=False)
    except Exception as e:
        logger.warning(f"ml_trainer: db meta persist failed: {e}")
    logger.info(f"ml_trainer: model saved (AUC mean {mean_auc}, n={n})")
    return {"trained": True, **meta}


def model_meta() -> Optional[Dict[str, Any]]:
    """Read meta from DB first (durable), fall back to local file."""
    payload = _db_get("meta")
    if payload:
        try:
            return json.loads(payload)
        except Exception:
            pass
    if os.path.exists(_META_PATH):
        try:
            with open(_META_PATH) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _write_status(state: str, **extra) -> None:
    payload = {"state": state, "updated_at": datetime.utcnow().isoformat(), **extra}
    try:
        _db_put("status", json.dumps(payload), is_binary=False)
    except Exception as e:
        logger.warning(f"ml_trainer: db status write failed: {e}")
    # Local file mirror (cheap; useful for in-process inspection).
    try:
        os.makedirs(_MODEL_DIR, exist_ok=True)
        with open(_STATUS_PATH, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


def get_status() -> Dict[str, Any]:
    payload = _db_get("status")
    if payload:
        try:
            return json.loads(payload)
        except Exception:
            pass
    if os.path.exists(_STATUS_PATH):
        try:
            with open(_STATUS_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"state": "no_status"}


def train_async(max_tickers: int = 40) -> Dict[str, Any]:
    """Kick training in a background thread and return immediately. Status
    polled via /api/ml/status or /api/ml/scorecard."""
    import threading
    cur = get_status()
    if cur.get("state") in ("collecting", "training"):
        return {"accepted": False, "reason": f"already running (state={cur.get('state')})", "status": cur}

    def _job():
        try:
            _write_status("collecting", max_tickers=max_tickers)
            samples = collect_samples(max_tickers=max_tickers)
            n = 0 if samples is None else len(samples)
            if n < _MIN_TOTAL_SAMPLES:
                _write_status("done", trained=False, n_samples=n,
                              reason=f"too few samples ({n} < {_MIN_TOTAL_SAMPLES})")
                return
            _write_status("training", n_samples=n)
            result = train(samples)
            _write_status("done", **{k: v for k, v in result.items() if k != "feature_columns"})
        except Exception as e:
            logger.exception("ml train_async failed")
            _write_status("error", error=str(e)[:500])

    t = threading.Thread(target=_job, name="ml-train", daemon=True)
    t.start()
    _write_status("queued", max_tickers=max_tickers)
    return {"accepted": True, "max_tickers": max_tickers, "note": "poll /api/ml/status"}
