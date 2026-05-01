"""r58 Transparency: human-friendly definitions for every check the
scanner and auto-trader can apply. Surfaced via /api/trading/auto/check-definitions
and rendered as tooltips in the Transparency UI panels.

Two dicts:
  • SCANNER_CHECKS — reasons returned by scanner.score_candidate / run_scan
  • TRADER_CHECKS  — `metrics.inc("autotrade_skip", reason=X)` reasons + events

Each entry has:
  - title: short human-readable label
  - what:  what the check does
  - why:   why it exists (the failure mode it prevents)
  - fix:   what the operator can flip to relax/tighten if the check is binding
"""
from __future__ import annotations
from typing import Any, Dict


SCANNER_CHECKS: Dict[str, Dict[str, str]] = {
    "no_ohlcv_data": {
        "title": "No price data",
        "what": "The data fetcher returned an empty or missing dataframe for this ticker.",
        "why": "Without OHLCV bars we can't compute any of the scoring features. Likely cause: vendor outage, delisted ticker, or symbol misspelling.",
        "fix": "Usually self-corrects on next scan when the data feed recovers.",
    },
    "insufficient_history": {
        "title": "Not enough history",
        "what": "Fewer than 65 daily bars exist (we want ~3 months minimum to compute SMA50, RVOL, RS-vs-SPY).",
        "why": "Recently-IPO'd names produce noisy features that can't be normalized against peers; we exclude them rather than rank them on partial data.",
        "fix": "Wait for the ticker to accumulate more history. Operator can lower the 65-bar floor in scanner.py if you want very-recent IPOs.",
    },
    "price_below_floor": {
        "title": "Price below $10 floor",
        "what": "Last close is below the $10 minimum.",
        "why": "Sub-$10 names have proportionally larger bid-ask spread relative to their price (a $0.05 spread is 1% slippage on a $5 stock vs 0.25% on $20). The cumulative slippage drag wipes out edge over many trades.",
        "fix": "Edit `PREFILTER_MIN_PRICE` in scanner.py if you want to trade penny stocks.",
    },
    "price_above_ceiling": {
        "title": "Price above $2000 ceiling",
        "what": "Last close is above the $2000 maximum.",
        "why": "Very-high-priced names (BRK.A) trade in tiny share quantities; risk-per-share rounding errors dominate.",
        "fix": "Edit `PREFILTER_MAX_PRICE` if you want to trade BRK.A class.",
    },
    "nan_features": {
        "title": "NaN data",
        "what": "Mean volume or price came back NaN/inf — corrupted or missing bars in the rolling window.",
        "why": "NaN propagates through the score and produces meaningless rankings. Fail-closed: skip the ticker entirely.",
        "fix": "Usually self-corrects on next scan when the data feed cleans up.",
    },
    "non_positive_features": {
        "title": "Zero/negative volume or price",
        "what": "20-day mean volume or price came back ≤ 0 — vendor data corruption.",
        "why": "Division by zero in RVOL / dollar-volume. Skip rather than crash.",
        "fix": "Self-corrects on next scan.",
    },
    "below_dollar_volume": {
        "title": "Below $10M ADV",
        "what": "20-day average daily dollar volume < $10M.",
        "why": "Below this threshold, our 1-2% position sizes start moving the price on entry/exit. The slippage exceeds the edge.",
        "fix": "Edit `PREFILTER_MIN_DOLLAR_VOL` in scanner.py to lower the liquidity floor.",
    },
    "earnings_window": {
        "title": "Earnings within 48h",
        "what": "The ticker has an earnings announcement within the next 48 hours.",
        "why": "Earnings prints produce 5-15% gaps that ignore technical setups. Holding through the print is essentially a binary coin-flip on guidance.",
        "fix": "Excluded automatically; revisit the ticker after earnings.",
    },
    "below_top_quintile": {
        "title": "Below top 20% of universe",
        "what": "Ticker scored, but its score fell below the 80th-percentile threshold of the universe today.",
        "why": "On quiet market days, even a 'good' ticker may not be among the top 20% — we'd rather hold cash than force trades on mediocre setups.",
        "fix": "On a screening basis only; you can broaden the top_n via cfg.universe_top_n if you want more candidates.",
    },
    "below_top_n": {
        "title": "Ranked below top-N cutoff",
        "what": "Ticker passed the top-quintile threshold but ranked below cfg.universe_top_n in the final cut.",
        "why": "Bounded pool — we only persist the top-N to candidate_pool to keep the per-ticker analysis loop tractable.",
        "fix": "Raise cfg.universe_top_n in the Advanced Gates UI to admit more tickers.",
    },
    "scan_timeout": {
        "title": "Scan timed out",
        "what": "Scoring this ticker took longer than the 240-second scan deadline (slow vendor response, etc.).",
        "why": "We cap each scan at 4 minutes to prevent a stuck request from wedging across cron ticks.",
        "fix": "Self-corrects on next scan when the vendor recovers.",
    },
    "scoring_error": {
        "title": "Scoring error",
        "what": "An unexpected exception occurred while computing the score for this ticker.",
        "why": "Defensive — surface the failure rather than silently dropping the ticker.",
        "fix": "Check the logs for the underlying error message.",
    },
    "future_exception": {
        "title": "Worker crashed",
        "what": "The thread-pool worker raised an unhandled exception.",
        "why": "Defensive — the future result was unreachable.",
        "fix": "Check logs.",
    },
    "unknown_error": {
        "title": "Unknown error",
        "what": "Score returned None without a tagged reason.",
        "why": "Defensive bucket for legacy code paths that haven't been migrated to the rejection-tag system.",
        "fix": "File a bug.",
    },
}


