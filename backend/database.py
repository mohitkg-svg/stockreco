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
    __tablename__ = "watchlist"
    ticker = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    # Per-ticker auto-trade gate. Global enable lives on AutoTraderConfig — both
    # must be true for a signal/put-play to open a position.
    auto_trade_enabled = Column(Boolean, default=True)


class Signal(Base):
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
    option_pct_of_equity = Column(Float, default=0.10)
    max_risk_per_trade_pct = Column(Float, default=0.02)  # 2% of equity
    trade_options = Column(Boolean, default=False)    # Master toggle — enables PUT auto-buy for bearish theses
    trade_calls = Column(Boolean, default=False)      # Enables CALL auto-buy for sub-threshold bullish setups
    # Aggressive-options mode: treat options as the PRIMARY growth vehicle.
    # When true: liberalizes call/put triggers, lowers score gate, raises
    # per-ticker option cap, and removes the concentration guard that
    # prevented stacking calls on top of existing stock longs. Meant to be
    # used alongside a 30/70 stock/option budget split.
    aggressive_options_mode = Column(Boolean, default=False)
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
    # Max concurrent open positions across the whole portfolio. Complements
    # max_per_sector (which only bounds correlated exposure).
    max_concurrent_positions = Column(Integer, default=10)
    # Flatten open positions at 15:55 ET (intraday strategy guardrail).
    flatten_by_eod = Column(Boolean, default=False)
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
    idempotency_key = Column(String, index=True, nullable=True)
    # High-water-mark price reached during the trade (for chandelier-exit trail)
    high_water_mark = Column(Float, nullable=True)
    low_water_mark = Column(Float, nullable=True)   # for short trades
    # Sector tag captured at entry (for correlation sizing)
    sector = Column(String, nullable=True)
    # Critical-audit fix #11: snapshot of qty at entry so partial trims at
    # T1/T2 reference a fixed denominator, not the shrinking current qty.
    # Prevents exponential position decay across cascaded trims.
    original_qty = Column(Float, nullable=True)


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
    _ensure_column("auto_trader_config", "max_concurrent_positions", "INTEGER DEFAULT 10")
    _ensure_column("auto_trader_config", "flatten_by_eod", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "trade_calls", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "aggressive_options_mode", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "entry_order_type", "VARCHAR DEFAULT 'market'")
    _ensure_column("auto_trader_config", "use_universe_scanner", "BOOLEAN DEFAULT FALSE")
    _ensure_column("auto_trader_config", "universe_top_n", "INTEGER DEFAULT 30")
    _ensure_column("auto_trader_config", "ticker_blacklist", "VARCHAR DEFAULT ''")
    _ensure_column("auto_trades", "original_qty", "DOUBLE PRECISION")
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
    finally:
        db.close()
