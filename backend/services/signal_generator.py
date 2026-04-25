import json
from typing import Dict, Any, List, Optional
from services.indicators import extract_latest
from services.support_resistance import pivot_points, swing_levels, nearest_support_resistance, classify_levels_relative_to_price, multi_timeframe_levels
from services.pattern_detector import detect_patterns
from services.supply_demand import detect_zones, nearest_demand_below, nearest_supply_above, in_zone
from services.fibonacci import compute_fib_levels, fib_supports_below, fib_resistances_above, near_key_fib
from services.gap_detector import (
    unfilled_gaps,
    gap_targets_above,
    gap_targets_below,
    support_gaps_below,
    resistance_gaps_above,
    in_gap,
    gap_patterns,
)
import pandas as pd
import numpy as np


# Tunables live in services/config.py — re-exported here under the
# legacy names so existing call sites keep working.
from services.config import (
    STOP_ATR_MULT_BY_TF as _STOP_ATR_MULTS,
    ADX_CHOP_MAX,
    ADX_TREND_MIN,
)


def _stop_atr_mult(timeframe: str) -> float:
    return _STOP_ATR_MULTS.get(timeframe, 2.0)


def _calibrate_long_stop(
    *,
    price: float,
    atr: float,
    df: pd.DataFrame,
    candidates: List[Optional[float]],
    timeframe: str,
) -> float:
    """
    Pick a long-side stop that:
      • Is the SECOND-tightest valid candidate (drops the noisiest level)
      • Sits at least cfg×ATR below price (timeframe-dependent multiplier)
      • Sits below the most recent 5-bar swing low (structural buffer)
    The two structural guards are MIN-clamps — they only WIDEN the stop, never
    tighten it. Falls back to ATR-only if no structural candidates survive.
    """
    mult = _stop_atr_mult(timeframe)
    atr_floor = price - mult * atr

    # 5-bar swing low — small buffer below to avoid stop-hunt wicks.
    # Postmortem fix H6: clamp distance to ≤ 3×ATR. After a 10% intraday
    # flush + recovery, an unclamped 5-bar low can sit 10%+ below current
    # price; the resulting risk-per-share collapses position sizing to 1
    # share or zero and silently kills the entry on volatile names.
    try:
        swing_lo_5 = float(df["Low"].iloc[-5:].min()) * 0.997
        swing_lo_5 = max(swing_lo_5, price - 3.0 * atr)
    except Exception:
        swing_lo_5 = atr_floor

    valid = [c for c in candidates if c is not None and c < price]
    valid_sorted = sorted(valid, reverse=True)  # tightest first

    if len(valid_sorted) >= 2:
        chosen = valid_sorted[1]
    elif valid_sorted:
        chosen = valid_sorted[0]
    else:
        chosen = atr_floor

    # MIN = furthest from price = most conservative
    stop = min(chosen, atr_floor, swing_lo_5)
    # Hard sanity floor — never let the stop sit ON OR ABOVE price
    stop = min(stop, price - 0.5 * atr)
    return round(stop, 2)


def _calibrate_short_stop(
    *,
    price: float,
    atr: float,
    df: pd.DataFrame,
    candidates: List[Optional[float]],
    timeframe: str,
) -> float:
    """Mirror of _calibrate_long_stop for shorts: stop sits ABOVE price."""
    mult = _stop_atr_mult(timeframe)
    atr_ceiling = price + mult * atr
    try:
        swing_hi_5 = float(df["High"].iloc[-5:].max()) * 1.003
        # Postmortem fix H6 (mirror): clamp swing-high to ≤ 3×ATR above price
        # so a recent spike doesn't bury the short stop.
        swing_hi_5 = min(swing_hi_5, price + 3.0 * atr)
    except Exception:
        swing_hi_5 = atr_ceiling

    valid = [c for c in candidates if c is not None and c > price]
    valid_sorted = sorted(valid)  # tightest first (closest above price)

    if len(valid_sorted) >= 2:
        chosen = valid_sorted[1]
    elif valid_sorted:
        chosen = valid_sorted[0]
    else:
        chosen = atr_ceiling

    # MAX = furthest from price = most conservative
    stop = max(chosen, atr_ceiling, swing_hi_5)
    stop = max(stop, price + 0.5 * atr)
    return round(stop, 2)


