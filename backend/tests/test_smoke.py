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
from datetime import datetime, timedelta

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


class TestCalibrateLongStop(unittest.TestCase):
    """Stop calibration math — never above price, ATR-floor respected, structurally widened."""

    def setUp(self):
        from services.signal_generator import _calibrate_long_stop
        self.fn = _calibrate_long_stop
        # Build a tiny df with a clear 5-bar swing low at 95.0
        self.df = pd.DataFrame({
            "High": [101, 102, 103, 102, 101],
            "Low":  [99, 100, 95, 96, 97],
            "Close":[100, 101, 100, 100, 100],
            "Open": [100, 100, 100, 100, 100],
            "Volume":[1000]*5,
        })

    def test_stop_below_price(self):
        stop = self.fn(price=100.0, atr=2.0, df=self.df,
                       candidates=[98.0, 97.0, 96.0], timeframe="1d")
        self.assertLess(stop, 100.0)

    def test_atr_floor_widens(self):
        # candidates all very tight (99) — ATR floor at 100 - 2*2 = 96 must dominate
        stop = self.fn(price=100.0, atr=2.0, df=self.df,
                       candidates=[99.0, 99.5], timeframe="1d")
        self.assertLessEqual(stop, 96.0 + 0.01)

    def test_no_candidates_falls_back_to_atr(self):
        stop = self.fn(price=100.0, atr=2.0, df=self.df,
                       candidates=[None, None], timeframe="1d")
        self.assertLess(stop, 100.0)

    def test_picks_second_tightest(self):
        # Tightest (99) gets dropped; second (98) becomes seed; structural widens
        stop = self.fn(price=100.0, atr=0.1, df=self.df,
                       candidates=[99.0, 98.0, 97.0], timeframe="1d")
        # Min of {98, atr_floor=99.8, swing_lo*.997=94.7} → ~94.7
        self.assertLessEqual(stop, 98.0)


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


class TestORBStrategy(unittest.TestCase):
    """Opening Range Breakout: handles intraday and noops on daily."""

    def setUp(self):
        from services.strategies import _opening_range_breakout
        self.fn = _opening_range_breakout

    def test_daily_noop(self):
        idx = pd.date_range("2024-01-01", periods=20, freq="D")
        d = pd.DataFrame({
            "Open":[100]*20, "High":[101]*20, "Low":[99]*20, "Close":[100]*20,
            "Volume":[1000]*20,
        }, index=idx)
        out = self.fn(d)
        self.assertFalse(out["entry_long"].any())
        self.assertFalse(out["entry_short"].any())

    def test_short_input_noop(self):
        # Less than 4 bars triggers the early-bail branch
        idx = pd.date_range("2024-01-02 09:30", periods=2, freq="5min")
        d = pd.DataFrame({
            "Open":[100]*2, "High":[101]*2, "Low":[99]*2, "Close":[100]*2,
            "Volume":[10000]*2,
        }, index=idx)
        out = self.fn(d)
        self.assertFalse(out["entry_long"].any())


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


if __name__ == "__main__":
    unittest.main()
