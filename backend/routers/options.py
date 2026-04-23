from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Dict, List, Optional
import logging

from database import get_db, WatchlistStock, Signal
from services.options_analyzer import suggest_options_for_signal
from services.bear_thesis import build_bear_thesis
from services.bull_thesis import build_bull_thesis
from routers._auth import require_api_key

# Timeframes Put-Play Watch will try, in preference order for "best thesis".
# Short TFs are included so intraday SELL signals on otherwise-bullish names
# (e.g. MU: 1d BUY but 5m/15m/30m/1h SELL) still surface weekly put plays.
_PUTS_WATCH_TFS = ["1h", "4h", "30m", "15m", "5m", "1d"]

router = APIRouter(prefix="/api/options", tags=["options"], dependencies=[Depends(require_api_key)])
logger = logging.getLogger(__name__)


def _latest_actionable_signal(ticker: str, db: Session) -> Optional[Signal]:
    """
    Pick a signal suitable for options: prefer longer timeframes (1mo > 1d > 4h > 1h)
    because tight intraday targets can't generate 3:1 option R:R.
    """
    preferred_order = ["1mo", "1d", "4h", "1h", "30m", "15m", "5m"]
    candidates = db.query(Signal).filter(
        Signal.ticker == ticker,
        Signal.signal_type != "NEUTRAL",
    ).order_by(desc(Signal.generated_at)).all()

    by_tf = {}
    for s in candidates:
        if s.timeframe not in by_tf:  # keep latest per timeframe
            by_tf[s.timeframe] = s

    for tf in preferred_order:
        if tf in by_tf and by_tf[tf].confidence >= 60:
            return by_tf[tf]
    # Fallback: highest confidence overall
    return max(by_tf.values(), key=lambda s: s.confidence, default=None)


def _has_recent_buy(ticker: str, db: Session, min_confidence: float = 70) -> bool:
    """True if any recent timeframe has a strong BUY — we then skip the put scan."""
    rows = db.query(Signal).filter(
        Signal.ticker == ticker,
        Signal.signal_type == "BUY",
    ).order_by(desc(Signal.generated_at)).limit(7).all()
    return any(s.confidence >= min_confidence for s in rows)


@router.get("/puts-watch")
def puts_watch(min_bear_confidence: float = Query(45), db: Session = Depends(get_db)):
    """
    Scan the watchlist for bearish put-play setups. For each ticker we try
    multiple timeframes (preferring those with a recent SELL signal) and keep
    whichever produces the highest-scoring qualifying put chain. A strong BUY
    on the daily chart no longer vetoes the ticker — it's entirely possible to
    have a bullish daily bias while the 15m/1h is rolling over, and scalpers
    want those short-dated puts to surface.
    """
    stocks = db.query(WatchlistStock).all()
    out = []
    skipped: List[Dict] = []
    for stock in stocks:
        # Order timeframes: ones with a fresh SELL first (most actionable for a
        # put play right now), then the rest as fallback. Dedupes across the
        # sell-ordered list + the static candidate list.
        recent_sells = (db.query(Signal)
                        .filter(Signal.ticker == stock.ticker,
                                Signal.signal_type == "SELL",
                                Signal.confidence >= 50)
                        .order_by(desc(Signal.generated_at)).all())
        tfs_ordered: List[str] = []
        seen = set()
        for s in recent_sells:
            if s.timeframe in _PUTS_WATCH_TFS and s.timeframe not in seen:
                tfs_ordered.append(s.timeframe); seen.add(s.timeframe)
        for tf in _PUTS_WATCH_TFS:
            if tf not in seen:
                tfs_ordered.append(tf); seen.add(tf)

        # Track the best-scoring (thesis, contracts) across all tried TFs.
        best = None
        fallback_reason: Optional[str] = None
        for tf in tfs_ordered:
            try:
                thesis = build_bear_thesis(stock.ticker, tf)
            except Exception as e:
                logger.warning(f"bear_thesis {stock.ticker} {tf} failed: {e}")
                continue
            if not thesis:
                fallback_reason = fallback_reason or "not bearish enough on any timeframe"
                continue
            if thesis["confidence"] < min_bear_confidence:
                fallback_reason = (fallback_reason or
                                   f"bear conviction {thesis['confidence']} < threshold")
                continue
            try:
                sugg = suggest_options_for_signal(stock.ticker, thesis)
            except Exception as e:
                logger.warning(f"puts suggest failed for {stock.ticker} {tf}: {e}")
                continue
            if not sugg["contracts"]:
                fallback_reason = fallback_reason or "no PUT met R:R + liquidity filters"
                continue
            top_score = sugg["contracts"][0]["score"]
            if best is None or top_score > best["score"]:
                best = {
                    "score": top_score,
                    "thesis": thesis,
                    "contracts": sugg["contracts"][:5],
                }
        if best is None:
            skipped.append({"ticker": stock.ticker,
                            "reason": fallback_reason or "no viable put play"})
            continue
        out.append({
            "ticker": stock.ticker,
            "name": stock.name,
            "thesis": best["thesis"],
            "top_contracts": best["contracts"],
        })
    out.sort(key=lambda r: r["top_contracts"][0]["score"] if r["top_contracts"] else 0, reverse=True)
    return {"suggestions": out, "skipped": skipped}


