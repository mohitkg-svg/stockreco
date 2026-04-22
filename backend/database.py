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
    # Postgres: use a small connection pool with pre-ping so idle connections
    # that Neon/Cloud SQL may drop are recycled transparently.
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 5

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
    trade_options = Column(Boolean, default=False)    # off by default — needs option-trading approval
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
