"""FastAPI app entrypoint + dual-service runtime composition.

The same Python image runs in two distinct Cloud Run services
differentiated by the `RUN_MODE` env var:

  * `RUN_MODE=api` (default): HTTP traffic, scanner schedules, signal
    generation, alt-data refresh jobs. Min 1 / max 3 instances.
    Frontend SPA is served from `/`. Scheduler runs scan + alt-data jobs.

  * `RUN_MODE=manager`: internal-ingress only. Runs the 20-second
    `manage_open_positions` loop + 60-minute broker reconciliation +
    boot-time reconciliation. Min/max=1 instance — doubling would
    dual-fire the manage loop.

Both services share the same Cloud SQL Postgres database; coordination
is via `auto_trades` rows (api inserts pending/open; manager updates).
Process-local state (BP reservations, circuit breakers, in-memory
caches) is per-service by design.

Module-level surface (in dependency order):
  * `.env` loader (no python-dotenv dependency)
  * Logging setup (rotating file + structured JSON to stdout)
  * `lifespan` async context manager — registers all scheduler jobs
    based on `RUN_MODE` and starts the live-quotes WebSocket
  * Global `app = FastAPI(...)` with router includes + CORS + rate
    limiter middleware
  * `/api/health` handler (subsystem heartbeat for liveness probes)

Critical invariants:
  * `_load_dotenv()` must run BEFORE any service import — service
    modules read `os.getenv` at import time.
  * `RUN_MODE=manager` returns early in lifespan after registering its
    minimal job set; api-mode falls through to register everything else.
  * Cloud Run liveness probe targets `/api/health`; manager's probe
    trips when `last_manage_at` exceeds 120s during RTH.

NOT in this module:
  * Service-level logic (lives in `services/*`)
  * Router endpoint implementations (`routers/*`)
  * Database setup (`database.py`)
"""
import logging
import os
from typing import Optional, Dict
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

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.background import BackgroundScheduler
from database import create_tables, SessionLocal, WatchlistStock, AutoTraderConfig
from routers import watchlist, analysis, backtest, options, stream, trading, news, alerts as alerts_router, chat as chat_router, analyst_ratings as analyst_ratings_router, macro as macro_router, ml as ml_router, fundamentals as fundamentals_router, social as social_router, ai_judge as ai_judge_router, admin as admin_router
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


