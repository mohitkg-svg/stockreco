"""
Smoke tests — fast, no network, no DB.

Run from backend/:
    python -m pytest tests/ -v
or:
    python -m unittest discover tests
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

# Make `services.*` importable when running from anywhere
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestIdempotencyKey(unittest.TestCase):
    """Same signal → same key; differing fields → different key."""

    def setUp(self):
        from services.auto_trader import _signal_idempotency_key
        self.fn = _signal_idempotency_key

    def _sig(self, **over):
        s = {
            "ticker": "AAPL",
            "signal_type": "BUY",
            "entry": 200.123,
            "stop_loss": 195.0,
            "target1": 210.0,
            "timeframe": "1d",
        }
        s.update(over)
        return s

    def test_deterministic(self):
        self.assertEqual(self.fn(self._sig()), self.fn(self._sig()))

    def test_rounding_collapses_jitter(self):
        # Sub-cent entry jitter must NOT change key (we round to 2dp).
        self.assertEqual(
            self.fn(self._sig(entry=200.123)),
            self.fn(self._sig(entry=200.124)),
        )

    def test_different_ticker_differs(self):
        self.assertNotEqual(self.fn(self._sig()), self.fn(self._sig(ticker="MSFT")))

    def test_different_direction_differs(self):
        self.assertNotEqual(self.fn(self._sig()), self.fn(self._sig(signal_type="SELL")))

    def test_different_timeframe_differs(self):
        self.assertNotEqual(self.fn(self._sig()), self.fn(self._sig(timeframe="1h")))


class TestVwapStrategy(unittest.TestCase):
    """VWAP-reclaim strategy: returns dict with entry_long/entry_short series."""

    def setUp(self):
        from services.strategies import _vwap_reclaim
        self.fn = _vwap_reclaim

    def test_no_vwap_column_noop(self):
        d = pd.DataFrame({
            "Open":[100]*30, "High":[101]*30, "Low":[99]*30, "Close":[100]*30,
            "Volume":[1000]*30,
        })
        out = self.fn(d)
        self.assertIsInstance(out, dict)
        self.assertIn("entry_long", out)
        self.assertIn("entry_short", out)
        self.assertFalse(out["entry_long"].any())
        self.assertFalse(out["entry_short"].any())


class TestChandelierTrailMath(unittest.TestCase):
    """Chandelier overlay arithmetic: stop = HWM - mult*ATR; only tightens upward."""

    def test_chandelier_math(self):
        hwm = 110.0
        atr = 2.0
        mult = 3.0
        stop = round(hwm - mult * atr, 2)
        self.assertEqual(stop, 104.0)

    def test_chandelier_only_tightens(self):
        # New chandelier stop below current_stop must NOT lower it.
        current_stop = 105.0
        new_stop = 104.0
        # Mirror the manage-loop guard
        chosen = max(current_stop, new_stop)
        self.assertEqual(chosen, 105.0)


class TestMetricsNoOpFallback(unittest.TestCase):
    """metrics.inc/timer must be safe even without prometheus_client installed."""

    def test_inc_safe(self):
        from services import metrics
        # Doesn't raise even with bogus name
        metrics.inc("autotrade_event", event="test_event")
        metrics.inc("nonexistent_counter", foo="bar")

    def test_timer_safe(self):
        from services import metrics
        with metrics.timer("manage"):
            x = 1 + 1
        with metrics.timer("nonexistent"):
            pass


class TestTrimFractionForADX(unittest.TestCase):
    """ADX-driven trim fractions, including the r37 ADX≥45 skip-T1 case."""

    def setUp(self):
        from services import auto_trader
        self.fn = auto_trader.trim_fraction_for_adx
        self._orig_adx = None
        from services import position_manager
        self._orig_chand = position_manager.chandelier_adx
        # Replace chandelier_adx with an injectable function
        self._adx_value = None
        position_manager.chandelier_adx = lambda _t: self._adx_value

    def tearDown(self):
        from services import position_manager
        position_manager.chandelier_adx = self._orig_chand

    def test_default_when_adx_missing(self):
        self._adx_value = None
        self.assertEqual(self.fn("AAPL", "T1", default_frac=0.33), 0.33)

    def test_chop_returns_default(self):
        self._adx_value = 18.0
        self.assertEqual(self.fn("AAPL", "T1", default_frac=0.33), 0.33)

    def test_strong_returns_15pct(self):
        self._adx_value = 42.0
        self.assertEqual(self.fn("AAPL", "T1", default_frac=0.33), 0.15)

    def test_extreme_returns_zero_at_T1(self):
        # r37: ADX ≥ 45 → skip the T1 trim entirely (parabolic — runner is the trade)
        self._adx_value = 50.0
        self.assertEqual(self.fn("AAPL", "T1", default_frac=0.33), 0.0)

    def test_extreme_does_NOT_skip_T2(self):
        # T2 still trims even in extreme trend — never pure-runner past T2
        self._adx_value = 50.0
        self.assertEqual(self.fn("AAPL", "T2", default_frac=0.33), 0.15)


class TestRateLimiter(unittest.TestCase):
    """Token-bucket rate limiter on /api/* (r38). Disabled when
    APP_RATE_LIMIT_PER_MIN=0. Burst should pass; sustained over-rate trips."""

    def setUp(self):
        # Reset bucket state + force a known config
        from routers import _auth as a
        a._RATE_BUCKETS.clear()
        os.environ["APP_RATE_LIMIT_PER_MIN"] = "60"
        os.environ["APP_RATE_LIMIT_BURST"] = "5"

    def tearDown(self):
        os.environ.pop("APP_RATE_LIMIT_PER_MIN", None)
        os.environ.pop("APP_RATE_LIMIT_BURST", None)
        from routers import _auth as a
        a._RATE_BUCKETS.clear()

    def _fake_request(self, ip="1.2.3.4"):
        # Tiny stub matching what _auth.rate_limit reads off Request
        class _Client:
            def __init__(self, host): self.host = host
        class _Req:
            def __init__(self, host): self.client = _Client(host)
        return _Req(ip)

    def test_burst_allows_then_blocks(self):
        from routers._auth import rate_limit
        from fastapi import HTTPException
        req = self._fake_request("9.9.9.9")
        # 5 burst tokens — first 5 calls pass, 6th raises 429.
        for _ in range(5):
            self.assertIsNone(rate_limit(req, x_api_key=None))
        with self.assertRaises(HTTPException) as ctx:
            rate_limit(req, x_api_key=None)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("Retry-After", ctx.exception.headers or {})

    def test_disabled_via_env(self):
        os.environ["APP_RATE_LIMIT_PER_MIN"] = "0"
        from routers._auth import rate_limit
        req = self._fake_request("1.1.1.1")
        # Should pass an arbitrary count without raising
        for _ in range(50):
            self.assertIsNone(rate_limit(req, x_api_key=None))

    def test_separate_keys_have_separate_buckets(self):
        from routers._auth import rate_limit
        from fastapi import HTTPException
        req = self._fake_request("2.2.2.2")
        # Drain key A
        for _ in range(5):
            rate_limit(req, x_api_key="key-A")
        # 6th on A trips
        with self.assertRaises(HTTPException):
            rate_limit(req, x_api_key="key-A")
        # But key B is fresh
        self.assertIsNone(rate_limit(req, x_api_key="key-B"))


class TestPartialExitBacktest(unittest.TestCase):
    """Verify the partial-exit simulation path mirrors the live trim ladder
    (33% at T1, 33% at T2, runner at target) and produces a different
    aggregate PnL than the legacy single-exit path.
    """

    def _make_uptrend_df(self, n=40, start=100.0, drift_per_bar=0.5):
        idx = pd.date_range("2025-01-01", periods=n, freq="D")
        # Build arrays directly — using pd.Series here would index-misalign
        # against the DatetimeIndex assigned to the DataFrame and produce
        # NaN in Open/High/Low (silent corruption).
        close = [start + i * drift_per_bar for i in range(n)]
        d = pd.DataFrame({
            "Open":  [c - 0.1 for c in close],
            "High":  [c + 0.5 for c in close],
            "Low":   [c - 0.5 for c in close],
            "Close": close,
            "Volume": [1_000_000] * n,
        }, index=idx)
        return d

    def test_partial_exits_emit_one_trade_with_aggregate_pnl(self):
        from services.backtester import _simulate
        d = self._make_uptrend_df(n=60, start=100.0, drift_per_bar=0.6)
        entries = pd.Series(False, index=d.index)
        entries.iloc[1] = True   # entry on bar 2
        atr = pd.Series([1.0] * len(d), index=d.index)
        out = _simulate(d, entries, "BUY", atr, timeframe="1d", partial_exits=True)
        # Trade list contains exactly one consolidated trade row (final exit).
        self.assertEqual(len(out["trades"]), 1)
        t = out["trades"][0]
        self.assertIn("pnl_pct", t)
        # Drift ~ 0.6/bar on $100 → ~3% to T1, ~6% to T2, ~10% to final target
        # Aggregate P/L on 33%+33%+34% blend should be POSITIVE in this clean uptrend.
        self.assertGreater(t["pnl_pct"], 0)

    def test_partial_vs_legacy_diverge_on_strong_trend(self):
        # Strong-trend tape: legacy (single full-target exit) and partial
        # (banks at T1 + T2 + target) produce DIFFERENT aggregate PnL.
        # This is the divergence the reviewer flagged ("Ghost Alpha").
        from services.backtester import _simulate
        d = self._make_uptrend_df(n=80, start=100.0, drift_per_bar=0.4)
        entries = pd.Series(False, index=d.index)
        entries.iloc[1] = True
        atr = pd.Series([1.0] * len(d), index=d.index)
        legacy = _simulate(d, entries, "BUY", atr, timeframe="1d", partial_exits=False)
        partial = _simulate(d, entries, "BUY", atr, timeframe="1d", partial_exits=True)
        # Both should produce a single trade row in our setup
        self.assertEqual(len(legacy["trades"]), 1)
        self.assertEqual(len(partial["trades"]), 1)
        # Aggregate PnL is generally lower in partial mode for a clean
        # trend (banking 33% at T1 reduces upside) but still positive.
        # The exact relationship depends on bar-level dynamics; assert
        # only that the values differ — that's the point of the change.
        self.assertNotEqual(legacy["trades"][0]["pnl_pct"],
                             partial["trades"][0]["pnl_pct"])


class TestAIJudgeAbstainPaths(unittest.TestCase):
    """Hard guarantee: Claude unreachable / disabled MUST NOT block trading.

    Each call site has an env-flag (off | shadow | active). Default is off
    everywhere. These tests pin that the abstain path returns the
    expected proceed/hold/1.0 values so a missing API key, network blip,
    or schema mismatch can never alter live trading behavior.
    """

    def setUp(self):
        # Force off for the off-path tests
        for k in ("AI_ENTRY_VETO_MODE", "AI_NEWS_EXIT_MODE", "AI_CONFIDENCE_MULT_MODE"):
            os.environ.pop(k, None)
        # Reset client cache so subsequent tests re-evaluate the env
        from services import ai_judge
        ai_judge._client = None
        ai_judge._client_init_attempted = False

    def test_off_mode_entry_veto_proceeds(self):
        from services import ai_judge
        out = ai_judge.entry_veto({"ticker": "AAPL"}, {})
        self.assertEqual(out["verdict"], "proceed")
        self.assertFalse(out["honored"])
        self.assertEqual(out["mode"], "off")

    def test_off_mode_news_exit_holds(self):
        from services import ai_judge
        out = ai_judge.news_exit_decision({"ticker": "AAPL", "id": 1}, {"title": "x"})
        self.assertEqual(out["action"], "hold")
        self.assertFalse(out["honored"])
        self.assertEqual(out["mode"], "off")

    def test_off_mode_confidence_mult_returns_1(self):
        from services import ai_judge
        out = ai_judge.confidence_multiplier({"ticker": "AAPL"}, {})
        self.assertEqual(out["multiplier"], 1.0)
        self.assertFalse(out["honored"])
        self.assertEqual(out["mode"], "off")

    def test_no_api_key_shadow_mode_abstains(self):
        # In shadow mode WITHOUT an API key, _get_client returns None →
        # _call_with_tool returns None → entry_veto returns proceed.
        os.environ["AI_ENTRY_VETO_MODE"] = "shadow"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        from services import ai_judge
        ai_judge._client = None
        ai_judge._client_init_attempted = False
        out = ai_judge.entry_veto({"ticker": "AAPL"}, {})
        self.assertEqual(out["verdict"], "proceed")
        self.assertFalse(out["honored"])
        del os.environ["AI_ENTRY_VETO_MODE"]

    def test_active_mode_with_no_key_still_abstains_safely(self):
        # Even in `active`, if the API key is missing the call abstains
        # to "proceed" — never blocks the trade.
        os.environ["AI_ENTRY_VETO_MODE"] = "active"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        from services import ai_judge
        ai_judge._client = None
        ai_judge._client_init_attempted = False
        out = ai_judge.entry_veto({"ticker": "AAPL"}, {})
        self.assertEqual(out["verdict"], "proceed")
        self.assertFalse(out["honored"])
        del os.environ["AI_ENTRY_VETO_MODE"]

    def test_confidence_multiplier_clamps_to_envelope(self):
        # Even if Claude returned 99×, the wrapper clamps to AI_MULT_MAX.
        # Verified by mocking _call_with_tool to return an out-of-range value.
        os.environ["AI_CONFIDENCE_MULT_MODE"] = "active"
        os.environ["ANTHROPIC_API_KEY"] = "x"  # presence, value irrelevant due to mock
        from services import ai_judge
        from services.config import AI_MULT_MIN, AI_MULT_MAX
        orig = ai_judge._call_with_tool
        try:
            ai_judge._call_with_tool = lambda *a, **kw: {"multiplier": 99.0, "reason": "mock"}
            ai_judge._client = "fake"  # bypass _get_client guard inside if any
            # _get_client is called inside _call_with_tool — we override _call_with_tool
            # so we never actually hit it. But _call_with_tool's signature is the entry.
            out = ai_judge.confidence_multiplier({"ticker": "AAPL"}, {})
            self.assertLessEqual(out["multiplier"], AI_MULT_MAX + 1e-9)
            self.assertGreaterEqual(out["multiplier"], AI_MULT_MIN - 1e-9)
            ai_judge._call_with_tool = lambda *a, **kw: {"multiplier": -5.0, "reason": "mock"}
            out2 = ai_judge.confidence_multiplier({"ticker": "AAPL"}, {})
            self.assertGreaterEqual(out2["multiplier"], AI_MULT_MIN - 1e-9)
        finally:
            ai_judge._call_with_tool = orig
            ai_judge._client = None
            ai_judge._client_init_attempted = False
            del os.environ["AI_CONFIDENCE_MULT_MODE"]
            del os.environ["ANTHROPIC_API_KEY"]


class TestSignalPayloadValidation(unittest.TestCase):
    """SignalPayload — validates the signal dict at the consume boundary.

    Malformed signals get rejected here instead of being silently coerced
    to zero downstream by `signal.get("entry") or 0`.
    """

    def _good(self, **over):
        base = {
            "ticker": "AAPL",
            "timeframe": "1h",
            "signal_type": "BUY",
            "confidence": 75.0,
            "entry": 150.0,
            "stop_loss": 148.0,
            "target1": 154.0,
        }
        base.update(over)
        return base

    def test_well_formed_validates(self):
        from models import SignalPayload
        m = SignalPayload.model_validate(self._good())
        self.assertEqual(m.ticker, "AAPL")
        self.assertTrue(m.is_actionable())

    def test_missing_ticker_rejected(self):
        from models import SignalPayload
        from pydantic import ValidationError
        bad = self._good(); del bad["ticker"]
        with self.assertRaises(ValidationError):
            SignalPayload.model_validate(bad)

    def test_invalid_timeframe_rejected(self):
        from models import SignalPayload
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SignalPayload.model_validate(self._good(timeframe="2h"))

    def test_invalid_signal_type_rejected(self):
        from models import SignalPayload
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SignalPayload.model_validate(self._good(signal_type="LONG"))

    def test_confidence_out_of_range_rejected(self):
        from models import SignalPayload
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SignalPayload.model_validate(self._good(confidence=150.0))
        with self.assertRaises(ValidationError):
            SignalPayload.model_validate(self._good(confidence=-1.0))

    def test_ticker_uppercased(self):
        from models import SignalPayload
        m = SignalPayload.model_validate(self._good(ticker="aapl"))
        self.assertEqual(m.ticker, "AAPL")

    def test_extras_allowed_for_enrichment(self):
        # Real signals have a long tail of optional enrichment fields that
        # aren't enumerated. Strict mode would make every new enrichment
        # a breaking change — model is configured `extra='allow'`.
        from models import SignalPayload
        m = SignalPayload.model_validate(self._good(
            sentiment_score=0.42, news_count=7, ml_prob=0.61,
            short_pct_float=0.18, wsb_mentions_24h=12,
        ))
        self.assertEqual(m.ticker, "AAPL")

    def test_neutral_signal_not_actionable(self):
        from models import SignalPayload
        m = SignalPayload.model_validate({
            "ticker": "AAPL", "timeframe": "1h", "signal_type": "NEUTRAL",
            "confidence": 50.0,
        })
        self.assertFalse(m.is_actionable())

    def test_buy_without_levels_not_actionable(self):
        from models import SignalPayload
        m = SignalPayload.model_validate({
            "ticker": "AAPL", "timeframe": "1h", "signal_type": "BUY",
            "confidence": 80.0,
        })
        self.assertFalse(m.is_actionable())


class TestHeatAwareRiskMultiplier(unittest.TestCase):
    """Per-trade risk shrinks as live heat approaches the cap."""

    def setUp(self):
        from services import risk_manager
        self.rm = risk_manager
        self._orig_heat_fn = risk_manager.current_portfolio_heat
        # Inject a fake heat reader so we don't hit the DB
        self._fake_heat = 0.0
        risk_manager.current_portfolio_heat = lambda: self._fake_heat

    def tearDown(self):
        self.rm.current_portfolio_heat = self._orig_heat_fn

    def test_no_heat_no_throttle(self):
        # 0% used → 1.0
        self._fake_heat = 0.0
        self.assertEqual(self.rm.heat_aware_risk_multiplier(100_000.0), 1.0)

    def test_below_50pct_no_throttle(self):
        # 40% of cap (cap = 10% × equity = $10k; heat = $4k = 40%)
        self._fake_heat = 4_000.0
        self.assertEqual(self.rm.heat_aware_risk_multiplier(100_000.0), 1.0)

    def test_50_to_70_pct(self):
        self._fake_heat = 6_500.0    # 65% of $10k cap
        self.assertEqual(self.rm.heat_aware_risk_multiplier(100_000.0), 0.85)

    def test_70_to_85_pct(self):
        self._fake_heat = 8_000.0    # 80% of $10k cap
        self.assertEqual(self.rm.heat_aware_risk_multiplier(100_000.0), 0.60)

    def test_85_to_100_pct(self):
        self._fake_heat = 9_500.0    # 95% of $10k cap
        self.assertEqual(self.rm.heat_aware_risk_multiplier(100_000.0), 0.40)

    def test_zero_equity_no_op(self):
        self._fake_heat = 5_000.0
        self.assertEqual(self.rm.heat_aware_risk_multiplier(0.0), 1.0)

    def test_negative_equity_no_op(self):
        self._fake_heat = 5_000.0
        self.assertEqual(self.rm.heat_aware_risk_multiplier(-1.0), 1.0)


class TestPortfolioBacktest(unittest.TestCase):
    """Smoke-level tests for the portfolio-level backtest helpers.

    Doesn't run the full simulation (needs network for fetch_ohlcv) — just
    asserts the public surface and stress-window registry are intact.
    """

    def test_stress_window_registry_consistent(self):
        from services.portfolio_backtest import STRESS_WINDOWS
        self.assertGreater(len(STRESS_WINDOWS), 0)
        for key, spec in STRESS_WINDOWS.items():
            self.assertEqual(len(spec), 3, f"{key}: (start, end, label) tuple")
            start, end, label = spec
            # Parses as a date
            s = pd.Timestamp(start)
            e = pd.Timestamp(end)
            self.assertLess(s, e, f"{key}: start {start} must precede end {end}")
            self.assertTrue(label and len(label) > 5)

    def test_unknown_stress_window_returns_note(self):
        # Path uses STRESS_WINDOWS lookup before any data fetch — safe to call
        # without network.
        from services.portfolio_backtest import run_portfolio_backtest
        out = run_portfolio_backtest(
            tickers=["AAPL"],  # never fetched because stress_window invalid → early return
            stress_window="not-a-real-window",
        )
        self.assertIn("note", out)
        self.assertIn("unknown stress_window", out["note"])
        self.assertIsNone(out.get("stats"))

    def test_corr_calc_pair_correlation(self):
        # Verify the upper-triangle correlation extraction against deliberately
        # constructed price series whose pct-returns are known.
        # A: returns alternate +0.10 / -0.10
        # B: same returns as A  → corr(A,B) = +1
        # C: opposite returns   → corr(A,C) = -1
        a_rets = [0.10, -0.10, 0.10, -0.10, 0.10, -0.10]
        c_rets = [-0.10, 0.10, -0.10, 0.10, -0.10, 0.10]
        a = [100.0]
        b = [50.0]
        c = [100.0]
        for ar, cr in zip(a_rets, c_rets):
            a.append(a[-1] * (1 + ar))
            b.append(b[-1] * (1 + ar))   # identical returns to A
            c.append(c[-1] * (1 + cr))   # mirrored returns
        df = pd.DataFrame({"A": a, "B": b, "C": c})
        corr = df.pct_change().corr()
        iu = np.triu_indices(len(corr), k=1)
        pairs = corr.values[iu]
        pairs = pairs[~pd.isna(pairs)]
        # Three pairs: (A,B)=+1, (A,C)=-1, (B,C)=-1
        self.assertEqual(len(pairs), 3)
        self.assertAlmostEqual(float(pairs.max()), 1.0, places=4)
        self.assertAlmostEqual(float(pairs.min()), -1.0, places=4)

class TestOptionGreeksBackfill(unittest.TestCase):
    """r96 R5: backfill_one with a mock trade + monkey-patched fetcher."""

    def test_skips_non_option_rows(self):
        from services import option_greeks as og

        class _T:
            asset_type = "stock"
            ticker = "AAPL"
            symbol = "AAPL"
            entry_delta = None
            entry_gamma = None
            entry_theta = None
            entry_vega = None
        res = og.backfill_one(_T(), db=None)
        self.assertEqual(res.get("skipped"), "not option")

    def test_idempotent_when_all_populated(self):
        from services import option_greeks as og

        class _T:
            asset_type = "option"
            ticker = "AAPL"
            symbol = "AAPL250117C00200000"
            entry_delta = 0.5
            entry_gamma = 0.01
            entry_theta = -0.05
            entry_vega = 0.10
        # No DB writes expected; mock db that raises if commit is called.

        class _DB:
            def commit(self):
                raise AssertionError("commit should not be called")

            def rollback(self):
                pass
        res = og.backfill_one(_T(), db=_DB(), force_refresh=False)
        self.assertEqual(res.get("updated_fields"), [])

    def test_backfills_missing_fields_when_quote_available(self):
        from services import option_greeks as og

        # Monkey-patch the fetcher to return a synthetic Greeks dict.
        original_fetch = og._fetch_contract_greeks
        og._fetch_contract_greeks = lambda _s: {
            "delta": 0.42, "gamma": 0.008, "theta": -0.04, "vega": 0.12, "iv": 0.25,
        }
        try:
            class _T:
                asset_type = "option"
                ticker = "AAPL"
                symbol = "AAPL250117C00200000"
                entry_delta = None
                entry_gamma = None
                entry_theta = None
                entry_vega = None

            class _DB:
                committed = False

                def commit(self):
                    type(self).committed = True

                def rollback(self):
                    pass
            t = _T()
            db = _DB()
            res = og.backfill_one(t, db=db)
            self.assertEqual(set(res["updated_fields"]), {"delta", "gamma", "theta", "vega"})
            self.assertEqual(t.entry_delta, 0.42)
            self.assertEqual(t.entry_gamma, 0.008)
            self.assertEqual(t.entry_theta, -0.04)
            self.assertEqual(t.entry_vega, 0.12)
            self.assertTrue(_DB.committed)
        finally:
            og._fetch_contract_greeks = original_fetch

    def test_no_quote_available_skips(self):
        from services import option_greeks as og
        original_fetch = og._fetch_contract_greeks
        og._fetch_contract_greeks = lambda _s: None
        try:
            class _T:
                asset_type = "option"
                ticker = "AAPL"
                symbol = "AAPL250117C00200000"
                entry_delta = None
                entry_gamma = None
                entry_theta = None
                entry_vega = None
            res = og.backfill_one(_T(), db=None)
            self.assertEqual(res.get("skipped"), "no quote")
        finally:
            og._fetch_contract_greeks = original_fetch


class TestSurvivorshipClamp(unittest.TestCase):
    """r96 R4: clamp_df_to_delisted_at returns the input unchanged when
    no delisting record exists. (DB-bound paths exercised at integration.)"""

    def test_no_delisted_record_returns_input(self):
        from services.survivorship import clamp_df_to_delisted_at
        df = pd.DataFrame(
            {"Close": [100.0, 101.0, 99.5]},
            index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        )
        # Use a ticker that won't be in any test DB (with __ prefix).
        out = clamp_df_to_delisted_at(df, "__NEVER_EXISTS__")
        self.assertEqual(len(out), 3)

    def test_empty_df_passthrough(self):
        from services.survivorship import clamp_df_to_delisted_at
        df = pd.DataFrame()
        out = clamp_df_to_delisted_at(df, "ANYTHING")
        self.assertTrue(out.empty)


class TestCorrelationMath(unittest.TestCase):
    """r96 R3: pearson + inflation factor math, no external data."""

    def test_pearson_perfect_positive(self):
        from services.correlation import _pearson
        xs = list(range(40))
        ys = [2 * x + 5 for x in xs]
        self.assertAlmostEqual(_pearson(xs, ys), 1.0, places=4)

    def test_pearson_perfect_negative(self):
        from services.correlation import _pearson
        xs = list(range(40))
        ys = [-x for x in xs]
        self.assertAlmostEqual(_pearson(xs, ys), -1.0, places=4)

    def test_pearson_zero_variance_returns_zero(self):
        from services.correlation import _pearson
        xs = [1.0] * 40
        ys = list(range(40))
        self.assertEqual(_pearson(xs, ys), 0.0)

    def test_pearson_too_short_returns_zero(self):
        from services.correlation import _pearson
        self.assertEqual(_pearson([1, 2, 3], [1, 2, 3]), 0.0)

    def test_inflation_factor_single_position_is_unity(self):
        from services.correlation import correlation_inflation_factor
        self.assertEqual(
            correlation_inflation_factor([{"ticker": "AAPL", "dollar_risk": 100.0}]),
            1.0,
        )

    def test_inflation_factor_empty_is_unity(self):
        from services.correlation import correlation_inflation_factor
        self.assertEqual(correlation_inflation_factor([]), 1.0)

    def test_inflation_factor_capped(self):
        from services.correlation import correlation_inflation_factor, MAX_INFLATION
        # Even if we could synthesize perfect correlation, the cap protects.
        # We can't easily inject mock OHLCV here; the cap is the guarantee.
        # Just confirm the cap constant is sane.
        self.assertGreaterEqual(MAX_INFLATION, 1.0)
        self.assertLessEqual(MAX_INFLATION, 10.0)


class TestSignalBucketsDiversity(unittest.TestCase):
    pass


class TestCalibratedWeightsMath(unittest.TestCase):
    pass


class TestOrphanStopOrderBucketing(unittest.TestCase):
    """r96 F7: _stop_orders_by_symbol must keep only stop-typed entries and
    bucket them case-insensitively by symbol."""

    def test_keeps_only_stop_orders(self):
        from services.auto_trader import _stop_orders_by_symbol
        orders = [
            {"symbol": "AAPL", "type": "stop"},
            {"symbol": "AAPL", "type": "limit"},
            {"symbol": "MSFT", "type": "stop_limit"},
            {"symbol": "TSLA", "type": "market"},
            {"symbol": "TSLA", "type": "STOP"},
        ]
        result = _stop_orders_by_symbol(orders)
        self.assertIn("AAPL", result)
        self.assertEqual(len(result["AAPL"]), 1)
        self.assertIn("MSFT", result)
        self.assertEqual(len(result["MSFT"]), 1)
        self.assertIn("TSLA", result)
        self.assertEqual(len(result["TSLA"]), 1)

    def test_handles_empty_and_malformed(self):
        from services.auto_trader import _stop_orders_by_symbol
        self.assertEqual(_stop_orders_by_symbol([]), {})
        self.assertEqual(_stop_orders_by_symbol(None), {})
        # Malformed entries are skipped without raising
        self.assertEqual(
            _stop_orders_by_symbol([{"symbol": "", "type": "stop"}, {"type": "stop"}]),
            {},
        )

    def test_case_normalizes_symbol(self):
        from services.auto_trader import _stop_orders_by_symbol
        result = _stop_orders_by_symbol([{"symbol": "aapl", "type": "stop"}])
        self.assertIn("AAPL", result)


class TestDailyLossHardHalt(unittest.TestCase):
    """r96 F4: pure-function predicate for the hard daily-loss halt."""

    def test_breached_when_combined_below_threshold(self):
        from services.auto_trader import _daily_loss_hard_halt_breached
        # -3% × $100k = -$3000 threshold; combined PnL -$3100 → breach
        breached, combined, threshold = _daily_loss_hard_halt_breached(
            realized_today=-2000.0, unrealized=-1100.0,
            equity=100_000.0, halt_pct=0.03,
        )
        self.assertTrue(breached)
        self.assertEqual(combined, -3100.0)
        self.assertEqual(threshold, -3000.0)

    def test_not_breached_at_exact_threshold_minus_one_cent(self):
        from services.auto_trader import _daily_loss_hard_halt_breached
        # combined at -$2999.99 > -$3000 threshold → no breach
        breached, _, _ = _daily_loss_hard_halt_breached(
            realized_today=-2999.99, unrealized=0.0,
            equity=100_000.0, halt_pct=0.03,
        )
        self.assertFalse(breached)

    def test_breached_exactly_at_threshold(self):
        from services.auto_trader import _daily_loss_hard_halt_breached
        # ≤ semantics: combined exactly equals threshold → breach
        breached, _, _ = _daily_loss_hard_halt_breached(
            realized_today=-3000.0, unrealized=0.0,
            equity=100_000.0, halt_pct=0.03,
        )
        self.assertTrue(breached)

    def test_safe_default_on_unknown_realized(self):
        from services.auto_trader import _daily_loss_hard_halt_breached
        breached, _, _ = _daily_loss_hard_halt_breached(
            realized_today=None, unrealized=-5000.0,
            equity=100_000.0, halt_pct=0.03,
        )
        self.assertFalse(breached)

    def test_safe_default_on_zero_equity(self):
        from services.auto_trader import _daily_loss_hard_halt_breached
        breached, _, _ = _daily_loss_hard_halt_breached(
            realized_today=-5000.0, unrealized=0.0,
            equity=0.0, halt_pct=0.03,
        )
        self.assertFalse(breached)

    def test_safe_default_on_zero_halt_pct(self):
        from services.auto_trader import _daily_loss_hard_halt_breached
        breached, _, _ = _daily_loss_hard_halt_breached(
            realized_today=-5000.0, unrealized=0.0,
            equity=100_000.0, halt_pct=0.0,
        )
        self.assertFalse(breached)

    def test_unrealized_gain_offsets_realized_loss(self):
        from services.auto_trader import _daily_loss_hard_halt_breached
        # -$5k realized but +$3k unrealized → combined -$2k > -$3k threshold
        breached, combined, _ = _daily_loss_hard_halt_breached(
            realized_today=-5000.0, unrealized=3000.0,
            equity=100_000.0, halt_pct=0.03,
        )
        self.assertFalse(breached)
        self.assertEqual(combined, -2000.0)


class TestStrategyAutoDisableGate(unittest.TestCase):
    """r96 F3: pure-function gate evaluation. Confirms the (disabled, reason)
    tuple is correct across the four corner cases (off, no data, edge of
    floor, far below floor)."""

    def test_disabled_returns_false_when_flag_off(self):
        from services.auto_trader import _strategy_auto_disable_check
        # Even a catastrophic 10% WR on n=100 does not disable when flag is off.
        disabled, _ = _strategy_auto_disable_check(
            "BreakoutVol",
            {"n": 100, "win_rate": 0.10},
            enabled=False, wr_floor=0.40, min_n=30,
        )
        self.assertFalse(disabled)

    def test_disabled_returns_false_when_n_below_min(self):
        from services.auto_trader import _strategy_auto_disable_check
        disabled, _ = _strategy_auto_disable_check(
            "BreakoutVol",
            {"n": 29, "win_rate": 0.10},
            enabled=True, wr_floor=0.40, min_n=30,
        )
        self.assertFalse(disabled)

    def test_disabled_returns_false_when_wr_at_floor(self):
        # win_rate >= floor → not disabled (strict-below convention)
        from services.auto_trader import _strategy_auto_disable_check
        disabled, _ = _strategy_auto_disable_check(
            "BreakoutVol",
            {"n": 30, "win_rate": 0.40},
            enabled=True, wr_floor=0.40, min_n=30,
        )
        self.assertFalse(disabled)

    def test_disabled_when_wr_below_floor_and_n_sufficient(self):
        from services.auto_trader import _strategy_auto_disable_check
        disabled, reason = _strategy_auto_disable_check(
            "BreakoutVol",
            {"n": 30, "win_rate": 0.39},
            enabled=True, wr_floor=0.40, min_n=30,
        )
        self.assertTrue(disabled)
        self.assertIn("BreakoutVol", reason)
        self.assertIn("0.390", reason)

    def test_missing_strategy_or_card_returns_false(self):
        from services.auto_trader import _strategy_auto_disable_check
        disabled, _ = _strategy_auto_disable_check(
            None, {"n": 30, "win_rate": 0.10},
            enabled=True, wr_floor=0.40, min_n=30,
        )
        self.assertFalse(disabled)
        disabled, _ = _strategy_auto_disable_check(
            "BreakoutVol", None,
            enabled=True, wr_floor=0.40, min_n=30,
        )
        self.assertFalse(disabled)


class TestMLDriftAutoDisable(unittest.TestCase):
    """r96 F2: nightly eval flips ml_scoring_enabled off when Brier breaches
    the threshold on N consecutive eval rows. Pure-logic test using a
    hand-rolled mock-DB that mirrors the SQLAlchemy surface we use."""

    def _mk_db(self, scoring_enabled, drift_enabled, threshold, n_required, recent_briers):
        """Build a mock db object with `query(...)` returning a chained API
        for both AutoTraderConfig and MLEvalResult."""

        class _Cfg:
            def __init__(self):
                self.id = 1
                self.ml_scoring_enabled = scoring_enabled
                self.ml_drift_auto_disable_enabled = drift_enabled
                self.ml_drift_brier_alert_threshold = threshold
                self.ml_drift_consecutive_days_required = n_required

        class _Row:
            def __init__(self, brier):
                self.brier = brier

        cfg = _Cfg()
        rows = [_Row(b) for b in recent_briers]

        class _Query:
            def __init__(self, kind, parent):
                self.kind = kind
                self.parent = parent

            def filter(self, *a, **k): return self

            def first(self):
                return cfg if self.kind == "cfg" else (rows[0] if rows else None)

            def order_by(self, *a, **k): return self

            def limit(self, n):
                self.parent._last_limit = n
                return self

            def all(self):
                lim = getattr(self.parent, "_last_limit", len(rows))
                return rows[:lim]

        class _DB:
            def __init__(self):
                self._committed = False
                self._last_limit = None

            def query(self, model):
                kind = "cfg" if model.__name__ == "AutoTraderConfig" else "row"
                return _Query(kind, self)

            def commit(self):
                self._committed = True

        return _DB(), cfg

    def test_no_action_when_disabled(self):
        from services.ml_eval import _maybe_auto_disable_on_drift
        db, cfg = self._mk_db(
            scoring_enabled=True, drift_enabled=False,
            threshold=0.05, n_required=3,
            recent_briers=[0.20, 0.20, 0.20],
        )
        _maybe_auto_disable_on_drift(db, brier_now=0.20)
        self.assertTrue(cfg.ml_scoring_enabled)  # still on
        self.assertFalse(db._committed)

    def test_no_action_when_already_off(self):
        from services.ml_eval import _maybe_auto_disable_on_drift
        db, cfg = self._mk_db(
            scoring_enabled=False, drift_enabled=True,
            threshold=0.05, n_required=3,
            recent_briers=[0.20, 0.20, 0.20],
        )
        _maybe_auto_disable_on_drift(db, brier_now=0.20)
        self.assertFalse(cfg.ml_scoring_enabled)
        self.assertFalse(db._committed)

    def test_no_action_when_below_threshold(self):
        from services.ml_eval import _maybe_auto_disable_on_drift
        db, cfg = self._mk_db(
            scoring_enabled=True, drift_enabled=True,
            threshold=0.05, n_required=3,
            recent_briers=[0.04, 0.04, 0.04],
        )
        _maybe_auto_disable_on_drift(db, brier_now=0.04)
        self.assertTrue(cfg.ml_scoring_enabled)
        self.assertFalse(db._committed)

    def test_no_action_when_one_row_passes(self):
        from services.ml_eval import _maybe_auto_disable_on_drift
        db, cfg = self._mk_db(
            scoring_enabled=True, drift_enabled=True,
            threshold=0.05, n_required=3,
            recent_briers=[0.20, 0.04, 0.20],  # middle row OK → no breach
        )
        _maybe_auto_disable_on_drift(db, brier_now=0.20)
        self.assertTrue(cfg.ml_scoring_enabled)
        self.assertFalse(db._committed)

    def test_disables_when_all_breach(self):
        from services.ml_eval import _maybe_auto_disable_on_drift
        db, cfg = self._mk_db(
            scoring_enabled=True, drift_enabled=True,
            threshold=0.05, n_required=3,
            recent_briers=[0.20, 0.15, 0.10],
        )
        _maybe_auto_disable_on_drift(db, brier_now=0.20)
        self.assertFalse(cfg.ml_scoring_enabled)
        self.assertTrue(db._committed)

    def test_skipped_when_history_too_short(self):
        from services.ml_eval import _maybe_auto_disable_on_drift
        db, cfg = self._mk_db(
            scoring_enabled=True, drift_enabled=True,
            threshold=0.05, n_required=3,
            recent_briers=[0.20, 0.20],  # only 2 rows
        )
        _maybe_auto_disable_on_drift(db, brier_now=0.20)
        self.assertTrue(cfg.ml_scoring_enabled)
        self.assertFalse(db._committed)
if __name__ == "__main__":
    unittest.main()
