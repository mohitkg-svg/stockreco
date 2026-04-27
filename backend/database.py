"""Database setup, ORM models, and lightweight migrations.

DATABASE_URL selection (in order):
  1. `DATABASE_URL` env var (production: Cloud SQL Postgres via unix
     socket; staging: Neon / managed Postgres).
  2. Fallback to `sqlite:///./stockapp.db` for local dev / tests.

Postgres path: connection-pool tuning is sized for Cloud SQL db-f1-micro
(~25 total connection slots, 3-5 superuser-reserved). Pool size 8 +
overflow 7 = 15 — leaves room for scheduler jobs to initialize
simultaneously without tripping "remaining connection slots reserved".

SQLite path: WAL journal mode + 30s busy_timeout is set via a SQLAlchemy
`connect`-event PRAGMA hook. Required because `manage_open_positions`
holds a session through 1-5s Alpaca REST round-trips while
`consider_signal` from the scan thread may be trying to INSERT — 5s
default busy_timeout wasn't enough.

Migration strategy:
  * `create_tables()` (called from `main.py:lifespan`) runs `metadata.create_all`
    + a small set of additive `_ensure_column` calls. SQLAlchemy
    creates new tables from the model classes; column additions to
    existing tables are handled idempotently by `_ensure_column`.
  * Singleton `AutoTraderConfig` row (id=1) is seeded on first call.
  * For anything more complex than "add a nullable column" use the
    versioned migration runner that tracks applied versions in a
    `schema_migrations` table — defined elsewhere; not yet needed
    in production.

Public surface:
  * `engine`, `SessionLocal`, `Base` — standard SQLAlchemy trinity.
  * `get_db()` — FastAPI dependency yielding a session.
  * Each ORM class is a thin row representation; query patterns live
    in the consuming services.
"""
import os
from sqlalchemy import create_engine, Column, String, Float, DateTime, Integer, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# DATABASE_URL selection order:
#   1. explicit env var (e.g. Neon / Cloud SQL / local Postgres)
#   2. fall back to the on-disk SQLite file used in early development
# Postgres URLs bypass the SQLite-specific connect_args + PRAGMAs below.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./stockapp.db")
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

_engine_kwargs = {}
if _IS_SQLITE:
    # `timeout` is pysqlite's BusyTimeout in seconds — must match (or exceed) the
    # PRAGMA busy_timeout below. Without this the DBAPI gives up after the
    # default 5s, even if SQLite's pragma would have waited longer.
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
else:
    # Postgres: connection pool with pre-ping. Cloud SQL db-f1-micro only
    # has ~25 total slots with 3-5 reserved for superusers, so keep our
    # ceiling well under that. 8+7=15 leaves room for the background
    # scheduler jobs (news poll, universe scanner, best_strategy) to share
    # the same pool without tripping "remaining connection slots reserved"
    # at boot when they all initialize simultaneously.
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_size"] = 8
    _engine_kwargs["max_overflow"] = 7
    _engine_kwargs["pool_recycle"] = 3600
    _engine_kwargs["pool_timeout"] = 30

engine = create_engine(DATABASE_URL, **_engine_kwargs)

# SQLite-only tuning: WAL journal, relaxed sync, long busy timeout. Postgres
# handles concurrency natively, so the PRAGMA event listener is skipped for
# non-SQLite URLs.
from sqlalchemy import event as _sa_event


if _IS_SQLITE:
    @_sa_event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # 30s — `manage_open_positions` holds its session through Alpaca REST
            # round-trips (1–5s each) while `consider_signal` from the scan thread
            # may be trying to INSERT a new auto_trade. 5s wasn't enough; 30s
            # comfortably absorbs the longest manage cycle we've observed.
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class WatchlistStock(Base):
    """User-curated watchlist row. The set of tickers the scanner runs
    against (in addition to the universe-scanner CandidatePool).

    Each row is a single ticker; per-ticker `auto_trade_enabled` lets
    the operator block specific symbols without removing them from the
    watchlist (e.g., during earnings, after a bad post-mortem).
    """
    __tablename__ = "watchlist"
    ticker = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    # Per-ticker auto-trade gate. Global enable lives on AutoTraderConfig — both
    # must be true for a signal/put-play to open a position.
    auto_trade_enabled = Column(Boolean, default=True)


