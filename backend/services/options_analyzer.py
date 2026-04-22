"""
Option-play selector: given a stock signal (direction + targets + stop),
filter the option chain to contracts with >= 3:1 reward-to-risk and score them.
"""
from typing import Dict, Any, List, Optional
from datetime import datetime
from services.options_fetcher import fetch_option_chain, fetch_expirations


MIN_RR = 2.0  # Lowered from 3.0 — intraday SELL signals (5m/15m/30m) have
              # tight T1-T3 bands (3-7% moves) that rarely clear 3:1 against
              # realistic put premiums, so PUTS tabs stayed empty even when
              # the signal was actionable. 2:1 still gates out junk but lets
              # short-timeframe scalps through.
MIN_DTE = 2         # include weeklies (expirations ≥ 2 days out)
MAX_DTE = 90
MIN_VOLUME = 5
MIN_OI = 25
WEEKLY_DTE = 7
PREMIUM_STOP_PCT = 0.50   # exit when contract has lost 50% of its premium


def _mid_price(o: dict) -> Optional[float]:
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


def _score(rr_t1: float, rr_t2: float, rr_t3: float, dte: int, vol: int, oi: int, iv: float, delta_proxy: float) -> float:
    """0-100 composite score for an option contract."""
    # Use best R:R across targets (T1 often too close for 3:1)
    best_rr = max(rr_t1, rr_t2, rr_t3)
    rr_score = min(best_rr / 5.0, 1.0) * 40                    # 5:1 on any target → 40
    rr2_bonus = min(rr_t2 / 10.0, 1.0) * 15                    # 10:1 on T2 → 15
    # DTE sweet spot 30-60d; weeklies get decent score when R:R is very high
    if 30 <= dte <= 60:
        dte_score = 15
    elif 20 <= dte <= 75:
        dte_score = 12
    elif 8 <= dte <= 19:
        dte_score = 8
    elif dte <= WEEKLY_DTE:
        # Weeklies: good only when best_rr is exceptional (theta burns fast)
        dte_score = 10 if best_rr >= 6 else 5
    else:
        dte_score = 5
    # Liquidity
    liq = min((vol / 100.0), 1.0) * 10 + min((oi / 500.0), 1.0) * 10
    # Delta ~0.4-0.6 is ideal (good gearing, enough intrinsic)
    delta_score = 10 * max(0.0, 1.0 - abs(abs(delta_proxy) - 0.5) / 0.5)
    return round(rr_score + rr2_bonus + dte_score + liq + delta_score, 1)


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

    # Only consider expirations within DTE window
    eligible_exps = [e for e in expirations if MIN_DTE <= _dte(e) <= MAX_DTE]
    if not eligible_exps:
        eligible_exps = expirations[:3]

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
            if vol < MIN_VOLUME or oi < MIN_OI:
                continue
            iv = float(o.get("impliedVolatility") or 0)

            # Keep strikes reasonably close to money (within 25% either side for runner plays)
            if abs(strike - spot) / spot > 0.25:
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

            delta = _delta_proxy(spot, strike, is_call)
            score = _score(rr_t1, rr_t2, rr_t3, dte, vol, oi, iv, delta)

            breakeven = strike + premium if is_call else strike - premium

            # ---- Stop-loss levels for the contract ----
            # 1) Premium stop: exit if contract loses 50% of entry premium (hard $ cap)
            premium_stop = round(premium * (1 - PREMIUM_STOP_PCT), 2)
            max_loss_premium_stop = round(premium * PREMIUM_STOP_PCT * 100, 2)

            # 2) Thesis stop: underlying hits signal's stop_loss → thesis invalidated.
            #    Estimate contract value at that level using delta.
            est_prem_at_underlying_stop = max(0.01, premium + delta * (sl - spot))
            est_loss_at_underlying_stop = round((premium - est_prem_at_underlying_stop) * 100, 2)

            # Effective stop = whichever triggers first (gives smaller loss)
            effective_stop_premium = max(premium_stop, est_prem_at_underlying_stop)
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
                "symbol": o.get("contractSymbol"),
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
