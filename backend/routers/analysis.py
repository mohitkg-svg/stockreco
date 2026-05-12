"""Analysis router — runs the signal-generation pipeline on demand.

Endpoints under `/api/analysis/*`:
  * `POST /api/analysis/{ticker}` — full multi-timeframe analysis
    (indicators + S/R + patterns + zones + Fib + MTF alignment) and
    persistence of every emitted signal to the `signals` table
  * `GET /api/analysis/overview` — dashboard summary across watchlist
  * `GET /api/analysis/chart/{ticker}` — OHLCV + indicator overlay +
    S/R levels for the chart pane
  * `GET /api/analysis/recent-signals` — recent signal feed

The heavy lifting is delegated to `services.signal_generator` and
`services.indicators`. This router is mostly orchestration: fetch
OHLCV, run signal generation per timeframe, insert into DB, build
the response shape.

Background-task dispatch (auto-trader entry submission) happens
post-response so the HTTP response isn't blocked on broker round-trips.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List, Optional
import json
import numpy as np
from database import get_db, WatchlistStock, Signal
from models import AnalysisResponse, SignalResponse, ChartDataResponse, ChartCandle, IndicatorLine, SupportResistanceLevel, OverviewItem
from services.data_fetcher import fetch_ohlcv, get_ticker_info, get_current_price, TIMEFRAME_CONFIG
from services.indicators import compute_indicators, get_chart_indicator_series
from services.support_resistance import pivot_points, swing_levels, classify_levels_relative_to_price
from services.supply_demand import detect_zones
from services.fibonacci import compute_fib_levels
from services.gap_detector import detect_all_gaps
from services.signal_generator import generate_signal, get_timeframe_alignment
from services.backtester import run_multi_strategy
from services import auto_trader
from routers._auth import require_api_key
import logging
import time

router = APIRouter(prefix="/api/analysis", tags=["analysis"], dependencies=[Depends(require_api_key)])
logger = logging.getLogger(__name__)

ANALYSIS_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d", "1mo"]

# Cache for multi-strategy backtest per ticker (1-hour TTL).
# Keyed by (ticker, cfg_version) — when AutoTraderConfig.updated_at changes
# (user tweaked thresholds), the old cached score is invalidated naturally
# because the next lookup uses a fresh key. This prevents the "I lowered the
# confidence floor but signals still look stale for an hour" footgun.
_backtest_cache: dict = {}
_BACKTEST_TTL = 3600


def _backtest_cache_version() -> str:
    """A short string that changes whenever inputs to backtest scoring change.
    Today: AutoTraderConfig.updated_at. Add bars-fingerprint here if the
    backtester ever takes more inputs from config."""
    try:
        from database import SessionLocal as _SL, AutoTraderConfig as _ATC
        s = _SL()
        try:
            cfg = s.query(_ATC).filter(_ATC.id == 1).first()
            if cfg and cfg.updated_at:
                return cfg.updated_at.isoformat()
        finally:
            s.close()
    except Exception:
        pass
    return "v0"


def _get_ticker_backtest(ticker: str) -> Optional[dict]:
    """Return cached multi-strategy backtest results on 1d data (1-hour TTL)."""
    now = time.time()
    key = (ticker, _backtest_cache_version())
    if key in _backtest_cache:
        res, exp = _backtest_cache[key]
        if now < exp:
            return res
    try:
        df = fetch_ohlcv(ticker, "1d")
        if df.empty or len(df) < 60:
            return None
        multi = run_multi_strategy(df, timeframe="1d")
        _backtest_cache[key] = (multi, now + _BACKTEST_TTL)
        return multi
    except Exception as e:
        logger.warning(f"Backtest failed for {ticker}: {e}")
        return None


def _apply_backtest_to_signal(signal: dict, backtest: Optional[dict]) -> dict:
    """Blend historical backtest score with technical confidence and annotate reasoning."""
    if not backtest or signal["signal_type"] == "NEUTRAL":
        return signal

    results = backtest.get("results") or []
    direction = signal["signal_type"]

    # F2: Strip chronic-losing strategies. A strategy with ≥10 trades AND a
    # total return worse than -10% isn't just unlucky on this ticker — it
    # has a persistent edge against us. Letting it contribute to the "best
    # strategy" pick or the voter tally biased confidence toward ticker/setup
    # combinations the market has already rejected.
    _CHRONIC_LOSS_PCT = -10.0
    _CHRONIC_MIN_TRADES = 10
    def _not_chronic(r: dict) -> bool:
        s = r.get("stats") or {}
        return not (
            (s.get("total_trades") or 0) >= _CHRONIC_MIN_TRADES
            and (s.get("total_return_pct") or 0) <= _CHRONIC_LOSS_PCT
        )
    results = [r for r in results if _not_chronic(r)]

    # F4: Regime-gated strategy mix. Mean-reversion strategies in strong
    # trends get run over; breakout strategies in choppy ranges fakeout.
    # Drop the strategy *family* that's fighting the regime for this ticker
    # before picking the best. ADX comes off the live signal (1d strongest).
    _adx = None
    try:
        _adx = float(signal.get("adx") or 0) or None
    except Exception:
        _adx = None
    _STRATEGY_FAMILY = {
        "Bollinger Breakout": "BREAKOUT", "Donchian Breakout": "BREAKOUT",
        "Opening Range Breakout": "BREAKOUT",
        "RSI Mean Reversion": "MEANREV", "VWAP Reclaim": "MEANREV",
    }
    if _adx is not None:
        if _adx < 20:
            results = [r for r in results if _STRATEGY_FAMILY.get(r["strategy"]) != "BREAKOUT"]
        elif _adx > 35:
            results = [r for r in results if _STRATEGY_FAMILY.get(r["strategy"]) != "MEANREV"]

    # Best strategy matching our direction
    same_dir = [r for r in results if r["direction"] == direction and r["stats"]["total_trades"] >= 3]
    # Best strategy in opposite direction (used as a "disagreement" check)
    opp_dir = [r for r in results if r["direction"] != direction and r["stats"]["total_trades"] >= 3]

    best_same = max(same_dir, key=lambda r: r["confidence"]) if same_dir else None
    best_opp = max(opp_dir, key=lambda r: r["confidence"]) if opp_dir else None

    tech_conf = float(signal["confidence"])
    extra_reasons = []

    if best_same:
        bt_score = best_same["confidence"]
        ret = best_same["stats"]["total_return_pct"]
        wr = best_same["stats"]["win_rate"]
        n = best_same["stats"]["total_trades"]
        # NaN-safe coercion: backtester occasionally emits NaN for confidence
        # (zero-trade edge cases); NaN comparisons are always False so the
        # downstream clamp wouldn't catch it — guard explicitly here.
        try:
            bt_score_f = float(bt_score)
            tech_conf_f = float(tech_conf)
            import math as _math
            if _math.isnan(bt_score_f) or _math.isnan(tech_conf_f):
                return signal
        except (TypeError, ValueError):
            return signal
        # Blend: 60% live technicals, 40% historical edge
        blended = 0.6 * tech_conf_f + 0.4 * bt_score_f
        extra_reasons.append(
            f"📊 Backtest: best {direction} strategy for this stock is '{best_same['strategy']}' "
            f"— {ret:+.1f}% return, {wr:.0f}% win-rate across {n} trades (score {bt_score:.0f}/100). "
            f"Confidence adjusted {tech_conf:.0f}→{blended:.0f}."
        )
        signal["confidence"] = max(0, min(95, round(blended)))
        signal["backtest_score"] = bt_score
        signal["backtest_best_strategy"] = best_same["strategy"]
        signal["backtest_return_pct"] = ret
        signal["backtest_win_rate"] = wr
        signal["backtest_trades"] = n
    else:
        # No matching-direction strategy has enough trades — reduce confidence
        signal["confidence"] = max(0, round(tech_conf * 0.75))
        extra_reasons.append(
            f"⚠️ Backtest: no {direction} strategy produced ≥3 valid trades historically on this ticker — "
            f"confidence reduced {tech_conf:.0f}→{signal['confidence']}."
        )
        signal["backtest_score"] = None
        signal["backtest_best_strategy"] = None

    # Opposite-direction warning
    if best_opp and best_same and best_opp["confidence"] > best_same["confidence"] + 15:
        extra_reasons.append(
            f"⚠️ The opposite-direction strategy '{best_opp['strategy']}' scored "
            f"{best_opp['confidence']:.0f}/100 historically — contradicts this live signal."
        )
        signal["confidence"] = max(0, signal["confidence"] - 10)

    # Strategy voting bonus: when multiple INDEPENDENT strategies in the
    # signal direction also have positive returns, that's a confluence vote.
    # Postmortem fix #5: count distinct *categories* (TREND / BREAKOUT / GAP /
    # MEANREV) rather than raw strategy count — three near-duplicate trend
    # strategies all firing on the same SMA cross is one vote, not three. We
    # were over-rewarding correlated voters and pushing confidence above gates
    # for trades that lacked true confluence.
    profitable_voters = [
        r for r in same_dir
        if r["stats"]["total_return_pct"] > 0 and r["stats"]["win_rate"] >= 45
    ]
    _STRATEGY_CATEGORY = {
        # Trend / moving-average regime
        "Trend Following": "TREND",
        "Golden/Death Cross": "TREND",
        "EMA Pullback": "TREND",
        "MACD Crossover": "TREND",
        # Breakout / volatility expansion
        "Bollinger Breakout": "BREAKOUT",
        "Donchian Breakout": "BREAKOUT",
        "Opening Range Breakout": "BREAKOUT",
        # Mean reversion
        "RSI Mean Reversion": "MEANREV",
        "VWAP Reclaim": "MEANREV",
        # Gap / structure
        "Gap Fill": "GAP",
        "Gap & Go": "GAP",
        "FVG Pullback": "GAP",
    }
    voter_categories = {
        _STRATEGY_CATEGORY.get(p["strategy"], p["strategy"])
        for p in profitable_voters
    }
    if len(voter_categories) >= 2:
        bonus = min(8, 3 * (len(voter_categories) - 1))
        names = ", ".join(p["strategy"] for p in profitable_voters[:5])
        extra_reasons.append(
            f"🗳️ Strategy vote: {len(profitable_voters)} {direction} strategies "
            f"across {len(voter_categories)} independent categories ({', '.join(sorted(voter_categories))}) "
            f"are net-profitable ({names}) → +{bonus} confidence"
        )
        signal["confidence"] = min(95, signal["confidence"] + bonus)
    elif len(profitable_voters) == 0 and same_dir:
        extra_reasons.append(
            f"🗳️ Strategy vote: NO {direction} strategy is net-profitable historically — "
            "soft warning that conditions don't reward this direction on this stock"
        )
        signal["confidence"] = max(0, signal["confidence"] - 5)

    if extra_reasons:
        signal["reasoning"] = (signal.get("reasoning") or "") + "\n" + "\n".join(extra_reasons)

    return signal


def _run_analysis_for_ticker(ticker: str, db: Session) -> List[dict]:
    """Run full TA analysis for a ticker across all timeframes. Saves to DB."""
    signals = []
    # Pre-compute multi-strategy backtest once per ticker (cached)
    backtest = _get_ticker_backtest(ticker)
    for tf in ANALYSIS_TIMEFRAMES:
        try:
            df = fetch_ohlcv(ticker, tf)
            if df.empty:
                continue
            df_ind = compute_indicators(df)
            signal = generate_signal(ticker, tf, df_ind)
            signal = _apply_backtest_to_signal(signal, backtest)
            signals.append(signal)

            # Upsert signal in DB
            existing = db.query(Signal).filter(
                Signal.ticker == ticker, Signal.timeframe == tf
            ).order_by(desc(Signal.generated_at)).first()

            # Mark as new if signal type changed
            is_new = not existing or existing.signal_type != signal["signal_type"]

            db_signal = Signal(
                ticker=signal["ticker"],
                timeframe=signal["timeframe"],
                signal_type=signal["signal_type"],
                confidence=signal["confidence"],
                entry=signal.get("entry"),
                stop_loss=signal.get("stop_loss"),
                target1=signal.get("target1"),
                target2=signal.get("target2"),
                target3=signal.get("target3"),
                reasoning=signal.get("reasoning"),
                patterns=signal.get("patterns"),
                strategy=signal.get("strategy"),
                is_new=is_new,
            )
            db.add(db_signal)
            # COMMIT before calling consider_signal — auto_trader opens its own
            # SessionLocal and tries to INSERT INTO auto_trades. If we only
            # flushed here, this session would still hold the SQLite writer lock
            # and every insert from the scheduled scan would race it, spending
            # 30s in busy_timeout before failing with "database is locked".
            # Per-timeframe commit keeps the write window tiny.
            db.commit()
            db.refresh(db_signal)  # re-hydrate id + defaults after commit
            try:
                auto_trader.consider_signal(signal, signal_id=db_signal.id)
            except Exception as e:
                logger.warning(f"auto_trader hook error on {ticker} {tf}: {e}")
        except Exception as e:
            # r89: include traceback so root-cause is grep-able. Previously we
            # only logged the message ("index 14 out of bounds…") with no clue
            # to which call raised — GEV 1mo took days to trace.
            import traceback as _tb
            logger.error(
                f"Error analyzing {ticker} {tf}: {e}\n{_tb.format_exc()}"
            )
            db.rollback()  # drop any half-built state so the next iteration starts clean
    # After all timeframes are scored: if NONE are a strong BUY, hunt put-plays.
    try:
        any_buy = any(s.get("signal_type") == "BUY" and (s.get("confidence") or 0) >= 70 for s in signals)
        if not any_buy:
            auto_trader.consider_put_play(ticker)
    except Exception as e:
        logger.warning(f"put-play hook error on {ticker}: {e}")
    # Call-play hunt: fires for tickers where the stock auto-trader DID NOT
    # open a new position (either sub-threshold BUY, or stock already at its
    # per-ticker cap). consider_call_play enforces its own concentration
    # guard — it won't stack a call on a stock trade that still has headroom.
    try:
        auto_trader.consider_call_play(ticker)
    except Exception as e:
        logger.warning(f"call-play hook error on {ticker}: {e}")
    # Reverse-thesis check: if a high-conviction OPPOSITE signal landed for an
    # open auto-trade on this ticker, close it now (don't wait for 60s tick).
    try:
        auto_trader.check_reversals_for(ticker)
    except Exception as e:
        logger.warning(f"reversal hook error on {ticker}: {e}")
    return signals


# Overview response cache — polled every 60s by the frontend; even with warm
# per-ticker OHLCV caches the live-quote lookup + DB round-trip ran ~2-3s for a
# 15-stock watchlist, which is what the user perceives as "the UI didn't load".
# 20s TTL is well under the 60s poll interval, so price staleness is bounded.
_overview_cache: dict = {"payload": None, "expiry": 0.0, "fingerprint": None}
_OVERVIEW_TTL = 20.0


@router.post("/scan")
def trigger_scan(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Manually trigger a full watchlist scan in the background (parallel)."""
    def _do_scan():
        from concurrent.futures import ThreadPoolExecutor
        from database import SessionLocal as _SL
        _bootstrap = _SL()
        try:
            tickers = [s.ticker for s in _bootstrap.query(WatchlistStock).all()]
        finally:
            _bootstrap.close()

        def _one(ticker: str):
            _local = _SL()
            try:
                _run_analysis_for_ticker(ticker, _local)
            except Exception as e:
                logger.error(f"Scan error for {ticker}: {e}")
            finally:
                _local.close()

        if tickers:
            with ThreadPoolExecutor(max_workers=min(4, len(tickers)), thread_name_prefix="manual-scan") as pool:
                list(pool.map(_one, tickers))
        from main import _app_health
        from datetime import datetime as _dt, timezone as _tz
        _app_health["last_scan_at"] = _dt.now(_tz.utc).isoformat()
        logger.info("Manual scan complete.")

    background_tasks.add_task(_do_scan)
    return {"status": "scan started"}


