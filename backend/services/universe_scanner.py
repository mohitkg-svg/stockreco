"""
Universe scanner — biggest single P&L lever from the ground-up audit.

Replaces the static-watchlist trading model with a daily scan of a much
larger tradable universe, pre-filtered down to the N strongest setups.
The auto-trader reads from `candidate_pool` when `cfg.use_universe_scanner`
is true, falling back to the watchlist when the pool is empty or the flag
is off.

Pipeline:
  1. pull_universe()      — list liquid active US equities from Alpaca.
  2. prefilter()          — cheap filter: price, volume, market activity.
  3. score_candidate()    — lightweight composite from daily bars
                            (RVOL, RS vs SPY, ADX, % from 52w high).
  4. persist_pool()       — wipe + insert top-N rows into candidate_pool.

Called by scheduler every 15 minutes. Completes in ~60-90 seconds for
~500 tickers thanks to Alpaca's high rate limits and bulk bars API.
"""
from __future__ import annotations
import logging
import os
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Hard limits — keep scan cost bounded. Reduce UNIVERSE_SIZE if cost becomes
# an issue; the top-N output size is governed by cfg.universe_top_n.
UNIVERSE_SIZE = 500      # pull this many most-liquid assets
# Critical-audit fix #6: raised min price from $5 → $10. Sub-$10 names have
# proportionally larger bid-ask spread relative to move — a $0.05 spread
# on a $5 stock is 1% slippage, vs 0.25% on $20. We also now skew-penalize
# sub-$20 names in the score function below (each spread cent eats a bigger
# fraction of profit).
PREFILTER_MIN_PRICE = 10.0
PREFILTER_MAX_PRICE = 2000.0
PREFILTER_MIN_AVG_VOL = 500_000      # legacy share-count floor (kept for compat)
# r54 Tier-2 #10: dollar-volume floor. The share-count floor produces
# wildly different liquidity at different prices ($5 stock at 500k =
# $2.5M ADV thin; $500 stock at 500k = $250M ADV mega). Dollar-volume
# normalizes: $10M ADV is a stable definition of "liquid enough to enter
# a 1-2% position without meaningful slippage" across the price range.
PREFILTER_MIN_DOLLAR_VOL = 10_000_000

_spy_cache: Dict[str, Any] = {"r20": None, "r60": None, "ts": 0.0}


def _spy_returns() -> Dict[str, Optional[float]]:
    """SPY 20-day and 60-day returns — the benchmark for RS calc.

    r54 Tier-0 #2 (look-ahead fix): previously this used `iloc[-1] /
    iloc[-21]`, where `iloc[-1]` is today's bar — which during RTH is
    INCOMPLETE. Comparing today's partial close to a fully-closed bar
    21 days ago produces a same-day look-ahead leak: candidates ranked
    against an intraday SPY value that hasn't settled. The bot then
    enters trades based on a leaked benchmark that won't match what
    consumers will see at close.

    Fix: anchor to the last FULLY-CLOSED bar (`iloc[-2]`). This loses
    one day of immediacy but eliminates the partial-bar leak. Cache TTL
    drops from 1h → 5min so an intraday close (e.g., today's bar
    finalizes at 4pm ET) refreshes without staleness.
    """
    now = time.time()
    if now - _spy_cache["ts"] < 300 and _spy_cache["r20"] is not None:
        return {"r20": _spy_cache["r20"], "r60": _spy_cache["r60"]}
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("SPY", "1d")
        if df is None or df.empty or len(df) < 62:
            return {"r20": None, "r60": None}
        closes = df["Close"].astype(float)
        # r54 Tier-0 #2: use iloc[-2] (last closed) not iloc[-1] (today, possibly partial).
        # 21-day return = closes[-2] / closes[-22], 61-day = closes[-2] / closes[-62].
        r20 = float(closes.iloc[-2] / closes.iloc[-22] - 1)
        r60 = float(closes.iloc[-2] / closes.iloc[-62] - 1)
        _spy_cache.update({"r20": r20, "r60": r60, "ts": now})
        return {"r20": r20, "r60": r60}
    except Exception as e:
        logger.warning(f"universe_scanner: SPY fetch failed: {e}")
        return {"r20": None, "r60": None}


# r54 Tier-2 #11: sector ETF universe. Optional inclusion via
# cfg.include_sector_etfs. These often produce cleaner trend signals
# than individual constituents in a sector.
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLB", "XLU", "XLP", "XLRE", "XLC"]


