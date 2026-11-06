from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from inventory_management_service.db import engine

# PostgreSQL advisory lock key that serializes startup migrations across processes.
MIGRATION_LOCK = 746238190517


def run_migrations():
    """Apply pending migrations before serving requests, one process at a time."""
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    with engine().connect() as connection:
        # Migrate at READ COMMITTED whatever the database or role default. Under
        # REPEATABLE READ or SERIALIZABLE, the snapshot would be taken by the lock
        # statement, before waiting, so a process that waited for another one would not
        # see its committed migration and would try to apply it again.
        connection.execution_options(isolation_level="READ COMMITTED")
        with connection.begin():
            # Concurrent application workers must not migrate the database together.
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