TRADER_CHECKS: Dict[str, Dict[str, str]] = {
    # Pre-flight state
    "malformed_signal": {
        "title": "Signal validation failed",
        "what": "The signal payload didn't pass schema validation (missing fields, wrong types).",
        "why": "Defensive — never trade on a corrupt signal.",
        "fix": "Check signal_generator output for the ticker.",
    },
    "bp_breaker": {
        "title": "Buying-power breaker",
        "what": "A recent submit failed with insufficient buying power; the breaker is preventing further submits for ~30 minutes.",
        "why": "Avoids spinning on rejected orders when the account is BP-saturated.",
        "fix": "Self-clears in 30 min or close a position to free BP.",
    },
    "broker_down": {
        "title": "Broker down",
        "what": "Alpaca returned 5xx errors; the breaker is paused for ~10 minutes.",
        "why": "Don't spin on broker outages.",
        "fix": "Self-clears when broker recovers.",
    },
    "account_blocked": {
        "title": "Account blocked",
        "what": "Alpaca says the account is trading-blocked, account-blocked, or transfers-blocked.",
        "why": "Hard kill — broker won't accept any new orders.",
        "fix": "Resolve at the broker (compliance hold, etc.).",
    },
    "pdt_lockout": {
        "title": "PDT lockout (24h)",
        "what": "A recent submit failed with a PDT 403; the breaker holds for 24 hours.",
        "why": "Repeated PDT rejects can escalate to a 90-day account lockout.",
        "fix": "Self-clears in 24h.",
    },
    "db_down": {
        "title": "DB down",
        "what": "Postgres returned an OperationalError; the breaker is paused.",
        "why": "Avoid retry storms on DB outages.",
        "fix": "Self-clears when DB recovers.",
    },
    "advisory_lock_held": {
        "title": "Concurrent scan in progress",
        "what": "Another instance is already evaluating the same ticker (Postgres advisory lock).",
        "why": "Cross-instance dedup on Cloud Run multi-instance deploys.",
        "fix": "Self-clears when the other scan completes.",
    },
    "entry_lock_timeout": {
        "title": "Entry lock timeout",
        "what": "Couldn't acquire the in-process entry lock within 30s.",
        "why": "Serializes entries within a single instance to prevent oversizing under burst load.",
        "fix": "Usually transient.",
    },
    "trading_frozen": {
        "title": "Trading frozen (WR or expectancy below threshold)",
        "what": "Recent closed-trade win rate is below 35% (n≥5) OR expectancy is ≤ 0 (n≥10) OR 5+ consecutive losses.",
        "why": "Pause new entries when the strategy is empirically broken until equity stabilizes.",
        "fix": "Self-clears as losing trades age past the 30-day window. Operator can age them out manually via /api/admin/age-out-trades.",
    },
    "crisis_mode": {
        "title": "Crisis mode",
        "what": "Account drawdown ≥5%, session DD ≥4%, VIX > 30 with SPY-5d < -5%, OR trading_frozen is also active.",
        "why": "Compound hazard — pause to assess.",
        "fix": "Self-clears when conditions improve. Can be reset via /api/admin/reset-equity-peak after reviewing the underlying cause.",
    },
    "disabled": {
        "title": "Auto-trader disabled",
        "what": "cfg.enabled = false (master switch).",
        "why": "Operator paused the bot.",
        "fix": "Click 'Start' in the Auto-Trader UI.",
    },
    "killed": {
        "title": "Kill switch active",
        "what": "cfg.killed = true (persistent kill).",
        "why": "Operator killed the bot — does not auto-rearm.",
        "fix": "Use the /api/trading/unkill endpoint or UI button.",
    },
    "pdt_limit": {
        "title": "PDT day-trade limit",
        "what": "≥3 day-trades in the trailing 5 business days (preventing the 4th from triggering 90-day PDT lock).",
        "why": "Live margin accounts <$25k get locked at 4 day-trades in 5 days.",
        "fix": "Wait until older day-trades age out, or top up to >$25k. Disable cfg.pdt_enforce on paper.",
    },
    "broker_not_enabled": {
        "title": "Broker not enabled",
        "what": "Alpaca client failed to initialize (bad API keys?).",
        "why": "Hard kill.",
        "fix": "Check APCA_API_KEY_ID / APCA_API_SECRET_KEY env vars.",
    },
    # Signal validation
    "non_buy_signal": {
        "title": "Not a BUY signal",
        "what": "The signal_type is HOLD, SELL, or NEUTRAL.",
        "why": "Stock-side only enters on BUY. SELL signals route through put-play hunt instead.",
        "fix": "Expected — signal_generator produces HOLD on most timeframes.",
    },
    "one_min_bar_disagrees": {
        "title": "1-minute bar disagrees with direction",
        "what": "The most recent closed 1-minute bar(s) printed in the wrong direction (close < open for BUY).",
        "why": "Prevents entering at a wick high — wait for the very-short-term tape to confirm.",
        "fix": "Set cfg.entry_1m_gate_mode to 'relaxed' (2-of-3 majority) or 'off' to skip this check.",
    },
    "missing_levels": {
        "title": "Missing entry/stop/target",
        "what": "The signal didn't carry entry, stop_loss, or target1.",
        "why": "Can't size a trade without price levels.",
        "fix": "Bug in signal_generator — file a ticket.",
    },
    "bad_t1_geometry": {
        "title": "T1 too close to entry",
        "what": "Target 1 is less than 0.6% above entry — not enough room for a profitable trade after costs.",
        "why": "Costs eat the profit on tight T1.",
        "fix": "Signal-side problem; signal_generator should produce a wider T1.",
    },
    "tf_not_allowed": {
        "title": "Timeframe not in allowlist",
        "what": "The signal's timeframe isn't in cfg.signal_timeframes (default '1h,4h,1d').",
        "why": "Filter out short-TF noise.",
        "fix": "Edit cfg.signal_timeframes in the Advanced Gates UI to include more TFs.",
    },
    # Confidence
    "below_confidence_threshold": {
        "title": "Below confidence threshold",
        "what": "Signal confidence is below cfg.confidence_threshold.",
        "why": "Don't trade weak setups.",
        "fix": "Lower cfg.confidence_threshold in the Auto-Trader Config UI (currently the most-flipped knob).",
    },
    "calibration_gate": {
        "title": "Calibration gate",
        "what": "The Wilson-95% lower bound of the realized win rate at this confidence bucket is below 35% (n≥30 trades).",
        "why": "If 'high confidence' has historically lost money, don't keep trading it.",
        "fix": "Self-clears as more wins accumulate at this confidence level.",
    },
    "ai_veto": {
        "title": "AI judge vetoed",
        "what": "The Anthropic-Claude-based judge returned 'skip' on this ticker.",
        "why": "An LLM with cross-asset context vetoes trades the technical signal misses.",
        "fix": "Set AI_ENTRY_VETO_MODE env var to 'off' or 'shadow' to disable / log-only.",
    },
    # Market context
    "opening_filter": {
        "title": "Opening 15min filter",
        "what": "It's 9:30-9:45 ET and the signal is on a 5/15/30-min timeframe.",
        "why": "Opening volatility makes short-TF entries noisy.",
        "fix": "Hardcoded — can't disable.",
    },
    "closing_filter": {
        "title": "Closing 10min filter",
        "what": "It's 15:50-16:00 ET and the signal is on a 5/15/30-min timeframe.",
        "why": "Closing-auction volatility makes short-TF entries noisy.",
        "fix": "Hardcoded.",
    },
    "macro_blackout": {
        "title": "Macro release blackout",
        "what": "A CPI/NFP/FOMC release is happening within the macro-blackout window.",
        "why": "Markets often spike both directions on macro releases — hold cash.",
        "fix": "Self-clears when the window passes.",
    },
    "macro_blackout_gate_error": {
        "title": "Macro blackout gate errored",
        "what": "The macro-calendar service raised an exception; we fail-closed.",
        "why": "Don't trade through a release we couldn't verify is past.",
        "fix": "Check macro_calendar service health.",
    },
    "earnings_gate_error": {
        "title": "Earnings calendar errored",
        "what": "Earnings-window check failed; fail-closed.",
        "why": "Don't risk trading INTO an earnings print on a flaky calendar.",
        "fix": "Check yfinance.",
    },
    "ticker_chop": {
        "title": "Ticker is in chop",
        "what": "ADX < 18 (range-bound, no clear trend).",
        "why": "Trend-following strategies underperform in chop.",
        "fix": "Mean-reversion strategies bypass this gate via cfg.",
    },
    "strategy_off_regime": {
        "title": "Strategy not allowed in this regime",
        "what": "The current SPY/VIX regime (TREND/CHOP/HIGH_VOL) doesn't permit this strategy.",
        "why": "Strategy-regime fit — we don't run momentum in chop.",
        "fix": "Wait for regime to flip, or edit regime_router rules.",
    },
    "halt_suspect": {
        "title": "Quote stale (halt suspected)",
        "what": "Live quote hasn't updated for 30+ seconds during RTH.",
        "why": "Possibly a halted ticker — don't trade.",
        "fix": "Self-clears when quotes resume.",
    },
    "pre_fomc_quiet_hour": {
        "title": "60min before FOMC",
        "what": "Quiet-hour window before an FOMC release.",
        "why": "Avoid pre-release positioning noise.",
        "fix": "Self-clears.",
    },
    "spread_widening": {
        "title": "Spread widening",
        "what": "Bid-ask spread > 1.8× its 20-bar EMA.",
        "why": "Wide spread = high slippage; wait for normal market.",
        "fix": "Self-clears as spread tightens.",
    },
    "aggressor_flow": {
        "title": "Adverse aggressor flow",
        "what": "Tape pressure is persistently against our intended direction.",
        "why": "Don't fight the flow.",
        "fix": "Self-clears when flow reverses.",
    },
    # Risk gates
    "daily_loss_halt": {
        "title": "Daily loss halt",
        "what": "Combined realized + unrealized P&L for today ≤ -cfg.daily_loss_limit_pct × equity.",
        "why": "Stop adding risk on a losing day.",
        "fix": "Self-clears at next session. Raise cfg.daily_loss_limit_pct in Advanced Gates if too tight.",
    },
    "auto_deleverage": {
        "title": "Session DD ≥6% — auto kill",
        "what": "Equity dropped ≥6% within today's session.",
        "why": "Activates the kill switch automatically.",
        "fix": "Manual review before unkill.",
    },
    "session_dd_4pct": {
        "title": "Session DD ≥4% halt",
        "what": "Equity dropped ≥4% within today's session.",
        "why": "Pause new entries until session ends.",
        "fix": "Self-clears at next session.",
    },
    "max_concurrent_cap": {
        "title": "Max concurrent positions",
        "what": "Open auto-trades count ≥ cfg.max_concurrent_positions (regime-adjusted).",
        "why": "Prevent over-leveraging into too many positions.",
        "fix": "Raise cfg.max_concurrent_positions in Advanced Gates, or close existing positions.",
    },
    "correlation_cap": {
        "title": "Correlation cap",
        "what": "More than cfg.max_correlated_open of currently-open tickers have ρ ≥ 0.70 with this candidate.",
        "why": "Highly-correlated positions concentrate risk.",
        "fix": "Raise cfg.max_correlated_open in Advanced Gates, or close a correlated position.",
    },
    "earnings_cluster": {
        "title": "Earnings cluster",
        "what": "≥4 currently-open positions have earnings within 7 days.",
        "why": "Earnings concentration = 7-day binary risk.",
        "fix": "Wait for earnings to pass on existing positions.",
    },
    "leverage_cap": {
        "title": "Leverage cap",
        "what": "Gross book leverage ≥ cfg.leverage_cap (default 1.5×).",
        "why": "Bound book leverage.",
        "fix": "Raise cfg.leverage_cap in Advanced Gates, or close positions.",
    },
    "book_var_99": {
        "title": "Book VaR-99 cap",
        "what": "Estimated 99%-VaR loss exceeds cfg.book_var_99_cap_pct × equity.",
        "why": "Cap tail risk across the book.",
        "fix": "Raise cfg.book_var_99_cap_pct, or close positions to free VaR budget. Currently the bot's binding gate after 5 entries.",
    },
    "adaptive_zero": {
        "title": "Adaptive risk = 0",
        "what": "Compounded VIX/expectancy/WR/strategy-DD multiplier dropped below 0.25, snapped to 0.",
        "why": "Multiple bad signals are stacking — pause until conditions improve.",
        "fix": "Self-clears as recent trades win or VIX falls.",
    },
    "account_drawdown": {
        "title": "Account drawdown ≥10%",
        "what": "60-day equity drawdown ≥10% — adaptive multiplier snaps to 0.",
        "why": "Hard pause when account is in deep drawdown.",
        "fix": "Self-clears as equity recovers.",
    },
    "r47_credit_cb": {
        "title": "Credit-spread circuit breaker",
        "what": "HYG-LQD spread or similar credit-stress signal tripped.",
        "why": "Credit stress precedes equity drops.",
        "fix": "Self-clears when credit normalizes.",
    },
    "idempotency_conflict": {
        "title": "Duplicate entry attempt",
        "what": "A trade with the same idempotency key (ticker+direction+date+levels) was just submitted.",
        "why": "Prevents double-entries on race conditions.",
        "fix": "Defensive — should never happen.",
    },
    # Geometry
    "bad_rr": {
        "title": "Risk/reward below floor",
        "what": "Net (T1 − entry − costs) / (entry − stop) < cfg.rr_min (default 1.3).",
        "why": "Don't take trades where the math doesn't work after costs.",
        "fix": "Lower cfg.rr_min in Advanced Gates if too strict.",
    },
    # Option-side
    "no_bear_thesis": {
        "title": "No bear thesis",
        "what": "build_bear_thesis returned None — couldn't construct a put setup for this ticker.",
        "why": "No SELL/short setup met the threshold.",
        "fix": "Expected on bullish/neutral tickers.",
    },
    "no_bull_thesis": {
        "title": "No bull thesis",
        "what": "build_bull_thesis returned None.",
        "why": "No BUY setup met the threshold.",
        "fix": "Expected on bearish/neutral tickers.",
    },
    "iv_rank_graded_veto": {
        "title": "IV rank graded veto",
        "what": "IV rank too low for long-premium options (cfg.iv_rank_graded_sizing).",
        "why": "Buying puts/calls in low-IV environments has poor expected value.",
        "fix": "Disable cfg.iv_rank_graded_sizing if you want to buy options regardless of IV.",
    },
    "iv_crush_sidestep": {
        "title": "IV crush risk",
        "what": "Earnings-related IV crush would damage long-premium positions post-print.",
        "why": "Defensive option-side gate.",
        "fix": "Self-clears post-earnings.",
    },
    "portfolio_greeks_cap": {
        "title": "Portfolio Greeks cap",
        "what": "Aggregate vega / gamma / net-delta exceeds the configured caps.",
        "why": "Bound option-portfolio risk.",
        "fix": "Close offsetting positions or raise the Greeks caps.",
    },
    "option_slippage_abandon": {
        "title": "Option slippage too high at submit",
        "what": "Mid-vs-fill drift exceeded the slippage tolerance — order abandoned.",
        "why": "Don't pay 5%+ in slippage on a single contract.",
        "fix": "Self-corrects on next setup with tighter spread.",
    },
}


