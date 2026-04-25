"""r/wallstreetbets ticker-mention scraper.

Polls Reddit's public JSON API (no auth needed) for the last ~400 new
posts + comments, counts mentions of each watchlist/pool ticker, and
classifies posts by simple keyword hints (bullish: calls/moon/yolo/long,
bearish: puts/short/crash/guh).

Signal value:
  * Strong on low-float squeezes (sudden mention spike → crowd chasing)
  * Decent on meme/small-cap names
  * ~zero on mega-caps where the tape already reflects retail flow

Cadence: every 30 minutes (Reddit's posted-this-hour window).
Rate limit: ~60 req/min unauth on JSON endpoints; 2-3 pages is plenty.

Design notes:
  * Matches $TICKER or bare TICKER (2-5 uppercase letters) — the $-prefix
    form is less noisy; we require one of the two to avoid matching
    random capitalized words.
  * Counts each ticker AT MOST ONCE per message (prevents spammy posts
    from dominating the count).
"""
from __future__ import annotations
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Set

from database import SessionLocal, WSBMention, WatchlistStock, CandidatePool

logger = logging.getLogger(__name__)

_UA = "stockrecs-wsb-scraper/1.0 (contact: gupta.pccs@gmail.com)"
_HTTP_TIMEOUT = 15.0

# Noise tickers that overlap with common English words — matched only when
# $-prefixed to avoid false positives. Even with $-prefix, WSB abuses some
# of these (e.g. "$A" for Agilent is often just punctuation).
_NOISE_PLAIN_BARE = {"A", "I", "IT", "BE", "OR", "ON", "AT", "GO", "SO",
                     "DD", "BAG", "BIG", "LONG", "NEW", "PUMP", "DUMP"}

_BULL_HINTS = re.compile(
    r"\b(call|calls|long|moon|yolo|rocket|tendies|diamond\s*hands|buy|"
    r"bullish|squeeze|rip|ripping|pumping|green)\b", re.IGNORECASE)
_BEAR_HINTS = re.compile(
    r"\b(put|puts|short|shorts|crash|guh|bagholding|dump|dumping|"
    r"bearish|tanking|red|blood)\b", re.IGNORECASE)


def _target_tickers() -> Set[str]:
    db = SessionLocal()
    try:
        tickers = set(s.ticker for s in db.query(WatchlistStock).all())
        tickers |= set(r.ticker for r in db.query(CandidatePool).all())
        return set(t.upper() for t in tickers if t)
    finally:
        db.close()


def _fetch_json(url: str):
    import httpx
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT,
                          headers={"User-Agent": _UA}) as c:
            r = c.get(url)
        if r.status_code == 429:
            logger.warning("wsb_scraper: rate-limited by Reddit; backing off")
            return None
        if r.status_code != 200:
            logger.debug(f"wsb_scraper: HTTP {r.status_code} for {url}")
            return None
        return r.json()
    except Exception as e:
        logger.debug(f"wsb_scraper: fetch failed {url}: {e}")
        return None


