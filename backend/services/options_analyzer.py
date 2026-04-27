"""
Option-play selector: given a stock signal (direction + targets + stop),
filter the option chain to contracts with >= 3:1 reward-to-risk and score them.
"""
import logging
import math
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
from services.options_fetcher import fetch_option_chain, fetch_expirations

logger = logging.getLogger(__name__)

# IV-vs-RV gate: reject contracts whose implied volatility is meaningfully
# above the underlying's realized volatility. Paying up for IV before vol
# mean-reverts is a losing trade even when direction is right (options
# analyzer post-mortem on CRWV-class weeklies showed IV rank 75+ eaten by
# post-entry vol crush).
_IV_RV_RATIO_MAX = 1.75        # skip contracts when IV > 1.75× 20d realized vol
_IV_RV_WINDOW = 20             # trading days of daily returns for the RV calc

# 20d realized-vol cache: ticker -> (rv_annualized, expiry_ts).
_rv_cache: Dict[str, tuple] = {}
_RV_TTL_SEC = 6 * 3600   # recompute twice/day — daily bars don't change intraday


def _realized_vol_20d(ticker: str) -> Optional[float]:
    """Annualised 20-day realized volatility using daily log-returns.
    Cached 6h. Returns None on failure — callers should treat that as
    "unknown, don't block" rather than reject-by-default."""
    now = time.time()
    cached = _rv_cache.get(ticker)
    if cached and now < cached[1]:
        return cached[0]
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(ticker, "1d")
        if df is None or len(df) < _IV_RV_WINDOW + 1:
            return None
        closes = df["Close"].astype(float).iloc[-(_IV_RV_WINDOW + 1):]
        # log returns
        import numpy as np
        r = np.log(closes / closes.shift(1)).dropna()
        if r.empty:
            return None
        std_daily = float(r.std())
        rv_ann = std_daily * math.sqrt(252)
        _rv_cache[ticker] = (rv_ann, now + _RV_TTL_SEC)
        return rv_ann
    except Exception as e:
        logger.debug(f"realized-vol compute failed for {ticker}: {e}")
        return None


def _iv_is_expensive(ticker: str, iv: float) -> bool:
    """True when the contract's IV is > _IV_RV_RATIO_MAX × underlying's RV.
    r43 fix #1.23: when RV is unavailable, fall back to an absolute IV cap
    (1.0 = 100% annualized) so unknown-RV new IPOs / illiquid names don't
    silently skip the volatility filter — exactly the names where IV is
    most prone to be inflated.
    """
    if not iv or iv <= 0:
        return False
    rv = _realized_vol_20d(ticker)
    if rv is None or rv <= 0:
        return iv > 1.0
    # Both IV and RV are annualized decimal (e.g. 0.35 = 35%).
    return iv > _IV_RV_RATIO_MAX * rv


# r43 fix #1.23: 1y IV-rank lookup. We don't store historical IV per ticker
# (that would require a separate ingestion job). Instead, we compute an
# *implied* rank from the underlying's realized-vol distribution: contracts
# whose IV exceeds the 90th percentile of the underlying's 1y rolling
# 20d-RV are flagged as IV-expensive. This is a coarse proxy but catches
# the common case (vol-event-priced contracts on otherwise quiet names).
def _iv_rank_too_high(ticker: str, iv: float, threshold: float = 0.90) -> bool:
    if not iv or iv <= 0:
        return False
    try:
        from services.data_fetcher import fetch_ohlcv as _fo
        import numpy as _np
        df = _fo(ticker, "1d")
        if df is None or df.empty or len(df) < 252:
            return False
        closes = df["Close"].astype(float).tail(252)
        rets = _np.log(closes / closes.shift(1)).dropna()
        # Rolling 20d realized-vol annualized as a sample distribution.
        rv_series = rets.rolling(20).std() * (252 ** 0.5)
        rv_series = rv_series.dropna()
        if len(rv_series) < 50:
            return False
        cutoff = float(rv_series.quantile(threshold))
        return iv > cutoff * 1.05
    except Exception:
        return False


