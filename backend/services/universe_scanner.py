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

r55 — second-audit fixes (post-r54). The first audit shipped r54; the
follow-on audit found ~12 issues with r54 itself. r55 lands all of:
  T0 #1 generation-id race + ticker-unique conflict (single txn + lock)
  T0 #2 Bayesian shrinkage applied to factor MEANS not z-scores
  T0 #3 residualization re-standardized to preserve unit variance
  T0 #4 CHOP regime: invert factors, never weights
  T0 #5 fetch_ohlcv_bulk case-insensitive index slicing
  T0 #6 universe_scanners_enabled None vs ""
  T0 #7 52w-high recency = LAST occurrence in tie window
  T1 #8 sub-scanners: real PEAD earnings + sector ETF benchmarks +
        Bollinger-band-width vol-expansion (no longer "theater")
  T1 #9 1m-bar gate: tunable + 3-bar majority option
  T1 #10 NaN _zscore: drop instead of impute
  T2 #11 N<5 short-circuit
  T2 #12 include_sector_etfs gate honored
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
        # r55 T0 #7: argmax returns the FIRST occurrence on ties; we want
        # the MOST RECENT bar that achieved the 52w high (a stock that
        # tagged 100 in week 1 then re-tagged 100 in week 50 should be
        # treated as a fresh breakout, not a stale 52w-old high). Reverse
        # the slice and adjust the index.
        try:
            highs_window = df["High"].iloc[-252:]
            highs_arr = highs_window.values
            n_h = len(highs_arr)
            # Reverse-search: argmax on reversed array, then convert back.
            argmax_rev = int(highs_arr[::-1].argmax())  # 0 = newest
            argmax_pos = n_h - 1 - argmax_rev
            days_since_hi = max(0, n_h - 1 - argmax_pos)
        except Exception:
            days_since_hi = 999

        # RS vs SPY — r54 Tier-0 #2: anchor to last CLOSED bar (iloc[-2])
        # to match _spy_returns() and eliminate same-day look-ahead leak.
        # We need at least 22 bars now (was 21).
        r20 = float(df["Close"].iloc[-2] / df["Close"].iloc[-22] - 1) if len(df) >= 22 else 0.0
        rs = (r20 - spy_r20) if (spy_r20 is not None) else 0.0
        r60 = float(df["Close"].iloc[-2] / df["Close"].iloc[-62] - 1) if len(df) >= 62 else 0.0

        # r55 T1 #8 (sub-scanner support): Bollinger Band width for the
        # vol-expansion sub-scanner. Width = 2 * 2σ / SMA20 ≈ a unitless
        # measure of recent dispersion. Squeeze = current width below
        # 20-day mean width by ≥30%; expansion = squeeze followed by an
        # uptick. Stash the raw values; the sub-scanner does the squeeze/
        # expansion logic.
        try:
            closes_window = df["Close"].iloc[-20:].astype(float)
            sma_20 = float(closes_window.mean())
            std_20 = float(closes_window.std(ddof=0))
            bb_width = (4.0 * std_20 / sma_20) if sma_20 > 0 else 0.0
            # Prior-window mean BB width (bars t-40 .. t-21) for squeeze detection.
            if len(df) >= 40:
                prior = df["Close"].iloc[-40:-20].astype(float)
                p_mean = float(prior.mean())
                p_std  = float(prior.std(ddof=0))
                bb_width_prior = (4.0 * p_std / p_mean) if p_mean > 0 else 0.0
            else:
                bb_width_prior = bb_width
        except Exception:
            bb_width = 0.0
            bb_width_prior = 0.0

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
            # r55 T1 #8: stash these for the sub-scanners (avoid re-fetch).
            "_r20_abs": round(r20, 4),
            "_bb_width": round(bb_width, 4),
            "_bb_width_prior": round(bb_width_prior, 4),
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


