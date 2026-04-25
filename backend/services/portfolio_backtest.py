"""Portfolio-level backtest with correlation + cap enforcement.

The existing services/backtester.py evaluates each ticker in isolation —
it answers "does this strategy work on this ticker" but not "does my
whole book hold up when 20 correlated names drop 3% on the same day".

This module runs the SAME per-ticker strategies across a cross-sectional
daily time-series, aggregating the trades into a single equity curve
that respects the live-trader's risk rules:

  * max concurrent positions (cfg.max_concurrent_positions)
  * max per sector (cfg.max_per_sector)
  * beta-weighted portfolio heat (RISK_PORTFOLIO_HEAT_CAP_PCT)
  * daily loss limit (cfg.daily_loss_limit_pct)

Output is a composite equity curve + stats (max drawdown, sharpe,
per-sector concentration, drawdown-duration). This is the report that
actually answers "would my caps have saved me during the 2024 Aug carry-
trade unwind" — a question the per-ticker backtest can't.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class PortfolioTrade:
    ticker: str
    sector: Optional[str]
    direction: str           # BUY | SELL
    entry_date: pd.Timestamp
    entry_price: float
    stop_price: float
    target_price: float
    beta: float
    shares: float
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None   # win | loss | open
    pnl: float = 0.0


@dataclass
class PortfolioStats:
    starting_equity: float
    ending_equity: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown_pct: float
    max_drawdown_days: int
    sharpe_ratio: Optional[float]
    cap_rejection_count: int  # how many candidate trades were rejected by portfolio caps
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    per_sector_exposure_max: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _beta_of(ticker: str) -> float:
    """Pull beta from Fundamentals if available (clamped 0.5..2.0)."""
    try:
        from services.fundamentals import beta_weight
        return beta_weight(ticker)
    except Exception:
        return 1.0


def _sector_of(ticker: str) -> Optional[str]:
    try:
        from services.fundamentals import get_fundamentals
        f = get_fundamentals(ticker)
        return (f or {}).get("sector")
    except Exception:
        return None


def _hit_target_or_stop(df: pd.DataFrame, entry_idx: int,
                        direction: str, entry: float, stop: float,
                        target: float, max_hold_bars: int = 30) -> Tuple[int, str, float]:
    """Scan bars after entry_idx, return (exit_idx, outcome, exit_price).
    outcome ∈ {'win', 'loss', 'timeout'}."""
    end_idx = min(entry_idx + max_hold_bars, len(df) - 1)
    for i in range(entry_idx + 1, end_idx + 1):
        hi = float(df["High"].iat[i])
        lo = float(df["Low"].iat[i])
        if direction == "BUY":
            if lo <= stop:
                return i, "loss", stop
            if hi >= target:
                return i, "win", target
        else:  # SELL
            if hi >= stop:
                return i, "loss", stop
            if lo <= target:
                return i, "win", target
    # Timeout — mark-to-market at final bar
    return end_idx, "timeout", float(df["Close"].iat[end_idx])


def run_portfolio_backtest(
    tickers: Optional[List[str]] = None,
    starting_equity: float = 100_000.0,
    risk_per_trade_pct: float = 0.02,
    max_concurrent: int = 15,
    max_per_sector: int = 5,
    max_portfolio_heat_pct: float = 0.10,
    daily_loss_limit_pct: float = 0.03,
    max_tickers: int = 50,
    lookback_days: int = 365,
    min_hold_bars: int = 3,
    max_hold_bars: int = 30,
) -> Dict[str, Any]:
    """Run the composite backtest. Returns {stats, trades, rejections}."""
    from database import SessionLocal, WatchlistStock, CandidatePool
    from services.data_fetcher import fetch_ohlcv
    from services.indicators import compute_indicators
    from services.signal_generator import generate_signal

    if tickers is None:
        db = SessionLocal()
        try:
            t = set(s.ticker for s in db.query(WatchlistStock).all())
            t |= set(r.ticker for r in db.query(CandidatePool).all())
        finally:
            db.close()
        tickers = sorted(t)[:max_tickers]

    # Preload + index daily bars for every ticker, aligned on a common date
    # index. Skip tickers with insufficient history.
    data: Dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = fetch_ohlcv(t, "1d")
            if df is None or df.empty or len(df) < 260:
                continue
            df = compute_indicators(df)
            data[t] = df
        except Exception as e:
            logger.debug(f"portfolio_bt: {t} skip ({e})")
    if not data:
        return {"stats": None, "trades": [], "rejections": 0,
                "note": "no tickers with sufficient history"}

    # Common date set — intersect of all loaded tickers' trailing window
    latest = min(d.index[-1] for d in data.values())
    earliest = latest - pd.Timedelta(days=lookback_days)
    dates = pd.date_range(earliest, latest, freq="B")

    open_trades: List[PortfolioTrade] = []
    closed_trades: List[PortfolioTrade] = []
    equity = starting_equity
    equity_curve: List[Tuple[str, float]] = []
    daily_pnl: Dict[pd.Timestamp, float] = {}
    rejections = 0
    per_sector_exposure_max: Dict[str, int] = {}

    for d in dates:
        # Mark open positions to market at today's close for equity tracking
        todays_unrealized = 0.0
        for tr in open_trades:
            df = data.get(tr.ticker)
            if df is None or d not in df.index:
                continue
            close = float(df["Close"].at[d])
            todays_unrealized += (close - tr.entry_price) * tr.shares * (1 if tr.direction == "BUY" else -1)

        # Close-out check: did any open trades hit target/stop today?
        still_open = []
        for tr in open_trades:
            df = data.get(tr.ticker)
            if df is None or d not in df.index:
                still_open.append(tr)
                continue
            hi = float(df["High"].at[d])
            lo = float(df["Low"].at[d])
            exit_px = None; outcome = None
            if tr.direction == "BUY":
                if lo <= tr.stop_price:
                    exit_px, outcome = tr.stop_price, "loss"
                elif hi >= tr.target_price:
                    exit_px, outcome = tr.target_price, "win"
            else:
                if hi >= tr.stop_price:
                    exit_px, outcome = tr.stop_price, "loss"
                elif lo <= tr.target_price:
                    exit_px, outcome = tr.target_price, "win"
            # Max-hold timeout
            if exit_px is None and (d - tr.entry_date).days >= max_hold_bars:
                exit_px, outcome = float(df["Close"].at[d]), "timeout"
            if exit_px is not None:
                tr.exit_date = d; tr.exit_price = exit_px; tr.outcome = outcome
                tr.pnl = (exit_px - tr.entry_price) * tr.shares * (1 if tr.direction == "BUY" else -1)
                equity += tr.pnl
                daily_pnl[d] = daily_pnl.get(d, 0.0) + tr.pnl
                closed_trades.append(tr)
            else:
                still_open.append(tr)
        open_trades = still_open

        # Daily loss gate — if today's realized losses exceed the limit, pause new entries
        today_realized = daily_pnl.get(d, 0.0)
        daily_loss_hit = today_realized <= -starting_equity * daily_loss_limit_pct

        # Scan for new signals, respect caps
        if not daily_loss_hit and len(open_trades) < max_concurrent:
            for ticker in tickers:
                if len(open_trades) >= max_concurrent:
                    break
                if any(tr.ticker == ticker for tr in open_trades):
                    continue  # one position per ticker
                df = data.get(ticker)
                if df is None or d not in df.index:
                    continue
                idx_loc = df.index.get_loc(d)
                try:
                    sliced = df.iloc[: idx_loc + 1]
                    sig = generate_signal(ticker, "1d", sliced)
                except Exception:
                    continue
                if not sig or sig.get("signal_type") not in ("BUY", "SELL"):
                    continue
                # Caps — per-sector
                sector = _sector_of(ticker) or "?"
                same_sector = sum(1 for tr in open_trades if (tr.sector or "?") == sector)
                if same_sector >= max_per_sector:
                    rejections += 1
                    continue
                # Caps — beta-weighted portfolio heat
                beta = _beta_of(ticker)
                entry = float(sig["entry"] or 0)
                stop = float(sig["stop_loss"] or 0)
                target = float(sig["target1"] or 0)
                if entry <= 0 or stop <= 0 or target <= 0:
                    continue
                risk_per_share = abs(entry - stop)
                if risk_per_share <= 0:
                    continue
                shares = max(1, int((equity * risk_per_trade_pct) / risk_per_share))
                raw_heat = risk_per_share * shares
                weighted_heat = raw_heat * max(0.5, min(2.0, beta))
                current_heat = sum(
                    abs(t.entry_price - t.stop_price) * t.shares * max(0.5, min(2.0, t.beta))
                    for t in open_trades
                )
                if (current_heat + weighted_heat) > equity * max_portfolio_heat_pct:
                    rejections += 1
                    continue
                # Enter
                open_trades.append(PortfolioTrade(
                    ticker=ticker, sector=sector, direction=sig["signal_type"],
                    entry_date=d, entry_price=entry, stop_price=stop,
                    target_price=target, beta=beta, shares=shares,
                ))
                # Track peak per-sector concentration
                per_sector_exposure_max[sector] = max(
                    per_sector_exposure_max.get(sector, 0),
                    sum(1 for t in open_trades if (t.sector or "?") == sector)
                )

        equity_curve.append((d.strftime("%Y-%m-%d"), round(equity + todays_unrealized, 2)))

    # Compute stats
    ending_equity = equity_curve[-1][1] if equity_curve else starting_equity
    wins = sum(1 for t in closed_trades if t.outcome == "win")
    losses = sum(1 for t in closed_trades if t.outcome == "loss")
    total = len(closed_trades)
    win_rate = wins / total if total else 0.0
    # Drawdown
    peak = starting_equity
    max_dd_pct = 0.0
    max_dd_days = 0
    dd_start: Optional[pd.Timestamp] = None
    for ds, eq in equity_curve:
        if eq > peak:
            peak = eq
            dd_start = None
        else:
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > max_dd_pct:
                max_dd_pct = dd
            if dd_start is None and dd > 0:
                dd_start = pd.Timestamp(ds)
            if dd_start is not None:
                days = (pd.Timestamp(ds) - dd_start).days
                if days > max_dd_days:
                    max_dd_days = days
    # Sharpe (simplified, daily)
    sharpe = None
    if len(equity_curve) > 30:
        eqs = pd.Series([eq for _, eq in equity_curve])
        rets = eqs.pct_change().dropna()
        if len(rets) and rets.std() > 0:
            sharpe = float(round((rets.mean() / rets.std()) * (252 ** 0.5), 2))

    stats = PortfolioStats(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        total_trades=total,
        wins=wins, losses=losses, win_rate=round(win_rate, 3),
        max_drawdown_pct=round(max_dd_pct, 4),
        max_drawdown_days=int(max_dd_days),
        sharpe_ratio=sharpe,
        cap_rejection_count=rejections,
        equity_curve=equity_curve,
        per_sector_exposure_max=per_sector_exposure_max,
    )
    return {
        "stats": stats.as_dict(),
        "trades": [
            {
                "ticker": t.ticker, "sector": t.sector, "direction": t.direction,
                "entry_date": t.entry_date.strftime("%Y-%m-%d") if t.entry_date else None,
                "entry_price": t.entry_price, "stop_price": t.stop_price,
                "target_price": t.target_price, "shares": t.shares,
                "exit_date": t.exit_date.strftime("%Y-%m-%d") if t.exit_date else None,
                "exit_price": t.exit_price, "outcome": t.outcome, "pnl": round(t.pnl, 2),
            }
            for t in closed_trades
        ],
        "params": {
            "starting_equity": starting_equity,
            "risk_per_trade_pct": risk_per_trade_pct,
            "max_concurrent": max_concurrent,
            "max_per_sector": max_per_sector,
            "max_portfolio_heat_pct": max_portfolio_heat_pct,
            "daily_loss_limit_pct": daily_loss_limit_pct,
            "lookback_days": lookback_days,
            "tickers_count": len(data),
        },
    }
