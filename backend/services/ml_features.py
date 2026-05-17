"""ML feature extraction.

Single function `extract_features(...)` returns a flat dict of features for
a (ticker, timestamp, signal_context) triple. Used at BOTH training time
and inference time so the feature row is byte-identical regardless of
whether we're training or scoring a live signal.

Design rules:
  * No look-ahead. All features must be computable from data on or before
    `as_of_ts`.
  * NaN-tolerant. LightGBM handles missing values natively — features that
    can't be computed at a given moment (e.g. historical analyst ratings)
    return None and the model learns when to ignore them.
  * Cheap. The full vector materializes from data already in the DB or
    cached OHLCV — no external calls during training.
"""
from __future__ import annotations
import logging
import math
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import pandas as pd

logger = logging.getLogger(__name__)

# Correlated assets — 20-day returns capture cross-asset regime.
_CORR_TICKERS = ["GLD", "SLV", "USO", "UUP", "TLT", "QQQ", "SPY"]


def _safe(v) -> Optional[float]:
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except Exception:
        pass
    return None


def _pct_change(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b


def _last_row_at_or_before(df: pd.DataFrame, ts: datetime) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    idx = df.index
    try:
        ts = pd.Timestamp(ts).tz_localize(None) if pd.Timestamp(ts).tzinfo is None else pd.Timestamp(ts).tz_convert(None)
    except Exception:
        ts = pd.Timestamp(ts)
    if hasattr(idx, "tz") and idx.tz is not None:
        try:
            idx = idx.tz_convert(None)
            df = df.copy()
            df.index = idx
        except Exception:
            pass
    sliced = df[df.index <= ts]
    if sliced.empty:
        return None
    return sliced.iloc[-1]


def _technical_features(daily: pd.DataFrame, as_of: datetime) -> Dict[str, Optional[float]]:
    row = _last_row_at_or_before(daily, as_of)
    out: Dict[str, Optional[float]] = {
        "tech_rsi": None, "tech_macd_diff": None, "tech_adx": None,
        "tech_atr_pct": None, "tech_bb_pos": None,
        "tech_dist_sma20_pct": None, "tech_dist_sma50_pct": None, "tech_dist_sma200_pct": None,
        "tech_rvol": None,
        "ret_5d": None, "ret_10d": None, "ret_20d": None,
    }
    if row is None:
        return out
    close = _safe(row.get("Close"))
    # r47 fix #T0a-5: indicators.py emits RSI_14 / ADX_14 / MACD_12_26 /
    # MACDs_12_26 / BBU_20 / BBL_20 / SMA_20 / SMA_50 / SMA_200; legacy
    # short names returned None silently → ALL technical features were
    # missing on every train + inference sample, AUC reported was off
    # technical-data-free features only (ret_*, macro, regime). Keep
    # legacy fallbacks for backward compatibility with old saved frames.
    out["tech_rsi"] = _safe(row.get("RSI_14")) or _safe(row.get("RSI"))
    out["tech_adx"] = _safe(row.get("ADX_14")) or _safe(row.get("ADX"))
    macd = _safe(row.get("MACD_12_26")) or _safe(row.get("MACD"))
    macd_sig = _safe(row.get("MACDs_12_26")) or _safe(row.get("MACD_signal"))
    if macd is not None and macd_sig is not None:
        out["tech_macd_diff"] = macd - macd_sig
    atr = _safe(row.get("ATR_14"))
    if atr is not None and close:
        out["tech_atr_pct"] = atr / close
    bbu = _safe(row.get("BBU_20")) or _safe(row.get("BB_upper"))
    bbl = _safe(row.get("BBL_20")) or _safe(row.get("BB_lower"))
    if bbu is not None and bbl is not None and bbu > bbl and close is not None:
        out["tech_bb_pos"] = (close - bbl) / (bbu - bbl)
    sma20 = _safe(row.get("SMA_20")) or _safe(row.get("SMA20"))
    sma50 = _safe(row.get("SMA_50")) or _safe(row.get("SMA50"))
    sma200 = _safe(row.get("SMA_200")) or _safe(row.get("SMA200"))
    out["tech_dist_sma20_pct"] = _pct_change(close, sma20)
    out["tech_dist_sma50_pct"] = _pct_change(close, sma50)
    out["tech_dist_sma200_pct"] = _pct_change(close, sma200)
    vol = _safe(row.get("Volume"))
    vavg = _safe(row.get("VOL_SMA20"))
    if vol is not None and vavg and vavg > 0:
        out["tech_rvol"] = vol / vavg
    # Returns
    try:
        sliced = daily[daily.index <= row.name]
        if len(sliced) >= 21:
            out["ret_20d"] = _pct_change(close, _safe(sliced["Close"].iloc[-21]))
        if len(sliced) >= 11:
            out["ret_10d"] = _pct_change(close, _safe(sliced["Close"].iloc[-11]))
        if len(sliced) >= 6:
            out["ret_5d"] = _pct_change(close, _safe(sliced["Close"].iloc[-6]))
    except Exception:
        pass
    return out


def _correlated_assets_features(as_of: datetime, daily_cache: Optional[Dict[str, pd.DataFrame]] = None) -> Dict[str, Optional[float]]:
    """20-day returns for GLD, SLV, USO, UUP (DXY proxy), TLT, QQQ, SPY."""
    from services.data_fetcher import fetch_ohlcv
    out: Dict[str, Optional[float]] = {}
    for sym in _CORR_TICKERS:
        try:
            if daily_cache is not None and sym in daily_cache:
                df = daily_cache[sym]
            else:
                df = fetch_ohlcv(sym, "1d")
                if daily_cache is not None:
                    daily_cache[sym] = df
            if df is None or df.empty:
                out[f"corr_{sym}_ret_20d"] = None
                continue
            row = _last_row_at_or_before(df, as_of)
            if row is None:
                out[f"corr_{sym}_ret_20d"] = None
                continue
            sliced = df[df.index <= row.name]
            if len(sliced) >= 21:
                out[f"corr_{sym}_ret_20d"] = _pct_change(_safe(row.get("Close")), _safe(sliced["Close"].iloc[-21]))
            else:
                out[f"corr_{sym}_ret_20d"] = None
        except Exception as e:
            logger.debug(f"corr feature {sym}: {e}")
            out[f"corr_{sym}_ret_20d"] = None
    return out


def _macro_features(as_of: datetime) -> Dict[str, Optional[float]]:
    """Hours to/from nearest high/medium-impact macro event + blackout flag."""
    from database import SessionLocal, MacroEvent
    out: Dict[str, Optional[float]] = {
        "macro_hrs_to_next_high": None, "macro_hrs_since_last_high": None,
        "macro_in_blackout": 0.0,
        "macro_last_surprise_pct": None,
    }
    db = SessionLocal()
    try:
        nxt_high = (
            db.query(MacroEvent)
            .filter(MacroEvent.release_time_utc >= as_of, MacroEvent.importance == "high")
            .order_by(MacroEvent.release_time_utc.asc()).first()
        )
        last_high = (
            db.query(MacroEvent)
            .filter(MacroEvent.release_time_utc <= as_of, MacroEvent.importance == "high")
            .order_by(MacroEvent.release_time_utc.desc()).first()
        )
        if nxt_high:
            out["macro_hrs_to_next_high"] = max(0.0, min(168.0, (nxt_high.release_time_utc - as_of).total_seconds() / 3600))
        if last_high:
            out["macro_hrs_since_last_high"] = max(0.0, min(168.0, (as_of - last_high.release_time_utc).total_seconds() / 3600))
            if last_high.surprise_pct is not None:
                out["macro_last_surprise_pct"] = float(last_high.surprise_pct)
        # crude blackout: within 30m before any high event or 60m after
        if nxt_high and (nxt_high.release_time_utc - as_of) <= timedelta(minutes=30):
            out["macro_in_blackout"] = 1.0
        elif last_high and (as_of - last_high.release_time_utc) <= timedelta(minutes=60):
            out["macro_in_blackout"] = 1.0
    except Exception as e:
        logger.debug(f"macro features: {e}")
    finally:
        db.close()
    return out


def _regime_features(as_of: datetime, daily_cache: Optional[Dict[str, pd.DataFrame]] = None) -> Dict[str, Optional[float]]:
    """VIX level + 5d change. Uses ^VIX via internal data_fetcher."""
    out: Dict[str, Optional[float]] = {"regime_vix": None, "regime_vix_chg_5d": None}
    try:
        if daily_cache is not None and "^VIX" in daily_cache:
            df = daily_cache["^VIX"]
        else:
            from services.data_fetcher import _fetch_chart
            # Use robust internal fetcher (avoids yfinance JSONDecodeErrors + Alpaca warnings)
            df = _fetch_chart("^VIX", "1d", "2y")
            if daily_cache is not None:
                daily_cache["^VIX"] = df
        row = _last_row_at_or_before(df, as_of) if df is not None else None
        if row is not None:
            cur = _safe(row.get("Close"))
            out["regime_vix"] = cur
            sliced = df[df.index <= row.name]
            if len(sliced) >= 6:
                out["regime_vix_chg_5d"] = _pct_change(cur, _safe(sliced["Close"].iloc[-6]))
    except Exception as e:
        logger.debug(f"vix feature: {e}")
    return out


def _live_only_features(ticker: str) -> Dict[str, Optional[float]]:
    """Features only available at LIVE inference time (not for historical
    backtest rows). Returns None values for all keys so the schema matches
    even when these aren't computable."""
    out: Dict[str, Optional[float]] = {
        "analyst_mean": None, "analyst_count": None, "analyst_target_prem": None,
        "sentiment_bullish_pct": None, "sentiment_message_count": None,
    }
    try:
        from services.analyst_ratings import get_rating
        r = get_rating(ticker)
        if r:
            out["analyst_mean"] = _safe(r.get("mean"))
            out["analyst_count"] = _safe(r.get("analyst_count"))
            tm = _safe(r.get("target_mean"))
            # current price is needed; fetch from latest quote cache if avail
            try:
                from services.data_fetcher import get_ticker_info
                px = _safe(get_ticker_info(ticker).get("regularMarketPrice"))
                if tm and px:
                    out["analyst_target_prem"] = (tm - px) / px
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"live-only features analyst: {e}")
        
    try:
        from services.social_sentiment import get_sentiment
        s = get_sentiment(ticker)
        if s:
            out["sentiment_bullish_pct"] = _safe(s.get("bullish_pct_24h"))
            out["sentiment_message_count"] = _safe(s.get("message_count_24h"))
    except Exception as e:
        logger.debug(f"live-only features sentiment: {e}")
        
    return out