class Signal(Base):
    """One row per emitted signal. Persists every analysis-pipeline
    output — BUY / SELL / NEUTRAL alike — so we have full historical
    record for backtesting (forward-tested signal-vs-realized
    correlation), low-signal-volume alerting (r39), and the analysis-
    pane "recent signals" widget.

    Auto-trader reads from this when deciding whether to open a position;
    every actionable signal also gets the strategy / confidence /
    reasoning fields populated by `services.signal_generator`.

    Schema notes:
      * `signal_type`: literal "BUY", "SELL", or "NEUTRAL"
      * `entry/stop_loss/target1/2/3`: prices; NULL for NEUTRAL
      * `strategy`: free-text label of dominant signal-generator branch
      * `reasoning`: newline-joined human-readable contributors
      * `patterns`: JSON-stringified list of detected pattern names
      * `is_new`: UI flag that flips false after first user view
      * Backtest fields (`backtest_score` etc.) are populated by
        `_apply_backtest_to_signal` when the auto-trader path is invoked.
    """
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, index=True, nullable=False)
    timeframe = Column(String, nullable=False)
    signal_type = Column(String, nullable=False)  # BUY, SELL, NEUTRAL
    confidence = Column(Float, nullable=False)
    entry = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    target1 = Column(Float, nullable=True)
    target2 = Column(Float, nullable=True)
    target3 = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)
    patterns = Column(Text, nullable=True)  # JSON string
    strategy = Column(String, nullable=True)  # which strategy produced this signal
    generated_at = Column(DateTime, default=datetime.utcnow)
    is_new = Column(Boolean, default=True)


