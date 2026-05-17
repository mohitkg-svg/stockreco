"""r96 R3: correlation-aware portfolio risk math.

The existing `risk_manager.current_portfolio_heat` is beta-only: it sums
per-position $-at-risk × β(ticker). That treats every open position as
independent — five tech longs into a FOMC shock look like five trades to
the heat calc, but in reality they are ONE bet with 5× notional.

This module adds a correlation-inflation factor:

    σ²_full     Σ wi² σi² + 2 Σ_{i<j} wi wj σi σj ρij
    ───────  =  ─────────────────────────────────────
    σ²_diag                Σ wi² σi²

When all positions are uncorrelated, ratio → 1.0 (heat is honest).
When all positions are perfectly correlated, ratio → (Σ|wi|σi)² / Σ wi²σi²
which scales roughly with the number of positions.

`correlation_inflation_factor(positions)` returns this ratio, bounded
[1.0, MAX_INFLATION]. Callers multiply current_portfolio_heat by it when
cfg.correlation_aware_sizing_enabled is True, so heat above the configured
cap reflects clustered concentration risk, not just dollar sum.

Daily-returns are sourced from data_fetcher (cached). The function is
defensive: any fetch failure or insufficient history → returns 1.0
(no inflation, identical to legacy behavior).
"""
from __future__ import annotations
import logging
from typing import List, Dict, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# Cap to prevent a tiny universe of two highly-correlated names from
# producing a 20× heat inflation that totally blocks all new entries.
MAX_INFLATION = 3.0
# Need at least this many shared trading days for a correlation to mean
# anything; below this we fall back to 1.0.
MIN_OVERLAP_BARS = 30
# Lookback window in trading days.
DEFAULT_LOOKBACK_BARS = 60


def _fetch_daily_returns(ticker: str, lookback_bars: int) -> Optional[List[Tuple[str, float]]]:
    """Return list of (ISO-date, daily-pct-return) tuples for the trailing
    `lookback_bars`. None on any failure / insufficient data."""
    try:
        from services.data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(ticker, "1d")
        if df is None or df.empty or "Close" not in df.columns or len(df) < lookback_bars + 1:
            return None
        tail = df.tail(lookback_bars + 1)
        closes = tail["Close"].astype(float).tolist()
        dates = [str(d) for d in tail.index.tolist()]
        rets: List[Tuple[str, float]] = []
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            if prev <= 0:
                continue
            rets.append((dates[i], (closes[i] - prev) / prev))
        return rets
    except Exception as e:
        logger.debug(f"correlation._fetch_daily_returns({ticker}): {e}")
        return None


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return var ** 0.5


def _pearson(xs: List[float], ys: List[float]) -> float:
    """Pearson correlation. Returns 0 when degenerate (zero variance / no overlap)."""
    n = min(len(xs), len(ys))
    if n < MIN_OVERLAP_BARS:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx, my = _mean(xs), _mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = sum((a - mx) ** 2 for a in xs) ** 0.5
    dy = sum((b - my) ** 2 for b in ys) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    rho = num / (dx * dy)
    # Clamp because numerical drift can push slightly past ±1 on tiny n.
    return max(-1.0, min(1.0, rho))


def correlation_inflation_factor(
    positions: List[Dict],
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
) -> float:
    """Compute the σ²_full / σ²_diag inflation factor for the given positions.

    `positions` is a list of {ticker, dollar_risk} dicts. Dollar-risk is
    the position's $-at-risk (typically |entry - stop| × qty). Sigma is
    approximated by realized daily-return stdev over `lookback_bars`.

    Returns a value in [1.0, MAX_INFLATION]. 1.0 means uncorrelated /
    insufficient data (safe fallback — never inflates above legacy).
    """
    if not positions or len(positions) < 2:
        return 1.0
    by_ticker: Dict[str, float] = {}
    for p in positions:
        tk = (p.get("ticker") or "").upper()
        risk = float(p.get("dollar_risk") or 0.0)
        if not tk or risk <= 0:
            continue
        by_ticker[tk] = by_ticker.get(tk, 0.0) + risk
    tickers = sorted(by_ticker.keys())
    if len(tickers) < 2:
        return 1.0
    # Fetch returns + sigma per ticker.
    returns: Dict[str, Dict[str, float]] = {}
    sigmas: Dict[str, float] = {}
    for tk in tickers:
        rets = _fetch_daily_returns(tk, lookback_bars)
        if not rets:
            return 1.0  # missing data → safe fallback
        returns[tk] = {d: r for d, r in rets}
        sigmas[tk] = _stdev([r for _, r in rets])
        if sigmas[tk] <= 0:
            return 1.0
    # Diagonal portfolio variance: Σ wi² σi²
    diag_var = sum((by_ticker[tk] ** 2) * (sigmas[tk] ** 2) for tk in tickers)
    if diag_var <= 0:
        return 1.0
    # Off-diagonal cross terms: 2 Σ_{i<j} wi wj σi σj ρij
    cross = 0.0
    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            ti, tj = tickers[i], tickers[j]
            shared_dates = sorted(set(returns[ti].keys()) & set(returns[tj].keys()))
            if len(shared_dates) < MIN_OVERLAP_BARS:
                continue
            xs = [returns[ti][d] for d in shared_dates]
            ys = [returns[tj][d] for d in shared_dates]
            rho = _pearson(xs, ys)
            cross += 2.0 * by_ticker[ti] * by_ticker[tj] * sigmas[ti] * sigmas[tj] * rho
    full_var = diag_var + cross
    if full_var <= 0:
        return 1.0
    ratio = full_var / diag_var
    return max(1.0, min(MAX_INFLATION, ratio))


