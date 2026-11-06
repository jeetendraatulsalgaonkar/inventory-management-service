"""Automatic startup migrations.

The first two tests use an in-process client with the migration step replaced. The
others start the real application (uvicorn in its own process, as users run it) against
an isolated schema in the test database and check the database it leaves behind. A
fixture can hold the application's migration lock, so the tests decide exactly when a
startup may proceed and can observe the application while it waits.
"""

import re
import time
from pathlib import Path
from uuid import uuid7

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text

from inventory_management_service.main import PRODUCTS, app

ROOT = Path(__file__).resolve().parents[1]
HEAD = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
# The sample products exactly as the applied seed migration inserts them: SKU -> id.
SEEDS = dict(
    (sku, product_id) for product_id, sku in re.findall(
        r"\('([0-9a-f-]{36})'::uuid, '([A-Z0-9._-]+)'",
        (ROOT / "migrations" / "sql" / "0002_sample_products.sql").read_text(encoding="utf-8"),
    )
)


# ---------------------------------------------------------------------------
# In-process startup (no database)
# ---------------------------------------------------------------------------

def test_startup_runs_migrations_before_serving(monkeypatch):
    calls = []
    monkeypatch.setattr("inventory_management_service.main.run_migrations", lambda: calls.append("migrated"))
    with TestClient(app) as client:
        assert calls == ["migrated"]
        assert client.get("/docs").status_code == 200


def test_startup_stops_if_migration_fails(monkeypatch):
    def fail():
        raise RuntimeError("Database unavailable")

    monkeypatch.setattr("inventory_management_service.main.run_migrations", fail)
    with pytest.raises(RuntimeError, match="Database unavailable"):
        with TestClient(app):
            pytest.fail("The application must not start after a migration failure")


# ---------------------------------------------------------------------------
# Helpers for the PostgreSQL startup tests
# ---------------------------------------------------------------------------

def migrate(engine, revision):
    """Upgrade a schema in-process, as an earlier deployment would have."""
    config = Config(str(ROOT / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def table_exists(connection, name):
    return connection.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name})


def versions(engine):
    """Rows of alembic_version, or [] if the schema was never migrated."""
    with engine.connect() as connection:
        if not table_exists(connection, "alembic_version"):
            return []
        return connection.scalars(text("SELECT version_num FROM alembic_version")).all()


def product_rows(engine):
    """Every stored product row, complete, in id order."""
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(text("SELECT * FROM products ORDER BY id"))]


def sku_counts(engine):
    with engine.connect() as connection:
        return dict(connection.execute(text("SELECT sku, count(*) FROM products GROUP BY sku")).all())


def product_count(server):
    response = httpx.get(f"{server.base_url}{PRODUCTS}", params={"size": 1}, timeout=10)
    assert response.status_code == 200, response.text
    return response.json()["totalItems"]


# ---------------------------------------------------------------------------
# Fresh database and repeated startups
# ---------------------------------------------------------------------------

@pytest.mark.postgres
def test_startup_migrates_a_fresh_database(blank_database, app_servers):
    server = app_servers(blank_database.app_url())
    server.wait_until_serving()

    assert versions(blank_database.engine) == [HEAD]
    with blank_database.engine.connect() as connection:
        stored = dict(connection.execute(text("SELECT sku, id::text FROM products")).all())
        states = connection.execute(text(
            "SELECT DISTINCT status, deleted_at IS NULL, version FROM products"
        )).all()
    assert len(SEEDS) == 115
    assert stored == SEEDS                         # every sample, with its fixed id, once
    assert states == [("ACTIVE", True, 0)]
    assert product_count(server) == 115