# Family handlers — these are parametrized reasons like
# "bear_conf_53_below_60" or "source_mute_<strategy>".
def _bear_conf(reason: str) -> Dict[str, str]:
    return {
        "title": "Put-play confidence below floor",
        "what": f"The bear-thesis confidence is below the option-thesis floor (parsed from '{reason}').",
        "why": "Aggressive_options_mode raises the floor to 60; non-aggressive uses 0.85 × cfg.confidence_threshold (default 51).",
        "fix": "Either lower cfg.option_thesis_min_conf_aggressive (when aggressive) or cfg.option_thesis_min_conf_mult (when non-aggressive). Both editable in the Advanced Gates UI.",
    }


def _bull_conf(reason: str) -> Dict[str, str]:
    return {
        "title": "Call-play confidence below floor",
        "what": f"The bull-thesis confidence is below the option-thesis floor (parsed from '{reason}').",
        "why": "Same gate as bear_conf, mirror direction.",
        "fix": "Same — lower cfg.option_thesis_min_conf_aggressive or _mult.",
    }


def _source_mute(reason: str) -> Dict[str, str]:
    strategy = reason[len("source_mute_"):] if reason.startswith("source_mute_") else "unknown"
    return {
        "title": f"Strategy '{strategy}' auto-muted",
        "what": f"The '{strategy}' source-strategy has WR < 45% over n≥10 recent trades.",
        "why": "Don't keep trading a losing strategy. Re-enables when WR recovers.",
        "fix": "Disable cfg.source_mute_enabled in Advanced Gates if you want to trade through losing streaks.",
    }


