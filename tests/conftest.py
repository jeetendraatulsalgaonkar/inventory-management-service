import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def payload():
    return {
        "sku": " lap-dell-1 ",
        "name": " Dell XPS ",
        "description": "   ",
        "category": " ELECTRONICS ",
        "price": {"amount": "999999999999999.9999", "currency": "EUR"},
        "unitOfMeasure": "PIECE",
        "attributes": {"brand": "Dell"},
    }


@pytest.fixture
def database():
    url = os.environ.get("INVENTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set INVENTRA_TEST_DATABASE_URL to run PostgreSQL integration tests")
    url = make_url(url)
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != "inventra_test"
        or url.username != "inventra_test"
    ):
        pytest.fail("Use a dedicated PostgreSQL database and role both named inventra_test")

    schema = f"test_{uuid4().hex}"
    admin = create_engine(url)
    engine = create_engine(url, connect_args={"options": f"-c search_path={schema}"})
    try:
        with admin.begin() as connection:
            privileged = connection.scalar(text("""
                SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user
            """))
            if privileged:
                pytest.fail("The test role must be NOCREATEDB and NOSUPERUSER")
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            config = Config(str(ROOT / "alembic.ini"))
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            yield engine, config
        finally:
            engine.dispose()
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        admin.dispose()