@pytest.mark.postgres
def test_restarts_preserve_products_and_never_reseed(blank_database, app_servers):
    engine = blank_database.engine
    first = app_servers(blank_database.app_url())
    first.wait_until_serving()
    created = httpx.post(f"{first.base_url}{PRODUCTS}", timeout=10, json={
        "sku": "user-1", "name": "User product", "category": "TOOLS",
        "price": {"amount": "12.3400", "currency": "EUR"}, "unitOfMeasure": "PIECE", "attributes": {},
    })
    assert created.status_code == 201, created.text
    first.stop()

    # Edits made while the application was down: an edited sample, a soft-deleted
    # sample, and a sample removed entirely.
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE products SET name = 'Edited laptop', price_amount = 1.2345, version = 4,
                   updated_at = updated_at + interval '1 day'
            WHERE sku = 'DEMO-LAPTOP-001'
        """))
        connection.execute(text("""
            UPDATE products SET status = 'DELETED', deleted_at = updated_at WHERE sku = 'DEMO-MOUSE-001'
        """))
        connection.execute(text("DELETE FROM products WHERE sku = 'DEMO-KEYBOARD-001'"))
    before = product_rows(engine)

    for _ in range(2):
        server = app_servers(blank_database.app_url())
        server.wait_until_serving()
        assert versions(engine) == [HEAD]
        assert product_rows(engine) == before      # ids, edited values, and deletions unchanged
        server.stop()

    counts = sku_counts(engine)
    assert set(counts.values()) == {1}
    assert "DEMO-KEYBOARD-001" not in counts       # a removed sample is not re-seeded
    assert len(counts) == 115                      # 114 samples + 1 user product
    server = app_servers(blank_database.app_url())
    server.wait_until_serving()
    edited = httpx.get(f"{server.base_url}{PRODUCTS}/{SEEDS['DEMO-LAPTOP-001']}", timeout=10).json()
    assert (edited["name"], edited["price"]["amount"], edited["version"]) == ("Edited laptop", 1.2345, 4)
    assert httpx.get(f"{server.base_url}{PRODUCTS}/{SEEDS['DEMO-MOUSE-001']}", timeout=10).status_code == 404
    assert httpx.get(created.headers["location"].replace(first.base_url, server.base_url), timeout=10).status_code == 200


@pytest.mark.postgres
def test_upgrade_from_the_first_release_preserves_existing_products(blank_database, app_servers):
    """A database created by release 0001 (no seed) already holds user products, one of
    which uses a sample SKU. Startup applies the seed without touching them."""
    engine = blank_database.engine
    migrate(engine, "0001")
    own, clash = uuid7(), uuid7()
    with engine.begin() as connection:
        for product_id, sku, name in [(own, "USER-1", "Own product"), (clash, "DEMO-MOUSE-001", "My mouse")]:
            connection.execute(text("""
                INSERT INTO products (id, sku, name, category, price_amount, price_currency, unit_of_measure,
                                      attributes, status, created_at, updated_at, version)
                VALUES (:id, :sku, :name, 'MINE', 7.5000, 'USD', 'BOX', '{"k": "v"}', 'INACTIVE',
                        now() - interval '1 year', now() - interval '1 day', 3)
            """), {"id": product_id, "sku": sku, "name": name})
    existing = product_rows(engine)

    server = app_servers(blank_database.app_url())
    server.wait_until_serving()

    assert versions(engine) == [HEAD]
    rows = product_rows(engine)
    assert [row for row in rows if row[0] in (own, clash)] == existing
    counts = sku_counts(engine)
    assert set(counts.values()) == {1}             # the clashing sample SKU is not duplicated
    assert len(rows) == 2 + 114
    with engine.connect() as connection:
        stored = dict(connection.execute(text("SELECT sku, id::text FROM products")).all())
    assert stored["DEMO-MOUSE-001"] == str(clash)
    assert {sku: stored[sku] for sku in SEEDS if sku != "DEMO-MOUSE-001"} == {
        sku: product_id for sku, product_id in SEEDS.items() if sku != "DEMO-MOUSE-001"
    }


# ---------------------------------------------------------------------------
# Concurrency and ordering
# ---------------------------------------------------------------------------

# A database or role can change the default isolation level; startup must still work.
ISOLATION = [None, "default_transaction_isolation=repeatable read", "default_transaction_isolation=serializable"]


@pytest.mark.postgres
@pytest.mark.parametrize("isolation", ISOLATION, ids=["server-default", "repeatable-read", "serializable"])
def test_concurrent_startups_migrate_exactly_once(blank_database, app_servers, migration_lock, isolation):
    name = "inventra-concurrent-startup"
    url = blank_database.app_url(*filter(None, [isolation]), application_name=name)
    servers = [app_servers(url) for _ in range(4)]
    # All four reach the migration lock before any of them may migrate.
    migration_lock.wait_for_waiters(name, 4)
    assert not any(server.accepts_connections() for server in servers)
    migration_lock.release()

    for server in servers:
        server.wait_until_serving()
    assert versions(blank_database.engine) == [HEAD]
    assert sku_counts(blank_database.engine) == dict.fromkeys(SEEDS, 1)
    assert [product_count(server) for server in servers] == [115] * 4


@pytest.mark.postgres
@pytest.mark.parametrize("args", [(), ("--workers", "2")], ids=["single-process", "two-workers"])
def test_no_request_is_answered_before_migrations_finish(blank_database, app_servers, migration_lock, args):
    name = "inventra-ordering"
    server = app_servers(blank_database.app_url(application_name=name), *args)
    migration_lock.wait_for_waiters(name, 2 if args else 1)

    # Startup is blocked inside the migration. For several seconds, no request gets an
    # answer: a single process is not listening yet; with workers, the parent holds the
    # socket but no worker serves it.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert server.process.poll() is None
        with pytest.raises(httpx.TransportError):
            httpx.get(f"{server.base_url}{PRODUCTS}", timeout=1)
    with blank_database.engine.connect() as connection:
        assert not table_exists(connection, "products")
    migration_lock.release()

    server.wait_until_serving()
    assert product_count(server) == 115


# ---------------------------------------------------------------------------
# Migration failure
# ---------------------------------------------------------------------------

def prepare_conflicting_table(engine):
    """A products table the migrations did not create (e.g. from another tool)."""
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE products (id uuid PRIMARY KEY, sku varchar(64))"))
        connection.execute(text("INSERT INTO products VALUES ('00000000-0000-7000-8000-000000000001', 'LEGACY-1')"))
    return 'relation "products" already exists'


def prepare_seed_conflict(engine):
    """Release 0001 with a product that reuses a sample's id: the seed (0002) must fail."""
    migrate(engine, "0001")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO products (id, sku, name, category, price_amount, price_currency, unit_of_measure,
                                  attributes, status, created_at, updated_at)
            VALUES (:id, 'USER-CLASH', 'Clash', 'MINE', 1, 'EUR', 'PIECE', '{}', 'ACTIVE', now(), now())
        """), {"id": SEEDS["DEMO-LAPTOP-001"]})
    return 'duplicate key value violates unique constraint "pk_products"'


@pytest.mark.postgres
@pytest.mark.parametrize("prepare", [prepare_conflicting_table, prepare_seed_conflict],
                         ids=["conflicting-table", "seed-conflict-after-0001"])
def test_failed_migration_stops_startup_and_changes_nothing(blank_database, app_servers, prepare):
    engine = blank_database.engine
    expected_error = prepare(engine)
    before_versions, before_rows = versions(engine), product_rows(engine)

    server = app_servers(blank_database.app_url())
    listened = False
    deadline = time.monotonic() + 60
    while server.process.poll() is None and time.monotonic() < deadline:
        listened = listened or server.accepts_connections()
        time.sleep(0.05)
    assert server.process.poll() is not None, f"startup did not stop after the failure:\n{server.output}"
    assert server.wait_for_exit() != 0
    assert not listened                            # it never accepted a connection
    assert expected_error in server.output
    assert "Application startup failed" in server.output
    # The whole upgrade rolled back: version and data are as before.
    assert versions(engine) == before_versions
    assert product_rows(engine) == before_rows