class AutoTraderConfig(Base):
    """Singleton config row (id=1) holding auto-trader settings."""
    __tablename__ = "auto_trader_config"
    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False)
    confidence_threshold = Column(Float, default=75.0)
    max_pct_of_equity = Column(Float, default=0.50)   # total cap
    stock_pct_of_equity = Column(Float, default=0.40) # of equity, not of cap
    # r39 audit fix #11: defaulted to 0.10 (10% of equity) but recent paper
    # losses (CNTA -$2440, VTWO -$6500 = 90% of dollar losses) all came from
    # naked options. Cut to 5% pre-live until ≥ 100 closed option trades
    # establish positive expectancy. Operator can raise via /api/trading/
    # auto/config once data exists.
    option_pct_of_equity = Column(Float, default=0.05)
    max_risk_per_trade_pct = Column(Float, default=0.02)  # 2% of equity
    trade_options = Column(Boolean, default=False)    # Master toggle — enables PUT auto-buy for bearish theses
    trade_calls = Column(Boolean, default=False)      # Enables CALL auto-buy for sub-threshold bullish setups
    # Aggressive-options mode: treat options as the PRIMARY growth vehicle.
    # When true: liberalizes call/put triggers, lowers score gate, raises
    # per-ticker option cap, and removes the concentration guard that
    # prevented stacking calls on top of existing stock longs. Meant to be
    # used alongside a 30/70 stock/option budget split.
    aggressive_options_mode = Column(Boolean, default=False)
    # ML scorer toggle. False = shadow mode (predictions logged but multiplier
    # is 1.0 — no effect on signals). Flip to True after evaluating logged
    # predictions vs realized outcomes (typically 1-2 weeks of paper data).
    ml_scoring_enabled = Column(Boolean, default=False)
    # Entry order type: "market" (default) or "limit_at_mid".
    # limit_at_mid submits a limit at (bid+ask)/2 with a 3-min cancel timer.
    # Saves ~half the bid-ask spread on liquid names. For illiquid or
    # fast-moving signals, market is still safer.
    entry_order_type = Column(String, default="market")
    # When true, auto-trader scans the union of watchlist + candidate_pool
    # (top-N tickers from universe scanner). When false, watchlist only.
    use_universe_scanner = Column(Boolean, default=False)
    # How many candidates the scanner keeps in the pool.
    universe_top_n = Column(Integer, default=30)
    # CSV of ticker symbols to never auto-trade (stock or options). Applied
    # in consider_signal / consider_call_play / consider_put_play and by the
    # universe scanner (which skips blacklisted names from the pool).
    ticker_blacklist = Column(String, default="")
    # CSV of timeframes whose signals are eligible to open auto-trades. Anything
    # not on this list (e.g. "1mo", "5m") is ignored even if confidence > gate.
    signal_timeframes = Column(String, default="1h,4h,1d")
    # Default ATR multiplier for stop-distance calibration. The signal generator
    # writes a structurally-anchored stop, but if it sits inside this multiple
    # of ATR we widen it to noise-survival distance.
    stop_atr_mult = Column(Float, default=2.0)
    # Chandelier-exit ATR multiplier — once a trade is in profit (level_index>0)
    # we additionally trail the stop at HWM - mult×ATR. Most aggressive of
    # state-machine and chandelier wins. 0 disables chandelier overlay.
    chandelier_atr_mult = Column(Float, default=3.0)
    # Dry-run: log + record AutoTrade rows but skip Alpaca order submission.
    # Useful for validating signal quality / sizing without paying spreads.
    dry_run = Column(Boolean, default=False)
    # Max concurrent open positions in the same sector — soft correlation cap.
    max_per_sector = Column(Integer, default=3)
    # Daily loss limit — realized PnL gate. When today's realized PnL drops
    # below -(equity × daily_loss_limit_pct), the scanner stops opening new
    # positions until tomorrow. 0 disables (not recommended for live).
    daily_loss_limit_pct = Column(Float, default=0.03)  # 3% of equity
    # Kill-switch — when set, auto_trader.consider_signal short-circuits
    # immediately regardless of `enabled`. Toggled via POST /api/trading/kill.
    # Separate from `enabled` so a kill persists across deploys / restarts
    # without a human rearming the scanner by accident.
    killed = Column(Boolean, default=False)
    killed_at = Column(DateTime, nullable=True)
    killed_reason = Column(String, nullable=True)
    # r41 review fix B: PDT enforcement. False (default) = informational
    # only (paper-account behavior). Flip to True when going live with a
    # margin account < $25k — the gate then blocks new entries when 3+
    # day-trades have already happened in the trailing 5 business days
    # (preventing a 4th which would trigger a 90-day PDT lock).
    pdt_enforce = Column(Boolean, default=False)
    # r41-promote-auto: when True, the periodic reconcile job calls
    # sync_positions_from_alpaca + promote_adopted_to_managed in
    # sequence — every external position the bot finds is auto-adopted
    # AND auto-promoted to bot management with bot-computed levels.
    # When False (default), the periodic job only alerts via
    # detect_unexpected_positions; reconciliation is operator-driven.
    auto_promote_adopted = Column(Boolean, default=False)
    # Max concurrent open positions across the whole portfolio. Complements
    # max_per_sector (which only bounds correlated exposure).
    max_concurrent_positions = Column(Integer, default=10)
    # Flatten open positions at 15:55 ET (intraday strategy guardrail).
    # Pre-live default: flatten everything by EOD so overnight gap risk is
    # contained during the initial live-trading phase. Flip to False after
    # calibration is solid and gap risk has been stress-tested.
    flatten_by_eod = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AutoTrade(Base):
    """One row per automated entry. Tracks lifecycle from signal → fill → exit."""
    __tablename__ = "auto_trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, index=True, nullable=False)        # underlying
    symbol = Column(String, nullable=False)                    # actual traded symbol (stock or OCC)
    asset_type = Column(String, nullable=False, default="stock")  # "stock" | "option"
    side = Column(String, nullable=False)                      # "buy" | "sell"
    qty = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=True)                 # filled avg
    requested_entry = Column(Float, nullable=True)             # signal entry
    stop_loss = Column(Float, nullable=False)
    current_stop = Column(Float, nullable=False)               # mutated as price progresses
    target1 = Column(Float, nullable=True)
    target2 = Column(Float, nullable=True)
    target3 = Column(Float, nullable=True)
    # Trail-state machine: 0 = before T1, 1 = past T1 (stop@entry),
    # 2 = past T2 (stop@T1), 3 = past T3 (stop@T2 + recalc), 4 = past new-T1 (stop@old-T3), …
    level_index = Column(Integer, default=0)
    # Audit log: JSON list of target sets used over the trade lifetime
    targets_history = Column(Text, nullable=True)
    hit_t1 = Column(Boolean, default=False)
    signal_id = Column(Integer, nullable=True)
    parent_order_id = Column(String, nullable=True)
    stop_order_id = Column(String, nullable=True)
    tp_order_id = Column(String, nullable=True)
    status = Column(String, default="pending")                 # pending|open|closed_target|closed_stop|closed_manual|error
    note = Column(Text, nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow)
    filled_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    realized_pl = Column(Float, nullable=True)
    # Post-mortem JSON populated when a stop closes the trade at a loss.
    # Shape: {verdict, summary, findings: [{title, body, severity}], lessons: [...], price_path: [...]}
    post_mortem = Column(Text, nullable=True)
    # Idempotency hash (ticker+signal_type+rounded entry/stop/T1) — prevents
    # the same signal from opening duplicate trades within the same scan window.
    # r46 fix #0.8: unique=True so the DB is the final guard against
    # duplicate concurrent inserts (multi-instance autoscale + Cloud Run
    # deploy overlap). Two scanners passing the dedup query before either
    # commits → second commit hard-fails with IntegrityError instead of
    # silently double-positioning.
    idempotency_key = Column(String, index=True, unique=True, nullable=True)
    # High-water-mark price reached during the trade (for chandelier-exit trail)
    high_water_mark = Column(Float, nullable=True)
    low_water_mark = Column(Float, nullable=True)   # for short trades
    # Sector tag captured at entry (for correlation sizing)
    sector = Column(String, nullable=True)
    # Critical-audit fix #11: snapshot of qty at entry so partial trims at
    # T1/T2 reference a fixed denominator, not the shrinking current qty.
    # Prevents exponential position decay across cascaded trims.
    original_qty = Column(Float, nullable=True)
    # r37: Persisted target-touch counter so the 2-tick debounce survives
    # Cloud Run instance restarts. Without persistence, a price spike that
    # crosses a target right after a redeploy would advance the trail on
    # the very first tick (no debounce state to consult), occasionally
    # chopping out winners on a 1-bar wick.
    target_touch_count = Column(Integer, default=0)
    # r41 review fix C: For OPTIONS, `requested_entry` is the option
    # PREMIUM (e.g. $2.00), not the underlying price. The premium-stop
    # spread-artifact guard previously compared current underlying
    # ($500) against premium ($2.00) and always evaluated to "against
    # us" — making the spread-artifact window fire premium-stops
    # incorrectly on every option trade in the first 5 minutes.
    # `underlying_entry_price` stores the underlying's price at the
    # moment of option entry so the comparison is meaningful.
    # Stocks: this column is None (use entry_price as before).
    # Options: set from thesis["entry"] in consider_call/put_play.
    underlying_entry_price = Column(Float, nullable=True)


