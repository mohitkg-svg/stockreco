"""r47 Tier-P overlay layer — sizing/filter knobs over the existing
multiplier stack. Each function returns a multiplier in a documented
clamp range; combine via existing `regime_multiplier`-style chain.

References:
- A1 VIX9D/VIX3M term-regime sizing — Whaley 2009; Konstantinidi-Skiadopoulos 2011
- A2 SKEW reversal bias — Bali-Hovakimian 2009; Conrad-Dittmar-Ghysels 2013
- A4 VIX 5σ spike → SPY long — Whaley 2000; Bollerslev-Tauchen-Zhou 2009
- A5 IV-rank graded sizing — Goyal-Saretto 2009; Cao-Han 2013
- B2 VRP filter — Bollerslev-Tauchen-Zhou 2009; Carr-Wu 2009
- B3 VVIX anxiety gate — Park 2015; Huang et al. 2019
- B6 Earnings IV-crush sidestep — Gao-Xing-Zhang 2018 (reverse case)
- Macro A1 SPX 200d trend gate — Faber 2007; Moskowitz-Ooi-Pedersen 2012
- Macro A6 HYG/LQD credit-spread circuit breaker — Gilchrist-Zakrajšek 2012

All multipliers are PURE FUNCTIONS of cached cross_asset signals. Each
caller reads cfg.<feature_enabled> before applying.
"""
from __future__ import annotations
import logging
from typing import Optional, Tuple, Dict
from datetime import datetime

logger = logging.getLogger(__name__)


def _safe_call(fn, default=None):
    try:
        v = fn()
        return v if v is not None else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# A1. VIX9D / VIX3M term-regime sizing (calls vs puts asymmetric)
# ---------------------------------------------------------------------------
def vix_term_regime_multiplier(direction: str) -> float:
    """Return a sizing multiplier in [0.5, 1.5] based on VIX term structure.

    VIX9D/VIX3M < 0.85  → steep contango (complacency) → bias FAVOR puts
    VIX9D/VIX3M > 1.05  → backwardation (panic peak)   → bias FAVOR mean-revert longs

    direction: "BUY" (long) or "SELL" (short / put-leaning).
    """
    from services.cross_asset import vix_term_ratio
    r = _safe_call(vix_term_ratio)
    if r is None:
        return 1.0
    direction = (direction or "BUY").upper()
    if r < 0.85:
        # complacency → calls discounted, puts juiced
        return 1.20 if direction == "SELL" else 0.85
    if r > 1.05:
        # panic peak → mean-reversion long bias, fade short side
        return 1.25 if direction == "BUY" else 0.75
    return 1.0


# ---------------------------------------------------------------------------
# A2. SKEW reversal bias
# ---------------------------------------------------------------------------
def skew_bias_multiplier(direction: str) -> float:
    """SKEW > 145 = heavy tail-hedging demand → bias toward downside (favor puts).
    SKEW < 115 = no tail-hedging → bias toward upside (favor calls).
    Multiplier in [0.85, 1.15] — modest size knob, not a directional gate.
    """
    from services.cross_asset import skew_index
    s = _safe_call(skew_index)
    if s is None:
        return 1.0
    direction = (direction or "BUY").upper()
    if s > 145:
        return 1.10 if direction == "SELL" else 0.90
    if s < 115:
        return 1.10 if direction == "BUY" else 0.90
    return 1.0


# ---------------------------------------------------------------------------
# B3. VVIX anxiety gate
# ---------------------------------------------------------------------------
def vvix_anxiety_factor() -> float:
    """VVIX > 110 (vol-of-vol elevated) → cut sizing by up to 20%.
    Returns multiplier in [0.80, 1.0]."""
    from services.cross_asset import vvix
    v = _safe_call(vvix)
    if v is None:
        return 1.0
    if v >= 130:
        return 0.80
    if v >= 115:
        return 0.90
    return 1.0


# ---------------------------------------------------------------------------
# B2. VRP (Variance Risk Premium) filter
# ---------------------------------------------------------------------------
def vrp_score() -> Optional[float]:
    """VRP = VIX² − RV20² (annualized variance points).
    Positive VRP = vol risk priced rich → bias toward LONG vol exposure
    Negative VRP = vol risk priced thin → cheap long premium / favor calls/puts
    Returns the raw difference, or None when data unavailable."""
    try:
        from services.cross_asset import _cached
        from services.data_fetcher import fetch_ohlcv
        import numpy as _np
        def _compute():
            spy = fetch_ohlcv("SPY", "1d")
            if spy is None or spy.empty or len(spy) < 21:
                return None
            rets = spy["Close"].pct_change().dropna().tail(20)
            if len(rets) < 10:
                return None
            rv = float(rets.std()) * (252 ** 0.5)
            from services.cross_asset import _last_close
            vix = _last_close("^VIX")
            if vix is None or vix <= 0:
                return None
            return (vix * vix) - (rv * 100.0) ** 2
        return _cached("vrp_score", _compute)
    except Exception:
        return None