@router.get("/overview", response_model=List[OverviewItem])
def get_overview(db: Session = Depends(get_db)):
    """Quick summary for all watchlist stocks with their latest strong signal.

    Performance: batches the per-ticker "best signal" lookup into ONE query
    instead of N (was O(watchlist_size) round-trips). For a 30-stock
    watchlist that's 30→1 query — visible in the polling loop that hits this
    endpoint every 60s.
    """
    stocks = db.query(WatchlistStock).all()
    if not stocks:
        return []
    tickers = [s.ticker for s in stocks]

    # Fingerprint the cache key on watchlist membership so adding/removing a
    # ticker busts the cache immediately. (Price updates are handled by TTL.)
    _fp = tuple(sorted(tickers))
    if (
        _overview_cache["fingerprint"] == _fp
        and _overview_cache["payload"] is not None
        and time.time() < _overview_cache["expiry"]
    ):
        return _overview_cache["payload"]

    # Pull every non-neutral signal for all watchlist tickers in one shot,
    # ordered so we can pick the best per ticker by simple iteration.
    rows = (
        db.query(Signal)
        .filter(
            Signal.ticker.in_(tickers),
            Signal.signal_type != "NEUTRAL",
        )
        .order_by(Signal.ticker, desc(Signal.confidence), desc(Signal.generated_at))
        .all()
    )
    best_by_ticker: dict = {}
    for r in rows:
        # First row per ticker wins (highest confidence, then most recent)
        if r.ticker not in best_by_ticker:
            best_by_ticker[r.ticker] = r

    # Parallel price fetch — get_current_price hits an external API (or
    # Yahoo-cached value) per ticker. Serial it dominates the endpoint; a
    # small pool collapses it to single-ticker latency.
    from concurrent.futures import ThreadPoolExecutor
    def _pi(t: str):
        try:
            return t, get_current_price(t)
        except Exception:
            return t, None
    prices: dict = {}
    if tickers:
        with ThreadPoolExecutor(max_workers=min(8, len(tickers)), thread_name_prefix="overview-px") as pool:
            for t, pi in pool.map(_pi, tickers):
                prices[t] = pi

    result = []
    for stock in stocks:
        price_info = prices.get(stock.ticker)
        best = best_by_ticker.get(stock.ticker)
        item = OverviewItem(
            ticker=stock.ticker,
            name=stock.name,
            price=price_info[0] if price_info else None,
            change_pct=price_info[1] if price_info else None,
            signal_type=best.signal_type if best else "NEUTRAL",
            confidence=best.confidence if best else None,
            timeframe=best.timeframe if best else None,
            is_new=best.is_new if best else False,
            auto_trade_enabled=bool(getattr(stock, "auto_trade_enabled", True)),
        )
        result.append(item)
    _overview_cache["payload"] = result
    _overview_cache["expiry"] = time.time() + _OVERVIEW_TTL
    _overview_cache["fingerprint"] = _fp
    return result