class CandidatePool(Base):
    """Universe scanner output — top-N tickers currently exhibiting a viable
    setup. Populated by `services/universe_scanner.py` every 15 minutes.
    Auto-trader's scan reads from the union of (watchlist, candidate_pool)
    when `cfg.use_universe_scanner` is true.
    """
    __tablename__ = "candidate_pool"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    sector = Column(String, nullable=True)
    price = Column(Float, nullable=True)
    score = Column(Float, nullable=False, index=True)  # composite pre-filter score
    rvol = Column(Float, nullable=True)
    rs_20d = Column(Float, nullable=True)
    rs_60d = Column(Float, nullable=True)
    adx = Column(Float, nullable=True)
    pct_from_52w_high = Column(Float, nullable=True)
    reason = Column(String, nullable=True)        # human-readable setup tag
    generated_at = Column(DateTime, default=datetime.utcnow, index=True)


class BestStrategyPerTicker(Base):
    """Cached winner of the per-ticker walk-forward backtest.

    Updated weekly by `compute_best_strategy_per_ticker()`. The signal
    generator uses this to preferentially emit signals from the strategy
    that has demonstrated edge on THIS ticker over the holdout window.
    """
    __tablename__ = "best_strategy_per_ticker"
    ticker = Column(String, primary_key=True)
    strategy = Column(String, nullable=False)
    direction = Column(String, nullable=False)     # BUY|SELL
    confidence = Column(Float, nullable=False)
    oos_trades = Column(Integer, nullable=True)
    win_rate = Column(Float, nullable=True)
    avg_pl = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConfidenceCalibration(Base):
    """Per-bucket realized win-rate from closed auto-trades.

    Populated nightly by `compute_confidence_calibration`. Used by
    `consider_signal` to apply an empirical multiplier on the risk budget:
    high-confidence buckets that have underperformed get shrunk, buckets
    that beat expectation get boosted. Closes the loop that was previously
    just being logged.
    """
    __tablename__ = "confidence_calibration"
    id = Column(Integer, primary_key=True, autoincrement=True)
    bucket = Column(String, index=True, unique=True, nullable=False)  # "70-79" etc.
    n = Column(Integer, nullable=False)
    win_rate = Column(Float, nullable=False)      # 0..1
    avg_pl = Column(Float, nullable=False)
    multiplier = Column(Float, nullable=False, default=1.0)  # risk-budget multiplier
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Alert(Base):
    """Operator-facing alerts — emitted by critical code paths (SL resubmit
    failures, BP circuit breaker trips, broker-down events, option assignment
    surprises, kill-switch activations). Rendered in the UI header as an
    unread-count bell so operators notice problems before they cost money.

    Severity: "critical" > "error" > "warning" > "info".
    """
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    severity = Column(String, index=True, nullable=False)  # critical/error/warning/info
    category = Column(String, index=True, nullable=False)  # e.g. "sl_invariant", "bp_breaker"
    message = Column(Text, nullable=False)
    ticker = Column(String, nullable=True)
    trade_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    acked_at = Column(DateTime, nullable=True)


class NewsEvent(Base):
    """One row per fetched news article, de-duplicated on `external_id`.

    Populated by services.news.poll_watchlist every 2 minutes. Phase 1 is
    read-only observability — auto-trader does NOT read this table yet.
    The `trade_id` FK is intentionally absent: we join news ↔ trades at
    query time (by ticker + time overlap) so historical news analysis
    works for trades that were closed before news ingestion started.
    """
    __tablename__ = "news_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    external_id = Column(String, unique=True, index=True, nullable=False)  # Alpaca news id
    ticker = Column(String, index=True, nullable=False)   # primary ticker (first mentioned symbol)
    symbols = Column(String, nullable=True)               # comma-separated list of all tickers mentioned
    source = Column(String, nullable=True)                # Benzinga / Reuters / etc
    author = Column(String, nullable=True)
    headline = Column(String, nullable=False)
    summary = Column(Text, nullable=True)
    url = Column(String, nullable=True)
    published_at = Column(DateTime, index=True, nullable=False)  # article timestamp
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # VADER compound score in [-1, +1]. Higher = more positive.
    sentiment_score = Column(Float, nullable=True)
    sentiment_label = Column(String, nullable=True)  # positive | negative | neutral
    # severity = abs(sentiment_score) * 100, rounded — a 0-100 score for
    # how strongly-signed the sentiment is, independent of direction.
    severity = Column(Integer, nullable=True)


