"""
Lightweight metrics layer.

Wraps `prometheus_client` if it's installed; otherwise becomes a no-op so
the app still runs in environments where metrics aren't wanted. The point
is to give us VISIBILITY into:

  • manage-loop latency           (how slow is the 60s scheduler?)
  • signal-generation latency     (per-ticker scan cost)
  • Yahoo / Alpaca call counts    (rate-limit budget burn)
  • auto-trade events             (opens, closes by reason)

Mount the /metrics endpoint by importing `register_metrics_endpoint(app)`
from main.py.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _ENABLED = True
except ImportError:  # pragma: no cover — metrics are optional
    _ENABLED = False
    Counter = Histogram = None  # type: ignore
    generate_latest = lambda: b""  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain"


# ---- Stable counter / histogram instances ---------------------------------
if _ENABLED:
    AUTOTRADE_EVENTS = Counter(
        "autotrader_events_total",
        "Auto-trader lifecycle events",
        ["event"],  # opened, closed_target, closed_stop, closed_manual, dry_run, skip_*
    )
    DATA_FETCHES = Counter(
        "data_fetcher_calls_total",
        "Outbound OHLCV fetches by source + outcome",
        ["source", "outcome"],  # source=yahoo|alpaca, outcome=ok|empty|error
    )
    SIGNAL_LATENCY = Histogram(
        "signal_generation_seconds",
        "Wall time per signal_generator.generate_signal call",
        ["timeframe"],
    )
    MANAGE_LATENCY = Histogram(
        "manage_loop_seconds",
        "Wall time per manage_open_positions tick",
    )


def inc(name: str, **labels) -> None:
    """Bump a counter by name. Silently no-ops when prometheus isn't installed."""
    if not _ENABLED:
        return
    counter = {
        "autotrade_event": AUTOTRADE_EVENTS,
        "data_fetch": DATA_FETCHES,
    }.get(name)
    if counter is None:
        return
    try:
        counter.labels(**labels).inc()
    except Exception as e:
        logger.debug(f"metrics inc({name}) failed: {e}")


@contextmanager
def timer(name: str, **labels):
    """`with timer('signal', timeframe='1d'): ...` — records to a histogram."""
    if not _ENABLED:
        yield
        return
    hist = {
        "signal": SIGNAL_LATENCY,
        "manage": MANAGE_LATENCY,
    }.get(name)
    if hist is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        try:
            obs = time.perf_counter() - t0
            if labels:
                hist.labels(**labels).observe(obs)
            else:
                hist.observe(obs)
        except Exception as e:
            logger.debug(f"metrics timer({name}) failed: {e}")


def register_metrics_endpoint(app) -> None:
    """Mount /metrics on a FastAPI app. No-op if prometheus_client missing."""
    if not _ENABLED:
        logger.info("prometheus_client not installed — /metrics endpoint disabled")
        return
    from fastapi import Response

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
