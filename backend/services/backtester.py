"""Per-ticker historical backtest engine.

Runs every strategy from `services.strategies.all_strategies()` against
a single ticker's OHLCV history (typically 2y of daily bars), simulating
trades with realistic costs (commission + slippage) and the same exit
state machine the live engine uses (partial trims at T1/T2, runner at
target, soft-BE / BE stop tightening).

Two consumers:
  * `routers/backtest.py` — exposes per-strategy stats to the UI for
    "which strategy works best for this ticker?".
  * `services/signal_generator.py` (via `_apply_backtest_to_signal`) —
    blends a backtest "best-strategy" score into the live signal's
    confidence multiplier.

Surface:
  * `run_backtest(df, signal_type, timeframe)` — single signal_type's stats
  * `run_multi_strategy(df, timeframe)` — all strategies, with walk-forward
    folds + composite confidence
  * `score_strategy(stats)` — collapse a stats dict into a 0-100 number

Realism additions (since r37):
  * Partial-exit ladder (T1=50% of distance to target, T2=85%) matching
    the live engine. Toggle via `partial_exits=False` for legacy single-
    exit comparison.
  * Per-side cost haircut via `BT_COMMISSION_BPS` + `BT_SLIPPAGE_BPS`.
  * Gap-aware exits (open price used as fill when a bar gaps through
    stop or target).
  * Gap-fill targeting (unfilled bear/bull gaps act as secondary targets).
  * Sharpe annualization sourced per timeframe (r34) — `_BARS_PER_YEAR`.
  * Liquidity gate (r39) at `run_multi_strategy` entry — skips backtests
    on tickers with median 20-bar $-volume < $10M.

NOT in this module (deliberate scope split):
  * Multi-asset / cross-ticker correlation simulation lives in
    `services/portfolio_backtest.py`.
  * Live trade execution / partial-fill bookkeeping lives in
    `services/auto_trader.py` + `services/execution_engine.py`.
"""
import logging
import pandas as pd
import numpy as np
import os
from typing import Dict, Any, List, Optional
from services.indicators import compute_indicators
from services.strategies import all_strategies
from services.config import STOP_ATR_MULT_BY_TF

logger = logging.getLogger(__name__)


# Trading cost model — applied as a per-side haircut to entry/exit prices so
# every trade pays a realistic spread+commission round-trip drag. These default
# to retail-broker norms; override via env to tune for your venue.
COMMISSION_BPS = float(os.getenv("BT_COMMISSION_BPS", "1.0"))   # 0.01% per side (most brokers free now)
SLIPPAGE_BPS   = float(os.getenv("BT_SLIPPAGE_BPS",   "5.0"))   # 0.05% per side (typical retail spread)
COST_PER_SIDE  = (COMMISSION_BPS + SLIPPAGE_BPS) / 10000.0      # → 0.06% per side, 0.12% round-trip

# Audit fix C2: the backtester used to hardcode stop=1.5×ATR / target=2.5×ATR
# regardless of timeframe, while the live signal_generator uses timeframe-
# calibrated multipliers from STOP_ATR_MULT_BY_TF. Backtest stats therefore
# understated risk on short TFs (where stops need to be wider for volatility)
# and overstated R:R on long TFs. We now source the stop mult from the same
# config table and scale the target to preserve the original 1.67 R:R ratio.
DEFAULT_STOP_ATR_MULT = 1.5
DEFAULT_RR = 2.5 / 1.5  # target:risk ratio (preserved when stop mult changes)


# Bars-per-year by timeframe for Sharpe annualization. Equity in `_simulate`
# is appended per bar, so per-bar pct_change → bar-frequency stdev. Multiplying
# by sqrt(252) (the daily factor) blindly was the bug — a 5m strategy got
# annualized as if it had 252 returns/year when it actually had ~19,656,
# which UNDERSTATES the proper sqrt-N factor by 8.8× and produced inflated
# Sharpe numbers on intraday TFs. Trading-day basis: 6.5h × 60 = 390 min/day.
_BARS_PER_YEAR = {
    "1m": 390 * 252,
    "5m": 78 * 252,
    "15m": 26 * 252,
    "30m": 13 * 252,
    "1h": int(6.5 * 252),
    "4h": int(1.625 * 252),  # ~2 bars/day rounded for cash session
    "1d": 252,
    "1wk": 52,
    "1mo": 12,
}