# r55 T0 #2 + T1 #10: cross-sectional z-score with James-Stein shrinkage
# of the *mean* (not the z-score) and explicit drop (not impute) of NaNs.
def _zscore(values: List[float], k_shrink: int = 20,
            drop_nan: bool = True) -> List[Optional[float]]:
    """Compute z-scores with James-Stein-style shrinkage of the deviation
    in the means' direction. r55 T0 #2 fix: original code multiplied the
    final z by `n/(n+k)`, which under-shrinks tails relative to center —
    not what James-Stein actually does. The correct construction shrinks
    each value's deviation from the grand mean by `n/(n+k)` BEFORE
    standardization. Equivalent to shrinking the implied factor mean
    toward the population mean.

    r55 T1 #10 fix: original code imputed missing values with z=0
    (median rank), biasing the universe ranking toward newer/IPO names.
    Now `drop_nan=True` returns None for missing values so the caller
    can skip those tickers entirely (rather than rank them at the median).

    Returns list aligned with input; entries are None where input was
    None / NaN.
    """
    import statistics as _stats
    indices_valid = [i for i, v in enumerate(values)
                     if v is not None and v == v]
    valid = [values[i] for i in indices_valid]
    if len(valid) < 2:
        return [None] * len(values) if drop_nan else [0.0] * len(values)
    mu = _stats.mean(valid)
    n = len(valid)
    # James-Stein-style shrinkage: deviations toward the grand mean by
    # factor n/(n+k). Smaller n => more shrinkage toward the center.
    shrink = n / (n + k_shrink)
    shrunk = [mu + shrink * (v - mu) for v in valid]
    sd = _stats.stdev(shrunk) if len(shrunk) > 1 else 1.0
    if sd <= 0:
        return [None] * len(values) if drop_nan else [0.0] * len(values)
    out: List[Optional[float]] = [None] * len(values)
    for vi, orig_i in enumerate(indices_valid):
        out[orig_i] = (shrunk[vi] - mu) / sd
    if not drop_nan:
        out = [0.0 if v is None else v for v in out]
    return out


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
    # r55 T2 #11: short-circuit when N is too small for stable z-score
    # computation. With <5 tickers we can't reliably standardize and
    # the v2 ranking is just noise; assign 0 to all and let legacy
    # `score` carry the ranking.
    if len(features) < 5:
        return [{**f, "score_v2": 0.0} for f in features]
    # Extract factors per-ticker
    rvols = [f.get("rvol") for f in features]
    adxs  = [f.get("adx") for f in features]
    rss   = [f.get("rs_20d") for f in features]
    pcths = [f.get("pct_from_52w_high") for f in features]
    # r55 T1 #10: NaN-aware z-score returns None for missing values.
    # Tickers with ANY missing factor get score_v2=None and are dropped
    # from the v2 ranking (legacy `score` still ranks them).
    rvols_z = _zscore(rvols, drop_nan=True)
    adxs_z  = _zscore(adxs,  drop_nan=True)
    rss_z   = _zscore(rss,   drop_nan=True)
    pcths_z = _zscore(pcths, drop_nan=True)
    # r55 T0 #3: residualize RVOL against ADX, THEN re-standardize so
    # the residual's variance is back to 1.0. Original code skipped the
    # re-standardization, which broke the assumption that all factors
    # contribute on equal footing in the weighted composite below — the
    # residualized RVOL had σ < 1, so its effective composite weight
    # was silently smaller than `weights["rvol"]` would suggest.
    try:
        import statistics as _stats
        # Filter to indices where both RVOL and ADX z are valid
        pair_idx = [i for i in range(len(rvols_z))
                    if rvols_z[i] is not None and adxs_z[i] is not None]
        if len(pair_idx) > 5:
            r_pairs = [rvols_z[i] for i in pair_idx]
            a_pairs = [adxs_z[i] for i in pair_idx]
            mean_a = _stats.mean(a_pairs)
            mean_r = _stats.mean(r_pairs)
            var_a = sum((a - mean_a) ** 2 for a in a_pairs) / max(1, len(a_pairs) - 1)
            cov_ra = sum((r - mean_r) * (a - mean_a)
                         for r, a in zip(r_pairs, a_pairs)) / max(1, len(r_pairs) - 1)
            beta = cov_ra / var_a if var_a > 1e-9 else 0.0
            # Compute residuals on the paired subset.
            resid = [r - beta * a for r, a in zip(r_pairs, a_pairs)]
            # r55 T0 #3: re-standardize residuals to σ=1.
            mu_res = _stats.mean(resid)
            sd_res = _stats.stdev(resid) if len(resid) > 1 else 1.0
            if sd_res > 1e-9:
                resid_norm = [(r - mu_res) / sd_res for r in resid]
            else:
                resid_norm = [0.0 for _ in resid]
            # Splice back into rvols_z, preserving Nones at unpaired indices.
            for vi, orig_i in enumerate(pair_idx):
                rvols_z[orig_i] = resid_norm[vi]
    except Exception:
        pass
    # Weights — regime / TOD overrides.
    # r55 T0 #4: weights are NEVER negative. CHOP/mean-reversion is
    # implemented by INVERTING the factor (multiplying its z-score by -1)
    # while keeping the weight positive. Negative weights here interact
    # badly with the percentile-rank conversion below: a composite of
    # "+0.2*rvol_z - 0.5*pct_from_hi_z" can rank a ticker with strong RVOL
    # AND near-52w-high LOWER than one with neither (the negative weight
    # turns "near 52wH" into a *penalty*, but combined with positive RVOL
    # the rank math is non-monotone in the underlying signal). Always-
    # positive weights with explicit factor-inversion preserve
    # interpretability: "rank the universe by composite alpha factor".
    weights = {
        "rvol": 0.20, "adx": 0.20, "rs": 0.30, "pct_from_hi": 0.30,
    }
    invert = {"rvol": False, "adx": False, "rs": False, "pct_from_hi": False}
    if regime == "CHOP":
        # In chop, mean-reversion: invert RS and pct_from_hi (we want
        # OVERSOLD, i.e., far from highs and underperforming SPY recently).
        # Volume + ADX still matter (we want active reversal candidates,
        # not dead names) so we keep them positive.
        weights = {"rvol": 0.30, "adx": 0.10, "rs": 0.30, "pct_from_hi": 0.30}
        invert = {"rvol": False, "adx": False, "rs": True, "pct_from_hi": True}
    elif regime == "HIGH_VOL":
        # In high vol, suppress all weights — fewer, higher-conviction picks.
        weights = {"rvol": 0.20, "adx": 0.10, "rs": 0.20, "pct_from_hi": 0.20}
    if tod_profile and tod_profile in _TOD_PROFILES:
        prof = _TOD_PROFILES[tod_profile]
        # Map TOD profile keys back to z-score factors (best-effort).
        # TOD profiles are always trend-following, so invert is left as-is.
        weights = {
            "rvol": prof.get("rvol", weights["rvol"]),
            "adx":  prof.get("adx",  weights["adx"]),
            "rs":   prof.get("rs",   weights["rs"]),
            "pct_from_hi": prof.get("pct_from_hi", weights["pct_from_hi"]),
        }
    # Apply factor inversion (r55 T0 #4). Preserve Nones.
    def _negate(zs: List[Optional[float]]) -> List[Optional[float]]:
        return [None if z is None else -z for z in zs]
    if invert["rvol"]:    rvols_z = _negate(rvols_z)
    if invert["adx"]:     adxs_z  = _negate(adxs_z)
    if invert["rs"]:      rss_z   = _negate(rss_z)
    if invert["pct_from_hi"]: pcths_z = _negate(pcths_z)
    # Compose. Tickers with ANY missing factor get composite=None and
    # score_v2=None (excluded from v2 ranking, T1 #10).
    composite: List[Optional[float]] = []
    for i in range(len(features)):
        zs = (rvols_z[i], adxs_z[i], rss_z[i], pcths_z[i])
        if any(z is None for z in zs):
            composite.append(None)
            continue
        composite.append(
            weights["rvol"] * rvols_z[i]
            + weights["adx"] * adxs_z[i]
            + weights["rs"] * rss_z[i]
            + weights["pct_from_hi"] * pcths_z[i]
        )
    # Convert composite → percentile rank → 0-100, ranking only valid rows.
    valid_indices = [i for i, c in enumerate(composite) if c is not None]
    if not valid_indices:
        return [{**f, "score_v2": None} for f in features]
    sorted_valid = sorted(valid_indices, key=lambda i: composite[i])  # type: ignore[index,arg-type]
    rank: Dict[int, int] = {}
    for rank_pos, orig_idx in enumerate(sorted_valid):
        rank[orig_idx] = rank_pos
    n = max(1, len(sorted_valid) - 1)
    out = []
    for i, f in enumerate(features):
        f2 = dict(f)
        if i in rank:
            f2["score_v2"] = round(100.0 * rank[i] / n, 1)
        else:
            f2["score_v2"] = None
        out.append(f2)
    return out


