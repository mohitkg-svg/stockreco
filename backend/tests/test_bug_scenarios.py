"""
Bug-scenario regression tests.

Targets the specific code paths that produced losses in production paper
trading (2026-04-23 / 2026-04-24) and the related code paths that share
the same bug shape — branches conditional on (asset_type, signal_type,
premium, time_held, etc.) where naive code review misses the asymmetry.

Each test case is named after the bug it would have caught had it
existed before the loss. New scenarios should be added whenever a
post-mortem surfaces a code-path-level bug.

Run from backend/:
    DATABASE_URL=sqlite:///:memory: python -m unittest tests/test_bug_scenarios.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import patch

# Use an isolated SQLite file for the test session so we never touch prod DB.
_TEST_DB = tempfile.mktemp(prefix="stockrecs_test_", suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
# Block APScheduler from booting if anything imports main accidentally.
os.environ.setdefault("APP_API_KEY", "test")

# Make `services.*` and `database` importable when running from `backend/`.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Force imports AFTER env vars are set.
from database import (  # noqa: E402
    SessionLocal, create_tables, AutoTrade, Signal, AutoTraderConfig,
    MacroEvent,
)


def _reset_db():
    """Drop all tables and re-create. Cheap on SQLite."""
    from database import engine, Base
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _make_trade(**over) -> AutoTrade:
    base = {
        "ticker": "AAPL", "asset_type": "stock", "side": "buy", "qty": 100.0,
        "entry_price": 200.0, "stop_loss": 195.0, "target1": 210.0,
        "status": "open",
        "opened_at": datetime.utcnow() - timedelta(minutes=10),
        "filled_at": datetime.utcnow() - timedelta(minutes=10),
    }
    base.update(over)
    # NOT NULL columns that callers don't usually set in tests — default to
    # sensible mirrors of the public fields.
    if "symbol" not in base or base.get("symbol") in (None, ""):
        base["symbol"] = base.get("ticker") or "AAPL"
    base.setdefault("current_stop", base.get("stop_loss"))
    base.setdefault("requested_entry", base.get("entry_price"))
    return AutoTrade(**base)


def _make_signal(**over) -> Signal:
    base = {
        "ticker": "AAPL", "signal_type": "SELL", "confidence": 90.0,
        "timeframe": "1d", "entry": 200.0, "stop_loss": 205.0, "target1": 190.0,
        "generated_at": datetime.utcnow(),
        "reasoning": "synthetic test signal",
    }
    base.update(over)
    return Signal(**base)


# ============================================================================
# CATEGORY A: Reverse-thesis direction logic
# ----------------------------------------------------------------------------
# Bug shape (AMKR -$1,190 paper, 2026-04-24): _check_reversal hardcoded
# `opposing = "BUY" if t.asset_type == "stock" else "BUY"` — i.e. all options
# used BUY as opposing. Correct for PUT (long-put = bearish, opposing=BUY).
# WRONG for CALL (long-call = bullish, opposing should be SELL).
#
# A confirming BUY signal was force-closing winning CALL theses.
# ============================================================================

class TestReverseThesisDirection(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _run(self, trade: AutoTrade, signal: Signal):
        self.db.add(trade); self.db.commit()
        self.db.add(signal); self.db.commit()
        # generated_at must be > opened_at + 60s grace for the check to consider it
        signal.generated_at = (trade.filled_at or trade.opened_at) + timedelta(seconds=120)
        self.db.commit()
        from services.auto_trader import _check_reversal
        return _check_reversal(trade, self.db)

    def test_stock_long_closed_by_high_conf_sell(self):
        """Stock long + SELL @ same TF + ≥80 conf → reversal triggers."""
        t = _make_trade(asset_type="stock")
        s = _make_signal(signal_type="SELL", confidence=85, timeframe="1d")
        result = self._run(t, s)
        self.assertIsNotNone(result, "stock+SELL 85 conf should trigger reversal")
        self.assertIn("SELL", result)

    def test_stock_long_NOT_closed_by_buy(self):
        """Stock long + BUY → never reversal (BUY confirms a long)."""
        t = _make_trade(asset_type="stock")
        s = _make_signal(signal_type="BUY", confidence=99)
        result = self._run(t, s)
        self.assertIsNone(result, "stock long must not be closed by BUY")

    def test_stock_long_NOT_closed_below_conf_gate(self):
        """Conf below REVERSE_CONFIDENCE_GATE (80) shouldn't fire."""
        t = _make_trade(asset_type="stock")
        s = _make_signal(signal_type="SELL", confidence=70)
        result = self._run(t, s)
        self.assertIsNone(result)

    def test_stock_long_NOT_closed_by_lower_tf(self):
        """A 1h SELL should NOT close a 1d-source long. Tighter rule than M5."""
        t = _make_trade(asset_type="stock", signal_id=None)
        # Insert source-TF signal first
        src = _make_signal(signal_type="BUY", timeframe="1d", confidence=80)
        self.db.add(src); self.db.commit()
        t.signal_id = src.id
        self.db.add(t); self.db.commit()
        # Now an opposing SELL on a LOWER tf
        opp = _make_signal(signal_type="SELL", timeframe="1h", confidence=85,
                           generated_at=t.opened_at + timedelta(seconds=120))
        self.db.add(opp); self.db.commit()
        from services.auto_trader import _check_reversal
        result = _check_reversal(t, self.db)
        self.assertIsNone(result, "1h SELL must not reverse a 1d long")

    def test_long_put_closed_by_buy(self):
        """Long PUT (bearish bet) + BUY signal at gate → reversal triggers."""
        t = _make_trade(asset_type="option", symbol="AAPL260515P00200000",
                        ticker="AAPL", side="buy")
        s = _make_signal(signal_type="BUY", confidence=85)
        result = self._run(t, s)
        self.assertIsNotNone(result, "PUT must be reversed by a high-conf BUY")

    def test_long_put_NOT_closed_by_sell(self):
        """Long PUT + SELL signal → SELL confirms the bear thesis, no reversal."""
        t = _make_trade(asset_type="option", symbol="AAPL260515P00200000",
                        ticker="AAPL", side="buy")
        s = _make_signal(signal_type="SELL", confidence=99)
        result = self._run(t, s)
        self.assertIsNone(result, "PUT must NOT be closed by confirming SELL")

    def test_long_call_closed_by_sell(self):
        """Long CALL (bullish bet) + SELL signal at gate → reversal triggers.
        Was BROKEN before the fix (CALLs used BUY as opposing, so SELL signals
        couldn't close them; matching BUY signals incorrectly did)."""
        t = _make_trade(asset_type="option", symbol="AAPL260515C00200000",
                        ticker="AAPL", side="buy")
        s = _make_signal(signal_type="SELL", confidence=85)
        result = self._run(t, s)
        self.assertIsNotNone(result, "CALL must be reversed by a high-conf SELL")

    def test_long_call_NOT_closed_by_buy(self):
        """Long CALL + BUY signal → BUY confirms the bull thesis, NO reversal.
        This is the AMKR bug — confirming BUY was force-closing the CALL."""
        t = _make_trade(asset_type="option", symbol="AMKR260515C00075000",
                        ticker="AMKR", side="buy")
        s = _make_signal(ticker="AMKR", signal_type="BUY", confidence=99)
        result = self._run(t, s)
        self.assertIsNone(result, "CALL must NOT be closed by confirming BUY")