def _microstructure_features(ticker: str, as_of: datetime,
                              tape_day_df: Optional[pd.DataFrame] = None) -> Dict[str, Optional[float]]:
    """Wrapper around services.alpaca_tape.microstructure_features that
    gracefully no-ops if the tape module / Alpaca credentials are missing."""
    out: Dict[str, Optional[float]] = {
        "ms_trade_count": None, "ms_avg_size": None, "ms_dollar_volume": None,
        "ms_block_trade_pct": None, "ms_buysell_imbalance": None, "ms_tape_accel": None,
        "ms_ob_imbalance": None, "ms_l3_skew": None,
    }
    try:
        from services.alpaca_tape import microstructure_features
        ms = microstructure_features(ticker, as_of, lookback_minutes=30, day_df=tape_day_df)
        out.update(ms)
    except Exception as e:
        logger.debug(f"microstructure features {ticker}: {e}")
        
    try:
        from services.polygon_historical import get_historical_obi
        obi = get_historical_obi(ticker, as_of)
        if obi is not None:
            out["ms_ob_imbalance"] = obi
    except Exception as e:
        pass
        
    return out


def extract_features(
    ticker: str,
    as_of_ts: datetime,
    signal: Dict[str, Any],
    daily_df: Optional[pd.DataFrame] = None,
    daily_cache: Optional[Dict[str, pd.DataFrame]] = None,
    include_live_only: bool = True,
    tape_day_df: Optional[pd.DataFrame] = None,
    drop_target_geometry: bool = False,
) -> Dict[str, Optional[float]]:
    """Materialize the full feature row for a (ticker, ts, signal) triple.

    Pass `daily_df` (already-indicator-computed) when training over a known
    series to avoid refetching. Pass `daily_cache` (mutable dict) to share
    correlated-asset DataFrames across many calls in the same training loop.

    `include_live_only=False` for historical/backtest rows where analyst
    ratings and live news aren't available. The model still scores those
    columns but treats them as missing.

    `drop_target_geometry=True` zeroes out the entry/stop/target1-derived
    features (sig_stop_pct, sig_t1_pct) to close the F1 label-leak path:
    the labeler decides win/loss from the same levels, so a model trained on
    those features learns the labeler, not signal quality.
    """
    if daily_df is None:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators
        daily_df = compute_indicators(fetch_ohlcv(ticker, "1d"))
    feat: Dict[str, Optional[float]] = {}
    feat.update(_technical_features(daily_df, as_of_ts))
    feat.update(_correlated_assets_features(as_of_ts, daily_cache))
    feat.update(_macro_features(as_of_ts))
    feat.update(_regime_features(as_of_ts, daily_cache))
    feat.update(_microstructure_features(ticker, as_of_ts, tape_day_df=tape_day_df))
    if include_live_only:
        feat.update(_live_only_features(ticker))
    else:
        # keep schema consistent
        feat.update({"analyst_mean": None, "analyst_count": None, "analyst_target_prem": None,
                     "sentiment_bullish_pct": None, "sentiment_message_count": None})
    return feat


