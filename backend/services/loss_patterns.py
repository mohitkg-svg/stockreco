"""r53 Tier-3 A: Loss-fingerprint pre-trade veto.

`services/post_mortem.py` produces structured forensics on every losing
trade — `verdict`, `findings[].title`, `lessons[]` — and writes them to
`AutoTrade.post_mortem` JSON. Until now nothing read those back. This
module aggregates fingerprints across closed losers, computes a Bayes-
style lift score against winners, and exposes a veto that fires when a
new signal matches a high-lift losing fingerprint.

The veto runs as a gate inside `consider_signal` after the existing
liquidity/regime/sector gates and before the entry submission. Mode-
flagged via `cfg.loss_pattern_mode = {off, shadow, active}` so we can
shadow-log for a week before promoting to active.

Why this is the highest-priority Tier-3 idea: it learns ONLY from the
mistakes this account has actually made, not from generic literature.
The post-mortem fingerprints are concrete (e.g., "Stop too tight",
"Against daily trend", "No follow-through"), each maps to a re-runnable
pre-trade check, and the lift denominator (winners with same fingerprint)
is a hard statistical guard against over-fitting to a recent unlucky
streak.

Hard guards against over-fitting:
  - `_FINGERPRINT_MIN_OCCURRENCES = 5`: don't gate on a fingerprint with
    fewer than 5 losing examples.
  - `_FINGERPRINT_MIN_LIFT = 1.5`: only veto when the fingerprint is
    1.5x more common in losers than in winners.
  - `_LOOKBACK_DAYS = 90`: rolling window so the gate naturally
    decays old patterns.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_FINGERPRINT_MIN_OCCURRENCES = 5
_FINGERPRINT_MIN_LIFT = 1.5
_LOOKBACK_DAYS = 90

# Cache the aggregated fingerprints — recomputed every 6h. Operator can
# force-refresh via the admin endpoint.
_pattern_cache: Dict[str, Tuple[Dict[str, dict], float]] = {}
_PATTERN_CACHE_TTL = 6 * 3600.0


def _extract_fingerprints(post_mortem) -> List[str]:
    """Pull the fingerprint tokens we'll match against new signals.
    Tokens are the verdict + each finding's title (lowercased and
    stripped). Lessons are NOT used as fingerprints — they're prose.

    `post_mortem` may be a dict (already-decoded), a JSON string (the DB
    stores it as Text), or None. Handles all three shapes.
    """
    if not post_mortem:
        return []
    if isinstance(post_mortem, str):
        import json as _json
        try:
            post_mortem = _json.loads(post_mortem)
        except Exception:
            return []
    if not isinstance(post_mortem, dict):
        return []
    out: List[str] = []
    v = (post_mortem.get("verdict") or "").strip().lower()
    if v:
        out.append(f"verdict:{v}")
    for f in (post_mortem.get("findings") or []):
        if isinstance(f, dict):
            t = (f.get("title") or "").strip().lower()
            if t:
                out.append(f"finding:{t}")
    return out


def aggregate_fingerprints(days: int = _LOOKBACK_DAYS) -> Dict[str, dict]:
    """Compute fingerprint frequencies across closed losers AND winners
    in the last `days`. Returns
        {fingerprint: {n_losers, n_winners, lift, vetoable}}
    where `lift = (n_losers / total_losers) / (n_winners / total_winners)`
    and `vetoable = (n_losers >= 5 AND lift >= 1.5)`.
    """
    import time
    now_t = time.time()
    cache_key = str(days)
    cached = _pattern_cache.get(cache_key)
    if cached and now_t - cached[1] < _PATTERN_CACHE_TTL:
        return cached[0]
    from database import SessionLocal as _SL_lp, AutoTrade as _AT_lp
    db = _SL_lp()
    losers_fp: Dict[str, int] = {}
    winners_fp: Dict[str, int] = {}
    total_losers = 0
    total_winners = 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        rows = (db.query(_AT_lp)
                .filter(_AT_lp.closed_at.isnot(None))
                .filter(_AT_lp.closed_at >= cutoff)
                .filter(_AT_lp.post_mortem.isnot(None))
                .all())
        for r in rows:
            pm = r.post_mortem
            if not pm:
                continue
            tokens = _extract_fingerprints(pm)
            if not tokens:
                continue
            pl = float(r.realized_pl or 0)
            if pl < 0:
                total_losers += 1
                for t in set(tokens):  # dedupe within trade
                    losers_fp[t] = losers_fp.get(t, 0) + 1
            elif pl > 0:
                total_winners += 1
                for t in set(tokens):
                    winners_fp[t] = winners_fp.get(t, 0) + 1
    finally:
        db.close()

    out: Dict[str, dict] = {}
    if total_losers == 0:
        return out
    for fp, n_l in losers_fp.items():
        n_w = winners_fp.get(fp, 0)
        loser_freq = n_l / max(1, total_losers)
        winner_freq = (n_w / total_winners) if total_winners else 0.0
        # Lift: how much more common in losers than winners.
        # When winners=0, treat as a strong-but-bounded signal: lift=10.
        if winner_freq > 0:
            lift = loser_freq / winner_freq
        else:
            lift = 10.0 if n_l >= _FINGERPRINT_MIN_OCCURRENCES else 1.0
        vetoable = (n_l >= _FINGERPRINT_MIN_OCCURRENCES
                    and lift >= _FINGERPRINT_MIN_LIFT)
        out[fp] = {
            "n_losers": n_l,
            "n_winners": n_w,
            "loser_freq": round(loser_freq, 3),
            "winner_freq": round(winner_freq, 3),
            "lift": round(lift, 2),
            "vetoable": vetoable,
        }
    _pattern_cache[cache_key] = (out, now_t)
    return out


# ---------- Pre-trade fingerprint matchers --------------------------------
# Each maps a fingerprint string → a function that takes (signal_view, ctx)
# and returns True if THIS new signal would have produced the same
# fingerprint at exit time. Conservative on missing data (return False).


def _stop_too_tight(signal: dict, _ctx: dict) -> bool:
    """Fingerprint: 'finding:hit t1 then reversed into the stop' or
    'finding:stop too tight'. Pre-trade match: stop is < 1.0×ATR from
    entry."""
    try:
        entry = float(signal.get("entry") or 0)
        stop = float(signal.get("stop_loss") or 0)
        atr = float(signal.get("atr") or 0)
        if entry <= 0 or stop <= 0 or atr <= 0:
            return False
        return abs(entry - stop) < atr * 1.0
    except Exception:
        return False


def _against_daily_trend(signal: dict, _ctx: dict) -> bool:
    """Fingerprint: 'finding:against daily trend' / 'verdict:countertrend
    entry'. Pre-trade match: BUY signal but Close < SMA200, or SELL signal
    but Close > SMA200."""
    try:
        side = (signal.get("signal_type") or "").upper()
        close = float(signal.get("close") or signal.get("entry") or 0)
        sma200 = float(signal.get("sma200") or 0)
        if close <= 0 or sma200 <= 0:
            return False
        if side == "BUY" and close < sma200:
            return True
        if side == "SELL" and close > sma200:
            return True
        return False
    except Exception:
        return False


def _no_volume_confirmation(signal: dict, _ctx: dict) -> bool:
    """Fingerprint: 'finding:no volume confirmation'. Pre-trade match:
    rvol < 0.8 (today's volume below 80% of 20d avg)."""
    try:
        rvol = float(signal.get("rvol") or signal.get("volume_relative") or 0)
        return 0 < rvol < 0.8
    except Exception:
        return False


def _counter_momentum(signal: dict, _ctx: dict) -> bool:
    """Fingerprint: 'finding:counter-momentum entry' / 'verdict:bought a
    falling knife'. Pre-trade: BUY with negative MACD-hist, or SELL with
    positive MACD-hist."""
    try:
        side = (signal.get("signal_type") or "").upper()
        macd_h = float(signal.get("macd_hist") or 0)
        if side == "BUY" and macd_h < 0:
            return True
        if side == "SELL" and macd_h > 0:
            return True
        return False
    except Exception:
        return False


def _high_iv_option(signal: dict, _ctx: dict) -> bool:
    """Fingerprint: 'finding:iv-crush'. Pre-trade: option entry with
    iv > 1.5 × realized_vol (the bot's existing _iv_is_expensive check)."""
    try:
        if not signal.get("is_option"):
            return False
        iv = float(signal.get("iv") or 0)
        rv = float(signal.get("realized_vol") or 0)
        return iv > 0 and rv > 0 and iv > rv * 1.5
    except Exception:
        return False


# Map post-mortem fingerprint substrings → pre-trade matcher.
_MATCHERS: Dict[str, callable] = {
    "stop too tight": _stop_too_tight,
    "tight stop": _stop_too_tight,
    "stop placed": _stop_too_tight,        # PM phrasing: "Stop placed -86×ATR from entry"
    "against daily trend": _against_daily_trend,
    "countertrend": _against_daily_trend,
    "counter-momentum": _counter_momentum,
    "no volume confirmation": _no_volume_confirmation,
    "iv crush": _high_iv_option,
    "iv-crush": _high_iv_option,
}


def loss_pattern_veto(signal_view: dict, context: Optional[dict] = None) -> Optional[str]:
    """Returns the matched fingerprint name when this new signal would
    likely produce a known losing pattern; None when no high-lift match
    fires.

    Caller passes a SignalView dict (subset of consider_signal's signal
    dict — entry/stop_loss/target1/atr/rvol/macd_hist/sma200/etc.) plus
    optional context. Failures fail-open (return None) — better to miss
    a veto than block on a data hiccup.
    """
    try:
        ctx = context or {}
        agg = aggregate_fingerprints()
        if not agg:
            return None
        for fp, info in agg.items():
            if not info.get("vetoable"):
                continue
            # Find a matcher that applies to this fingerprint.
            matched_matcher = None
            fp_lower = fp.lower()
            for token, matcher in _MATCHERS.items():
                if token in fp_lower:
                    matched_matcher = matcher
                    break
            if matched_matcher is None:
                # Fingerprint is high-lift but we have no pre-trade
                # checker for it; skip (operator can add matchers later).
                continue
            try:
                if matched_matcher(signal_view, ctx):
                    return f"{fp} (lift={info['lift']}, n_losers={info['n_losers']})"
            except Exception:
                continue
        return None
    except Exception as e:
        logger.debug(f"loss_pattern_veto failed (fail-open): {e}")
        return None


def loss_pattern_summary() -> dict:
    """Operator-facing summary endpoint. Lists every fingerprint with
    its lift, occurrences, and whether the gate would currently veto on
    it. Useful for tuning the matchers."""
    agg = aggregate_fingerprints()
    vetoable = {fp: info for fp, info in agg.items() if info.get("vetoable")}
    return {
        "lookback_days": _LOOKBACK_DAYS,
        "min_occurrences": _FINGERPRINT_MIN_OCCURRENCES,
        "min_lift": _FINGERPRINT_MIN_LIFT,
        "n_fingerprints": len(agg),
        "n_vetoable": len(vetoable),
        "fingerprints": agg,
        "matchers_implemented": sorted(_MATCHERS.keys()),
    }
