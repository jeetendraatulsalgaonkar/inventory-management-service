import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.pool import NullPool

import httpx

from inventory_management_service.models import Product

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE = "inventra_test"     # the dedicated test database and role share this name


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


# ---------------------------------------------------------------------------
# Full verification: pytest --require-postgres
#
# A normal run skips the PostgreSQL tests when no test database is configured.
# With --require-postgres, the run validates the dedicated test database before any
# test starts and fails instead of reporting success if a PostgreSQL test was skipped
# or none ran, or a test schema was left behind. Runs that select only part of the
# suite are refused before any test starts.
# Exit codes: 0 passed; 1 tests failed or verification incomplete; 4 refused before
# running (configuration missing or unsafe, database unreachable, partial selection).
# ---------------------------------------------------------------------------

class Verification:
    def __init__(self, url, server):
        self.url = url                  # validated test database URL
        self.server = server            # description shown in the report header
        self.postgres_tests = set()     # node ids of the collected PostgreSQL tests
        self.postgres_ran = 0
        self.postgres_skipped = []
        self.deselected = 0
        self.schemas = set()            # schemas created by the database fixtures
        self.servers = []               # application processes started by tests
        self.problems = []


VERIFICATION = None


def pytest_addoption(parser):
    parser.addoption(
        "--require-postgres", action="store_true",
        help="Full verification: validate the dedicated PostgreSQL test database first, "
             "then fail unless every test, including every PostgreSQL test, ran and passed.",
    )


def refuse(message):
    """Stop before any test runs; pytest exits with code 4 (usage error)."""
    pytest.exit(f"Full verification refused: {message}", returncode=pytest.ExitCode.USAGE_ERROR)


def validate_test_database(url_text):
    """Check that the URL names the dedicated test database and that it is reachable
    and safe. Return (url, description). Nothing is created or changed."""
    if not url_text:
        refuse("INVENTRA_TEST_DATABASE_URL is not set in the environment or the project's .env file.")
    try:
        url = make_url(url_text)
    except ArgumentError:
        refuse("INVENTRA_TEST_DATABASE_URL is not a valid database URL.")
    shown = url.render_as_string(hide_password=True)
    if (url.drivername, url.database, url.username) != ("postgresql+psycopg", TEST_DATABASE, TEST_DATABASE):
        refuse(f"{shown} is not the dedicated test database. Use the postgresql+psycopg driver "
               f"with a database and role both named {TEST_DATABASE}.")
    development = os.environ.get("DATABASE_URL")
    if development:
        try:
            development_database = make_url(development).database
        except ArgumentError:
            development_database = None
        if development_database == TEST_DATABASE:
            refuse(f"DATABASE_URL also names {TEST_DATABASE}; the application and the tests "
                   "must not share a database.")
    engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            # Ask the server itself, in case a proxy or service file maps the name elsewhere.
            facts = connection.execute(text("""
                SELECT current_database() AS database, current_user AS role,
                       rolsuper OR rolcreatedb AS privileged,
                       has_database_privilege(current_database(), 'CREATE') AS can_create,
                       current_setting('server_version') AS version
                FROM pg_roles WHERE rolname = current_user
            """)).one()
    except DBAPIError as error:
        reason = str(error.orig).strip().splitlines()[0] if error.orig else str(error)
        refuse(f"cannot connect to {shown}: {reason}")
    finally:
        engine.dispose()
    if (facts.database, facts.role) != (TEST_DATABASE, TEST_DATABASE):
        refuse(f"the server reports database {facts.database!r} and role {facts.role!r}, "
               f"not {TEST_DATABASE}.")
    if facts.privileged:
        refuse(f"role {TEST_DATABASE} must be NOSUPERUSER and NOCREATEDB.")
    if not facts.can_create:
        refuse(f"role {TEST_DATABASE} cannot create schemas; run tests/init-test-db.sql.")
    return url, f"PostgreSQL {facts.version} at {url.host}:{url.port or 5432}/{facts.database} as {facts.role}"


def pytest_sessionstart(session):
    global VERIFICATION
    config = session.config
    if not config.getoption("--require-postgres"):
        return
    if any("::" in argument for argument in config.args):
        refuse("it runs the complete suite; do not select individual tests.")
    url, server = validate_test_database(setting("INVENTRA_TEST_DATABASE_URL"))
    # No test needs the development database. Removing DATABASE_URL from this process
    # ensures nothing can reach it through the application's engine.
    os.environ.pop("DATABASE_URL", None)
    VERIFICATION = Verification(url, server)


def pytest_report_header(config):
    if VERIFICATION is not None:
        return f"full verification: {VERIFICATION.server}"


def pytest_deselected(items):
    if VERIFICATION is not None:
        VERIFICATION.deselected += len(items)


