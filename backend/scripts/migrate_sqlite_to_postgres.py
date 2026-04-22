"""
One-shot migration: copy all rows from local SQLite to a Postgres target.

Usage (from backend/):
    DATABASE_URL=<postgres-url> python scripts/migrate_sqlite_to_postgres.py

Steps:
  1. Provision schema on the destination via Base.metadata.create_all().
  2. For each ORM model, SELECT all rows from SQLite and INSERT into Postgres,
     preserving IDs. Dest tables are truncated first so the script is
     idempotent — safe to re-run after fixing issues.
  3. Reset Postgres sequences to max(id) so subsequent app-driven inserts
     don't collide with the ids we just imported.
  4. Print a source→destination row-count comparison.

Safe to run while the backend is STOPPED. (Reading SQLite concurrently with
the live app would miss WAL-buffered rows — stop the server first.)
"""
from __future__ import annotations
import os
import sys

# Import from the backend package when invoked as `python scripts/…`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

from database import Base, WatchlistStock, Signal, AutoTraderConfig, AutoTrade


SQLITE_URL = os.getenv("SQLITE_URL", "sqlite:///./stockapp.db")
DEST_URL = os.getenv("DATABASE_URL")
if not DEST_URL or DEST_URL.startswith("sqlite"):
    print("ERROR: set DATABASE_URL to your Postgres connection string.")
    sys.exit(1)


# Order chosen so parent/singleton tables arrive before anything that might
# logically reference them (nothing truly FK-enforced in this schema today,
# but keep the convention so future FKs don't break the migration).
MODELS = [
    WatchlistStock,        # PK = ticker (string, no sequence)
    AutoTraderConfig,      # PK = id (singleton row with id=1)
    Signal,                # PK = id (autoincrement)
    AutoTrade,             # PK = id (autoincrement)
]


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
    with src_engine.connect() as s:
        rows = list(
            s.execute(text(
                "SELECT version, description, applied_at FROM schema_migrations"
            )).mappings()
        )
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


def main() -> None:
    # Redact password in printed URL
    safe_dest = DEST_URL.split("@", 1)[-1] if "@" in DEST_URL else DEST_URL
    print(f"Source:      {SQLITE_URL}")
    print(f"Destination: …@{safe_dest}")

    src_engine = create_engine(
        SQLITE_URL, connect_args={"check_same_thread": False}
    )
    dst_engine = create_engine(DEST_URL, pool_pre_ping=True)

    print("\nProvisioning schema on destination…")
    Base.metadata.create_all(bind=dst_engine)

    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)
    src = SrcSession()
    dst = DstSession()

    totals = {}
    try:
        for Model in MODELS:
            table = Model.__tablename__
            rows = src.query(Model).all()
            src_count = len(rows)
            # Wipe existing dest rows — makes the script idempotent.
            dst.execute(Model.__table__.delete())
            dst.commit()
            for row in rows:
                data = {c.name: getattr(row, c.name) for c in Model.__table__.columns}
                dst.add(Model(**data))
            dst.commit()
            # Reset sequence for integer-id tables only (WatchlistStock uses
            # ticker as PK, no sequence).
            if Model is not WatchlistStock and "id" in {c.name for c in Model.__table__.columns}:
                _reset_sequence(dst, table, "id")
                dst.commit()
            dst_count = dst.query(Model).count()
            totals[table] = (src_count, dst_count)
            print(f"  {table:25s}  {src_count:6d} → {dst_count:6d}")

        mig_count = _copy_schema_migrations(src_engine, dst_engine)
        totals["schema_migrations"] = (mig_count, mig_count)
        print(f"  {'schema_migrations':25s}  {mig_count:6d} rows copied")
    finally:
        src.close()
        dst.close()

    ok = all(s == d for (s, d) in totals.values())
    print("\n" + ("Row counts match. Migration complete." if ok else "MISMATCH — review above."))
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
