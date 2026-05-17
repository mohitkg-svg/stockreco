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


def _high52_proximity(d: pd.DataFrame) -> Dict:
    """52-week-high proximity momentum (George & Hwang 2004).

    r48 BACKLOG #backtest-F22: prior code fired on every up-day in the
    proximity window — too many duplicate signals. Add a 5-bar cooldown
    after firing to space out entries.
    """
    hi52 = d["High"].rolling(252, min_periods=120).max()
    near_hi = d["Close"] >= 0.95 * hi52
    adx_ok = d["ADX_14"] >= 25 if "ADX_14" in d.columns else pd.Series(True, index=d.index)
    raw_long = near_hi & adx_ok & (d["Close"] > d["Close"].shift(1))
    # Apply a 5-bar cooldown
    long_e = raw_long.copy()
    last_fired = -1000
    out_idx = list(raw_long.index)
    for i, idx in enumerate(out_idx):
        if bool(raw_long.loc[idx]):
            if i - last_fired < 5:
                long_e.loc[idx] = False
            else:
                last_fired = i
    empty = pd.Series(False, index=d.index)
    return {
        "name": "52w High Proximity",
        "description": "Within 5% of 252-bar high in trend regime (ADX ≥ 25), 5-bar cooldown",
        "regime": "trend",
        "entry_long": long_e.fillna(False),
        "entry_short": empty,
    }


def _lev_etf_decay_short(d: pd.DataFrame) -> Dict:
    """r48 BACKLOG #C3: leveraged ETF decay short on choppy tape.
    Cheng-Madhavan (JID 2009): daily-rebalanced leveraged ETFs decay in
    high-vol/low-trend regimes due to volatility drag. Trade: short
    leveraged ETF (or buy inverse) when 20d realized-vol > 75th pctile
    AND ADX < 20 (chop). Whitelist enforced at signal_generator level.
    """
    empty = pd.Series(False, index=d.index)
    if "ADX_14" not in d.columns or len(d) < 60:
        return {
            "name": "Lev-ETF Decay Short",
            "description": "Short leveraged ETF in chop regime",
            "regime": "chop",
            "entry_long": empty, "entry_short": empty,
        }
    rv20 = d["Close"].pct_change().rolling(20).std() * (252 ** 0.5)
    # r97: rolling 252-bar percentile rank (was rank(pct=True) — global rank
    # used the full series, leaking future bars into the historical signal).
    rv_pct = rv20.rolling(252, min_periods=60).apply(
        lambda x: (x[-1] >= x).sum() / len(x),
        raw=True,
    )
    chop = (d["ADX_14"] < 20) & (rv_pct >= 0.75)
    short_e = chop & (d["Close"] < d["Close"].shift(1))
    return {
        "name": "Lev-ETF Decay Short",
        "description": "Short lev-ETF when RV>75pct AND ADX<20 (chop)",
        "regime": "chop",
        "entry_long": empty,
        "entry_short": short_e.fillna(False),
    }


def _vix_spike_reversion(d: pd.DataFrame) -> Dict:
    """r47 Tier P — VIX 5σ spike → SPY/QQQ long mean-reversion.

    Mechanism: Bollerslev-Tauchen-Zhou (RFS 2009) variance-risk-premium
    spikes mean-revert; daily VIX shocks above 5σ of trailing-60d are
    dominated by panic dealer hedging and historically underperform their
    next-3-day implied magnitude. Trade: long index ETF the morning after
    a 5σ spike when VIX absolute level >= 25. Exit at +3d hold or 30%
    VIX retrace.

    The strategy is keyed on the underlying ETF's daily bars; it reads VIX
    history via cross_asset for the trigger condition. Only fires for
    SPY/QQQ universe by convention (caller filters in signal_generator
    via the `regime` field).
    """
    empty = pd.Series(False, index=d.index)
    long_e = pd.Series(False, index=d.index)
    # r82: docstring claims caller filters by ticker via the `regime` field,
    # but no such filter exists in signal_generator. Without the gate, every
    # ticker in the watchlist fires a long on its last bar during a VIX
    # spike — mass long entries on a market panic day. Hard-code the
    # whitelist here so the safety can't be lost by upstream refactors.
    _SPX_TICKERS = {"SPY", "QQQ", "IVV", "VOO"}
    _ticker = (d.attrs.get("ticker") if hasattr(d, "attrs") else None) or ""
    if str(_ticker).upper() not in _SPX_TICKERS:
        return {
            "name": "VIX 5σ Spike Reversion",
            "description": "Long SPY/QQQ next session after VIX +5σ spike (filtered to index ETFs)",
            "regime": "any",
            "entry_long": empty, "entry_short": empty,
        }
    try:
        from services.data_fetcher import fetch_ohlcv
        vix_df = fetch_ohlcv("^VIX", "1d")
        if vix_df is not None and not vix_df.empty:
            # Compute historical 5-sigma spikes cleanly against the VIX dataframe
            v_closes = vix_df["Close"]
            changes = v_closes.diff()
            mu = changes.rolling(60).mean()
            sigma = changes.rolling(60).std()
            
            # z-score of the change
            z = (changes - mu) / sigma
            
            # Find all dates where a spike occurred
            spike_dates = vix_df.index[(z >= 5.0) & (v_closes >= 25.0)]
            
            for sd in spike_dates:
                future_bars = d.index[d.index > sd]
                if len(future_bars) > 0:
                    # Enter on the open of the immediately following session
                    long_e.loc[future_bars[0]] = True
    except Exception:
        pass
    return {
        "name": "VIX 5σ Spike Reversion",
        "description": "Long SPY/QQQ next session after VIX +5σ spike (Bollerslev-Tauchen-Zhou)",
        "regime": "any",
        "entry_long": long_e.fillna(False),
        "entry_short": empty,
    }


def _inside_bar_breakout(d: pd.DataFrame) -> Dict:
    """r47 #T0c-4: Inside bar breakout.
    
    A bar is 'inside' if its high is lower than the previous bar's high and 
    its low is higher than the previous bar's low. We enter when price breaks
    the *parent* bar's high (long) or low (short).
    """
    parent_high = d["High"].shift(2)
    parent_low = d["Low"].shift(2)
    inside = (d["High"].shift(1) < parent_high) & (d["Low"].shift(1) > parent_low)
    long_e = inside & (d["Close"] > parent_high)
    short_e = inside & (d["Close"] < parent_low)
    return {
        "name": "Inside Bar Breakout",
        "description": "Breakout past the parent bar of an inside bar",
        "regime": "any",
        "entry_long": long_e.fillna(False),
        "entry_short": short_e.fillna(False),
    }


# QUANT REVISION: Orthogonal (uncorrelated) features. 
# Discarding multicollinear technical indicators. Keeping ONE mean-reversion factor, 
# and leaning into alternative data and structurally sound setups.
STRATEGY_FUNCS: List[Callable[[pd.DataFrame], Dict]] = [
    _gap_and_go,
    _vwap_reclaim,
    _vix_spike_reversion,
    _high52_proximity,
    _lev_etf_decay_short,
    _opening_reversal,
    _last_30min_momentum,
    _inside_bar_breakout,
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