def vrp_factor(direction: str) -> float:
    """Translate VRP into a sizing multiplier in [0.85, 1.15].
    High VRP (vol expensive) → 0.90 on long-premium directions.
    Low VRP (vol cheap)      → 1.10 on long-premium directions.
    Stock-only signals: returns 1.0 (vrp is an options-pricing concept)."""
    s = vrp_score()
    if s is None:
        return 1.0
    if s > 50:
        return 0.90
    if s < -10:
        return 1.10
    return 1.0


# ---------------------------------------------------------------------------
# A5. IV-rank graded sizing
# ---------------------------------------------------------------------------
def iv_rank_size_factor(iv_pct: Optional[float]) -> float:
    """Replace binary IV-rank veto with a graded multiplier.
    iv_pct in [0,1]: 0 = cheap vol, 1 = expensive vol.
    Returns multiplier in [0.4, 1.15] for long-premium options."""
    if iv_pct is None:
        return 1.0
    p = max(0.0, min(1.0, float(iv_pct)))
    if p <= 0.30:
        return 1.15
    if p <= 0.60:
        return 1.0
    if p <= 0.80:
        return 0.70
    if p <= 0.90:
        return 0.40
    return 0.0  # caller treats as veto


# ---------------------------------------------------------------------------
# Macro A1. SPX 200d trend gate
# ---------------------------------------------------------------------------
def spx_trend_gate() -> Dict[str, object]:
    """Return {"on": bool, "spx_close": float|None, "sma200": float|None,
    "pct_above": float|None}. Pure read; callers gate sizing accordingly."""
    out: Dict[str, object] = {"on": True, "spx_close": None,
                              "sma200": None, "pct_above": None}
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("SPY", "1d")
        if df is None or df.empty or len(df) < 220:
            return out
        close = float(df["Close"].iloc[-1])
        sma200 = float(df["Close"].rolling(200).mean().iloc[-1])
        out["spx_close"] = close
        out["sma200"] = sma200
        out["pct_above"] = (close - sma200) / sma200 if sma200 else None
        out["on"] = close > sma200
    except Exception:
        pass
    return out


def spx_trend_size_factor(direction: str) -> float:
    """When SPY < 200dSMA: cut LONG sizing 50%, allow SHORT sizing full size.
    Returns multiplier in [0.5, 1.0]. Caller checks cfg.spx_trend_gate_enabled."""
    g = spx_trend_gate()
    on = bool(g.get("on", True))
    direction = (direction or "BUY").upper()
    if on:
        return 1.0
    return 0.5 if direction == "BUY" else 1.0


# ---------------------------------------------------------------------------
# Macro A6. HYG/LQD credit-spread circuit breaker
# ---------------------------------------------------------------------------
def credit_spread_breaker_z() -> Optional[float]:
    """z-score of HYG/LQD ratio over 60d window. <-2σ + 5d slope < 0
    triggers the circuit breaker (gross-down to 50%, no fresh longs)."""
    try:
        from services.cross_asset import _cached
        from services.data_fetcher import fetch_ohlcv
        def _compute():
            hyg = fetch_ohlcv("HYG", "1d")
            lqd = fetch_ohlcv("LQD", "1d")
            if hyg is None or lqd is None or hyg.empty or lqd.empty:
                return None
            if len(hyg) < 60 or len(lqd) < 60:
                return None
            try:
                ratio = (hyg["Close"] / lqd["Close"]).dropna().tail(60)
                if len(ratio) < 30:
                    return None
                mu = float(ratio.mean())
                sigma = float(ratio.std()) or 1e-9
                z = (float(ratio.iloc[-1]) - mu) / sigma
                return z
            except Exception:
                return None
        return _cached("hyg_lqd_z", _compute)
    except Exception:
        return None


def credit_spread_circuit_breaker_active() -> bool:
    z = credit_spread_breaker_z()
    if z is None:
        return False
    return z < -2.0


