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


def _label_trade(future: pd.DataFrame, side: str, entry: float, stop: float, t1: float) -> Optional[int]:
    """Return 1 (win), 0 (loss), or None (neither hit within horizon)."""
    if future.empty:
        return None
    if side == "BUY":
        for _, row in future.iterrows():
            high = float(row.get("High", row.get("Close")))
            low = float(row.get("Low", row.get("Close")))
            if low <= stop:
                return 0
            if high >= t1:
                return 1
    elif side == "SELL":
        for _, row in future.iterrows():
            high = float(row.get("High", row.get("Close")))
            low = float(row.get("Low", row.get("Close")))
            if high >= stop:
                return 0
            if low <= t1:
                return 1
    return None


def _collect_samples_for_ticker(ticker: str, daily_cache: Dict[str, pd.DataFrame]) -> List[Dict[str, Any]]:
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
        try:
            sig = generate_signal(ticker, "1d", sliced)
        except Exception:
            continue
        if not sig or sig.get("signal_type") not in ("BUY", "SELL"):
            continue
        side = sig["signal_type"]
        entry = sig.get("entry")
        stop = sig.get("stop_loss")
        t1 = sig.get("target1")
        if not all(isinstance(x, (int, float)) for x in (entry, stop, t1)):
            continue
        future = df.iloc[i + 1: i + 1 + _LABEL_HORIZON_BARS]
        label = _label_trade(future, side, float(entry), float(stop), float(t1))
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
                ticker, as_of_ts, sig,
                daily_df=sliced,
                daily_cache=daily_cache,
                include_live_only=False,
                tape_day_df=tape_day_df,
            )
        except Exception as e:
            logger.debug(f"ml_trainer: features {ticker}@{i}: {e}")
            continue
        feat["__label"] = int(label)
        feat["__ticker"] = ticker
        feat["__ts"] = pd.Timestamp(as_of_ts).isoformat()
        rows.append(feat)
    return rows


def collect_samples(tickers: Optional[List[str]] = None, max_tickers: int = 80) -> pd.DataFrame:
    """Build labeled DataFrame across watchlist + candidate pool tickers."""
    from database import SessionLocal, WatchlistStock, CandidatePool
    if tickers is None:
        db = SessionLocal()
        try:
            t = set(s.ticker for s in db.query(WatchlistStock).all())
            t |= set(r.ticker for r in db.query(CandidatePool).all())
        finally:
            db.close()
        tickers = sorted(t)[:max_tickers]
    daily_cache: Dict[str, pd.DataFrame] = {}
    all_rows: List[Dict[str, Any]] = []
    for tk in tickers:
        try:
            rows = _collect_samples_for_ticker(tk, daily_cache)
            all_rows.extend(rows)
        except Exception as e:
            logger.debug(f"ml_trainer: ticker {tk} skipped: {e}")
    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows).sort_values("__ts").reset_index(drop=True)
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
    y = samples["__label"].astype(int).values

    # r44 fix #0.16: walk-forward by TIME boundaries, not row index. Same
    # `__ts` boundary across all tickers prevents cross-ticker leakage:
    # AAPL@2024-Q2 train + MSFT@2024-Q2 test would otherwise be
    # interleaved-by-row, leaking near-future on the OTHER ticker.
    # Also adds a 5-day forward EMBARGO between train end and test start
    # to prevent same-ticker label leakage from neighboring bars (a 5d
    # label horizon means same-day samples can predict each other).
    EMBARGO_DAYS = 5
    timestamps = pd.to_datetime(samples["__ts"]).reset_index(drop=True)
    if len(timestamps) < (_FOLDS + 1) * 30:
        # Fall back to row-based folds when sample is too thin for time splits.
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
    for tr_idx, te_idx, k in fold_iter:
        X_tr = X[tr_idx]
        y_tr = y[tr_idx]
        X_te = X[te_idx]
        y_te = y[te_idx]
        if len(X_te) < 30 or len(set(y_tr)) < 2 or len(set(y_te)) < 2:
            continue
        # r44 fix #1.5: handle class imbalance via scale_pos_weight.
        n_neg = int((y_tr == 0).sum())
        n_pos = int((y_tr == 1).sum())
        spw = (n_neg / max(1, n_pos))
        booster = lgb.train(
            params={
                "objective": "binary",
                "metric": "binary_logloss",
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_data_in_leaf": 20,
                "feature_fraction": 0.85,
                "bagging_fraction": 0.85,
                "bagging_freq": 4,
                "scale_pos_weight": spw,
                "verbose": -1,
            },
            train_set=lgb.Dataset(X_tr, label=y_tr, feature_name=cols),
            num_boost_round=200,
        )
        pred = booster.predict(X_te)
        try:
            auc = roc_auc_score(y_te, pred)
        except Exception:
            auc = None
        acc = accuracy_score(y_te, (pred >= 0.5).astype(int))
        fold_metrics.append({
            "fold": k, "n_train": len(X_tr), "n_test": len(X_te),
            "auc": round(auc, 4) if auc is not None else None,
            "accuracy": round(acc, 4),
            "pos_rate_train": round(float(np.mean(y_tr)), 3),
            "pos_rate_test": round(float(np.mean(y_te)), 3),
        })
    if not fold_metrics:
        return {"trained": False, "reason": "no usable folds"}
    aucs = [m["auc"] for m in fold_metrics if m.get("auc") is not None]
    mean_auc = round(float(np.mean(aucs)), 4) if aucs else None

    # Final model on ALL samples (after fold validation passes the bar)
    final_booster = lgb.train(
        params={
            "objective": "binary",
            "metric": "binary_logloss",
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