# Mirror of puts-watch for bullish CALL plays. Closes the long-side gap
# where sub-threshold BUYs + capacity-capped tickers were previously
# ignored. Runs off the same build_bull_thesis / suggest_options_for_signal
# pipeline, and skips tickers where a stock auto-trade still has headroom
# (per the same concentration guard consider_call_play uses).
_CALLS_WATCH_TFS = ["1d", "4h", "1h", "30m", "15m", "5m"]


@router.get("/calls-watch")
def calls_watch(min_bull_confidence: float = Query(45), db: Session = Depends(get_db)):
    """
    Scan the watchlist for bullish call-play setups. For each ticker we try
    multiple timeframes (preferring those with a recent BUY signal) and keep
    whichever produces the highest-scoring qualifying call chain.
    """
    stocks = db.query(WatchlistStock).all()
    out = []
    skipped: List[Dict] = []
    for stock in stocks:
        recent_buys = (db.query(Signal)
                       .filter(Signal.ticker == stock.ticker,
                               Signal.signal_type == "BUY",
                               Signal.confidence >= 50)
                       .order_by(desc(Signal.generated_at)).all())
        tfs_ordered: List[str] = []
        seen = set()
        for s in recent_buys:
            if s.timeframe in _CALLS_WATCH_TFS and s.timeframe not in seen:
                tfs_ordered.append(s.timeframe); seen.add(s.timeframe)
        for tf in _CALLS_WATCH_TFS:
            if tf not in seen:
                tfs_ordered.append(tf); seen.add(tf)

        best = None
        fallback_reason: Optional[str] = None
        for tf in tfs_ordered:
            try:
                thesis = build_bull_thesis(stock.ticker, tf)
            except Exception as e:
                logger.warning(f"bull_thesis {stock.ticker} {tf} failed: {e}")
                continue
            if not thesis:
                fallback_reason = fallback_reason or "not bullish enough on any timeframe"
                continue
            if thesis["confidence"] < min_bull_confidence:
                fallback_reason = (fallback_reason or
                                   f"bull conviction {thesis['confidence']} < threshold")
                continue
            try:
                sugg = suggest_options_for_signal(stock.ticker, thesis)
            except Exception as e:
                logger.warning(f"calls suggest failed for {stock.ticker} {tf}: {e}")
                continue
            if not sugg["contracts"]:
                fallback_reason = fallback_reason or "no CALL met R:R + liquidity filters"
                continue
            top_score = sugg["contracts"][0]["score"]
            if best is None or top_score > best["score"]:
                best = {
                    "score": top_score,
                    "thesis": thesis,
                    "contracts": sugg["contracts"][:5],
                }
        if best is None:
            skipped.append({"ticker": stock.ticker,
                            "reason": fallback_reason or "no viable call play"})
            continue
        out.append({
            "ticker": stock.ticker,
            "name": stock.name,
            "thesis": best["thesis"],
            "top_contracts": best["contracts"],
        })
    out.sort(key=lambda r: r["top_contracts"][0]["score"] if r["top_contracts"] else 0, reverse=True)
    return {"suggestions": out, "skipped": skipped}