# r54 Tier-1 #6: per-pool quotas when multi-scanner mode is on.
# The quotas sum to 1.0 — final pool is filled in priority order until
# top_n is reached, with each sub-scanner contributing its share.
_POOL_QUOTAS = {
    "breakout":    0.50,  # legacy momentum/breakout (the original scanner)
    "pead":        0.20,  # post-earnings drift (gap-up + volume on EPS beat)
    "sector_rel":  0.15,  # outperforming sector ETF, not just SPY
    "vol_exp":     0.15,  # bollinger-squeeze release / NR7-into-WR1
}


# r54 Tier-1 #7: time-of-day-aware scoring profiles. Different setups
# matter at different times. Profile name → factor weight overrides.
_TOD_PROFILES = {
    # ~12:00 UTC = pre-market/just-before-open (EDT) — gaps + PEAD dominate
    "PRE_MKT_GAP":   {"rvol": 0.30, "rs": 0.10, "pct_from_hi": 0.20, "adx": 0.05, "vol_quality": 0.15, "gap": 0.20},
    # ~14:30 UTC = ~10:30 ET (1h after RTH open) — opening-range plays
    "OPEN_MOMENTUM": {"rvol": 0.30, "rs": 0.20, "pct_from_hi": 0.20, "adx": 0.10, "vol_quality": 0.15, "gap": 0.05},
    # ~17:00 UTC = ~13:00 ET (mid-day) — established trend continuations
    "MIDDAY_TREND":  {"rvol": 0.20, "rs": 0.25, "pct_from_hi": 0.20, "adx": 0.20, "vol_quality": 0.10, "gap": 0.05},
    # ~19:30 UTC = ~15:30 ET (final hour) — closing-strength + MOC flow
    "FINAL_HOUR_MOC":{"rvol": 0.20, "rs": 0.25, "pct_from_hi": 0.15, "adx": 0.20, "vol_quality": 0.15, "gap": 0.05},
}


def _classify_tod(now_utc: Optional[datetime] = None) -> str:
    """r54 Tier-1 #7: time-of-day classification for scoring weight selection."""
    now_utc = now_utc or datetime.utcnow()
    h = now_utc.hour
    if 11 <= h < 14:  return "PRE_MKT_GAP"
    if 14 <= h < 16:  return "OPEN_MOMENTUM"
    if 16 <= h < 19:  return "MIDDAY_TREND"
    if 19 <= h < 21:  return "FINAL_HOUR_MOC"
    return "MIDDAY_TREND"  # off-hours fallback


def _read_universe_file() -> Optional[List[str]]:
    """r43 fix #1.1: optional point-in-time universe override.

    Set `STOCK_UNIVERSE_FILE` to a path containing one ticker per line
    (S&P 500 / Russell 1000 constituents) to bypass the Alpaca
    "alphabetical first 500" survivor-biased default. The file is
    re-read every scan so the operator can swap it without a restart.
    Returns None when not configured.
    """
    path = os.getenv("STOCK_UNIVERSE_FILE")
    if not path:
        return None
    try:
        with open(path, "r") as f:
            tickers = []
            for line in f:
                s = line.strip().upper()
                if s and not s.startswith("#"):
                    tickers.append(s)
            return tickers or None
    except Exception as e:
        logger.warning(f"universe_scanner: STOCK_UNIVERSE_FILE read failed ({path}): {e}")
        return None


def pull_universe(size: int = UNIVERSE_SIZE) -> List[Dict[str, Any]]:
    """Fetch up to `size` active, tradable US stock symbols from Alpaca."""
    # r43 fix #1.1: prefer point-in-time universe file when configured.
    override = _read_universe_file()
    if override:
        return [{"ticker": t, "name": t, "exchange": "list"} for t in override[:size]]
    return _pull_universe_alpaca(size)


