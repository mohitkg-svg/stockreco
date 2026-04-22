import pandas as pd
import numpy as np
import ta
from ta.trend import SMAIndicator, EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import OnBalanceVolumeIndicator
from typing import Dict, Any, List


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to the dataframe. Returns enriched df."""
    if df.empty or len(df) < 20:
        return df

    d = df.copy()
    close = d["Close"]
    high = d["High"]
    low = d["Low"]
    volume = d["Volume"]

    # Moving Averages
    d["SMA_20"] = SMAIndicator(close, window=20, fillna=False).sma_indicator()
    d["SMA_50"] = SMAIndicator(close, window=50, fillna=False).sma_indicator()
    d["SMA_200"] = SMAIndicator(close, window=200, fillna=False).sma_indicator()
    d["EMA_9"] = EMAIndicator(close, window=9, fillna=False).ema_indicator()
    d["EMA_21"] = EMAIndicator(close, window=21, fillna=False).ema_indicator()
    d["EMA_50"] = EMAIndicator(close, window=50, fillna=False).ema_indicator()

    # Momentum
    d["RSI_14"] = RSIIndicator(close, window=14, fillna=False).rsi()
    stoch = StochasticOscillator(high, low, close, window=14, smooth_window=3, fillna=False)
    d["STOCHk_14"] = stoch.stoch()
    d["STOCHd_14"] = stoch.stoch_signal()

    # Trend
    macd_obj = MACD(close, window_slow=26, window_fast=12, window_sign=9, fillna=False)
    d["MACD_12_26"] = macd_obj.macd()
    d["MACDs_12_26"] = macd_obj.macd_signal()
    d["MACDh_12_26"] = macd_obj.macd_diff()

    if len(d) >= 14:
        adx_obj = ADXIndicator(high, low, close, window=14, fillna=False)
        d["ADX_14"] = adx_obj.adx()
        d["DMP_14"] = adx_obj.adx_pos()
        d["DMN_14"] = adx_obj.adx_neg()

    # Volatility
    bb = BollingerBands(close, window=20, window_dev=2, fillna=False)
    d["BBU_20"] = bb.bollinger_hband()
    d["BBL_20"] = bb.bollinger_lband()
    d["BBM_20"] = bb.bollinger_mavg()

    if len(d) >= 14:
        d["ATR_14"] = AverageTrueRange(high, low, close, window=14, fillna=False).average_true_range()

    # Volume
    d["OBV"] = OnBalanceVolumeIndicator(close, volume, fillna=False).on_balance_volume()
    d["VOL_SMA20"] = volume.rolling(20).mean()

    # Session-anchored VWAP (intraday only — daily/monthly bars get rolling).
    # For intraday timeframes the index has time-of-day, so we group by date
    # and cumsum per session — VWAP resets at each new trading day.
    # For daily+ bars we fall back to a rolling 20-bar VWAP since per-day
    # session anchoring is meaningless above the day boundary.
    try:
        typical = (high + low + close) / 3.0
        if hasattr(d.index, "date") and len(d) >= 2 and d.index[1] - d.index[0] < pd.Timedelta(days=1):
            grp = pd.Index(d.index.date)
            cum_vp = (typical * volume).groupby(grp).cumsum()
            cum_vol = volume.groupby(grp).cumsum().replace(0, np.nan)
            d["VWAP"] = cum_vp / cum_vol
        else:
            cum_vp = (typical * volume).rolling(20).sum()
            cum_vol = volume.rolling(20).sum().replace(0, np.nan)
            d["VWAP"] = cum_vp / cum_vol
    except Exception:
        d["VWAP"] = np.nan

    return d


def extract_latest(df: pd.DataFrame) -> Dict[str, Any]:
    """Extract the latest indicator values as a flat dict."""
    if df.empty:
        return {}

    row = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else row

    def safe(val):
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None
        try:
            if np.isnan(float(val)):
                return None
        except Exception:
            return None
        return float(val)

    result = {
        "close": safe(row["Close"]),
        "open": safe(row["Open"]),
        "high": safe(row["High"]),
        "low": safe(row["Low"]),
        "volume": safe(row["Volume"]),
        "sma20": safe(row.get("SMA_20")),
        "sma50": safe(row.get("SMA_50")),
        "sma200": safe(row.get("SMA_200")),
        "ema9": safe(row.get("EMA_9")),
        "ema21": safe(row.get("EMA_21")),
        "ema50": safe(row.get("EMA_50")),
        "rsi": safe(row.get("RSI_14")),
        "stoch_k": safe(row.get("STOCHk_14")),
        "stoch_d": safe(row.get("STOCHd_14")),
        "macd": safe(row.get("MACD_12_26")),
        "macd_signal": safe(row.get("MACDs_12_26")),
        "macd_hist": safe(row.get("MACDh_12_26")),
        "prev_macd_hist": safe(prev.get("MACDh_12_26")),
        "adx": safe(row.get("ADX_14")),
        "dmp": safe(row.get("DMP_14")),
        "dmn": safe(row.get("DMN_14")),
        "bb_upper": safe(row.get("BBU_20")),
        "bb_lower": safe(row.get("BBL_20")),
        "bb_mid": safe(row.get("BBM_20")),
        "atr": safe(row.get("ATR_14")),
        "vol_sma20": safe(row.get("VOL_SMA20")),
        "obv": safe(row.get("OBV")),
    }

    # Derived signals
    c = result["close"]
    if c:
        result["above_sma20"] = bool(result["sma20"] and c > result["sma20"])
        result["above_sma50"] = bool(result["sma50"] and c > result["sma50"])
        result["above_sma200"] = bool(result["sma200"] and c > result["sma200"])
        result["above_ema21"] = bool(result["ema21"] and c > result["ema21"])

    # MACD crossover (histogram flipping positive)
    if result["macd_hist"] is not None and result["prev_macd_hist"] is not None:
        result["macd_bullish_cross"] = result["macd_hist"] > 0 and result["prev_macd_hist"] <= 0
        result["macd_bearish_cross"] = result["macd_hist"] < 0 and result["prev_macd_hist"] >= 0
    else:
        result["macd_bullish_cross"] = False
        result["macd_bearish_cross"] = False

    # Volume surge
    if result["volume"] and result["vol_sma20"]:
        result["volume_surge"] = result["volume"] > 1.5 * result["vol_sma20"]
    else:
        result["volume_surge"] = False

    return result


def get_chart_indicator_series(df: pd.DataFrame) -> dict:
    """Return time-series data for charting each indicator."""
    if df.empty:
        return {}

    result = {}
    ts = [int(t.timestamp()) for t in df.index]

    def series(col):
        if col not in df.columns:
            return []
        vals = df[col].tolist()
        return [{"time": t, "value": round(float(v), 4) if v is not None and not np.isnan(v) else None}
                for t, v in zip(ts, vals)]

    for col, name in [("SMA_20", "SMA20"), ("SMA_50", "SMA50"), ("SMA_200", "SMA200"),
                      ("EMA_9", "EMA9"), ("EMA_21", "EMA21"),
                      ("BBU_20", "BB_Upper"), ("BBL_20", "BB_Lower")]:
        s = series(col)
        if s:
            result[name] = s

    return result


def get_rsi_series(df: pd.DataFrame) -> list:
    if "RSI_14" not in df.columns:
        return []
    ts = [int(t.timestamp()) for t in df.index]
    return [{"time": t, "value": round(float(v), 2) if not np.isnan(v) else None}
            for t, v in zip(ts, df["RSI_14"].tolist())]


def get_macd_series(df: pd.DataFrame) -> dict:
    ts = [int(t.timestamp()) for t in df.index]
    result = {}
    for col, name in [("MACD_12_26", "MACD"), ("MACDs_12_26", "Signal"), ("MACDh_12_26", "Histogram")]:
        if col in df.columns:
            result[name] = [{"time": t, "value": round(float(v), 4) if not np.isnan(v) else None}
                            for t, v in zip(ts, df[col].tolist())]
    return result
