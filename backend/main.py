import logging
import os
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager


# ----- Load backend/.env BEFORE any service import ------------------------
# Nothing in the stack pulls python-dotenv, so .env on disk was being ignored
# (live_quotes / paper_trader read os.getenv at import time and would silently
# disable themselves). This 10-line parser handles `KEY=value`, `KEY="quoted"`,
# and `# comments` — enough for our 4 keys; we don't need full bash semantics.
def _load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Don't clobber an already-exported real shell var.
                os.environ.setdefault(key, val)
    except Exception as _e:
        # Don't crash the boot — the warning surfaces below if vars are missing.
        print(f"[.env] could not parse {path}: {_e}")


_load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler
from database import create_tables, SessionLocal, WatchlistStock
from routers import watchlist, analysis, backtest, options, stream, trading
from routers.analysis import _run_analysis_for_ticker
from routers._auth import require_api_key, auth_configured
from services import live_quotes, auto_trader, metrics

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

# ----- Logging: stderr + rotating file ------------------------------------
# A persistent on-disk log makes post-hoc diagnosis possible (uvicorn's terminal
# scrollback evaporates on restart). Rotates at 5 MB × 5 files = 25 MB cap.
# Override the path with LOG_DIR env var if you want it elsewhere.
LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "backend.log")

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)

# Idempotent setup — uvicorn's --reload re-imports this module; without the
# guard you'd stack duplicate handlers and write each line N times.
_LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_formatter = logging.Formatter(_LOG_FMT)

if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
           for h in _root_logger.handlers):
    _stream_h = logging.StreamHandler()
    _stream_h.setFormatter(_formatter)
    _root_logger.addHandler(_stream_h)

if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == LOG_FILE
           for h in _root_logger.handlers):
    _file_h = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
    _file_h.setFormatter(_formatter)
    _root_logger.addHandler(_file_h)

# Make uvicorn's loggers funnel through the same handlers (they default to
# their own stderr handler and would otherwise skip the file).
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _ul = logging.getLogger(_name)
    _ul.handlers = []        # drop uvicorn's default stderr handler
    _ul.propagate = True     # let the root handlers do the work

logger = logging.getLogger(__name__)
logger.info(f"Logging to {LOG_FILE} (rotating 5MB × 5)")


# ----- Rate-limit noisy third-party loggers -------------------------------
# Alpaca's websocket SDK retries every 1–3s on connection errors and logs each
# retry at ERROR. A transient outage (e.g. concurrent-connection limit while
# another process holds the socket) fills the log with thousands of identical
# lines. Keep the FIRST occurrence of each distinct message, then suppress
# repeats for 60s. Genuinely new errors still surface promptly.
class _RateLimitFilter(logging.Filter):
    def __init__(self, interval_sec: float = 60.0):
        super().__init__()
        self.interval = interval_sec
        self._last: dict = {}

    def filter(self, record: logging.LogRecord) -> bool:
        import time as _t
        # Key on the unformatted message template so args-varying lines still dedupe.
        key = (record.name, record.levelno, record.msg)
        now = _t.monotonic()
        last = self._last.get(key, 0.0)
        if now - last < self.interval:
            return False
        self._last[key] = now
        return True


_ws_rate_limit = _RateLimitFilter(interval_sec=60.0)
for _noisy in ("alpaca.common.websocket", "alpaca.data.live.websocket"):
    logging.getLogger(_noisy).addFilter(_ws_rate_limit)

scheduler = BackgroundScheduler()

# Lifecycle health flags — surfaced via /api/health so deployments can detect
# silent boot failures (e.g. live_quotes.start crashed but app booted anyway).
_app_health = {
    "scheduler_started": False,
    "live_quotes_started": False,
    "live_quotes_error": None,
    "last_scan_at": None,
    "last_manage_at": None,
}


def scheduled_scan():
    """Scan all watchlist stocks and update signals in DB."""
    from datetime import datetime as _dt, timezone as _tz
    db = SessionLocal()
    try:
        stocks = db.query(WatchlistStock).all()
        for stock in stocks:
            logger.info(f"Auto-scanning {stock.ticker}")
            try:
                _run_analysis_for_ticker(stock.ticker, db)
            except Exception as e:
                logger.error(f"Scan error for {stock.ticker}: {e}")
    finally:
        db.close()
    _app_health["last_scan_at"] = _dt.now(_tz.utc).isoformat()