class InstitutionalHoldings(Base):
    """Aggregated institutional-ownership snapshot per ticker.

    Sourced from yfinance `.institutional_holders` + `.mutualfund_holders`
    (which under the hood come from SEC 13F filings). Weekly refresh.
    Slow-moving signal (quarterly, ~45d lag). Useful mostly as a
    rising-vs-falling institutional-interest tilt.

    - `total_holders` = count of top-10 institutional + top-10 mutual fund holders
    - `weighted_pct_change_qoq` = position-weighted mean of pctChange across
      the top holders (positive = net institutional accumulation last quarter)
    - `new_initiation_count` = holders whose position is <=1 quarter old
    """
    __tablename__ = "institutional_holdings"
    ticker = Column(String, primary_key=True)
    as_of_quarter = Column(String, nullable=True)       # e.g. "2026Q1"
    total_holders = Column(Integer, nullable=True)
    weighted_pct_change_qoq = Column(Float, nullable=True)   # -1.0..+inf (0.25 = +25%)
    new_initiation_count = Column(Integer, nullable=True)
    top_holder_name = Column(String, nullable=True)
    top_holder_pct_held = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class InsiderSummary(Base):
    """Per-ticker rollup of SEC Form 4 insider transactions.

    Populated weekly. 30d + 90d aggregates of director/officer buys and
    sells. High insider-buy ratios are empirically predictive on small/mid
    caps (C-suite has information asymmetry + skin in the game). Less
    informative on mega-caps where insider trades are mostly 10b5-1
    scheduled dispositions.

    Data source: SEC EDGAR (free, no API key).
      https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=4&...
    """
    __tablename__ = "insider_summary"
    ticker = Column(String, primary_key=True)
    buy_count_30d = Column(Integer, nullable=True)
    buy_count_90d = Column(Integer, nullable=True)
    sell_count_30d = Column(Integer, nullable=True)
    sell_count_90d = Column(Integer, nullable=True)
    net_buy_ratio_90d = Column(Float, nullable=True)      # buys / (buys + sells), None if 0 total
    buy_dollar_90d = Column(Float, nullable=True)         # sum of $ value of insider purchases
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class WSBMention(Base):
    """r/wallstreetbets ticker-mention rollup.

    Pulled every 30 min from Reddit's public JSON API. Counts top-level
    post + comment mentions of each watchlist/pool ticker. The 7d z-score
    vs 30d baseline catches squeeze setups (sudden spike in retail
    attention). Signal is meaningful on retail-driven/meme tickers and
    low-float squeezes; near-zero on mega-caps where retail flow is
    already priced into the tape.
    """
    __tablename__ = "wsb_mentions"
    ticker = Column(String, primary_key=True)
    mentions_24h = Column(Integer, nullable=True)
    mentions_7d = Column(Integer, nullable=True)
    mentions_7d_zscore = Column(Float, nullable=True)   # vs 30d rolling baseline
    bullish_hint_24h = Column(Integer, nullable=True)   # "calls", "moon", "yolo"
    bearish_hint_24h = Column(Integer, nullable=True)   # "puts", "short", "crash"
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class SocialSentiment(Base):
    """Per-ticker retail social sentiment snapshot.

    Currently backed by Stocktwits public API. Weighted rolling 24h
    bullish/bearish message counts + 7d trend. Useful primarily on
    retail-driven / meme tickers; near-zero signal on liquid mega-caps
    where the tape itself already reflects retail flow.
    """
    __tablename__ = "social_sentiment"
    ticker = Column(String, primary_key=True)
    source = Column(String, nullable=False, default="stocktwits")
    message_count_24h = Column(Integer, nullable=True)
    bullish_pct_24h = Column(Float, nullable=True)     # 0..1
    bearish_pct_24h = Column(Float, nullable=True)     # 0..1 (bullish + bearish ≤ 1)
    message_count_7d_zscore = Column(Float, nullable=True)  # vs 30-day baseline
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class MLArtifact(Base):
    """Persisted ML training artifacts (model bytes, meta JSON, status JSON).

    Cloud Run /tmp is per-instance, so a model trained on instance A is
    invisible to instance B. Storing artifacts here makes them durable
    across container churn and scale events. Single-row-per-name pattern
    so writes overwrite cleanly.
    """
    __tablename__ = "ml_artifacts"
    name = Column(String, primary_key=True)         # 'model' | 'meta' | 'status'
    content = Column(Text, nullable=True)           # base64 for binary, json for text
    is_binary = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MLPrediction(Base):
    """Logged ML scorer predictions vs realized outcomes.

    Every signal that reaches the scorer produces a row. After the trade
    closes (if it becomes a trade), we backfill `realized_pl` and `outcome`
    so calibration plots can compare predicted P(win) to actual win rate.

    Drives shadow-mode evaluation: train + log for 1-2 weeks, plot bucket
    win rates, decide whether to flip ml_scoring_enabled=True.
    """
    __tablename__ = "ml_predictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, index=True, nullable=False)
    signal_type = Column(String, nullable=False)        # BUY|SELL
    timeframe = Column(String, nullable=False)
    predicted_winrate = Column(Float, nullable=False)   # 0..1
    signal_confidence = Column(Float, nullable=True)    # confidence at gen time
    trade_id = Column(Integer, nullable=True, index=True)  # set if a trade fired
    outcome = Column(Integer, nullable=True)            # 1=win, 0=loss, null=open
    realized_pl = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    closed_at = Column(DateTime, nullable=True)


