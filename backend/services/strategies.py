"""
Strategy definitions. Each strategy returns a dict:
  {
    "name": str,
    "description": str,
    "regime": "trend" | "chop" | "any",   # r42 fix #1.9
    "entry_long": pd.Series[bool],   # True = go long on next bar
    "entry_short": pd.Series[bool],  # True = go short on next bar
  }

`regime` declares which ADX regime the strategy is designed for:
  * "trend": only fires when ADX_14 ≥ ADX_TREND_MIN (typ. 25)
  * "chop":  only fires when ADX_14 ≤ ADX_CHOP_MAX (typ. 20)
  * "any":   regime-agnostic

The backtester applies these gates inside `all_strategies(d)` so we no
longer have a chop strategy and a trend strategy voting BUY+SELL on the
same bar. r42 fix #1.9.

The backtester consumes these and simulates trades with ATR-based stop/target.
"""
import pandas as pd
import numpy as np
from typing import Dict, Callable, List


def _trend_following(d: pd.DataFrame) -> Dict:
    """Long when price is above SMA50 AND momentum (RSI) is in the
    healthy 50–70 zone AND MACD histogram just flipped positive.
    Mirror conditions for shorts. The MACD-histogram-cross gate ensures
    we only enter on the FIRST bar of momentum confirmation, not every
    bar a trend persists."""
    long = (
        (d["Close"] > d["SMA_50"])
        & (d["RSI_14"] > 50) & (d["RSI_14"] < 70)
        & (d["MACDh_12_26"] > 0) & (d["MACDh_12_26"].shift(1) <= 0)
    )
    short = (
        (d["Close"] < d["SMA_50"])
        & (d["RSI_14"] < 50) & (d["RSI_14"] > 30)
        & (d["MACDh_12_26"] < 0) & (d["MACDh_12_26"].shift(1) >= 0)
    )
    return {
        "name": "Trend Following",
        "description": "Price above SMA50, RSI 50-70, MACD histogram flips positive",
        "regime": "trend",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _golden_cross(d: pd.DataFrame) -> Dict:
    """Major-trend-reversal signal: SMA50 crosses above SMA200 (golden
    cross) → long; below (death cross) → short. Slow-moving — fires
    a few times per year per ticker — but historically high R:R."""
    long = (d["SMA_50"] > d["SMA_200"]) & (d["SMA_50"].shift(1) <= d["SMA_200"].shift(1))
    short = (d["SMA_50"] < d["SMA_200"]) & (d["SMA_50"].shift(1) >= d["SMA_200"].shift(1))
    return {
        "name": "Golden/Death Cross",
        "description": "SMA50 crosses SMA200 (major trend reversal)",
        "regime": "trend",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _rsi_mean_reversion(d: pd.DataFrame) -> Dict:
    """Oversold-bounce long: RSI crosses UP through 30 while price is
    above the SMA200 (i.e., dip in an uptrend). Mirror for shorts: RSI
    crosses DOWN through 70 in a downtrend. The SMA200 trend-direction
    filter prevents catching falling-knife mean-reversion attempts."""
    long = (d["RSI_14"] > 30) & (d["RSI_14"].shift(1) <= 30) & (d["Close"] > d["SMA_200"])
    short = (d["RSI_14"] < 70) & (d["RSI_14"].shift(1) >= 70) & (d["Close"] < d["SMA_200"])
    return {
        "name": "RSI Mean Reversion",
        "description": "Oversold bounce (RSI crosses up through 30) in uptrend; inverse for shorts",
        "regime": "chop",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _macd_crossover(d: pd.DataFrame) -> Dict:
    """MACD line crosses signal line — classic momentum-rotation
    trigger. No trend-direction filter (works in both directions),
    making it noisier than `_trend_following` but faster to react."""
    long = (d["MACD_12_26"] > d["MACDs_12_26"]) & (d["MACD_12_26"].shift(1) <= d["MACDs_12_26"].shift(1))
    short = (d["MACD_12_26"] < d["MACDs_12_26"]) & (d["MACD_12_26"].shift(1) >= d["MACDs_12_26"].shift(1))
    return {
        "name": "MACD Crossover",
        "description": "MACD line crosses signal line",
        "regime": "any",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _bollinger_breakout(d: pd.DataFrame) -> Dict:
    """Volatility-expansion breakout: close pierces the Bollinger upper
    (long) or lower (short) band on at least 1.2× the 20-bar average
    volume. Volume filter rejects the no-conviction breakouts that
    typically reverse within a few bars."""
    vol_ok = d["Volume"] > 1.2 * d["VOL_SMA20"]
    long = (d["Close"] > d["BBU_20"]) & (d["Close"].shift(1) <= d["BBU_20"].shift(1)) & vol_ok
    short = (d["Close"] < d["BBL_20"]) & (d["Close"].shift(1) >= d["BBL_20"].shift(1)) & vol_ok
    return {
        "name": "Bollinger Breakout",
        "description": "Close breaks outside Bollinger Band with volume confirmation",
        "regime": "trend",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _donchian_breakout(d: pd.DataFrame) -> Dict:
    """Long on the FIRST close above the trailing 20-bar high (mirror
    for shorts). The crossover guard (`shift(1) <= ...`) ensures one
    entry per breakout, not one per confirmation bar — without it the
    same trend leg fires dozens of duplicate signals.

    Audit-fix lineage: C4. Inspired by the classic Turtle Trading rule.
    """
    high_20 = d["High"].rolling(20).max().shift(1)
    low_20 = d["Low"].rolling(20).min().shift(1)
    long = (d["Close"] > high_20) & (d["Close"].shift(1) <= high_20.shift(1))
    short = (d["Close"] < low_20) & (d["Close"].shift(1) >= low_20.shift(1))
    return {
        "name": "Donchian Breakout",
        "description": "First close above 20-bar high (long) or below 20-bar low (short) — true breakout, not continuation",
        "regime": "trend",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _ema_pullback(d: pd.DataFrame) -> Dict:
    """Trend-continuation pullback: in an uptrend (SMA50 > SMA200),
    buy bars whose Low touched the EMA21 but closed back above it.
    The "touched and recovered" pattern is the classic
    healthy-pullback-in-trend signature. Mirror logic for shorts."""
    uptrend = d["SMA_50"] > d["SMA_200"]
    downtrend = d["SMA_50"] < d["SMA_200"]
    touched_ema_below = (d["Low"] <= d["EMA_21"]) & (d["Close"] > d["EMA_21"])
    touched_ema_above = (d["High"] >= d["EMA_21"]) & (d["Close"] < d["EMA_21"])
    long = (uptrend & touched_ema_below).fillna(False)
    short = (downtrend & touched_ema_above).fillna(False)
    return {
        "name": "EMA Pullback",
        "description": "In trend, buy pullback that bounces off EMA21",
        "regime": "trend",
        "entry_long": long,
        "entry_short": short,
    }


def _opening_reversal(d: pd.DataFrame) -> Dict:
    """r46 Tier P: Opening Reversal after large gap.

    Heston/Korajczyk/Sadka (2010) and follow-up: first 30 minutes of
    trading after a >2% gap mean-revert ~55-60% of the time when the gap
    isn't catalyst-driven. We fade the gap on the first session bar that
    prints a lower high (gap-up) or higher low (gap-down).

    Regime: chop. Best fit: intraday TFs (5m/15m/30m).
    """
    prev_close = d["Close"].shift(1)
    gap_pct = (d["Open"] - prev_close) / prev_close
    gap_up = gap_pct > 0.02
    gap_dn = gap_pct < -0.02
    # First-bar reversal pattern: gap-up + lower high vs prior bar = exhaustion
    lower_high = d["High"] < d["High"].shift(1)
    higher_low = d["Low"] > d["Low"].shift(1)
    long_e = gap_dn & higher_low & (d["Close"] > d["Open"])
    short_e = gap_up & lower_high & (d["Close"] < d["Open"])
    return {
        "name": "Opening Reversal",
        "description": "Fade ≥ 2% gap on first-bar exhaustion (lower high / higher low + close-against-open)",
        "regime": "chop",
        "entry_long": long_e.fillna(False),
        "entry_short": short_e.fillna(False),
    }


def _last_30min_momentum(d: pd.DataFrame) -> Dict:
    """r46 Tier P: Last-30-min momentum predicts next-day open.

    Heston-Korajczyk-Sadka (2010), Bogousslavsky (2021): closing strength
    (final 30-min vs session VWAP, with elevated volume) predicts next-
    day open with R²~5%. Buy at close, hold to next-day open.

    Regime: any. Intraday-bar specific; on daily df this no-ops.
    """
    empty = pd.Series(False, index=d.index)
    try:
        if len(d) < 4 or (d.index[1] - d.index[0]) >= pd.Timedelta(days=1):
            return {"name": "Last-30min Momentum", "description": "—", "regime": "any",
                    "entry_long": empty, "entry_short": empty}
    except Exception:
        return {"name": "Last-30min Momentum", "description": "—", "regime": "any",
                "entry_long": empty, "entry_short": empty}
    if "VWAP" not in d.columns:
        return {"name": "Last-30min Momentum", "description": "no VWAP", "regime": "any",
                "entry_long": empty, "entry_short": empty}
    grp = pd.Index(d.index.date)
    bar_idx = pd.Series(range(len(d)), index=d.index).groupby(grp).cumcount()
    n_per_session = pd.Series(range(len(d)), index=d.index).groupby(grp).transform("count")
    is_last_30 = (n_per_session - bar_idx) <= 6   # last ~30 min on 5m bars
    vol_ok = d["Volume"] > 1.5 * d["VOL_SMA20"] if "VOL_SMA20" in d.columns else (d["Volume"] > 0)
    long_e = is_last_30 & (d["Close"] > d["VWAP"]) & vol_ok
    short_e = is_last_30 & (d["Close"] < d["VWAP"]) & vol_ok
    return {
        "name": "Last-30min Momentum",
        "description": "Last 30-min strength vs VWAP + 1.5× volume = next-day continuation",
        "regime": "any",
        "entry_long": long_e.fillna(False),
        "entry_short": short_e.fillna(False),
    }


def _news_spike_fade(d: pd.DataFrame) -> Dict:
    """r46 Tier P: After-news-spike fade.

    Tetlock (2007 JF) + retail-attention literature: ~80% of headline-
    driven intraday spikes >3% retrace 50% within 60 minutes when the
    news lacks fundamental substance. We approximate "spike" via 1×ATR
    move + RVOL>3 within a single bar; signal_generator can layer the
    news-timestamp filter on top.

    Regime: chop.
    """
    if "ATR_14" not in d.columns or "VOL_SMA20" not in d.columns:
        empty = pd.Series(False, index=d.index)
        return {"name": "News Spike Fade", "description": "—", "regime": "chop",
                "entry_long": empty, "entry_short": empty}
    bar_range = (d["High"] - d["Low"]).abs()
    big_bar = bar_range >= 1.5 * d["ATR_14"]
    rvol = d["Volume"] / d["VOL_SMA20"]
    rvol_spike = rvol >= 3.0
    # Fade direction: bar closed at the extreme = exhaustion
    bar_color_up = d["Close"] > d["Open"]
    bar_color_dn = d["Close"] < d["Open"]
    long_e = big_bar & rvol_spike & bar_color_dn   # huge red bar with spike → fade up
    short_e = big_bar & rvol_spike & bar_color_up   # huge green bar with spike → fade down
    return {
        "name": "News Spike Fade",
        "description": "Fade ≥ 1.5×ATR bar with RVOL ≥ 3 (mean-reversion of spike)",
        "regime": "chop",
        "entry_long": long_e.fillna(False),
        "entry_short": short_e.fillna(False),
    }


def _gap_fill(d: pd.DataFrame) -> Dict:
    """
    Gap-and-fill mean-reversion. After a session gap, price often retraces to fill it:
      • Gap down (Open < prev Low) in an uptrend (Close > SMA_50) → long, target = prev Close (fill)
      • Gap up   (Open > prev High) in a downtrend (Close < SMA_50) → short, target = prev Close (fill)
    Entry triggers on the gap bar itself; backtester enters at next bar's Open.
    """
    prev_high = d["High"].shift(1)
    prev_low = d["Low"].shift(1)
    gap_down = d["Open"] < prev_low
    gap_up = d["Open"] > prev_high
    long = (gap_down & (d["Close"] > d["SMA_50"])).fillna(False)
    short = (gap_up & (d["Close"] < d["SMA_50"])).fillna(False)
    return {
        "name": "Gap Fill",
        "description": "Fade overnight gaps that print against the prevailing trend (gap-down in uptrend → long; gap-up in downtrend → short)",
        "regime": "chop",
        "entry_long": long,
        "entry_short": short,
    }


def _fvg_pullback(d: pd.DataFrame) -> Dict:
    """
    Fair-Value-Gap pullback: enter on the first bar that taps an unfilled
    bullish FVG (long) or bearish FVG (short). Approximated bar-wise by
    detecting a 3-bar imbalance and triggering entry when the *current* bar's
    range overlaps the imbalance zone.

    Bullish FVG at i-2: Low[i-1] > High[i-3]  → zone = (High[i-3], Low[i-1])
    Trigger: bar i Low <= zone_top (price pulled back into the zone)
    """
    H = d["High"]
    L = d["Low"]
    O = d["Open"]
    C = d["Close"]
    # FVG forms on bar i-2 (using bars i-3 and i-1)
    bull_zone_top = L.shift(1)               # = Low[i-1]
    bull_zone_bot = H.shift(3)               # = High[i-3]
    has_bull_fvg = bull_zone_top > bull_zone_bot
    # Rejection candle filter: when price taps the FVG, require the bar to
    # close BULLISH (above its open) — proves buyers stepped in instead of
    # passively drifting through the zone. Cuts ~30% of false entries.
    bull_rejection = C > O
    long = (
        has_bull_fvg
        & (L <= bull_zone_top)
        & (C > bull_zone_bot)                 # didn't fully fill yet
        & bull_rejection                      # closed green at the tap
        & (C > d["SMA_50"])                   # only buy in trend
    ).fillna(False)

    bear_zone_top = L.shift(3)               # = Low[i-3]
    bear_zone_bot = H.shift(1)               # = High[i-1]
    has_bear_fvg = bear_zone_top > bear_zone_bot
    bear_rejection = C < O
    short = (
        has_bear_fvg
        & (H >= bear_zone_bot)
        & (C < bear_zone_top)
        & bear_rejection
        & (C < d["SMA_50"])
    ).fillna(False)

    return {
        "name": "FVG Pullback",
        "description": "Enter on first retrace into an unfilled fair-value gap (3-bar imbalance) in the direction of the trend",
        "regime": "trend",
        "entry_long": long,
        "entry_short": short,
    }


def _gap_and_go(d: pd.DataFrame) -> Dict:
    """
    Gap-and-Go (continuation, NOT fade). Opposite of _gap_fill: when price gaps
    in the *direction* of the prevailing trend AND the open holds (close above
    open for gap-ups, below for gap-downs), the gap acts as a launchpad rather
    than a magnet to fill. Volume confirmation required.

      • Gap up (Open > prev High) in uptrend (Close > SMA_50) AND close > open
        AND volume > 1.5×SMA20 → long
      • Gap down (Open < prev Low) in downtrend (Close < SMA_50) AND close < open
        AND volume > 1.5×SMA20 → short
    """
    prev_high = d["High"].shift(1)
    prev_low = d["Low"].shift(1)
    vol_surge = d["Volume"] > 1.5 * d["VOL_SMA20"]
    gap_up_hold = (d["Open"] > prev_high) & (d["Close"] > d["Open"]) & (d["Close"] > d["SMA_50"]) & vol_surge
    gap_dn_hold = (d["Open"] < prev_low)  & (d["Close"] < d["Open"]) & (d["Close"] < d["SMA_50"]) & vol_surge
    return {
        "name": "Gap & Go",
        "description": "Trend-aligned gap that holds (close past open, volume>1.5× avg) — launchpad continuation, not fade",
        "regime": "trend",
        "entry_long": gap_up_hold.fillna(False),
        "entry_short": gap_dn_hold.fillna(False),
    }


def _vwap_reclaim(d: pd.DataFrame) -> Dict:
    """
    VWAP reclaim: price closes back above (long) or below (short) VWAP after
    spending at least one bar on the wrong side. Classical institutional
    pivot — VWAP is where the average buyer's cost basis sits intraday.

      Long  : Close[i] > VWAP[i] AND Close[i-1] <= VWAP[i-1]
              AND prevailing trend is up (Close > SMA_50) AND volume not weak
      Short : Close[i] < VWAP[i] AND Close[i-1] >= VWAP[i-1]
              AND prevailing trend is down AND volume not weak

    NB: requires the VWAP column to exist; on daily+ bars this is a rolling
    proxy, on intraday it's session-anchored (resets per day).
    """
    if "VWAP" not in d.columns:
        # No-op strategy on dataframes without VWAP
        empty = pd.Series(False, index=d.index)
        return {"name": "VWAP Reclaim", "description": "—", "regime": "any", "entry_long": empty, "entry_short": empty}
    vol_ok = d["Volume"] > 0.8 * d["VOL_SMA20"]
    long = (
        (d["Close"] > d["VWAP"]) & (d["Close"].shift(1) <= d["VWAP"].shift(1))
        & (d["Close"] > d["SMA_50"]) & vol_ok
    )
    short = (
        (d["Close"] < d["VWAP"]) & (d["Close"].shift(1) >= d["VWAP"].shift(1))
        & (d["Close"] < d["SMA_50"]) & vol_ok
    )
    return {
        "name": "VWAP Reclaim",
        "description": "Price reclaims VWAP after at least one bar on the wrong side, in trend with normal volume",
        "regime": "any",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _opening_range_breakout(d: pd.DataFrame) -> Dict:
    """
    Opening-Range Breakout (ORB). On intraday data, define each session's
    opening range as the first N bars; subsequent bars that close beyond the
    OR high (long) or low (short) on volume trigger.

    On daily+ bars there's no meaningful "opening range" — the strategy
    no-ops cleanly (returns empty entry series).

      Long  : Close > OR_high AND prior bar Close <= OR_high AND vol > 1.2× SMA20
      Short : Close < OR_low  AND prior bar Close >= OR_low  AND vol > 1.2× SMA20

    OR window = first 3 bars of each session.
    """
    empty = pd.Series(False, index=d.index)
    # Bail on non-intraday bars (heuristic: median diff < 1 day)
    try:
        if len(d) < 4 or (d.index[1] - d.index[0]) >= pd.Timedelta(days=1):
            return {"name": "Opening Range Breakout", "description": "—", "regime": "trend",
                    "entry_long": empty, "entry_short": empty}
    except Exception:
        return {"name": "Opening Range Breakout", "description": "—", "regime": "trend",
                "entry_long": empty, "entry_short": empty}

    OR_BARS = 3
    grp = pd.Index(d.index.date)
    # bar position within each session (0-indexed)
    bar_idx = pd.Series(range(len(d)), index=d.index).groupby(grp).cumcount()
    # Audit fix C3: use transform("max")/transform("min") so the OR high/low
    # is a single STABLE value broadcast to every bar of the session. The old
    # cummax().ffill() approach produced an evolving value within the OR
    # window itself (bar 1's or_high only saw bar 0, bar 2 only saw bars 0-1);
    # after_or gated that out but it was fragile — a schema-compliant full-
    # session max is the correct semantic for Opening Range.
    or_high_src = d["High"].where(bar_idx < OR_BARS)
    or_low_src = d["Low"].where(bar_idx < OR_BARS)
    or_high = or_high_src.groupby(grp).transform("max")
    or_low = or_low_src.groupby(grp).transform("min")
    # Only fire OUTSIDE the OR window itself.
    after_or = bar_idx >= OR_BARS
    vol_ok = d["Volume"] > 1.2 * d["VOL_SMA20"]
    long = after_or & (d["Close"] > or_high) & (d["Close"].shift(1) <= or_high.shift(1)) & vol_ok
    short = after_or & (d["Close"] < or_low) & (d["Close"].shift(1) >= or_low.shift(1)) & vol_ok
    return {
        "name": "Opening Range Breakout",
        "description": "First close beyond the session's opening-range high/low (3-bar OR) on volume>1.2× avg",
        "regime": "trend",
        "entry_long": long.fillna(False),
        "entry_short": short.fillna(False),
    }


def _nr7_breakout(d: pd.DataFrame) -> Dict:
    """r44 Wave 7 — NR7 (Narrow-Range-of-7) volatility breakout.

    Crabel (1990); Connors empirical: a bar whose range is the smallest
    of the last 7 precedes ≥1×ATR expansion 65-70% of the time. We enter
    on the FIRST close beyond the NR7 bar's High/Low with volume
    confirmation.

    Regime: any (volatility-compression precursor — fires before the
    regime classifier sees the breakout).
    """
    rng = d["High"] - d["Low"]
    is_nr7 = rng == rng.rolling(7, min_periods=7).min()
    is_nr7_prev = is_nr7.shift(1).fillna(False)
    vol_ok = d["Volume"] > 1.2 * d["VOL_SMA20"]
    long_e = is_nr7_prev & (d["Close"] > d["High"].shift(1)) & vol_ok
    short_e = is_nr7_prev & (d["Close"] < d["Low"].shift(1)) & vol_ok
    return {
        "name": "NR7 Breakout",
        "description": "First close beyond the NR7 (narrowest of 7) bar's H/L with volume>1.2× avg",
        "regime": "any",
        "entry_long": long_e.fillna(False),
        "entry_short": short_e.fillna(False),
    }


def _inside_bar_breakout(d: pd.DataFrame) -> Dict:
    """r44 Wave 7 — Inside-bar continuation breakout. Bar fully contained
    inside prior bar's range (high < prev_high AND low > prev_low) signals
    consolidation; the FIRST close beyond the prior bar's H/L is a high-
    probability continuation trigger.
    """
    prev_h = d["High"].shift(1)
    prev_l = d["Low"].shift(1)
    inside = (d["High"].shift(1) < prev_h.shift(1)) & (d["Low"].shift(1) > prev_l.shift(1))
    long_e = inside & (d["Close"] > prev_h)
    short_e = inside & (d["Close"] < prev_l)
    return {
        "name": "Inside Bar Breakout",
        "description": "First close beyond a prior-bar inside bar's range (continuation)",
        "regime": "any",
        "entry_long": long_e.fillna(False),
        "entry_short": short_e.fillna(False),
    }


def _high52_proximity(d: pd.DataFrame) -> Dict:
    """r44 Wave 7 — 52-week-high proximity momentum (George & Hwang 2004).
    Long when price is within 5% of the 252-bar high AND ADX ≥ 25 (trend).
    """
    hi52 = d["High"].rolling(252, min_periods=120).max()
    near_hi = d["Close"] >= 0.95 * hi52
    adx_ok = d["ADX_14"] >= 25 if "ADX_14" in d.columns else pd.Series(True, index=d.index)
    long_e = near_hi & adx_ok & (d["Close"] > d["Close"].shift(1))
    empty = pd.Series(False, index=d.index)
    return {
        "name": "52w High Proximity",
        "description": "Within 5% of 252-bar high in trend regime (ADX ≥ 25)",
        "regime": "trend",
        "entry_long": long_e.fillna(False),
        "entry_short": empty,
    }


STRATEGY_FUNCS: List[Callable[[pd.DataFrame], Dict]] = [
    _trend_following,
    _golden_cross,
    _rsi_mean_reversion,
    _macd_crossover,
    _bollinger_breakout,
    _donchian_breakout,
    _ema_pullback,
    _gap_fill,
    _gap_and_go,
    _fvg_pullback,
    _vwap_reclaim,
    _opening_range_breakout,
    # r44 Wave 7 additions:
    _nr7_breakout,
    _inside_bar_breakout,
    _high52_proximity,
    # r46 Tier P additions:
    _opening_reversal,
    _last_30min_momentum,
    _news_spike_fade,
]


def all_strategies(d: pd.DataFrame) -> List[Dict]:
    """Build all strategies against an indicator-enriched dataframe.

    r42 fix #1.9: regime-gate per strategy. The current ADX_14 value at the
    last bar of `d` decides which strategies' entries survive — chop
    strategies are zero'd out on trending bars and vice versa. "any"-regime
    strategies always pass through. The gate is bar-level (uses ADX_14
    series), so a single dataframe can have alternating regimes correctly
    handled: the per-bar mask is multiplied against entry_long/short.
    """
    from services.config import ADX_TREND_MIN, ADX_CHOP_MAX
    has_adx = "ADX_14" in d.columns
    out = []
    for fn in STRATEGY_FUNCS:
        try:
            s = fn(d)
        except Exception:
            continue
        regime = s.get("regime", "any")
        if has_adx and regime in ("trend", "chop"):
            adx = d["ADX_14"]
            if regime == "trend":
                mask = (adx >= ADX_TREND_MIN).fillna(False)
            else:
                mask = (adx <= ADX_CHOP_MAX).fillna(False)
            s["entry_long"] = (s["entry_long"] & mask).fillna(False)
            s["entry_short"] = (s["entry_short"] & mask).fillna(False)
        out.append(s)
    return out
