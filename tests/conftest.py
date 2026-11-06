import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from inventory_management_service.models import Product

ROOT = Path(__file__).resolve().parents[1]


def setting(name):
    """Read a test setting from the environment, falling back to the project's .env file.

    IDE test runners such as PyCharm do not inherit variables set in a terminal, so a
    git-ignored .env file in the project root (lines like NAME=value) also works.
    The environment wins when both are set.
    """
    if os.environ.get(name):
        return os.environ[name]
    path = ROOT / ".env"
    if not path.is_file():
        return None
    # utf-8-sig tolerates the byte-order mark that some Windows editors write.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.strip().partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


class FakeSession:
    """In-memory stand-in for the SQLAlchemy Session used by the service layer.

    It supports the calls the services make (begin, add, connection, scalar, and
    scalars), records them in ``events`` in call order, and returns queued
    ``results``. This lets unit tests check service logic and the statements it
    issues without PostgreSQL.
    """

    def __init__(self, *results):
        self.results = list(results)
        self.events = []
        self.added = []
        self.commit_error = None
        self.in_transaction = False

    @contextmanager
    def begin(self):
        assert not self.in_transaction, "nested transactions are not expected"
        self.in_transaction = True
        self.events.append(("begin",))
        try:
            yield self
            if self.commit_error is not None:
                raise self.commit_error
            # A real flush assigns the initial optimistic-locking version.
            for instance in self.added:
                if instance.version is None:
                    instance.version = Product.__mapper__.version_id_generator(None)
            self.events.append(("commit",))
        finally:
            self.in_transaction = False

    def add(self, instance):
        assert self.in_transaction, "add() must run inside a transaction"
        self.added.append(instance)
        self.events.append(("add", instance))

    def connection(self, execution_options=None):
        assert self.in_transaction, "connection() must run inside a transaction"
        self.events.append(("connection", execution_options or {}))

    def scalar(self, statement):
        assert self.in_transaction, "queries must run inside a transaction"
        self.events.append(("scalar", statement))
        return self.results.pop(0)

    def scalars(self, statement):
        assert self.in_transaction, "queries must run inside a transaction"
        self.events.append(("scalars", statement))
        return iter(self.results.pop(0))

    @property
    def statements(self):
        return [event[1] for event in self.events if event[0] in ("scalar", "scalars")]


@pytest.fixture
def fake_session():
    return FakeSession()


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
    url = setting("INVENTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set INVENTRA_TEST_DATABASE_URL, as an environment variable or in the project's "
            ".env file, to run PostgreSQL integration tests"
        )
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
