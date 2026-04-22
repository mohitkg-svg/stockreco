"""
Central tunables — magic numbers extracted from across the services package.

Nothing here mutates per-request. If a value needs to be runtime-configurable,
move it to AutoTraderConfig (DB-backed). Everything in this file is a knob you
tweak in source and redeploy.
"""

# ---- Cache (data_fetcher) ---------------------------------------------------
# Hard cap on cached series; LRU-evict oldest on overflow. Prevents the dict
# from growing unbounded across thousands of (ticker, timeframe, source) keys.
DATA_CACHE_MAX_ENTRIES = 512

# ---- Auto-trader (auto_trader) ---------------------------------------------
# Window during which a duplicate signal (same ticker/type/levels) is suppressed.
IDEMPOTENCY_LOOKBACK_HOURS = 12
# Live-price fallback cache TTL — protects the Alpaca/Yahoo quote API from
# being hammered when manage loop and signal eval both want a fresh price.
PRICE_FALLBACK_TTL_SEC = 30.0

# ---- Backtester cost model -------------------------------------------------
# Round-trip drag in basis points. Applied as worse-fill on entry AND exit.
COMMISSION_BPS = 1.0   # broker take per side (paper: 0; conservative for sim)
SLIPPAGE_BPS = 5.0     # half-spread + impact per side

# ---- Signal generator regime gates -----------------------------------------
ADX_CHOP_MAX = 20.0    # below this = mean-reversion regime (flip breakout)
ADX_TREND_MIN = 25.0   # above this = trend-following regime (favor breakouts)

# ---- Stop calibration ------------------------------------------------------
# Per-timeframe ATR multiplier — wider on higher TFs because ATR_14 is in
# bar-units, so a 1d ATR is already a much bigger absolute move than a 5m ATR.
STOP_ATR_MULT_BY_TF = {
    "5m": 3.0, "15m": 2.8, "30m": 2.5,
    "1h": 2.2, "4h": 2.0, "1d": 2.0, "1mo": 1.8,
}

# ---- Multi-timeframe S/R weights -------------------------------------------
# When clustering swings across timeframes, higher-TF levels carry more weight.
MTF_WEIGHTS = {"1mo": 4, "1d": 3, "4h": 2, "1h": 1}
