"""r96 R4: survivorship-bias mitigation for the backtest universe.

The audit flagged that backtester + portfolio_backtest pulled tickers from
WatchlistStock and CandidatePool — the CURRENTLY-active set. Names that
were in the watchlist a year ago but have since been delisted are absent,
which inflates historical Sharpe (the universe is biased toward
"this-name-still-trades-now → it never went bankrupt").

This module exposes:

  * `mark_delisted(ticker)` — flag a ticker as delisted (idempotent).
    Persists `delisted=True` + `delisted_at=now()` on WatchlistStock.
    Auto-detection from data_fetcher's repeated empty-DF returns is a
    follow-on; for MVP this is operator-triggered via the admin endpoint.

  * `list_universe(include_delisted=False)` — returns the universe for
    backtest purposes. Default behavior matches legacy (active only);
    `include_delisted=True` adds delisted names so the historical
    backtest is run on a true point-in-time set.

  * `clamp_df_to_delisted_at(df, ticker)` — when a delisted ticker is
    included, its OHLCV is clipped at the recorded delisted_at so the
    backtest doesn't pretend it kept trading post-delisting.

The flag `cfg.survivorship_filter_enabled` gates whether callers actually
USE the include-delisted universe. Default False — operator opts in once
they've marked enough tickers for the difference to be meaningful.
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def mark_delisted(ticker: str, when: Optional[datetime] = None) -> bool:
    """Set WatchlistStock.delisted=True + delisted_at=now (or `when`).
    Idempotent. Returns True if a row was modified, False otherwise."""
    try:
        from database import SessionLocal, WatchlistStock
        db = SessionLocal()
        try:
            row = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker.upper()).first()
            if row is None:
                return False
            if bool(getattr(row, "delisted", False)):
                return False  # already marked
            row.delisted = True
            row.delisted_at = when or datetime.utcnow()
            db.commit()
            logger.warning(f"survivorship.mark_delisted: {ticker.upper()} flagged at {row.delisted_at}")
            return True
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"survivorship.mark_delisted({ticker}): {e}")
        return False


def list_universe(include_delisted: bool = False) -> List[str]:
    """Return the union of WatchlistStock and CandidatePool tickers.
    When include_delisted=True, also includes WatchlistStock rows where
    delisted=True. Sorted, deduplicated, uppercase.
    """
    try:
        from database import SessionLocal, WatchlistStock, CandidatePool
        db = SessionLocal()
        try:
            tickers = set()
            wq = db.query(WatchlistStock)
            if not include_delisted:
                # Survivorship-biased default: only currently-active rows.
                wq = wq.filter(
                    (WatchlistStock.delisted == False)  # noqa: E712
                    | (WatchlistStock.delisted.is_(None))
                )
            for r in wq.all():
                tickers.add((r.ticker or "").upper())
            for r in db.query(CandidatePool).all():
                tickers.add((r.ticker or "").upper())
            tickers.discard("")
            return sorted(tickers)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"survivorship.list_universe: {e}")
        return []


def clamp_df_to_delisted_at(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """When a delisted ticker is included in a backtest, clip its OHLCV
    to bars at-or-before the recorded delisted_at — so the simulation
    doesn't pretend the ticker kept trading post-delisting. Returns the
    input df unchanged if no delisting record exists.
    """
    if df is None or df.empty:
        return df
    try:
        from database import SessionLocal, WatchlistStock
        db = SessionLocal()
        try:
            row = db.query(WatchlistStock).filter(WatchlistStock.ticker == ticker.upper()).first()
            if row is None or not bool(getattr(row, "delisted", False)) or not row.delisted_at:
                return df
            ts = pd.Timestamp(row.delisted_at)
            # Many of our daily frames have a tz-naive DatetimeIndex; coerce
            # safely so the comparison doesn't raise.
            idx = df.index
            try:
                if hasattr(idx, "tz") and idx.tz is not None:
                    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
            except Exception:
                pass
            return df[df.index <= ts]
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"clamp_df_to_delisted_at({ticker}): {e}")
        return df


def survivorship_enabled() -> bool:
    """Cheap accessor for the cfg flag — callers in the backtest hot
    path can short-circuit without a DB round-trip when not used."""
    try:
        from database import SessionLocal, AutoTraderConfig
        db = SessionLocal()
        try:
            cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            return bool(getattr(cfg, "survivorship_filter_enabled", False)) if cfg else False
        finally:
            db.close()
    except Exception:
        return False