@router.get("/{ticker}", response_model=AnalysisResponse)
def get_analysis(ticker: str, refresh: bool = False, db: Session = Depends(get_db)):
    """Full TA analysis for a ticker. Refreshes if requested or no data.

    r53h: removed the hard "ticker must be in watchlist" gate. Operator
    workflow now opens charts directly from position cards / orders for
    tickers they hold but never explicitly added to the watchlist —
    most commonly the underlying of an option position. The analysis
    is bounded compute (one OHLCV fetch + indicators per timeframe) and
    auth-gated by the X-API-Key middleware, so off-watchlist requests
    aren't a meaningful abuse vector for a single-user app.
    """
    ticker = ticker.upper()

    # Check if we have recent signals
    recent_signals = db.query(Signal).filter(Signal.ticker == ticker).order_by(
        desc(Signal.generated_at)
    ).limit(len(ANALYSIS_TIMEFRAMES)).all()

    if refresh or not recent_signals:
        fresh_signals = _run_analysis_for_ticker(ticker, db)
        recent_signals = db.query(Signal).filter(Signal.ticker == ticker).order_by(
            desc(Signal.generated_at)
        ).limit(len(ANALYSIS_TIMEFRAMES)).all()

    # Deduplicate: one signal per timeframe (latest)
    seen_tf = set()
    signals = []
    for s in recent_signals:
        if s.timeframe not in seen_tf:
            seen_tf.add(s.timeframe)
            signals.append(s)

    signal_dicts = [
        {"timeframe": s.timeframe, "signal_type": s.signal_type} for s in signals
    ]
    alignment = get_timeframe_alignment(signal_dicts)

    # Pick primary signal: highest confidence non-neutral, prefer 1d timeframe
    primary = None
    best_conf = 0
    for s in signals:
        if s.signal_type != "NEUTRAL" and s.confidence > best_conf:
            best_conf = s.confidence
            primary = s

    price_info = get_current_price(ticker)
    info = get_ticker_info(ticker)

    # Attach backtest metadata (not stored in DB) to each response
    backtest = _get_ticker_backtest(ticker)

    def _to_response(s) -> SignalResponse:
        resp = SignalResponse.model_validate(s)
        if backtest and resp.signal_type != "NEUTRAL":
            results = backtest.get("results") or []
            same_dir = [r for r in results if r["direction"] == resp.signal_type and r["stats"]["total_trades"] >= 3]
            if same_dir:
                best = max(same_dir, key=lambda r: r["confidence"])
                resp.backtest_score = best["confidence"]
                resp.backtest_best_strategy = best["strategy"]
                resp.backtest_return_pct = best["stats"]["total_return_pct"]
                resp.backtest_win_rate = best["stats"]["win_rate"]
                resp.backtest_trades = best["stats"]["total_trades"]
        return resp

    return AnalysisResponse(
        ticker=ticker,
        name=info.get("name", ticker),
        current_price=price_info[0] if price_info else None,
        change_pct=price_info[1] if price_info else None,
        signals=[_to_response(s) for s in signals],
        primary_signal=_to_response(primary) if primary else None,
        timeframe_alignment=alignment,
    )