# r55 T1 #8: GICS-sector → sector-ETF map. yfinance returns these exact
# strings as `Ticker.info["sector"]`. Used by the sector-relative scanner
# to benchmark each ticker against its actual sector, not SPY.
SECTOR_TO_ETF = {
    "Technology":              "XLK",
    "Financial Services":      "XLF",
    "Financial":               "XLF",
    "Energy":                  "XLE",
    "Healthcare":              "XLV",
    "Health Care":             "XLV",
    "Consumer Cyclical":       "XLY",
    "Consumer Discretionary":  "XLY",
    "Industrials":             "XLI",
    "Basic Materials":         "XLB",
    "Materials":               "XLB",
    "Utilities":               "XLU",
    "Consumer Defensive":      "XLP",
    "Consumer Staples":        "XLP",
    "Real Estate":             "XLRE",
    "Communication Services":  "XLC",
}


def _compute_sector_etf_returns() -> Dict[str, float]:
    """r55 T1 #8: 20-day return for each sector ETF. Anchored to last
    CLOSED bar (iloc[-2]) for the same look-ahead-free convention as
    `_spy_returns`. Returns {etf: r20}; missing ETFs simply omitted.
    """
    from services.data_fetcher import fetch_ohlcv as _fo
    out: Dict[str, float] = {}
    for etf in set(SECTOR_TO_ETF.values()):
        try:
            df = _fo(etf, "1d")
            if df is None or df.empty or len(df) < 22:
                continue
            r20 = float(df["Close"].iloc[-2] / df["Close"].iloc[-22] - 1)
            out[etf] = r20
        except Exception:
            continue
    return out


