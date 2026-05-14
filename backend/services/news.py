"""
News ingestion + sentiment scoring.

Phase 1: read-only observability.
  • Poll Alpaca's news API every 2 minutes for all watchlist tickers.
  • VADER sentiment scoring (compound ∈ [-1, +1]).
  • De-dup on the article's Alpaca `id` so re-polls don't create duplicates.
  • Persist to news_events table.

The auto-trader does NOT consume this table in phase 1. We collect a week
of data first, analyze trade-vs-news correlations via the /api/news/trade
endpoint, then decide in phase 2 which hooks are worth wiring in.

Alpaca news API docs:
  GET https://data.alpaca.markets/v1beta1/news
  params: symbols=AAPL,TSLA  start=ISO  limit=50  sort=desc
  headers: APCA-API-KEY-ID, APCA-API-SECRET-KEY
  free tier: ~200 req/min — comfortable for a 2-minute poll cadence.
"""
from __future__ import annotations
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

import httpx

from database import SessionLocal, WatchlistStock, NewsEvent

logger = logging.getLogger(__name__)

# r44 fix #0.6: bounded background pool for AI news-exit dispatch so
# Claude latency doesn't block the news poller (which holds the SQLite
# WAL writer lock — see DESIGN.md scan-vs-manage contention design).
_NEWS_AI_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="news-ai")

# r47 fix #T0d-2: r46 added a "per-ticker AI rate limit (3/hr)" to keep
# earnings-day spam from firing 50 AI judges. The implementation made
# `_ai_rate` LOCAL to ingest(), so every poll call reset it — the cap
# was effectively per-batch (typically 1 batch/poll), not per-hour.
# Now: module-level keyed by (ticker, hour_bucket_int) and pruned on each
# ingest pass to keep memory bounded.
import threading as _ai_th
_AI_RATE_LOCK = _ai_th.Lock()
_AI_RATE: Dict[tuple, int] = {}
_AI_RATE_PER_TICKER_HOUR = 3