def _autotrade_event_check(reason: str) -> Dict[str, str]:
    """Events that act as silent rejects without a skip-counter."""
    events = {
        "fat_finger_reject": {
            "title": "Fat-finger guard",
            "what": "Risk-per-share / entry was outside [0.1%, 10%] — looks like a typo or stale signal.",
            "why": "Defensive — never submit an order that's clearly mis-sized.",
            "fix": "Defensive; check for a stale signal.",
        },
        "gap_open_reject": {
            "title": "Gap-open reject",
            "what": "Live price has drifted >2% from the signal's entry — the setup has moved on.",
            "why": "Don't chase a gapped-up entry.",
            "fix": "Self-clears on the next signal pass.",
        },
        "illiquid_skip": {
            "title": "Illiquid skip",
            "what": "Live liquidity check failed (median typ-px × volume < $10M).",
            "why": "Same as scanner liquidity gate, applied at entry.",
            "fix": "Self-corrects.",
        },
        "stop_too_tight_atr": {
            "title": "Stop too tight (vs ATR)",
            "what": "(entry - stop) is < 0.8 × ATR — the stop is inside normal noise.",
            "why": "Get stopped out by random noise.",
            "fix": "Signal-side problem; signal_generator should produce a wider stop.",
        },
        "portfolio_heat_cap": {
            "title": "Portfolio heat cap",
            "what": "Beta-weighted portfolio risk exposure already at the cap.",
            "why": "Bound aggregate position risk.",
            "fix": "Close positions or raise the cap.",
        },
        "sector_heat_cap": {
            "title": "Sector heat cap",
            "what": "$ exposure in this sector ≥ 4% of equity.",
            "why": "Avoid sector concentration.",
            "fix": "Diversify across sectors.",
        },
        "earnings_skip": {
            "title": "Earnings within 48h",
            "what": "Same as scanner earnings_window, applied at entry time.",
            "why": "Don't trade into the print.",
            "fix": "Wait.",
        },
    }
    return events.get(reason, {
        "title": reason,
        "what": "(no definition registered yet)",
        "why": "—",
        "fix": "—",
    })