MIN_RR = 2.0  # Lowered from 3.0 — intraday SELL signals (5m/15m/30m) have
              # tight T1-T3 bands (3-7% moves) that rarely clear 3:1 against
              # realistic put premiums, so PUTS tabs stayed empty even when
              # the signal was actionable. 2:1 still gates out junk but lets
              # short-timeframe scalps through.
MIN_DTE = 10        # Post-mortem fix (CRWV -$2,561): weeklies ≤ 3 DTE get
                    # shredded by theta even when the underlying moves in
                    # your favor. 10 DTE minimum keeps us in contracts
                    # where a 2-3 day thesis has time to play out.
# r46 Tier 1 parameter tune: 90 → 75 (gamma is anemic past 60 DTE),
# MIN_VOLUME 5 → 25, MIN_OI 25 → 100 (hard liquidity floor — even SPY
# weeklies clear these comfortably; the spread filter catches everything else).
MAX_DTE = 75
MIN_VOLUME = 25
MIN_OI = 100
WEEKLY_DTE = 7
PREMIUM_STOP_PCT = 0.50   # exit when contract has lost 50% of its premium


def _mid_price(o: dict) -> Optional[float]:
    """Best-effort fair price for a contract: prefer (bid+ask)/2 when
    both quotes are non-zero (true mid), fall back to last-traded price.

    Returns None when none are available — caller should treat this as
    "skip this contract, can't price it". The fallback order matters:
    `last` can be hours stale on illiquid chains, so we only use it
    when the live two-sided quote is missing entirely.
    """
    bid = o.get("bid") or 0
    ask = o.get("ask") or 0
    last = o.get("lastPrice") or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return last or None


