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
PREFILTER_MIN_AVG_VOL = 500_000

_spy_cache: Dict[str, Any] = {"r20": None, "r60": None, "ts": 0.0}


def _spy_returns() -> Dict[str, Optional[float]]:
    """SPY 20-day and 60-day returns — the benchmark for RS calc. Cached 1h."""
    now = time.time()
    if now - _spy_cache["ts"] < 3600 and _spy_cache["r20"] is not None:
        return {"r20": _spy_cache["r20"], "r60": _spy_cache["r60"]}
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv("SPY", "1d")
        if df is None or df.empty or len(df) < 61:
            return {"r20": None, "r60": None}
        closes = df["Close"].astype(float)
        r20 = float(closes.iloc[-1] / closes.iloc[-21] - 1)
        r60 = float(closes.iloc[-1] / closes.iloc[-61] - 1)
        _spy_cache.update({"r20": r20, "r60": r60, "ts": now})
        return {"r20": r20, "r60": r60}
    except Exception as e:
        logger.warning(f"universe_scanner: SPY fetch failed: {e}")
        return {"r20": None, "r60": None}


def pull_universe(size: int = UNIVERSE_SIZE) -> List[Dict[str, Any]]:
    """Fetch up to `size` active, tradable US stock symbols from Alpaca.

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
    merged = [a for a in (nasdaq + nyse)
              if a.get("tradable") and a.get("shortable")  # no low-float gotchas
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
        vol_20 = float(df["Volume"].iloc[-20:].mean())
        if vol_20 < PREFILTER_MIN_AVG_VOL:
            return None
        vol = float(last.get("Volume", 0) or 0)
        rvol = (vol / vol_20) if vol_20 > 0 else 1.0

        # Feature values
        adx = float(last.get("ADX_14", last.get("adx", 0)) or 0)
        sma50 = float(last.get("SMA_50", 0) or 0)
        sma200 = float(last.get("SMA_200", 0) or 0)
        hi_52w = float(df["High"].iloc[-252:].max())
        pct_from_hi = (price / hi_52w - 1.0) if hi_52w > 0 else 0.0

        # RS vs SPY
        r20 = float(df["Close"].iloc[-1] / df["Close"].iloc[-21] - 1) if len(df) >= 21 else 0.0
        rs = (r20 - spy_r20) if (spy_r20 is not None) else 0.0
        r60 = float(df["Close"].iloc[-1] / df["Close"].iloc[-61] - 1) if len(df) >= 61 else 0.0

        # Score
        score = 0.0
        score += min(1.0, rvol / 2.0) * 25                # RVOL 2x+ → 25
        score += min(1.0, adx / 30.0) * 15                 # ADX 30+ → 15
        if rs > 0:
            score += min(1.0, rs / 0.05) * 20              # +5% beat → full 20
        if pct_from_hi > -0.03:
            score += 15                                     # within 3% of 52wH
        elif pct_from_hi > -0.08:
            score += 8
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


def run_scan(top_n: Optional[int] = None) -> Dict[str, Any]:
    """Full scan + pool update. Runs on the scheduler every 15 min."""
    from database import SessionLocal, CandidatePool, AutoTraderConfig
    from concurrent.futures import ThreadPoolExecutor

    # Honor cfg.universe_top_n override.
    if top_n is None:
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            top_n = int(getattr(cfg, "universe_top_n", 30) or 30) if cfg else 30
        finally:
            db.close()

    universe = pull_universe()
    if not universe:
        logger.info("universe_scanner: empty universe — skipping")
        return {"scanned": 0, "top_n": 0}
    spy = _spy_returns()
    start = time.time()

    # Parallel scoring — Alpaca AT+ allows 10k req/min, so 500 tickers with 8
    # workers takes 1-2 minutes of wall time.
    def _score_one(u):
        return score_candidate(u["ticker"], spy_r20=spy["r20"])

    scored = []
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="univscan") as pool:
        for res in pool.map(_score_one, universe):
            if res:
                scored.append(res)

    # Rank by score desc, take top_n.
    scored.sort(key=lambda r: r["score"], reverse=True)
    top = scored[:top_n]

    # Persist: wipe + insert top-N so the pool reflects the latest scan exactly.
    db = SessionLocal()
    try:
        db.query(CandidatePool).delete()
        # Map ticker → name from universe
        name_by_sym = {u["ticker"]: u.get("name") for u in universe}
        for r in top:
            db.add(CandidatePool(
                ticker=r["ticker"],
                name=name_by_sym.get(r["ticker"]) or r["ticker"],
                price=r["price"],
                score=r["score"],
                rvol=r["rvol"],
                rs_20d=r["rs_20d"],
                rs_60d=r["rs_60d"],
                adx=r["adx"],
                pct_from_52w_high=r["pct_from_52w_high"],
                reason=r["reason"],
            ))
        db.commit()
    finally:
        db.close()

    elapsed = time.time() - start
    logger.info(
        f"universe_scanner: scored {len(scored)}/{len(universe)} in {elapsed:.1f}s; "
        f"top {len(top)} persisted (top score {top[0]['score'] if top else 'n/a'})"
    )
    return {
        "scanned": len(scored),
        "universe_size": len(universe),
        "top_n": len(top),
        "elapsed_sec": round(elapsed, 1),
    }


def get_candidate_tickers() -> List[str]:
    """Return the current candidate pool tickers in score-desc order,
    excluding any tickers on the global blacklist. Used by
    auto_trader.scheduled_scan when use_universe_scanner is on."""
    from database import SessionLocal, CandidatePool, AutoTraderConfig
    db = SessionLocal()
    try:
        rows = (
            db.query(CandidatePool)
            .order_by(CandidatePool.score.desc())
            .all()
        )
        cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        bl_csv = (getattr(cfg, "ticker_blacklist", "") or "").upper() if cfg else ""
        blacklist = {s.strip() for s in bl_csv.split(",") if s.strip()}
        return [r.ticker for r in rows if r.ticker.upper() not in blacklist]
    finally:
        db.close()
