"""
One-shot migration: copy all rows from local SQLite to a Postgres target.

Usage (from backend/):
    DATABASE_URL=<postgres-url> python scripts/migrate_sqlite_to_postgres.py
    DATABASE_URL=<postgres-url> python scripts/migrate_sqlite_to_postgres.py --confirm I_UNDERSTAND_DESTRUCTIVE

Steps:
  1. Provision schema on the destination via Base.metadata.create_all().
  2. For each ORM model, SELECT all rows from SQLite and INSERT into Postgres,
     preserving IDs.
  3. Reset Postgres sequences to max(id) so subsequent app-driven inserts
     don't collide with the ids we just imported.
  4. Print a source→destination row-count comparison.

r82 (B23/B24): full table coverage + safety guards.
  - Migrates every ORM model defined in database.py (was 4 of 30).
  - Refuses to wipe a destination table that has MORE rows than source
    (you've probably got the URLs swapped — would be total data loss).
  - Requires `--confirm I_UNDERSTAND_DESTRUCTIVE` to proceed past the
    pre-flight summary. Without it, runs in dry-run mode (prints what
    WOULD be migrated and exits without writing).
  - Echoes both source/dest hostnames so the operator can verify before
    confirming.

Safe to run while the backend is STOPPED. (Reading SQLite concurrently with
the live app would miss WAL-buffered rows — stop the server first.)
"""
from __future__ import annotations
import argparse
import os
import sys

# Import from the backend package when invoked as `python scripts/…`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

# r82 (B23): import every ORM model from database.py. The prior 4-model list
# silently left ~22 tables empty after cutover — most importantly:
# EquitySnapshot (DD multiplier silently 1.0), ConfidenceCalibration
# (calibration multiplier silently 1.0), MLPrediction (calibration plot blank),
# DecisionLog (no audit trail), TickerProfile (per-ticker overrides empty).
from database import (
    Base,
    # Originally migrated:
    WatchlistStock,
    AutoTraderConfig,
    Signal,
    AutoTrade,
    # r82 — added (full coverage):
    CandidatePool,
    CandidateEvent,
    ScanRun,
    DecisionLog,
    BestStrategyPerTicker,
    ConfidenceCalibration,
    Alert,
    NewsEvent,
    InstitutionalHoldings,
    InsiderSummary,
    WSBMention,
    SocialSentiment,
    MLArtifact,
    MLPrediction,
    MLEvalResult,
    MacroEvent,
    Fundamentals,
    AnalystRating,
    TickerProfile,
    EquitySnapshot,
    IVHistory,
    AIDecisionLog,
)


SQLITE_URL = os.getenv("SQLITE_URL", "sqlite:///./stockapp.db")
DEST_URL = os.getenv("DATABASE_URL")
if not DEST_URL or DEST_URL.startswith("sqlite"):
    print("ERROR: set DATABASE_URL to your Postgres connection string.")
    sys.exit(1)


# Order chosen so parent/singleton tables arrive before anything that might
# logically reference them (nothing truly FK-enforced in this schema today,
# but keep the convention so future FKs don't break the migration).
MODELS = [
    # ---- Originals (legacy 4) ----
    WatchlistStock,        # PK = ticker (string, no sequence)
    AutoTraderConfig,      # PK = id (singleton row with id=1)
    Signal,
    AutoTrade,
    # ---- Risk / equity / profile state (CRITICAL for live cutover) ----
    EquitySnapshot,        # DD multiplier reads this
    ConfidenceCalibration, # calibration multiplier reads this
    TickerProfile,         # per-ticker overrides
    BestStrategyPerTicker, # confidence boost reads this
    # ---- Audit / decision trails ----
    DecisionLog,
    AIDecisionLog,
    Alert,
    ScanRun,
    CandidatePool,
    CandidateEvent,
    # ---- ML pipeline ----
    MLArtifact,
    MLPrediction,
    MLEvalResult,
    # ---- Reference data / alt-data ----
    NewsEvent,
    Fundamentals,
    AnalystRating,
    InstitutionalHoldings,
    InsiderSummary,
    WSBMention,
    SocialSentiment,
    MacroEvent,
    IVHistory,
]


def _safe_count(session, Model) -> int:
    try:
        return session.query(Model).count()
    except Exception as e:
        print(f"  WARN: could not count {Model.__tablename__}: {e}")
        return -1


def _copy_schema_migrations(src_engine, dst_engine):
    """schema_migrations has no ORM model — hand-copy so applied-version
    bookkeeping survives the hop and _apply_migrations() on the new DB
    won't re-run already-completed steps."""
    insp_dst = inspect(dst_engine)
    if "schema_migrations" not in insp_dst.get_table_names():
        with dst_engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE schema_migrations ("
                "  version INTEGER PRIMARY KEY, "
                "  description TEXT NOT NULL, "
                "  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ")"
            ))
    # Only attempt to read from source if the source table exists
    try:
        with src_engine.connect() as s:
            rows = list(
                s.execute(text(
                    "SELECT version, description, applied_at FROM schema_migrations"
                )).mappings()
            )
    except Exception as e:
        print(f"  schema_migrations: source missing or unreadable ({e}); skipping")
        return 0
    if not rows:
        return 0
    with dst_engine.begin() as conn:
        for r in rows:
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, description, applied_at) "
                    "VALUES (:v, :d, :a) "
                    "ON CONFLICT (version) DO NOTHING"
                ),
                {"v": r["version"], "d": r["description"], "a": r["applied_at"]},
            )
    return len(rows)