def _dte(expiration_ts: int) -> int:
    """Days-to-expiration anchored at 16:00 ET = 20:00 UTC of the expiry date.

    Postmortem fix H7: Yahoo provides expiration as a date (00:00 UTC). US
    options actually expire at 16:00 ET (20:00 UTC). Friday-morning UTC
    measurement against a 00:00-anchor returns dte=0 for that day's expiry
    even though there are 6 trading hours left, and the MIN_DTE filter then
    drops valid same-week monthlies. Anchor to the real cash-settlement
    moment and ceil so a contract expiring tomorrow always reports dte ≥ 1.
    """
    import math
    # Snap to end-of-day UTC for the expiration date.
    eod_anchor = (int(expiration_ts) // 86400) * 86400 + 20 * 3600
    seconds_remaining = eod_anchor - datetime.utcnow().timestamp()
    if seconds_remaining <= 0:
        return 0
    return max(0, math.ceil(seconds_remaining / 86400))


def _score(
    rr_t1: float, rr_t2: float, rr_t3: float, dte: int, vol: int, oi: int, iv: float,
    delta_proxy: float,
    premium: float = 0.0,
    # Real Greeks from Alpaca OPRA/indicative feed (AT+ tier). When None,
    # we fall back to the delta_proxy heuristic so Yahoo fallback still scores.
    delta: Optional[float] = None, theta: Optional[float] = None,
    gamma: Optional[float] = None, vega: Optional[float] = None,
) -> float:
    """Composite score for an option contract. Max ~125 with Greeks, ~100 without.

    Sections (each independent so operator can intuit why a contract ranks):
      R:R core              → 40 pts
      R:R T2 bonus          → 15 pts
      DTE sweet-spot        → 15 pts
      Liquidity (vol/OI)    → 20 pts
      Delta fitness         → 10 pts  (real delta when available)
      Theta efficiency      → 15 pts  (Greeks-only)
      Vega-crush penalty    → −10 pts (Greeks-only)
      Gamma leverage        → 10 pts  (Greeks-only)
    """
    best_rr = max(rr_t1, rr_t2, rr_t3)
    rr_score = min(best_rr / 5.0, 1.0) * 40
    rr2_bonus = min(rr_t2 / 10.0, 1.0) * 15

    if 30 <= dte <= 60:
        dte_score = 15
    elif 20 <= dte <= 75:
        dte_score = 12
    elif 8 <= dte <= 19:
        dte_score = 8
    elif dte <= WEEKLY_DTE:
        dte_score = 10 if best_rr >= 6 else 5
    else:
        dte_score = 5

    liq = min((vol / 100.0), 1.0) * 10 + min((oi / 500.0), 1.0) * 10

    # r43 fix #1.24: asymmetric delta scoring. The previous symmetric formula
    # `10 * (1 - |delta - 0.5| / 0.5)` scored delta=0.7 (ITM, tracks
    # underlying tightly) and delta=0.1 (OTM lottery) the same — both got
    # ~6 pts. For directional plays on 30-45 DTE, delta 0.4-0.7 is
    # empirically much better. New scoring:
    #   |delta| in [0.4, 0.7] → full 10 pts (the directional sweet spot)
    #   |delta| in [0.25, 0.4] → 5 pts (linear ramp from 5→10)
    #   |delta| > 0.7         → 7 pts (deep ITM, costs more)
    #   |delta| < 0.25        → 0 pts (OTM lottery, harsh penalty)
    d_eff = delta if delta is not None else delta_proxy
    abs_d = abs(d_eff)
    if 0.4 <= abs_d <= 0.7:
        delta_score = 10.0
    elif 0.25 <= abs_d < 0.4:
        # Linear 5 → 10 across [0.25, 0.4].
        delta_score = 5.0 + (abs_d - 0.25) * (5.0 / 0.15)
    elif abs_d > 0.7:
        delta_score = 7.0
    else:
        delta_score = 0.0

    # ---- Greeks-aware adjustments ----
    theta_efficiency = 0.0
    vega_penalty = 0.0
    gamma_reward = 0.0

    if theta is not None and premium > 0:
        # Critical-audit fix #3: don't DOUBLE-penalize weeklies. The dte_score
        # above already penalizes <=7 DTE (5-10 pts vs 15). Applying both
        # dte_score AND theta_efficiency on weeklies punishes good 2-3 day
        # setups that happen to be on short calendar. Skip theta efficiency
        # entirely for weeklies — dte_score handles calendar risk there.
        if dte > WEEKLY_DTE:
            theta_pct = abs(theta) / premium
            if theta_pct <= 0.01:
                theta_efficiency = 15
            elif theta_pct <= 0.02:
                theta_efficiency = 10
            elif theta_pct <= 0.03:
                theta_efficiency = 5
            elif theta_pct <= 0.05:
                theta_efficiency = 0
            else:
                theta_efficiency = -8

    if vega is not None and iv and iv > 0 and premium > 0:
        # Vol-crush exposure = vega × IV / premium — high when we're paying
        # for inflated vol that may mean-revert against the thesis.
        vc = abs(vega) * float(iv) / premium
        if vc > 0.10:
            vega_penalty = -10
        elif vc > 0.05:
            vega_penalty = -5

    if gamma is not None and premium > 0:
        # Gamma-per-dollar: how fast delta changes per underlying unit per
        # dollar of premium. High = strong leverage on favorable moves.
        gpd = gamma / premium
        if gpd >= 0.05:
            gamma_reward = 10
        elif gpd >= 0.02:
            gamma_reward = 5

    total = (rr_score + rr2_bonus + dte_score + liq + delta_score
             + theta_efficiency + vega_penalty + gamma_reward)
    return round(total, 1)


def _delta_proxy(spot: float, strike: float, is_call: bool) -> float:
    """Rough delta: 0.5 at ATM, scales with moneyness. Not BS — just a heuristic."""
    if spot <= 0:
        return 0.5
    rel = (spot - strike) / spot
    if is_call:
        return max(0.05, min(0.95, 0.5 + rel * 5))
    else:
        return -max(0.05, min(0.95, 0.5 - rel * 5))


def suggest_options_for_signal(ticker: str, signal: dict, limit: int = 50) -> Dict[str, Any]:
    """
    Given a signal dict (needs signal_type, entry, stop_loss, target1, target2, target3),
    pick suitable call/put contracts with R:R >= 3:1 at Target 1.

    `limit` caps how many top-scored contracts come back. Default 50 (was 10);
    the chain often surfaces dozens of qualifying contracts and the UI showed
    a confusing "Found 172" while only 10 rows were visible.
    """
    direction = signal.get("signal_type")
    entry = signal.get("entry")
    t1 = signal.get("target1")
    t2 = signal.get("target2")
    t3 = signal.get("target3")
    sl = signal.get("stop_loss")  # underlying stock stop

    if direction not in ("BUY", "SELL") or not all([entry, t1, t2, t3, sl]):
        return {"contracts": [], "note": "Signal missing entry/targets/stop — cannot size options."}

    expirations = fetch_expirations(ticker)
    if not expirations:
        return {"contracts": [], "note": "No option expirations available for this ticker."}

    # Only consider expirations within DTE window. Previously fell back to
    # expirations[:3] when empty — that silently bypassed MIN_DTE and let a
    # NFLX 3-DTE put through that lost $1020 held overnight into expiry week.
    # If the chain has no contracts in [MIN_DTE, MAX_DTE], we pass.
    eligible_exps = [e for e in expirations if MIN_DTE <= _dte(e) <= MAX_DTE]
    if not eligible_exps:
        return {"contracts": [], "note": f"No expirations in DTE window [{MIN_DTE}, {MAX_DTE}]."}

    contracts: List[dict] = []
    for exp_ts in eligible_exps[:8]:  # first 8 expirations: covers weeklies + monthlies
        chain = fetch_option_chain(ticker, exp_ts)
        if not chain:
            continue
        spot = chain.get("quote_price") or entry
        dte = _dte(exp_ts)
        exp_date = datetime.utcfromtimestamp(exp_ts).strftime("%Y-%m-%d")

        leg_list = chain["calls"] if direction == "BUY" else chain["puts"]
        is_call = (direction == "BUY")

        for o in leg_list:
            strike = o.get("strike")
            premium = _mid_price(o)
            if not strike or not premium or premium <= 0.05:
                continue
            vol = int(o.get("volume") or 0)
            oi = int(o.get("openInterest") or 0)
            # r43 fix #0.8: when feed-side OI/vol is unknown (Alpaca snapshot
            # path returns 0/0), don't auto-fail; rely on the spread filter
            # below + premium-vs-mid sanity. When OI/vol IS known (Yahoo),
            # apply the historical gates.
            if (vol > 0 or oi > 0) and (vol < MIN_VOLUME or oi < MIN_OI):
                continue
            # r43 fix #0.9: bid-ask spread filter normalized vs PREMIUM (mid),
            # not strike. Previously `(ask-bid)/strike > 0.05` — for a
            # $200-strike $1 put with $4 spread (= 400% of premium,
            # untradeable), the gate evaluated 4/200=2% and PASSED. The
            # filter was effectively neutered for cheap OTM contracts —
            # exactly the ones with the worst spreads. New gate: spread ≤
            # 20% of mid OR ≤ $0.05 (the cheapest-tick floor).
            bid = float(o.get("bid") or 0)
            ask = float(o.get("ask") or 0)
            if bid > 0 and ask > bid:
                spread = ask - bid
                mid_for_spread = (ask + bid) / 2.0
                if mid_for_spread <= 0:
                    continue
                if spread > 0.05 and (spread / mid_for_spread) > 0.20:
                    continue
            iv = float(o.get("impliedVolatility") or 0)

            # IV-vs-RV gate — skip over-priced premium. Contracts where IV
            # is more than 1.75× the underlying's 20d realized vol get
            # punished by mean-reversion (IV crush) even when direction is
            # right. r43 fix #1.23: also reject when implied IV-rank > 90th
            # percentile of underlying's rolling 20d-RV distribution
            # (catches event-priced contracts on otherwise-quiet names).
            if _iv_is_expensive(ticker, iv):
                continue
            if _iv_rank_too_high(ticker, iv, threshold=0.90):
                continue

            # r43 fix #1.25: ATR-anchored strike-width gate. The previous
            # hardcoded 25% gate was reasonable for $500 stocks, useless for
            # $5 stocks (entire chain in-band). Use 5×ATR or 25%-of-spot,
            # whichever is more restrictive (and never < 10%-of-spot to
            # preserve the original behavior on mid-priced names).
            try:
                from services.indicators import compute_indicators as _ci
                from services.data_fetcher import fetch_ohlcv as _fo_atr
                atr_df = _fo_atr(ticker, "1d")
                if atr_df is not None and not atr_df.empty:
                    _ind = _ci(atr_df.tail(40))
                    atr_col = next((c for c in _ind.columns if c.startswith("ATR_")), None)
                    if atr_col:
                        atr_val = float(_ind[atr_col].iloc[-1])
                        atr_band = max(0.10 * spot, min(0.25 * spot, 5.0 * atr_val))
                    else:
                        atr_band = 0.25 * spot
                else:
                    atr_band = 0.25 * spot
            except Exception:
                atr_band = 0.25 * spot
            if abs(strike - spot) > atr_band:
                continue

            # Reward at each target (intrinsic value at target minus premium paid)
            if is_call:
                reward_t1 = max(0.0, t1 - strike) - premium
                reward_t2 = max(0.0, t2 - strike) - premium
                reward_t3 = max(0.0, t3 - strike) - premium
            else:
                reward_t1 = max(0.0, strike - t1) - premium
                reward_t2 = max(0.0, strike - t2) - premium
                reward_t3 = max(0.0, strike - t3) - premium

            risk = premium  # absolute max loss per share on a long option
            rr_t1 = reward_t1 / risk if risk > 0 else 0
            rr_t2 = reward_t2 / risk if risk > 0 else 0
            rr_t3 = reward_t3 / risk if risk > 0 else 0

            # R:R using a managed stop (smaller realised loss if stop is honoured)
            # Computed after stop levels below — placeholder here, filled in post-dict.

            # Qualify if ANY target achieves >= 3:1. T1 is often too close for tight
            # intraday moves, so the measured move (T2) or runner (T3) is the real test.
            best_rr = max(rr_t1, rr_t2, rr_t3)
            if best_rr < MIN_RR:
                continue

            proxy = _delta_proxy(spot, strike, is_call)
            # Real Greeks from Alpaca snapshot (None when falling back to Yahoo).
            real_delta = o.get("delta")
            real_theta = o.get("theta")
            real_gamma = o.get("gamma")
            real_vega  = o.get("vega")
            score = _score(
                rr_t1, rr_t2, rr_t3, dte, vol, oi, iv, proxy,
                premium=premium,
                delta=real_delta, theta=real_theta,
                gamma=real_gamma, vega=real_vega,
            )
            # For downstream consumers: surface the real delta when we have it,
            # fall back to proxy. This is what gets shown as "Δ" in the UI.
            delta = real_delta if real_delta is not None else proxy

            breakeven = strike + premium if is_call else strike - premium

            # ---- Stop-loss levels for the contract ----
            # 1) Premium stop: exit if contract loses 50% of entry premium (hard $ cap)
            premium_stop = round(premium * (1 - PREMIUM_STOP_PCT), 2)
            max_loss_premium_stop = round(premium * PREMIUM_STOP_PCT * 100, 2)

            # 2) Thesis stop: underlying hits signal's stop_loss → thesis invalidated.
            #    Estimate contract value at that level using delta.
            est_prem_at_underlying_stop = max(0.01, premium + delta * (sl - spot))
            est_loss_at_underlying_stop = round((premium - est_prem_at_underlying_stop) * 100, 2)

            # r43 fix #1.9: previously this took max() of the two stops which
            # made the higher (less-loss) stop bind — but for a typical
            # delta-0.4 contract `est_prem_at_underlying_stop` is ALWAYS
            # higher than `premium_stop` (premium - delta*distance >
            # 0.5*premium), so the underlying-aware stop NEVER bound. The
            # premium-50% stop always fired first regardless of underlying
            # direction, closing positions for max-managed-loss when the
            # underlying was fine.
            #
            # New behavior: take the MIN of the two (the more-pessimistic
            # stop, lower premium = bigger loss accepted before stopping).
            # This makes the underlying-aware stop bind for tight underlying
            # stops and the premium-50% stop bind only when underlying
            # really does fall through. Correct for sizing-by-stop-loss.
            effective_stop_premium = min(premium_stop, est_prem_at_underlying_stop)
            effective_loss = round((premium - effective_stop_premium) * 100, 2)

            # R:R using managed stop instead of full-premium loss.
            # The 0.05 floor protects against div-by-zero, but if the TRUE
            # managed_risk is below the floor AND no target hits MIN_RR against
            # that floor, drop the contract — otherwise the floor inflates
            # rr_managed and lets sub-threshold contracts slip into the list.
            true_managed_risk = max(premium - effective_stop_premium, 0.0)
            managed_risk = max(true_managed_risk, 0.05)
            best_reward = max(reward_t1, reward_t2, reward_t3)
            if best_reward < managed_risk * MIN_RR:
                continue
            rr_t1_managed = round(reward_t1 / managed_risk, 2) if reward_t1 > 0 else round(rr_t1, 2)
            rr_t2_managed = round(reward_t2 / managed_risk, 2) if reward_t2 > 0 else round(rr_t2, 2)
            rr_t3_managed = round(reward_t3 / managed_risk, 2) if reward_t3 > 0 else round(rr_t3, 2)

            contracts.append({
                "type": "CALL" if is_call else "PUT",
                # r43 fix #0.1: Alpaca-feed contracts only set `_occ` (the OCC
                # symbol). Yahoo-feed contracts set `contractSymbol`. Falling
                # back keeps both feeds working; without this, every
                # Alpaca-sourced contract had `symbol=None` and Alpaca rejected
                # the option order with a confusing error.
                "symbol": o.get("contractSymbol") or o.get("_occ"),
                "strike": round(float(strike), 2),
                "expiration": exp_date,
                "dte": dte,
                "is_weekly": dte <= WEEKLY_DTE,
                "premium_stop": premium_stop,
                "max_loss_at_premium_stop": max_loss_premium_stop,
                "underlying_stop": round(float(sl), 2),
                "est_premium_at_underlying_stop": round(est_prem_at_underlying_stop, 2),
                "est_loss_at_underlying_stop": est_loss_at_underlying_stop,
                "effective_stop_premium": round(effective_stop_premium, 2),
                "effective_max_loss": effective_loss,
                "rr_t1_managed": rr_t1_managed,
                "rr_t2_managed": rr_t2_managed,
                "rr_t3_managed": rr_t3_managed,
                "premium": round(float(premium), 2),
                "bid": float(o.get("bid") or 0),
                "ask": float(o.get("ask") or 0),
                "last": float(o.get("lastPrice") or 0),
                "volume": vol,
                "open_interest": oi,
                "iv": round(iv * 100, 1),
                "delta_estimate": round(delta, 2),
                "breakeven": round(breakeven, 2),
                "max_loss_per_contract": round(premium * 100, 2),
                "reward_at_t1": round(reward_t1 * 100, 2),
                "reward_at_t2": round(reward_t2 * 100, 2),
                "reward_at_t3": round(reward_t3 * 100, 2),
                "rr_t1": round(rr_t1, 2),
                "rr_t2": round(rr_t2, 2),
                "rr_t3": round(rr_t3, 2),
                "score": score,
                "in_the_money": bool(o.get("inTheMoney")),
            })

    # Sort by score, return top picks
    contracts.sort(key=lambda c: c["score"], reverse=True)
    total = len(contracts)
    top = contracts[:limit]
    if not contracts:
        note = f"No contracts met the R:R ≥ {MIN_RR}:1 + liquidity filters."
    elif total > limit:
        note = f"Showing top {len(top)} of {total} contracts meeting R:R ≥ {MIN_RR}:1"
    else:
        note = f"Found {total} contracts meeting R:R ≥ {MIN_RR}:1"
    return {"contracts": top, "note": note, "total": total}
