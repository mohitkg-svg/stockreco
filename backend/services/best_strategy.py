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

    # r43 fix #1.3: rank by OOS Sharpe (with min-trades-per-fold gate),
    # not by `confidence` (a display number, not OOS expectancy). Also
    # the original code had a per-direction bug — overwrote BUY with
    # SELL on the second iteration because the row had only one
    # (ticker)-keyed slot. We now compute a winner per direction and
    # store ONLY the strongest direction (BUY OR SELL, whichever has
    # higher OOS Sharpe AND meets the min-OOS-trades floor).
    MIN_OOS_TRADES = 10
    updated = 0
    db = SessionLocal()
    try:
        for ticker in sorted(tickers):
            try:
                df = fetch_ohlcv(ticker, "1d")
                if df is None or df.empty or len(df) < 252:
                    continue
                # r82: stamp ticker on df.attrs so per-ticker strategies
                # (e.g., _vix_spike_reversion which is restricted to SPY/QQQ)
                # can identify the underlying.
                try:
                    df.attrs["ticker"] = ticker
                except Exception:
                    pass
                multi = run_multi_strategy(df, timeframe="1d")
                if not multi.get("results"):
                    continue
                # r47 fix #T0a-3: prior code read non-existent stat keys
                # (`avg_pl` and `oos_trades`-as-trade-count). Backtester
                # actually emits `total_trades` (count) and `oos_trades` is
                # the WF fold-count (0..4). avg_pl is derived from
                # win_rate × avg_win + (1-wr) × avg_loss. Without these
                # corrections the ranker silently degraded to pure-Sharpe
                # selection of noise (n_trades floor never met).
                def _score(r: Dict[str, Any]) -> float:
                    s = r.get("stats") or {}
                    sharpe = float(s.get("sharpe_ratio") or 0.0)
                    n = int(s.get("total_trades") or 0)
                    wr_pct = float(s.get("win_rate") or 0.0)  # 0-100
                    wr = wr_pct / 100.0
                    avg_win = float(s.get("avg_win_pct") or 0.0)
                    avg_loss = float(s.get("avg_loss_pct") or 0.0)
                    # avg_loss_pct is stored as a NEGATIVE number (loss)
                    avg_pl = wr * avg_win + (1.0 - wr) * avg_loss
                    if n < MIN_OOS_TRADES:
                        return -1e9   # below sample-size floor — never picked
                    primary = n * wr * avg_pl
                    return primary if abs(primary) > 1e-6 else sharpe
                buy_winners = [r for r in multi["results"] if r["direction"] == "BUY"]
                sell_winners = [r for r in multi["results"] if r["direction"] == "SELL"]
                top_buy = max(buy_winners, key=_score) if buy_winners else None
                top_sell = max(sell_winners, key=_score) if sell_winners else None
                # If neither side meets the min-trades floor, skip the ticker.
                cands = [c for c in (top_buy, top_sell) if c and _score(c) > -1e8]
                if not cands:
                    continue
                w = max(cands, key=_score)
                direction = w["direction"]
                row = db.query(BestStrategyPerTicker).filter(
                    BestStrategyPerTicker.ticker == ticker
                ).first()
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
    """If the ticker's persisted best-strategy validated edge in `direction`
    with sufficient OOS evidence, return 1.08 (8% confidence boost).

    r47 fix #T0a-6: caller passes literal "Composite (multi-factor)" but
    persisted strategies have specific names (e.g. "Trend Following"). The
    string-match guarded the boost so tightly it never fired in production.
    Composite signals are by definition a basket — boost when the ticker
    has *any* validated direction-edge AND the live signal direction agrees.
    Strategy-name match (when supplied) gives an additional small bump."""
    row = get_for_ticker(ticker)
    if not row:
        return 1.0
    if row["direction"] != direction:
        return 1.0
    n = int(row.get("oos_trades") or 0)
    conf = float(row.get("confidence") or 0)
    if n < 3 or conf < 50:
        return 1.0
    # Base boost: ticker has demonstrated edge in this direction.
    base = 1.06
    # Stronger boost when the live strategy NAME also matches.
    try:
        if strategy and str(strategy).strip().lower() == str(row["strategy"]).strip().lower():
            return 1.10
    except Exception:
        pass
    return base