def pytest_collection_finish(session):
    if VERIFICATION is None:
        return
    if VERIFICATION.deselected:
        refuse(f"it runs every test, but {VERIFICATION.deselected} were deselected (-k, -m, or --lf).")
    # However the suite was named (no arguments, the project, or the tests folder, as
    # IDE runners do), every test module must have been collected.
    expected = {path.name for path in (ROOT / "tests").glob("test_*.py")}
    collected = {item.path.name for item in session.items}
    if missing := sorted(expected - collected):
        refuse(f"it runs the complete suite, but these test modules were not collected: {', '.join(missing)}.")
    VERIFICATION.postgres_tests = {
        item.nodeid for item in session.items if item.get_closest_marker("postgres")
    }


def pytest_runtest_logreport(report):
    if VERIFICATION is None or report.nodeid not in VERIFICATION.postgres_tests:
        return
    if report.skipped:
        VERIFICATION.postgres_skipped.append(report.nodeid)
    elif report.when == "call":
        VERIFICATION.postgres_ran += 1


def pytest_sessionfinish(session, exitstatus):
    if VERIFICATION is None or exitstatus not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return
    problems = VERIFICATION.problems
    if VERIFICATION.postgres_skipped:
        problems.append(f"{len(VERIFICATION.postgres_skipped)} PostgreSQL integration test(s) were skipped: "
                        + ", ".join(VERIFICATION.postgres_skipped[:5]))
    if VERIFICATION.postgres_ran == 0:
        problems.append("no PostgreSQL integration test ran.")
    if VERIFICATION.schemas:
        engine = create_engine(VERIFICATION.url, poolclass=NullPool, connect_args={"connect_timeout": 5})
        try:
            with engine.connect() as connection:
                leftover = connection.scalars(
                    text("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:names)"),
                    {"names": sorted(VERIFICATION.schemas)},
                ).all()
        finally:
            engine.dispose()
        if leftover:
            problems.append(f"test schemas were not cleaned up: {', '.join(leftover)}")
    running = [server for server in VERIFICATION.servers if server.process.poll() is None]
    if running:
        problems.append(f"{len(running)} application process(es) started by tests were still running.")
        for server in running:      # only processes this run started
            server.stop()
    if problems and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus):
    # After a refusal (exit code 4) or an interruption, pytest already shows the reason.
    if VERIFICATION is None or exitstatus not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return
    if VERIFICATION.problems or exitstatus != pytest.ExitCode.OK:
        terminalreporter.section("FULL VERIFICATION FAILED", sep="=", red=True, bold=True)
        for problem in VERIFICATION.problems or ["tests failed; see above."]:
            terminalreporter.write_line(f"- {problem}")
    else:
        terminalreporter.section(
            f"FULL VERIFICATION PASSED: {VERIFICATION.postgres_ran} PostgreSQL integration tests ran "
            f"against {VERIFICATION.server}; none skipped or deselected; test schemas removed",
            sep="=", green=True, bold=True,
        )