def generate_signal(ticker: str, timeframe: str, df: pd.DataFrame) -> Dict[str, Any]:
    """
    Generate a trading signal for the given ticker/timeframe.
    Returns dict with signal_type, confidence, entry, stop_loss, targets, reasoning, patterns.
    """
    if df.empty or len(df) < 30:
        return _neutral_signal(ticker, timeframe, "Insufficient data")

    ind = extract_latest(df)
    if not ind or ind.get("close") is None:
        return _neutral_signal(ticker, timeframe, "No indicator data")

    price = ind["close"]
    # Pivots are only meaningful at the daily level (R1/S1/etc are derived from
    # the prior session's H/L/C). For intraday signals, fetch the daily df and
    # compute pivots there so 5m/1h targets reference the same R1 traders watch.
    if timeframe in ("5m", "15m", "30m", "1h", "4h"):
        try:
            from services.data_fetcher import fetch_ohlcv as _fo_pv
            _daily_for_pivots = _fo_pv(ticker, "1d")
            pivots = pivot_points(_daily_for_pivots) if not _daily_for_pivots.empty else pivot_points(df)
        except Exception:
            pivots = pivot_points(df)
    else:
        pivots = pivot_points(df)
    swing_lvls = swing_levels(df)
    classified_lvls = classify_levels_relative_to_price(swing_lvls, price)
    nearest_sup, nearest_res = nearest_support_resistance(classified_lvls, price)
    patterns = detect_patterns(df)
    # Append gap-derived patterns (overnight gaps + FVGs) so the UI surfaces them
    patterns = patterns + gap_patterns(df)
    zones = detect_zones(df, price)
    fib = compute_fib_levels(df)
    # Multi-timeframe S/R confluence — only fetched for higher-TF analyses to
    # avoid recursion (a 5m run pulling 1d isn't worth the latency for every bar).
    mtf_levels: List[Dict] = []
    if timeframe in ("1h", "4h", "1d", "1mo"):
        try:
            mtf_levels = multi_timeframe_levels(ticker, timeframes=["1d", "4h", "1h"])
        except Exception:
            mtf_levels = []
    # Gap context: unfilled price-gaps and FVGs around the current price
    gap_supports = support_gaps_below(df, price)        # bull gaps below = support
    gap_resists = resistance_gaps_above(df, price)      # bear gaps above = resistance
    inside_gap = in_gap(df, price)                       # price currently inside an unfilled gap?
    gap_fill_above = gap_targets_above(df, price)        # bear-gap fill targets above (upside)
    gap_fill_below = gap_targets_below(df, price)        # bull-gap fill targets below (downside)

    # --- Score bullish conditions ---
    bull_score = 0
    bear_score = 0
    reasons = []

    # Trend conditions
    if ind.get("above_sma200"):
        bull_score += 15
        reasons.append("✅ Price above SMA200 (long-term uptrend)")
    else:
        bear_score += 15
        reasons.append("❌ Price below SMA200 (long-term downtrend)")

    if ind.get("above_sma50"):
        bull_score += 12
        reasons.append("✅ Price above SMA50 (medium-term uptrend)")
    else:
        bear_score += 12
        reasons.append("❌ Price below SMA50 (medium-term downtrend)")

    if ind.get("above_sma20"):
        bull_score += 8
        reasons.append("✅ Price above SMA20 (short-term uptrend)")
    else:
        bear_score += 8
        reasons.append("❌ Price below SMA20 (short-term downtrend)")

    # RSI
    rsi = ind.get("rsi")
    if rsi:
        if 50 < rsi < 70:
            bull_score += 10
            reasons.append(f"✅ RSI {rsi:.1f} — bullish momentum, not overbought")
        elif rsi >= 70:
            # In chop, overbought = mean-reversion short opportunity (boost)
            bear_score += 12 if (ind.get("adx") or 0) < ADX_CHOP_MAX else 5
            reasons.append(f"⚠️ RSI {rsi:.1f} — overbought, caution on longs")
        elif 30 < rsi <= 50:
            bear_score += 10
            reasons.append(f"❌ RSI {rsi:.1f} — bearish momentum")
        elif rsi <= 30:
            # Oversold in chop = mean-reversion long opportunity (boost)
            bull_score += 12 if (ind.get("adx") or 0) < ADX_CHOP_MAX else 5
            reasons.append(f"⚠️ RSI {rsi:.1f} — oversold, potential bounce")

    # MACD
    if ind.get("macd_bullish_cross"):
        bull_score += 15
        reasons.append("✅ MACD bullish crossover — momentum shifting up")
    elif ind.get("macd_bearish_cross"):
        bear_score += 15
        reasons.append("❌ MACD bearish crossover — momentum shifting down")
    elif ind.get("macd_hist") is not None:
        if ind["macd_hist"] > 0:
            bull_score += 5
            reasons.append("✅ MACD histogram positive")
        else:
            bear_score += 5
            reasons.append("❌ MACD histogram negative")

    # ADX regime classification (trending vs ranging)
    adx = ind.get("adx") or 0.0
    dmp = ind.get("dmp")
    dmn = ind.get("dmn")
    regime_chop = adx and adx < ADX_CHOP_MAX
    regime_trend = adx and adx > ADX_TREND_MIN
    if regime_trend:
        if dmp and dmn and dmp > dmn:
            bull_score += 10
            reasons.append(f"✅ ADX {adx:.1f} — strong bullish trend")
        elif dmp and dmn and dmn > dmp:
            bear_score += 10
            reasons.append(f"❌ ADX {adx:.1f} — strong bearish trend")
    elif regime_chop:
        reasons.append(
            f"⚠️ ADX {adx:.1f} — choppy/ranging regime; breakouts likely to fail, "
            "mean-reversion plays favored"
        )

    # Volume surge: confirms a directional move when present.
    if ind.get("volume_surge"):
        if bull_score > bear_score:
            bull_score += 10
            reasons.append("✅ Volume surge confirms bullish move")
        else:
            bear_score += 10
            reasons.append("❌ Volume surge confirms bearish move")

    # Volume FLOOR gate: a breakout / breakdown on volume BELOW the 20-bar
    # average is the textbook fakeout. We don't veto the signal (it might
    # still be valid on structure alone), but we shave conviction so it stops
    # crossing the auto-trader confidence floor on its own.
    vol = ind.get("volume") or 0
    vol_avg = ind.get("vol_sma20") or 0
    # Audit fix M1: the current bar may be forming (intraday polling sees it
    # mid-session) so `vol` is often <30% of a closed bar. Without this
    # guard, a perfectly healthy breakout triggers the low-volume fakeout
    # penalty just because we caught the bar at 10:05 instead of 15:59.
    # Fall back to the prior closed bar's volume when the current one looks
    # partial (< 30% of 20-bar avg and < 50% of prior bar).
    try:
        prior_vol = float(df["Volume"].iloc[-2]) if len(df) >= 2 else 0.0
    except Exception:
        prior_vol = 0.0
    partial_bar = (
        vol_avg > 0 and prior_vol > 0
        and vol < 0.3 * vol_avg
        and vol < 0.5 * prior_vol
    )
    if partial_bar:
        vol = prior_vol
    near_breakout = (
        (nearest_res and price >= nearest_res * 0.995)
        or (nearest_sup and price <= nearest_sup * 1.005)
    )
    if vol_avg > 0 and vol > 0 and near_breakout and vol < 0.8 * vol_avg:
        # Shave both sides equally — direction was already decided above.
        bull_score = max(0, bull_score - 8)
        bear_score = max(0, bear_score - 8)
        reasons.append(
            f"⚠️ Volume {vol:.0f} is only {(vol/vol_avg)*100:.0f}% of 20-bar avg "
            f"({vol_avg:.0f}) at a breakout/breakdown level — weak conviction, fakeout risk"
        )

    # Breakout check — regime-aware. In chop, fade the breakout instead of confirming it.
    breakout_score = 15 if not regime_chop else 6
    if nearest_res and price >= nearest_res * 0.995:
        if regime_chop:
            # Failed breakout setup: expect rejection
            bear_score += 8
            reasons.append(
                f"🚫 Price at resistance ${nearest_res:.2f} but ADX {adx:.1f} <20 — chop regime, "
                "breakout unlikely to follow through; expect rejection"
            )
        else:
            bull_score += breakout_score
            reasons.append(f"🚀 Price breaking out above resistance at ${nearest_res:.2f}")
    if nearest_sup and price <= nearest_sup * 1.005:
        if regime_chop:
            bull_score += 8
            reasons.append(
                f"🚫 Price at support ${nearest_sup:.2f} but ADX {adx:.1f} <20 — chop regime, "
                "breakdown likely fails; expect bounce"
            )
        else:
            bear_score += breakout_score
            reasons.append(f"📉 Price breaking down below support at ${nearest_sup:.2f}")

    # ---- Supply/demand zone awareness ----
    in_demand = in_zone(zones, price, "demand")
    in_supply = in_zone(zones, price, "supply")
    if in_demand:
        bull_score += 18
        reasons.append(
            f"🟢 Price inside demand zone ${in_demand['low']:.2f}-${in_demand['high']:.2f} "
            f"(score {in_demand['score']:.0f}, {in_demand['retests']} retests) — institutional accumulation base"
        )
    if in_supply:
        bear_score += 18
        reasons.append(
            f"🔴 Price inside supply zone ${in_supply['low']:.2f}-${in_supply['high']:.2f} "
            f"(score {in_supply['score']:.0f}, {in_supply['retests']} retests) — institutional distribution base"
        )
    next_demand = nearest_demand_below(zones, price)
    next_supply = nearest_supply_above(zones, price)

    # ---- Fibonacci awareness ----
    fib_below = fib_supports_below(fib, price) if fib else []
    fib_above = fib_resistances_above(fib, price) if fib else []
    if fib:
        leg_dir = fib["direction"]
        reasons.append(
            f"📐 Fib swing leg: {leg_dir.upper()} from ${fib['swing_low']:.2f} → ${fib['swing_high']:.2f}"
            f" (size ${fib['leg_size']:.2f})"
        )
        # Bounce/rejection at a key retracement (38.2 / 50 / 61.8) is high-conviction
        key = near_key_fib(fib, price)
        if key:
            if leg_dir == "up":
                # In an up-leg, retracements are pullback support → bullish bounce zone
                bull_score += 14
                reasons.append(
                    f"🟢 Price at golden-ratio pullback (Fib {key['label']} = ${key['price']:.2f}) of the up-leg "
                    f"— classic continuation buy zone"
                )
            else:
                # In a down-leg, retracements are bounce resistance → bearish rejection
                bear_score += 14
                reasons.append(
                    f"🔴 Price at Fib {key['label']} retracement (${key['price']:.2f}) of the down-leg "
                    f"— common rejection zone for shorts"
                )
        # Inform the trader about the next fib level on each side
        if fib_below:
            f0 = fib_below[0]
            reasons.append(
                f"📐 Nearest Fib support below: {f0['label']} {f0['kind']} = ${f0['price']:.2f}"
            )
        if fib_above:
            f0 = fib_above[0]
            reasons.append(
                f"📐 Nearest Fib resistance above: {f0['label']} {f0['kind']} = ${f0['price']:.2f}"
            )
    if next_demand:
        reasons.append(
            f"📊 Nearest demand zone below: ${next_demand['low']:.2f}-${next_demand['high']:.2f} (score {next_demand['score']:.0f})"
        )
    if next_supply:
        reasons.append(
            f"📊 Nearest supply zone above: ${next_supply['low']:.2f}-${next_supply['high']:.2f} (score {next_supply['score']:.0f})"
        )

    # ---- Gap awareness (price gaps + Fair Value Gaps) ----
    # 1) Price sitting INSIDE an unfilled gap is a magnet zone.
    if inside_gap:
        gtype = "bullish FVG / gap-up" if inside_gap["direction"] == "bull" else "bearish FVG / gap-down"
        # Bullish unfilled gap acts as support → favor longs
        if inside_gap["direction"] == "bull":
            bull_score += 12
            reasons.append(
                f"🟦 Price inside unfilled {gtype} ${inside_gap['bottom']:.2f}-${inside_gap['top']:.2f} "
                f"({inside_gap['fill_pct']*100:.0f}% filled) — institutional support zone, dip-buy bias"
            )
        else:
            bear_score += 12
            reasons.append(
                f"🟥 Price inside unfilled {gtype} ${inside_gap['bottom']:.2f}-${inside_gap['top']:.2f} "
                f"({inside_gap['fill_pct']*100:.0f}% filled) — overhead supply pocket, rally-fade bias"
            )

    # 2) Nearby unfilled gap as the next major target/magnet.
    if gap_resists:
        g0 = gap_resists[0]
        bull_score += 6  # upside magnet
        reasons.append(
            f"🎯 Unfilled bearish gap above ${g0['bottom']:.2f}-${g0['top']:.2f} "
            f"acts as upside magnet (price often returns to fill imbalance)"
        )
    if gap_supports:
        g0 = gap_supports[0]
        # A fresh untested bullish FVG below = strong demand floor
        bull_score += 6
        reasons.append(
            f"🛡️ Unfilled bullish gap below ${g0['bottom']:.2f}-${g0['top']:.2f} "
            f"— established demand floor; stops sit below this zone"
        )

    # 3) Punish chasing into thin air with no gap support nearby (bull case).
    if not gap_supports and not gap_resists:
        reasons.append("ℹ️ No active price-gap / FVG levels nearby")

    # ---- Multi-timeframe S/R confluence ----
    # Levels visible on multiple TFs (e.g. 1d + 4h + 1h) are stronger and act
    # as more reliable barriers/magnets than single-TF swings.
    mtf_supports_below = [l for l in mtf_levels if l["price"] < price * 0.998 and l["strength"] >= 3]
    mtf_resists_above = [l for l in mtf_levels if l["price"] > price * 1.002 and l["strength"] >= 3]
    if mtf_supports_below:
        s0 = mtf_supports_below[0]
        bull_score += 6
        reasons.append(
            f"🧱 Multi-TF support ${s0['price']:.2f} confluent across "
            f"{', '.join(s0['timeframes'])} (strength {s0['strength']}/5) — strong floor"
        )
    if mtf_resists_above:
        r0 = mtf_resists_above[0]
        bear_score += 6
        reasons.append(
            f"🧱 Multi-TF resistance ${r0['price']:.2f} confluent across "
            f"{', '.join(r0['timeframes'])} (strength {r0['strength']}/5) — strong ceiling"
        )

    # Bollinger Bands
    bb_upper = ind.get("bb_upper")
    bb_lower = ind.get("bb_lower")
    if bb_upper and price > bb_upper:
        reasons.append(f"⚠️ Price above upper Bollinger Band — extended move")
    elif bb_lower and price < bb_lower:
        bull_score += 5
        reasons.append(f"✅ Price at lower Bollinger Band — potential bounce zone")

    # Pattern bonus
    # Audit fix H7/M9: Golden Cross / Death Cross are already fully scored by
    # the SMA50/SMA200 stack above (above_sma200 +15, above_sma50 +12); and
    # gap / FVG patterns are scored by the gap-awareness block (inside_gap
    # ±12, gap_resists/gap_supports +6). Counting them here too triple-
    # weights the same evidence and produces inflated confidence on any bar
    # that sits above SMA200 the day after a golden cross. Skip the pattern
    # bonus for those — their reasoning line is still emitted below so the
    # UI shows the pattern.
    _DEDUP_PATTERN_NAMES = {
        "Golden Cross", "Death Cross",
        "Gap Up", "Gap Down",
        "Bullish FVG", "Bearish FVG",
        "Unfilled Gap",
    }
    for pat in patterns:
        already_scored = pat.get("name") in _DEDUP_PATTERN_NAMES
        if pat["type"] == "bullish":
            if not already_scored:
                bull_score += pat["confidence"] // 10
            reasons.append(f"📈 Pattern: {pat['name']} — {pat['description']}")
        elif pat["type"] == "bearish":
            if not already_scored:
                bear_score += pat["confidence"] // 10
            reasons.append(f"📉 Pattern: {pat['name']} — {pat['description']}")
        else:
            reasons.append(f"🔲 Pattern: {pat['name']} — {pat['description']}")

    # --- Determine signal ---
    total = bull_score + bear_score
    if total == 0:
        return _neutral_signal(ticker, timeframe, "No clear signal conditions")

    confidence_bull = (bull_score / total) * 100
    confidence_bear = (bear_score / total) * 100

    atr = ind.get("atr") or price * 0.02

    if bull_score > bear_score and confidence_bull >= 55:
        signal_type = "BUY"
        # Regime-aware confidence (profit-audit #2):
        _regime_mult = 1.0
        _adx_cur = float(ind.get("adx") or 0)
        if _adx_cur and _adx_cur < 20:
            _regime_mult *= 0.85
        elif _adx_cur and _adx_cur > 35:
            _regime_mult *= 1.05
        _rsi_cur = float(ind.get("rsi") or 50)
        if _rsi_cur > 75:
            _regime_mult *= 0.90

        # Ground-up Tier 1: RVOL boost/penalty.
        # High RVOL breakouts work 2-3× as often as low-RVOL. Reject signals
        # with RVOL < 0.6 (fakeout risk) and boost those with RVOL > 1.5.
        try:
            _vol_cur = float(ind.get("volume") or 0)
            _vol_avg = float(ind.get("vol_sma20") or 0)
            rvol = (_vol_cur / _vol_avg) if _vol_avg > 0 else 1.0
        except Exception:
            rvol = 1.0
        if rvol >= 2.0:
            _regime_mult *= 1.12
        elif rvol >= 1.5:
            _regime_mult *= 1.06
        elif rvol < 0.6:
            _regime_mult *= 0.85

        # Ground-up Tier 1: Relative strength vs SPY (20-day).
        # Tickers outperforming SPY have momentum tail wind; laggards are
        # usually value traps. Compute on the fly — cheap with cached OHLCV.
        try:
            from services.data_fetcher import fetch_ohlcv as _fo_rs
            spy_df = _fo_rs("SPY", "1d")
            if spy_df is not None and not spy_df.empty and len(spy_df) >= 21:
                spy_r20 = float(spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-21] - 1)
                tk_r20 = float(df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1) if len(df) >= 21 else 0.0
                rs_diff = tk_r20 - spy_r20
                if rs_diff >= 0.05:
                    _regime_mult *= 1.10   # strong leader
                elif rs_diff >= 0.02:
                    _regime_mult *= 1.04
                elif rs_diff <= -0.05:
                    _regime_mult *= 0.88   # laggard
                elif rs_diff <= -0.02:
                    _regime_mult *= 0.94
        except Exception:
            pass

        # Ground-up Tier 3: Sector momentum + market breadth.
        try:
            from services.market_context import (
                sector_confidence_multiplier,
                breadth_confidence_multiplier,
            )
            from services.data_fetcher import get_ticker_info as _gti
            _sector = (_gti(ticker).get("sector") or "").strip()
            _regime_mult *= sector_confidence_multiplier(_sector, "BUY")
            _regime_mult *= breadth_confidence_multiplier("BUY")
        except Exception:
            pass

        # Ground-up Tier 2: best-strategy-per-ticker boost.
        try:
            from services.best_strategy import confidence_boost
            _regime_mult *= confidence_boost(ticker, "Composite (multi-factor)", "BUY")
        except Exception:
            pass

        # Ground-up Tier 2: recent-earnings catalyst boost (PEAD).
        try:
            from services.earnings import recent_earnings_catalyst
            if recent_earnings_catalyst(ticker, days_back=10):
                _regime_mult *= 1.05   # modest boost; overdoing gets volatile
        except Exception:
            pass

        # Analyst-rating consensus (±12%). Strong Wall Street agreement with
        # the BUY direction nudges confidence up; strong SELL consensus nudges
        # it down. Neutral when coverage < 3 or data stale > 10 days.
        try:
            from services.analyst_ratings import rating_multiplier as _ar_mult, rating_reason_line as _ar_reason
            _regime_mult *= _ar_mult(ticker, "BUY")
            _ar_line = _ar_reason(ticker, "BUY")
            if _ar_line:
                reasons.append(_ar_line)
        except Exception:
            pass

        # Fundamentals quality score (±8%). Strong balance sheet + growth
        # + reasonable valuation boosts BUY; junk fundamentals dampen.
        try:
            from services.fundamentals import quality_multiplier as _f_mult, quality_reason_line as _f_reason
            _regime_mult *= _f_mult(ticker, "BUY")
            _f_line = _f_reason(ticker, "BUY")
            if _f_line:
                reasons.append(_f_line)
        except Exception:
            pass

        # Short interest (±8%). Crowded shorts on a BUY = fundamental
        # skepticism is real. Moderate SI = potential squeeze tilt.
        try:
            from services.fundamentals import short_interest_multiplier, short_interest_reason_line
            _regime_mult *= short_interest_multiplier(ticker, "BUY")
            _si_line = short_interest_reason_line(ticker, "BUY")
            if _si_line:
                reasons.append(_si_line)
        except Exception:
            pass

        # Stocktwits retail sentiment (±4%). Confirming/contradicting lean
        # on meaningful volume only (≥20 messages / 24h).
        try:
            from services.social_sentiment import sentiment_multiplier as _st_mult, sentiment_reason_line as _st_reason
            _regime_mult *= _st_mult(ticker, "BUY")
            _st_line = _st_reason(ticker, "BUY")
            if _st_line:
                reasons.append(_st_line)
        except Exception:
            pass

        # SEC Form 4 insider activity (±6%). Director/officer buying on
        # small/mid caps is empirically predictive.
        try:
            from services.insider_trades import insider_multiplier, insider_reason_line
            _regime_mult *= insider_multiplier(ticker, "BUY")
            _ins_line = insider_reason_line(ticker, "BUY")
            if _ins_line:
                reasons.append(_ins_line)
        except Exception:
            pass

        # WSB crowd sentiment (±3%) — noisy, only tilts on clear bullish/bearish
        # lean with adequate mention volume.
        try:
            from services.wsb_scraper import wsb_multiplier, wsb_reason_line
            _regime_mult *= wsb_multiplier(ticker, "BUY")
            _w_line = wsb_reason_line(ticker, "BUY")
            if _w_line:
                reasons.append(_w_line)
        except Exception:
            pass

        # Institutional accumulation / distribution QoQ (±3%, slow-moving).
        try:
            from services.institutional import institutional_multiplier, institutional_reason_line
            _regime_mult *= institutional_multiplier(ticker, "BUY")
            _i_line = institutional_reason_line(ticker, "BUY")
            if _i_line:
                reasons.append(_i_line)
        except Exception:
            pass

        # ML scorer: predicts P(win). Always runs (logs prediction); multiplier
        # is 1.0 unless ml_scoring_enabled=True in config (shadow-mode default).
        try:
            from services.ml_scorer import score_and_apply, predict_winrate
            from services.auto_trader import get_config_dict as _cfg_dict
            _ml_enabled = bool(_cfg_dict().get("ml_scoring_enabled", False))
            _stub_signal = {"signal_type": "BUY", "confidence": confidence_bull,
                            "entry": price, "stop_loss": None, "target1": None}
            _ml_mult = score_and_apply(ticker, _stub_signal, scoring_enabled=_ml_enabled)
            _regime_mult *= _ml_mult
            _p = predict_winrate(ticker, _stub_signal)
            if _p is not None:
                tag = "shadow" if not _ml_enabled else f"×{_ml_mult:.2f}"
                reasons.append(f"🤖 ML P(win)={_p:.2f} ({tag})")
        except Exception:
            pass

        confidence = min(round(confidence_bull * _regime_mult), 95)
        entry = round(price, 2)

        # Stop: prefer just below nearest demand-zone LOW (stronger than swing-low alone)
        zone_floors = []
        if in_demand:
            zone_floors.append(in_demand["low"] * 0.997)  # slightly below zone to give room
        if next_demand:
            zone_floors.append(next_demand["low"] * 0.997)
        sup_floor = nearest_sup * 0.997 if nearest_sup and nearest_sup < price else None
        # Fib floor: just below the nearest fib support so a wick into the level doesn't stop us out
        fib_floor = (fib_below[0]["price"] * 0.997) if fib_below else None
        # Gap floor: nearest unfilled bullish FVG below acts as a hard support shelf;
        # placing the stop just under its BOTTOM avoids being wicked out by a routine gap-fill.
        gap_floor = (gap_supports[0]["bottom"] * 0.997) if gap_supports else None
        zone_floor = (max(zone_floors) if zone_floors else None)
        # Multi-TF support floor: the nearest STRONG (≥3) confluence below price
        mtf_floor = (mtf_supports_below[0]["price"] * 0.997) if mtf_supports_below else None
        stop_loss = _calibrate_long_stop(
            price=price, atr=atr, df=df, timeframe=timeframe,
            candidates=[zone_floor, gap_floor, fib_floor, sup_floor, mtf_floor],
        )
        risk = max(entry - stop_loss, 0.01)

        # Targets: prefer fresh supply zones above price, then fib extensions/retracements,
        # then pivots/resistance, then R-multiples
        above = []
        for z in zones.get("supply", []):
            if z["low"] > entry:
                above.append(z["low"])
        for f in fib_above:
            above.append(float(f["price"]))
        for lvl in [pivots.get("R1"), pivots.get("R2"), pivots.get("R3"), nearest_res]:
            if lvl and lvl > entry:
                above.append(float(lvl))
        # Unfilled bear-gap fills above = high-probability magnets
        for gp in gap_fill_above:
            if gp > entry:
                above.append(float(gp))
        # Multi-TF resistances above = institutional ceilings (strong T2/T3 candidates)
        for r in mtf_resists_above:
            above.append(float(r["price"]))
        # Ground-up Tier 2: Volume-profile levels (POC/VAH/VAL) — high-probability
        # magnets where large institutions previously transacted.
        try:
            from services.volume_profile import compute_volume_profile, levels_above
            _vp = compute_volume_profile(df, window=60, num_bins=40)
            for lvl in levels_above(_vp, entry):
                above.append(float(lvl))
        except Exception:
            pass
        above = sorted(set(round(v, 2) for v in above))
        # Audit fix H3: enforce a minimum R:R ≥ 1.0 on T1. Without this, a
        # supply cluster that sits 0.3×risk above entry could become T1 and
        # the trade would be mathematically unprofitable after fees/slippage
        # (live auto_trader's MIN_RR=2.0 would reject it anyway, but the
        # signal would still display misleading targets in the UI).
        t1_floor = entry + max(risk * 1.0, atr * 0.5)
        above = [lvl for lvl in above if lvl >= t1_floor]
        # Enforce min spacing between targets so they're not bunched on one tight cluster
        spread = max(risk * 0.75, atr * 0.5)
        picked = []
        for lvl in above:
            if not picked or lvl - picked[-1] >= spread:
                picked.append(lvl)
            if len(picked) == 3:
                break
        t1 = picked[0] if len(picked) >= 1 else entry + risk * 1.5
        t2 = picked[1] if len(picked) >= 2 else max(entry + risk * 2.5, t1 + risk)
        t3 = picked[2] if len(picked) >= 3 else max(entry + risk * 4.0, t2 + risk)
    elif bear_score > bull_score and confidence_bear >= 55:
        signal_type = "SELL"
        # Regime-aware confidence (profit-audit #2, SELL/put side mirror).
        _regime_mult = 1.0
        _adx_cur = float(ind.get("adx") or 0)
        if _adx_cur and _adx_cur < 20:
            _regime_mult *= 0.85
        elif _adx_cur and _adx_cur > 35:
            _regime_mult *= 1.05
        _rsi_cur = float(ind.get("rsi") or 50)
        if _rsi_cur < 25:
            _regime_mult *= 0.90

        # Ground-up Tier 1 mirror: RVOL, RS, sector, breadth (SELL direction).
        try:
            _vol_cur = float(ind.get("volume") or 0)
            _vol_avg = float(ind.get("vol_sma20") or 0)
            rvol = (_vol_cur / _vol_avg) if _vol_avg > 0 else 1.0
        except Exception:
            rvol = 1.0
        if rvol >= 2.0:
            _regime_mult *= 1.12
        elif rvol >= 1.5:
            _regime_mult *= 1.06
        elif rvol < 0.6:
            _regime_mult *= 0.85

        try:
            from services.data_fetcher import fetch_ohlcv as _fo_rs
            spy_df = _fo_rs("SPY", "1d")
            if spy_df is not None and not spy_df.empty and len(spy_df) >= 21:
                spy_r20 = float(spy_df["Close"].iloc[-1] / spy_df["Close"].iloc[-21] - 1)
                tk_r20 = float(df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1) if len(df) >= 21 else 0.0
                rs_diff = tk_r20 - spy_r20
                # For SELL, UNDERperformance is the edge.
                if rs_diff <= -0.05:
                    _regime_mult *= 1.10
                elif rs_diff <= -0.02:
                    _regime_mult *= 1.04
                elif rs_diff >= 0.05:
                    _regime_mult *= 0.88
                elif rs_diff >= 0.02:
                    _regime_mult *= 0.94
        except Exception:
            pass

        try:
            from services.market_context import (
                sector_confidence_multiplier,
                breadth_confidence_multiplier,
            )
            from services.data_fetcher import get_ticker_info as _gti
            _sector = (_gti(ticker).get("sector") or "").strip()
            _regime_mult *= sector_confidence_multiplier(_sector, "SELL")
            _regime_mult *= breadth_confidence_multiplier("SELL")
        except Exception:
            pass

        # Ground-up Tier 2: best-strategy-per-ticker boost.
        try:
            from services.best_strategy import confidence_boost
            _regime_mult *= confidence_boost(ticker, "Composite (multi-factor)", "SELL")
        except Exception:
            pass

        # Analyst-rating consensus — mirror of BUY side.
        try:
            from services.analyst_ratings import rating_multiplier as _ar_mult, rating_reason_line as _ar_reason
            _regime_mult *= _ar_mult(ticker, "SELL")
            _ar_line = _ar_reason(ticker, "SELL")
            if _ar_line:
                reasons.append(_ar_line)
        except Exception:
            pass

        # Fundamentals quality — mirror of BUY side. Junk fundamentals
        # confirm a bearish thesis.
        try:
            from services.fundamentals import quality_multiplier as _f_mult, quality_reason_line as _f_reason
            _regime_mult *= _f_mult(ticker, "SELL")
            _f_line = _f_reason(ticker, "SELL")
            if _f_line:
                reasons.append(_f_line)
        except Exception:
            pass

        # Short interest — already-crowded short = easy money has been made,
        # penalize fresh SELLs on heavily-shorted names.
        try:
            from services.fundamentals import short_interest_multiplier, short_interest_reason_line
            _regime_mult *= short_interest_multiplier(ticker, "SELL")
            _si_line = short_interest_reason_line(ticker, "SELL")
            if _si_line:
                reasons.append(_si_line)
        except Exception:
            pass

        # Stocktwits retail sentiment (±4%) — mirror.
        try:
            from services.social_sentiment import sentiment_multiplier as _st_mult, sentiment_reason_line as _st_reason
            _regime_mult *= _st_mult(ticker, "SELL")
            _st_line = _st_reason(ticker, "SELL")
            if _st_line:
                reasons.append(_st_line)
        except Exception:
            pass

        # SEC Form 4 — heavy insider selling confirms bearish.
        try:
            from services.insider_trades import insider_multiplier, insider_reason_line
            _regime_mult *= insider_multiplier(ticker, "SELL")
            _ins_line = insider_reason_line(ticker, "SELL")
            if _ins_line:
                reasons.append(_ins_line)
        except Exception:
            pass

        # WSB — bearish crowd lean confirms SELL.
        try:
            from services.wsb_scraper import wsb_multiplier, wsb_reason_line
            _regime_mult *= wsb_multiplier(ticker, "SELL")
            _w_line = wsb_reason_line(ticker, "SELL")
            if _w_line:
                reasons.append(_w_line)
        except Exception:
            pass

        # Institutional distribution confirms bearish.
        try:
            from services.institutional import institutional_multiplier, institutional_reason_line
            _regime_mult *= institutional_multiplier(ticker, "SELL")
            _i_line = institutional_reason_line(ticker, "SELL")
            if _i_line:
                reasons.append(_i_line)
        except Exception:
            pass

        # ML scorer mirror — see BUY-side comment.
        try:
            from services.ml_scorer import score_and_apply, predict_winrate
            from services.auto_trader import get_config_dict as _cfg_dict
            _ml_enabled = bool(_cfg_dict().get("ml_scoring_enabled", False))
            _stub_signal = {"signal_type": "SELL", "confidence": confidence_bear,
                            "entry": price, "stop_loss": None, "target1": None}
            _ml_mult = score_and_apply(ticker, _stub_signal, scoring_enabled=_ml_enabled)
            _regime_mult *= _ml_mult
            _p = predict_winrate(ticker, _stub_signal)
            if _p is not None:
                tag = "shadow" if not _ml_enabled else f"×{_ml_mult:.2f}"
                reasons.append(f"🤖 ML P(win)={_p:.2f} ({tag})")
        except Exception:
            pass

        confidence = min(round(confidence_bear * _regime_mult), 95)
        entry = round(price, 2)

        zone_ceilings = []
        if in_supply:
            zone_ceilings.append(in_supply["high"] * 1.003)
        if next_supply:
            zone_ceilings.append(next_supply["high"] * 1.003)
        res_ceiling = nearest_res * 1.003 if nearest_res and nearest_res > price else None
        fib_ceiling = (fib_above[0]["price"] * 1.003) if fib_above else None
        # Gap ceiling: place short stop just above nearest unfilled bearish gap top so a fill-the-gap rip doesn't stop us out
        gap_ceiling = (gap_resists[0]["top"] * 1.003) if gap_resists else None
        zone_ceiling = (min(zone_ceilings) if zone_ceilings else None)
        mtf_ceiling = (mtf_resists_above[0]["price"] * 1.003) if mtf_resists_above else None
        stop_loss = _calibrate_short_stop(
            price=price, atr=atr, df=df, timeframe=timeframe,
            candidates=[zone_ceiling, gap_ceiling, fib_ceiling, res_ceiling, mtf_ceiling],
        )
        risk = max(stop_loss - entry, 0.01)

        below = []
        for z in zones.get("demand", []):
            if z["high"] < entry:
                below.append(z["high"])
        for f in fib_below:
            below.append(float(f["price"]))
        for lvl in [pivots.get("S1"), pivots.get("S2"), pivots.get("S3"), nearest_sup]:
            if lvl and lvl < entry:
                below.append(float(lvl))
        # Unfilled bull-gap fills below = downside magnets for shorts
        for gp in gap_fill_below:
            if gp < entry:
                below.append(float(gp))
        # Multi-TF supports below = institutional floors (strong T2/T3 candidates)
        for s in mtf_supports_below:
            below.append(float(s["price"]))
        # Ground-up Tier 2: Volume-profile levels (POC/VAH/VAL below price).
        try:
            from services.volume_profile import compute_volume_profile, levels_below
            _vp = compute_volume_profile(df, window=60, num_bins=40)
            for lvl in levels_below(_vp, entry):
                below.append(float(lvl))
        except Exception:
            pass
        below = sorted(set(round(v, 2) for v in below), reverse=True)
        # Audit fix H3 (short mirror): drop any candidate closer than 1R to
        # entry so T1 always offers ≥ 1:1 reward-to-risk.
        t1_ceiling = entry - max(risk * 1.0, atr * 0.5)
        below = [lvl for lvl in below if lvl <= t1_ceiling]
        spread = max(risk * 0.75, atr * 0.5)
        picked = []
        for lvl in below:
            if not picked or picked[-1] - lvl >= spread:
                picked.append(lvl)
            if len(picked) == 3:
                break
        t1 = picked[0] if len(picked) >= 1 else entry - risk * 1.5
        t2 = picked[1] if len(picked) >= 2 else min(entry - risk * 2.5, t1 - risk)
        t3 = picked[2] if len(picked) >= 3 else min(entry - risk * 4.0, t2 - risk)
    else:
        return _neutral_signal(ticker, timeframe, "Mixed signals — no clear directional bias")

    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "signal_type": signal_type,
        "confidence": confidence,
        "entry": entry,
        "stop_loss": stop_loss,
        "target1": round(float(t1), 2) if t1 else None,
        "target2": round(float(t2), 2) if t2 else None,
        "target3": round(float(t3), 2) if t3 else None,
        "reasoning": "\n".join(reasons),
        "patterns": json.dumps([p["name"] for p in patterns]),
        "strategy": "Composite (multi-factor)",
        # F4: surface ADX so _apply_backtest_to_signal can regime-gate the
        # strategy pool (drop BREAKOUTs in chop, MEANREV in strong trend).
        "adx": float(adx) if adx else None,
    }


def _neutral_signal(ticker: str, timeframe: str, reason: str) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "signal_type": "NEUTRAL",
        "confidence": 50,
        "entry": None,
        "stop_loss": None,
        "target1": None,
        "target2": None,
        "target3": None,
        "reasoning": reason,
        "patterns": "[]",
        "strategy": "Composite (multi-factor)",
    }


def get_timeframe_alignment(signals: List[Dict]) -> Dict[str, str]:
    """Summarize signal direction per timeframe for the alignment grid."""
    return {s["timeframe"]: s["signal_type"] for s in signals}
