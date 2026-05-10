"""r82 (B33): cross-instance AI/chat call budget via DB-backed atomic counter.

Replaces the per-process in-memory dicts in `ai_judge.py` and
`routers/chat.py`. With Cloud Run min=1, max=2 instances + cold-restarts,
the in-memory counters could be exceeded by Nx (each instance had its
own dict; cold-start zeroed it). This module uses
``INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count``
for an atomic single-statement increment shared across instances.

Both Postgres and modern SQLite (3.35+, included with Python 3.12)
support the RETURNING clause and ON CONFLICT DO UPDATE.

Public API:
    bump_and_check(channel, cap) -> (ok, count_after_increment)

The function increments first (so a race never under-counts), then
compares to the cap; if over, returns ok=False. Callers should NOT
proceed when ok=False. Cost is one DB round-trip per AI/chat call —
trivial vs. Anthropic latency.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Tuple

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def bump_and_check(channel: str, cap: int) -> Tuple[bool, int]:
    """Atomically increment today's counter for ``channel`` and return
    ``(ok, count_after)``.

    ok = True iff count_after <= cap. Caller must abort when ok=False.

    On any DB error, returns (True, 0) — fail-OPEN. The counter is a
    cost-cap, not a safety gate; refusing every AI call due to a DB
    blip would be a worse failure mode than the hypothetical small
    overage during the outage.
    """
    if cap <= 0:
        # 0 = disabled / no cap
        return True, 0
    try:
        from database import SessionLocal, engine
    except Exception:
        return True, 0
    day = _today_utc()
    try:
        db = SessionLocal()
        try:
            dialect = engine.dialect.name
            if dialect == "postgresql":
                stmt = text(
                    "INSERT INTO ai_call_budget (date, channel, count, cost_usd, last_call_at) "
                    "VALUES (:d, :ch, 1, 0.0, NOW()) "
                    "ON CONFLICT (date, channel) DO UPDATE "
                    "SET count = ai_call_budget.count + 1, last_call_at = NOW() "
                    "RETURNING count"
                )
            else:
                # SQLite (3.35+) supports the same syntax.
                stmt = text(
                    "INSERT INTO ai_call_budget (date, channel, count, cost_usd, last_call_at) "
                    "VALUES (:d, :ch, 1, 0.0, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (date, channel) DO UPDATE "
                    "SET count = count + 1, last_call_at = CURRENT_TIMESTAMP "
                    "RETURNING count"
                )
            row = db.execute(stmt, {"d": day, "ch": channel}).first()
            db.commit()
            count_after = int(row[0]) if row else 1
            return (count_after <= cap), count_after
        finally:
            db.close()
    except SQLAlchemyError as e:
        logger.warning(f"ai_call_budget: DB error on bump_and_check({channel}): {e}")
        return True, 0
    except Exception as e:
        logger.warning(f"ai_call_budget: unexpected error on bump_and_check({channel}): {e}")
        return True, 0


def peek(channel: str) -> int:
    """Read today's count for ``channel`` without incrementing."""
    try:
        from database import SessionLocal
        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT count FROM ai_call_budget WHERE date = :d AND channel = :ch"),
                {"d": _today_utc(), "ch": channel},
            ).first()
            return int(row[0]) if row else 0
        finally:
            db.close()
    except Exception:
        return 0