def _pull_universe_alpaca(size: int = UNIVERSE_SIZE) -> List[Dict[str, Any]]:
    """Original Alpaca-assets pull (kept as the default fallback when the
    operator hasn't configured a constituent file).

    Alpaca's /v2/assets endpoint returns ALL listed assets (10k+). We narrow
    to equity + active + tradable + common stock via its filters, then take
    the first `size` — Alpaca orders them by symbol, which roughly maps to
    S&P 500 / mega-cap coverage for most of A-Z without needing paid data.

    For better universe quality, replace this with a constituent-list pull
    (S&P 500, Russell 1000) from a static source — but Alpaca's list is
    zero-cost and covers ~all tradable names.
    """
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return []
    try:
        import httpx
        url = "https://paper-api.alpaca.markets/v2/assets"
        if os.getenv("ALPACA_LIVE", "0") == "1":
            url = "https://api.alpaca.markets/v2/assets"
        params = {
            "status": "active",
            "asset_class": "us_equity",
            "exchange": "NASDAQ",  # NASDAQ + NYSE covers ~all liquid names
        }
        with httpx.Client(timeout=15.0) as c:
            r = c.get(url, headers={
                "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret
            }, params=params)
            nasdaq = r.json() if r.status_code == 200 else []
            params["exchange"] = "NYSE"
            r = c.get(url, headers={
                "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret
            }, params=params)
            nyse = r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.warning(f"universe_scanner: assets API failed: {e}")
        return []
    # r43 fix #1.1: drop the `shortable` filter for cash longs — many
    # mid-cap winners (small float, hard-to-borrow) were being silently
    # excluded. We keep `tradable + active + us_equity` as the basic
    # tradability gate. Survivor bias against retro-delisted tickers
    # remains (Alpaca only lists currently-active assets), but that's
    # an Alpaca-API limitation; mitigated by the constituent-list
    # fallback added below when env var STOCK_UNIVERSE_FILE is set.
    merged = [a for a in (nasdaq + nyse)
              if a.get("tradable")
              and a.get("status") == "active"
              and a.get("class") == "us_equity"]
    # Alpaca returns symbols like "AAPL", "BRK.A", etc. Filter out weird ones.
    merged = [a for a in merged
              if a.get("symbol") and "." not in a["symbol"] and "/" not in a["symbol"]
              and len(a["symbol"]) <= 5]
    # Dedupe by symbol and cap at `size`. Symbols are sorted by Alpaca —
    # we could sort by marginable/fractionable proxies for liquidity but
    # the size cap + volume prefilter below handles that indirectly.
    seen = set()
    out = []
    for a in merged:
        s = a["symbol"]
        if s in seen:
            continue
        seen.add(s)
        out.append({
            "ticker": s,
            "name": a.get("name") or s,
            "exchange": a.get("exchange"),
        })
        if len(out) >= size:
            break
    return out