def markowitz_covariance_multiplier(new_ticker: str, db: Any) -> float:
    """
    Calculates the Marginal Contribution to Risk (MCTR) of adding the new ticker
    to the existing portfolio. Uses the Markowitz covariance matrix framework.
    Returns a sizing multiplier:
      Highly correlated (MCTR high) -> multiplier < 1.0
      Uncorrelated    (MCTR low)  -> multiplier = 1.0
      Hedging         (MCTR < 0)  -> multiplier > 1.0
    """
    from database import AutoTrade
    open_trades = db.query(AutoTrade).filter(AutoTrade.status.in_(["pending", "open", "adopted"])).all()
    by_ticker: Dict[str, float] = {}
    for ot in open_trades:
        risk = 0.0
        oe = ot.entry_price or ot.requested_entry or 0.0
        os_ = ot.current_stop or ot.stop_loss or 0.0
        if ot.asset_type == "stock" and oe > 0 and os_ > 0:
            risk = abs(float(oe) - float(os_)) * (ot.qty or 0)
        elif ot.asset_type == "option" and oe > 0:
            risk = float(oe) * 100 * (ot.qty or 0)
        if risk > 0 and ot.ticker:
            by_ticker[ot.ticker.upper()] = by_ticker.get(ot.ticker.upper(), 0.0) + risk

    if not by_ticker:
        return 1.0  # Empty book, no covariance to penalize
        
    tickers = sorted(list(set(by_ticker.keys()) | {new_ticker.upper()}))
    returns = {tk: {d: r for d, r in _fetch_daily_returns(tk, 60) or []} for tk in tickers}
    
    if new_ticker.upper() not in returns or len(returns[new_ticker.upper()]) < 20:
        return 1.0

    new_rets = returns[new_ticker.upper()]
    total_book_risk = sum(by_ticker.values())
    weighted_correlation = 0.0
    
    for tk, risk in by_ticker.items():
        if tk == new_ticker.upper() or tk not in returns:
            continue
        tk_rets = returns[tk]
        shared_dates = sorted(set(new_rets.keys()) & set(tk_rets.keys()))
        if len(shared_dates) < 20:
            continue
        rho = _pearson([new_rets[d] for d in shared_dates], [tk_rets[d] for d in shared_dates])
        weighted_correlation += (risk / total_book_risk) * rho

    # Linear map: corr 0.8 -> 0.6x size, corr 0 -> 1.0x size, corr -0.8 -> 1.4x size
    return float(max(0.5, min(1.5, 1.0 - (weighted_correlation * 0.5))))


def optimize_portfolio_mvo(tickers: List[str], expected_returns: List[float], lookback: int = 252) -> Dict[str, float]:
    """
    Markowitz Mean-Variance Optimization (MVO).
    Computes the Tangency Portfolio weights given empirical covariance
    and ML-predicted expected returns.
    
    W = (Σ⁻¹) μ / (1ᵀ Σ⁻¹ μ)
    """
    import numpy as np
    import pandas as pd
    from sklearn.covariance import LedoitWolf

    if not tickers or len(tickers) != len(expected_returns) or len(tickers) < 2:
        return {tk: 1.0 / max(1, len(tickers)) for tk in tickers}

    returns_dict = {}
    for tk in tickers:
        rets = _fetch_daily_returns(tk, lookback)
        if rets and len(rets) >= lookback * 0.8:
            returns_dict[tk] = {d: r for d, r in rets}

    if not returns_dict:
        return {tk: 1.0 / len(tickers) for tk in tickers}

    df = pd.DataFrame(returns_dict).dropna(axis=1)
    valid_tickers = df.columns.tolist()

    if len(valid_tickers) < 2:
        return {tk: 1.0 / len(valid_tickers) for tk in valid_tickers}

    # QUANT REVISION: Ledoit-Wolf Shrinkage for Covariance Matrix Stabilization
    cov_matrix = LedoitWolf().fit(df).covariance_
    mu = np.array([expected_returns[tickers.index(tk)] for tk in valid_tickers])

    try:
        cov_inv = np.linalg.pinv(cov_matrix)
        w_unnorm = cov_inv @ mu
        w_long = np.maximum(w_unnorm, 0.0) # Long only
        sum_w = np.sum(w_long)
        if sum_w > 0:
            weights = w_long / sum_w
        else:
            weights = np.ones(len(valid_tickers)) / len(valid_tickers)
    except Exception:
        weights = np.ones(len(valid_tickers)) / len(valid_tickers)

    out = {tk: float(w) for tk, w in zip(valid_tickers, weights)}
    for tk in tickers:
        if tk not in out:
            out[tk] = 0.0
    return out
