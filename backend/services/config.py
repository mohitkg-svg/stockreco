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

# ---- Risk envelope (auto_trader) -------------------------------------------
# Confidence-risk multiplier ramps from 1.0 at threshold to this at 100% conf.
RISK_MAX_CONFIDENCE_MULT = 1.75
# Kelly-criterion multiplier cap. Tightened from 1.35 to 1.2 during the
# pre-live phase: with <100 closed trades we don't trust bucket win-rates
# enough to let Kelly size up aggressively. Raise back to 1.35 once
# calibration is validated against 100+ realized outcomes.
RISK_KELLY_MAX_MULT = 1.2
# Below this historical win rate, don't trust the bucket — force kelly_mult=1.0.
RISK_KELLY_MIN_WIN_RATE = 55.0
# Hard ceiling on the COMPOUND risk multiplier
# (conf × kelly × cal × strategy × VIX). Critical-audit fix #1: stack was
# producing 4-5× risk in rare edge cases.
RISK_MULT_CEILING = 2.0
# Unrealized drawdown threshold: above this fraction of equity, halve sizing.
RISK_PORTFOLIO_HEAT_CAP_PCT = 0.10
# Slippage tolerances vs ATR — shift entry by this much, reject if worse.
RISK_SLIPPAGE_SHIFT_ATR = 0.3
RISK_SLIPPAGE_REJECT_ATR = 1.0

# ---- ML scorer envelope (ml_scorer) ----------------------------------------
# P(win) → confidence-multiplier mapping. Tight envelope because v1 trains
# on synthetic backtest labels, not live trades.
ML_MULT_HIGH = 1.12      # P(win) >= 0.70
ML_MULT_LIFT = 1.06      # P(win) >= 0.60
ML_MULT_NEUTRAL = 1.00   # 0.45 ≤ P < 0.60
ML_MULT_DAMP = 0.94      # 0.35 ≤ P < 0.45
ML_MULT_LOW = 0.88       # P(win) < 0.35

# ---- LLM chat (routers/chat) -----------------------------------------------
# Centralized so model upgrades happen in one place. Downgrade to
# claude-haiku-4-5 to cut chat token cost ~5× at the expense of nuance.
CHAT_MODEL = "claude-opus-4-7"
CHAT_MAX_TOKENS = 8000

# ---- AI judge — entry veto, exit decision, confidence multiplier -----------
# Defaults to Haiku (fast + cheap) — the AI judge is in the trade-decision
# critical path so latency matters. Override to Opus only if accuracy of
# semantic judgments measurably trails (track in shadow mode for ≥ 200
# entries before switching).
AI_JUDGE_MODEL = "claude-haiku-4-5-20251001"
AI_JUDGE_MAX_TOKENS = 512
# Latency budget per call. Trade is held in `consider_signal` for at most
# this long; on timeout we abstain (proceed without veto).
AI_JUDGE_TIMEOUT_SEC = 5.0
# AI confidence multiplier is a downward-bias range — symmetric envelope
# would let "AI loves it" double the bet; not what we want from an LLM.
# Stack ceiling RISK_MULT_CEILING already caps everything at 2.0×.
AI_MULT_MIN = 0.6
AI_MULT_MAX = 1.4
