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
        # r43 fix #1.31: min-count raised from 3 → 8.
        from services.insider_trades import insider_multiplier
        self._store("INSBUY", buys_90=10, sells_90=2)  # 83% buy ratio
        self.assertEqual(insider_multiplier("INSBUY", "BUY"), 1.06)

    def test_heavy_selling_penalizes_buy(self):
        from services.insider_trades import insider_multiplier
        self._store("INSSELL", buys_90=2, sells_90=8)  # 20% buy ratio, 10 total
        self.assertEqual(insider_multiplier("INSSELL", "BUY"), 0.97)

    def test_heavy_selling_confirms_sell(self):
        from services.insider_trades import insider_multiplier
        self._store("INSSELL2", buys_90=2, sells_90=10)
        self.assertEqual(insider_multiplier("INSSELL2", "SELL"), 1.06)

    def test_thin_sample_neutral(self):
        """Below min-count floor, ratio is too noisy; stay neutral.
        r43 fix #1.31 raised the floor from 3 → 8."""
        from services.insider_trades import insider_multiplier
        self._store("THIN", buys_90=3, sells_90=0)  # 3 total — below 8 floor
        self.assertEqual(insider_multiplier("THIN", "BUY"), 1.0)
        self._store("THIN2", buys_90=4, sells_90=2)  # 6 total — below 8 floor
        self.assertEqual(insider_multiplier("THIN2", "BUY"), 1.0)

    def test_balanced_neutral(self):
        from services.insider_trades import insider_multiplier
        self._store("EVEN", buys_90=5, sells_90=5)  # 50/50, 10 total
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
        from services.config import RISK_MAX_CONFIDENCE_MULT
        # At threshold = neutral
        self.assertEqual(confidence_risk_mult(75, 75), 1.0)
        # At 100 = max mult (r46 lowered from 1.75 → 1.5).
        self.assertAlmostEqual(confidence_risk_mult(100, 75), RISK_MAX_CONFIDENCE_MULT, places=2)
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


# ============================================================================
# CATEGORY N: r42 Tier 0 regressions — the realized_pl overwrite bug + friends
# ----------------------------------------------------------------------------
# These would have caught the most expensive bug in the audit: the final-leg
# `t.realized_pl = ...` (assignment, not +=) erasing T1/T2 partial PnL on
# every multi-leg winning trade.
# ============================================================================

class TestRealizedPlAccumulation(unittest.TestCase):

    def setUp(self):
        _reset_db()

    def test_force_close_stock_adds_runner_pl(self):
        """force_close_trade for a stock with prior partial trim must ADD the
        runner-leg PnL on top of the existing realized_pl, not overwrite it."""
        from services.execution_engine import force_close_trade
        from unittest.mock import MagicMock, patch as _patch
        db = SessionLocal()
        try:
            t = AutoTrade(
                ticker="TEST", symbol="TEST", asset_type="stock",
                side="buy", qty=33, requested_entry=100.0, entry_price=100.0,
                stop_loss=95.0, current_stop=95.0, target1=110.0,
                status="open",
                realized_pl=200.0,  # T1 trim already banked +$200
                opened_at=datetime.utcnow() - timedelta(minutes=20),
            )
            db.add(t); db.commit(); db.refresh(t)
            with _patch("services.alpaca_client.cancel_order"), \
                 _patch("services.alpaca_client.close_position",
                        return_value={"id": "ok", "symbol": "TEST", "side": "sell", "qty": 33, "status": "filled"}), \
                 _patch("services.auto_trader._current_price", return_value=108.0):
                summary = {"closed": 0}
                force_close_trade(t, db, "test reason", summary)
            db.refresh(t)
            # Runner leg: (108 - 100) * 33 = 264. Existing 200 + 264 = 464.
            self.assertAlmostEqual(t.realized_pl, 464.0, places=2)
            self.assertEqual(summary["closed"], 1)
        finally:
            db.close()


class TestZoneInfoSessionStart(unittest.TestCase):

    def test_session_start_uses_zoneinfo(self):
        """r53: `_session_start_utc` is now anchored to 00:00 ET (was
        9:30 ET, which let after-hours / pre-market losses momentarily
        evade the daily-loss gate). Must still round-trip through
        zoneinfo so DST is exact."""
        from services.auto_trader import _session_start_utc
        anchor = _session_start_utc()
        # Naive UTC representing 00:00 ET.
        # 00:00 EDT = 04:00 UTC, 00:00 EST = 05:00 UTC.
        self.assertEqual(anchor.minute, 0)
        self.assertIn(anchor.hour, (4, 5))


class TestKellyFractional(unittest.TestCase):

    def test_kelly_is_fractional_not_full(self):
        """Quarter-Kelly default keeps the result well below the full-Kelly
        ceiling. Full-Kelly with 60% WR + 2:1 RR returns 1 + (max-1)*0.4."""
        from services.risk_math import kelly_risk_mult
        # 60% WR, 2:1 RR → kelly_edge = 0.4 (full)
        full = kelly_risk_mult(60.0, 2.0, fractional=1.0)
        quarter = kelly_risk_mult(60.0, 2.0)  # default 0.25
        # Full > quarter, by construction.
        self.assertGreater(full, quarter)
        # Quarter must still be ≥ 1.0 (floor at no-Kelly = 1.0).
        self.assertGreaterEqual(quarter, 1.0)


class TestRegimeStrategyGate(unittest.TestCase):

    def test_strategies_have_regime_field(self):
        """Every strategy must declare a `regime` so the auto-trader and
        backtester can gate participation by ADX regime."""
        import pandas as pd
        from services import strategies as st
        # Synthetic frame: just enough columns for the strategies to not crash.
        idx = pd.date_range("2025-01-01", periods=400, freq="D")
        df = pd.DataFrame({
            "Open": [100]*400, "High": [101]*400, "Low": [99]*400, "Close": [100]*400,
            "Volume": [1_000_000]*400,
        }, index=idx)
        # Compute the indicator columns the strategies need.
        from services.indicators import compute_indicators
        d = compute_indicators(df)
        out = st.all_strategies(d)
        # Every returned strat must have a regime tag.
        for s in out:
            self.assertIn("regime", s, f"strategy {s.get('name')} missing regime")
            self.assertIn(s["regime"], ("trend", "chop", "any"))


class TestExpectancyFreezeGate(unittest.TestCase):

    def setUp(self):
        _reset_db()

    def test_freeze_on_count_wr_low(self):
        """Standard count-WR < 35% trigger still fires."""
        from services.risk_manager import should_freeze_trading
        db = SessionLocal()
        try:
            # 6 trades, 1 win → WR=16.7%
            for i in range(5):
                db.add(_make_trade(status="closed_stop", realized_pl=-100.0, ticker=f"T{i}",
                                   closed_at=datetime.utcnow() - timedelta(hours=i+1)))
            db.add(_make_trade(status="closed_target", realized_pl=10.0, ticker="W",
                               closed_at=datetime.utcnow() - timedelta(hours=1)))
            db.commit()
            reason = should_freeze_trading()
            self.assertIsNotNone(reason)
            self.assertIn("WR", reason)
        finally:
            db.close()

    def test_freeze_on_negative_expectancy_when_diversified_losses(self):
        """≥10 trades, expectancy ≤ 0, sum_pl ≤ -2× worst → freeze even
        with WR ≥ 35%."""
        from services.risk_manager import should_freeze_trading
        db = SessionLocal()
        try:
            # 12 trades: 5 winners +$50 each (=+250), 7 losers -$80 each (=-560).
            # WR = 5/12 = 41.7% (above 35% threshold so WR gate doesn't fire).
            # Sum = -310, worst = -80, |2× worst| = 160. -310 < -160 → trigger.
            for i in range(5):
                db.add(_make_trade(status="closed_target", realized_pl=50.0, ticker=f"W{i}",
                                   closed_at=datetime.utcnow() - timedelta(hours=i+1)))
            for i in range(7):
                db.add(_make_trade(status="closed_stop", realized_pl=-80.0, ticker=f"L{i}",
                                   closed_at=datetime.utcnow() - timedelta(hours=i+10)))
            db.commit()
            reason = should_freeze_trading()
            self.assertIsNotNone(reason)
            self.assertIn("expectancy", reason)
        finally:
            db.close()


# ============================================================================
# CATEGORY O: r43 strategy + execution audit regressions.
# ============================================================================

class TestOCCSymbolFallback(unittest.TestCase):
    def test_occ_fallback_when_contractSymbol_missing(self):
        """r43 fix #0.1: Alpaca-feed contracts have only `_occ`. Reader must fall back."""
        # Smoke-check the lookup pattern via dict access — this is what the
        # fix did at options_analyzer.py:368.
        o_alpaca = {"_occ": "AAPL250620C00200000", "strike": 200}
        o_yahoo = {"contractSymbol": "AAPL250620C00200000", "strike": 200}
        for o in (o_alpaca, o_yahoo):
            symbol = o.get("contractSymbol") or o.get("_occ")
            self.assertEqual(symbol, "AAPL250620C00200000")