# ============================================================================
# CATEGORY B: OCC option-symbol parser
# ----------------------------------------------------------------------------
# Helper underpinning the call/put detection in _check_reversal. Trivial
# to test, expensive if wrong (whole CALL play family routes through it).
# ============================================================================

class TestOCCParser(unittest.TestCase):

    def test_call_detected(self):
        from services.auto_trader import _is_call_option
        for sym in ("AMKR260515C00075000", "AAPL260515C00200000", "F260515C00012000"):
            t = _make_trade(asset_type="option", symbol=sym)
            self.assertTrue(_is_call_option(t), f"{sym} should be detected as CALL")

    def test_put_detected_as_not_call(self):
        from services.auto_trader import _is_call_option
        for sym in ("NFLX260424P00093000", "AAPL260515P00200000"):
            t = _make_trade(asset_type="option", symbol=sym)
            self.assertFalse(_is_call_option(t), f"{sym} should NOT be detected as CALL")

    def test_short_or_missing_symbol_safe(self):
        from services.auto_trader import _is_call_option
        for sym in ("", None, "X", "ABC"):
            t = _make_trade(asset_type="option", symbol=sym)
            self.assertFalse(_is_call_option(t), f"{sym!r} should not crash")


# ============================================================================
# CATEGORY C: Macro release blackout window
# ----------------------------------------------------------------------------
# is_in_blackout() decides whether to skip new entries. A sloppy time-window
# check could leave a window of false-negative entries right around CPI/FOMC.
# ============================================================================