@router.get("/{ticker}")
def get_options_for_ticker(
    ticker: str,
    timeframe: Optional[str] = Query(None, description="Pin to a specific timeframe's latest signal (matches the primary the UI displays). Falls back to longest-timeframe heuristic when omitted."),
    side: str = Query("auto", pattern="^(auto|calls|puts)$", description="Override leg side. 'auto' uses the signal direction (BUY→calls, SELL→puts); 'calls'/'puts' force that side regardless."),
    db: Session = Depends(get_db),
):
    """Return suggested call/put contracts for the latest signal on this ticker.

    Without `timeframe`, we use a heuristic that prefers longer timeframes —
    but that can flip direction (e.g. an old 1mo BUY beats today's 4h SELL),
    so the UI passes the primary signal's timeframe explicitly to keep the
    contract direction consistent with what the user sees.
    """
    ticker = ticker.upper()
    if not db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker).first():
        raise HTTPException(status_code=404, detail=f"{ticker} not in watchlist")

    # When the user pins a side (calls/puts), we MUST find a signal of the
    # matching direction — otherwise we'd evaluate puts against an upward-
    # pointing BUY signal's targets (reward = strike − target1, which is
    # negative when target1 > strike) and nothing would qualify. That was the
    # reason MU's PUTS tab returned 0 contracts even though it had actionable
    # SELL signals on shorter timeframes.
    desired_type = None
    if side == "calls":
        desired_type = "BUY"
    elif side == "puts":
        desired_type = "SELL"

    def _find_signal(want_type: Optional[str]) -> Optional[Signal]:
        # 1) Pinned timeframe with matching direction (if requested)
        if timeframe:
            q = db.query(Signal).filter(
                Signal.ticker == ticker,
                Signal.timeframe == timeframe,
                Signal.signal_type != "NEUTRAL",
            )
            if want_type:
                q = q.filter(Signal.signal_type == want_type)
            row = q.order_by(desc(Signal.generated_at)).first()
            if row:
                return row
        # 2) Any timeframe with matching direction — prefer the longest still
        #    actionable so option premium decay isn't crushing.
        if want_type:
            preferred_order = ["1mo", "1d", "4h", "1h", "30m", "15m", "5m"]
            rows = (db.query(Signal)
                    .filter(Signal.ticker == ticker,
                            Signal.signal_type == want_type)
                    .order_by(desc(Signal.generated_at))
                    .all())
            by_tf = {}
            for s in rows:
                by_tf.setdefault(s.timeframe, s)
            for tf in preferred_order:
                if tf in by_tf:
                    return by_tf[tf]
            # Fallback: any matching-direction signal
            return rows[0] if rows else None
        # 3) Auto: heuristic across all directions
        return _latest_actionable_signal(ticker, db)

    sig = _find_signal(desired_type)
    if not sig:
        msg = (
            f"No {desired_type} signal available for {ticker} — try the other tab or run analysis first."
            if desired_type else
            "No BUY/SELL signal available — run analysis first."
        )
        return {
            "ticker": ticker,
            "signal": None,
            "side": side if side != "auto" else None,
            "contracts": [],
            "note": msg,
        }

    signal_dict = {
        "signal_type": sig.signal_type,
        "confidence": sig.confidence,
        "timeframe": sig.timeframe,
        "entry": sig.entry,
        "stop_loss": sig.stop_loss,
        "target1": sig.target1,
        "target2": sig.target2,
        "target3": sig.target3,
    }
    # signal_type now already matches the requested side, so no override needed.
    analyzer_signal = signal_dict
    result = suggest_options_for_signal(ticker, analyzer_signal)
    return {
        "ticker": ticker,
        "signal": signal_dict,
        "side": "calls" if analyzer_signal["signal_type"] == "BUY" else "puts",
        "contracts": result["contracts"],
        "note": result["note"],
        "total": result.get("total"),
    }


@router.get("")
def get_options_suggestions(
    min_confidence: float = Query(70, description="Min signal confidence to include"),
    db: Session = Depends(get_db),
):
    """Scan the watchlist and return option ideas for every stock meeting min_confidence."""
    stocks = db.query(WatchlistStock).all()
    out = []
    for stock in stocks:
        sig = _latest_actionable_signal(stock.ticker, db)
        if not sig or sig.confidence < min_confidence:
            continue
        signal_dict = {
            "signal_type": sig.signal_type,
            "confidence": sig.confidence,
            "timeframe": sig.timeframe,
            "entry": sig.entry,
            "stop_loss": sig.stop_loss,
            "target1": sig.target1,
            "target2": sig.target2,
            "target3": sig.target3,
        }
        try:
            suggestion = suggest_options_for_signal(stock.ticker, signal_dict)
        except Exception as e:
            logger.error(f"Options suggest failed for {stock.ticker}: {e}")
            continue
        if not suggestion["contracts"]:
            continue
        out.append({
            "ticker": stock.ticker,
            "name": stock.name,
            "signal": signal_dict,
            "top_contracts": suggestion["contracts"][:3],
        })
    # Sort tickers by their top contract score
    out.sort(key=lambda r: (r["top_contracts"][0]["score"] if r["top_contracts"] else 0), reverse=True)
    return {"min_confidence": min_confidence, "suggestions": out}
