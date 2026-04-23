"""
Per-ticker best-strategy persistence.

Weekly job: for each watchlist + candidate-pool ticker, run the walk-forward
backtest, record the top strategy for each direction (BUY/SELL). Signal
generator consults this when emitting signals — preferentially issues
signals from strategies that have demonstrated edge on THIS ticker.

Reduces "one-size-fits-all" strategy mismatch: trend-following works great
on trending mega-caps but fails on mean-reverting ones; pullback-to-MA is
the opposite. Per-ticker winner selection captures this.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

from database import SessionLocal, BestStrategyPerTicker, WatchlistStock, CandidatePool

logger = logging.getLogger(__name__)


def recompute_all() -> Dict[str, Any]:
    """Recompute best-strategy for every watchlist + candidate-pool ticker.
    Called from the scheduler weekly. Takes ~5-10 min for 30-50 tickers."""
    from services.data_fetcher import fetch_ohlcv
    from services.backtester import run_multi_strategy

    db = SessionLocal()
    try:
        tickers = set(s.ticker for s in db.query(WatchlistStock).all())
        # Union with any currently-in-pool tickers so universe-scanned names
        # also get per-ticker tuning.
        tickers |= set(r.ticker for r in db.query(CandidatePool).all())
    finally:
        db.close()

    if not tickers:
        return {"updated": 0}

    updated = 0
    db = SessionLocal()
    try:
        for ticker in sorted(tickers):
            try:
                df = fetch_ohlcv(ticker, "1d")
                if df is None or df.empty or len(df) < 252:
                    continue
                multi = run_multi_strategy(df, timeframe="1d")
                best = multi.get("best")
                if not best:
                    continue
                # Pick the best for each direction.
                for direction in ("BUY", "SELL"):
                    winners = [r for r in multi["results"] if r["direction"] == direction]
                    if not winners:
                        continue
                    w = max(winners, key=lambda r: r["confidence"])
                    row = db.query(BestStrategyPerTicker).filter(
                        BestStrategyPerTicker.ticker == ticker
                    ).first()
                    # One row per ticker — hold the side with higher confidence
                    # (BUY is our primary direction; SELL wins only when much stronger).
                    if row is None:
                        row = BestStrategyPerTicker(
                            ticker=ticker, strategy=w["strategy"],
                            direction=direction, confidence=w["confidence"],
                            oos_trades=w.get("oos_trades") or 0,
                            win_rate=(w["stats"].get("win_rate") or 0) / 100.0,
                            avg_pl=w["stats"].get("avg_pl") or 0.0,
                        )
                        db.add(row)
                        updated += 1
                    else:
                        # Overwrite if new winner has higher confidence
                        if w["confidence"] > row.confidence or row.direction == direction:
                            row.strategy = w["strategy"]
                            row.direction = direction
                            row.confidence = w["confidence"]
                            row.oos_trades = w.get("oos_trades") or 0
                            row.win_rate = (w["stats"].get("win_rate") or 0) / 100.0
                            row.avg_pl = w["stats"].get("avg_pl") or 0.0
                            updated += 1
            except Exception as e:
                logger.debug(f"best_strategy {ticker}: {e}")
                continue
        db.commit()
    finally:
        db.close()
    logger.info(f"best_strategy: recomputed for {len(tickers)} tickers, {updated} rows updated")
    return {"tickers": len(tickers), "updated": updated}


def get_for_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Return the persisted best-strategy row for `ticker`, or None."""
    db = SessionLocal()
    try:
        row = db.query(BestStrategyPerTicker).filter(
            BestStrategyPerTicker.ticker == ticker.upper()
        ).first()
        if not row:
            return None
        return {
            "ticker": row.ticker,
            "strategy": row.strategy,
            "direction": row.direction,
            "confidence": row.confidence,
            "oos_trades": row.oos_trades,
            "win_rate": row.win_rate,
            "avg_pl": row.avg_pl,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    finally:
        db.close()


def confidence_boost(ticker: str, strategy: Optional[str], direction: str) -> float:
    """If `strategy` matches the ticker's persisted best-strategy row for
    `direction`, return 1.08 (8% confidence boost). Otherwise 1.0.
    Used by signal_generator at confidence-finalization time."""
    row = get_for_ticker(ticker)
    if not row or not strategy:
        return 1.0
    if row["direction"] != direction:
        return 1.0
    if str(strategy).strip().lower() == str(row["strategy"]).strip().lower():
        # High confidence + OOS validated → give a small bump.
        if (row.get("oos_trades") or 0) >= 3 and row["confidence"] >= 50:
            return 1.08
    return 1.0