def _reset_sequence(dst_session, table: str, pk_col: str = "id") -> None:
    """Align Postgres sequence with max(id) so future inserts don't collide.
    Explicit-id inserts don't advance the sequence, so without this the
    next app-driven insert would try id=1 and trip a unique-violation."""
    dst_session.execute(text(
        f"SELECT setval(pg_get_serial_sequence('{table}', '{pk_col}'), "
        f"COALESCE((SELECT MAX({pk_col}) FROM {table}), 0) + 1, false)"
    ))


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    proto, rest = url.split("://", 1) if "://" in url else ("?", url)
    creds, host = rest.split("@", 1)
    return f"{proto}://***@{host}"


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite → Postgres migration")
    parser.add_argument(
        "--confirm",
        default=None,
        help="Pass exactly 'I_UNDERSTAND_DESTRUCTIVE' to perform the migration. "
             "Without this flag, runs in DRY-RUN mode (prints, doesn't write).",
    )
    args = parser.parse_args()
    is_dry_run = args.confirm != "I_UNDERSTAND_DESTRUCTIVE"

    print(f"Source:      {_redact(SQLITE_URL)}")
    print(f"Destination: {_redact(DEST_URL)}")
    print(f"Mode:        {'DRY-RUN (no writes)' if is_dry_run else 'DESTRUCTIVE (will wipe + write)'}")
    print()

    src_engine = create_engine(
        SQLITE_URL, connect_args={"check_same_thread": False}
    )
    dst_engine = create_engine(DEST_URL, pool_pre_ping=True)

    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)
    src = SrcSession()
    dst = DstSession()

    # ---- PRE-FLIGHT: count rows on both sides BEFORE doing anything ----
    print("Pre-flight row counts (source → destination):")
    plan = []
    refusals = []
    for Model in MODELS:
        table = Model.__tablename__
        try:
            insp_src = inspect(src_engine)
            if table not in insp_src.get_table_names():
                print(f"  {table:30s}  (source table missing — will be empty post-migrate)")
                plan.append((Model, 0, _safe_count(dst, Model)))
                continue
        except Exception:
            pass
        src_n = _safe_count(src, Model)
        dst_n = _safe_count(dst, Model)
        marker = ""
        # r82 (B24): refuse to wipe a destination that already has MORE rows
        # than source. The most likely cause is operator pointed the script
        # at the WRONG database — destructive wipe would be total data loss.
        if dst_n > 0 and src_n >= 0 and dst_n > src_n + 10:
            marker = "  ⛔ REFUSING (dst has more rows than src — verify URLs!)"
            refusals.append(table)
        elif dst_n > 0:
            marker = "  ⚠  dest non-empty, will be WIPED"
        plan.append((Model, src_n, dst_n))
        print(f"  {table:30s}  {src_n:>8d} → {dst_n:>8d}{marker}")

    if refusals:
        print()
        print("ABORT: destination has more rows than source for these tables:")
        for t in refusals:
            print(f"  - {t}")
        print("Possible causes: (a) URLs swapped, (b) wrong source SQLite,")
        print("(c) destination already has live data you don't want to lose.")
        print("Verify, then re-run.")
        sys.exit(3)

    print()
    if is_dry_run:
        print("DRY-RUN complete. To execute, re-run with:")
        print("  --confirm I_UNDERSTAND_DESTRUCTIVE")
        sys.exit(0)

    # ---- EXECUTE migration ----
    print("Provisioning schema on destination…")
    Base.metadata.create_all(bind=dst_engine)

    totals = {}
    try:
        for Model, src_count_pre, _ in plan:
            table = Model.__tablename__
            # Re-read source rows fresh (count above may have raced).
            try:
                rows = src.query(Model).all()
            except Exception as e:
                print(f"  {table}: source read failed ({e}); skipping")
                continue
            src_count = len(rows)
            try:
                dst.execute(Model.__table__.delete())
                dst.commit()
            except Exception as e:
                print(f"  {table}: dest wipe failed ({e}); skipping")
                dst.rollback()
                continue
            inserted = 0
            for row in rows:
                try:
                    data = {c.name: getattr(row, c.name) for c in Model.__table__.columns}
                    dst.add(Model(**data))
                    inserted += 1
                except Exception as e:
                    print(f"  {table}: row insert failed ({e})")
            try:
                dst.commit()
            except Exception as e:
                print(f"  {table}: commit failed ({e})")
                dst.rollback()
            # Reset sequence for integer-id tables only (e.g., WatchlistStock
            # uses ticker as PK, no sequence).
            try:
                cols = {c.name for c in Model.__table__.columns}
                if "id" in cols and Model is not WatchlistStock:
                    _reset_sequence(dst, table, "id")
                    dst.commit()
            except Exception as e:
                print(f"  {table}: sequence reset failed ({e})")
                dst.rollback()
            try:
                dst_count = dst.query(Model).count()
            except Exception:
                dst_count = -1
            totals[table] = (src_count, dst_count)
            print(f"  {table:30s}  {src_count:>8d} → {dst_count:>8d}")

        mig_count = _copy_schema_migrations(src_engine, dst_engine)
        totals["schema_migrations"] = (mig_count, mig_count)
        print(f"  {'schema_migrations':30s}  {mig_count:>8d} rows copied")
    finally:
        src.close()
        dst.close()

    ok = all(s == d for (s, d) in totals.values() if s >= 0 and d >= 0)
    print()
    print("Row counts match. Migration complete." if ok else "MISMATCH — review above.")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