# ----- Chart response cache -----------------------------------------------
# The heavy work here (indicators, S/R, zones, fibs, gaps) is purely a
# function of the OHLCV dataframe. Switching timeframes in the UI was
# recomputing all of it every click — 1-3s per click with no state change.
# Cache keyed by (ticker, timeframe, bar_count, last_bar_ts) so it
# auto-invalidates when a new bar arrives without needing a timer.
# Short TTL floor (intraday bars evolve tick-by-tick; give live-quote
# overlay a chance to be fresh) but big enough to make repeated timeframe
# toggling feel instant.
_chart_cache: dict = {}
_CHART_TTL_BY_TF = {
    "5m": 15, "15m": 30, "30m": 45, "1h": 60,
    "4h": 120, "1d": 300, "1mo": 600,
}


# Track which (ticker, tf) pairs have a pre-warm in flight or scheduled so
# we don't fire duplicate background fetches when the user clicks rapidly.
import threading as _t_warm
_warming: set = set()
_warming_lock = _t_warm.Lock()


def _prewarm_other_tfs(ticker: str, current_tf: str) -> None:
    """Background-fetch the other intraday timeframes for the same ticker so
    the next chart click is a cache-hit. Runs once per (ticker, tf) at a time
    — additional triggers while the previous prewarm is still running are
    dropped."""
    # Order matters: warm the close-by timeframes first (user is more likely
    # to switch within a "neighborhood" than across the whole spectrum).
    NEIGHBORS = {
        "5m":  ["15m", "30m", "1h"],
        "15m": ["5m", "30m", "1h"],
        "30m": ["15m", "1h", "5m"],
        "1h":  ["30m", "4h", "15m"],
        "4h":  ["1h", "1d", "30m"],
        "1d":  ["4h", "1h", "1mo"],
        "1mo": ["1d", "4h"],
    }
    targets = NEIGHBORS.get(current_tf, [])
    for tf in targets:
        key = (ticker, tf)
        with _warming_lock:
            if key in _warming:
                continue
            _warming.add(key)
        try:
            df = fetch_ohlcv(ticker, tf)
            if df.empty:
                continue
            # Mirror the endpoint's bar cap so the prewarmed cache entry
            # is keyed on the SAME (len, last_ts) the real request will use.
            _cap_pw = 240 if tf == "1mo" else 600
            if len(df) > _cap_pw:
                df = df.iloc[-_cap_pw:].copy()
            # Compute the same payload the endpoint would compute, so the
            # FOLLOWING chart request hits both the OHLCV cache AND the
            # chart-response cache.
            try:
                df_ind = compute_indicators(df)
                price = float(df_ind["Close"].iloc[-1])
                ind_series = get_chart_indicator_series(df_ind)
                _COLORS = {"SMA20":"#f59e0b","SMA50":"#3b82f6","SMA200":"#ef4444",
                           "EMA9":"#a855f7","EMA21":"#06b6d4","BB_Upper":"#6b7280","BB_Lower":"#6b7280"}
                indicators = [IndicatorLine(name=n, color=_COLORS.get(n, "#888"),
                                            values=[v for v in vals if v["value"] is not None])
                              for n, vals in ind_series.items()]
                candles = [ChartCandle(time=int(ts.timestamp()),
                                       open=round(float(r["Open"]),4), high=round(float(r["High"]),4),
                                       low=round(float(r["Low"]),4), close=round(float(r["Close"]),4),
                                       volume=round(float(r["Volume"]),0))
                           for ts, r in df_ind.iterrows()]
                swing_lvls = swing_levels(df_ind)
                classified = classify_levels_relative_to_price(swing_lvls, price)
                sr_levels = [SupportResistanceLevel(price=l["price"], type=l["type"],
                                                    strength=l.get("strength",1)) for l in classified]
                zones = detect_zones(df_ind, price)
                fib = compute_fib_levels(df_ind)
                gaps = detect_all_gaps(df_ind)
                resp = ChartDataResponse(ticker=ticker, timeframe=tf, candles=candles,
                                         indicators=indicators, support_resistance=sr_levels,
                                         supply_demand_zones=zones, fibonacci=fib, gaps=gaps)
                ck = (ticker, tf, len(df), int(df.index[-1].timestamp()))
                _chart_cache[ck] = (resp, time.time() + _CHART_TTL_BY_TF.get(tf, 60))
            except Exception as e:
                logger.debug(f"prewarm compute failed {ticker} {tf}: {e}")
        except Exception as e:
            logger.debug(f"prewarm fetch failed {ticker} {tf}: {e}")
        finally:
            with _warming_lock:
                _warming.discard(key)