class MacroEvent(Base):
    """Scheduled US macroeconomic releases with consensus expectation and
    (post-release) actual value.

    Populated daily by services/macro_calendar.py. Auto-trader reads this to
    impose pre-release / post-release blackout windows on new entries.

    Importance: high = market-moving (CPI/PPI/NFP/FOMC/GDP/PCE),
                medium = ISM/Retail/Sentiment,
                low = minor data points.
    """
    __tablename__ = "macro_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String, index=True, nullable=False)  # e.g. "CPI", "FOMC"
    event_name = Column(String, nullable=False)             # human-readable
    country = Column(String, default="US", nullable=False)
    importance = Column(String, index=True, nullable=False) # high|medium|low
    release_time_utc = Column(DateTime, index=True, nullable=False, unique=False)
    consensus = Column(Float, nullable=True)                # market expectation
    actual = Column(Float, nullable=True)                   # post-release value
    unit = Column(String, nullable=True)                    # %, M, K, etc.
    surprise_pct = Column(Float, nullable=True)             # (actual-consensus)/consensus
    released_at = Column(DateTime, nullable=True)           # when actual was fetched
    fred_series_id = Column(String, nullable=True)          # optional FRED series for fetch
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Fundamentals(Base):
    """Per-ticker fundamental snapshot.

    Pulled weekly (and on-demand) from yfinance .info. Most fields update
    quarterly with earnings — they don't change between fetches in the same
    week. We hash the stable fields and skip writes when nothing changed,
    so the table records a *history of changes* rather than weekly noise.

    Signal generator uses `quality_score` (a -100..+100 composite) to apply
    a small confidence multiplier — high-quality balance sheet + growth
    boosts BUY conviction; junk fundamentals dampen it.
    """
    __tablename__ = "fundamentals"
    ticker = Column(String, primary_key=True)
    sector = Column(String, nullable=True, index=True)
    industry = Column(String, nullable=True)
    market_cap = Column(Float, nullable=True)
    shares_outstanding = Column(Float, nullable=True)

    # Valuation
    pe_ratio = Column(Float, nullable=True)            # trailing P/E
    pe_forward = Column(Float, nullable=True)
    peg_ratio = Column(Float, nullable=True)
    price_to_book = Column(Float, nullable=True)
    price_to_sales = Column(Float, nullable=True)
    ev_to_ebitda = Column(Float, nullable=True)

    # Growth
    revenue_growth_yoy = Column(Float, nullable=True)  # decimal e.g. 0.18 = 18%
    earnings_growth_yoy = Column(Float, nullable=True)

    # Profitability
    profit_margin = Column(Float, nullable=True)
    operating_margin = Column(Float, nullable=True)
    return_on_equity = Column(Float, nullable=True)
    return_on_assets = Column(Float, nullable=True)

    # Balance sheet / liquidity
    debt_to_equity = Column(Float, nullable=True)
    current_ratio = Column(Float, nullable=True)

    # Cash flow / income
    free_cash_flow = Column(Float, nullable=True)
    dividend_yield = Column(Float, nullable=True)

    # Risk — 5y beta vs SPY. Used to beta-weight portfolio heat: 5 trades in
    # high-beta tech aren't equivalent to 5 trades in utilities.
    beta = Column(Float, nullable=True)

    # Short interest — % of float shorted + days-to-cover (short ratio).
    # High SI is bimodal for longs: squeeze-upside vs fundamental-skepticism.
    # Signal_generator uses it to gate against crowded shorts when going long
    # and to amplify shorts that already have squeeze pressure.
    short_pct_float = Column(Float, nullable=True)   # 0..1 (0.15 = 15%)
    short_ratio = Column(Float, nullable=True)       # days-to-cover

    # Composite — see services.fundamentals.compute_quality_score
    quality_score = Column(Float, nullable=True, index=True)

    # Change-detection: SHA256 over the stable numeric fields. If the new
    # fetch hashes identical, we only update last_checked_at — saves write
    # I/O and produces a clean "what actually changed" timeline.
    data_hash = Column(String, nullable=True, index=True)

    last_checked_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_changed_at = Column(DateTime, default=datetime.utcnow)


