import logging
import os
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import inspect, text
from typing import Generator

logger = logging.getLogger("app.database")

# SQLite Database path relative to project
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bank_statement_converter.db")

# connect_args={"check_same_thread": False} is required for SQLite in multithreaded environments like FastAPI
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False
)

# Columns added after the first release. create_all() only creates missing *tables*, so existing databases
# need these added in place. Purely additive: nothing is dropped or rewritten.
_ADDED_COLUMNS = {
    "ledger_runs": {"provider": "VARCHAR DEFAULT 'xero'", "client_id": "VARCHAR", "auto_sync": "BOOLEAN DEFAULT 0"},
    "coding_rules": {"provider": "VARCHAR DEFAULT 'xero'", "client_id": "VARCHAR"},
}


def migrate_db(db_engine=None) -> None:
    """Add any missing columns listed in _ADDED_COLUMNS. Safe to run on every start."""
    eng = db_engine or engine
    insp = inspect(eng)
    with eng.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    logger.info("Added column %s.%s", table, name)


def recover_interrupted_runs(db_engine=None) -> int:
    """Background jobs run on threads of THIS process, so at startup none can still be alive: any run marked as
    running was orphaned by a restart/crash and would otherwise sit on 'fetching' forever. Returns runs recovered.

      fetching/categorising, no data yet  -> failed (fetch again)
      categorising with data (AI retry)   -> ready  (data is intact; AI step was cut short)
      applying                            -> ready  (re-applying is safe: it re-sends the same codes)"""
    eng = db_engine or engine
    no_items = "(items IS NULL OR items = 'null')"
    with eng.begin() as conn:
        if not inspect(conn).has_table("ledger_runs"):
            return 0
        n = 0
        n += conn.execute(text(
            f"UPDATE ledger_runs SET status='failed', error='Interrupted by an app restart before it finished. Fetch again.' "
            f"WHERE status IN ('fetching','categorising') AND {no_items}")).rowcount
        n += conn.execute(text(
            "UPDATE ledger_runs SET status='ready', progress='The AI step was interrupted by an app restart.' "
            f"WHERE status IN ('fetching','categorising') AND NOT {no_items}")).rowcount
        n += conn.execute(text(
            "UPDATE ledger_runs SET status='ready', error='An apply was interrupted by an app restart. Some lines may already "
            "be updated; check, then apply again (re-applying is safe).' WHERE status='applying'")).rowcount
    if n:
        logger.warning("Recovered %s ledger run(s) interrupted by a restart", n)
    return n


def init_db() -> None:
    """Initialize the database schema, creating all tables if they do not exist."""
    SQLModel.metadata.create_all(engine)
    migrate_db()
    recover_interrupted_runs()

def get_db() -> Generator[Session, None, None]:
    """Dependency generator to yield database sessions to API routes."""
    with Session(engine) as session:
        yield session