@router.get("/{ticker}/chart", response_model=ChartDataResponse)
def get_chart_data(
    ticker: str,
    timeframe: str = Query("1d", description="Timeframe for chart data"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db)
):
    """OHLCV + indicator series for chart rendering."""
    ticker = ticker.upper()
    if timeframe not in TIMEFRAME_CONFIG:
        raise HTTPException(status_code=400, detail=f"Invalid timeframe: {timeframe}")

    df = fetch_ohlcv(ticker, timeframe)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No data for {ticker} {timeframe}")

    # Cap dataframe BEFORE running heavy compute. Yahoo's intraday endpoint
    # returns extended-hours bars (pre-market 4-9:30 ET + after-hours
    # 16-20 ET = ~2.5x the regular-hours count), so e.g. "10d of 5min"
    # comes back as ~1,900 bars instead of ~780. The chart only shows
    # 50-80 bars by default and SMA200 is the deepest indicator we plot,
    # so 600 bars is ample headroom for indicators + S/R swings + zones.
    # On 5m/15m/1h this drops compute_indicators + detect_zones from
    # ~3-4s to ~0.5s — the dominant chunk of the chart-load wait.
    _CHART_BAR_CAPS = {
        "5m": 600, "15m": 600, "30m": 600, "1h": 600,
        "4h": 600, "1d": 600, "1mo": 240,
    }
    _cap = _CHART_BAR_CAPS.get(timeframe, 600)
    if len(df) > _cap:
        df = df.iloc[-_cap:].copy()

    # Cache lookup — key ties to the actual bars we just fetched so a new
    # bar printing naturally busts the entry even before the TTL expires.
    _ck = (ticker, timeframe, len(df), int(df.index[-1].timestamp()))
    _ce = _chart_cache.get(_ck)
    if _ce is not None:
        _payload, _expiry = _ce
        if time.time() < _expiry:
            if background_tasks is not None:
                background_tasks.add_task(_prewarm_other_tfs, ticker, timeframe)
            return _payload

    df_ind = compute_indicators(df)
    price = float(df_ind["Close"].iloc[-1])

    # Candles
    candles = []
    for ts, row in df_ind.iterrows():
        candles.append(ChartCandle(
            time=int(ts.timestamp()),
            open=round(float(row["Open"]), 4),
            high=round(float(row["High"]), 4),
            low=round(float(row["Low"]), 4),
            close=round(float(row["Close"]), 4),
            volume=round(float(row["Volume"]), 0),
        ))

    # Indicator series
    ind_series = get_chart_indicator_series(df_ind)
    INDICATOR_COLORS = {
        "SMA20": "#f59e0b",
        "SMA50": "#3b82f6",
        "SMA200": "#ef4444",
        "EMA9": "#a855f7",
        "EMA21": "#06b6d4",
        "BB_Upper": "#6b7280",
        "BB_Lower": "#6b7280",
    }
    indicators = [
        IndicatorLine(
            name=name,
            color=INDICATOR_COLORS.get(name, "#888"),
            values=[v for v in vals if v["value"] is not None],
        )
        for name, vals in ind_series.items()
    ]

    # Support/Resistance levels
    swing_lvls = swing_levels(df_ind)
    classified = classify_levels_relative_to_price(swing_lvls, price)
    sr_levels = [
        SupportResistanceLevel(price=lvl["price"], type=lvl["type"], strength=lvl.get("strength", 1))
        for lvl in classified
    ]

    zones = detect_zones(df_ind, price)
    fib = compute_fib_levels(df_ind)
    gaps = detect_all_gaps(df_ind)

    resp = ChartDataResponse(
        ticker=ticker,
        timeframe=timeframe,
        candles=candles,
        indicators=indicators,
        support_resistance=sr_levels,
        supply_demand_zones=zones,
        fibonacci=fib,
        gaps=gaps,
    )
    _chart_cache[_ck] = (resp, time.time() + _CHART_TTL_BY_TF.get(timeframe, 60))
    # Bound memory: keep only the 128 most-recently-written entries.
    if len(_chart_cache) > 128:
        # Drop the oldest-expiring half in one pass.
        _items = sorted(_chart_cache.items(), key=lambda kv: kv[1][1])
        for _k, _ in _items[:64]:
            _chart_cache.pop(_k, None)
    # Pre-warm neighbour timeframes off the request thread so the next
    # click is a cache hit. Runs after the response is sent.
    if background_tasks is not None:
        background_tasks.add_task(_prewarm_other_tfs, ticker, timeframe)
    return resp
