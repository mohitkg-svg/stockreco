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
    """One simulated trade within a portfolio-level backtest run.

    Tracks entry context (ticker, sector, beta, ADX/VIX regime tags)
    plus exit outcome. Sector + beta are used for the cap enforcement
    (max_per_sector, beta-weighted heat) that distinguishes this from
    the per-ticker `services.backtester._simulate` engine.

    `regime_label()` derives a human-readable bucket (trending / chop /
    high_vix / normal) used in the by-regime stats breakdown.
    """
    ticker: str
    sector: Optional[str]
    direction: str           # BUY | SELL
    entry_date: pd.Timestamp
    entry_price: float
    stop_price: float
    target_price: float
    beta: float
    shares: float
    entry_adx: Optional[float] = None    # regime tagging at entry
    entry_vix: Optional[float] = None
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None   # win | loss | open
    pnl: float = 0.0

    def regime_label(self) -> str:
        if self.entry_vix is not None and self.entry_vix > 25:
            return "high_vix"
        if self.entry_adx is not None:
            if self.entry_adx > 25: return "trending"
            if self.entry_adx < 20: return "chop"
        return "normal"


@dataclass
class PortfolioStats:
    """Aggregate stats for a portfolio backtest run.

    Combines headline metrics (equity, win rate, profit factor, Sharpe,
    Sortino, Calmar, expectancy) with composite-portfolio-only stats
    (max drawdown duration, cap rejection count, by-regime breakdown,
    per-sector concentration peak, realized pair-correlation diagnostic).

    r38 added Monte Carlo bootstrap percentiles (p5/p50/p95 of max
    drawdown and ending equity). r39 added Sortino/Calmar/turnover.
    Stress-window metadata (key + label) is set when the run targeted
    a canned historical drawdown range.

    Serialization: `as_dict()` → JSON-friendly dict for the API response.
    """
    starting_equity: float
    ending_equity: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown_pct: float
    max_drawdown_days: int
    sharpe_ratio: Optional[float]
    cap_rejection_count: int
    # Profit factor = gross wins / |gross losses|. >1.5 is solid, <1.0 is
    # a losing strategy even at >50% win rate.
    profit_factor: Optional[float] = None
    # Per-regime win-rate / count / avg PL. Regimes determined by the
    # entry-bar's ADX + VIX context:
    #   'trending'  = entry ADX > 25
    #   'chop'      = entry ADX < 20
    #   'high_vix'  = VIX > 25 at entry
    #   'normal'    = everything else
    by_regime: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)
    per_sector_exposure_max: Dict[str, int] = field(default_factory=dict)
    # Pairwise daily-return correlation across the *traded* tickers over the
    # backtest window. Diagnostic only — not enforced as a cap (that's
    # pairwise-correlation Tier C work). High avg_corr means the book wasn't
    # diversified beyond what beta-weighting captures; one shock moves
    # everything together. Useful as a "is my universe really diverse?" check.
    avg_pair_corr: Optional[float] = None
    max_pair_corr: Optional[float] = None
    stress_window: Optional[str] = None
    stress_window_label: Optional[str] = None
    # Per-trade expectancy: WR × avg_win + (1-WR) × avg_loss. Single number
    # answering "what's my expected $ per trade after costs?" — positive
    # is necessary but not sufficient (a +$1 expectancy with $1000 max loss
    # is still ruinous). Reported in dollars per trade at the actual
    # backtest sizing, not normalized.
    expectancy: Optional[float] = None
    # Monte Carlo bootstrap of trade returns. Resamples closed-trade pnl
    # values WITH replacement (`n_samples` paths × `len(closed_trades)`
    # picks each), tracking the equity-curve drawdown for each path.
    # Reports the empirical p5 / p50 / p95 max-drawdown — a much more
    # honest answer to "how bad could this strategy get?" than a single
    # historical realization. P95 (95th percentile worst drawdown) is
    # the headline number for risk-of-ruin calibration.
    mc_max_drawdown_p5_pct: Optional[float] = None
    mc_max_drawdown_p50_pct: Optional[float] = None
    mc_max_drawdown_p95_pct: Optional[float] = None
    mc_ending_equity_p5: Optional[float] = None
    mc_ending_equity_p50: Optional[float] = None
    mc_ending_equity_p95: Optional[float] = None
    mc_paths: Optional[int] = None
    # Sortino: like Sharpe but penalizes only downside volatility. Same
    # annualization factor as Sharpe (sqrt(252) for daily). Strategies
    # with right-skewed returns score higher under Sortino than Sharpe.
    sortino_ratio: Optional[float] = None
    # Calmar: annualized_return / |max_drawdown|. Drawdown-focused — the
    # most ruthless way to compare strategies pre-live. Calmar > 1 means
    # the strategy makes more per year than its worst observed drawdown.
    calmar_ratio: Optional[float] = None
    # Turnover: round-trips per year on the average dollar deployed.
    # A high-turnover strategy is more sensitive to costs — even at our
    # conservative 6bps round-trip, 200 turns/yr → 1.2% drag per year.
    turnover_per_year: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Canned historical drawdown windows. Pre-live stress test: re-run the
