"""Stocktwits retail sentiment — fetch + score + multiplier.

Source: Stocktwits public API (free, no auth, ~200 req/hr rate limit).
  GET https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json
Returns recent messages with per-message sentiment tags ("Bullish",
"Bearish", or untagged). We aggregate last 24h into bullish %, bearish %,
and total message volume.

Signal value: meaningful on retail-driven tickers (small/mid caps, meme
tickers, recently-public names). Near-zero signal on AAPL/NVDA where
institutional flow already dominates. We keep the multiplier envelope
tight (0.96-1.04) because retail sentiment is a lagging/confirmation
signal, not a primary driver.
"""
from __future__ import annotations
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

from database import SessionLocal, SocialSentiment, WatchlistStock, CandidatePool

logger = logging.getLogger(__name__)

_MULT_NEUTRAL = 1.00
_MULT_CONFIRM = 1.04     # retail agrees with our direction
_MULT_CONTRA = 0.96      # retail disagrees

# Thresholds — fraction of tagged messages that must lean one way for the
# reading to count as a direction. We require meaningful volume (≥ 20 messages
# in 24h) before trusting the split; below that the % is too noisy.
_MIN_MESSAGES = 20
_STRONG_LEAN_PCT = 0.60   # ≥60% bullish (of tagged) = strong bullish lean


def _fetch_one(ticker: str) -> Optional[Dict[str, Any]]:
    """Pull recent messages for `ticker` from Stocktwits.

    r43 fix #1.30: paginate up to 5 pages (≤150 messages) so the 24h
    window is faithfully covered for both quiet and viral tickers.
    Previously this fetched ONE page (30 messages) which (a) under-counted
    viral names — total_24h capped at 30 even when 1000 messages existed —
    and (b) span-overshot quiet names, where 30 messages span 5 days but
    only the last 24h were counted, biasing the volume gate.
    """
    msgs = []
    try:
        import httpx
        import os
        import time
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker.upper()}.json"
        cutoff_24h_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
        max_id = None
        
        headers = {"User-Agent": "stockrecs-bot/1.0"}
        api_key = os.getenv("STOCKTWITS_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            
        with httpx.Client(timeout=10.0, headers=headers) as c:
            for _ in range(5):
                params = {"max": max_id} if max_id else None
                r = c.get(url, params=params)
                if r.status_code == 429:
                    logger.warning(f"stocktwits {ticker}: HTTP 429 Rate Limit. Backing off.")
                    time.sleep(60.0)
                    break
                if r.status_code != 200:
                    logger.debug(f"stocktwits {ticker}: HTTP {r.status_code}")
                    break
                data = r.json() or {}
                page = data.get("messages") or []
                if not page:
                    break
                msgs.extend(page)
                # Stop when oldest message in this page is older than 24h.
                oldest_ts_str = page[-1].get("created_at") or ""
                try:
                    oldest_ts = datetime.fromisoformat(oldest_ts_str.replace("Z", "+00:00"))
                    if oldest_ts.tzinfo is None:
                        oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)
                    if oldest_ts.timestamp() < cutoff_24h_ts:
                        break
                except Exception:
                    break
                max_id = page[-1].get("id")
                if not max_id:
                    break
    except Exception as e:
        logger.debug(f"stocktwits {ticker} fetch failed: {e}")
        return None

    if not msgs:
        return None

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    bullish = 0
    bearish = 0
    total_24h = 0
    for m in msgs:
        # Stocktwits timestamp: 'created_at' = ISO 8601 with +0000
        ts_str = m.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < cutoff_24h:
            continue
        total_24h += 1
        sent = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        if sent == "Bullish":
            bullish += 1
        elif sent == "Bearish":
            bearish += 1
    tagged = bullish + bearish
    if tagged == 0:
        # Message volume still useful even if untagged
        return {
            "ticker": ticker.upper(),
            "message_count_24h": total_24h,
            "bullish_pct_24h": None,
            "bearish_pct_24h": None,
        }
    return {
        "ticker": ticker.upper(),
        "message_count_24h": total_24h,
        "bullish_pct_24h": round(bullish / tagged, 3),
        "bearish_pct_24h": round(bearish / tagged, 3),
    }