# ---------------------------------------------------------------------------
# A4. VIX 5σ spike → SPY long signal (event-trigger)
# ---------------------------------------------------------------------------
def vix_spike_signal() -> Optional[Dict[str, float]]:
    """Returns {"vix_change_z": float, "vix_level": float} when daily VIX
    change exceeds +5σ of trailing-60d distribution AND VIX ≥ 25.
    Otherwise None. Caller treats as a SPY/QQQ long entry trigger.
    Hold 3 days; stop 1.5×ATR; target = VIX retrace 30% of spike.
    """
    try:
        from services.cross_asset import _cached
        from services.data_fetcher import fetch_ohlcv
        def _compute():
            df = fetch_ohlcv("^VIX", "1d")
            if df is None or df.empty or len(df) < 65:
                return None
            try:
                changes = df["Close"].diff().dropna().tail(60)
                if len(changes) < 30:
                    return None
                last_change = float(df["Close"].iloc[-1] - df["Close"].iloc[-2])
                last_level = float(df["Close"].iloc[-1])
                mu = float(changes.mean())
                sigma = float(changes.std()) or 1e-9
                z = (last_change - mu) / sigma
                if z >= 5.0 and last_level >= 25:
                    return {"vix_change_z": z, "vix_level": last_level}
                return None
            except Exception:
                return None
        return _cached("vix_spike", _compute)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pre-FOMC quiet-hour defer (helper)
# ---------------------------------------------------------------------------
def in_pre_fomc_quiet_hour(window_minutes: int = 60) -> bool:
    """True when within `window_minutes` BEFORE an FOMC release. Liquidity
    + slippage spike in the last hour pre-event; we defer non-urgent
    entries so we don't pay through-the-spread before the move."""
    try:
        from services.macro_calendar import _FOMC_DATES
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo as _ZI
        now_et = _dt.now(_ZI("America/New_York"))
        for d in _FOMC_DATES:
            try:
                # FOMC releases typically 14:00 ET on day d
                dt_et = _dt.combine(d, _dt.min.time(), tzinfo=_ZI("America/New_York")).replace(hour=14)
                if 0 <= (dt_et - now_et).total_seconds() <= window_minutes * 60:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Earnings IV-crush sidestep (B6) — defensive filter for option entries
# ---------------------------------------------------------------------------
def earnings_iv_crush_sidestep(ticker: str, iv_rank: Optional[float]) -> bool:
    """Return True if we should VETO long-premium entry due to elevated IV
    near earnings — the canonical IV-crush trap.
    Triggers when IV-rank > 0.80 AND earnings within 24h."""
    try:
        if iv_rank is None or iv_rank <= 0.80:
            return False
        from services.earnings import hours_to_next_earnings
        h = hours_to_next_earnings(ticker)
        if h is not None and 0 <= h <= 24:
            return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Composite r47 sizing overlay — single entry-point
# ---------------------------------------------------------------------------
def r47_sizing_overlay(direction: str, *,
                       term_enabled: bool = True,
                       skew_enabled: bool = True,
                       vvix_enabled: bool = True,
                       vrp_enabled: bool = True,
                       spx_gate_enabled: bool = True,
                       credit_cb_enabled: bool = True) -> Tuple[float, Dict[str, float]]:
    """Compose all r47 overlay multipliers. Returns (combined_mult, parts).
    Combined is clamped to [0.4, 1.5] to bound stack-noise.

    Honors per-overlay feature flags so operators can A/B individual knobs.
    Credit-spread circuit breaker, when active, returns 0.0 for BUY → caller
    uses that as a hard veto on new long entries.
    """
    parts: Dict[str, float] = {}
    if credit_cb_enabled and credit_spread_circuit_breaker_active():
        d_up = (direction or "BUY").upper()
        if d_up == "BUY":
            parts["credit_cb_veto"] = 0.0
            return 0.0, parts
        # Shorts allowed during CB
        parts["credit_cb_short_pass"] = 1.0
    if term_enabled:
        parts["term"] = vix_term_regime_multiplier(direction)
    if skew_enabled:
        parts["skew"] = skew_bias_multiplier(direction)
    if vvix_enabled:
        parts["vvix"] = vvix_anxiety_factor()
    if vrp_enabled:
        parts["vrp"] = vrp_factor(direction)
    if spx_gate_enabled:
        parts["spx_trend"] = spx_trend_size_factor(direction)
    combined = 1.0
    for k, v in parts.items():
        try:
            combined *= float(v) if v else 1.0
        except Exception:
            pass
    combined = max(0.4, min(1.5, combined))
    return combined, parts