def feature_columns() -> List[str]:
    """Return the canonical, ordered list of feature column names. Used to
    materialize feature dicts into a 2-D matrix for training/inference."""
    return [
        # technical
        "tech_rsi", "tech_macd_diff", "tech_adx", "tech_atr_pct", "tech_bb_pos",
        "tech_dist_sma20_pct", "tech_dist_sma50_pct", "tech_dist_sma200_pct",
        "tech_rvol", "ret_5d", "ret_10d", "ret_20d",
        # correlated
        "corr_GLD_ret_20d", "corr_SLV_ret_20d", "corr_USO_ret_20d", "corr_UUP_ret_20d",
        "corr_TLT_ret_20d", "corr_QQQ_ret_20d", "corr_SPY_ret_20d",
        # macro
        "macro_hrs_to_next_high", "macro_hrs_since_last_high", "macro_in_blackout",
        "macro_last_surprise_pct",
        # regime
        "regime_vix", "regime_vix_chg_5d",
        # microstructure (Alpaca consolidated tape, last 30 min)
        "ms_trade_count", "ms_avg_size", "ms_dollar_volume",
        "ms_block_trade_pct", "ms_buysell_imbalance", "ms_tape_accel",
        "ms_ob_imbalance", "ms_l3_skew",
        # live-only
        "analyst_mean", "analyst_count", "analyst_target_prem",
        "sentiment_bullish_pct", "sentiment_message_count",
    ]