def _upsert(row: Dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        r = db.query(SocialSentiment).filter(
            SocialSentiment.ticker == row["ticker"]
        ).first()
        if r is None:
            r = SocialSentiment(ticker=row["ticker"], source="stocktwits")
            db.add(r)
        r.message_count_24h = row.get("message_count_24h")
        r.bullish_pct_24h = row.get("bullish_pct_24h")
        r.bearish_pct_24h = row.get("bearish_pct_24h")
        r.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def refresh_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    row = _fetch_one(ticker)
    if row is None:
        return None
    _upsert(row)
    return get_sentiment(ticker)


def refresh_all(max_workers: int = 2) -> Dict[str, Any]:
    """Pull sentiment for watchlist + candidate pool. Stocktwits rate-limits
    unauth traffic to ~200/hr; we stay well under with 2 workers."""
    from concurrent.futures import ThreadPoolExecutor
    db = SessionLocal()
    try:
        tickers = set(s.ticker for s in db.query(WatchlistStock).all())
        tickers |= set(r.ticker for r in db.query(CandidatePool).all())
    finally:
        db.close()
    tickers = sorted(tickers)
    if not tickers:
        return {"checked": 0, "total": 0}
    ok = 0
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="stocktwits") as ex:
        for res in ex.map(lambda t: refresh_ticker(t), tickers):
            if res is not None:
                ok += 1
    logger.info(f"social_sentiment: refreshed {ok}/{len(tickers)} tickers")
    return {"checked": ok, "total": len(tickers)}


def get_sentiment(ticker: str) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        r = db.query(SocialSentiment).filter(
            SocialSentiment.ticker == ticker.upper()
        ).first()
        if r is None:
            return None
        return {
            "ticker": r.ticker,
            "source": r.source,
            "message_count_24h": r.message_count_24h,
            "bullish_pct_24h": r.bullish_pct_24h,
            "bearish_pct_24h": r.bearish_pct_24h,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
    finally:
        db.close()


_MAX_MARKETCAP_FOR_SOCIAL_SIGNAL = 50_000_000_000  # $50B — above this the tape already priced it in


def _is_retail_moveable(ticker: str) -> bool:
    """Retail sentiment only informative on small/mid caps. Above $50B market
    cap the tape already reflects retail flow; sentiment adds noise, not signal."""
    try:
        from services.fundamentals import get_fundamentals
        f = get_fundamentals(ticker)
        if not f:
            # No fundamentals yet → default to trusting sentiment.
            # Erring on the side of applying the signal when we don't know.
            return True
        mcap = f.get("market_cap")
        if mcap is None:
            return True
        return float(mcap) < _MAX_MARKETCAP_FOR_SOCIAL_SIGNAL
    except Exception:
        return True


def sentiment_multiplier(ticker: str, direction: str) -> float:
    """±4% envelope. Requires min message volume to be trusted AND market cap
    below $50B (retail sentiment has near-zero signal on mega-caps)."""
    r = get_sentiment(ticker)
    if r is None:
        return _MULT_NEUTRAL
    total = r.get("message_count_24h") or 0
    if total < _MIN_MESSAGES:
        return _MULT_NEUTRAL
    if not _is_retail_moveable(ticker):
        return _MULT_NEUTRAL
    bull = r.get("bullish_pct_24h")
    if bull is None:
        return _MULT_NEUTRAL
    direction = (direction or "").upper()
    if direction == "BUY":
        if bull >= _STRONG_LEAN_PCT: return _MULT_CONFIRM
        if bull <= 1 - _STRONG_LEAN_PCT: return _MULT_CONTRA
        return _MULT_NEUTRAL
    if direction == "SELL":
        if bull <= 1 - _STRONG_LEAN_PCT: return _MULT_CONFIRM
        if bull >= _STRONG_LEAN_PCT: return _MULT_CONTRA
        return _MULT_NEUTRAL
    return _MULT_NEUTRAL


def sentiment_reason_line(ticker: str, direction: str) -> Optional[str]:
    r = get_sentiment(ticker)
    if r is None or (r.get("message_count_24h") or 0) < _MIN_MESSAGES or r.get("bullish_pct_24h") is None:
        return None
    mult = sentiment_multiplier(ticker, direction)
    if mult == _MULT_NEUTRAL:
        return None  # skip the line if it's not moving the multiplier
    bull = r["bullish_pct_24h"]
    msgs = r["message_count_24h"]
    mark = "💬✅" if mult > _MULT_NEUTRAL else "💬⚠️"
    lean = "bullish" if bull >= 0.5 else "bearish"
    return f"{mark} Stocktwits (24h): {msgs} msgs, {bull*100:.0f}% {lean} — {'confirms' if mult > _MULT_NEUTRAL else 'contradicts'} {direction}"