def _annualization_factor(timeframe: Optional[str]) -> float:
    """Return `sqrt(bars_per_year)` for Sharpe annualization.

    Equity curve in `_simulate` is per-bar (one entry per OHLCV row),
    so per-bar pct_change → bar-frequency stdev. Multiplying by the
    daily `sqrt(252)` blindly inflated intraday Sharpe by 8.8× on 5m.
    Falls back to `sqrt(252)` if the timeframe isn't in `_BARS_PER_YEAR`.
    """
    n = _BARS_PER_YEAR.get(timeframe, 252)
    return float(n ** 0.5)


def _apply_costs(price: float, side: str, direction: str) -> float:
    """
    Worsen the price by COST_PER_SIDE in the direction that hurts the trader.
      • BUY  entry  → +cost  (you pay slightly more)
      • BUY  exit   → -cost  (you receive slightly less)
      • SELL entry → -cost  (short fills slightly lower)
      • SELL exit  → +cost  (you cover slightly higher)
    """
    if direction == "BUY":
        return price * (1 + COST_PER_SIDE) if side == "entry" else price * (1 - COST_PER_SIDE)
    else:
        return price * (1 - COST_PER_SIDE) if side == "entry" else price * (1 + COST_PER_SIDE)


def _simulate(
    d: pd.DataFrame,
    entries: pd.Series,
    direction: str,
    atr: pd.Series,
    timeframe: Optional[str] = None,
    partial_exits: bool = True,
) -> Dict[str, Any]:
    """Simulate trades given an entry signal series.

    Args:
        d: OHLCV frame with `Open / High / Low / Close / Volume` columns,
           indexed by timestamp. Indicators may be present but aren't
           required by `_simulate` itself (caller supplies `atr`).
        entries: boolean Series aligned to `d.index`. `entries.iloc[i-1] = True`
           triggers a market-open entry on bar `i`.
        direction: 'BUY' (longs) or 'SELL' (shorts).
        atr: ATR series aligned to `d.index`. Used to compute stop and target
           distances per bar (with a fallback chain when the value is missing).
        timeframe: optional, used for `STOP_ATR_MULT_BY_TF` lookup and
           Sharpe annualization. Falls back to `DEFAULT_STOP_ATR_MULT=1.5`.
        partial_exits: when True (default, r37), simulates the LIVE
           auto_trader state machine — 33% banked at T1 (50% of distance
           to target), another 33% at T2 (85%), runner exits at the full
           ATR target. Stop tightens to soft-BE at T1, full BE at T2.
           Closes the "Ghost Alpha" backtest-vs-live divergence. Set False
           for legacy all-in/all-out behavior.

    Returns:
        `{trades, equity_curve, stats}` — trade list with entry/exit/PnL,
        per-bar equity values, and aggregate stats from `_build_stats`.

    Behavior worth knowing:
        * Gap-aware: a bar that opens past the stop or target fills at the
          OPEN price, not the level (realistic worst-case slippage on gaps).
        * Conservative bias: when a bar's range covers BOTH stop and target,
          the stop is taken (we can't know intra-bar print order).
        * Unfilled bear/bull gaps act as secondary targets — exit at
          whichever fires first.
        * Costs (`COMMISSION_BPS + SLIPPAGE_BPS`) are applied to entry AND
          exit price in the direction that hurts the trader.
        * `partial_pl_dollars` accumulates each leg's contribution; portfolio
          updates use a snapshot taken at trade open (r39 fix #25 — prevents
          geometric inflation when T1+T2+target hit in the same bar).

    `entry_idx`/`hist` lookups use `df.iloc[:i+1]` to enforce no look-ahead
    on the gap-target computation (the only place future bars could leak in).
    """
    from services.gap_detector import gap_targets_above as _gta, gap_targets_below as _gtb

    stop_mult = STOP_ATR_MULT_BY_TF.get(timeframe, DEFAULT_STOP_ATR_MULT) if timeframe else DEFAULT_STOP_ATR_MULT
    target_mult = stop_mult * DEFAULT_RR  # preserve 1.67 R:R

    trades: List[dict] = []
    portfolio = 10000.0
    equity = [{"time": int(d.index[0].timestamp()), "value": round(portfolio, 2)}]
    in_trade = False
    entry_price = 0.0
    entry_date = None
    stop = 0.0
    target = 0.0
    gap_target: Optional[float] = None  # nearest unfilled gap-fill, computed at entry

    n = len(d)
    for i in range(1, n):
        row = d.iloc[i]
        ts = int(d.index[i].timestamp())
        # ATR fallback chain: real ATR → recent realized stdev × √2 (proxy for
        # daily range) → 2% of Close → hard floor 0.01. The stdev fallback
        # uses the trailing 14 bars of high-low ranges so a low-vol name (utility)
        # gets a tighter fallback and a high-vol name (small-cap biotech) gets
        # a wider one — the old hardcoded 2% misclassified both. Without the
        # Close-NaN guard, NaN-poisoned ATR propagates into stop/target math
        # and silently corrupts the equity curve.
        _atr_val = atr.iloc[i]
        _close_val = row["Close"]
        if not pd.isna(_atr_val) and float(_atr_val) > 0:
            a = float(_atr_val)
        else:
            # Fallback chain when ATR is missing/zero. Best → worst:
            #   1. Trailing 14-bar median High–Low range (adapts to actual
            #      realized volatility for THIS symbol+TF).
            #   2. Stdev of trailing 14 closes — captures direction-only
            #      moves the median-range can miss.
            #   3. Flat 2% of Close — wrong for low-vol utilities AND
            #      high-beta growth, but never zero.
            #   4. Hard floor 0.01 so stop/target math never blows up.
            a = None
            if i >= 14:
                try:
                    win = d.iloc[max(0, i - 14):i]
                    rng = (win["High"] - win["Low"]).dropna()
                    if len(rng) >= 5:
                        med_rng = float(rng.median())
                        if med_rng > 0:
                            a = med_rng
                except Exception:
                    a = None
            if a is None and i >= 14:
                try:
                    sd = float(d["Close"].iloc[i - 14:i].std())
                    if not pd.isna(sd) and sd > 0:
                        a = sd
                except Exception:
                    pass
            if a is None and not pd.isna(_close_val) and float(_close_val) > 0:
                a = float(_close_val) * 0.02
            if a is None or a <= 0:
                a = 0.01
            a = max(a, 0.01)

        if not in_trade and bool(entries.iloc[i - 1]):
            entry_price = _apply_costs(float(row["Open"]), "entry", direction)
            entry_date = d.index[i]
            # r39 audit cleanup: removed unused `entry_idx = i`.
            # Compute gap-fill levels from history visible at entry (no look-ahead)
            hist = d.iloc[:i + 1]
            try:
                if direction == "BUY":
                    fills_above = _gta(hist, entry_price)
                    gap_target = fills_above[0] if fills_above else None
                else:
                    fills_below = _gtb(hist, entry_price)
                    gap_target = fills_below[0] if fills_below else None
            except Exception:
                gap_target = None
            r = stop_mult * a   # risk distance in $
            if direction == "BUY":
                stop = entry_price - r
                target = entry_price + target_mult * a
                # Partial-exit ladder: T1 at 50% of distance to final
                # target, T2 at 85%. This keeps T1 < T2 < target regardless
                # of the configured R:R (live engine uses S/R-based pivots
                # which don't always fall at integer R-multiples; this is
                # a 1:1-with-distance approximation that captures the
                # banking cadence without needing S/R data).
                t1_px = entry_price + 0.5 * (target - entry_price)
                t2_px = entry_price + 0.85 * (target - entry_price)
                soft_be = entry_price - 0.3 * r
            else:
                stop = entry_price + r
                target = entry_price - target_mult * a
                t1_px = entry_price - 0.5 * (entry_price - target)
                t2_px = entry_price - 0.85 * (entry_price - target)
                soft_be = entry_price + 0.3 * r
            # Per-trade state for partial-exit simulation. Live engine trims
            # 33% at T1, 33% of original at T2, runner at target. Stop
            # tightens to soft-BE then BE as targets hit.
            frac_remaining = 1.0
            hit_t1 = False
            hit_t2 = False
            partial_pl_dollars = 0.0     # accumulated $-PnL per dollar of original exposure
            # r39 audit fix #25: snapshot portfolio at trade open. Each
            # partial leg's portfolio update applies `contrib × portfolio_at_open`
            # rather than `contrib × current_portfolio` — so a multi-leg
            # winner doesn't inflate geometrically across legs.
            portfolio_at_trade_open = portfolio
            in_trade = True

        elif in_trade:
            # Gap-target sanity: must sit at least 1 cent past the entry, otherwise
            # any random bar's high/low instantly "fills" it and labels the exit
            # "gap_fill" with effectively-flat P/L.
            _GAP_MIN_DIST = 0.01
            open_price = float(row["Open"])
            hi = float(row["High"])
            lo = float(row["Low"])

            if not partial_exits:
                # Legacy single-exit path. Kept for sanity comparison.
                exit_price = None
                exit_reason = None
                if direction == "BUY":
                    if open_price <= stop:
                        exit_price, exit_reason = open_price, "stop_gap"
                    elif open_price >= target:
                        exit_price, exit_reason = open_price, "target_gap"
                    elif lo <= stop:
                        exit_price, exit_reason = stop, "stop"
                    elif (gap_target and (gap_target - entry_price) >= _GAP_MIN_DIST
                          and hi >= gap_target and gap_target < target):
                        exit_price, exit_reason = gap_target, "gap_fill"
                    elif hi >= target:
                        exit_price, exit_reason = target, "target"
                else:
                    if open_price >= stop:
                        exit_price, exit_reason = open_price, "stop_gap"
                    elif open_price <= target:
                        exit_price, exit_reason = open_price, "target_gap"
                    elif hi >= stop:
                        exit_price, exit_reason = stop, "stop"
                    elif (gap_target and (entry_price - gap_target) >= _GAP_MIN_DIST
                          and lo <= gap_target and gap_target > target):
                        exit_price, exit_reason = gap_target, "gap_fill"
                    elif lo <= target:
                        exit_price, exit_reason = target, "target"
                if exit_price is not None:
                    exit_price = _apply_costs(exit_price, "exit", direction)
                    pnl_pct = ((exit_price - entry_price) / entry_price) if direction == "BUY" \
                              else ((entry_price - exit_price) / entry_price)
                    portfolio += portfolio * pnl_pct
                    trades.append({
                        "entry_date": str(entry_date.date()),
                        "exit_date": str(d.index[i].date()),
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "pnl_pct": round(pnl_pct * 100, 2),
                        "exit_reason": exit_reason,
                        "type": direction,
                    })
                    in_trade = False
                    gap_target = None
            else:
                # Partial-exit path matching the live state machine.
                #
                # Order of evaluation within a single bar (pessimistic bias):
                #   1. Gap-through stop  → flatten remainder at open
                #   2. Gap-through final target → flatten remainder at open
                #   3. Intrabar stop  → flatten remainder at stop
                #   4. T1 hit (if !hit_t1, BUY: hi>=t1) → bank 33%, stop→soft-BE
                #   5. T2 hit (if !hit_t2, BUY: hi>=t2) → bank 33% of original, stop→entry
                #   6. Final target → flatten remainder at target
                # We re-process the same bar for T1→T2→target so a strong
                # bar that crosses multiple levels banks profit at each.
                #
                # `pnl_for_exit(px, frac)` returns the contribution to total
                # PnL-per-dollar-of-original-exposure for selling `frac` of
                # the *original* position at px.
                def _pnl_per_unit(px: float, frac: float) -> float:
                    px_net = _apply_costs(px, "exit", direction)
                    if direction == "BUY":
                        return ((px_net - entry_price) / entry_price) * frac
                    return ((entry_price - px_net) / entry_price) * frac

                bar_remainder_exit = False  # set when the runner is fully closed this bar

                def _flush(px: float, reason: str) -> None:
                    """Close all remaining size at px with the given reason."""
                    nonlocal frac_remaining, bar_remainder_exit, partial_pl_dollars, portfolio
                    if frac_remaining <= 0:
                        return
                    contrib = _pnl_per_unit(px, frac_remaining)
                    partial_pl_dollars += contrib
                    # r39 audit fix #25: use snapshot, not current portfolio
                    portfolio += portfolio_at_trade_open * contrib
                    trades.append({
                        "entry_date": str(entry_date.date()),
                        "exit_date": str(d.index[i].date()),
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(_apply_costs(px, "exit", direction), 2),
                        "pnl_pct": round(partial_pl_dollars * 100, 2),
                        "exit_reason": reason,
                        "type": direction,
                    })
                    frac_remaining = 0.0
                    bar_remainder_exit = True

                # 1) Gap-through stop
                if direction == "BUY" and open_price <= stop:
                    _flush(open_price, "stop_gap")
                elif direction == "SELL" and open_price >= stop:
                    _flush(open_price, "stop_gap")
                # 2) Gap-through final target (rare but real on opens)
                if not bar_remainder_exit:
                    if direction == "BUY" and open_price >= target:
                        _flush(open_price, "target_gap")
                    elif direction == "SELL" and open_price <= target:
                        _flush(open_price, "target_gap")
                # 3) Intrabar stop
                if not bar_remainder_exit:
                    if direction == "BUY" and lo <= stop:
                        _flush(stop, "stop")
                    elif direction == "SELL" and hi >= stop:
                        _flush(stop, "stop")
                # 4) T1 partial — bank 33%
                # r39 audit fix #25: previously each partial leg used
                # `portfolio += portfolio * contrib` (compounding off
                # current portfolio). When T1+T2+target hit in the same
                # bar, T2's contrib applied to a portfolio inflated by
                # T1 — geometric-vs-arithmetic mismatch overstating gains.
                # Now: each leg's contrib applies to `portfolio_at_trade_open`
                # so all three legs compose linearly off the same base.
                if not bar_remainder_exit and not hit_t1:
                    t1_hit = (direction == "BUY" and hi >= t1_px) or \
                             (direction == "SELL" and lo <= t1_px)
                    if t1_hit:
                        contrib = _pnl_per_unit(t1_px, 0.33)
                        partial_pl_dollars += contrib
                        portfolio += portfolio_at_trade_open * contrib
                        frac_remaining -= 0.33
                        hit_t1 = True
                        # Tighten stop to soft-BE.
                        stop = soft_be if direction == "BUY" else soft_be
                # 5) T2 partial — bank another 33%-of-original
                if not bar_remainder_exit and hit_t1 and not hit_t2:
                    t2_hit = (direction == "BUY" and hi >= t2_px) or \
                             (direction == "SELL" and lo <= t2_px)
                    if t2_hit:
                        contrib = _pnl_per_unit(t2_px, 0.33)
                        partial_pl_dollars += contrib
                        portfolio += portfolio_at_trade_open * contrib
                        frac_remaining -= 0.33
                        hit_t2 = True
                        # Tighten stop to entry (full BE).
                        stop = entry_price
                # 6) Runner exit at final target
                if not bar_remainder_exit and frac_remaining > 0:
                    final_hit = (direction == "BUY" and hi >= target) or \
                                (direction == "SELL" and lo <= target)
                    if final_hit:
                        _flush(target, "target")
                # If the runner is still alive, the trade carries to the next bar.
                if bar_remainder_exit:
                    in_trade = False
                    gap_target = None
                    hit_t1 = hit_t2 = False
                    frac_remaining = 0.0
                    partial_pl_dollars = 0.0

        equity.append({"time": ts, "value": round(portfolio, 2)})

    return _build_stats(trades, equity, portfolio, timeframe=timeframe)


