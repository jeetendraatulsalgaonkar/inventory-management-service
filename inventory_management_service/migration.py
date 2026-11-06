from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from inventory_management_service.db import engine


def run_migrations():
    """Apply pending migrations before serving requests, one process at a time."""
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    with engine().begin() as connection:
        # Concurrent application workers must not migrate the database together.
        connection.execute(text("SELECT pg_advisory_xact_lock(746238190517)"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
