from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from inventory_management_service.db import get_session
from inventory_management_service.main import PRODUCTS, app
from inventory_management_service.models import Product

pytestmark = pytest.mark.postgres


@pytest.fixture
def client(database):
    engine, _ = database

    def session():
        with Session(engine, expire_on_commit=False) as value:
            yield value

    app.dependency_overrides[get_session] = session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_persistence_and_duplicate(database, client, payload):
    engine, _ = database
    first = client.post(PRODUCTS, json=payload)
    assert first.status_code == 201, first.text
    data = first.json(parse_float=Decimal)
    duplicate = client.post(PRODUCTS, json=payload | {"sku": "LAP-DELL-1", "name": "Replacement"})
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.headers["content-type"] == "application/problem+json"
    assert duplicate.json()["type"] == "urn:inventra:problem:duplicate-sku"
    assert duplicate.json()["sku"] == "LAP-DELL-1"
    with Session(engine) as session:
        product = session.scalars(select(Product)).one()
        assert product.id == UUID(data["id"])
        assert product.name == "Dell XPS"
        assert product.price_amount == data["price"]["amount"]
        assert product.attributes == {"brand": "Dell"}
        assert product.version == data["version"] == 0
        assert product.created_at == product.updated_at
        assert product.deleted_at is None
    assert client.post(PRODUCTS, json=payload | {"sku": "new-sku"}).status_code == 201


def test_concurrent_create_has_one_winner(database, client, payload):
    engine, _ = database
    barrier = Barrier(2)

    def create(sku):
        barrier.wait(timeout=10)
        return client.post(PRODUCTS, json=payload | {"sku": sku})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create, ["lap-dell-1", " LAP-DELL-1 "]))
    assert sorted(response.status_code for response in responses) == [201, 409]
    with Session(engine) as session:
        assert len(session.scalars(select(Product)).all()) == 1


def test_migration_roundtrip(database):
    engine, config = database
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        connection.rollback()
        command.downgrade(config, "base")
        assert not inspect(connection).has_table("products")
        connection.rollback()
        command.upgrade(config, "head")
        assert inspect(connection).has_table("products")