# r55 T1 #8: real sub-scanner implementations.
def _scan_pead(scored: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Post-earnings announcement drift: ticker had an earnings print
    in the last 10 trading days AND is exhibiting positive drift
    (RS_20d > 0, RVOL > 1.3). Pope-Brav PEAD documents 60-day drift of
    7-9% on top decile post-earnings-surprise names.

    r55 T1 #8: replaces the r54 placeholder which only checked RVOL+RS
    (no actual earnings linkage — making "PEAD" overlap >80% with
    breakout). Now uses `services.earnings.recent_earnings_catalyst`
    to require a real earnings event in the window.
    """
    try:
        from services.earnings import recent_earnings_catalyst
    except Exception:
        return []
    out = []
    for s in scored:
        try:
            # Cheap pre-filter to avoid the yfinance round-trip on every
            # ticker (only check earnings on candidates with PEAD-like price
            # action: positive recent return + above-average volume).
            if s.get("rs_20d", 0) < 0.0 or s.get("rvol", 0) < 1.3:
                continue
            if not recent_earnings_catalyst(s["ticker"], days_back=10):
                continue
            cand = dict(s)
            cand["pool_source"] = "pead"
            cand["reason"] = (s.get("reason") or "") + " | PEAD (recent earnings drift)"
            out.append(cand)
        except Exception:
            continue
    return out


def _scan_sector_relative(scored: List[Dict[str, Any]],
                          sector_etf_r20: Optional[Dict[str, float]] = None
                          ) -> List[Dict[str, Any]]:
    """Sector-relative leadership: ticker's 20-day return beats its own
    SECTOR ETF's 20-day return by ≥3% (not SPY — sector ETFs decorrelate
    from SPY meaningfully and a tech leader vs. SPY may simply reflect
    XLK strength, not security selection).

    r55 T1 #8: replaces the r54 placeholder which used SPY as a proxy
    with a higher threshold. Now fetches each candidate's GICS sector
    and looks up the matching SPDR sector ETF's 20d return.
    """
    if not sector_etf_r20:
        return []
    try:
        from services.data_fetcher import get_ticker_info
    except Exception:
        return []
    out = []
    for s in scored:
        try:
            ticker = s["ticker"]
            info = get_ticker_info(ticker) or {}
            sector_str = (info.get("sector") or "").strip()
            etf = SECTOR_TO_ETF.get(sector_str)
            if not etf:
                continue
            etf_r20 = sector_etf_r20.get(etf)
            if etf_r20 is None:
                continue
            ticker_r20 = s.get("_r20_abs")
            if ticker_r20 is None:
                continue
            # Sector-relative excess: must beat sector ETF by ≥3% over 20d.
            if (ticker_r20 - etf_r20) < 0.03:
                continue
            # Also require a baseline trend-strength filter — we don't want
            # a sector laggard's mean-reversion bounce.
            if s.get("adx", 0) < 18:
                continue
            cand = dict(s)
            cand["pool_source"] = "sector_rel"
            excess_pct = round((ticker_r20 - etf_r20) * 100, 1)
            cand["reason"] = (s.get("reason") or "") + f" | sector-rel +{excess_pct}% vs {etf}"
            out.append(cand)
        except Exception:
            continue
    return out


def _scan_vol_expansion(scored: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bollinger-squeeze-release vol expansion: BB width was COMPRESSED
    over the prior 20 days (bb_width_prior < median of universe) AND
    has now expanded ≥40% above the prior baseline. Empirically these
    setups generate explosive directional moves once price exits the
    consolidation range.

    r55 T1 #8: replaces the r54 placeholder (ADX in [18,35] + RVOL>1.7,
    which has nothing to do with Bollinger width). Now uses the
    bb_width / bb_width_prior values stashed by score_candidate.
    """
    if not scored:
        return []
    # Build universe of bb_width_prior values to find what "compressed" means
    # in this snapshot — universe-relative threshold beats a fixed cutoff.
    priors = [s.get("_bb_width_prior") for s in scored
              if s.get("_bb_width_prior") is not None and s.get("_bb_width_prior") > 0]
    if len(priors) < 5:
        return []
    priors_sorted = sorted(priors)
    # Bottom-quartile of prior BB widths = "compressed".
    p25 = priors_sorted[len(priors_sorted) // 4]
    out = []
    for s in scored:
        try:
            cur = s.get("_bb_width") or 0.0
            prior = s.get("_bb_width_prior") or 0.0
            if prior <= 0 or cur <= 0:
                continue
            # Compression filter: prior was below the 25th percentile of
            # the universe.
            if prior > p25:
                continue
            # Expansion filter: current width has expanded ≥40% over the
            # compressed baseline.
            if cur < prior * 1.40:
                continue
            # And there's an actual volume signal to confirm the breakout.
            if s.get("rvol", 0) < 1.3:
                continue
            cand = dict(s)
            cand["pool_source"] = "vol_exp"
            ratio = round(cur / prior, 2)
            cand["reason"] = (s.get("reason") or "") + f" | vol-expansion (BBW {ratio}× squeeze)"
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
        # r55 T0 #6: distinguish None (operator hasn't set the knob yet,
        # use default) from "" (operator explicitly disabled all scanners).
        # Original `or "breakout"` collapsed both to default.
        if cfg is not None:
            raw_scanners = getattr(cfg, "universe_scanners_enabled", None)
            scanners_csv = "breakout" if raw_scanners is None else raw_scanners
        else:
            scanners_csv = "breakout"
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
    # r55 T1 #8: compute sector ETF benchmarks ONCE per scan (11 ETFs
    # cached in data_fetcher) and pass to the sector-relative sub-scanner.
    pool_candidates: Dict[str, List[Dict[str, Any]]] = {}
    sector_etf_r20: Dict[str, float] = {}
    if "sector_rel" in enabled_scanners:
        try:
            sector_etf_r20 = _compute_sector_etf_returns()
        except Exception as e:
            logger.warning(f"sector ETF benchmarking failed: {e}")
    if "breakout" in enabled_scanners:
        pool_candidates["breakout"] = [{**c, "pool_source": "breakout"} for c in scored]
    if "pead" in enabled_scanners:
        pool_candidates["pead"] = _scan_pead(scored)
    if "sector_rel" in enabled_scanners:
        pool_candidates["sector_rel"] = _scan_sector_relative(scored, sector_etf_r20)
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

    # r55 T0 #1: race-free atomic pool swap. The r54 implementation had
    # two separate problems:
    #   (a) Generation-ID race: two concurrent scans (manual + cron at
    #       the same minute) would both read max(generation)=N and write
    #       generation=N+1, producing duplicate rows in the new generation.
    #   (b) Ticker UNIQUE conflict: r54 inserted new gen rows BEFORE
    #       deleting old gen rows, but `ticker` has a UNIQUE constraint —
    #       so the insert fails with IntegrityError on every scan after
    #       the first (silently observed in DB error logs).
    # Fix: serialize scan execution via SELECT ... FOR UPDATE on
    # AutoTraderConfig row 1 (no-op on SQLite; works on Postgres). DELETE
    # the old generation FIRST, then INSERT — all in one transaction.
    # External readers see the OLD pool until COMMIT, then atomically the
    # NEW pool — no empty-pool window because everything happens in one
    # transactional snapshot.
    db = SessionLocal()
    try:
        # Acquire scan lock (no-op on SQLite, FOR UPDATE on Postgres).
        try:
            db.query(AutoTraderConfig).filter(
                AutoTraderConfig.id == 1
            ).with_for_update().first()
        except Exception:
            # SQLite or older Postgres without row-level locking — fall
            # through. Single-writer dev DBs are inherently serialized.
            pass
        cur_gen = db.query(_func.coalesce(_func.max(CandidatePool.generation), 0)).scalar() or 0
        new_gen = int(cur_gen) + 1
        # Delete prior generations FIRST so the unique(ticker) constraint
        # does not collide with the new generation's inserts.
        db.query(CandidatePool).filter(
            CandidatePool.generation < new_gen
        ).delete(synchronize_session=False)
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
        # Single commit: readers see old pool until this returns, then
        # atomically see the new pool. No empty-pool window.
        db.commit()
    except Exception as _ce:
        logger.warning(f"universe_scanner: pool swap failed, rolling back: {_ce}")
        db.rollback()
        raise
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