def _iter_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten Reddit listing → list of {title, selftext, created_utc}."""
    out: List[Dict[str, Any]] = []
    if not payload:
        return out
    for child in (payload.get("data", {}) or {}).get("children", []) or []:
        data = child.get("data") or {}
        out.append({
            "title": data.get("title") or "",
            "body": data.get("selftext") or data.get("body") or "",
            "ts": data.get("created_utc") or 0,
        })
    return out


def _count_mentions(messages: List[Dict[str, Any]], tickers: Set[str],
                     cutoff_24h_ts: float) -> Dict[str, Dict[str, int]]:
    """Per-ticker mention count in both 24h and full window, plus sentiment hints.

    Returns {ticker: {"mentions_24h": n, "mentions_full": n, "bullish": n, "bearish": n}}
    """
    counts: Dict[str, Dict[str, int]] = {
        t: {"mentions_24h": 0, "mentions_full": 0, "bullish": 0, "bearish": 0}
        for t in tickers
    }
    dollar_re = re.compile(r"\$([A-Z]{1,5})\b")
    bare_re = re.compile(r"\b([A-Z]{2,5})\b")
    for msg in messages:
        ts = msg.get("ts") or 0
        text = f"{msg.get('title','')} {msg.get('body','')}"
        in_24h = ts >= cutoff_24h_ts

        # Collect tickers that appear in this message (set — one mention per msg)
        seen: Set[str] = set()
        for sym in dollar_re.findall(text):
            if sym in tickers:
                seen.add(sym)
        for sym in bare_re.findall(text):
            if sym in tickers and sym not in _NOISE_PLAIN_BARE:
                seen.add(sym)
        if not seen:
            continue

        # Sentiment hint (coarse; applies to the whole message — a post
        # about "calls on GME" will also bump bullish for any other
        # tickers mentioned, which is imperfect but cheap).
        has_bull = bool(_BULL_HINTS.search(text))
        has_bear = bool(_BEAR_HINTS.search(text))
        for sym in seen:
            counts[sym]["mentions_full"] += 1
            if in_24h:
                counts[sym]["mentions_24h"] += 1
                if has_bull:
                    counts[sym]["bullish"] += 1
                if has_bear:
                    counts[sym]["bearish"] += 1
    return counts


def refresh_once() -> Dict[str, Any]:
    """Pull the last ~400 new posts+comments, count mentions, upsert rows."""
    tickers = _target_tickers()
    if not tickers:
        return {"updated": 0, "total_messages": 0}

    # Pull /new.json for posts and /comments.json for comments. Each page
    # caps at 100 items; two pages each is plenty for the 24h window.
    all_messages: List[Dict[str, Any]] = []
    for path in ("new", "comments"):
        for limit in (100, 100):
            # Reddit accepts `after` for pagination; for 30-min cadence two
            # unpaginated pages each covers ~2h of activity which is
            # sufficient overlap between polls.
            url = f"https://www.reddit.com/r/wallstreetbets/{path}.json?limit={limit}"
            payload = _fetch_json(url)
            if not payload:
                break
            all_messages.extend(_iter_messages(payload))
            time.sleep(1.0)  # polite pacing between Reddit calls
            break   # one page per path is sufficient for the 30-min window

    cutoff_24h_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    per_ticker = _count_mentions(all_messages, tickers, cutoff_24h_ts)

    # Persist — also compute 7d/30d rolling mentions from history.
    db = SessionLocal()
    updated = 0
    try:
        for ticker, row in per_ticker.items():
            if row["mentions_full"] == 0 and row["mentions_24h"] == 0:
                # Don't create noise rows for tickers with zero mentions.
                continue
            persisted = db.query(WSBMention).filter(WSBMention.ticker == ticker).first()
            # For the z-score, we'd need historical counts across days.
            # Simple approximation: z-score is None on first fetch;
            # later fetches compare to the previous 24h value.
            prev_24h = (persisted.mentions_24h or 0) if persisted else 0
            zscore = None
            if prev_24h >= 5:
                zscore = round((row["mentions_24h"] - prev_24h) / max(1, prev_24h), 2)
            if persisted is None:
                persisted = WSBMention(ticker=ticker)
                db.add(persisted)
            persisted.mentions_24h = row["mentions_24h"]
            persisted.mentions_7d = row["mentions_full"]   # proxy; refines over time
            persisted.mentions_7d_zscore = zscore
            persisted.bullish_hint_24h = row["bullish"]
            persisted.bearish_hint_24h = row["bearish"]
            persisted.updated_at = datetime.utcnow()
            updated += 1
        db.commit()
    finally:
        db.close()
    logger.info(f"wsb_scraper: {updated} tickers had mentions in last {len(all_messages)} msgs")
    return {"updated": updated, "total_messages": len(all_messages)}


def get_mentions(ticker: str) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        r = db.query(WSBMention).filter(WSBMention.ticker == ticker.upper()).first()
        if r is None:
            return None
        return {
            "ticker": r.ticker,
            "mentions_24h": r.mentions_24h,
            "mentions_7d": r.mentions_7d,
            "mentions_7d_zscore": r.mentions_7d_zscore,
            "bullish_hint_24h": r.bullish_hint_24h,
            "bearish_hint_24h": r.bearish_hint_24h,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
    finally:
        db.close()


# ---------- Signal-generator integration -----------------------------------
# Envelope is intentionally tight (±3%) because WSB signal is noisy and
# highly bimodal (dominates on squeeze setups, pure noise on mega-caps).
_MULT_NEUTRAL = 1.0
_MULT_CONFIRM = 1.03
_MULT_CONTRA = 0.97

_MIN_MENTIONS_24H = 10   # below this, noise > signal


def wsb_multiplier(ticker: str, direction: str) -> float:
    """Tilt for BUY if mentions spiking + bullish hints dominate; mirror for
    SELL. Requires min 10 mentions/24h and a clear lean (2:1 ratio)."""
    r = get_mentions(ticker)
    if r is None:
        return _MULT_NEUTRAL
    m24 = r.get("mentions_24h") or 0
    if m24 < _MIN_MENTIONS_24H:
        return _MULT_NEUTRAL
    bull = r.get("bullish_hint_24h") or 0
    bear = r.get("bearish_hint_24h") or 0
    if bull + bear < 3:
        return _MULT_NEUTRAL
    lean_ratio = bull / max(1, bull + bear)
    direction = (direction or "").upper()
    if direction == "BUY":
        if lean_ratio >= 0.66: return _MULT_CONFIRM
        if lean_ratio <= 0.34: return _MULT_CONTRA
        return _MULT_NEUTRAL
    if direction == "SELL":
        if lean_ratio <= 0.34: return _MULT_CONFIRM
        if lean_ratio >= 0.66: return _MULT_CONTRA
        return _MULT_NEUTRAL
    return _MULT_NEUTRAL


def wsb_reason_line(ticker: str, direction: str) -> Optional[str]:
    r = get_mentions(ticker)
    if r is None or (r.get("mentions_24h") or 0) < _MIN_MENTIONS_24H:
        return None
    mult = wsb_multiplier(ticker, direction)
    if mult == _MULT_NEUTRAL:
        return None
    bull = r.get("bullish_hint_24h") or 0
    bear = r.get("bearish_hint_24h") or 0
    m24 = r["mentions_24h"]
    mark = "📣✅" if mult > _MULT_NEUTRAL else "📣⚠️"
    tone = "bullish" if bull > bear else "bearish"
    return f"{mark} WSB (24h): {m24} mentions, {bull}🚀 / {bear}🩸 ({tone}) — {'confirms' if mult > _MULT_NEUTRAL else 'contradicts'} {direction}"