class AnalystRating(Base):
    """Aggregated Wall Street analyst consensus per ticker.

    Pulled from yfinance .info: `recommendationMean` is a 1-5 scale where
    1=StrongBuy, 2=Buy, 3=Hold, 4=Sell, 5=StrongSell. `recommendationKey`
    is the string label, `numberOfAnalystOpinions` is coverage count.

    Refreshed 4× per day. Signal generator reads this to apply a light
    confidence multiplier — strong consensus in the direction of the signal
    nudges confidence up, consensus against nudges it down. Slow-moving
    signal, so ±10-12% is the correct weighting envelope.
    """
    __tablename__ = "analyst_ratings"
    ticker = Column(String, primary_key=True)
    mean = Column(Float, nullable=True)            # 1.0 (StrongBuy) .. 5.0 (StrongSell)
    key = Column(String, nullable=True)            # "strong_buy" | "buy" | "hold" | "sell" | "strong_sell"
    analyst_count = Column(Integer, nullable=True)
    target_mean = Column(Float, nullable=True)     # consensus price target
    target_high = Column(Float, nullable=True)
    target_low = Column(Float, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class TickerProfile(Base):
    """r46 Tier 1: per-ticker overrides + cached statistics. Bot used to be
    fully uniform (TSLA's 4%/day vol got the same ATR-mult floor as KO's
    0.7%/day). Populated by the weekly walk-forward best-strategy run; reads
    are best-effort with global config fallback at every site.
    """
    __tablename__ = "ticker_profiles"
    ticker = Column(String, primary_key=True)
    realized_vol_30d = Column(Float, nullable=True)        # daily stdev, 30d
    vol_mult = Column(Float, default=1.0, nullable=True)   # ATR-mult scaler
    beta_60d_realized = Column(Float, nullable=True)
    confidence_threshold_override = Column(Float, nullable=True)
    median_chain_spread_pct = Column(Float, nullable=True)
    min_rr_override = Column(Float, nullable=True)
    min_dte_override = Column(Integer, nullable=True)
    trend_persistence_score = Column(Float, nullable=True)
    chandelier_mult_override = Column(Float, nullable=True)
    has_earnings_calendar = Column(Boolean, default=True, nullable=True)
    correlation_cluster_id = Column(String, nullable=True)
    news_count_p50_30d = Column(Float, nullable=True)
    median_winning_hold_bars = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class EquitySnapshot(Base):
    """r46 fix #0.2: persisted equity timeseries so multi-day drawdown
    tracking actually works. Prior code referenced
    `paper_trader.get_portfolio_history()` which doesn't exist; the
    fallback used last_equity (1-day session DD) as a stand-in for 60-day
    DD, silently degrading the graduated DD multiplier from r44 #1.2.

    Recorder writes one row every 5 minutes during RTH plus one at EOD.
    `account_drawdown_multiplier` reads peak-to-trough over the configured
    horizon (default 60d).
    """
    __tablename__ = "equity_snapshots"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, index=True, nullable=False, default=datetime.utcnow)
    equity = Column(Float, nullable=False)
    cash = Column(Float, nullable=True)
    buying_power = Column(Float, nullable=True)
    realized_pl_today = Column(Float, nullable=True)
    unrealized_pl = Column(Float, nullable=True)
    open_positions = Column(Integer, nullable=True)
    spy_close = Column(Float, nullable=True)             # SPY price for benchmark overlay


class AIDecisionLog(Base):
    """Audit log for every Claude judge call (entry_veto, news_exit,
    confidence_multiplier).

    Used to review shadow-mode decisions before flipping a call site to
    `active`. Each row captures the inputs (compressed), the model's
    response, latency, and whether we honored the verdict. Indexed on
    `created_at` so the operator can filter recent decisions; on
    `call_site` so per-channel analysis is fast.

    `prompt_summary` and `response` are stored as JSON strings to keep
    the table schema flat across DB engines (SQLite + Postgres).
    """
    __tablename__ = "ai_decision_log"
    id = Column(Integer, primary_key=True)
    call_site = Column(String, index=True)            # entry_veto | news_exit | confidence_multiplier
    mode = Column(String)                             # off | shadow | active
    prompt_summary = Column(String)                   # JSON-stringified compact context
    response = Column(String)                         # JSON-stringified verdict + reason
    latency_ms = Column(Integer)
    honored = Column(Boolean, default=False)
    error = Column(String, nullable=True)             # null on success, "abstain" / msg otherwise
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


