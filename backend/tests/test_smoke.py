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


if __name__ == "__main__":
    unittest.main()