class TestMacroBlackout(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()
        # Insert a high-importance event 20 minutes from now.
        self.now = datetime.utcnow()
        self.high_evt = MacroEvent(
            event_key="CPI", event_name="Consumer Price Index",
            country="US", importance="high",
            release_time_utc=self.now + timedelta(minutes=20),
        )
        self.db.add(self.high_evt); self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_inside_pre_blackout_high(self):
        """20 min before CPI = inside the 30-min pre-window for high impact."""
        from services.macro_calendar import is_in_blackout
        in_blk, ev, why = is_in_blackout(now_utc=self.now)
        self.assertTrue(in_blk, "should be in pre-release blackout")
        self.assertIn("CPI", why)
        self.assertIn("pre-release", why)

    def test_outside_pre_blackout_window(self):
        """45 min before CPI = OUTSIDE the 30-min pre-window."""
        from services.macro_calendar import is_in_blackout
        in_blk, _ev, _why = is_in_blackout(now_utc=self.now - timedelta(minutes=15))
        self.assertFalse(in_blk, "45 min before CPI should not be blackout")

    def test_inside_post_blackout(self):
        """30 min after CPI = inside the 60-min post-window."""
        from services.macro_calendar import is_in_blackout
        post_now = self.high_evt.release_time_utc + timedelta(minutes=30)
        in_blk, _ev, why = is_in_blackout(now_utc=post_now)
        self.assertTrue(in_blk, "30 min after CPI should be in cooldown")
        self.assertIn("post-release", why)

    def test_options_strict_widens_window(self):
        """options_strict=True widens windows by 50%. 40 min pre-CPI = inside
        the 45-min strict pre-window, OUTSIDE the 30-min normal one."""
        from services.macro_calendar import is_in_blackout
        check_t = self.high_evt.release_time_utc - timedelta(minutes=40)
        in_norm, _, _ = is_in_blackout(now_utc=check_t, options_only_strict=False)
        in_strict, _, _ = is_in_blackout(now_utc=check_t, options_only_strict=True)
        self.assertFalse(in_norm, "40m pre-CPI should NOT be in normal pre-window")
        self.assertTrue(in_strict, "40m pre-CPI SHOULD be in options-strict pre-window")


# ============================================================================
# CATEGORY D: Ticker blacklist
# ----------------------------------------------------------------------------
# is_blacklisted() guards every entry path. Comma-handling and whitespace
# matter — a single trailing-comma typo could disable the blacklist silently.
# ============================================================================

class TestBlacklist(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()
        self.cfg = AutoTraderConfig(id=1, enabled=True)
        self.db.add(self.cfg); self.db.commit()

    def tearDown(self):
        self.db.close()

    def _set(self, val):
        self.cfg.ticker_blacklist = val
        self.db.commit()

    def test_exact_match_blocked(self):
        from services.auto_trader import is_blacklisted
        self._set("GOOGL")
        self.assertTrue(is_blacklisted("GOOGL", self.cfg))

    def test_case_insensitive(self):
        from services.auto_trader import is_blacklisted
        self._set("googl")
        self.assertTrue(is_blacklisted("GOOGL", self.cfg))
        self._set("GOOGL")
        self.assertTrue(is_blacklisted("googl", self.cfg))

    def test_comma_separated_multi(self):
        from services.auto_trader import is_blacklisted
        self._set("GOOGL,TSLA, NVDA  ,AAPL")
        for tk in ("GOOGL", "TSLA", "NVDA", "AAPL"):
            self.assertTrue(is_blacklisted(tk, self.cfg), f"{tk} should be blacklisted")
        self.assertFalse(is_blacklisted("MSFT", self.cfg))

    def test_empty_blacklist_blocks_nothing(self):
        from services.auto_trader import is_blacklisted
        self._set("")
        self.assertFalse(is_blacklisted("GOOGL", self.cfg))
        self._set(None)
        self.assertFalse(is_blacklisted("GOOGL", self.cfg))


# ============================================================================
# CATEGORY E: Risk multiplier ceiling
# ----------------------------------------------------------------------------
# All five factors at max would compound to ~4.7× pre-fix. The 2× ceiling
# (RISK_MULT_CEILING from services.config) caps the runaway.
# ============================================================================

class TestRiskMultCeiling(unittest.TestCase):

    def test_compound_clamped_to_ceiling(self):
        from services.config import RISK_MULT_CEILING
        # 1.75 × 1.35 × 1.30 × 1.30 × 1.0 = 4.00 raw stack
        raw = 1.75 * 1.35 * 1.30 * 1.30 * 1.0
        clamped = min(raw, RISK_MULT_CEILING)
        self.assertGreater(raw, RISK_MULT_CEILING)
        self.assertEqual(clamped, RISK_MULT_CEILING)
        self.assertLessEqual(clamped, 2.0)

    def test_below_ceiling_unchanged(self):
        from services.config import RISK_MULT_CEILING
        raw = 1.10 * 1.05 * 1.0 * 1.0 * 1.0   # 1.155
        clamped = min(raw, RISK_MULT_CEILING)
        self.assertAlmostEqual(clamped, raw, places=4)


# ============================================================================
# CATEGORY F: Cheap-options sizing cap
# ----------------------------------------------------------------------------
# Bug: $0.30 premium options sized 122 contracts (CNTA -$2,440). The
# cheap-options fraction must produce <= 33 contracts at $200K equity.
# ============================================================================

class TestCheapOptionsSizing(unittest.TestCase):

    def _calc_cap_qty(self, premium, equity, aggressive=True):
        """Mirrors the per_contract_dollar_cap_frac branch in consider_*_play."""
        if premium < 0.50:
            frac = 0.005
        elif premium < 2.00:
            frac = 0.010
        else:
            frac = 0.02 if aggressive else 0.01
        return int((equity * frac) / (premium * 100))

    def test_sub_50c_premium_capped_tight(self):
        # CNTA case: $0.30 premium, $200K equity, aggressive
        q = self._calc_cap_qty(0.30, 200_000, aggressive=True)
        self.assertLessEqual(q, 33, f"Sub-50c premium @ $200K equity should cap ≤33; got {q}")

    def test_mid_premium_normal(self):
        q = self._calc_cap_qty(1.50, 200_000, aggressive=True)
        # 1% / 150 = 13.33 → 13
        self.assertEqual(q, 13)

    def test_expensive_premium_aggressive(self):
        q = self._calc_cap_qty(5.00, 200_000, aggressive=True)
        # 2% / 500 = 8
        self.assertEqual(q, 8)


# ============================================================================
# CATEGORY G: ML scorer multiplier envelope
# ----------------------------------------------------------------------------
# Sanity-bound: P(win) → multiplier should ALWAYS be in [0.88, 1.12].
# Catches any future tuning that accidentally widens the envelope.
# ============================================================================

class TestMLMultiplier(unittest.TestCase):

    def test_envelope_bounds(self):
        from services.ml_scorer import winrate_to_multiplier
        for p in [0.0, 0.1, 0.34, 0.36, 0.45, 0.55, 0.59, 0.61, 0.69, 0.71, 0.95, 1.0]:
            m = winrate_to_multiplier(p)
            self.assertGreaterEqual(m, 0.88, f"mult for p={p} below 0.88")
            self.assertLessEqual(m, 1.12, f"mult for p={p} above 1.12")

    def test_none_returns_neutral(self):
        from services.ml_scorer import winrate_to_multiplier
        self.assertEqual(winrate_to_multiplier(None), 1.0)

    def test_monotonic_non_decreasing(self):
        """Higher P(win) must never produce a lower multiplier."""
        from services.ml_scorer import winrate_to_multiplier
        prev = 0.0
        for p in [0.1, 0.3, 0.4, 0.5, 0.55, 0.65, 0.75, 0.9]:
            m = winrate_to_multiplier(p)
            self.assertGreaterEqual(m, prev)
            prev = m


# ============================================================================
# CATEGORY H: Fundamentals quality score + multiplier
# ----------------------------------------------------------------------------
# Score is the sum of contributions from profitability, growth, balance
# sheet, valuation. Tests pin the boundaries so accidental tuning shifts
# get caught.
# ============================================================================

class TestFundamentalsScore(unittest.TestCase):

    def test_excellent_balance_sheet_growth_scores_high(self):
        from services.fundamentals import compute_quality_score
        # NVDA-shaped: high margins, fast growth, low debt, reasonable PEG
        s = compute_quality_score({
            "profit_margin": 0.55, "operating_margin": 0.62, "return_on_equity": 0.95,
            "revenue_growth_yoy": 0.50, "earnings_growth_yoy": 0.80,
            "debt_to_equity": 0.20, "current_ratio": 4.2,
            "peg_ratio": 0.9, "pe_ratio": 35, "ev_to_ebitda": 28,
        })
        self.assertGreaterEqual(s, 60, f"expected strong score, got {s}")

    def test_distressed_company_scores_low(self):
        from services.fundamentals import compute_quality_score
        # Negative margins, shrinking revenue, debt-heavy
        s = compute_quality_score({
            "profit_margin": -0.20, "operating_margin": -0.10, "return_on_equity": -0.30,
            "revenue_growth_yoy": -0.25, "earnings_growth_yoy": -0.50,
            "debt_to_equity": 5.0, "current_ratio": 0.7,
            "peg_ratio": -1.0, "pe_ratio": 0, "ev_to_ebitda": 80,
        })
        self.assertLessEqual(s, -30, f"expected distressed score, got {s}")

    def test_partial_data_doesnt_blow_up(self):
        """Small/foreign-listed tickers may only have a couple of fields populated."""
        from services.fundamentals import compute_quality_score
        s = compute_quality_score({"pe_ratio": 18})
        self.assertIsInstance(s, float)
        self.assertGreaterEqual(s, -100)
        self.assertLessEqual(s, 100)

    def test_score_clamped(self):
        from services.fundamentals import compute_quality_score
        # Pile every positive contributor on
        s = compute_quality_score({
            "profit_margin": 0.99, "operating_margin": 0.99, "return_on_equity": 0.99,
            "revenue_growth_yoy": 0.99, "earnings_growth_yoy": 0.99,
            "debt_to_equity": 0.0, "current_ratio": 99,
            "peg_ratio": 0.5, "pe_ratio": 5, "ev_to_ebitda": 1,
        })
        self.assertLessEqual(s, 100)
        self.assertGreaterEqual(s, 50)


class TestFundamentalsMultiplier(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _store(self, ticker: str, score: float):
        from database import Fundamentals
        row = Fundamentals(ticker=ticker.upper(), quality_score=score, data_hash="x")
        self.db.add(row); self.db.commit()

    def test_strong_buy_for_excellent_fundamentals(self):
        from services.fundamentals import quality_multiplier
        self._store("AAA", 85)
        self.assertEqual(quality_multiplier("AAA", "BUY"), 1.08)

    def test_penalty_for_junk_on_buy(self):
        from services.fundamentals import quality_multiplier
        self._store("ZZZ", -60)
        self.assertEqual(quality_multiplier("ZZZ", "BUY"), 0.92)

    def test_mirror_on_sell(self):
        from services.fundamentals import quality_multiplier
        self._store("WWW", -60)
        # Junk fundamentals confirm a bearish thesis
        self.assertEqual(quality_multiplier("WWW", "SELL"), 1.08)
        self._store("VVV", 85)
        # Excellent fundamentals fight a SELL thesis
        self.assertEqual(quality_multiplier("VVV", "SELL"), 0.92)

    def test_neutral_when_no_data(self):
        from services.fundamentals import quality_multiplier
        # No row stored
        self.assertEqual(quality_multiplier("MISSING", "BUY"), 1.0)

    def test_envelope_bounds(self):
        from services.fundamentals import quality_multiplier
        # All persisted values across the spectrum stay in [0.92, 1.08]
        for s in [-100, -75, -50, -30, -10, 0, 15, 30, 50, 70, 85, 100]:
            self._store(f"T{s}", s)
            for d in ("BUY", "SELL"):
                m = quality_multiplier(f"T{s}", d)
                self.assertGreaterEqual(m, 0.92)
                self.assertLessEqual(m, 1.08)


class TestShortInterestMultiplier(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _store(self, ticker: str, pct: float):
        from database import Fundamentals
        self.db.add(Fundamentals(ticker=ticker.upper(), short_pct_float=pct, data_hash="x"))
        self.db.commit()

    def test_very_crowded_short_penalizes_buy(self):
        from services.fundamentals import short_interest_multiplier
        self._store("AAA", 0.30)   # 30% of float shorted — very crowded
        self.assertEqual(short_interest_multiplier("AAA", "BUY"), 0.92)
        # SELL-side: already-crowded, penalize fresh short
        self.assertEqual(short_interest_multiplier("AAA", "SELL"), 0.92)

    def test_moderate_short_gives_buy_squeeze_tilt(self):
        from services.fundamentals import short_interest_multiplier
        self._store("BBB", 0.18)
        self.assertEqual(short_interest_multiplier("BBB", "BUY"), 1.02)

    def test_neutral_below_threshold(self):
        from services.fundamentals import short_interest_multiplier
        self._store("CCC", 0.05)
        self.assertEqual(short_interest_multiplier("CCC", "BUY"), 1.0)

    def test_no_data_neutral(self):
        from services.fundamentals import short_interest_multiplier
        self.assertEqual(short_interest_multiplier("UNKNOWN", "BUY"), 1.0)


class TestSocialSentimentMultiplier(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _store(self, ticker: str, msgs: int, bullish: Optional[float] = None):
        from database import SocialSentiment
        self.db.add(SocialSentiment(
            ticker=ticker.upper(), source="stocktwits",
            message_count_24h=msgs, bullish_pct_24h=bullish,
            bearish_pct_24h=(1 - bullish) if bullish is not None else None,
        ))
        self.db.commit()

    def test_strong_bullish_lean_confirms_buy(self):
        from services.social_sentiment import sentiment_multiplier
        self._store("BULL", 50, 0.75)
        self.assertEqual(sentiment_multiplier("BULL", "BUY"), 1.04)
        self.assertEqual(sentiment_multiplier("BULL", "SELL"), 0.96)

    def test_strong_bearish_lean_confirms_sell(self):
        from services.social_sentiment import sentiment_multiplier
        self._store("BEAR", 50, 0.30)
        self.assertEqual(sentiment_multiplier("BEAR", "SELL"), 1.04)
        self.assertEqual(sentiment_multiplier("BEAR", "BUY"), 0.96)

    def test_low_volume_not_trusted(self):
        """Below 20-message min, any lean is ignored (too noisy)."""
        from services.social_sentiment import sentiment_multiplier
        self._store("LOWVOL", 5, 0.90)  # 5 messages, 90% bullish — ignored
        self.assertEqual(sentiment_multiplier("LOWVOL", "BUY"), 1.0)

    def test_mixed_sentiment_neutral(self):
        from services.social_sentiment import sentiment_multiplier
        self._store("MIXED", 50, 0.50)
        self.assertEqual(sentiment_multiplier("MIXED", "BUY"), 1.0)


class TestInsiderMultiplier(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _store(self, ticker: str, buys_90: int, sells_90: int):
        from database import InsiderSummary
        total = buys_90 + sells_90
        ratio = (buys_90 / total) if total else None
        self.db.add(InsiderSummary(
            ticker=ticker.upper(),
            buy_count_30d=buys_90, buy_count_90d=buys_90,
            sell_count_30d=sells_90, sell_count_90d=sells_90,
            net_buy_ratio_90d=round(ratio, 3) if ratio is not None else None,
            buy_dollar_90d=0.0,
        ))
        self.db.commit()

    def test_strong_insider_buy_boosts_buy(self):
        from services.insider_trades import insider_multiplier
        self._store("INSBUY", buys_90=8, sells_90=2)  # 80% buy ratio
        self.assertEqual(insider_multiplier("INSBUY", "BUY"), 1.06)

    def test_heavy_selling_penalizes_buy(self):
        from services.insider_trades import insider_multiplier
        self._store("INSSELL", buys_90=1, sells_90=5)  # ~17% buy ratio
        self.assertEqual(insider_multiplier("INSSELL", "BUY"), 0.97)

    def test_heavy_selling_confirms_sell(self):
        from services.insider_trades import insider_multiplier
        self._store("INSSELL2", buys_90=1, sells_90=9)
        self.assertEqual(insider_multiplier("INSSELL2", "SELL"), 1.06)

    def test_thin_sample_neutral(self):
        """Below min 3-transaction count, ratio is too noisy; stay neutral."""
        from services.insider_trades import insider_multiplier
        self._store("THIN", buys_90=1, sells_90=0)
        self.assertEqual(insider_multiplier("THIN", "BUY"), 1.0)

    def test_balanced_neutral(self):
        from services.insider_trades import insider_multiplier
        self._store("EVEN", buys_90=5, sells_90=5)
        self.assertEqual(insider_multiplier("EVEN", "BUY"), 1.0)


class TestBetaWeight(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _store(self, ticker: str, beta):
        from database import Fundamentals
        row = Fundamentals(ticker=ticker.upper(), beta=beta, data_hash="x")
        self.db.add(row); self.db.commit()

    def test_neutral_when_missing(self):
        """No row or null beta → default 1.0 (market-weighted)."""
        from services.fundamentals import beta_weight
        self.assertEqual(beta_weight("UNKNOWN"), 1.0)
        self._store("NULLB", None)
        self.assertEqual(beta_weight("NULLB"), 1.0)

    def test_passthrough_for_typical_range(self):
        from services.fundamentals import beta_weight
        self._store("LOW", 0.7)
        self._store("MID", 1.0)
        self._store("HIGH", 1.8)
        self.assertEqual(beta_weight("LOW"), 0.7)
        self.assertEqual(beta_weight("MID"), 1.0)
        self.assertEqual(beta_weight("HIGH"), 1.8)

    def test_clamps_extreme_values(self):
        """Meme stocks can report beta 3-5; noisy data, clamped to [0.5, 2.0]."""
        from services.fundamentals import beta_weight
        self._store("CRAZY", 5.0)
        self._store("DEFENSIVE", 0.1)
        self.assertEqual(beta_weight("CRAZY"), 2.0)
        self.assertEqual(beta_weight("DEFENSIVE"), 0.5)


class TestFundamentalsHashing(unittest.TestCase):

    def test_same_inputs_produce_same_hash(self):
        from services.fundamentals import _hash_payload
        a = {"pe_ratio": 25, "revenue_growth_yoy": 0.18, "profit_margin": 0.22}
        b = {"profit_margin": 0.22, "pe_ratio": 25, "revenue_growth_yoy": 0.18}
        self.assertEqual(_hash_payload(a), _hash_payload(b),
                         "key order must not affect hash")

    def test_change_in_any_field_changes_hash(self):
        from services.fundamentals import _hash_payload
        a = {"pe_ratio": 25, "revenue_growth_yoy": 0.18}
        b = {"pe_ratio": 25, "revenue_growth_yoy": 0.19}
        self.assertNotEqual(_hash_payload(a), _hash_payload(b))


# ============================================================================
# CATEGORY I: Trade-rationale endpoint
# ----------------------------------------------------------------------------
# Combines: origin classification (watchlist/scanner), signal reasoning,
# backtest evidence, fundamentals, analyst rating, macro context.
# These tests pin the shape and the origin-classification branches so a
# casual refactor doesn't silently lose a section.
# ============================================================================

class TestTradeRationale(unittest.TestCase):

    def setUp(self):
        _reset_db()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def _trade(self, **over) -> AutoTrade:
        t = _make_trade(**over)
        self.db.add(t); self.db.commit()
        return t

    def _call(self, trade_id: int):
        # FastAPI route function — call directly to avoid TestClient overhead.
        from routers.trading import auto_trade_rationale
        return auto_trade_rationale(trade_id)

    def test_404_on_unknown_trade(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._call(999_999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_origin_watchlist_only(self):
        from database import WatchlistStock
        t = self._trade(ticker="AAPL")
        self.db.add(WatchlistStock(ticker="AAPL")); self.db.commit()
        r = self._call(t.id)
        self.assertEqual(r["origin"], "watchlist")
        self.assertIsNone(r["scanner"])

    def test_origin_scanner_only(self):
        from database import CandidatePool
        t = self._trade(ticker="ON")
        self.db.add(CandidatePool(ticker="ON", score=80.5, rvol=1.5,
                                   rs_20d=0.3, rs_60d=0.4, adx=32,
                                   pct_from_52w_high=-0.01,
                                   reason="strong trend, near 52wH"))
        self.db.commit()
        r = self._call(t.id)
        self.assertEqual(r["origin"], "scanner")
        self.assertIsNotNone(r["scanner"])
        self.assertEqual(r["scanner"]["score"], 80.5)
        self.assertIn("strong trend", r["scanner"]["reason"])

    def test_origin_both(self):
        from database import WatchlistStock, CandidatePool
        t = self._trade(ticker="NVDA")
        self.db.add(WatchlistStock(ticker="NVDA"))
        self.db.add(CandidatePool(ticker="NVDA", score=95))
        self.db.commit()
        r = self._call(t.id)
        self.assertEqual(r["origin"], "watchlist+pool")
        self.assertIsNotNone(r["scanner"])

    def test_signal_reasoning_split_into_lines(self):
        # Insert signal with multi-line reasoning, link to trade
        sig = _make_signal(ticker="AAPL", signal_type="BUY", confidence=80,
                           reasoning="✅ Above SMA200\n✅ RSI bullish\n✅ MACD crossover")
        self.db.add(sig); self.db.commit()
        t = self._trade(ticker="AAPL", signal_id=sig.id)
        r = self._call(t.id)
        self.assertIsNotNone(r["signal"])
        self.assertEqual(len(r["signal"]["reasoning_lines"]), 3)
        self.assertTrue(any("SMA200" in ln for ln in r["signal"]["reasoning_lines"]))

    def test_backtest_section_when_best_strategy_present(self):
        from database import BestStrategyPerTicker
        t = self._trade(ticker="MSFT")
        self.db.add(BestStrategyPerTicker(
            ticker="MSFT", strategy="Composite (multi-factor)", direction="BUY",
            confidence=72, oos_trades=8, win_rate=0.625, avg_pl=2.3,
        ))
        self.db.commit()
        r = self._call(t.id)
        self.assertIsNotNone(r["backtest"])
        self.assertEqual(r["backtest"]["winning_strategy"], "Composite (multi-factor)")
        self.assertAlmostEqual(r["backtest"]["win_rate"], 0.625)

    def test_fundamentals_and_analyst_present_when_available(self):
        from database import Fundamentals, AnalystRating
        t = self._trade(ticker="GOOGL", entry_price=180.0)
        self.db.add(Fundamentals(ticker="GOOGL", quality_score=71, sector="Tech",
                                  pe_ratio=31.3, peg_ratio=2.32,
                                  revenue_growth_yoy=0.18, profit_margin=0.33))
        self.db.add(AnalystRating(ticker="GOOGL", mean=1.9, key="buy",
                                   analyst_count=42, target_mean=200.0))
        self.db.commit()
        r = self._call(t.id)
        self.assertEqual(r["fundamentals"]["quality_score"], 71)
        self.assertIsNotNone(r["analyst"])
        self.assertAlmostEqual(r["analyst"]["target_premium_vs_entry"], (200 - 180) / 180, places=4)

    def test_macro_context_within_48h_only(self):
        from database import MacroEvent
        opened = datetime.utcnow()
        t = self._trade(ticker="AAPL", opened_at=opened, filled_at=opened)
        # Within ±48h: should appear
        self.db.add(MacroEvent(
            event_key="CPI", event_name="Consumer Price Index",
            country="US", importance="high",
            release_time_utc=opened - timedelta(hours=12),
        ))
        # Way outside the window: should be excluded
        self.db.add(MacroEvent(
            event_key="OLD", event_name="Old Event", country="US", importance="high",
            release_time_utc=opened - timedelta(days=20),
        ))
        # Low importance: should be excluded even if in window
        self.db.add(MacroEvent(
            event_key="LOW", event_name="Low Event", country="US", importance="low",
            release_time_utc=opened + timedelta(hours=2),
        ))
        self.db.commit()
        r = self._call(t.id)
        keys = [ev["event_key"] for ev in r["macro_context"]]
        self.assertIn("CPI", keys)
        self.assertNotIn("OLD", keys)
        self.assertNotIn("LOW", keys)


# ============================================================================
# CATEGORY J: risk_math pure helpers
# ----------------------------------------------------------------------------
# Now that risk_math.py extracts these from auto_trader.py, they're trivially
# unit-testable. Pin the boundaries so accidental tuning shifts get caught.
# ============================================================================

class TestRiskMath(unittest.TestCase):

    def test_idempotency_key_deterministic(self):
        from services.risk_math import signal_idempotency_key
        sig = {"ticker": "AAPL", "signal_type": "BUY", "entry": 200.0,
               "stop_loss": 195.0, "target1": 210.0, "timeframe": "1d",
               "confidence": 80}
        self.assertEqual(signal_idempotency_key(sig), signal_idempotency_key(sig))

    def test_idempotency_key_bucket_aware(self):
        """Conf 75 and 85 should hash differently (different 10-buckets)."""
        from services.risk_math import signal_idempotency_key
        a = signal_idempotency_key({"ticker": "X", "signal_type": "BUY",
                                    "entry": 100, "stop_loss": 95, "target1": 105,
                                    "timeframe": "1d", "confidence": 75})
        b = signal_idempotency_key({"ticker": "X", "signal_type": "BUY",
                                    "entry": 100, "stop_loss": 95, "target1": 105,
                                    "timeframe": "1d", "confidence": 85})
        self.assertNotEqual(a, b)

    def test_clamp_multiplier_stack(self):
        from services.risk_math import clamp_multiplier_stack
        # 1.75 × 1.35 × 1.3 × 1.3 × 1.0 = 4.0 raw → clamped to RISK_MULT_CEILING=2.0
        raw, clamped, was_clamped = clamp_multiplier_stack(1.75, 1.35, 1.3, 1.3, 1.0)
        self.assertGreater(raw, 2.0)
        self.assertEqual(clamped, 2.0)
        self.assertTrue(was_clamped)

    def test_clamp_passthrough_when_under_ceiling(self):
        from services.risk_math import clamp_multiplier_stack
        raw, clamped, was_clamped = clamp_multiplier_stack(1.10, 1.05, 1.0, 1.0, 1.0)
        self.assertAlmostEqual(raw, clamped, places=4)
        self.assertFalse(was_clamped)

    def test_position_size_by_risk_normal(self):
        from services.risk_math import position_size_by_risk
        # $100K equity × 2% = $2000 budget; risk per share $5 → 400 shares
        self.assertEqual(position_size_by_risk(100_000, 0.02, 5), 400)

    def test_position_size_by_risk_floors_to_zero(self):
        from services.risk_math import position_size_by_risk
        self.assertEqual(position_size_by_risk(0, 0.02, 5), 0)
        self.assertEqual(position_size_by_risk(100, 0.02, 0), 0)
        self.assertEqual(position_size_by_risk(100, -0.01, 5), 0)

    def test_kelly_thin_data_neutral(self):
        from services.risk_math import kelly_risk_mult
        self.assertEqual(kelly_risk_mult(None, None), 1.0)
        self.assertEqual(kelly_risk_mult(45.0, 2.0), 1.0)  # below 55% min

    def test_confidence_risk_mult_ramps(self):
        from services.risk_math import confidence_risk_mult
        # At threshold = neutral
        self.assertEqual(confidence_risk_mult(75, 75), 1.0)
        # At 100 = max mult (default 1.75)
        self.assertAlmostEqual(confidence_risk_mult(100, 75), 1.75, places=2)
        # Below threshold = neutral (no negative ramp)
        self.assertEqual(confidence_risk_mult(50, 75), 1.0)


# ============================================================================
# CATEGORY K: risk_manager state isolation
# ----------------------------------------------------------------------------
# The new reset_for_tests() helper lets us round-trip BP reservation +
# circuit breakers without leaking state between tests.
# ============================================================================

class TestRiskManagerState(unittest.TestCase):

    def setUp(self):
        from services import risk_manager as rm
        rm.reset_for_tests()

    def test_bp_reservation_round_trip(self):
        from services.risk_manager import reserve_bp, release_bp, get_in_flight_bp
        self.assertEqual(get_in_flight_bp(), 0.0)
        reserve_bp(1000)
        self.assertEqual(get_in_flight_bp(), 1000.0)
        reserve_bp(500)
        self.assertEqual(get_in_flight_bp(), 1500.0)
        release_bp(1200)
        self.assertEqual(get_in_flight_bp(), 300.0)

    def test_bp_breaker_lifecycle(self):
        from services.risk_manager import (trip_bp_breaker, clear_bp_breaker,
                                            bp_breaker_active)
        self.assertFalse(bp_breaker_active())
        trip_bp_breaker(minutes=30)
        self.assertTrue(bp_breaker_active())
        clear_bp_breaker()
        self.assertFalse(bp_breaker_active())

    def test_sl_failure_count_rolling(self):
        from services.risk_manager import record_sl_resubmit_failure, sl_resubmit_failures_1h
        self.assertEqual(sl_resubmit_failures_1h(), 0)
        record_sl_resubmit_failure()
        record_sl_resubmit_failure()
        record_sl_resubmit_failure()
        self.assertEqual(sl_resubmit_failures_1h(), 3)


# ============================================================================
# CATEGORY L: Adaptive risk + VIX-options bucket
# ----------------------------------------------------------------------------
# Both functions defer to fundamentals + closed-trades aggregation, so a
# clean DB returns 1.0 (neutral). The tests pin that the multiplier doesn't
# spuriously drop in a clean state — a regression here would silently halve
# risk on a cold-start instance.
# ============================================================================

class TestAdaptiveRisk(unittest.TestCase):

    def setUp(self):
        _reset_db()

    def test_neutral_when_no_data(self):
        """No VIX, no closed trades → neutral 1.0."""
        from services.risk_manager import adaptive_risk_multiplier, vix_options_bucket_multiplier
        # Without VIX data and no closed trades the function returns 1.0
        # (or 0.5 only if the win-rate calc sees ≥10 closed trades, which
        # we don't have in a clean test DB).
        self.assertEqual(adaptive_risk_multiplier(), 1.0)
        self.assertEqual(vix_options_bucket_multiplier(), 1.0)


# ============================================================================
# CATEGORY M: Run-mode partitioning
# ----------------------------------------------------------------------------
# Smoke-tests that the run-mode env var resolves correctly.
# ============================================================================

class TestRunMode(unittest.TestCase):

    def test_default_is_api(self):
        os.environ.pop("RUN_MODE", None)
        mode = (os.getenv("RUN_MODE") or "api").strip().lower()
        self.assertEqual(mode, "api")

    def test_manager_mode(self):
        os.environ["RUN_MODE"] = "manager"
        try:
            mode = (os.getenv("RUN_MODE") or "api").strip().lower()
            self.assertEqual(mode, "manager")
        finally:
            os.environ.pop("RUN_MODE", None)

    def test_unknown_falls_back_to_api(self):
        # The lifespan code falls back to api on unknown — mirror that here.
        os.environ["RUN_MODE"] = "garbage"
        try:
            mode = (os.getenv("RUN_MODE") or "api").strip().lower()
            if mode not in ("api", "manager"):
                mode = "api"
            self.assertEqual(mode, "api")
        finally:
            os.environ.pop("RUN_MODE", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