def _ensure_column(table: str, column: str, ddl: str):
    """Tiny SQLite migration helper — ALTER TABLE ADD COLUMN if missing.

    Note: this only handles the trivial "add nullable column" case. For
    anything more (rename, type change, drop, data backfill) use the
    `_apply_migrations` runner below — it tracks applied versions in a
    `schema_migrations` table so each upgrade runs exactly once.
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns(table)}
    if column in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


# ---- Numbered schema migrations ------------------------------------------
# Each entry is (version_int, description, callable(connection)).
# The runner records applied versions in `schema_migrations`; never edit a
# past entry — add a new one with a higher version number.
def _mig_001_init(_conn):
    """Sentinel: schema_migrations table is created by the runner itself."""
    pass


_MIGRATIONS = [
    (1, "init schema_migrations table", _mig_001_init),
]


def _apply_migrations():
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version INTEGER PRIMARY KEY, "
            "  description TEXT NOT NULL, "
            "  applied_at DATETIME DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))
        applied = {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}
    for version, desc, fn in _MIGRATIONS:
        if version in applied:
            continue
        try:
            with engine.begin() as conn:
                fn(conn)
                conn.execute(text(
                    "INSERT INTO schema_migrations (version, description) VALUES (:v, :d)"
                ), {"v": version, "d": desc})
        except Exception as e:
            import logging as _lg
            _lg.getLogger(__name__).error(f"migration v{version} ({desc}) FAILED: {e}")
            raise


def create_tables():
    Base.metadata.create_all(bind=engine)
    # Versioned migrations run BEFORE the legacy _ensure_column block so
    # numbered upgrades can rely on the base schema being present.
    _apply_migrations()
    # Migrations for columns added after the initial schema
    _ensure_column("auto_trades", "post_mortem", "TEXT")
    _ensure_column("auto_trades", "target3", "FLOAT")
    _ensure_column("auto_trades", "level_index", "INTEGER DEFAULT 0")
    _ensure_column("auto_trades", "targets_history", "TEXT")
    _ensure_column("watchlist", "auto_trade_enabled", "BOOLEAN DEFAULT 1")
    _ensure_column("auto_trader_config", "signal_timeframes", "VARCHAR DEFAULT '1h,4h,1d'")
    _ensure_column("auto_trader_config", "stop_atr_mult", "FLOAT DEFAULT 2.0")
    _ensure_column("auto_trader_config", "chandelier_atr_mult", "FLOAT DEFAULT 3.0")
    _ensure_column("auto_trader_config", "dry_run", "BOOLEAN DEFAULT 0")
    _ensure_column("auto_trader_config", "max_per_sector", "INTEGER DEFAULT 3")
    _ensure_column("auto_trades", "idempotency_key", "VARCHAR")
    _ensure_column("auto_trades", "high_water_mark", "FLOAT")
    _ensure_column("auto_trades", "low_water_mark", "FLOAT")
    _ensure_column("auto_trades", "sector", "VARCHAR")
    # Real-money safety columns (kill switch, daily-loss gate, position caps)
    # — added as nullable with sensible defaults so existing rows are fine.
    # DDL uses DOUBLE PRECISION for Postgres compat; SQLite accepts FLOAT too.
    _ensure_column("auto_trader_config", "daily_loss_limit_pct", "DOUBLE PRECISION DEFAULT 0.03")
    _ensure_column("auto_trader_config", "killed", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "killed_at", "TIMESTAMP")
    _ensure_column("auto_trader_config", "killed_reason", "VARCHAR")
    _ensure_column("auto_trader_config", "pdt_enforce", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "auto_promote_adopted", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "max_concurrent_positions", "INTEGER DEFAULT 10")
    # Legacy default (FALSE) preserved for existing rows; new rows get TRUE via ORM default
    _ensure_column("auto_trader_config", "flatten_by_eod", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "trade_calls", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "aggressive_options_mode", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "ml_scoring_enabled", "BOOLEAN DEFAULT FALSE")
    _ensure_column("fundamentals", "beta", "FLOAT")
    _ensure_column("fundamentals", "short_pct_float", "FLOAT")
    _ensure_column("fundamentals", "short_ratio", "FLOAT")
    _ensure_column("auto_trader_config", "entry_order_type", "VARCHAR DEFAULT 'market'")
    _ensure_column("auto_trader_config", "use_universe_scanner", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "universe_top_n", "INTEGER DEFAULT 30")
    _ensure_column("auto_trader_config", "ticker_blacklist", "VARCHAR DEFAULT ''")
    _ensure_column("auto_trades", "original_qty", "DOUBLE PRECISION")
    _ensure_column("auto_trades", "target_touch_count", "INTEGER DEFAULT 0")
    _ensure_column("auto_trades", "underlying_entry_price", "DOUBLE PRECISION")
    # Seed singleton config row if missing
    db = SessionLocal()
    try:
        if not db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first():
            db.add(AutoTraderConfig(id=1))
            db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # r44 fix Wave 6: rollback on exception so the session isn't tainted
        # for the next caller. On Postgres autocommit-rollback is implicit;
        # on SQLite-WAL the writer-lock release happens here.
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()
