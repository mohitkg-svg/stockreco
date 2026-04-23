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
from database import create_tables, SessionLocal, WatchlistStock, AutoTraderConfig
from routers import watchlist, analysis, backtest, options, stream, trading, news, alerts as alerts_router
from routers.analysis import _run_analysis_for_ticker
from routers._auth import require_api_key, auth_configured
from services import live_quotes, auto_trader, metrics
from services import news as news_svc

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
    """Scan tickers (watchlist + optional universe-scanner candidates) in
    parallel and update signals in DB."""
    from datetime import datetime as _dt, timezone as _tz
    from concurrent.futures import ThreadPoolExecutor
    db = SessionLocal()
    try:
        tickers = [s.ticker for s in db.query(WatchlistStock).all()]
        # Ground-up Tier 1: universe scanner candidates.
        cfg = db.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
        if cfg and getattr(cfg, "use_universe_scanner", False):
            try:
                from services.universe_scanner import get_candidate_tickers
                pool = get_candidate_tickers()
                # Union (watchlist first for UI-priority, then candidates).
                seen = set(tickers)
                for t in pool:
                    if t not in seen:
                        tickers.append(t)
                        seen.add(t)
                logger.info(f"Scan universe: watchlist={len(tickers) - len(pool) + len(seen - set(pool))}, candidates={len(pool)}")
            except Exception as e:
                logger.warning(f"universe_scanner read failed: {e}")
    finally:
        db.close()

    def _scan_one(ticker: str):
        logger.info(f"Auto-scanning {ticker}")
        _local = SessionLocal()
        try:
            _run_analysis_for_ticker(ticker, _local)
        except Exception as e:
            logger.error(f"Scan error for {ticker}: {e}")
        finally:
            _local.close()

    # Cap at 4 to stay inside the Yahoo token-bucket (30 req/min) — each
    # ticker fans out to 7 timeframes + meta; 4 parallel scanners ≈ 28 rps.
    max_workers = min(4, max(1, len(tickers)))
    if tickers:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scan") as pool:
            list(pool.map(_scan_one, tickers))
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
    # Audit fix #2: manage loop cadence 60s → 20s. Previously any broker-
    # side SL drop could leave a position naked for up to 60s before the
    # next tick detected it. 20s caps the exposure window at ~1/3 of that
    # while still respecting Alpaca's REST rate limit on status-poll calls.
    scheduler.add_job(
        _scheduled_manage, "interval", seconds=20, id="auto_trader_manage",
        max_instances=1, coalesce=True, misfire_grace_time=10,
    )
    # Audit fix #9: hourly detection of unexpected stock positions
    # (option-assignment surprises, manual trades outside the bot, etc.).
    # Alerts the operator if Alpaca reports a position we don't track.
    scheduler.add_job(
        auto_trader.detect_unexpected_positions, "interval", minutes=60,
        id="unexpected_positions_audit",
        max_instances=1, coalesce=True, misfire_grace_time=120,
    )
    # News ingestion — alpaca-py 0.21.1 doesn't yet export NewsDataStream,
    # so the 2-min REST poll remains our news ingestion path. The
    # live_quotes._news_worker is already coded to auto-activate once the
    # SDK adds the class (falls back to no-op silently on import error).
    # Cadence stays at 2m — fast enough for event-driven reactions.
    scheduler.add_job(
        news_svc.poll_watchlist, "interval", minutes=2, id="news_poll",
        max_instances=1, coalesce=True, misfire_grace_time=60,
    )
    # Ground-up Tier 1: universe scanner every 15 min.
    # Scans ~500 liquid US equities, scores by RVOL/ADX/RS/52w-high proximity,
    # keeps top 30 in candidate_pool. Auto-trader reads from this when
    # cfg.use_universe_scanner=True. No-op when off.
    # Ground-up Tier 1: universe scanner. Runs 4× per day at market-relevant
    # UTC slots (~pre-open, ~10am ET, midday, ~3pm ET during EDT):
    #   12:00 UTC  — 08:00 ET pre-market warm-up (catches overnight movers)
    #   14:30 UTC  — 10:30 ET mid-morning (first hour of RTH has played out)
    #   17:00 UTC  — 13:00 ET midday lull (fresh afternoon setups)
    #   19:30 UTC  — 15:30 ET final hour (captures closing-auction flow)
    try:
        from services import universe_scanner as _usnv
        from apscheduler.triggers.cron import CronTrigger as _Cron
        for hh, mm in [(12, 0), (14, 30), (17, 0), (19, 30)]:
            scheduler.add_job(
                _usnv.run_scan,
                trigger=_Cron(hour=hh, minute=mm),
                id=f"universe_scan_{hh:02d}{mm:02d}",
                max_instances=1, coalesce=True, misfire_grace_time=900,
            )
    except Exception as _e:
        logger.warning(f"universe_scanner job not scheduled: {_e}")

    # Ground-up Tier 2: weekly best-strategy-per-ticker recompute.
    # Walk-forward backtest across every tracked ticker; persist the winning
    # (strategy, direction) per ticker into best_strategy_per_ticker. Signal
    # generator preferentially emits signals from these winners. Runs Sunday
    # 04:00 UTC so it lands before Monday's open.
    try:
        from services import best_strategy as _bs
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            _bs.recompute_all,
            trigger=_Cron(day_of_week="sun", hour=4, minute=0),
            id="best_strategy_weekly",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    except Exception as _e:
        logger.warning(f"best_strategy job not scheduled: {_e}")
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

    # Re-subscribe option stream to any OCC symbols of trades open at boot.
    try:
        from database import AutoTrade as _AT
        _db2 = SessionLocal()
        try:
            occ_syms = [r.symbol for r in _db2.query(_AT).filter(
                _AT.asset_type == "option",
                _AT.status.in_(["pending", "open"]),
            ).all() if r.symbol]
        finally:
            _db2.close()
        if occ_syms:
            live_quotes.ensure_option_symbols(occ_syms)
    except Exception as e:
        logger.warning(f"option-stream boot resubscribe skipped: {e}")

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
    # Audit fix #3: verify broker connectivity at boot. A silent None
    # return from _get_client() (e.g., creds mounted but malformed) would
    # otherwise let the app boot healthy and silently fail every order.
    from services import paper_trader as _pt_boot
    _boot_client = _pt_boot._get_client()
    if _boot_client is None:
        raise RuntimeError(
            "ALPACA_LIVE=1 but Alpaca TradingClient could not be initialized — "
            "check APCA_API_KEY_ID / APCA_API_SECRET_KEY are set and valid"
        )
    try:
        _boot_acct = _pt_boot.get_account()
        if not _boot_acct:
            raise RuntimeError("Alpaca /account probe returned empty — creds may be wrong")
        logger.critical(
            f"Boot: Alpaca LIVE account verified · equity=${float(_boot_acct['equity']):.0f} "
            f"· cash=${float(_boot_acct['cash']):.0f} · pdt={_boot_acct.get('pattern_day_trader')}"
        )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Alpaca /account probe failed at boot: {e} — refusing to start live")
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
app.include_router(news.router)
app.include_router(alerts_router.router)


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

    # Audit fix #11: surface critical operational state so the frontend
    # bell icon / external monitors can detect silent-failure modes.
    bp_breaker = False
    broker_down_flag = False
    sl_failures_1h = 0
    killed_flag = False
    killed_reason = None
    alerts_unacked = 0
    try:
        from services import auto_trader as _at_h
        bp_breaker = _at_h.bp_breaker_active()
        broker_down_flag = _at_h.broker_down()
        sl_failures_1h = _at_h.sl_resubmit_failures_1h()
    except Exception:
        pass
    try:
        _dbk = SessionLocal()
        try:
            _cfg = _dbk.query(AutoTraderConfig).filter(AutoTraderConfig.id == 1).first()
            if _cfg:
                killed_flag = bool(_cfg.killed)
                killed_reason = _cfg.killed_reason
        finally:
            _dbk.close()
    except Exception:
        pass
    try:
        from services import alerts as _alerts_h
        alerts_unacked = _alerts_h.count_unacked(since_hours=24)
    except Exception:
        pass

    degraded = (
        not _app_health["scheduler_started"]
        or not _app_health["live_quotes_started"]
        or (stream_stale_secs is not None and stream_stale_secs > 120)
        or bp_breaker
        or broker_down_flag
        or killed_flag
        or sl_failures_1h > 0
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
        # Audit fix #11 — new critical-state fields
        "bp_breaker_active": bp_breaker,
        "broker_down": broker_down_flag,
        "sl_resubmit_failures_1h": sl_failures_1h,
        "killed": killed_flag,
        "killed_reason": killed_reason,
        "alerts_unacked": alerts_unacked,
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