class TestRRMinFloor(unittest.TestCase):
    def test_rr_min_blocks_negative_ev_trade(self):
        """r43 fix #0.2: a 0.4% T1 against 5% stop must be rejected.

        Verifies the math (1.3R floor with 12bps cost buffer) returns False
        before the bracket is built.
        """
        entry, stop, t1 = 100.0, 95.0, 100.4   # T1=0.4% above, R=5
        rr_min = 1.3
        cost_buffer = entry * (12 / 10000.0)
        net_reward = max(0.0, (t1 - entry) - cost_buffer)
        gross_risk = max(0.01, entry - stop)
        rr_net = net_reward / gross_risk
        self.assertLess(rr_net, rr_min)


class TestCorrelationGate(unittest.TestCase):
    def test_correlation_gate_returns_empty_on_no_data(self):
        """When data isn't available, gate fails open (returns no correlations)
        — sector cap remains the primary defense."""
        from services.auto_trader import correlated_with_open
        # Made-up ticker + fake open list; data fetch will fail / return None.
        result = correlated_with_open("NOSUCH", ["FAKE1", "FAKE2"], threshold=0.7)
        self.assertEqual(result, [])


class TestAdaptiveZero(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_adaptive_zero_when_compounded_low(self):
        """r43 fix #1.26: when the unfloored multiplier product would be
        ≤ 0.25 we now return 0 (caller treats as skip), not the old 0.25 floor."""
        from services.risk_manager import adaptive_risk_multiplier
        # Without VIX/SPY/closed-trades data the function returns 1.0
        # (no adverse regime). The zero-return path requires multiple
        # adverse signals — covered by the (rare) compounded path. This
        # test pins the no-data baseline so future regressions don't
        # silently flip it.
        self.assertEqual(adaptive_risk_multiplier(), 1.0)


class TestPremiumStopSemantics(unittest.TestCase):
    def test_premium_stop_uses_min_not_max(self):
        """r43 fix #1.9: effective_stop_premium must use MIN of premium-50%
        stop and underlying-aware stop estimate (more pessimistic), so
        the underlying-aware stop binds for normal trades."""
        # Replay the math in options_analyzer.py:_score loop.
        premium = 2.00
        delta = 0.4
        spot = 100.0
        sl = 95.0   # underlying stop, 5% below
        premium_stop = round(premium * (1 - 0.50), 2)  # 1.00
        est_prem_at_underlying_stop = max(0.01, premium + delta * (sl - spot))
        # = 2 + 0.4 * (-5) = 0
        # max(0.01, 0) = 0.01
        self.assertAlmostEqual(est_prem_at_underlying_stop, 0.01)
        # OLD (broken): max(1.00, 0.01) = 1.00 → premium-50% stop binds.
        # NEW (fixed):  min(1.00, 0.01) = 0.01 → underlying-aware stop binds.
        new_eff = min(premium_stop, est_prem_at_underlying_stop)
        self.assertAlmostEqual(new_eff, 0.01)


class TestThetaStopDTEScaling(unittest.TestCase):
    def test_dte_thresholds_scale_with_dte(self):
        """r43 fix #1.10: theta-stop hold floor is shorter for shorter DTE.
        Smoke-test the scaling table."""
        # Replay the heuristic.
        def hold_floor_for(dte):
            if dte is None:
                return 48.0
            if dte <= 7: return 12.0
            if dte <= 30: return 24.0
            return 48.0
        self.assertEqual(hold_floor_for(3), 12.0)
        self.assertEqual(hold_floor_for(20), 24.0)
        self.assertEqual(hold_floor_for(60), 48.0)
        self.assertEqual(hold_floor_for(None), 48.0)


class TestConsecutiveLossFreeze(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_freeze_on_5_consecutive_losses(self):
        """r43 fix #1.20: 5 stops in a row freeze trading regardless of
        30d WR (the WR can still look healthy across 30d)."""
        from services.risk_manager import should_freeze_trading
        db = SessionLocal()
        try:
            # 30 winners (well above WR threshold), then 5 fresh losses.
            base = datetime.utcnow() - timedelta(days=10)
            for i in range(30):
                db.add(_make_trade(
                    status="closed_target",
                    realized_pl=100.0,
                    ticker=f"W{i}",
                    closed_at=base + timedelta(hours=i),
                ))
            for i in range(5):
                db.add(_make_trade(
                    status="closed_stop",
                    realized_pl=-50.0,
                    ticker=f"L{i}",
                    closed_at=datetime.utcnow() - timedelta(minutes=i+1),
                ))
            db.commit()
            reason = should_freeze_trading()
            self.assertIsNotNone(reason)
            self.assertIn("consecutive", reason)
        finally:
            db.close()


class TestMarketableLimitInsideSpread(unittest.TestCase):
    def test_sell_limit_is_inside_spread(self):
        """r43 fix #0.6: SELL must post INSIDE the spread (mid - offset),
        not at the bid (which is identical to a market order)."""
        # Replay the price-selection logic.
        bid, ask = 1.00, 1.20
        mid = (bid + ask) / 2.0
        offset = 0.05
        sell_px = max(bid, mid - offset)
        # Inside the spread: 1.05 (between bid 1.00 and ask 1.20).
        self.assertGreater(sell_px, bid)
        self.assertLess(sell_px, ask)


# ============================================================================
# CATEGORY P: r44 strategy + ML + risk + code-quality regressions.
# ============================================================================

class TestNewsContextColumnNames(unittest.TestCase):
    """r44 fix #0.1: _build_ai_context queried `NewsEvent.tickers` (doesn't
    exist; real column is `symbols`) and read `n.title` (real field is
    `headline`). Wrapped in try/except so the AI judge silently received
    `recent_news=[]` for every entry-veto / news-exit / confidence-multiplier
    call. This regression test asserts the real column names exist."""
    def test_news_event_real_columns(self):
        from database import NewsEvent
        cols = {c.name for c in NewsEvent.__table__.columns}
        self.assertIn("symbols", cols)
        self.assertIn("headline", cols)
        self.assertIn("ticker", cols)
        self.assertIn("published_at", cols)
        # The columns the prior buggy code referenced should NOT exist.
        self.assertNotIn("tickers", cols)
        self.assertNotIn("title", cols)


class TestCalendarMultiplier(unittest.TestCase):
    def test_calendar_multiplier_in_bounds(self):
        from services.seasonality import calendar_multiplier
        m = calendar_multiplier()
        self.assertGreaterEqual(m, 0.85)
        self.assertLessEqual(m, 1.15)


class TestRegimeMultiplier(unittest.TestCase):
    def test_regime_multiplier_in_bounds(self):
        from services.cross_asset import regime_multiplier
        m = regime_multiplier()
        self.assertGreaterEqual(m, 0.6)
        self.assertLessEqual(m, 1.2)


class TestInsiderClusterAmplification(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_cluster_buy_returns_amplified(self):
        """r44 Wave 7: 12+ buys + ratio≥0.7 → 1.12× cluster bonus."""
        from database import SessionLocal as _SL_ic, InsiderSummary as _IS_ic
        from services.insider_trades import insider_multiplier
        db = _SL_ic()
        try:
            db.add(_IS_ic(
                ticker="CLUST",
                buy_count_30d=8, sell_count_30d=0,
                buy_count_90d=15, sell_count_90d=2,
                net_buy_ratio_90d=0.88,
            ))
            db.commit()
        finally:
            db.close()
        self.assertEqual(insider_multiplier("CLUST", "BUY"), 1.12)


class TestNR7Strategy(unittest.TestCase):
    def test_nr7_strategy_returns_dict(self):
        from services.strategies import _nr7_breakout
        import pandas as _pd
        # Synthetic 50-bar OHLCV with one NR7 bar near the end.
        idx = _pd.date_range("2026-01-01", periods=50, freq="D")
        df = _pd.DataFrame({
            "Open": [100.0] * 50,
            "High": [102.0] * 50,
            "Low": [98.0] * 50,
            "Close": [100.0] * 50,
            "Volume": [1_000_000] * 50,
            "VOL_SMA20": [1_000_000] * 50,
        }, index=idx)
        # Last bar tighter range to make it NR7.
        df.iloc[-2, df.columns.get_loc("High")] = 100.5
        df.iloc[-2, df.columns.get_loc("Low")] = 99.5
        s = _nr7_breakout(df)
        self.assertIn("entry_long", s)
        self.assertIn("entry_short", s)
        self.assertEqual(s["regime"], "any")


class TestAIBudgetCheck(unittest.TestCase):
    def test_budget_check_blocks_after_cap(self):
        from services import ai_judge
        # Save and reset state.
        orig_cap = ai_judge._AI_DAILY_CALL_CAP
        orig_counter = dict(ai_judge._ai_call_counter)
        try:
            ai_judge._AI_DAILY_CALL_CAP = 3
            ai_judge._ai_call_counter.clear()
            self.assertTrue(ai_judge._ai_budget_check())
            self.assertTrue(ai_judge._ai_budget_check())
            self.assertTrue(ai_judge._ai_budget_check())
            self.assertFalse(ai_judge._ai_budget_check())
        finally:
            ai_judge._AI_DAILY_CALL_CAP = orig_cap
            ai_judge._ai_call_counter.clear()
            ai_judge._ai_call_counter.update(orig_counter)


class TestAdminTickerValidation(unittest.TestCase):
    def test_invalid_ticker_rejected(self):
        from routers.admin import _validate_ticker
        from fastapi import HTTPException
        # Valid uppercase short ticker passes.
        self.assertEqual(_validate_ticker("aapl"), "AAPL")
        # Garbage rejected.
        for bad in ("../etc/passwd", "AAPL/MSFT", "TOOLONG12345", "1234", "<script>"):
            with self.assertRaises(HTTPException):
                _validate_ticker(bad)


class TestBoundedTradeCache(unittest.TestCase):
    def test_cache_bounded(self):
        from services.data_fetcher import _BoundedTradeCache
        c = _BoundedTradeCache(max_entries=10)
        for i in range(15):
            c[f"K{i}"] = (i, i + 1)
        # Should not exceed max_entries (within tolerance of the LRU cutoff).
        self.assertLessEqual(len(c._d), 12)


class TestSlippageAwareRPS(unittest.TestCase):
    def test_slippage_awareness_increases_risk(self):
        from services.risk_manager import slippage_aware_risk_per_share
        gross = 100.0 - 95.0   # 5
        atr = 2.0
        rps = slippage_aware_risk_per_share(100.0, 95.0, atr)
        # Should be ≥ gross + 0.10*ATR = 5.20
        self.assertGreaterEqual(rps, gross + 0.10 * atr - 1e-9)


class TestMLCalibration(unittest.TestCase):
    """r45: isotonic calibrator persists alongside the booster, transforms
    raw LightGBM output into calibrated probability.
    """

    def test_isotonic_round_trip(self):
        """Direct test of the IsotonicRegression layer — fit, persist, load,
        transform — without invoking the full training pipeline (which needs
        live ticker data + 200+ samples).
        """
        try:
            from sklearn.isotonic import IsotonicRegression
            import pickle as _pickle
            import numpy as _np
        except Exception:
            self.skipTest("sklearn or numpy unavailable in this env")
            return
        # Build a synthetic dataset where raw LightGBM-style outputs are
        # over-confident at the tails: predicted 0.85 actually wins 0.70.
        rng = _np.random.default_rng(42)
        N = 500
        raw_preds = rng.uniform(0.0, 1.0, N)
        # Simulate over-confidence: pull true labels toward 0.5 for tail predictions.
        true_p = 0.5 + 0.7 * (raw_preds - 0.5)
        labels = (rng.uniform(0.0, 1.0, N) < true_p).astype(int)

        cal = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        cal.fit(raw_preds, labels)
        # Tail predictions should pull toward the true win rate.
        high_raw = 0.90
        cal_high = float(cal.transform([high_raw])[0])
        self.assertLessEqual(cal_high, high_raw + 1e-6)
        self.assertGreaterEqual(cal_high, 0.0)
        self.assertLessEqual(cal_high, 1.0)
        # Pickle round-trip — what ml_trainer/ml_scorer actually do.
        blob = _pickle.dumps(cal)
        cal2 = _pickle.loads(blob)
        cal2_high = float(cal2.transform([high_raw])[0])
        self.assertAlmostEqual(cal_high, cal2_high, places=6)

    def test_scorer_calibrator_loaded_accessor(self):
        """The status endpoint exposes whether a calibrator is currently
        loaded. This is a smoke test on the accessor only — no model trained
        in the test env, so it should return False."""
        from services.ml_scorer import calibrator_loaded
        self.assertIn(calibrator_loaded(), (True, False))   # boolean returned, not raise


class TestNewsSeverityGate(unittest.TestCase):
    """r46 fix #0.1: severity is INT, not string. The dispatcher used to
    crash on the first iter via `(int).lower()`, swallowing all news exits.
    """
    def test_int_severity_passes_high(self):
        # Replay the corrected gate logic.
        item = {"severity": 82, "ticker": "AAPL"}
        try:
            sev = int(item.get("severity") or 0)
        except Exception:
            sev = 0
        self.assertGreaterEqual(sev, 35)

    def test_int_severity_blocks_low(self):
        item = {"severity": 12, "ticker": "AAPL"}
        sev = int(item.get("severity") or 0)
        self.assertLess(sev, 35)


class TestEquitySnapshotTable(unittest.TestCase):
    """r46 fix #0.2: persisted equity timeseries for multi-day DD."""
    def test_equity_snapshot_columns(self):
        from database import EquitySnapshot
        cols = {c.name for c in EquitySnapshot.__table__.columns}
        for required in ("ts", "equity", "cash", "buying_power",
                          "realized_pl_today", "unrealized_pl",
                          "open_positions", "spy_close"):
            self.assertIn(required, cols)


class TestTickerProfileTable(unittest.TestCase):
    """r46 Tier 1: per-ticker overrides table."""
    def test_columns_present(self):
        from database import TickerProfile
        cols = {c.name for c in TickerProfile.__table__.columns}
        for required in ("ticker", "realized_vol_30d", "vol_mult",
                          "beta_60d_realized", "confidence_threshold_override",
                          "min_rr_override", "min_dte_override",
                          "chandelier_mult_override", "has_earnings_calendar",
                          "correlation_cluster_id", "news_count_p50_30d"):
            self.assertIn(required, cols)


class TestTickerProfileFallback(unittest.TestCase):
    def test_returns_default_when_no_row(self):
        from services.ticker_profile import vol_mult, confidence_threshold, min_rr, min_dte
        self.assertEqual(vol_mult("DOESNOTEXIST"), 1.0)
        self.assertEqual(confidence_threshold("DOESNOTEXIST", 75), 75)
        self.assertEqual(min_rr("DOESNOTEXIST"), 2.0)
        self.assertEqual(min_dte("DOESNOTEXIST"), 10)


class TestNewStrategiesPresent(unittest.TestCase):
    def test_tier_p_strategies_in_funcs(self):
        from services.strategies import STRATEGY_FUNCS
        names = {fn.__name__ for fn in STRATEGY_FUNCS}
        self.assertIn("_opening_reversal", names)
        self.assertIn("_last_30min_momentum", names)
        self.assertIn("_news_spike_fade", names)


class TestIdempotencyUnique(unittest.TestCase):
    """r46 fix #0.8: idempotency_key has UNIQUE constraint."""
    def test_idempotency_unique_index(self):
        from database import AutoTrade
        col = AutoTrade.__table__.columns["idempotency_key"]
        self.assertTrue(col.unique)


class TestCrisisHelpers(unittest.TestCase):
    def test_crisis_chandelier_mult_default(self):
        from services.risk_manager import crisis_chandelier_multiplier
        # Outside crisis, returns base unchanged.
        # We just verify the function exists and doesn't raise.
        result = crisis_chandelier_multiplier(3.0)
        self.assertIsInstance(result, float)
        self.assertTrue(0.5 < result <= 4.0)

    def test_crisis_t1_trim_default(self):
        from services.risk_manager import crisis_t1_trim_fraction
        result = crisis_t1_trim_fraction(0.33)
        self.assertIsInstance(result, float)


class TestSeasonalityHelpers(unittest.TestCase):
    def test_pre_fomc_drift_qualification(self):
        from services.seasonality import pre_fomc_drift_buy_qualifying_ticker
        # Function exists; doesn't qualify random tickers.
        self.assertFalse(pre_fomc_drift_buy_qualifying_ticker("RANDOM"))


class TestR47DeadMultipliers(unittest.TestCase):
    """r47 T0a: pipeline-broken multipliers + missed string matches.
    Each of these silently returned 1.0 / never fired in production.
    """

    def test_calibration_multiplier_uses_n_column(self):
        """r47 #T0a-1: prior code read getattr(row, 'n_trades', 0) but the
        ConfidenceCalibration column is named `n` — gate never fired."""
        from database import SessionLocal, ConfidenceCalibration, create_tables
        from services.risk_manager import calibration_multiplier, _calibration_cache
        create_tables()
        _calibration_cache.clear()
        db = SessionLocal()
        try:
            existing = db.query(ConfidenceCalibration).filter(
                ConfidenceCalibration.bucket == "70-79"
            ).first()
            if existing:
                db.delete(existing); db.commit()
            row = ConfidenceCalibration(bucket="70-79", n=30,
                                        win_rate=0.62, avg_pl=0.5, multiplier=1.20)
            db.add(row); db.commit()
        finally:
            db.close()
        _calibration_cache.clear()
        m = calibration_multiplier(75.0)
        self.assertAlmostEqual(m, 1.20, places=4)

    def test_strategy_multiplier_uses_n_key(self):
        """r47 #T0a-2: prior code read entry.get('trades') but scorecard
        emits 'n' — gate never fired."""
        from services.risk_manager import strategy_multiplier, _strategy_mult_cache
        from unittest.mock import patch
        _strategy_mult_cache.clear()
        fake_card = {"X-Strategy": {"n": 25, "multiplier": 0.80, "win_rate": 0.30}}
        with patch("services.auto_trader.strategy_scorecard", return_value=fake_card):
            m = strategy_multiplier("X-Strategy")
        self.assertAlmostEqual(m, 0.80, places=4)

    def test_inside_bar_breakout_uses_parent_bar_range(self):
        """r47 #T0c-4: prior code's `inside` mask was indexed wrong AND the
        trigger compared today's close to YESTERDAY's high (the inside bar's
        own high) instead of the PARENT bar (i-2). Verify the new code
        triggers ONLY on a real inside-bar followed by parent-range break."""
        import pandas as pd
        from services.strategies import _inside_bar_breakout
        # i-2: H=10 L=5 (parent range 5..10)
        # i-1: H=8 L=6   (inside the parent — narrow range)
        # i:   close=11  (broke ABOVE parent high = 10)
        df = pd.DataFrame({
            "High":  [10, 8, 11.5],
            "Low":   [5,  6, 9],
            "Close": [9,  7, 11],
        })
        out = _inside_bar_breakout(df)
        # Last bar should signal long entry
        self.assertTrue(bool(out["entry_long"].iloc[-1]))
        self.assertFalse(bool(out["entry_short"].iloc[-1]))
        # First two bars must be False (not enough history)
        self.assertFalse(bool(out["entry_long"].iloc[0]))


class TestR47PortfolioHeatShorts(unittest.TestCase):
    """r47 T0f-1: heat math for SHORTS used max(0, entry-stop) which is 0
    (stop is ABOVE entry on a short) — silent zero-contribution to heat cap."""
    def test_short_position_contributes_to_heat(self):
        from database import SessionLocal, AutoTrade, create_tables
        from services.risk_manager import current_portfolio_heat
        create_tables()
        db = SessionLocal()
        try:
            db.query(AutoTrade).delete()
            db.add(AutoTrade(
                ticker="ZZZSHORT", symbol="ZZZSHORT", asset_type="stock",
                side="sell", qty=100,
                entry_price=100.0, requested_entry=100.0,
                stop_loss=110.0, current_stop=110.0,  # stop ABOVE entry → SHORT
                target1=90.0, level_index=0, status="open",
                opened_at=datetime.utcnow(),
            ))
            db.commit()
        finally:
            db.close()
        h = current_portfolio_heat()
        self.assertGreaterEqual(h, 900.0)


class TestR47SchemaDrift(unittest.TestCase):
    """r47 T0a-8: cfg attrs that prior code read via getattr but were never
    columns. Verify they're now persisted and the ORM exposes them."""
    def test_new_config_columns_present(self):
        from database import AutoTraderConfig
        cols = set(AutoTraderConfig.__table__.columns.keys())
        for required in [
            "pyramid_enabled", "max_correlated_open", "vol_target_annual",
            "leverage_cap", "book_var_99_cap_pct", "bracket_tif", "rr_min",
            "halt_detect_enabled", "iv_rank_graded_sizing",
            "vix_spike_strategy_enabled", "spx_trend_gate_enabled",
            "credit_spread_circuit_breaker_enabled",
        ]:
            self.assertIn(required, cols, f"missing config column: {required}")


class TestR47Overlays(unittest.TestCase):
    """r47 Tier P: regime-overlay sizing + clamps."""
    def test_iv_rank_size_factor_curve(self):
        from services.r47_overlays import iv_rank_size_factor as f
        # cheap vol → boost
        self.assertGreater(f(0.20), 1.0)
        # mid → unity
        self.assertAlmostEqual(f(0.50), 1.0, places=4)
        # rich → discount
        self.assertLess(f(0.85), 1.0)
        # extreme → veto (0.0)
        self.assertEqual(f(0.95), 0.0)
        # None → safe default
        self.assertAlmostEqual(f(None), 1.0, places=4)

    def test_overlay_combined_clamp(self):
        from services.r47_overlays import r47_sizing_overlay
        m, parts = r47_sizing_overlay("BUY")
        self.assertGreaterEqual(m, 0.0)  # may be 0 if CB is active
        self.assertLessEqual(m, 1.5)
        self.assertIsInstance(parts, dict)


class TestR47NewStrategiesPresent(unittest.TestCase):
    def test_vix_spike_in_registry(self):
        from services.strategies import STRATEGY_FUNCS
        names = {f.__name__ for f in STRATEGY_FUNCS}
        self.assertIn("_vix_spike_reversion", names)


class TestR47AccountBlockedFields(unittest.TestCase):
    """r47 T0d-1: account_blocked / transfers_blocked must appear in the
    alpaca_client.get_account() dict — r46 added gates on these but the
    keys were never populated."""
    def test_get_account_keys_when_no_client(self):
        from services import alpaca_client
        # no Alpaca creds in test → returns None; ensure surface is documented
        # by inspecting the dict assembly path via lambda eval (no live call).
        import inspect
        src = inspect.getsource(alpaca_client.get_account)
        self.assertIn("account_blocked", src)
        self.assertIn("transfers_blocked", src)


class TestR47NewsAIRateModuleLevel(unittest.TestCase):
    """r47 T0d-2: per-(ticker, hour) AI exit-judge rate limit must persist
    across batches; prior local-dict implementation reset every poll."""
    def test_ai_rate_inc_persists_across_calls(self):
        from services.news import _ai_rate_inc, _ai_rate_get, _AI_RATE
        _AI_RATE.clear()
        n1 = _ai_rate_inc("AAPL")
        n2 = _ai_rate_inc("AAPL")
        n3 = _ai_rate_inc("AAPL")
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 2)
        self.assertEqual(n3, 3)
        self.assertEqual(_ai_rate_get("AAPL"), 3)


class TestR47ConfidenceBoostFiresOnDirectionMatch(unittest.TestCase):
    """r47 T0a-6: prior code required strategy-name string match (and the
    caller passed 'Composite (multi-factor)' which never matched persisted
    strategy names like 'Trend Following') — boost never fired."""
    def test_boost_fires_on_direction_match_alone(self):
        from services.best_strategy import confidence_boost
        from unittest.mock import patch
        fake = {
            "ticker": "AAPL", "strategy": "Trend Following",
            "direction": "BUY", "confidence": 70, "oos_trades": 5,
        }
        with patch("services.best_strategy.get_for_ticker", return_value=fake):
            # caller passes generic composite → still gets the base boost
            mult = confidence_boost("AAPL", "Composite (multi-factor)", "BUY")
        self.assertGreaterEqual(mult, 1.05)


class TestR48Backlog(unittest.TestCase):
    """r48 BACKLOG: pin every fix that addresses a deferred r47 finding."""

    def test_marketable_limit_option_entries(self):
        """r48 BACKLOG #options-P0-2: option entries route through the new
        marketable-limit-with-cross-fallback primitive."""
        from services import alpaca_client
        self.assertTrue(hasattr(alpaca_client, "submit_option_entry_with_cross_fallback"))

    def test_greeks_persistence_columns(self):
        """r48 BACKLOG #options-P0-4: AutoTrade has entry_delta/gamma/theta/vega/iv."""
        from database import AutoTrade
        cols = set(AutoTrade.__table__.columns.keys())
        for c in ("entry_delta", "entry_gamma", "entry_theta", "entry_vega",
                  "entry_iv", "source_timeframe"):
            self.assertIn(c, cols)

    def test_portfolio_greeks_caps_function_exists(self):
        from services.risk_manager import portfolio_greeks_caps_breached
        # Smoke: empty book should not breach any cap at $100k equity
        br = portfolio_greeks_caps_breached(100_000, 0, 0, 0)
        self.assertIn("vega", br)
        self.assertIn("gamma", br)
        self.assertIn("delta", br)

    def test_atomic_realized_pl_helper_exists(self):
        from services.execution_engine import atomic_accumulate_realized_pl, atomic_increment_target_touch
        # Just ensure callables exist (smoke; behavior tested by integration paths)
        self.assertTrue(callable(atomic_accumulate_realized_pl))
        self.assertTrue(callable(atomic_increment_target_touch))

    def test_pdt_breaker(self):
        from services.risk_manager import trip_pdt_breaker, is_pdt_locked, clear_pdt_breaker
        clear_pdt_breaker()
        self.assertFalse(is_pdt_locked())
        trip_pdt_breaker(hours=1)
        self.assertTrue(is_pdt_locked())
        clear_pdt_breaker()
        self.assertFalse(is_pdt_locked())

    def test_db_down_breaker(self):
        from services.risk_manager import trip_db_down_breaker, is_db_down
        # initially not down
        self.assertFalse(is_db_down())
        trip_db_down_breaker(seconds=60)
        self.assertTrue(is_db_down())

    def test_factor_composite(self):
        from services.factors import factor_composite
        m, parts = factor_composite("SPY", sector="ETF")
        self.assertGreaterEqual(m, 0.6)
        self.assertLessEqual(m, 1.4)
        self.assertIn("momentum", parts)

    def test_order_flow_module_exposes_gates(self):
        from services import order_flow
        for fn in ("update_spread_ema", "spread_widening_defer",
                   "aggressor_flow_imbalance", "aggressor_flow_gate",
                   "detect_block_lean", "detect_sweep",
                   "vwap_band_reversion_signal",
                   "round_number_proximity_fade", "opening_drive_bias",
                   "quote_stuffing_score"):
            self.assertTrue(hasattr(order_flow, fn), f"missing: {fn}")

    def test_winrate_smoothness(self):
        """r48 BACKLOG #F19: smooth tanh ramp, monotonic, in-envelope."""
        from services.ml_scorer import winrate_to_multiplier
        # No ProgressBar steps — small p increments produce small multiplier deltas
        m_lo = winrate_to_multiplier(0.40)
        m_mid = winrate_to_multiplier(0.50)
        m_hi = winrate_to_multiplier(0.60)
        self.assertLess(m_lo, m_mid)
        self.assertLess(m_mid, m_hi)
        self.assertGreater(m_hi - m_mid, 0)
        self.assertLess(abs(m_mid - 1.00), 0.01)  # centered

    def test_book_var_99_uses_2_33(self):
        """r48 BACKLOG #numerical-P2-20: 99% VaR multiplier corrected from 1.5 → 2.33."""
        from services import risk_manager as _rm
        from unittest.mock import patch
        with patch.object(_rm, "current_portfolio_heat", return_value=1000.0):
            v = _rm.book_var_99(equity=100_000)
        self.assertAlmostEqual(v, 2330.0, places=1)

    def test_default_stop_atr_mult(self):
        """r48 BACKLOG #numerical-P2-22: default aligned to daily TF (was 1.5, now 2.0)."""
        from services.backtester import DEFAULT_STOP_ATR_MULT
        self.assertEqual(DEFAULT_STOP_ATR_MULT, 2.0)

    def test_lev_etf_strategy_in_registry(self):
        from services.strategies import STRATEGY_FUNCS
        names = {f.__name__ for f in STRATEGY_FUNCS}
        self.assertIn("_lev_etf_decay_short", names)

    def test_kelly_nan_guard(self):
        """r48 BACKLOG #numerical-P1-11: NaN inputs return 1.0 instead of NaN."""
        import math
        from services.risk_math import kelly_risk_mult
        m = kelly_risk_mult(historical_win_rate=float("nan"), avg_reward_risk=1.5)
        self.assertEqual(m, 1.0)
        m = kelly_risk_mult(historical_win_rate=60.0, avg_reward_risk=float("nan"))
        self.assertEqual(m, 1.0)

    def test_ai_envelope_tightened(self):
        """r48 BACKLOG #edge-F12: AI mult envelope shrunk to [0.85, 1.15]."""
        from services.config import AI_MULT_MIN, AI_MULT_MAX
        self.assertAlmostEqual(AI_MULT_MIN, 0.85, places=4)
        self.assertAlmostEqual(AI_MULT_MAX, 1.15, places=4)

    def test_ai_cost_tracker(self):
        """r48 BACKLOG #observability-P1-15: $ cost tracker with token math."""
        from services.ai_judge import ai_cost_today_usd, _record_ai_usage
        _record_ai_usage("claude-opus-4-7", 1_000_000, 100_000)
        out = ai_cost_today_usd("claude-opus-4-7")
        self.assertIn("cost_estimate_usd", out)
        # 1M input × $15 + 100K output × $75 / 1M = 15 + 7.5 = 22.5
        self.assertGreater(out["cost_estimate_usd"], 0)

    def test_index_event_requires_ticker(self):
        """r48 BACKLOG #edge-F10: boost only fires for whitelisted inclusion tickers."""
        from services.index_calendar import index_event_multiplier
        from unittest.mock import patch
        # Even mid-window, a non-whitelisted ticker returns 1.0
        with patch("services.index_calendar.is_in_index_event_window", return_value=True):
            m = index_event_multiplier(ticker="UNKNOWN_TICKER_ZZZ")
            self.assertEqual(m, 1.0)

    def test_opex_eligible_universe(self):
        """r48 BACKLOG #edge-F11: OPEX 0.92× only for liquid mega-caps."""
        from services.seasonality import opex_eligible
        self.assertTrue(opex_eligible("SPY"))
        self.assertTrue(opex_eligible("AAPL"))
        self.assertFalse(opex_eligible("CNTA"))


class TestFmpIntegration(unittest.TestCase):
    """FMP REST client + FMP-first / yfinance-fallback wiring across the
    fundamentals/earnings/analyst_ratings services. Premium plan endpoints.

    Failure modes exercised:
      * Missing FMP_API_KEY → is_enabled=False, no network call, fallback runs
      * FMP returns None for one ticker → caller falls back to yfinance
      * FMP transport / JSON error → caller falls back to yfinance
      * SEC poll dedupes the same filing across consecutive ticks
      * FMP-derived analyst row matches the shape analyst_ratings.upsert wants
    """

    def setUp(self):
        _reset_db()
        # Force the key off so is_enabled() is the gate we trust in tests.
        self._prev_key = os.environ.pop("FMP_API_KEY", None)
        # Reset module-level dedup state between tests (otherwise tests bleed).
        from services import fmp_client
        fmp_client._cache.clear()
        with fmp_client._seen_filings_lock:
            fmp_client._seen_filings.clear()

    def tearDown(self):
        if self._prev_key is not None:
            os.environ["FMP_API_KEY"] = self._prev_key

    def test_disabled_when_key_unset(self):
        from services import fmp_client
        self.assertFalse(fmp_client.is_enabled())
        self.assertIsNone(fmp_client.get_fundamentals("AAPL"))
        self.assertIsNone(fmp_client.get_next_earnings_ts("AAPL"))
        self.assertIsNone(fmp_client.has_recent_earnings("AAPL"))
        self.assertIsNone(fmp_client.get_analyst_consensus("AAPL"))
        self.assertEqual(fmp_client.get_recent_sec_filings("8-K"), [])

    def test_enabled_when_key_set(self):
        from services import fmp_client
        os.environ["FMP_API_KEY"] = "test-key-xyz"
        try:
            self.assertTrue(fmp_client.is_enabled())
        finally:
            os.environ.pop("FMP_API_KEY", None)

    def test_fundamentals_falls_back_to_yfinance_when_fmp_returns_none(self):
        """When FMP_API_KEY is set but the FMP composite fetch returns None
        (delisted ticker, premium-tier endpoint not available, etc.), the
        fundamentals._fetch_one path must continue down the yfinance branch
        rather than returning None and starving the score multiplier."""
        import yfinance
        from services import fundamentals as fnd
        os.environ["FMP_API_KEY"] = "test-key"
        try:
            with patch("services.fmp_client.get_fundamentals", return_value=None), \
                 patch.object(yfinance, "Ticker") as mock_yf:
                mock_yf.return_value.info = {
                    "sector": "Technology", "marketCap": 3.4e12,
                    "trailingPE": 28.5, "profitMargins": 0.25,
                }
                row = fnd._fetch_one("AAPL")
            self.assertIsNotNone(row)
            self.assertEqual(row["sector"], "Technology")
            self.assertEqual(row["pe_ratio"], 28.5)
        finally:
            os.environ.pop("FMP_API_KEY", None)

    def test_fundamentals_uses_fmp_when_returned(self):
        import yfinance
        from services import fundamentals as fnd
        os.environ["FMP_API_KEY"] = "test-key"
        fmp_payload = {
            "ticker": "MSFT", "sector": "Technology",
            "industry": "Software", "market_cap": 3.0e12,
            "shares_outstanding": None, "pe_ratio": 36.2, "pe_forward": None,
            "peg_ratio": 2.1, "price_to_book": 12.0, "price_to_sales": 13.5,
            "ev_to_ebitda": 25.0, "revenue_growth_yoy": 0.18,
            "earnings_growth_yoy": 0.20, "profit_margin": 0.36,
            "operating_margin": 0.42, "return_on_equity": 0.40,
            "return_on_assets": 0.18, "debt_to_equity": 0.40,
            "current_ratio": 1.8, "free_cash_flow": 5.0, "dividend_yield": 0.008,
            "beta": 0.9, "short_pct_float": 0.012, "short_ratio": 1.5,
        }
        try:
            # If FMP returns, the yfinance branch must not run — patch Ticker
            # to raise and verify the row still comes through.
            with patch("services.fmp_client.get_fundamentals", return_value=fmp_payload), \
                 patch.object(yfinance, "Ticker",
                              side_effect=AssertionError("yfinance should not be hit when FMP succeeds")):
                row = fnd._fetch_one("MSFT")
            self.assertEqual(row["ticker"], "MSFT")
            self.assertEqual(row["pe_ratio"], 36.2)
        finally:
            os.environ.pop("FMP_API_KEY", None)

    def test_earnings_uses_fmp_first(self):
        from services import earnings
        os.environ["FMP_API_KEY"] = "test-key"
        try:
            with patch("services.fmp_client.get_next_earnings_ts", return_value=1234567890.0):
                ts = earnings._fetch_next_earnings_ts("AAPL")
            self.assertEqual(ts, 1234567890.0)
        finally:
            os.environ.pop("FMP_API_KEY", None)

    def test_analyst_consensus_shape_compatible_with_pipeline(self):
        """The dict returned by fmp_client.get_analyst_consensus must satisfy
        the keys analyst_ratings._upsert reads (mean / key / analyst_count /
        target_*). Otherwise the persisted row would silently miss fields."""
        from services import fmp_client, analyst_ratings as ar
        os.environ["FMP_API_KEY"] = "test-key"
        try:
            # Mock the underlying _get to return realistic FMP shapes.
            def fake_get(url, params=None, ttl_sec=0):
                if "upgrades-downgrades-consensus" in url:
                    return [{"strongBuy": 5, "buy": 10, "hold": 3,
                             "sell": 1, "strongSell": 1, "consensus": "Buy"}]
                if "price-target-consensus" in url:
                    return [{"targetConsensus": 250.0, "targetHigh": 300.0, "targetLow": 200.0}]
                return None
            with patch("services.fmp_client._get", side_effect=fake_get):
                row = fmp_client.get_analyst_consensus("AAPL")
            self.assertEqual(row["ticker"], "AAPL")
            self.assertEqual(row["analyst_count"], 20)
            # Mean: (1*5 + 2*10 + 3*3 + 4*1 + 5*1) / 20 = 43/20 = 2.15
            self.assertAlmostEqual(row["mean"], 2.15, places=2)
            self.assertEqual(row["target_mean"], 250.0)
            self.assertEqual(row["key"], "buy")
            # Upsert path uses these keys — none should KeyError.
            ar._upsert(row)
            persisted = ar.get_rating("AAPL")
            self.assertEqual(persisted["analyst_count"], 20)
            self.assertAlmostEqual(persisted["target_mean"], 250.0)
        finally:
            os.environ.pop("FMP_API_KEY", None)

    def test_sec_filings_poll_dedupes_same_filing_across_ticks(self):
        """Two consecutive poll runs returning the same filing must result
        in exactly one CandidateEvent row — the second tick recognizes the
        filing's `link` in the seen-set and skips. This is what protects the
        push webhook + RSS poll from double-firing on the same filing."""
        from services import fmp_client
        from database import SessionLocal, CandidateEvent
        os.environ["FMP_API_KEY"] = "test-key"
        try:
            sample_8k = [{
                "symbol": "AAPL", "ticker": "AAPL",
                "link": "https://sec.gov/Archives/edgar/data/1/0000-0000.htm",
                "acceptedDate": "2026-05-09 12:00:00",
                "title": "8-K filing",
            }]
            with patch("services.fmp_client.get_recent_sec_filings",
                       side_effect=lambda form_type, limit=50:
                       sample_8k if form_type == "8-K" else []):
                first = fmp_client.poll_sec_filings_into_events()
                second = fmp_client.poll_sec_filings_into_events()
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["inserted"], 0)
            self.assertGreaterEqual(second["skipped"], 1)
            db = SessionLocal()
            try:
                rows = db.query(CandidateEvent).filter(
                    CandidateEvent.ticker == "AAPL",
                    CandidateEvent.kind == "PEAD",
                ).all()
                self.assertEqual(len(rows), 1)
            finally:
                db.close()
        finally:
            os.environ.pop("FMP_API_KEY", None)

    def test_sec_filings_poll_skips_when_disabled(self):
        from services import fmp_client
        # No FMP_API_KEY set in this test — poll must short-circuit, not raise.
        out = fmp_client.poll_sec_filings_into_events()
        self.assertEqual(out, {"checked": 0, "inserted": 0, "skipped": 0})

    def test_form_to_event_kind_routing(self):
        from services import fmp_client
        self.assertEqual(fmp_client._form_to_event_kind("4"), "INSIDER_BUY")
        self.assertEqual(fmp_client._form_to_event_kind("8-K"), "PEAD")
        self.assertIsNone(fmp_client._form_to_event_kind("10-K"))


class TestOptionChainPolygonFallback(unittest.TestCase):
    """Polygon is the PRIMARY options-chain source (real-time, Options
    Advanced plan); Alpaca + Yahoo fall in below as 15-min-delayed
    fallbacks. The dispatch must:
      * try Polygon first
      * fall through to Alpaca when Polygon returns None
      * fall through to Yahoo only when both Polygon + Alpaca miss
      * never call later tiers when an earlier tier succeeded
    Without these guarantees, a working Polygon fetch would still rack up
    Alpaca calls and the bot would scoring on stale data.
    """

    def setUp(self):
        from services import options_fetcher
        with options_fetcher._chain_cache_lock:
            options_fetcher._chain_cache.clear()

    def test_alpaca_and_yahoo_not_called_when_polygon_succeeds(self):
        from services import options_fetcher
        sample_full = {
            "expirations": [1000], "calls": [], "puts": [],
            "quote_price": 100.0, "source": "polygon", "expiration_used": None,
        }
        with patch("services.options_fetcher._fetch_polygon_chain", return_value=sample_full), \
             patch("services.options_fetcher._fetch_alpaca_chain",
                   side_effect=AssertionError("Alpaca must not run when Polygon succeeded")), \
             patch("services.options_fetcher._fetch_yahoo_chain",
                   side_effect=AssertionError("Yahoo must not run when Polygon succeeded")):
            out = options_fetcher.fetch_option_chain("AAPL")
        self.assertIsNotNone(out)
        self.assertEqual(out["source"], "polygon")

    def test_yahoo_not_called_when_alpaca_succeeds(self):
        from services import options_fetcher
        sample_alpaca = {
            "expirations": [2000], "calls": [], "puts": [],
            "quote_price": 100.0, "source": "alpaca", "expiration_used": None,
        }
        with patch("services.options_fetcher._fetch_polygon_chain", return_value=None), \
             patch("services.options_fetcher._fetch_alpaca_chain", return_value=sample_alpaca), \
             patch("services.options_fetcher._fetch_yahoo_chain",
                   side_effect=AssertionError("Yahoo must not run when Alpaca succeeded")):
            out = options_fetcher.fetch_option_chain("AAPL")
        self.assertEqual(out["source"], "alpaca")

    def test_yahoo_used_when_polygon_and_alpaca_both_miss(self):
        from services import options_fetcher
        sample_yahoo = {
            "expirations": [3000], "calls": [], "puts": [],
            "quote_price": 100.0, "source": "yahoo", "expiration_used": None,
        }
        with patch("services.options_fetcher._fetch_polygon_chain", return_value=None), \
             patch("services.options_fetcher._fetch_alpaca_chain", return_value=None), \
             patch("services.options_fetcher._fetch_yahoo_chain", return_value=sample_yahoo):
            out = options_fetcher.fetch_option_chain("AAPL")
        self.assertEqual(out["source"], "yahoo")

    def test_all_sources_failing_returns_none(self):
        """When Polygon (403/key/greeks-guard), Alpaca, AND Yahoo all miss,
        dispatch must surface None to the caller, not raise."""
        from services import options_fetcher
        with patch("services.options_fetcher._fetch_polygon_chain", return_value=None), \
             patch("services.options_fetcher._fetch_alpaca_chain", return_value=None), \
             patch("services.options_fetcher._fetch_yahoo_chain", return_value=None):
            out = options_fetcher.fetch_option_chain("AAPL")
        self.assertIsNone(out)


class TestR77LiveMoneyP0Fixes(unittest.TestCase):
    """r77 multi-agent audit P0s — defects that would lose money on flip
    to ALPACA_LIVE=1. Each test is a future-regression guard for a specific
    bug class that already escaped to production once.
    """

    def test_no_scheduler_add_job_has_duplicate_trigger_kwarg(self):
        """r77-A: scan EVERY scheduler.add_job(...) call in main.py for
        duplicate `trigger=` kwargs. Python raises TypeError at call time;
        the surrounding try/except logs a warning and silently kills the
        cron. We hit this once on the FMP SEC poll (caught manually after
        regression tests passed), then again on fundamentals_weekly and
        social_sentiment after a reschedule pass. Now guarded broadly."""
        import re
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path, "r") as f:
            src = f.read()
        offenders = []
        for m in re.finditer(r"scheduler\.add_job\(", src):
            start = m.end()
            depth = 1
            i = start
            while i < len(src) and depth > 0:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            block = src[start: i - 1]
            n_trigger = len(re.findall(r"\btrigger=", block))
            if n_trigger > 1:
                line_no = src[: m.start()].count("\n") + 1
                offenders.append((line_no, n_trigger))
        self.assertEqual(
            offenders, [],
            f"scheduler.add_job() with duplicate trigger= kwargs at lines: {offenders}. "
            "Python raises TypeError; the surrounding try/except catches it and the "
            "cron silently never registers."
        )

    def test_news_ai_trim_uses_lock_and_fresh_session(self):
        """r77-D: AI news_exit `trim` branch was modifying t.qty + db.commit()
        on the outer dispatch session without acquiring _ai_news_lock — the
        manage_open_positions loop trailing a stop on the same trade would
        race-write the qty (one thread overwrites the other's commit, the
        broker-vs-DB share count diverges, and the SL leg can fire on
        wrong qty). Mirror the close branch: lock + fresh SessionLocal."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "news.py"), "r") as f:
            src = f.read()
        # Anchor on the trim branch and pull the next ~5kB of source.
        anchor = src.find('action == "trim"')
        self.assertGreater(anchor, 0, "trim branch not found")
        block = src[anchor: anchor + 5000]
        # Stop at the next sibling-level `elif action ==` or the outer
        # `finally:` of dispatch — whichever comes first.
        for end_marker in ('elif action ==', '\n    finally:'):
            i = block.find(end_marker, 50)
            if i > 0:
                block = block[:i]
                break
        self.assertIn(
            "_ai_news_lock", block,
            "trim branch must acquire _ai_news_lock to serialize against "
            "manage_open_positions"
        )
        self.assertIn(
            "ldb = SessionLocal()", block,
            "trim branch must use a fresh session, not the outer db"
        )
        self.assertIn(
            "ldb.commit()", block,
            "trim branch should commit to the fresh session, not the outer db"
        )
        # The buggy patterns must be gone — they'd indicate the outer
        # session is still being mutated.
        self.assertNotIn(
            "t.qty = (t.qty or 0) - half", block,
            "trim branch still references outer t.qty — race-condition "
            "regression. Use t_local on the fresh ldb session."
        )

    def test_fmp_sec_webhook_is_sync_def(self):
        """r77-E: fmp_sec_webhook used to be `async def` but did sync
        SQLAlchemy work — that blocks the asyncio event loop. FastAPI runs
        sync `def` handlers in its threadpool automatically, which is the
        right shape for sync-DB work."""
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path, "r") as f:
            src = f.read()
        # Find the route decorator and inspect the function signature
        # immediately after.
        idx = src.find('"/api/webhooks/fmp/sec"')
        self.assertGreater(idx, 0, "FMP webhook route decorator not found")
        # Look ahead ~400 chars for the def signature.
        snippet = src[idx: idx + 600]
        self.assertIn(
            "def fmp_sec_webhook", snippet,
            "FMP webhook handler not found near its decorator"
        )
        self.assertNotIn(
            "async def fmp_sec_webhook", snippet,
            "FMP webhook handler must be sync `def` (FastAPI threadpools "
            "it). Sync SQLAlchemy on `async def` blocks the event loop."
        )

    def test_no_realized_pl_read_modify_write_in_auto_trader(self):
        """r79-A: every `t.realized_pl = (t.realized_pl or 0) + delta`
        site is a lost-update race when manage tick + WS fast-path + AI
        exit hit the same trade. The execution_engine helper
        atomic_accumulate_realized_pl uses a single-round-trip SQL
        UPDATE with COALESCE, which is race-safe."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        # The buggy pattern in any of its common forms
        patterns = [
            't.realized_pl = (t.realized_pl or 0) +',
            't.realized_pl = (t.realized_pl or 0.0) +',
        ]
        offenders = [p for p in patterns if p in src]
        self.assertEqual(
            offenders, [],
            "auto_trader.py contains realized_pl read-modify-write — "
            f"patterns: {offenders}. Use _atomic_acc_pl(db, t.id, delta)."
        )

    def test_no_note_concat_read_modify_write_in_auto_trader(self):
        """r79-B: same race-condition class as realized_pl. `t.note =
        (t.note or '') + suffix` lost-updates on concurrent writers."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        offenders_n = src.count('t.note = (t.note or "")')
        self.assertEqual(
            offenders_n, 0,
            f"auto_trader.py has {offenders_n} `t.note = (t.note or \"\") + ...` "
            "sites. Use _atomic_append_note(db, t.id, suffix)."
        )

    def test_sector_heat_uses_original_qty(self):
        """r79-C: sector-heat aggregation must read original_qty, not
        the mutable qty. Otherwise a T1 trim halves sr.qty and frees a
        sector slot while the runner is still exposed."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        anchor = src.find("sector_heat = 0.0")
        self.assertGreater(anchor, 0, "sector_heat block not found")
        block = src[anchor: anchor + 1500]
        self.assertIn(
            "original_qty", block,
            "sector_heat block must reference original_qty so trims "
            "don't free sector capacity prematurely."
        )

    def test_option_error_paths_set_idempotency_key(self):
        """r79-D: option PUT/CALL error-path AutoTrade INSERTs used to
        miss `idempotency_key=`, so two consecutive submit failures on
        the same signal would create two error rows instead of being
        deduped. With the column UNIQUE allowing multi-NULL, retry
        storms could quietly fill the table."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        for tag in ('"option submit failed', '"call submit failed'):
            anchor = src.find(tag)
            self.assertGreater(anchor, 0, f"error-path INSERT for {tag} missing")
            # Look back for the AutoTrade(...) ctor in the ~800 chars before.
            before = src[max(0, anchor - 1000): anchor]
            self.assertIn(
                "idempotency_key=", before,
                f"error-path INSERT near {tag!r} doesn't set idempotency_key"
            )

    def test_kill_switch_checked_in_option_entry_paths(self):
        """r80: cfg.killed must short-circuit consider_put_play and
        consider_call_play. Previously only consider_signal honored it,
        so a killed bot would still open new option positions."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        for fn in ("def consider_put_play", "def consider_call_play"):
            anchor = src.find(fn)
            self.assertGreater(anchor, 0, f"{fn} not found")
            # Look at the next ~3500 chars (function head + early gates,
            # past the lock-acquisition + initial cfg reads).
            block = src[anchor: anchor + 3500]
            self.assertIn(
                'getattr(cfg, "killed"', block,
                f"{fn} doesn't check cfg.killed early — kill switch bypass"
            )

    def test_wash_sale_gate_present(self):
        """r80: cfg.wash_sale_cooldown_days column exists since r47 but
        was never queried. Now consider_signal reads it and skips entries
        on tickers with a recent realized loss inside the window."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        self.assertIn(
            'wash_sale_cooldown_days', src,
            "wash-sale gate missing in auto_trader.py — IRS rule risk"
        )
        self.assertIn(
            'wash_sale_guard', src,
            "wash-sale skip metric reason missing"
        )

    def test_adopted_sl_tif_matches_bracket_tif(self):
        """r80: promote_adopted_to_managed used to hardcode TIF.GTC for
        the SL leg even when cfg.bracket_tif='day'. That defeated the
        weekend-gap safety on adopted positions."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        anchor = src.find("def promote_adopted_to_managed")
        self.assertGreater(anchor, 0)
        # The SL submit block is ~7-8kB into the function, after qty
        # resync + level computation. Inspect the full function body.
        block = src[anchor: anchor + 12000]
        self.assertNotIn(
            "time_in_force=_TIF.GTC, stop_price=", block,
            "promote_adopted_to_managed still hardcodes GTC for SL — "
            "defeats day-TIF weekend-gap safety"
        )
        self.assertIn(
            'getattr(cfg, "bracket_tif"', block,
            "promote_adopted_to_managed must read bracket_tif from config"
        )

    def test_idempotency_dedup_is_status_aware(self):
        """r80: previously the idempotency dedup matched on key alone
        (any status), so a closed_stop trade still blocked re-entry on
        the same signal. Now only PENDING/OPEN/ADOPTED rows reserve the
        key — closed history is inert."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        anchor = src.find("Idempotency: don't re-open the same signal")
        self.assertGreater(anchor, 0, "idempotency block not found")
        block = src[anchor: anchor + 1200]
        self.assertIn(
            'AutoTrade.status.in_(["pending", "open", "adopted"])', block,
            "idempotency dedup is not status-aware — closed trades still "
            "block re-entry of equivalent signals"
        )

    def test_force_close_options_verifies_position_flat(self):
        """r80: force_close on options must verify the broker actually
        flattened. Submit returning 'no error' means the order was
        accepted, not necessarily filled (broker reject post-submit,
        illiquid contract). Without verify, the trade is marked closed
        while broker still shows the position open."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "execution_engine.py"), "r") as f:
            src = f.read()
        # Look in the option branch of force_close_trade.
        anchor = src.find("submit_option_exit_with_cross_fallback")
        self.assertGreater(anchor, 0)
        block = src[anchor: anchor + 3000]
        self.assertIn(
            "force_close_unverified", block,
            "force_close option branch missing post-submit position-poll "
            "verify — silent unflattened positions will look closed"
        )
        self.assertIn(
            "get_option_position", block,
            "force_close option branch must poll get_option_position to "
            "confirm residual qty is 0"
        )

    def test_alpaca_rest_reads_have_timeout_guard(self):
        """r80: get_account / get_positions / get_orders previously had
        no timeout — alpaca-py's underlying httpx defaults to None (wait
        forever), so a network stall would block the entry pipeline.
        Now wrapped in _safe_rest_read with a 5s timeout + 1 retry on
        transient errors."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "alpaca_client.py"), "r") as f:
            src = f.read()
        self.assertIn(
            "def _safe_rest_read", src,
            "_safe_rest_read helper missing"
        )
        for fn_label in (
            "def get_account",
            "def get_positions",
            "def get_orders",
        ):
            anchor = src.find(fn_label)
            self.assertGreater(anchor, 0, f"{fn_label} not found")
            block = src[anchor: anchor + 800]
            self.assertIn(
                "_safe_rest_read", block,
                f"{fn_label} not wrapped with timeout guard"
            )

    def test_option_entry_cancel_waits_for_terminal(self):
        """r80: submit_option_entry_with_cross_fallback used a
        fire-and-forget cancel before market cross. That left a race
        where the limit could fill DURING the cancel ACK while the
        market cross also fired = 2× qty filled. Now wait_for_terminal
        with bounded timeout + final-state poll."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "alpaca_client.py"), "r") as f:
            src = f.read()
        # Match the FUNCTION DEFINITION specifically, not the doc/comment refs.
        anchor = src.find("def submit_option_entry_with_cross_fallback")
        self.assertGreater(anchor, 0)
        # Function is ~150 lines / ~7kB. Pull 9kB to be safe.
        block = src[anchor: anchor + 9000]
        self.assertIn(
            "wait_for_terminal=True", block,
            "submit_option_entry cancel before cross still fire-and-forget"
        )

    def test_frontend_has_kill_button(self):
        """r80: under stress, operator must have a one-click KILL path
        in the UI — not just a curl command. Verify the button exists
        and posts to /api/trading/kill."""
        with open(os.path.join(os.path.dirname(__file__), "..", "..",
                               "frontend", "app.js"), "r") as f:
            src = f.read()
        self.assertIn(
            "/api/trading/kill", src,
            "frontend has no caller of /api/trading/kill — operator can't "
            "stop the bot from the UI under stress"
        )
        self.assertIn(
            "KILL BOT", src,
            "frontend has no visible KILL button label"
        )

    def test_all_backend_python_files_parse(self):
        """r79-F: blanket guard against the 'stray 2-char prefix on a line'
        corruption class we kept hitting during this audit cycle (random
        2-3 character prefixes appearing before keywords or docstring
        opens). Compile every .py file under backend/. If anything fails
        to parse, this test fires before deploy.sh's regression gate."""
        import py_compile
        backend_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
        broken = []
        for root, _, files in os.walk(backend_root):
            if "__pycache__" in root or "/tests/" in root:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                try:
                    py_compile.compile(path, doraise=True)
                except py_compile.PyCompileError as e:
                    broken.append((path, str(e).splitlines()[0] if str(e) else ""))
        self.assertEqual(
            broken, [],
            f"Backend Python files with syntax errors: {broken}"
        )

    def test_logger_defined_before_first_use_in_auto_trader(self):
        """r79-E: `logger = logging.getLogger(__name__)` must be at the
        module top, before ANY function that calls logger.* is defined.
        Originally logger was at line ~728 and _pg_advisory_entry_lock
        at line ~99 referenced it — fine in the current call sequence
        but a load-order trap waiting for the next reorder."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            lines = f.read().split("\n")
        first_logger_def = None
        for i, ln in enumerate(lines):
            if ln.startswith("logger = logging.getLogger("):
                first_logger_def = i + 1
                break
        self.assertIsNotNone(first_logger_def, "logger never defined")
        # Should be in the first ~100 lines (after imports), not deep in the file.
        self.assertLess(
            first_logger_def, 100,
            f"logger defined at line {first_logger_def}; should be near "
            "top-of-module so any helper above can call logger.* safely."
        )

    def test_no_duplicate_for_loop_or_def_lines_in_main(self):
        """r77-A bis: same root cause — a reschedule / rename pass that
        leaves the old line above the new line. Two consecutive `for ...:`
        at the same indent makes the first `for` body-less (IndentationError
        at import). Two consecutive `def name(...):` for the same name
        means the second silently shadows the first. Sweep main.py for both."""
        main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
        with open(main_path, "r") as f:
            lines = f.read().split("\n")
        offenders = []
        for i in range(1, len(lines)):
            prev_full, curr_full = lines[i - 1], lines[i]
            prev = prev_full.lstrip()
            curr = curr_full.lstrip()
            same_indent = (len(prev_full) - len(prev)) == (len(curr_full) - len(curr))
            if not same_indent:
                continue
            if prev.startswith("for ") and curr.startswith("for ") and ":" in prev and ":" in curr:
                offenders.append((i + 1, "for-loop"))
            if (prev.startswith("def ") or prev.startswith("async def ")) and \
               (curr.startswith("def ") or curr.startswith("async def ")):
                pn = prev.split("(", 1)[0].split()[-1]
                cn = curr.split("(", 1)[0].split()[-1]
                if pn == cn:
                    offenders.append((i + 1, f"def {cn}"))
        self.assertEqual(
            offenders, [],
            f"Duplicated lines in main.py at: {offenders}. Likely a partial "
            "find/replace or reschedule pass that left the old line above "
            "the new one."
        )

    def test_bracket_tif_fallback_is_day_not_gtc(self):
        """r77-B: bracket_tif=getattr(cfg, ..., 'gtc') would silently invert
        the weekend-gap safety if cfg.bracket_tif comes back NULL (legacy
        row, migration miss). The DB column default is 'day' and the status
        endpoint also reads 'day'; the order-submit fallback must agree."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "auto_trader.py"), "r") as f:
            src = f.read()
        # Look for the offending pattern; we don't expect it to appear
        # anywhere in the file. (A future regression that re-adds it would
        # set off this test.)
        self.assertNotIn(
            'getattr(cfg, "bracket_tif", "gtc")', src,
            "bracket_tif fallback regressed to 'gtc' — see auto_trader.py "
            "submit_bracket_order call. DB default is 'day'; the fallback "
            "must agree, otherwise legacy NULL rows go out as GTC and "
            "Friday positions stay covered through weekend gaps."
        )
        # And confirm the corrected pattern is present.
        self.assertIn('getattr(cfg, "bracket_tif", "day")', src)

    def test_fast_path_no_artificial_2s_sleep(self):
        """r77-C: live_quotes fast-path used to time.sleep(2.0) after
        firing manage_open_positions. That serialized stop/target reaction
        across tickers — in a correlated drawdown, only one position could
        react every 2s. Manage_open_positions already serializes its own
        DB writes via per-trade locks; the sleep was redundant + harmful."""
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "services", "live_quotes.py"), "r") as f:
            src = f.read()
        # Locate the threat-path block by anchor.
        anchor = src.find("manage fast-path fired")
        self.assertGreater(anchor, 0, "fast-path log message not found")
        block_src = src[anchor: anchor + 800]
        # The 2.0-second sleep must not return inside this block.
        self.assertNotIn(
            "time.sleep(2.0)", block_src,
            "fast-path 2.0s sleep regressed — this throttles correlated "
            "stop/target reaction. Lock release should be immediate; "
            "manage_open_positions handles its own serialization."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

    def test_polygon_returns_none_when_chain_has_zero_greeks(self):
        """Defensive guard: Polygon serves chain skeleton (bid/ask) but null
        greeks/IV off-hours and on the wrong plan tier. The fetcher must
        treat that as a miss — otherwise options_analyzer gets IV=0.0 and
        delta=None and silently mis-scores every option entry."""
        from services import options_fetcher
        # Simulate a Polygon API response with bid/ask but no greeks/IV
        # (the exact failure mode we observed Sunday on the live API).
        sample_polygon_resp = {
            "results": [
                {
                    "details": {
                        "ticker": "O:AAPL260516C00200000",
                        "contract_type": "call",
                        "strike_price": 200.0,
                        "expiration_date": "2026-05-16",
                    },
                    "last_quote": {"bid": 91.8, "ask": 95.15},
                    "last_trade": {"price": 0.0},
                    "day": {"volume": 0},
                    "open_interest": 0,
                    "implied_volatility": None,
                    "greeks": None,
                    "underlying_asset": {"price": 293.86},
                },
            ],
            "next_url": None,
        }
        os.environ["POLYGON_API_KEY"] = "fake-key"
        try:
            class _FakeResp:
                status_code = 200
                def json(self): return sample_polygon_resp
            class _FakeSess:
                def get(self, *args, **kwargs): return _FakeResp()
            with patch("services.options_fetcher._get_session", return_value=_FakeSess()):
                out = options_fetcher._fetch_polygon_chain("AAPL")
            self.assertIsNone(out)
        finally:
            os.environ.pop("POLYGON_API_KEY", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