def _record_manage_tick():
    from datetime import datetime as _dt, timezone as _tz
    _app_health["last_manage_at"] = _dt.now(_tz.utc).isoformat()


def _scheduled_manage():
    """Wrap auto_trader.manage_open_positions to timestamp the tick in health."""
    try:
        auto_trader.manage_open_positions()
    finally:
        _record_manage_tick()


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    # Audit fix D3: explicit max_instances=1 + coalesce so a slow scan doesn't
    # stack a second one. Also ties both jobs to a 60s misfire grace window.
    scheduler.add_job(
        scheduled_scan, "interval", minutes=15, id="watchlist_scan",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    # Trail stops to break-even at T1 and reconcile filled exits every 60s.
    scheduler.add_job(
        _scheduled_manage, "interval", seconds=60, id="auto_trader_manage",
        max_instances=1, coalesce=True, misfire_grace_time=30,
    )
    # F1: Nightly confidence-vs-realized calibration (03:10 UTC = 23:10 ET).
    # Pure read-only aggregation — logs the win-rate per confidence bucket
    # so miscalibration (e.g. "80-conf bucket wins less than 60-conf")
    # becomes visible without having to eyeball the trades table.
    try:
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            auto_trader.compute_confidence_calibration,
            trigger=_Cron(hour=3, minute=10),
            id="auto_trader_calibration",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    except Exception as _e:
        logger.warning(f"calibration job not scheduled: {_e}")
    scheduler.start()
    _app_health["scheduler_started"] = True
    logger.info("Scheduler started — auto-scan 15m, auto-trader manage 60s")

    # Boot live quote stream with current watchlist
    db = SessionLocal()
    try:
        tickers = [s.ticker for s in db.query(WatchlistStock).all()]
    finally:
        db.close()
    try:
        await live_quotes.start(tickers)
        _app_health["live_quotes_started"] = True
    except Exception as e:
        # Don't crash the whole app — Yahoo polling still works without WS — but
        # record the error so /api/health can flag the silent-stream-failure.
        _app_health["live_quotes_error"] = str(e)
        logger.error(f"Could not start live quotes: {e}")

    yield
    scheduler.shutdown()
    try:
        await live_quotes.stop()
    except Exception:
        pass
    # Audit fix L1: cleanly shut the post-mortem worker pool so daemon
    # threads don't linger past app exit (and don't drop in-flight
    # post-mortems from the very last manage tick).
    try:
        from services.auto_trader import _post_mortem_pool
        _post_mortem_pool.shutdown(wait=True, cancel_futures=False)
    except Exception:
        pass


app = FastAPI(title="Stock Technical Analysis API", lifespan=lifespan)

# ----- Real-money safety banner (A3) --------------------------------------
# Flipping `ALPACA_LIVE=1` alone is one keystroke away from live trading; a
# stray env var in a deploy, a typo, or a copy-pasted .env moves real money.
# Require a second explicit consent var so live mode demands two deliberate
# config changes. Refuse to boot live without the API key (A1) in place too.
_ALPACA_LIVE = os.getenv("ALPACA_LIVE", "0").strip() == "1"
_LIVE_CONSENT = os.getenv("I_UNDERSTAND_LIVE_RISK", "").strip().lower() == "yes"
if _ALPACA_LIVE:
    if not _LIVE_CONSENT:
        raise RuntimeError(
            "ALPACA_LIVE=1 requires I_UNDERSTAND_LIVE_RISK=yes — "
            "refusing to start live trading without explicit consent"
        )
    if not auth_configured():
        raise RuntimeError(
            "ALPACA_LIVE=1 requires APP_API_KEY to be set — "
            "refuse to expose live trading endpoints without auth"
        )
    logger.critical(
        "========================================\n"
        "  LIVE TRADING MODE ENABLED\n"
        "  Real money is at risk on every order.\n"
        "========================================"
    )
else:
    logger.info("Paper trading mode (ALPACA_LIVE != '1')")

# Log auth state once at boot — a silent "no APP_API_KEY → open access" used to
# be easy to miss in a deploy checklist.
if auth_configured():
    logger.info("API auth ENABLED (APP_API_KEY is set)")
else:
    logger.warning("API auth DISABLED — APP_API_KEY is empty; do not use in production")

# CORS origins — comma-separated env override for prod/preview deployments,
# defaults to local Vite + CRA dev servers.
_CORS_DEFAULT = "http://localhost:5173,http://localhost:3000"
_cors_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", _CORS_DEFAULT).split(",") if o.strip()]

# Audit fix A2: keep allow_credentials=False — we authenticate via X-API-Key
# header (stateless), not via cookies/sessions. Credentialed CORS combined
# with unrestricted origins is how CSRF attacks drain accounts. Restrict
# methods and headers to what we actually need.
if _ALPACA_LIVE and not _cors_origins:
    raise RuntimeError(
        "ALPACA_LIVE=1 requires CORS_ALLOW_ORIGINS to be explicitly set — "
        "refusing to accept default localhost origins in live mode"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)
logger.info(f"CORS allowed origins: {_cors_origins}")

# Mount /metrics endpoint (no-op if prometheus_client isn't installed).
metrics.register_metrics_endpoint(app)

app.include_router(watchlist.router)
app.include_router(analysis.router)
app.include_router(backtest.router)
app.include_router(options.router)
app.include_router(stream.router)
app.include_router(trading.router)


@app.get("/api/health")
def health():
    """Health includes subsystem flags so deploys can detect partial-boot states.
    The HTTP status stays 200 (the app IS up); callers should inspect the
    `degraded` flag and the per-subsystem fields to decide alerting.

    Extended (G1): surfaces last-scan / last-manage timestamps, today's realized
    PnL, stream staleness, and the kill-switch state so one curl gives you the
    "is it trading correctly right now?" answer without poking 5 endpoints.
    """
    import time as _time
    from datetime import datetime, timedelta, timezone as _tz
    from sqlalchemy import func
    from database import AutoTrade

    stream_stale_secs = None
    try:
        latest_q_ts = max(
            (q.get("ts", 0) for q in live_quotes.all_stock_quotes().values()),
            default=0,
        )
        if latest_q_ts > 0:
            stream_stale_secs = round(_time.time() - latest_q_ts, 1)
    except Exception:
        pass

    # Today's realized PnL across all auto-trades (used by daily loss gate).
    realized_today = 0.0
    open_positions = 0
    try:
        db = SessionLocal()
        try:
            start_of_day = datetime.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            realized_today = float(
                db.query(func.coalesce(func.sum(AutoTrade.realized_pl), 0.0))
                .filter(AutoTrade.closed_at >= start_of_day)
                .scalar() or 0.0
            )
            open_positions = int(
                db.query(func.count(AutoTrade.id))
                .filter(AutoTrade.status == "open")
                .scalar() or 0
            )
        finally:
            db.close()
    except Exception:
        pass

    degraded = (
        not _app_health["scheduler_started"]
        or not _app_health["live_quotes_started"]
        or (stream_stale_secs is not None and stream_stale_secs > 120)
    )
    return {
        "status": "ok",
        "degraded": degraded,
        "scheduler_started": _app_health["scheduler_started"],
        "live_quotes_started": _app_health["live_quotes_started"],
        "live_quotes_error": _app_health["live_quotes_error"],
        "stream_stale_secs": stream_stale_secs,
        "last_scan_at": _app_health.get("last_scan_at"),
        "last_manage_at": _app_health.get("last_manage_at"),
        "realized_pnl_today": round(realized_today, 2),
        "open_positions": open_positions,
        "auth_configured": auth_configured(),
        "alpaca_live": _ALPACA_LIVE,
    }


@app.post("/api/admin/clear-cache", dependencies=[Depends(require_api_key)])
def admin_clear_cache(scope: str = "all"):
    """
    Wipe in-memory caches without restarting the server.
      scope=ohlcv     → just price/indicator cache in data_fetcher
      scope=backtest  → analysis router's per-ticker backtest cache
      scope=fallback  → auto_trader's 30s price-fallback cache
      scope=all       → all of the above
    Returns counts of cleared entries per cache.
    """
    out = {}
    if scope in ("all", "ohlcv"):
        from services import data_fetcher as _df
        n = len(_df._cache)
        _df._cache.clear()
        out["ohlcv"] = n
    if scope in ("all", "backtest"):
        from routers.analysis import _backtest_cache as _bc
        n = len(_bc)
        _bc.clear()
        out["backtest"] = n
    if scope in ("all", "fallback"):
        from services.auto_trader import _price_fallback_cache as _pfc
        n = len(_pfc)
        _pfc.clear()
        out["fallback"] = n
    return {"cleared": out, "scope": scope}


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