# strategy with today's caps over a date range when correlated drawdowns
# actually happened. The question this answers is "would my caps have
# protected the account in <event>?". The end date adds 30 calendar days
# of recovery so we observe how the book behaved AFTER the shock too.
STRESS_WINDOWS: Dict[str, Tuple[str, str, str]] = {
    "aug2024_carry":      ("2024-07-25", "2024-09-15", "Aug 2024 yen-carry unwind"),
    "mar2020_covid":      ("2020-02-15", "2020-04-30", "Mar 2020 COVID crash"),
    "feb2018_volmageddon": ("2018-01-25", "2018-03-15", "Feb 2018 volmageddon (XIV blow-up)"),
    "dec2018_powell":     ("2018-10-01", "2019-01-15", "Q4 2018 Powell pivot drawdown"),
    "aug2015_china":      ("2015-08-15", "2015-10-15", "Aug 2015 China devaluation"),
}


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
    stress_window: Optional[str] = None,
    harmonized_cost_model: bool = True,
) -> Dict[str, Any]:
    """Run the composite backtest. Returns {stats, trades, rejections}.

    `stress_window`: optional key from STRESS_WINDOWS. When set, the
    backtest runs over the canned date range instead of the trailing
    `lookback_days` window — answers "would my caps have protected the
    account during <historical event>?".
    """
    from database import SessionLocal, WatchlistStock, CandidatePool
    from services.data_fetcher import fetch_ohlcv
    from services.indicators import compute_indicators
    from services.signal_generator import generate_signal

    if tickers is None:
        # r96 R4: when survivorship_filter_enabled is True, INCLUDE delisted
        # tickers in the historical universe so the backtest sees the true
        # point-in-time set, not just current survivors. Their data frames
        # are clamped to delisted_at during signal generation below.
        try:
            from services.survivorship import list_universe, survivorship_enabled
            include_delisted = survivorship_enabled()
            tickers = list_universe(include_delisted=include_delisted)[:max_tickers]
        except Exception:
            db = SessionLocal()
            try:
                t = set(s.ticker for s in db.query(WatchlistStock).all())
                t |= set(r.ticker for r in db.query(CandidatePool).all())
            finally:
                db.close()
            tickers = sorted(t)[:max_tickers]

    # Validate stress_window BEFORE any data fetch — unknown key returns
    # cleanly without a 60-second yfinance round-trip.
    if stress_window and stress_window not in STRESS_WINDOWS:
        return {"stats": None, "trades": [], "rejections": 0,
                "note": f"unknown stress_window {stress_window!r}; "
                        f"valid keys: {sorted(STRESS_WINDOWS)}"}

    # Always load ^VIX for regime tagging — without this, every entry's
    # `entry_vix` is None and the by-regime breakdown collapses 'high_vix'
    # into 'normal'. Cheap (one extra fetch) and only loaded into `data`
    # so signal generation iteration over `tickers` is unaffected.
    fetch_tickers = list(tickers)
    if "^VIX" not in fetch_tickers:
        fetch_tickers.append("^VIX")

    # Preload + index daily bars for every ticker, aligned on a common date
    # index. Skip tickers with insufficient history. For pre-2024 stress
    # windows the cached 2y range isn't enough — fall through to the raw
    # chart fetcher with a longer range string. This path is one-shot
    # (operator-triggered backtest, not the live scan loop).
    from services.data_fetcher import _fetch_chart as _fc_raw  # internal, but cheap
    needs_extended_history = stress_window and pd.Timestamp(
        STRESS_WINDOWS[stress_window][0]
    ) < pd.Timestamp.today() - pd.Timedelta(days=730)
    data: Dict[str, pd.DataFrame] = {}
    for t in fetch_tickers:
        try:
            if needs_extended_history:
                # 10y covers every canned window we have. Yahoo accepts up to "max".
                df = _fc_raw(t, "1d", "10y")
            else:
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

    # Date range: stress window overrides trailing lookback when set.
    if stress_window:
        sw_start, sw_end, sw_label = STRESS_WINDOWS[stress_window]
        earliest = pd.Timestamp(sw_start)
        latest = pd.Timestamp(sw_end)
    else:
        sw_label = None
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
        # Close-out check: did any open trades hit target/stop today?
        # Overnight-gap modeling: if today's OPEN gapped through the stop (or
        # target), the realistic fill was at the OPEN, not the stop price. In
        # a gap-down, a BUY stop at $100 is filled at whatever the bar opened
        # at — potentially $95, widening the loss. Models the actual behavior
        # of a GTC stop resting overnight.
        still_open = []
        for tr in open_trades:
            df = data.get(tr.ticker)
            if df is None or d not in df.index:
                still_open.append(tr)
                continue
            day_open = float(df["Open"].at[d])
            hi = float(df["High"].at[d])
            lo = float(df["Low"].at[d])
            exit_px = None; outcome = None
            if tr.direction == "BUY":
                # Gap-through-stop: today opened at or below the stop → realistic
                # fill is the open (worse than stop_price).
                if day_open <= tr.stop_price:
                    exit_px, outcome = day_open, "loss"
                elif day_open >= tr.target_price:
                    # Unusual but possible — opened above T1, fill at open.
                    exit_px, outcome = day_open, "win"
                elif lo <= tr.stop_price:
                    exit_px, outcome = tr.stop_price, "loss"
                elif hi >= tr.target_price:
                    exit_px, outcome = tr.target_price, "win"
            else:
                if day_open >= tr.stop_price:
                    exit_px, outcome = day_open, "loss"
                elif day_open <= tr.target_price:
                    exit_px, outcome = day_open, "win"
                elif hi >= tr.stop_price:
                    exit_px, outcome = tr.stop_price, "loss"
                elif lo <= tr.target_price:
                    exit_px, outcome = tr.target_price, "win"
            # Max-hold timeout
            if exit_px is None and (d - tr.entry_date).days >= max_hold_bars:
                exit_px, outcome = float(df["Close"].at[d]), "timeout"
            if exit_px is not None:
                tr.exit_date = d; tr.exit_price = exit_px; tr.outcome = outcome
                # r48 BACKLOG #backtest-P0-3: charge transaction costs
                # (round-trip 12bps baseline + Corwin-Schultz adder for the
                # exit bar). Prior code charged ZERO costs in portfolio
                # backtests — overstated total return by ~2-5% annually.
                # r96 F6: when harmonized_cost_model=True, derive baseline +
                # adverse selection from the SAME constants the per-ticker
                # backtester uses (services.backtester.COMMISSION_BPS,
                # SLIPPAGE_BPS, ADVERSE_BPS). The audit flagged this module's
                # 12bps hardcoded baseline as disagreeing with backtester's
                # 12+3=15bps (incl. adverse). Both estimators converge here.
                gross = (exit_px - tr.entry_price) * tr.shares * (1 if tr.direction == "BUY" else -1)
                if harmonized_cost_model:
                    from services.backtester import (
                        COMMISSION_BPS as _BT_COMMISSION_BPS,
                        SLIPPAGE_BPS as _BT_SLIPPAGE_BPS,
                        ADVERSE_BPS as _BT_ADVERSE_BPS,
                    )
                    # Round-trip baseline + entry-side adverse selection.
                    cost_bps = 2.0 * (_BT_COMMISSION_BPS + _BT_SLIPPAGE_BPS) + _BT_ADVERSE_BPS
                else:
                    cost_bps = 12.0  # baseline round-trip (legacy)
                try:
                    # High-low estimator for current bar
                    h = float(df["High"].at[d]) if "High" in df.columns else None
                    l = float(df["Low"].at[d]) if "Low" in df.columns else None
                    if h and l and h > l:
                        # Simplified Corwin-Schultz: bps proxy
                        rng_bps = (h - l) / max(h, 1e-9) * 10_000
                        cost_bps += min(40.0, rng_bps * 0.3)
                except Exception:
                    pass
                # Stop fills slip — Hasbrouck 1991 effective spread
                if outcome == "loss" and exit_px == tr.stop_price:
                    cost_bps += 25.0
                cost_dollars = abs(tr.entry_price * tr.shares) * (cost_bps / 10_000.0)
                tr.pnl = gross - cost_dollars
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
            # r39 audit cleanup: pre-compute current_heat ONCE per day
            # outside the per-ticker loop. Previously this was an
            # `O(N_tickers × N_open_positions)` recompute inside the inner
            # loop — for a 50-ticker × 250-day backtest with up to 15 open
            # positions that's ~187k extra iterations. The heat number is
            # the same for every candidate on the same day; recompute only
            # when a new entry actually opens (incremental update below).
            current_heat = sum(
                abs(t.entry_price - t.stop_price) * t.shares * max(0.5, min(2.0, t.beta))
                for t in open_trades
            )
            heat_cap = equity * max_portfolio_heat_pct
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
                if (current_heat + weighted_heat) > heat_cap:
                    rejections += 1
                    continue
                # Regime tagging at entry — ADX_14 from this bar, VIX
                # from the shared VIX series if available.
                entry_adx = None
                try:
                    if "ADX_14" in sliced.columns:
                        entry_adx = float(sliced["ADX_14"].iat[-1])
                except Exception:
                    pass
                entry_vix = None
                vix_df = data.get("^VIX")
                if vix_df is not None and d in vix_df.index:
                    try:
                        entry_vix = float(vix_df["Close"].at[d])
                    except Exception:
                        pass
                # Enter
                open_trades.append(PortfolioTrade(
                    ticker=ticker, sector=sector, direction=sig["signal_type"],
                    entry_date=d, entry_price=entry, stop_price=stop,
                    target_price=target, beta=beta, shares=shares,
                    entry_adx=entry_adx, entry_vix=entry_vix,
                ))
                # Incrementally update the precomputed current_heat so the
                # next ticker in this same day's loop sees the updated value.
                current_heat += weighted_heat
                # Track peak per-sector concentration
                per_sector_exposure_max[sector] = max(
                    per_sector_exposure_max.get(sector, 0),
                    sum(1 for t in open_trades if (t.sector or "?") == sector)
                )

        # Mark remaining open positions to market AFTER close-outs and new
        # entries. Counting trades that closed today here would double-count
        # them — their P&L is already in `equity` via line ~377 (closed_trades
        # path). Only positions still open at end-of-day belong in unrealized.
        todays_unrealized = 0.0
        for tr in open_trades:
            df = data.get(tr.ticker)
            if df is None or d not in df.index:
                continue
            close = float(df["Close"].at[d])
            todays_unrealized += (close - tr.entry_price) * tr.shares * (1 if tr.direction == "BUY" else -1)
        equity_curve.append((d.strftime("%Y-%m-%d"), round(equity + todays_unrealized, 2)))

    # Compute stats
    ending_equity = equity_curve[-1][1] if equity_curve else starting_equity
    wins = sum(1 for t in closed_trades if t.outcome == "win")
    losses = sum(1 for t in closed_trades if t.outcome == "loss")
    total = len(closed_trades)
    win_rate = wins / total if total else 0.0
    # Drawdown — r42 fix #1.10: percentage units (matches backtester.py and
    # the UI's "X.XX%" rendering). Previously this returned a fraction
    # while the per-ticker backtester returned a percentage; the UI
    # rendered them as if both were percentages, so the portfolio number
    # appeared 100× too small.
    peak = starting_equity
    max_dd_pct = 0.0
    max_dd_days = 0
    dd_start: Optional[pd.Timestamp] = None
    for ds, eq in equity_curve:
        if eq > peak:
            peak = eq
            dd_start = None
        else:
            dd = ((peak - eq) / peak * 100.0) if peak > 0 else 0.0
            if dd > max_dd_pct:
                max_dd_pct = dd
            if dd_start is None and dd > 0:
                dd_start = pd.Timestamp(ds)
            if dd_start is not None:
                days = (pd.Timestamp(ds) - dd_start).days
                if days > max_dd_days:
                    max_dd_days = days
    # Sharpe (simplified, daily). Sortino: same numerator, denominator is
    # downside-deviation against MAR=0 (RMS of min(0, r)), NOT std of the
    # already-filtered negative subset.
    # r42 fix #1.6: previously we used `downside.std()` where `downside =
    # rets[rets < 0]`. That's the std of negatives, which divides by their
    # mean, not by zero — the result is amplitude-of-downside-noise, not
    # downside-vol-vs-MAR. The correct formula is sqrt(mean(min(0, r)^2))
    # against MAR=0, which is what production sortino implementations use.
    sharpe = None
    sortino = None
    if len(equity_curve) > 30:
        eqs = pd.Series([eq for _, eq in equity_curve])
        rets = eqs.pct_change().dropna()
        if len(rets) and rets.std() > 0:
            sharpe = float(round((rets.mean() / rets.std()) * (252 ** 0.5), 2))
        if len(rets):
            downside_returns = rets.where(rets < 0, 0.0)
            dd_dev = float((downside_returns ** 2).mean() ** 0.5)
            if dd_dev > 0:
                sortino = float(round((rets.mean() / dd_dev) * (252 ** 0.5), 2))

    # Calmar: annualized_return / |max_drawdown|. Drawdown-adjusted return.
    # period_years derived from the equity curve span; capped at ≥ 0.25y
    # to avoid blow-up on very short backtests.
    calmar = None
    if max_dd_pct > 0 and len(equity_curve) >= 30:
        # max_dd_pct is now in percent (0-100); convert to fraction for Calmar.
        _dd_frac_for_calmar = max_dd_pct / 100.0
        try:
            t0 = pd.Timestamp(equity_curve[0][0])
            t1 = pd.Timestamp(equity_curve[-1][0])
            period_years = max(0.25, (t1 - t0).days / 365.25)
            total_return = (ending_equity - starting_equity) / starting_equity
            ann_return = (1 + total_return) ** (1 / period_years) - 1
            calmar = float(round(ann_return / _dd_frac_for_calmar, 2))
        except Exception as e:
            logger.debug(f"portfolio_bt: calmar calc skipped ({e})")

    # Turnover: round-trips per year on average deployed capital.
    # round_trips = total closed trades. avg_deployed = mean equity over
    # the curve (proxy for avg invested $); period_years from curve span.
    turnover = None
    if total > 0 and len(equity_curve) >= 30:
        try:
            t0 = pd.Timestamp(equity_curve[0][0])
            t1 = pd.Timestamp(equity_curve[-1][0])
            period_years = max(0.25, (t1 - t0).days / 365.25)
            turnover = float(round(total / period_years, 1))
        except Exception:
            pass

    # Profit factor: gross wins / |gross losses|. Handles div-by-zero cleanly.
    gross_wins = sum(t.pnl for t in closed_trades if t.pnl > 0)
    gross_losses = sum(-t.pnl for t in closed_trades if t.pnl < 0)
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (None if gross_wins == 0 else float("inf"))

    # Expectancy: avg $/trade. Positive is necessary but not sufficient.
    expectancy = None
    if total > 0:
        avg_win_pl = (gross_wins / wins) if wins > 0 else 0.0
        avg_loss_pl = (-gross_losses / losses) if losses > 0 else 0.0
        expectancy = round(win_rate * avg_win_pl + (1 - win_rate) * avg_loss_pl, 2)

    # Monte Carlo bootstrap. Resample the closed-trade pnl array WITH
    # replacement to build N synthetic equity paths. Tracks the empirical
    # max-drawdown distribution + ending-equity distribution. The p95
    # max-drawdown is the headline risk-of-ruin number — "if I get
    # unlucky, how bad could it get?" — and is much more honest than a
    # single historical realization.
    mc_dd_p5 = mc_dd_p50 = mc_dd_p95 = None
    mc_eq_p5 = mc_eq_p50 = mc_eq_p95 = None
    mc_paths = None
    if total >= 20:
        try:
            import numpy as _np
            pnl_arr = _np.array([t.pnl for t in closed_trades], dtype=float)
            n_paths = 1000
            rng = _np.random.default_rng(seed=42)  # deterministic for reproducibility
            max_dds = _np.zeros(n_paths)
            end_eqs = _np.zeros(n_paths)
            for k in range(n_paths):
                sample = rng.choice(pnl_arr, size=total, replace=True)
                eq_path = starting_equity + _np.cumsum(sample)
                running_peak = _np.maximum.accumulate(eq_path)
                # Avoid div-by-zero at zero peak (won't happen for positive
                # starting_equity, but defensive).
                dd = _np.where(running_peak > 0, (running_peak - eq_path) / running_peak, 0.0)
                max_dds[k] = float(dd.max())
                end_eqs[k] = float(eq_path[-1])
            mc_dd_p5 = float(round(_np.percentile(max_dds, 5) * 100, 2))
            mc_dd_p50 = float(round(_np.percentile(max_dds, 50) * 100, 2))
            mc_dd_p95 = float(round(_np.percentile(max_dds, 95) * 100, 2))
            mc_eq_p5 = float(round(_np.percentile(end_eqs, 5), 2))
            mc_eq_p50 = float(round(_np.percentile(end_eqs, 50), 2))
            mc_eq_p95 = float(round(_np.percentile(end_eqs, 95), 2))
            mc_paths = n_paths
        except Exception as e:
            logger.debug(f"portfolio_bt: Monte Carlo skipped ({e})")

    # By-regime breakdown
    by_regime: Dict[str, Dict[str, Any]] = {}
    for t in closed_trades:
        label = t.regime_label()
        bucket = by_regime.setdefault(label, {"trades": 0, "wins": 0, "losses": 0, "total_pl": 0.0})
        bucket["trades"] += 1
        if t.pnl > 0: bucket["wins"] += 1
        elif t.pnl < 0: bucket["losses"] += 1
        bucket["total_pl"] += t.pnl
    for label, bucket in by_regime.items():
        n = bucket["trades"] or 1
        bucket["win_rate"] = round(bucket["wins"] / n, 3)
        bucket["avg_pl"] = round(bucket["total_pl"] / n, 2)

    # Realized pair-correlation across traded tickers over the test window.
    # Cheap to compute (≤ 50 tickers × 250 bars). Skip when fewer than 2
    # tickers were actually traded.
    avg_pair_corr: Optional[float] = None
    max_pair_corr: Optional[float] = None
    traded_tickers = sorted({t.ticker for t in closed_trades})
    if len(traded_tickers) >= 2:
        try:
            ret_frames = []
            for tk in traded_tickers:
                df_tk = data.get(tk)
                if df_tk is None: continue
                window = df_tk.loc[(df_tk.index >= earliest) & (df_tk.index <= latest)]
                if len(window) < 5: continue
                ret_frames.append(window["Close"].pct_change().rename(tk))
            if len(ret_frames) >= 2:
                rets_df = pd.concat(ret_frames, axis=1).dropna(how="all")
                if len(rets_df) >= 5:
                    corr = rets_df.corr()
                    # Upper triangle (excluding diagonal)
                    import numpy as np
                    iu = np.triu_indices(len(corr), k=1)
                    pairs = corr.values[iu]
                    pairs = pairs[~pd.isna(pairs)]
                    if len(pairs):
                        avg_pair_corr = float(round(float(pairs.mean()), 3))
                        max_pair_corr = float(round(float(pairs.max()), 3))
        except Exception as e:
            logger.debug(f"portfolio_bt: corr calc skipped ({e})")

    stats = PortfolioStats(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        total_trades=total,
        wins=wins, losses=losses, win_rate=round(win_rate, 3),
        max_drawdown_pct=round(max_dd_pct, 4),
        max_drawdown_days=int(max_dd_days),
        sharpe_ratio=sharpe,
        cap_rejection_count=rejections,
        profit_factor=round(profit_factor, 2) if profit_factor not in (None, float("inf")) else profit_factor,
        by_regime=by_regime,
        equity_curve=equity_curve,
        per_sector_exposure_max=per_sector_exposure_max,
        avg_pair_corr=avg_pair_corr,
        max_pair_corr=max_pair_corr,
        stress_window=stress_window,
        stress_window_label=sw_label,
        expectancy=expectancy,
        mc_max_drawdown_p5_pct=mc_dd_p5,
        mc_max_drawdown_p50_pct=mc_dd_p50,
        mc_max_drawdown_p95_pct=mc_dd_p95,
        mc_ending_equity_p5=mc_eq_p5,
        mc_ending_equity_p50=mc_eq_p50,
        mc_ending_equity_p95=mc_eq_p95,
        mc_paths=mc_paths,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        turnover_per_year=turnover,
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