def lookup(reason: str, source: str = "trader") -> Dict[str, Any]:
    """Resolve a single reason to its definition, handling parametrized
    reasons like 'bear_conf_X_below_Y' and 'source_mute_<strategy>'."""
    if not reason:
        return {"title": "(none)", "what": "", "why": "", "fix": ""}
    if source == "scanner":
        return SCANNER_CHECKS.get(reason, {
            "title": reason, "what": "(no definition)", "why": "—", "fix": "—",
        })
    # Trader: handle exact match first.
    if reason in TRADER_CHECKS:
        return TRADER_CHECKS[reason]
    if reason.startswith("bear_conf_"):
        return _bear_conf(reason)
    if reason.startswith("bull_conf_"):
        return _bull_conf(reason)
    if reason.startswith("source_mute_"):
        return _source_mute(reason)
    # Fallback to event-style.
    return _autotrade_event_check(reason)


# r64: canonical gate ORDER for each evaluation kind. consider_signal
# (stock) fires gates top-down and short-circuits at the first reject.
# The Decision Log audit panel uses this list to render ✅ (passed
# BEFORE the failing gate), ⛔ (the failing gate), ⚪ (didn't run because
# of short-circuit AFTER the failing gate).
GATE_ORDER_STOCK = [
    # Pre-flight state (auto-trader.py:2090-2298)
    "malformed_signal",
    "bp_breaker",
    "broker_down",
    "account_blocked",
    "pdt_lockout",
    "db_down",
    "non_buy_signal",            # signal_type != BUY
    "one_min_bar_disagrees",     # 1m bar gate
    "entry_lock_timeout",
    "advisory_lock_held",
    "trading_frozen",
    "crisis_mode",
    "disabled",
    "killed",
    "pdt_limit",
    "broker_not_enabled",
    "below_confidence_threshold",
    # Risk / book gates (~2330-2400)
    "daily_loss_halt",
    "auto_deleverage",
    "session_dd_4pct",
    "max_concurrent_cap",
    # Time-of-day filters (~2435-2450)
    "opening_filter",
    "closing_filter",
    # Signal validation (~2470-2520)
    "tf_not_allowed",
    "missing_levels",
    "bad_t1_geometry",
    "bad_rr",
    # Per-ticker / per-stock state (~2620-2780)
    "ticker_chop",
    "earnings_gate_error",
    "macro_blackout",
    "macro_blackout_gate_error",
    "strategy_off_regime",
    "loss_pattern_veto",
    "correlation_cap",
    # Sizing / heat / book caps (~2820-2999)
    "ai_veto",
    "adaptive_zero",
    "account_drawdown",
    "earnings_cluster",
    "leverage_cap",
    "book_var_99",
    # Calibration / overlay / flow (~3050-3370)
    "r47_credit_cb",
    "calibration_gate",
    "pre_fomc_quiet_hour",
    "spread_widening",
    "aggressor_flow",
    "halt_suspect",
    "idempotency_conflict",
]
GATE_ORDER_OPTION_PUT = [
    "malformed_signal",
    "bp_breaker",
    "broker_down",
    "account_blocked",
    "pdt_lockout",
    "db_down",
    "no_bear_thesis",
    "bear_conf_*_below_*",        # parametrized family
    "iv_rank_graded_veto",
    "iv_crush_sidestep",
    "macro_blackout",
    "earnings_gate_error",
    "leverage_cap",
    "book_var_99",
    "portfolio_greeks_cap",
    "option_slippage_abandon",
    "idempotency_conflict",
]
GATE_ORDER_OPTION_CALL = [
    "malformed_signal",
    "bp_breaker",
    "broker_down",
    "account_blocked",
    "pdt_lockout",
    "db_down",
    "no_bull_thesis",
    "bull_conf_*_below_*",
    "iv_rank_graded_veto",
    "iv_crush_sidestep",
    "macro_blackout",
    "earnings_gate_error",
    "leverage_cap",
    "book_var_99",
    "portfolio_greeks_cap",
    "option_slippage_abandon",
    "idempotency_conflict",
]


def gate_order_for(kind: str) -> list:
    """Return the canonical gate firing order for a given decision kind."""
    if kind in ("option_put", "option"):
        return GATE_ORDER_OPTION_PUT
    if kind == "option_call":
        return GATE_ORDER_OPTION_CALL
    return GATE_ORDER_STOCK


def all_definitions() -> Dict[str, Any]:
    """Return everything as JSON for the UI to render tooltips."""
    return {
        "scanner": SCANNER_CHECKS,
        "trader": TRADER_CHECKS,
        "families": {
            "bear_conf_*_below_*": _bear_conf("bear_conf_X_below_Y"),
            "bull_conf_*_below_*": _bull_conf("bull_conf_X_below_Y"),
            "source_mute_*": _source_mute("source_mute_<strategy>"),
        },
        # r64: canonical gate firing order per decision kind.
        "gate_order": {
            "stock": GATE_ORDER_STOCK,
            "option_put": GATE_ORDER_OPTION_PUT,
            "option_call": GATE_ORDER_OPTION_CALL,
        },
    }
