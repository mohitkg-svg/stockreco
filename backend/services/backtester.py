import pandas as pd
import numpy as np
import os
from typing import Dict, Any, List, Optional
from services.indicators import compute_indicators
from services.strategies import all_strategies
from services.config import STOP_ATR_MULT_BY_TF


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
) -> Dict[str, Any]:
    """Simulate trades given an entry signal series. direction = 'BUY' or 'SELL'.

    Gap-aware: if an unfilled bear-gap sits above the entry (long) or an unfilled
    bull-gap sits below (short), we treat the gap-fill midpoint as a *secondary*
    target and exit at whichever fires first (gap-fill or ATR-target).

    Audit fix C2: stop multiplier sources from STOP_ATR_MULT_BY_TF (same table
    the live signal generator uses) so backtest stats reflect live risk.
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
    entry_idx = 0
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
            if a is None and not pd.isna(_close_val) and float(_close_val) > 0:
                a = max(float(_close_val) * 0.02, 0.01)
            if a is None or a <= 0:
                a = 0.01

        if not in_trade and bool(entries.iloc[i - 1]):
            entry_price = _apply_costs(float(row["Open"]), "entry", direction)
            entry_date = d.index[i]
            entry_idx = i
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
            if direction == "BUY":
                stop = entry_price - stop_mult * a
                target = entry_price + target_mult * a
            else:
                stop = entry_price + stop_mult * a
                target = entry_price - target_mult * a
            in_trade = True

        elif in_trade:
            exit_price = None
            exit_reason = None
            # Gap-target sanity: must sit at least 1 cent past the entry, otherwise
            # any random bar's high/low instantly "fills" it and labels the exit
            # "gap_fill" with effectively-flat P/L.
            _GAP_MIN_DIST = 0.01
            # Audit fix C1/H15: a bar that GAPS THROUGH the stop or target opens
            # past the level — the realistic fill is the open price, not the
            # level itself. The old code silently filled at the level and
            # produced understated losses / overstated gains on gap bars.
            open_price = float(row["Open"])
            if direction == "BUY":
                if open_price <= stop:
                    # Gap-down through stop: fill at open (worse than stop).
                    exit_price, exit_reason = open_price, "stop_gap"
                elif open_price >= target:
                    # Gap-up through target: fill at open (better than target).
                    exit_price, exit_reason = open_price, "target_gap"
                elif float(row["Low"]) <= stop:
                    # Intrabar stop — when a bar touches BOTH stop and target we
                    # can't know which printed first, so we conservatively take
                    # the stop (pessimistic bias, standard backtest convention).
                    exit_price, exit_reason = stop, "stop"
                elif (gap_target and (gap_target - entry_price) >= _GAP_MIN_DIST
                      and float(row["High"]) >= gap_target and gap_target < target):
                    exit_price, exit_reason = gap_target, "gap_fill"
                elif float(row["High"]) >= target:
                    exit_price, exit_reason = target, "target"
            else:
                if open_price >= stop:
                    exit_price, exit_reason = open_price, "stop_gap"
                elif open_price <= target:
                    exit_price, exit_reason = open_price, "target_gap"
                elif float(row["High"]) >= stop:
                    exit_price, exit_reason = stop, "stop"
                elif (gap_target and (entry_price - gap_target) >= _GAP_MIN_DIST
                      and float(row["Low"]) <= gap_target and gap_target > target):
                    exit_price, exit_reason = gap_target, "gap_fill"
                elif float(row["Low"]) <= target:
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

        equity.append({"time": ts, "value": round(portfolio, 2)})

    return _build_stats(trades, equity, portfolio, timeframe=timeframe)


def _build_stats(trades: List[dict], equity: List[dict], final_portfolio: float, timeframe: Optional[str] = None) -> Dict[str, Any]:
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