class FakeSession:
    """In-memory stand-in for the SQLAlchemy Session used by the service layer.

    It supports the calls the services make (begin, add, add_all, connection, scalar,
    scalars, and COPY through the driver cursor of ``connection()``), records them in
    ``events`` in call order, and returns queued ``results``; a queued exception is
    raised instead. ``copied`` holds the rows of every COPY whose transaction committed. This lets unit
    tests check service logic and the statements it issues without PostgreSQL.
    ``commit_error`` fails the next commit only, as a failed transaction does; later
    transactions of the session commit normally.
    """

    def __init__(self, *results):
        self.results = list(results)
        self.events = []
        self.added = []
        self.copied = []
        self.pending_rows = []          # copied in the current transaction, not yet committed
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
                error, self.commit_error = self.commit_error, None
                self.events.append(("rollback",))
                raise error
            self.copied += self.pending_rows
            # A real flush assigns the initial optimistic-locking version.
            for instance in self.added:
                if instance.version is None:
                    instance.version = Product.__mapper__.version_id_generator(None)
            self.events.append(("commit",))
        finally:
            self.in_transaction = False
            self.pending_rows = []

    def add(self, instance):
        assert self.in_transaction, "add() must run inside a transaction"
        self.added.append(instance)
        self.events.append(("add", instance))

    def add_all(self, instances):
        for instance in instances:
            self.add(instance)

    def connection(self, execution_options=None):
        assert self.in_transaction, "connection() must run inside a transaction"
        self.events.append(("connection", execution_options or {}))
        # session.connection().connection.cursor() is the driver cursor.
        return SimpleNamespace(connection=SimpleNamespace(cursor=lambda: SimpleNamespace(copy=self.copy)))

    @contextmanager
    def copy(self, statement):
        assert self.in_transaction, "COPY must run inside a transaction"
        rows = []
        yield SimpleNamespace(write_row=lambda row: rows.append(tuple(row)))
        self.pending_rows += rows
        self.events.append(("copy", statement, rows))

    def next_result(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result                # a queued exception makes the query fail
        return result

    def scalar(self, statement):
        assert self.in_transaction, "queries must run inside a transaction"
        self.events.append(("scalar", statement))
        return self.next_result()

    def scalars(self, statement):
        assert self.in_transaction, "queries must run inside a transaction"
        self.events.append(("scalars", statement))
        return iter(self.next_result())

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


def test_database_url():
    """The dedicated test database URL. Skips when unset, except in full verification."""
    url = setting("INVENTRA_TEST_DATABASE_URL")
    if not url and VERIFICATION is not None:
        pytest.fail("INVENTRA_TEST_DATABASE_URL disappeared during full verification")
    if not url:
        pytest.skip(
            "Set INVENTRA_TEST_DATABASE_URL, as an environment variable or in the project's "
            ".env file, to run PostgreSQL integration tests"
        )
    url = make_url(url)
    if (
        url.drivername != "postgresql+psycopg"
        or url.database != TEST_DATABASE
        or url.username != TEST_DATABASE
    ):
        pytest.fail("Use a dedicated PostgreSQL database and role both named inventra_test")
    return url


class IsolatedSchema(SimpleNamespace):
    """A uniquely named, initially empty schema in the test database."""

    def app_url(self, *settings, application_name=None):
        """DATABASE_URL for an application process that should use this schema.

        ``settings`` are extra server settings such as "default_transaction_isolation=serializable".
        """
        options = " ".join(f"-c{item.replace(' ', chr(92) + ' ')}" for item in (f"search_path={self.schema}", *settings))
        query = {"options": options} | ({"application_name": application_name} if application_name else {})
        return self.url.update_query_dict(query).render_as_string(hide_password=False)


@pytest.fixture
def blank_database():
    """An empty schema in the test database, with nothing migrated. Dropped afterwards."""
    url = test_database_url()
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
        if VERIFICATION is not None:
            VERIFICATION.schemas.add(schema)
        try:
            yield IsolatedSchema(url=url, schema=schema, engine=engine, admin=admin)
        finally:
            engine.dispose()
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        admin.dispose()


def alembic_config():
    return Config(str(ROOT / "alembic.ini"))


def migrate(engine, revision="head"):
    """Upgrade a schema in-process, as a previous deployment would have."""
    config = alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
    return config


@pytest.fixture
def database(blank_database):
    """A test schema migrated to head, with the sample products."""
    config = migrate(blank_database.engine)
    yield blank_database.engine, config


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class AppServer:
    """The application started the way users start it: uvicorn in its own process."""

    def __init__(self, database_url, log_path, args=()):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        self._log = open(log_path, "wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "inventory_management_service.main:app",
             "--host", "127.0.0.1", "--port", str(self.port), *args],
            cwd=ROOT, env={**os.environ, "DATABASE_URL": database_url},
            stdout=self._log, stderr=subprocess.STDOUT,
        )

    @property
    def output(self):
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def accepts_connections(self):
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
            return True
        except OSError:
            return False

    def wait_until_serving(self, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            assert self.process.poll() is None, f"the server exited during startup:\n{self.output}"
            try:
                if httpx.get(f"{self.base_url}/openapi.json", timeout=2).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, f"the server did not start within {timeout}s:\n{self.output}"
            time.sleep(0.1)

    def wait_for_exit(self, timeout=60):
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pytest.fail(f"the server did not exit within {timeout}s:\n{self.output}")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._log.close()


@pytest.fixture
def app_servers(tmp_path):
    """Start application processes with start(database_url, *uvicorn_args); all are stopped afterwards."""
    started = []

    def start(database_url, *args):
        server = AppServer(database_url, tmp_path / f"server-{len(started)}.log", args)
        started.append(server)
        if VERIFICATION is not None:
            VERIFICATION.servers.append(server)
        return server

    try:
        yield start
    finally:
        for server in started:
            server.stop()


@pytest.fixture
def migration_lock(blank_database):
    """Hold the application's migration lock so that tests decide when startup may proceed."""
    from inventory_management_service.migration import MIGRATION_LOCK

    connection = blank_database.admin.connect()
    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK})
    connection.commit()

    def waiting(application_name):
        """Number of sessions of that application waiting for the migration lock."""
        count = connection.scalar(text("""
            SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.locktype = 'advisory' AND NOT l.granted AND a.application_name = :name
              AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
        """), {"name": application_name})
        # End the transaction: PostgreSQL keeps pg_stat_activity fixed within one.
        connection.commit()
        return count

    def wait_for_waiters(application_name, count, timeout=60):
        deadline = time.monotonic() + timeout
        while waiting(application_name) < count:
            assert time.monotonic() < deadline, f"{count} startups did not reach the migration lock"
            time.sleep(0.05)

    held = {"value": True}

    def release():
        if held["value"]:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK})
            connection.commit()
            held["value"] = False

    try:
        yield SimpleNamespace(wait_for_waiters=wait_for_waiters, release=release)
    finally:
        release()
        connection.close()
