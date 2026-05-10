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
        ["event"],  # opened, closed_target, closed_stop, closed_manual, etc.
    )
    AUTOTRADE_SKIPS = Counter(
        "autotrader_skips_total",
        "Signals rejected at a specific gate, by reason",
        ["reason"],  # bp_breaker, broker_down, daily_loss_halt, below_confidence, ...
    )
    DATA_FETCHES = Counter(
        "data_fetcher_calls_total",
        "Outbound OHLCV fetches by source + outcome",
        ["source", "outcome"],
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
    # r47 fix #T1-2: per-fill slippage histogram for live execution-quality
    # monitoring. Buckets cover sub-bp through ~3% slip.
    SLIPPAGE_BPS = Histogram(
        "autotrade_slippage_bps",
        "|filled - intent| / intent in basis points, per fill",
        ["asset_type"],
        buckets=(1, 3, 5, 10, 20, 40, 80, 160, 320),
    )


def inc(name: str, **labels) -> None:
    """Bump a counter by name. Silently no-ops when prometheus isn't installed."""
    if not _ENABLED:
        return
    counter = {
        "autotrade_event": AUTOTRADE_EVENTS,
        "autotrade_skip": AUTOTRADE_SKIPS,
        "data_fetch": DATA_FETCHES,
    }.get(name)
    if counter is None:
        return
    try:
        counter.labels(**labels).inc()
    except Exception as e:
        logger.debug(f"metrics inc({name}) failed: {e}")


def observe(name: str, value: float, **labels) -> None:
    """Record an observation on a histogram by name. Silent no-op when
    prometheus isn't installed or the name doesn't map. r47 #T1-2."""
    if not _ENABLED:
        return
    hist = {
        "autotrade_slippage_bps": SLIPPAGE_BPS,
        "manage_loop_seconds": MANAGE_LATENCY,
        "signal_generation_seconds": SIGNAL_LATENCY,
    }.get(name)
    if hist is None:
        return
    try:
        if labels:
            hist.labels(**labels).observe(float(value))
        else:
            hist.observe(float(value))
    except Exception as e:
        logger.debug(f"metrics observe({name}) failed: {e}")


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


def autotrade_skip_counts() -> dict:
    """r42 fix #1.25: snapshot of autotrade_skip counters by reason for the
    UI's "rejected signals" view. Returns {reason: count}; empty when
    prometheus_client isn't installed.
    """
    if not _ENABLED:
        return {}
    out: dict = {}
    try:
        for sample in AUTOTRADE_SKIPS.collect():
            for s in sample.samples:
                if s.name.endswith("_total"):
                    reason = s.labels.get("reason") or "unknown"
                    out[reason] = int(s.value)
    except Exception as e:
        logger.debug(f"autotrade_skip_counts failed: {e}")
    return out


def autotrade_event_counts() -> dict:
    """r42 fix #1.25 sibling: snapshot of autotrade_event counters."""
    if not _ENABLED:
        return {}
    out: dict = {}
    try:
        for sample in AUTOTRADE_EVENTS.collect():
            for s in sample.samples:
                if s.name.endswith("_total"):
                    event = s.labels.get("event") or "unknown"
                    out[event] = int(s.value)
    except Exception as e:
        logger.debug(f"autotrade_event_counts failed: {e}")
    return out


def register_metrics_endpoint(app) -> None:
    """Mount /metrics on a FastAPI app. No-op if prometheus_client missing."""
    if not _ENABLED:
        logger.info("prometheus_client not installed — /metrics endpoint disabled")
        return
    from fastapi import Depends, Response
    # r82: gate behind APP_API_KEY. Cloud Run uses --allow-unauthenticated;
    # without this guard /metrics leaked autotrade event volume + skip
    # reasons to anyone scraping the public URL — useful intel for an
    # attacker timing an account takeover for max damage.
    from routers._auth import require_api_key

    @app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_api_key)])
    def metrics_endpoint():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