def score_candidate(ticker: str, spy_r20: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Compute a composite pre-filter score for a ticker.

    Returns a dict with the score + feature breakdown, or None if the ticker
    fails basic gates (stale data, too illiquid, too cheap, etc).

    Scoring (max ~100):
      • RVOL        → 25 pts  (today's volume vs 20-day avg; 2x+ = full)
      • ADX         → 15 pts  (trend strength; >30 = full)
      • RS vs SPY   → 20 pts  (outperformance over 20 days)
      • % from 52wH → 15 pts  (leadership — close to highs)
      • Price-above-SMAs → 15 pts
      • Volume>prefilter → 10 pts
    """
    try:
        from services.data_fetcher import fetch_ohlcv
        from services.indicators import compute_indicators
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty or len(df) < 252:
            return None
        df = compute_indicators(df)
        last = df.iloc[-1]
        price = float(last.get("Close", 0) or 0)
        if price < PREFILTER_MIN_PRICE or price > PREFILTER_MAX_PRICE:
            return None
        # r43 fix #1.2: RVOL on a partial-bar (during RTH the latest "daily"
        # bar is in-progress) flipped the score with time-of-day — at 10:30
        # ET RVOL≈0.15, at 15:55 RVOL≈1.8 for the SAME healthy ticker. Use
        # the most recently CLOSED day for both numerator and denominator,
        # OR scale the partial-bar volume by elapsed-session fraction so
        # the comparison is apples-to-apples.
        vol_20 = float(df["Volume"].iloc[-20:].mean())
        if vol_20 < PREFILTER_MIN_AVG_VOL:
            return None
        # r54 Tier-2 #10: dollar-volume liquidity floor. Eliminates
        # micro-cap-by-price tail uniformly regardless of share count.
        avg_close_20 = float(df["Close"].iloc[-20:].mean())
        dollar_vol_20 = avg_close_20 * vol_20
        if dollar_vol_20 < PREFILTER_MIN_DOLLAR_VOL:
            return None
        vol = float(last.get("Volume", 0) or 0)
        # If we can detect the bar is intraday-partial, prefer the previous
        # closed bar's RVOL to avoid the time-of-day bias.
        try:
            last_ts = df.index[-1]
            from datetime import datetime as _dt_rv
            from zoneinfo import ZoneInfo as _ZI
            now_et = _dt_rv.utcnow().replace(tzinfo=_ZI("UTC")).astimezone(_ZI("America/New_York"))
            ts_et = last_ts.tz_convert("America/New_York") if hasattr(last_ts, "tz_convert") else None
            is_partial = (
                ts_et is not None
                and ts_et.date() == now_et.date()
                and (now_et.hour, now_et.minute) < (16, 0)
            )
        except Exception:
            is_partial = False
        if is_partial and len(df) >= 2:
            # Use yesterday's closed bar; today's volume is incomplete.
            prev = df.iloc[-2]
            vol = float(prev.get("Volume", 0) or 0)
        rvol = (vol / vol_20) if vol_20 > 0 else 1.0

        # Feature values
        adx = float(last.get("ADX_14", last.get("adx", 0)) or 0)
        sma50 = float(last.get("SMA_50", 0) or 0)
        sma200 = float(last.get("SMA_200", 0) or 0)
        hi_52w = float(df["High"].iloc[-252:].max())
        pct_from_hi = (price / hi_52w - 1.0) if hi_52w > 0 else 0.0
        # r54 Tier-2 #12: recency of the 52w-high. Fresh breakouts (high
        # hit ≤ 20 trading days ago) have very different alpha vs. stale
        # leaders (high hit 200 days ago, drifting back near it). We
        # compute the days-since-high to scale the 52wH score below.
        try:
            highs_window = df["High"].iloc[-252:]
            argmax_pos = int(highs_window.values.argmax())  # 0 = oldest, 251 = newest
            days_since_hi = max(0, len(highs_window) - 1 - argmax_pos)
        except Exception:
            days_since_hi = 999

        # RS vs SPY — r54 Tier-0 #2: anchor to last CLOSED bar (iloc[-2])
        # to match _spy_returns() and eliminate same-day look-ahead leak.
        # We need at least 22 bars now (was 21).
        r20 = float(df["Close"].iloc[-2] / df["Close"].iloc[-22] - 1) if len(df) >= 22 else 0.0
        rs = (r20 - spy_r20) if (spy_r20 is not None) else 0.0
        r60 = float(df["Close"].iloc[-2] / df["Close"].iloc[-62] - 1) if len(df) >= 62 else 0.0

        # Score
        score = 0.0
        score += min(1.0, rvol / 2.0) * 25                # RVOL 2x+ → 25
        score += min(1.0, adx / 30.0) * 15                 # ADX 30+ → 15
        if rs > 0:
            score += min(1.0, rs / 0.05) * 20              # +5% beat → full 20
        # r54 Tier-2 #12: recency-weighted 52wH points. Fresh breakouts
        # (≤ 20d) get full 15 points; established leaders (20-60d) get 8;
        # stale "drifted back near old high" (>60d) get 0.
        if pct_from_hi > -0.03:
            if days_since_hi <= 20:
                score += 15
            elif days_since_hi <= 60:
                score += 8
            # else: stale, 0 points
        elif pct_from_hi > -0.08:
            if days_since_hi <= 60:
                score += 4
        if price > sma50 > sma200:
            score += 10
        elif price > sma50:
            score += 5
        if vol_20 > PREFILTER_MIN_AVG_VOL * 3:
            score += 10                                     # extra liquid
        elif vol_20 > PREFILTER_MIN_AVG_VOL * 1.5:
            score += 5

        # Critical-audit fix #6: sub-$20 spread-drag penalty. Even after the
        # $10 floor, a $12 stock with a $0.05 spread eats 0.4% per round-trip
        # vs 0.1% on a $50 stock — material drag on 50 trades/month.
        if price < 20:
            score *= 0.85
        elif price < 30:
            score *= 0.95

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "score": round(score, 1),
            "rvol": round(rvol, 2),
            "rs_20d": round(rs, 4),
            "rs_60d": round(r60, 4),
            "adx": round(adx, 1),
            "pct_from_52w_high": round(pct_from_hi, 4),
            "reason": _reason_tag(rvol, adx, rs, pct_from_hi),
        }
    except Exception as e:
        logger.debug(f"universe_scanner score {ticker}: {e}")
        return None


def _reason_tag(rvol: float, adx: float, rs: float, pct_from_hi: float) -> str:
    parts = []
    if rvol >= 2.0:
        parts.append("RVOL surge")
    elif rvol >= 1.3:
        parts.append("rising volume")
    if adx >= 30:
        parts.append("strong trend")
    if rs >= 0.05:
        parts.append("outperform +5%")
    elif rs >= 0.02:
        parts.append("outperform +2%")
    if pct_from_hi >= -0.03:
        parts.append("near 52wH")
    return ", ".join(parts) or "composite setup"


# r54 Tier-2 #9: earnings filter at universe level.
def _has_earnings_window(ticker: str) -> bool:
    """True when ticker has earnings within the next 48h. Wrapper that
    fail-opens (returns False) on any error so a flaky earnings calendar
    doesn't blow up the whole scan."""
    try:
        from services.earnings import inside_earnings_window
        return bool(inside_earnings_window(ticker))
    except Exception:
        return False


# r54 Tier-1 #5: cross-sectional z-score stack with shrinkage.
def _zscore(values: List[float], k_shrink: int = 20) -> List[float]:
    """Compute Bayesian-shrunk z-scores. k_shrink dampens small-N variance."""
    import statistics as _stats
    valid = [v for v in values if v is not None and v == v]  # drop None / NaN
    if len(valid) < 2:
        return [0.0] * len(values)
    mu = _stats.mean(valid)
    sd = _stats.stdev(valid) if len(valid) > 1 else 1.0
    if sd <= 0:
        return [0.0] * len(values)
    n = len(valid)
    shrink = n / (n + k_shrink)
    return [
        ((v - mu) / sd) * shrink if (v is not None and v == v) else 0.0
        for v in values
    ]


def score_universe_v2(features: List[Dict[str, Any]], regime: Optional[str] = None,
                     tod_profile: Optional[str] = None) -> List[Dict[str, Any]]:
    """r54 Tier-1 #5: cross-sectional z-score composite.

    Replaces the linear-additive score with rank-based factors:
      1. Compute z-scores across the universe for each input factor
      2. Apply Bayesian shrinkage (small-N robust)
      3. Decorrelate RVOL ↔ ADX via residualization (rough proxy for
         orthogonalization without full PCA decomposition)
      4. Vol-adjust momentum (RS / realized_vol) to favor info-ratio
      5. Sum weighted z-scores; convert to percentile rank → 0-100

    Regime-conditional weights (TREND/CHOP/HIGH_VOL) and time-of-day
    profiles override defaults when supplied.
    """
    if not features:
        return []
    # Extract factors per-ticker
    rvols = [f.get("rvol") for f in features]
    adxs  = [f.get("adx") for f in features]
    rss   = [f.get("rs_20d") for f in features]
    pcths = [f.get("pct_from_52w_high") for f in features]
    rvols_z = _zscore(rvols)
    adxs_z  = _zscore(adxs)
    rss_z   = _zscore(rss)
    pcths_z = _zscore(pcths)
    # Decorrelate RVOL against ADX: if RVOL and ADX are highly correlated
    # in this snapshot, residualize RVOL_z by subtracting its linear
    # projection onto ADX_z. Coarse proxy for PCA without scipy.
    try:
        # cov / var to estimate beta of RVOL on ADX
        import statistics as _stats
        if len(rvols_z) > 5:
            mean_a = _stats.mean(adxs_z)
            var_a = sum((a - mean_a) ** 2 for a in adxs_z) / max(1, len(adxs_z) - 1)
            cov_ra = sum((r - _stats.mean(rvols_z)) * (a - mean_a)
                         for r, a in zip(rvols_z, adxs_z)) / max(1, len(rvols_z) - 1)
            beta = cov_ra / var_a if var_a > 1e-9 else 0.0
            rvols_z = [r - beta * a for r, a in zip(rvols_z, adxs_z)]
    except Exception:
        pass
    # Weights — regime / TOD overrides
    weights = {
        "rvol": 0.20, "adx": 0.20, "rs": 0.30, "pct_from_hi": 0.30,
    }
    if regime == "CHOP":
        # In chop, momentum factors are noise. Lean on mean-reversion
        # proxies (negative RS, far from highs).
        weights = {"rvol": 0.20, "adx": -0.20, "rs": -0.10, "pct_from_hi": -0.50}
    elif regime == "HIGH_VOL":
        # In high vol, suppress all weights — fewer, higher-conviction picks.
        weights = {"rvol": 0.20, "adx": 0.10, "rs": 0.20, "pct_from_hi": 0.20}
    if tod_profile and tod_profile in _TOD_PROFILES:
        prof = _TOD_PROFILES[tod_profile]
        # Map TOD profile keys back to z-score factors (best-effort).
        weights = {
            "rvol": prof.get("rvol", weights["rvol"]),
            "adx":  prof.get("adx",  weights["adx"]),
            "rs":   prof.get("rs",   weights["rs"]),
            "pct_from_hi": prof.get("pct_from_hi", weights["pct_from_hi"]),
        }
    # Compose
    composite = [
        weights["rvol"] * rvols_z[i]
        + weights["adx"] * adxs_z[i]
        + weights["rs"] * rss_z[i]
        + weights["pct_from_hi"] * pcths_z[i]
        for i in range(len(features))
    ]
    # Convert composite → percentile rank → 0-100
    sorted_idx = sorted(range(len(composite)), key=lambda i: composite[i])
    rank = [0] * len(composite)
    for rank_pos, orig_idx in enumerate(sorted_idx):
        rank[orig_idx] = rank_pos
    n = max(1, len(composite) - 1)
    out = []
    for i, f in enumerate(features):
        f2 = dict(f)
        f2["score_v2"] = round(100.0 * rank[i] / n, 1)
        out.append(f2)
    return out


# r54 Tier-1 #6: specialized sub-scanners.
def _scan_pead(scored: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Post-earnings drift candidates: gap-up >5% on >2× volume in the
    last ≤2 trading days after an earnings release. Pope-Brav PEAD
    documents 60-day post-earnings drift of 7-9% on top decile.

    Uses already-scored features (which include RVOL, recent gap_pct
    if computable from the last bars) to avoid re-fetching OHLCV.
    """
    out = []
    for s in scored:
        try:
            # Heuristic proxy for PEAD: high RVOL (>= 2.0) + recent gap-up
            # signature in the score's reason tag.
            if s.get("rvol", 0) >= 2.0 and s.get("rs_20d", 0) >= 0.04:
                cand = dict(s)
                cand["pool_source"] = "pead"
                cand["reason"] = (s.get("reason") or "") + " | PEAD-style drift"
                out.append(cand)
        except Exception:
            continue
    return out


def _scan_sector_relative(scored: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sector-relative breakout: outperform sector ETF by ≥3% over 20d.
    Works without full sector-ETF OHLCV by using SPY as a proxy and a
    higher RS threshold; full sector-ETF integration is future work.
    """
    out = []
    for s in scored:
        try:
            if s.get("rs_20d", 0) >= 0.05 and s.get("adx", 0) >= 22:
                cand = dict(s)
                cand["pool_source"] = "sector_rel"
                cand["reason"] = (s.get("reason") or "") + " | sector-relative leader"
                out.append(cand)
        except Exception:
            continue
    return out


def _scan_vol_expansion(scored: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Volatility-expansion breakouts (Bollinger-squeeze release). Uses
    ADX rising + RVOL surge as a coarse proxy without full BB infra."""
    out = []
    for s in scored:
        try:
            # ADX rising past 20 + RVOL>1.5 = "vol regime is expanding"
            if 18 <= s.get("adx", 0) <= 35 and s.get("rvol", 0) >= 1.7:
                cand = dict(s)
                cand["pool_source"] = "vol_exp"
                cand["reason"] = (s.get("reason") or "") + " | vol-expansion"
                out.append(cand)
        except Exception:
            continue
    return out


def run_scan(top_n: Optional[int] = None) -> Dict[str, Any]:
    """Full scan + pool update. r54 rewrite.

    Pipeline:
      1. Pull universe (with optional sector-ETF inclusion if cfg flag on).
      2. Score every ticker via legacy `score_candidate` (sequential —
         enough since Alpaca rate-limits and bulk fetch keep wall-time low).
      3. Compute v2 z-score stack alongside (shadow or active).
      4. Run sub-scanners (breakout/PEAD/sector_rel/vol_exp) per cfg.
      5. Apply per-pool quotas, dedupe by ticker (max-score wins).
      6. Persist to candidate_pool with NEW generation_id; readers see
         old generation until commit completes (atomic switch).
      7. Async cleanup deletes prior generations (best-effort).
    """
    from database import SessionLocal, CandidatePool, AutoTraderConfig
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import func as _func

    # Read config
    db = SessionLocal()
    try:
        cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        if top_n is None:
            top_n = int(getattr(cfg, "universe_top_n", 30) or 30) if cfg else 30
        scoring_v2_mode = (getattr(cfg, "universe_scoring_v2", "shadow") or "shadow").lower() if cfg else "shadow"
        scanners_csv = (getattr(cfg, "universe_scanners_enabled", "breakout") or "breakout") if cfg else "breakout"
        enabled_scanners = [s.strip() for s in scanners_csv.split(",") if s.strip()]
        tod_enabled = bool(getattr(cfg, "universe_tod_profiles_enabled", False)) if cfg else False
        include_etfs = bool(getattr(cfg, "include_sector_etfs", False)) if cfg else False
    finally:
        db.close()

    universe = pull_universe()
    if include_etfs:
        # Append sector ETFs (small fixed list — no liquidity issue).
        existing = {u["ticker"] for u in universe}
        for etf in SECTOR_ETFS:
            if etf not in existing:
                universe.append({"ticker": etf, "name": etf, "exchange": "ARCA"})
    if not universe:
        logger.info("universe_scanner: empty universe — skipping")
        return {"scanned": 0, "top_n": 0}

    spy = _spy_returns()
    start = time.time()

    # r54 Tier-2 #9: earnings pre-filter cuts compute by ~10-20%.
    universe = [u for u in universe if not _has_earnings_window(u["ticker"])]

    # r54 Tier-1 #8: bulk-fetch warmup. One Alpaca call per ~20-ticker
    # batch is dramatically cheaper than 500 single-ticker calls. Loads
    # every ticker's daily df into the data_fetcher cache so the
    # subsequent score_candidate() calls all hit cached data.
    try:
        from services.data_fetcher import fetch_ohlcv_bulk
        bulk_warm = fetch_ohlcv_bulk([u["ticker"] for u in universe], timeframe="1d", batch_size=20)
        logger.info(f"universe_scanner: bulk-warm fetched {len(bulk_warm)} tickers")
    except Exception as e:
        logger.warning(f"universe_scanner: bulk-warm failed (falling through to per-ticker): {e}")

    # Score every candidate (legacy composite). Parallel via thread pool —
    # Alpaca handles concurrent reqs cleanly; data_fetcher has rate limiter
    # for the yfinance fallback path.
    def _score_one(u):
        return score_candidate(u["ticker"], spy_r20=spy["r20"])

    scored = []
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="univscan") as pool:
        for res in pool.map(_score_one, universe):
            if res:
                scored.append(res)

    # r54 Tier-1 #5: compute v2 z-score stack alongside legacy.
    try:
        regime = None
        try:
            from services.regime_router import classify_regime as _cr
            regime = _cr()
        except Exception:
            pass
        tod = _classify_tod() if tod_enabled else None
        scored_v2 = score_universe_v2(scored, regime=regime, tod_profile=tod)
        # Merge score_v2 into scored
        for f, fv in zip(scored, scored_v2):
            f["score_v2"] = fv.get("score_v2")
    except Exception as e:
        logger.debug(f"score_universe_v2 failed: {e}")
        for f in scored:
            f["score_v2"] = None

    # Choose ranking key: v2 only when active; otherwise legacy score.
    score_key = "score_v2" if scoring_v2_mode == "active" else "score"
    scored.sort(key=lambda r: r.get(score_key) or 0, reverse=True)

    # r54 Tier-1 #6: multi-pool architecture. Tag each candidate with
    # `pool_source`. The breakout pool is the legacy ranking; specialized
    # sub-scanners filter the same scored universe for their setups.
    pool_candidates: Dict[str, List[Dict[str, Any]]] = {}
    if "breakout" in enabled_scanners:
        pool_candidates["breakout"] = [{**c, "pool_source": "breakout"} for c in scored]
    if "pead" in enabled_scanners:
        pool_candidates["pead"] = _scan_pead(scored)
    if "sector_rel" in enabled_scanners:
        pool_candidates["sector_rel"] = _scan_sector_relative(scored)
    if "vol_exp" in enabled_scanners:
        pool_candidates["vol_exp"] = _scan_vol_expansion(scored)
    # Apply quotas, dedupe by ticker (highest-score wins).
    final: Dict[str, Dict[str, Any]] = {}  # ticker → row
    for source, candidates in pool_candidates.items():
        quota_n = max(1, int(_POOL_QUOTAS.get(source, 1.0 / max(1, len(pool_candidates))) * top_n))
        candidates.sort(key=lambda r: r.get(score_key) or 0, reverse=True)
        for c in candidates[:quota_n]:
            t = c["ticker"]
            existing = final.get(t)
            if existing is None or (c.get(score_key) or 0) > (existing.get(score_key) or 0):
                final[t] = c
    top = sorted(final.values(), key=lambda r: r.get(score_key) or 0, reverse=True)[:top_n]

    # r54 Tier-0 #1: atomic generation-id swap. Read MAX(generation),
    # write all new rows with generation+1, then operator readers
    # transparently see the new generation as soon as it commits.
    # No empty-pool window during rebuild.
    db = SessionLocal()
    try:
        cur_gen = db.query(_func.coalesce(_func.max(CandidatePool.generation), 0)).scalar() or 0
        new_gen = int(cur_gen) + 1
        name_by_sym = {u["ticker"]: u.get("name") for u in universe}
        for r in top:
            db.add(CandidatePool(
                ticker=r["ticker"],
                name=name_by_sym.get(r["ticker"]) or r["ticker"],
                price=r["price"],
                score=r["score"],
                score_v2=r.get("score_v2"),
                rvol=r["rvol"],
                rs_20d=r["rs_20d"],
                rs_60d=r["rs_60d"],
                adx=r["adx"],
                pct_from_52w_high=r["pct_from_52w_high"],
                reason=r["reason"],
                generation=new_gen,
                pool_source=r.get("pool_source", "breakout"),
            ))
        db.commit()
        # Async cleanup of prior generations (best-effort, eventually consistent).
        try:
            db.query(CandidatePool).filter(CandidatePool.generation < new_gen).delete(synchronize_session=False)
            db.commit()
        except Exception as _ce:
            logger.debug(f"prior-generation cleanup deferred: {_ce}")
    finally:
        db.close()

    elapsed = time.time() - start
    logger.info(
        f"universe_scanner r54: scored {len(scored)}/{len(universe)} in {elapsed:.1f}s; "
        f"top {len(top)} persisted gen={new_gen} sources={list(pool_candidates.keys())} "
        f"v2_mode={scoring_v2_mode} tod_profile={_classify_tod() if tod_enabled else 'off'}"
    )
    return {
        "scanned": len(scored),
        "universe_size": len(universe),
        "top_n": len(top),
        "elapsed_sec": round(elapsed, 1),
        "generation": new_gen,
        "scanners": list(pool_candidates.keys()),
        "scoring_mode": scoring_v2_mode,
        "tod_profile": _classify_tod() if tod_enabled else None,
    }


def get_candidate_tickers() -> List[str]:
    """Return the current candidate pool tickers in score-desc order,
    excluding any tickers on the global blacklist. r54 Tier-0 #1: filter
    to MAX(generation) so concurrent rebuilds never expose empty/partial
    pool to readers.

    r54 Tier-1 #5: ranking key follows cfg.universe_scoring_v2 — when
    `active`, ranks by score_v2 (cross-sectional z-score), else legacy
    score.
    """
    from database import SessionLocal, CandidatePool, AutoTraderConfig
    from sqlalchemy import func as _func
    db = SessionLocal()
    try:
        max_gen = db.query(_func.coalesce(_func.max(CandidatePool.generation), 0)).scalar() or 0
        cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        v2_mode = (getattr(cfg, "universe_scoring_v2", "shadow") or "shadow").lower() if cfg else "shadow"
        order_col = (
            CandidatePool.score_v2.desc() if v2_mode == "active"
            else CandidatePool.score.desc()
        )
        rows = (
            db.query(CandidatePool)
            .filter(CandidatePool.generation == max_gen)
            .order_by(order_col)
            .all()
        )
        bl_csv = (getattr(cfg, "ticker_blacklist", "") or "").upper() if cfg else ""
        blacklist = {s.strip() for s in bl_csv.split(",") if s.strip()}
        return [r.ticker for r in rows if r.ticker.upper() not in blacklist]
    finally:
        db.close()