# ---- Structured JSON formatter for stdout (Cloud Logging-friendly) ----
# Cloud Logging auto-parses JSON lines and exposes each field as a queryable
# attribute (severity, logger, message, etc). This makes "show me all
# autotrade events for AAPL in the last hour" a one-line filter instead of
# regex-grepping a flat string. Falls back to plain-text if json import fails
# (it shouldn't — stdlib).
class _JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for Cloud Logging consumption.

    Each line is one JSON object with `severity / message / logger /
    timestamp` plus any extra fields attached to the LogRecord. Cloud
    Logging auto-parses these and indexes them as queryable fields,
    making `severity=ERROR` filters and ad-hoc `jsonPayload.event=...`
    searches possible without grep-on-text.

    Toggled via `LOG_JSON=1` (default in Cloud Run); set `LOG_JSON=0`
    for local dev where plaintext is easier to read.
    """
    # Cloud Logging maps these keys to its severity column.
    _SEV_MAP = {
        "DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING",
        "ERROR": "ERROR", "CRITICAL": "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        payload = {
            "ts": _dt.fromtimestamp(record.created, tz=_tz.utc).isoformat(),
            "severity": self._SEV_MAP.get(record.levelname, "DEFAULT"),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Pick up any structured extras (logger.info("...", extra={"ticker": ...}))
        for k, v in record.__dict__.items():
            if k in payload or k.startswith("_"):
                continue
            if k in ("name", "msg", "args", "levelname", "levelno", "pathname",
                     "filename", "module", "exc_info", "exc_text", "stack_info",
                     "lineno", "funcName", "created", "msecs", "relativeCreated",
                     "thread", "threadName", "processName", "process", "message",
                     "taskName"):
                continue
            try:
                _json.dumps(v)  # serializability check
                payload[k] = v
            except Exception:
                payload[k] = repr(v)
        try:
            return _json.dumps(payload, default=str)
        except Exception:
            return super().format(record)


# JSON to stdout in production (Cloud Run picks up structured fields);
# plaintext locally so dev tail-f stays readable.
_use_json_logs = os.getenv("LOG_JSON", "1") == "1"
_stdout_formatter = _JsonFormatter() if _use_json_logs else _formatter

if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
           for h in _root_logger.handlers):
    _stream_h = logging.StreamHandler()
    _stream_h.setFormatter(_stdout_formatter)
    _root_logger.addHandler(_stream_h)

if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == LOG_FILE
           for h in _root_logger.handlers):
    _file_h = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
    # On-disk file stays plaintext for human inspection (tail / less / grep -i).
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
    """Per-message-template log dedup filter.

    Keyed on `(logger_name, level, msg_template)` so args-varying lines
    (e.g. ticker-substituted error strings) still dedupe. First
    occurrence passes through; subsequent identical messages are
    suppressed for `interval_sec`. Set on the noisy Alpaca SDK loggers
    in particular — without this a 5-minute API outage produces
    thousands of identical log lines and floods Cloud Logging quota.
    """
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

# r44 fix #0.14: explicit executors so long-running jobs (ml_trainer ~15min,
# universe_scan ~2min, fundamentals ~3min) can't saturate the default
# 10-thread pool and starve fast jobs (calibration, reconcile, news poll).
# `heavy` is a 2-thread pool reserved for ml_weekly_retrain + universe_scan;
# `default` keeps a healthy 16-thread pool for everything else. Jitter
# added to all cron triggers prevents the 12:00 / 14:30 / 16:00 jobs from
# all firing in lockstep.
from apscheduler.executors.pool import ThreadPoolExecutor as _APSchedThreadPool
scheduler = BackgroundScheduler(
    executors={
        "default": _APSchedThreadPool(max_workers=16),
        "heavy": _APSchedThreadPool(max_workers=2),
    },
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 60,
    },
)

# r47 fix #T1-1 / observability P0-5: surface scheduler-level job failures
# and misfires as alerts. Without this, a deploy bug (typo, import error)
# can silently drop a critical job for hours; jobs that fail before their
# inner try/except just disappear.
try:
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
    _job_failure_counts: Dict[str, int] = {}

    def _job_error_listener(event):
        try:
            from services.alerts import alert as _raise_a
        except Exception:
            return
        jid = getattr(event, "job_id", "?") or "?"
        n = _job_failure_counts.get(jid, 0) + 1
        _job_failure_counts[jid] = n
        try:
            if n in (1, 3, 10):  # alert ladder
                exc = getattr(event, "exception", None)
                _raise_a(
                    "error" if n == 1 else "critical",
                    "scheduler_job_failed",
                    f"job '{jid}' failed (#{n}): {type(exc).__name__ if exc else 'unknown'}: {exc!r}"[:500],
                )
        except Exception:
            pass

    def _job_missed_listener(event):
        try:
            from services.alerts import alert as _raise_a
        except Exception:
            return
        try:
            _raise_a(
                "warning", "scheduler_misfire",
                f"job '{getattr(event, 'job_id', '?')}' misfired past grace_time",
            )
        except Exception:
            pass

    scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)
    scheduler.add_listener(_job_missed_listener, EVENT_JOB_MISSED)
except Exception as _le:
    logger.warning(f"scheduler listener install failed: {_le}")

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
                pool = get_candidate_tickers()   # already sorted by score desc
                # r46 Tier 1: union with EV-bias ordering. Prior code put
                # watchlist FIRST and then appended pool — meaning if budget
                # ran out mid-scan, watchlist names always processed but
                # high-conviction pool tickers were skipped. Now: interleave
                # so the top 50% of slots alternates pool↔watchlist by
                # score-rank, ensuring high-EV pool names see the same
                # priority as watchlist.
                seen = set(tickers)
                # Take pool order as authoritative (already EV-sorted).
                interleaved = []
                w_iter = iter(tickers)
                for p_t in pool:
                    if p_t not in seen:
                        interleaved.append(p_t); seen.add(p_t)
                    try:
                        w_t = next(w_iter)
                        if w_t not in {x for x in interleaved}:
                            interleaved.append(w_t)
                    except StopIteration:
                        pass
                # Drain remaining watchlist
                for w_t in w_iter:
                    if w_t not in {x for x in interleaved}:
                        interleaved.append(w_t)
                tickers = interleaved
                logger.info(f"Scan universe (EV-interleaved): {len(tickers)} tickers (watchlist + {len(pool)} candidates)")
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


def _ml_outcome_backfill():
    """For each MLPrediction without an outcome, look up the most recent
    closed AutoTrade for the same (ticker, signal_type, ~created_at window)
    and copy realized_pl + outcome. Drives the /api/ml/calibration endpoint."""
    from datetime import datetime as _dt, timedelta as _td
    from database import SessionLocal, MLPrediction, AutoTrade
    db = SessionLocal()
    try:
        rows = (
            db.query(MLPrediction)
            .filter(MLPrediction.outcome.is_(None))
            .filter(MLPrediction.created_at >= _dt.utcnow() - _td(days=30))
            .all()
        )
        # r47 fix #T0d-3: window widened from ±10min to ±24h to match
        # auto_trader._backfill_ml_outcome (r46 widened that path to ±24h).
        # Prior mismatch: a slow-fill trade or any trade whose close path
        # crashed during outcome backfill was permanently NULL because
        # the scheduler-driven backfill had a much narrower window.
        n = 0
        for p in rows:
            window_start = p.created_at - _td(hours=24)
            window_end = p.created_at + _td(hours=24)
            # Prefer trade_id-tracked rows (deterministic match) when set.
            t = None
            if getattr(p, "trade_id", None):
                t = db.query(AutoTrade).filter(AutoTrade.id == p.trade_id).first()
            if not t:
                t = (
                    db.query(AutoTrade)
                    .filter(AutoTrade.ticker == p.ticker,
                            AutoTrade.opened_at >= window_start,
                            AutoTrade.opened_at <= window_end,
                            AutoTrade.status.like("closed%"))
                    .order_by(AutoTrade.closed_at.desc())
                    .first()
                )
            if t and t.realized_pl is not None:
                p.trade_id = t.id
                p.realized_pl = t.realized_pl
                p.outcome = 1 if t.realized_pl > 0 else 0
                p.closed_at = t.closed_at
                n += 1
        if n:
            db.commit()
            logger.info(f"ml_outcome_backfill: backfilled {n} predictions")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()

    # Dual-service architecture (RUN_MODE):
    #   "api"     — DEFAULT. Registers everything EXCEPT the manage loop +
    #               reconciliation. Handles HTTP, scanner, signal generation,
    #               entries, all alt-data refresh jobs.
    #   "manager" — Registers ONLY the 20s manage loop + hourly broker
    #               reconciliation. Runs as a separate Cloud Run service
    #               (stockrecs-manager) so a crash in the api service can't
    #               leave open positions unmanaged.
    # Both services share the same Cloud SQL database. The api service
    # writes new AutoTrade rows; the manager service reads + updates them.
    _run_mode = (os.getenv("RUN_MODE") or "api").strip().lower()
    if _run_mode not in ("api", "manager"):
        logger.warning(f"Unknown RUN_MODE={_run_mode!r}, defaulting to 'api'")
        _run_mode = "api"
    logger.info(f"RUN_MODE={_run_mode}")

    if _run_mode == "manager":
        # Manager-only schedule: 20s manage + hourly reconciliation. Boot-time
        # reconciliation also runs once so a fresh container picks up any
        # state drift from a prior incarnation.
        scheduler.add_job(
            _scheduled_manage, "interval", seconds=20, id="auto_trader_manage",
            max_instances=1, coalesce=True, misfire_grace_time=10,
        )
        # r41-promote-auto: dispatch via auto_reconcile_positions which
        # consults `cfg.auto_promote_adopted`. When False (default), runs
        # detect_unexpected_positions (alerts only — current behavior).
        # When True, runs sync + promote in one shot so external positions
        # are automatically managed by the bot.
        scheduler.add_job(
            auto_trader.auto_reconcile_positions, "interval", minutes=60,
            id="positions_reconcile",
            max_instances=1, coalesce=True, misfire_grace_time=120,
        )
        try:
            auto_trader.auto_reconcile_positions()
        except Exception as _e:
            logger.warning(f"boot reconciliation failed: {_e}")
        scheduler.start()
        _app_health["scheduler_started"] = True
        logger.info("Manager service started — manage every 20s, reconcile every 60min")
        yield
        try:
            # r44 fix #0.15: wait=True drains in-flight jobs before exiting.
            # Without it, Cloud Run SIGTERM (10s grace) can kill broker
            # submissions mid-flight, leaving DB rows pending while the
            # actual order filled at Alpaca.
            scheduler.shutdown(wait=True)
        except Exception:
            pass
        return

    # ---- api mode below: original lifespan logic unchanged ----------
    # Audit fix D3: explicit max_instances=1 + coalesce so a slow scan doesn't
    # stack a second one. Also ties both jobs to a 60s misfire grace window.
    # Scan cadence 15m → 5m: 3× more entry opportunities per session. The
    # signal_generator short-circuits on cached data when inputs haven't
    # changed, so cost scales sub-linearly. Freshness fix (Critical #8)
    # caps signal age at 90m, so 5m scans align with the new gate.
    scheduler.add_job(
        scheduled_scan, "interval", minutes=5, id="watchlist_scan",
        max_instances=1, coalesce=True, misfire_grace_time=60,
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
    # Analyst ratings refresh, 4×/day. Aligned with universe scanner slots so
    # freshly-ranked candidates get ratings before the next scan cycle reads
    # them. Ratings move slowly — more-frequent polling adds no signal.
    try:
        from services import analyst_ratings as _ar
        from apscheduler.triggers.cron import CronTrigger as _Cron
        for hh, mm in [(11, 45), (14, 15), (16, 45), (19, 15)]:
            scheduler.add_job(
                _ar.refresh_all,
                trigger=_Cron(hour=hh, minute=mm),
                id=f"analyst_ratings_{hh:02d}{mm:02d}",
                max_instances=1, coalesce=True, misfire_grace_time=900,
            )
    except Exception as _e:
        logger.warning(f"analyst_ratings job not scheduled: {_e}")
    # Macro calendar — populate daily at 05:00 UTC (pre-US open) so the
    # window of upcoming events is fresh before the trading day. Hourly job
    # tries to backfill `actual` values from FRED for releases that just
    # happened (no-op if FRED_API_KEY is unset; calendar+blackout gate works
    # without it).
    try:
        from services import macro_calendar as _mc
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            lambda: _mc.populate_calendar(60),
            trigger=_Cron(hour=5, minute=0),
            id="macro_calendar_populate",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
        scheduler.add_job(
            lambda: _mc.fetch_actuals_for_recent_releases(24),
            "interval", minutes=15, id="macro_actuals_fetch",
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )
    except Exception as _e:
        logger.warning(f"macro_calendar job not scheduled: {_e}")
    # Fundamentals refresh — weekly Sunday 04:30 UTC. Lands BEFORE the
    # best_strategy job at 04:00 UTC (no, that's 04:00 — order is
    # best_strategy@04:00 → fundamentals@04:30 → ml@06:00). Most fundamental
    # fields only update quarterly with earnings, so weekly is plenty.
    # Hash-based change detection means unchanged tickers don't churn the DB.
    try:
        from services import fundamentals as _fnd
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            _fnd.refresh_all,
            trigger=_Cron(day_of_week="sun", hour=4, minute=30),
            id="fundamentals_weekly",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    except Exception as _e:
        logger.warning(f"fundamentals job not scheduled: {_e}")
    # Social sentiment (Stocktwits) — 4×/day. Rate-limited public API so
    # we stay conservative (~65 tickers × 2 workers = ~1-2 min per cycle).
    try:
        from services import social_sentiment as _ss
        from apscheduler.triggers.cron import CronTrigger as _Cron
        for hh, mm in [(12, 0), (15, 0), (18, 0), (21, 0)]:
            scheduler.add_job(
                _ss.refresh_all,
                trigger=_Cron(hour=hh, minute=mm),
                id=f"social_sentiment_{hh:02d}{mm:02d}",
                max_instances=1, coalesce=True, misfire_grace_time=900,
            )
    except Exception as _e:
        logger.warning(f"social_sentiment job not scheduled: {_e}")
    # SEC Form 4 — weekly Sunday 04:45 UTC (sits between fundamentals@04:30
    # and best_strategy@04:00). SEC rate-limits to 10 req/s, so we run
    # serially with a tiny pacing delay — ~3-5 min for 65 tickers.
    try:
        from services import insider_trades as _ins
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            _ins.refresh_all,
            trigger=_Cron(day_of_week="sun", hour=4, minute=45),
            id="insider_weekly",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    except Exception as _e:
        logger.warning(f"insider_trades job not scheduled: {_e}")
    # r/wallstreetbets scraper — every 30 min. Reddit rate-limits unauth at
    # ~60 req/min; we stay well under with 2 pages per run.
    try:
        from services import wsb_scraper as _wsb
        scheduler.add_job(
            _wsb.refresh_once,
            "interval", minutes=30, id="wsb_scraper",
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )
    except Exception as _e:
        logger.warning(f"wsb_scraper job not scheduled: {_e}")
    # Daily health check: low-signal-volume alert (r38). Fires at 22:00 UTC
    # (~5pm ET, after the close + scan settle). Compares today's signal
    # count against the trailing 7-day avg; alerts when scanner appears
    # degraded (today < 30% of baseline). Self-deduped via alerts.alert.
    try:
        from services.risk_manager import check_low_signal_volume
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            check_low_signal_volume,
            trigger=_Cron(hour=22, minute=0),
            id="health_low_signal_volume",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    except Exception as _e:
        logger.warning(f"low_signal_volume job not scheduled: {_e}")

    # r46 fix #0.2: equity snapshot recorder. 5-min during RTH (13:30-20:00 UTC,
    # weekdays) so the persisted equity timeseries actually exists for
    # account_drawdown_multiplier to read.
    try:
        from services.risk_manager import record_equity_snapshot
        from apscheduler.triggers.cron import CronTrigger as _CronES
        # r47 fix #T0d-4: prior cron used UTC `hour="13-20"` (= 9-16 ET in
        # EDT, but 8-15 ET in EST). For ~4 months/year (Nov-Mar) the recorder
        # stopped firing at 15:00 ET — missing the 15:00-16:00 close window
        # entirely. account_drawdown_multiplier read truncated equity series.
        # Pin the cron to America/New_York so it tracks the trading session
        # regardless of DST.
        scheduler.add_job(
            record_equity_snapshot,
            trigger=_CronES(day_of_week="mon-fri", hour="9-16", minute="*/5",
                            timezone="America/New_York"),
            id="equity_snapshot",
            max_instances=1, coalesce=True, misfire_grace_time=120,
        )
    except Exception as _e:
        logger.warning(f"equity_snapshot job not scheduled: {_e}")

    # Institutional holdings (13F proxy via yfinance) — weekly Sunday 05:15
    # UTC. 13F cadence is quarterly-with-lag so weekly is plenty.
    try:
        from services import institutional as _inst
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            _inst.refresh_all,
            trigger=_Cron(day_of_week="sun", hour=5, minute=15),
            id="institutional_weekly",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
    except Exception as _e:
        logger.warning(f"institutional job not scheduled: {_e}")
    # ML: weekly retrain on Sunday 06:00 UTC. Heavy job (5-15 min depending
    # on universe size). Initial training has to be triggered manually via
    # POST /api/ml/train after first deploy.
    try:
        from services import ml_trainer as _mt
        from apscheduler.triggers.cron import CronTrigger as _Cron
        scheduler.add_job(
            lambda: _mt.train(),
            trigger=_Cron(day_of_week="sun", hour=6, minute=0),
            id="ml_weekly_retrain",
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
        # Hourly outcome backfill: for closed AutoTrades that have an MLPrediction
        # row, copy the realized outcome onto the prediction so the calibration
        # endpoint can plot predicted-vs-actual.
        scheduler.add_job(
            _ml_outcome_backfill,
            "interval", minutes=30, id="ml_outcome_backfill",
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )
    except Exception as _e:
        logger.warning(f"ml jobs not scheduled: {_e}")
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
    # r44 fix #0.15: wait=True for clean shutdown (see manager-mode comment).
    try:
        scheduler.shutdown(wait=True)
    except Exception:
        pass
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

# r48 BACKLOG #perf-P3.24: gzip large responses (equity-curve, news/recent,
# /api/ml/calibration). 30d × 78 5min snapshots ≈ 500KB JSON / poll otherwise.
try:
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)
except Exception as _gz_e:
    logger.warning(f"GZipMiddleware not installed: {_gz_e}")


# r48 BACKLOG #observability-P0-6: frontend error reporter endpoint.
# Without this any runtime exception in the UI is silent — operator
# sees a blank dashboard and assumes the bot is dead.
@app.post("/api/log/frontend-error")
def _log_frontend_error(payload: dict):
    """Accept a minimal error payload from the dashboard and emit it as
    a `frontend_error` alert. Throttled by the alerts.py 5min dedup."""
    try:
        msg = (payload.get("msg") or "")[:500]
        url = (payload.get("url") or "")[:200]
        if msg:
            from services.alerts import alert as _ra
            _ra("warning", "frontend_error", f"{msg} (url={url})")
    except Exception:
        pass
    return {"ok": True}

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
    # r44 fix #0.4: refuse to boot live on default sqlite. With min_instances=1
    # max_instances=3 each Cloud Run instance has its own SQLite file →
    # three bots silently disagreeing about positions/signals.
    _db_url = os.getenv("DATABASE_URL", "")
    if not _db_url or _db_url.startswith("sqlite"):
        raise RuntimeError(
            "ALPACA_LIVE=1 requires a non-SQLite DATABASE_URL (Postgres/Cloud SQL/Neon). "
            "Default sqlite:///./stockapp.db is per-instance and gives each Cloud Run "
            "instance its own database — refuse to start."
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


# Rate-limit middleware (r38) — token bucket per X-API-Key (or client IP
# when unauth'd). Disabled when APP_RATE_LIMIT_PER_MIN=0. Applies to /api/*
# only; /metrics and /ws/* skip the bucket.
@app.middleware("http")
async def _rate_limit_middleware(request, call_next):
    """ASGI middleware applying the per-X-API-Key token-bucket limiter
    on every `/api/*` request. Defers to `routers._auth.rate_limit`
    for the actual bucket logic; this middleware only handles request
    routing + 429 response synthesis.

    Skips `/metrics`, `/ws/*`, and the SPA static routes so prometheus
    scrapes and live quote streams don't share buckets with HTTP API
    callers. Disabled entirely when `APP_RATE_LIMIT_PER_MIN=0`.
    """
    path = request.url.path or ""
    if path.startswith("/api/"):
        try:
            from routers._auth import rate_limit as _rl
            x_key = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
            _rl(request, x_api_key=x_key)
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers or {},
            )
    return await call_next(request)

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
app.include_router(chat_router.router)
app.include_router(analyst_ratings_router.router)
app.include_router(macro_router.router)
app.include_router(ml_router.router)
app.include_router(fundamentals_router.router)
app.include_router(social_router.router)
app.include_router(ai_judge_router.router)
app.include_router(admin_router.router)


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

    # Stale-stream alert: fire once when quotes stop flowing for >30s during RTH.
    # Operator needs to know BEFORE positions go unmanaged.
    try:
        if stream_stale_secs is not None and stream_stale_secs > 30:
            from services import alerts as _alerts
            from services import paper_trader as _pt_clk
            if _pt_clk.is_market_open():
                _alerts.alert(
                    severity="warning",
                    category="stream_stale",
                    message=f"Alpaca WS quotes stale {stream_stale_secs:.0f}s during RTH — positions may not be priced live",
                )
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

    # Manage-loop staleness — only relevant on the manager service (api
    # service doesn't run the manage loop, so its last_manage_at is None
    # and staleness is meaningless).
    manage_stale_secs = None
    _is_manager_proc = (os.getenv("RUN_MODE") or "api").strip().lower() == "manager"
    try:
        last_m = _app_health.get("last_manage_at")
        if last_m:
            from datetime import datetime as _dt_h, timezone as _tz_h
            last_dt = _dt_h.fromisoformat(last_m.replace("Z", "+00:00")) if isinstance(last_m, str) else last_m
            manage_stale_secs = (_dt_h.now(_tz_h.utc) - last_dt).total_seconds()
    except Exception:
        pass

    # If manager hasn't ticked in >120s, alert. Manage cadence is 20s so
    # 120s = 6 missed ticks — clearly something's wrong.
    if _is_manager_proc and manage_stale_secs is not None and manage_stale_secs > 120:
        try:
            from services import alerts as _alerts2
            _alerts2.alert(
                severity="error",
                category="manage_loop_stuck",
                message=f"Manage loop hasn't ticked in {manage_stale_secs:.0f}s — positions may not be tracked",
            )
        except Exception:
            pass

    degraded = (
        not _app_health["scheduler_started"]
        or not _app_health["live_quotes_started"]
        or (stream_stale_secs is not None and stream_stale_secs > 120)
        or (_is_manager_proc and manage_stale_secs is not None and manage_stale_secs > 120)
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
        # r46 Tier 1: surface crisis state + key risk-overlay diagnostics.
        "crisis_mode": _crisis_mode_flag(),
        "session_dd_pct": _session_dd_pct(),
        "account_dd_mult": _acct_dd_mult(),
        # r48 BACKLOG #observability-P1-15: AI cost tracker
        "ai_cost_today": _ai_cost_today(),
        # r48 BACKLOG #observability-P1-17: MLPrediction backlog
        "mlpred_backlog": _mlpred_backlog(),
        # r48 BACKLOG #observability-P1-18: cache freshness
        "options_chain_oldest_age_sec": None,
        "earnings_calendar_oldest_age_sec": None,
        # r48 BACKLOG #observability-P3-27: DB pool checkout
        "db_pool_checkedout": _db_pool_checkedout(),
        # r48 BACKLOG #failure-mode P1-7 / lifecycle P1-13: surface breakers
        "pdt_locked": _pdt_locked(),
        "db_down": _db_down_flag(),
    }


def _ai_cost_today() -> dict:
    try:
        from services.ai_judge import ai_cost_today_usd
        return ai_cost_today_usd()
    except Exception:
        return {}


def _mlpred_backlog() -> int:
    try:
        from database import SessionLocal as _SL_b, MLPrediction as _MP_b
        from datetime import datetime as _dt_b, timedelta as _td_b
        db = _SL_b()
        try:
            cutoff = _dt_b.utcnow() - _td_b(days=2)
            return int(db.query(_MP_b).filter(
                _MP_b.outcome.is_(None),
                _MP_b.created_at < cutoff,
            ).count())
        finally:
            db.close()
    except Exception:
        return 0


def _db_pool_checkedout() -> int:
    try:
        from database import engine as _eng
        return int(_eng.pool.checkedout())
    except Exception:
        return 0


def _pdt_locked() -> bool:
    try:
        from services.risk_manager import is_pdt_locked
        return bool(is_pdt_locked())
    except Exception:
        return False


def _db_down_flag() -> bool:
    try:
        from services.risk_manager import is_db_down
        return bool(is_db_down())
    except Exception:
        return False


def _crisis_mode_flag() -> bool:
    try:
        from services.risk_manager import in_crisis_mode
        return bool(in_crisis_mode())
    except Exception:
        return False


def _session_dd_pct() -> Optional[float]:
    try:
        from services.risk_manager import session_equity_drawdown_pct
        v = session_equity_drawdown_pct()
        return round(v * 100, 2) if v is not None else None
    except Exception:
        return None


def _acct_dd_mult() -> Optional[float]:
    try:
        from services.risk_manager import account_drawdown_multiplier
        return account_drawdown_multiplier()
    except Exception:
        return None


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