def _ai_rate_inc(ticker: str) -> int:
    """Atomically increment the per-(ticker, hour) AI exit-judge counter.
    Returns the post-increment count. Caller checks < cap before incrementing."""
    from datetime import datetime as _dt_air
    bucket = (ticker.upper(), int(_dt_air.utcnow().timestamp() // 3600))
    with _AI_RATE_LOCK:
        n = _AI_RATE.get(bucket, 0) + 1
        _AI_RATE[bucket] = n
        # Prune entries older than 2h (keep current + previous hour for grace).
        cur_h = bucket[1]
        for k in list(_AI_RATE.keys()):
            if k[1] < cur_h - 1:
                _AI_RATE.pop(k, None)
        return n


def _ai_rate_get(ticker: str) -> int:
    from datetime import datetime as _dt_air
    bucket = (ticker.upper(), int(_dt_air.utcnow().timestamp() // 3600))
    with _AI_RATE_LOCK:
        return _AI_RATE.get(bucket, 0)

_ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
_POSITIVE_THRESHOLD = 0.35   # VADER compound ≥ this → "positive"
_NEGATIVE_THRESHOLD = -0.35  # VADER compound ≤ this → "negative"

# VADER lexicon adds a small amount of financial context for headlines —
# default VADER was trained on social media so it misreads "beat estimates"
# (finance-positive but neutral on VADER) and "lawsuit" (finance-negative
# but VADER flags a lot of lawsuits as neutral). We augment the lexicon
# at module load time to nudge key finance terms. Kept small — over-tuning
# would over-fit our calibration. Full upgrade path is FinBERT (phase 2).
_FINANCIAL_LEXICON_BOOSTS: Dict[str, float] = {
    # bullish tilts
    "beat": 2.0, "beats": 2.0, "beating": 1.8, "raised": 1.5, "raises": 1.5,
    "upgrade": 2.5, "upgrades": 2.5, "upgraded": 2.5, "outperform": 2.2,
    "record": 1.8, "surge": 2.3, "surges": 2.3, "soars": 2.5, "rally": 1.8,
    "breakout": 1.5, "dividend": 1.2, "buyback": 1.8, "repurchase": 1.6,
    "beats-and-raises": 3.0, "bullish": 2.5,
    # bearish tilts
    "miss": -2.0, "misses": -2.0, "missed": -2.0,
    "downgrade": -2.5, "downgrades": -2.5, "downgraded": -2.5,
    "underperform": -2.2,
    "lawsuit": -2.0, "fraud": -3.0, "investigation": -2.0, "probe": -1.8,
    "recall": -1.8, "delay": -1.2, "delayed": -1.2,
    "bankruptcy": -3.5, "chapter11": -3.5,
    "warning": -1.8, "warn": -1.8,
    "plunge": -2.5, "plunges": -2.5, "tumbles": -2.3, "slump": -2.0,
    "guidance-cut": -2.8, "cut-guidance": -2.8,
    "bearish": -2.5,
}

# Sentiment scoring routes through the pluggable backend. Default VADER;
# opt-in FinBERT via SENTIMENT_BACKEND=finbert (requires transformers + torch).
from services.sentiment import score_text as _backend_score_text


def score_text(text: str) -> Dict[str, Any]:
    """Back-compat shim — delegates to services.sentiment."""
    out = _backend_score_text(text)
    # Older callers may not expect the 'backend' key; trim to the original
    # shape so news-event persistence doesn't break.
    return {"score": out["score"], "label": out["label"], "severity": out["severity"]}


def _alpaca_headers() -> Optional[Dict[str, str]]:
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return None
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def fetch_alpaca_news(
    symbols: List[str],
    since: Optional[datetime] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Fetch raw news items from Alpaca for the given symbols. Returns [].
    on failure (never raises — the poller should keep the scheduler alive)."""
    headers = _alpaca_headers()
    if not headers:
        logger.debug("news: APCA credentials missing — skipping fetch")
        return []
    if not symbols:
        return []
    params = {
        "symbols": ",".join(s.upper() for s in symbols),
        "limit": str(max(1, min(50, limit))),
        "sort": "desc",
        "include_content": "false",
    }
    if since is not None:
        # Alpaca expects RFC 3339; UTC with 'Z'.
        params["start"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(_ALPACA_NEWS_URL, headers=headers, params=params)
        if r.status_code != 200:
            logger.warning(f"news: Alpaca {r.status_code} {r.text[:200]}")
            return []
        data = r.json() or {}
        items = data.get("news") or []
        return items
    except Exception as e:
        logger.warning(f"news: fetch failed: {e}")
        return []


def _parse_published_at(raw: str) -> Optional[datetime]:
    """Alpaca returns ISO 8601 with 'Z' or +00:00. Return a naive UTC datetime
    (the app's convention throughout auto_trader / alpaca_client)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        try:
            return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def ingest(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Persist a batch of Alpaca news items to news_events, de-duped by external_id.
    Returns {inserted, skipped_dup, errors}."""
    out = {"inserted": 0, "skipped_dup": 0, "errors": 0}
    if not items:
        return out
    # Pre-compute the open-position set so we can flag freshly-inserted
    # news for AI exit-decision review without an extra DB query per row.
    _open_pos_tickers: set = set()
    try:
        from database import AutoTrade as _AT
        _db_pos = SessionLocal()
        try:
            _open_pos_tickers = {
                t for (t,) in _db_pos.query(_AT.ticker).filter(
                    _AT.status.in_(["pending", "open"])
                ).all()
            }
        finally:
            _db_pos.close()
    except Exception:
        pass
    # Buffer of (ticker, NewsEvent-as-dict) for post-commit AI dispatch.
    _new_for_open: List[Dict[str, Any]] = []

    db = SessionLocal()
    # r46 Tier 1: cross-source headline dedup. Reuters + Benzinga + Bloomberg
    # publishing the same M&A story = 3 separate ingest events otherwise.
    # We dedupe by (normalized_headline_first_60_chars + primary_ticker)
    # within a 30-minute window. Same primary ticker + same opening 60
    # chars of headline within 30 min = treated as duplicate.
    seen_headline_keys: set = set()
    try:
        from datetime import datetime as _dt_dh, timedelta as _td_dh
        recent_cut = _dt_dh.utcnow() - _td_dh(minutes=30)
        recent_rows = db.query(NewsEvent.headline, NewsEvent.ticker).filter(
            NewsEvent.published_at >= recent_cut
        ).all()
        for hl, tk in recent_rows:
            key = (str(tk or "").upper(), (hl or "").lower()[:60].strip())
            seen_headline_keys.add(key)
    except Exception:
        pass
    # r47 fix #T0d-2: see module-level _ai_rate_inc / _ai_rate_get.
    # The local-dict pattern below is gone; we delegate to the shared store.

    try:
        # Batch the existence check — one query vs N per-article selects.
        ext_ids = [str(it.get("id")) for it in items if it.get("id") is not None]
        if ext_ids:
            existing = {
                row[0] for row in db.query(NewsEvent.external_id).filter(
                    NewsEvent.external_id.in_(ext_ids)
                ).all()
            }
        else:
            existing = set()
        for it in items:
            try:
                ext_id = str(it.get("id") or "")
                if not ext_id:
                    continue
                if ext_id in existing:
                    out["skipped_dup"] += 1
                    continue
                headline = (it.get("headline") or "").strip()
                if not headline:
                    continue
                summary = (it.get("summary") or "").strip() or None
                source = (it.get("source") or "").strip() or None
                author = (it.get("author") or "").strip() or None
                url = (it.get("url") or "").strip() or None
                published_at = _parse_published_at(it.get("created_at") or it.get("updated_at") or "")
                if not published_at:
                    # No timestamp → can't correlate with trades. Skip.
                    continue
                symbols = [s for s in (it.get("symbols") or []) if s]
                if not symbols:
                    continue
                primary = symbols[0].upper()
                # r46 Tier 1: cross-source headline dedup.
                _hkey = (primary, headline.lower()[:60].strip())
                if _hkey in seen_headline_keys:
                    out["skipped_dup"] += 1
                    continue
                seen_headline_keys.add(_hkey)
                # Score headline + first line of summary for a richer signal
                text_for_score = headline
                if summary:
                    text_for_score = headline + ". " + summary.split(". ")[0]
                sent = score_text(text_for_score)
                row = NewsEvent(
                    external_id=ext_id,
                    ticker=primary,
                    symbols=",".join(s.upper() for s in symbols),
                    source=source, author=author,
                    headline=headline, summary=summary, url=url,
                    published_at=published_at,
                    sentiment_score=sent["score"],
                    sentiment_label=sent["label"],
                    severity=sent["severity"],
                )
                db.add(row)
                out["inserted"] += 1
                # Flag for AI exit-decision review if any open position
                # matches one of this article's symbols.
                if _open_pos_tickers:
                    for s in symbols:
                        s_up = s.upper()
                        if s_up in _open_pos_tickers:
                            # r47 fix #T0d-2: shared per-(ticker, hour) cap.
                            if _ai_rate_get(s_up) >= _AI_RATE_PER_TICKER_HOUR:
                                break
                            _ai_rate_inc(s_up)
                            _new_for_open.append({
                                "ticker": s_up,
                                "title": headline,
                                "summary": summary,
                                "source": source,
                                "url": url,
                                "published_at": published_at.isoformat() if published_at else None,
                                "sentiment_label": sent["label"],
                                "sentiment_score": sent["score"],
                                "severity": sent["severity"],
                            })
                            break
            except Exception as e:
                out["errors"] += 1
                logger.debug(f"news: ingest row error: {e}")
        db.commit()
    except Exception as e:
        # r44 fix Wave 6: on commit failure, clear `_new_for_open` so the
        # AI news-exit dispatcher doesn't run against rows that didn't persist.
        logger.warning(f"news: ingest batch failed: {e}")
        out["errors"] += 1
        _new_for_open.clear()
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()

    # Post-commit AI exit-decision dispatch. Only fires when AI_NEWS_EXIT_MODE
    # is shadow/active AND the news is at least medium-severity (skip
    # routine PR / sector blurbs). Each call is best-effort — failures
    # never propagate back into ingest's return value.
    # r44 fix #0.6: dispatch AI news-exit on a worker thread, NOT inline on
    # the scheduler thread. Prior code held the news poller blocked behind
    # 5-30s Claude calls × N items. With max_instances=1 + coalesce=True on
    # the news job, this dropped news polling cadence whenever Claude was
    # slow. Now the poller returns immediately; the dispatch runs in the
    # background pool and opens its own DB session.
    if _new_for_open:
        try:
            _NEWS_AI_POOL.submit(_dispatch_ai_news_exit, list(_new_for_open))
        except Exception as e:
            logger.debug(f"news: AI exit dispatch submit failed: {e}")
    return out


def _dispatch_ai_news_exit(news_for_open: List[Dict[str, Any]]) -> None:
    """For each news item on an open ticker, fire the AI judge and act on
    its verdict (close/trim) when honored. Runs synchronously after each
    ingest batch — small N (≤ 5 typical), Claude calls bounded by
    AI_JUDGE_TIMEOUT_SEC."""
    from services import ai_judge
    if ai_judge.news_exit_mode() == "off":
        return
    from database import AutoTrade
    # r47 fix #T0e-1: prior code held one DB session across all (item, trade)
    # iterations + multi-second Claude round-trips → lost-update races
    # against the manage tick mutating the same AutoTrade rows. Acquire
    # the manage lock around each close so we serialize state mutations
    # cleanly with the manage tick. Also use a fresh session per trade.
    try:
        from services.auto_trader import _manage_lock as _ai_news_lock
    except Exception:
        _ai_news_lock = None
    db = SessionLocal()
    try:
        for item in news_for_open:
            # r46 fix #0.1: severity is INT (sentiment.py:70 → int(round(abs(compound)*100))),
            # not string. Prior code did `(severity or "").lower()` which raised
            # AttributeError on the first iter — caught by an outer try/except,
            # silently dropping ALL news exits since this code shipped (r41).
            # Now: gate on numeric severity ≥ 35 (matches the |compound| ≥ 0.35
            # threshold used by sentiment.py for "neutral" vs "positive/negative").
            try:
                sev = int(item.get("severity") or 0)
            except Exception:
                sev = 0
            if sev < 35:
                continue
            ticker = item["ticker"]
            open_trades = db.query(AutoTrade).filter(
                AutoTrade.ticker == ticker,
                AutoTrade.status.in_(["pending", "open"]),
            ).all()
            for t in open_trades:
                trade_view = {
                    "id": t.id,
                    "ticker": t.ticker,
                    "asset_type": t.asset_type,
                    "qty": t.qty,
                    "entry_price": t.entry_price,
                    "current_stop": t.current_stop,
                    "target1": t.target1,
                    "target2": t.target2,
                    "target3": t.target3,
                    "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                }
                try:
                    res = ai_judge.news_exit_decision(trade_view, item)
                except Exception as e:
                    logger.debug(f"news: AI judge call failed for {ticker} #{t.id}: {e}")
                    continue
                if not res.get("honored"):
                    continue
                action = res.get("action", "hold")
                if action == "close":
                    # r47 fix #T0e-3: serialize against the manage tick so the
                    # AI thread doesn't lost-update mid-T1-trim or mid-stop-replace.
                    _got_lock = False
                    try:
                        if _ai_news_lock is not None:
                            _got_lock = _ai_news_lock.acquire(timeout=10.0)
                            if not _got_lock:
                                logger.warning(
                                    f"news: AI-close skipped for {ticker} #{t.id}: "
                                    f"could not acquire manage lock in 10s"
                                )
                                continue
                        # Use a fresh session for the close so a single bad
                        # row can't taint the rest of the dispatch loop.
                        ldb = SessionLocal()
                        try:
                            t_local = ldb.query(AutoTrade).filter(AutoTrade.id == t.id).first()
                            if not t_local or t_local.status not in ("pending", "open"):
                                continue
                            from services.execution_engine import force_close_trade
                            from services.auto_trader import _touch_clear as _tc
                            force_close_trade(
                                t_local, ldb,
                                reason=f"AI news_exit: {res.get('reason', '')}",
                                summary={},
                                status_override="closed_news_ai",
                                on_close=lambda closed_t: _tc(closed_t, ldb),
                            )
                        finally:
                            ldb.close()
                    except Exception as e:
                        logger.warning(f"news: AI-driven close failed for {ticker} #{t.id}: {e}")
                    finally:
                        if _got_lock and _ai_news_lock is not None:
                            try:
                                _ai_news_lock.release()
                            except Exception:
                                pass
                elif action == "trim":
                    # r77 fix: mirror the close branch — acquire the manage
                    # lock and use a fresh SessionLocal so a concurrent
                    # stop-trail or T1 trim in manage_open_positions can't
                    # lost-update t.qty against the outer dispatch session.
                    # Without this, two threads could each commit a halved
                    # qty and the broker-vs-DB share count would diverge.
                    _got_lock = False
                    try:
                        if _ai_news_lock is not None:
                            _got_lock = _ai_news_lock.acquire(timeout=10.0)
                            if not _got_lock:
                                logger.warning(
                                    f"news: AI-trim skipped for {ticker} #{t.id}: "
                                    f"could not acquire manage lock in 10s"
                                )
                                continue
                        ldb = SessionLocal()
                        try:
                            t_local = ldb.query(AutoTrade).filter(
                                AutoTrade.id == t.id
                            ).first()
                            if not t_local or t_local.status not in ("pending", "open"):
                                continue
                            from services import alpaca_client
                            if t_local.asset_type == "stock":
                                half = max(1, int((t_local.qty or 0) // 2))
                                from alpaca.trading.requests import MarketOrderRequest
                                from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                                c = alpaca_client._get_client()
                                if c is None:
                                    continue
                                res2 = c.submit_order(order_data=MarketOrderRequest(
                                    symbol=t_local.ticker, qty=half,
                                    side=_OS.SELL, time_in_force=_TIF.DAY,
                                ))
                                if res2 is not None:
                                    t_local.qty = (t_local.qty or 0) - half
                                    t_local.note = (t_local.note or "") + (
                                        f" | AI news_trim: -{half} shares "
                                        f"({res.get('reason', '')[:120]})"
                                    )
                                    ldb.commit()
                                    logger.info(
                                        f"AI news_exit TRIM {ticker} #{t_local.id}: "
                                        f"-{half} shares"
                                    )
                                    # r39 audit critical-4: must resize the broker
                                    # SL leg to remaining qty, else when the stop
                                    # fires Alpaca rejects (only have half the
                                    # shares) or the position flips short
                                    # depending on broker semantics. Same fix as
                                    # F3/T2 trims; news-driven trim was missing it.
                                    if t_local.stop_order_id and t_local.qty > 0:
                                        try:
                                            from alpaca.trading.requests import ReplaceOrderRequest
                                            c.replace_order_by_id(
                                                t_local.stop_order_id,
                                                order_data=ReplaceOrderRequest(
                                                    qty=int(t_local.qty)
                                                ),
                                            )
                                        except Exception as _re:
                                            logger.warning(
                                                f"news: AI-trim SL-resize failed "
                                                f"for {ticker} #{t_local.id}: {_re}"
                                            )
                            elif t_local.asset_type == "option":
                                half = max(1, int((t_local.qty or 0) // 2))
                                res2 = alpaca_client.submit_simple_option_order(
                                    occ_symbol=t_local.symbol, qty=half, side="sell",
                                    order_type="market", time_in_force="day",
                                )
                                if isinstance(res2, dict) and "error" not in res2:
                                    t_local.qty = (t_local.qty or 0) - half
                                    t_local.note = (t_local.note or "") + (
                                        f" | AI news_trim: -{half} contracts "
                                        f"({res.get('reason', '')[:120]})"
                                    )
                                    ldb.commit()
                                    logger.info(
                                        f"AI news_exit TRIM {ticker} "
                                        f"#{t_local.id}: -{half} contracts"
                                    )
                        finally:
                            ldb.close()
                    except Exception as e:
                        logger.warning(
                            f"news: AI-driven trim failed for {ticker} #{t.id}: {e}"
                        )
                    finally:
                        if _got_lock and _ai_news_lock is not None:
                            try:
                                _ai_news_lock.release()
                            except Exception:
                                pass
    finally:
        db.close()


def poll_watchlist(lookback_minutes: int = 30) -> Dict[str, Any]:
    """Scheduled job entrypoint. Polls Alpaca for all watchlist tickers and
    persists fresh articles. `lookback_minutes` lets us forgive brief
    scheduler outages without re-pulling a day of history."""
    db = SessionLocal()
    try:
        tickers = [s.ticker for s in db.query(WatchlistStock).all()]
    finally:
        db.close()
    if not tickers:
        return {"tickers": 0, "inserted": 0}
    since = datetime.utcnow() - timedelta(minutes=lookback_minutes)
    items = fetch_alpaca_news(tickers, since=since, limit=50)
    if not items:
        return {"tickers": len(tickers), "fetched": 0, "inserted": 0}
    result = ingest(items)
    result["tickers"] = len(tickers)
    result["fetched"] = len(items)
    logger.info(
        f"news: polled {len(tickers)} tickers, fetched {len(items)}, "
        f"inserted {result['inserted']}, dup {result['skipped_dup']}"
    )
    return result


# ---------- Query helpers for the router -----------------------------------

def list_for_ticker(ticker: str, limit: int = 25, since_hours: int = 72) -> List[Dict[str, Any]]:
    """Return recent news rows for a single ticker, newest first."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        rows = (
            db.query(NewsEvent)
            .filter(NewsEvent.ticker == ticker.upper(),
                    NewsEvent.published_at >= cutoff)
            .order_by(NewsEvent.published_at.desc())
            .limit(limit)
            .all()
        )
        return [_serialize(r) for r in rows]
    finally:
        db.close()


def list_recent(limit: int = 50, since_hours: int = 24) -> List[Dict[str, Any]]:
    """Return recent news across the whole watchlist (for dashboard)."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=since_hours)
        rows = (
            db.query(NewsEvent)
            .filter(NewsEvent.published_at >= cutoff)
            .order_by(NewsEvent.published_at.desc())
            .limit(limit)
            .all()
        )
        return [_serialize(r) for r in rows]
    finally:
        db.close()


def trade_context(trade_id: int, window_hours_before: int = 24, window_hours_after: int = 24) -> Dict[str, Any]:
    """Return news that landed during a trade's lifetime.

    For a trade opened at T0 and closed at T1 (or current time if still open),
    we return news published in [T0 - before, T1 + after] for the trade's
    ticker. The window defaults to ±24h so a trade that held 2h still
    captures the pre/post-mortem context.

    Also returns aggregate sentiment stats and a verdict:
       • aligned:    PL > 0 AND prevailing sentiment positive (or PL < 0 AND negative)
       • contrary:   trade worked despite opposing sentiment (or vice-versa)
       • no-signal:  too little news to judge
    """
    from database import AutoTrade
    db = SessionLocal()
    try:
        trade = db.query(AutoTrade).filter(AutoTrade.id == int(trade_id)).first()
        if not trade:
            return {"error": f"trade {trade_id} not found"}
        t0 = (trade.filled_at or trade.opened_at)
        t1 = trade.closed_at or datetime.utcnow()
        if not t0:
            return {"error": "trade has no opened_at"}
        start = t0 - timedelta(hours=window_hours_before)
        end = t1 + timedelta(hours=window_hours_after)
        rows = (
            db.query(NewsEvent)
            .filter(NewsEvent.ticker == trade.ticker,
                    NewsEvent.published_at >= start,
                    NewsEvent.published_at <= end)
            .order_by(NewsEvent.published_at.asc())
            .all()
        )
        articles = [_serialize(r) for r in rows]
        # Split by pre-trade / during-trade / post-trade buckets.
        pre, during, post = [], [], []
        for a in articles:
            when = datetime.fromisoformat(a["published_at"])
            if when < t0:
                pre.append(a)
            elif when <= t1:
                during.append(a)
            else:
                post.append(a)
        # Aggregate sentiment during trade window.
        during_scores = [a["sentiment_score"] for a in during if a.get("sentiment_score") is not None]
        avg_during = round(sum(during_scores) / len(during_scores), 3) if during_scores else 0.0
        verdict = _verdict(trade, avg_during, len(during))
        return {
            "trade_id": trade.id,
            "ticker": trade.ticker,
            "opened_at": t0.isoformat(),
            "closed_at": (trade.closed_at.isoformat() if trade.closed_at else None),
            "status": trade.status,
            "realized_pl": trade.realized_pl,
            "window": {
                "before_hours": window_hours_before,
                "after_hours": window_hours_after,
            },
            "news_counts": {"pre": len(pre), "during": len(during), "post": len(post)},
            "avg_sentiment_during": avg_during,
            "verdict": verdict,
            "articles": {
                "pre_trade": pre,
                "during_trade": during,
                "post_trade": post,
            },
        }
    finally:
        db.close()


def summary_analysis(days: int = 7) -> Dict[str, Any]:
    """Aggregate trade outcomes vs news-sentiment across the last N days.

    For every closed auto-trade in the window, classify its during-trade
    average sentiment as positive/negative/neutral and compare to the PL
    sign. Produces a 2×2 table (for stocks) that answers "is news sentiment
    predictive of our outcomes?" — directly informs whether phase 2 auto-
    trade hooks are worth building.
    """
    from database import AutoTrade
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        trades = db.query(AutoTrade).filter(
            AutoTrade.closed_at != None,  # noqa: E711
            AutoTrade.closed_at >= cutoff,
        ).all()
        matrix: Dict[str, Dict[str, int]] = {
            "positive_sent": {"win": 0, "loss": 0, "flat": 0},
            "negative_sent": {"win": 0, "loss": 0, "flat": 0},
            "neutral_sent": {"win": 0, "loss": 0, "flat": 0},
            "no_news":      {"win": 0, "loss": 0, "flat": 0},
        }
        trade_details: List[Dict[str, Any]] = []
        for t in trades:
            t0 = t.filled_at or t.opened_at
            t1 = t.closed_at
            if not t0 or not t1:
                continue
            # r90+: previously filtered on `NewsEvent.ticker == t.ticker`,
            # which missed every multi-symbol article (primary ticker stored
            # was a co-mentioned name, not the trade's ticker). Per-trade
            # context endpoint already uses the symbols-LIKE join; align the
            # bucketing aggregator with it so "no_news" stops swallowing 95%
            # of trades.
            from sqlalchemy import or_ as _or
            news_rows = (
                db.query(NewsEvent)
                .filter(_or(
                    NewsEvent.ticker == t.ticker,
                    NewsEvent.symbols.like(f"%{t.ticker}%"),
                ))
                .filter(NewsEvent.published_at >= t0,
                        NewsEvent.published_at <= t1)
                .all()
            )
            scores = [r.sentiment_score for r in news_rows if r.sentiment_score is not None]
            if not scores:
                bucket = "no_news"; avg = None
            else:
                avg = sum(scores) / len(scores)
                if avg >= _POSITIVE_THRESHOLD:
                    bucket = "positive_sent"
                elif avg <= _NEGATIVE_THRESHOLD:
                    bucket = "negative_sent"
                else:
                    bucket = "neutral_sent"
            pl = t.realized_pl or 0.0
            outcome = "win" if pl > 0 else ("loss" if pl < 0 else "flat")
            matrix[bucket][outcome] += 1
            trade_details.append({
                "trade_id": t.id,
                "ticker": t.ticker,
                "asset_type": t.asset_type,
                "status": t.status,
                "pl": pl,
                "news_count": len(news_rows),
                "avg_sentiment": round(avg, 3) if avg is not None else None,
                "bucket": bucket,
            })
        # Derive headline verdict: what's the hit rate when sentiment aligns with direction?
        hits = matrix["positive_sent"]["win"] + matrix["negative_sent"]["loss"]
        opportunities = (matrix["positive_sent"]["win"] + matrix["positive_sent"]["loss"]
                        + matrix["negative_sent"]["win"] + matrix["negative_sent"]["loss"])
        alignment_rate = round(100.0 * hits / opportunities, 1) if opportunities else None
        return {
            "days": days,
            "total_trades": len(trade_details),
            "matrix": matrix,
            "alignment_rate_pct": alignment_rate,
            "trades": trade_details,
        }
    finally:
        db.close()


# ---------- Internal ------------------------------------------------------

def _serialize(r: NewsEvent) -> Dict[str, Any]:
    return {
        "id": r.id,
        "external_id": r.external_id,
        "ticker": r.ticker,
        "symbols": (r.symbols or "").split(",") if r.symbols else [],
        "source": r.source,
        "author": r.author,
        "headline": r.headline,
        "summary": r.summary,
        "url": r.url,
        "published_at": r.published_at.isoformat() if r.published_at else None,
        "sentiment_score": r.sentiment_score,
        "sentiment_label": r.sentiment_label,
        "severity": r.severity,
    }


def _verdict(trade, avg_sent: float, n_articles: int) -> str:
    if n_articles == 0:
        return "no-news"
    pl = trade.realized_pl or 0.0
    if abs(avg_sent) < _POSITIVE_THRESHOLD:
        return "neutral-news"
    positive_news = avg_sent >= _POSITIVE_THRESHOLD
    profitable = pl > 0
    # For stocks: long-only, so positive news + profit = aligned.
    # For puts: short-biased, so negative news + profit = aligned.
    if trade.asset_type == "stock":
        aligned = (positive_news and profitable) or (not positive_news and not profitable and pl < 0)
    else:  # option (put)
        aligned = (not positive_news and profitable) or (positive_news and not profitable and pl < 0)
    return "aligned" if aligned else "contrary"