def _build_stats(trades: List[dict], equity: List[dict], final_portfolio: float, timeframe: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate per-trade outcomes into the stats dict consumed by the UI
    and `score_strategy`. Defines the response shape — downstream callers
    grep for these keys.

    Returned shape:
        {
          stats: {
            total_trades, win_rate, profit_factor, total_return_pct,
            max_drawdown_pct, sharpe_ratio, avg_win_pct, avg_loss_pct
          },
          equity_curve: [{time, value}, ...],   # downsampled to ~300 points
          trades: [{entry_date, exit_date, ...}, ...],   # last 50
        }

    Empty-trade case returns `_empty_stats()` (all zeros) so downstream
    consumers don't need to None-guard each field.

    Sharpe uses the timeframe-aware `_annualization_factor` (r34 fix —
    intraday Sharpes were 8.8× inflated by blind `sqrt(252)`).
    Profit factor caps at 99.9 to avoid `inf` JSON-serialization issues.
    """
    if not trades:
        return {
            "stats": _empty_stats(),
            "equity_curve": equity[::max(1, len(equity) // 300)] if equity else [],
            "trades": [],
        }

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) * 100
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else (float("inf") if wins else 0)
    total_return = (final_portfolio - 10000) / 10000 * 100

    eq_values = [e["value"] for e in equity]
    peak = np.maximum.accumulate(eq_values)
    drawdown = (np.array(eq_values) - peak) / peak * 100
    max_dd = float(np.min(drawdown)) if len(drawdown) else 0.0

    bar_ret = pd.Series(eq_values).pct_change().dropna()
    ann_factor = _annualization_factor(timeframe)
    sharpe = float(bar_ret.mean() / bar_ret.std() * ann_factor) if bar_ret.std() > 0 else 0.0

    return {
        "stats": {
            "total_trades": len(trades),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(min(profit_factor, 99.9), 2),
            "total_return_pct": round(total_return, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
        },
        "equity_curve": equity[::max(1, len(equity) // 300)],
        "trades": trades[-50:],
    }


def _empty_stats() -> dict:
    return {
        "total_trades": 0, "win_rate": 0, "profit_factor": 0,
        "total_return_pct": 0, "max_drawdown_pct": 0, "sharpe_ratio": 0,
        "avg_win_pct": 0, "avg_loss_pct": 0,
    }


def score_strategy(stats: dict) -> float:
    """
    Combine stats into a 0-100 confidence score measuring how well the strategy
    has worked historically on this stock. Strategies with no trades score 0.
    """
    if stats["total_trades"] < 3:
        return 0.0

    win_rate = stats["win_rate"]                      # 0-100
    pf = min(stats["profit_factor"], 5.0)             # cap at 5
    total_ret = stats["total_return_pct"]
    sharpe = stats["sharpe_ratio"]
    max_dd = abs(stats["max_drawdown_pct"])

    # Normalize components to 0-100
    win_score = win_rate                                        # already 0-100
    pf_score = min(pf / 3.0, 1.0) * 100                         # pf=3 → 100
    ret_score = max(0.0, min(total_ret / 50.0, 1.0)) * 100      # 50%+ return → 100
    sharpe_score = max(0.0, min((sharpe + 0.5) / 2.0, 1.0)) * 100  # sharpe 1.5 → 100
    dd_score = max(0.0, 100 - max_dd)                           # penalize drawdown

    # Weighted average
    score = (
        win_score * 0.25 +
        pf_score * 0.25 +
        ret_score * 0.20 +
        sharpe_score * 0.20 +
        dd_score * 0.10
    )
    # Heavy penalty for losing strategies
    if total_ret < 0:
        score *= 0.5
    return round(score, 1)


def run_multi_strategy(df: pd.DataFrame, timeframe: Optional[str] = None) -> Dict[str, Any]:
    """
    Ground-up Tier 2: Walk-forward evaluation.

    Split the series into 4 folds. For each fold, train on the prior
    history, test on the fold's window. Average the fold results into a
    "walk-forward confidence" that's much harder to game with overfitting
    than a single 80/20 split.

    Also reports the full-period result for UI display (equity curve, etc.)
    and an aggregate OOS confidence (mean of fold OOS scores).
    """
    if df.empty or len(df) < 120:
        return {"results": [], "best": None}

    d = compute_indicators(df.copy())
    d = d.dropna(subset=["SMA_50"]).copy()
    if len(d) < 60:
        return {"results": [], "best": None}

    # Sanity-filter malformed bars before simulating. Yahoo / Alpaca occasionally
    # emit zero-volume halt prints, or High<Low artifacts from corporate-action
    # adjustments. Both produce nonsense fills. Drop them quietly.
    if "High" in d.columns and "Low" in d.columns:
        bad_hl = d["High"] < d["Low"]
        if bad_hl.any():
            d = d[~bad_hl].copy()
    if "Volume" in d.columns:
        bad_vol = d["Volume"].fillna(0) <= 0
        if bad_vol.any():
            d = d[~bad_vol].copy()
    if len(d) < 60:
        return {"results": [], "best": None}

    # Liquidity Gate: Match the $10M median daily volume gate in auto_trader
    # so backtest results aren't inflated by spread-driven micro-cap fills
    # the live bot would never take. Tail 20 bars only — a name might have
    # been illiquid years ago but tradeable now (or vice-versa). NaN means
    # too few bars to assess; we don't reject on NaN (let other gates run).
    try:
        typ_px = (d["High"] + d["Low"] + d["Close"]) / 3.0
        dvol_tail = (typ_px * d["Volume"]).tail(20)
        med_dvol = float(dvol_tail.median())
        if not pd.isna(med_dvol) and 0 < med_dvol < 10_000_000:
            logger.info(f"Backtest skipped: fails $10M liquidity gate (median ${med_dvol/1e6:.1f}M)")
            return {"results": [], "best": None}
    except Exception as _e:
        logger.debug(f"backtester: liquidity gate skipped ({_e})")

    atr_col = next((c for c in d.columns if c.startswith("ATR_")), None)

    def _evaluate(frame: pd.DataFrame):
        """Run all strategies on `frame`. Returns dict keyed by (strategy, direction)."""
        if atr_col:
            a = frame[atr_col]
        else:
            # Better fallback than hardcoded 2%: 14-bar rolling High-Low range
            # (median over the trailing window). Adapts to actual realized
            # range for the symbol+TF rather than a flat assumption.
            rng = (frame["High"] - frame["Low"]).rolling(14, min_periods=5).median()
            close_2pct = frame["Close"] * 0.02
            a = rng.where(rng > 0, close_2pct).fillna(close_2pct)
        out = {}
        for strat in all_strategies(frame):
            for direction, series in [("BUY", strat["entry_long"]), ("SELL", strat["entry_short"])]:
                sim = _simulate(frame, series, direction, a, timeframe=timeframe)
                out[(strat["name"], direction)] = {
                    "strategy": strat["name"],
                    "description": strat["description"],
                    "direction": direction,
                    "stats": sim["stats"],
                    "equity_curve": sim["equity_curve"],
                    "trades": sim["trades"],
                }
        return out

    # Full-period run (for UI charts).
    full_results = _evaluate(d)

    # Walk-forward: 4 folds on the last half of the series. Each fold tests
    # a quarter-of-half = 1/8 of total bars. Earlier halves are treated as
    # training context (indicators are already computed on full history).
    n = len(d)
    wf_start = n // 2   # backtest on last half only
    fold_width = (n - wf_start) // 4
    wf_fold_conf: Dict[tuple, list] = {}  # key -> list of per-fold confidence

    if fold_width >= 20:
        for fold_i in range(4):
            s = wf_start + fold_i * fold_width
            e = s + fold_width
            fold_df = d.iloc[s:e].copy()
            if len(fold_df) < 20:
                continue
            fold_results = _evaluate(fold_df)
            for key, r in fold_results.items():
                if r["stats"].get("total_trades", 0) >= 2:
                    wf_fold_conf.setdefault(key, []).append(score_strategy(r["stats"]))
    # Aggregate walk-forward confidence — mean of fold scores, conservative
    # if too few folds produced trades.
    oos_results = {}
    for key, confs in wf_fold_conf.items():
        if len(confs) >= 2:
            # Mean across folds — folds that didn't produce trades are
            # implicitly penalized by being excluded from the numerator but
            # NOT the denominator here; use len(confs) so 2-of-4 folds with
            # 60 score each yields 60, not 30. The "robustness-of-folds"
            # nuance is carried as len(confs) in oos_trades.
            oos_results[key] = {
                "stats": {"total_trades": sum(
                    full_results[key]["stats"].get("total_trades", 0) // 4
                    for _ in range(1)
                )},
            }
            # Stash averaged confidence into a synthetic stats-equivalent
            oos_results[key]["_wf_confidence"] = round(sum(confs) / len(confs), 2)
            oos_results[key]["_wf_fold_count"] = len(confs)

    results = []
    for key, r in full_results.items():
        full_conf = score_strategy(r["stats"])
        oos_row = oos_results.get(key)
        if oos_row and oos_row.get("_wf_confidence") is not None:
            wf_conf = float(oos_row["_wf_confidence"])
            fold_count = int(oos_row.get("_wf_fold_count", 0) or 0)
            # Critical-audit fix #2: scale WF confidence by fold_count/4 so
            # a strategy that only produced qualifying trades in 2 of 4 folds
            # is demoted proportionally, not just via the flat 0.90 below.
            # Example: 70 confidence on 2/4 folds → scaled to 35 (vs 63 before),
            # properly demoting brittle regime-specific strategies.
            wf_conf_scaled = wf_conf * (max(0, fold_count) / 4.0)
            adj_conf = 0.65 * wf_conf_scaled + 0.35 * full_conf
            robustness = round(min(100, wf_conf_scaled), 1)
        else:
            adj_conf = full_conf * 0.80   # stricter than old 0.85 given no WF
            robustness = None
        r["confidence"] = round(adj_conf, 1)
        r["confidence_full"] = round(full_conf, 1)
        r["oos_confidence"] = robustness
        r["oos_trades"] = (oos_row.get("_wf_fold_count", 0) if oos_row else 0)
        results.append(r)

    # Sort by adjusted confidence; drop strategies with 0 full-period trades.
    results = [r for r in results if r["stats"]["total_trades"] > 0]
    results.sort(key=lambda r: r["confidence"], reverse=True)
    best = results[0] if results else None
    return {"results": results, "best": best}


def run_backtest(df: pd.DataFrame, signal_type: str = "BUY", timeframe: Optional[str] = None) -> Dict[str, Any]:
    """Backwards-compatible single-strategy backtest (trend-following only)."""
    multi = run_multi_strategy(df, timeframe=timeframe)
    for r in multi["results"]:
        if r["strategy"] == "Trend Following" and r["direction"] == signal_type:
            return {"stats": r["stats"], "equity_curve": r["equity_curve"], "trades": r["trades"]}
    if multi["results"]:
        r = multi["results"][0]
        return {"stats": r["stats"], "equity_curve": r["equity_curve"], "trades": r["trades"]}
    return {"stats": _empty_stats(), "equity_curve": [], "trades": []}
