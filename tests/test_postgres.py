from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
from threading import Barrier
import time
from uuid import UUID, uuid7

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import Session

from inventory_management_service.db import get_session
from inventory_management_service.main import PRODUCTS, app
from inventory_management_service.models import Product

pytestmark = pytest.mark.postgres
SAMPLES = json.loads(
    (Path(__file__).resolve().parents[1] / "data" / "sample-products.json").read_text(encoding="utf-8"),
    parse_float=Decimal,
)


@pytest.fixture
def client(database, monkeypatch):
    engine, _ = database
    monkeypatch.setattr("inventory_management_service.migration.engine", lambda: engine)

    def session():
        with Session(engine, expire_on_commit=False) as value:
            yield value

    app.dependency_overrides[get_session] = session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def post_products(client, *products):
    """POST the product documents as the JSON array that the create endpoint expects."""
    return client.post(PRODUCTS, json=list(products))


def create_one(client, product):
    """Create one product and return its representation."""
    response = post_products(client, product)
    assert response.status_code == 201, response.text
    (created,) = response.json(parse_float=Decimal)
    return created


def test_persistence_and_duplicate(database, client, payload):
    engine, _ = database
    data = create_one(client, payload)
    duplicate = post_products(client, payload | {"sku": "LAP-DELL-1", "name": "Replacement"})
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.headers["content-type"] == "application/problem+json"
    assert duplicate.json()["type"] == "urn:inventra:problem:duplicate-sku"
    assert duplicate.json()["skus"] == ["LAP-DELL-1"]
    with Session(engine) as session:
        product = session.scalars(select(Product).where(Product.sku == "LAP-DELL-1")).one()
        assert product.id == UUID(data["id"])
        assert product.name == "Dell XPS"
        assert product.price_amount == data["price"]["amount"]
        assert product.attributes == {"brand": "Dell"}
        assert product.version == data["version"] == 0
        assert product.created_at == product.updated_at
        assert product.deleted_at is None
    assert post_products(client, payload | {"sku": "new-sku"}).status_code == 201


def test_concurrent_create_has_one_winner(database, client, payload):
    engine, _ = database
    barrier = Barrier(2)

    def create(skus):
        barrier.wait(timeout=10)
        return post_products(client, *(payload | {"sku": sku} for sku in skus))

    # Two batches that share one SKU, spelled differently.
    requests = [["lap-dell-1", "ONLY-FIRST"], ["ONLY-SECOND", " LAP-DELL-1 "]]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create, requests))
    assert sorted(response.status_code for response in responses) == [201, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["skus"] == ["LAP-DELL-1"]
    winner = next(response for response in responses if response.status_code == 201).json()
    with Session(engine) as session:
        stored = session.scalars(select(Product.sku).where(Product.sku.in_(
            ["LAP-DELL-1", "ONLY-FIRST", "ONLY-SECOND"]
        ))).all()
    # The whole winning batch, and nothing of the losing one.
    assert sorted(stored) == sorted(item["sku"] for item in winner)


# ---------------------------------------------------------------------------
# Create Products: batches
# ---------------------------------------------------------------------------

def test_batch_is_created_atomically_in_request_order(database, client, payload):
    engine, _ = database
    products = [
        payload | {"sku": "batch-c", "name": "Batch C"},
        payload | {"sku": "batch-a", "name": "Batch A", "price": {"amount": "0.0001", "currency": "USD"}},
        payload | {"sku": "batch-b", "name": "Batch B", "description": " Second ", "attributes": {}},
    ]
    response = post_products(client, *products)
    assert response.status_code == 201, response.text
    assert "location" not in response.headers
    created = response.json(parse_float=Decimal)
    assert [item["sku"] for item in created] == ["BATCH-C", "BATCH-A", "BATCH-B"]     # request order
    assert created[1]["price"] == {"amount": Decimal("0.0001"), "currency": "USD"}
    assert created[2]["description"] == "Second"

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT id, sku, created_at, updated_at, status, version, xmin::text AS tx "
            "FROM products WHERE sku LIKE 'BATCH-%' ORDER BY id"
        )).all()
    # Stored in one transaction, with one creation time, and ids in request order.
    assert [row.sku for row in rows] == ["BATCH-C", "BATCH-A", "BATCH-B"]
    assert [str(row.id) for row in rows] == [item["id"] for item in created]
    assert len({row.tx for row in rows}) == 1
    assert len({(row.created_at, row.updated_at) for row in rows}) == 1
    assert {(row.status, row.version) for row in rows} == {("ACTIVE", 0)}
    # Every product reads back exactly as the create response showed it ...
    for item in created:
        assert client.get(f"{PRODUCTS}/{item['id']}").json(parse_float=Decimal) == item
    # ... and sorting by createdAt (ties broken by id) keeps the request order.
    listed = client.get(PRODUCTS, params={"search": "batch", "sort": "createdAt"}).json(parse_float=Decimal)
    assert listed["items"] == created


@pytest.mark.parametrize("taken", ["seeded", "soft-deleted"])
def test_batch_with_a_taken_sku_creates_nothing(database, client, payload, taken):
    engine, _ = database
    if taken == "soft-deleted":
        # Soft-deleted products keep their SKUs reserved.
        assert delete_ids(client, ids_of(engine, "DEMO-LAPTOP-001", "DEMO-MOUSE-001")).status_code == 204
    before = rows_by_id(engine)
    response = post_products(client, *(payload | {"sku": sku} for sku in [
        "fresh-1", "demo-mouse-001", "fresh-2", " Demo-Laptop-001 ", "fresh-3",
    ]))
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"] == "urn:inventra:problem:duplicate-sku"
    # Every taken SKU, canonical and in request order, not only the first one the insert hit.
    assert body["skus"] == ["DEMO-MOUSE-001", "DEMO-LAPTOP-001"]
    assert body["detail"] == (
        "Products with these SKUs already exist: DEMO-MOUSE-001, DEMO-LAPTOP-001. No product was created."
    )
    assert rows_by_id(engine) == before             # not even the fresh SKUs were stored
    # The fresh SKUs are still free.
    assert post_products(client, *(payload | {"sku": f"fresh-{n}"} for n in (1, 2, 3))).status_code == 201


def test_batch_conflicting_with_a_product_created_by_an_earlier_batch(database, client, payload):
    engine, _ = database
    assert post_products(client, payload | {"sku": "A-1"}, payload | {"sku": "B-2"}).status_code == 201
    before = rows_by_id(engine)
    response = post_products(client, payload | {"sku": "C-3"}, payload | {"sku": "b-2"})
    assert (response.status_code, response.json()["skus"]) == (409, ["B-2"])
    assert rows_by_id(engine) == before


@pytest.mark.parametrize(("raw", "kind", "detail"), [
    ("", "malformed-request", "The request body is missing."),
    ("null", "malformed-request", "The request body is null."),
    ("[]", "validation-error", "The product list is empty."),
    ("{", "malformed-request", "The request body is not valid JSON."),
    ('"products"', "malformed-request", "The request body must be a JSON array of products, not a JSON string."),
    ("SINGLE", "malformed-request", "The request body must be a JSON array of products, not a single product object."),
    ("NULL-ELEMENT", "malformed-request", "The request contains 1 invalid field(s) in 1 product(s)."),
    ("INVALID-ELEMENT", "validation-error", "The request contains 1 invalid field(s) in 1 product(s)."),
    ("REPEATED-SKU", "validation-error", "The request contains 1 invalid field(s) in 1 product(s)."),
])
def test_rejected_create_requests_store_nothing(database, client, payload, raw, kind, detail):
    engine, _ = database
    valid = [payload | {"sku": f"REJECTED-{n}"} for n in range(3)]
    raw = {
        "SINGLE": json.dumps(payload),
        "NULL-ELEMENT": json.dumps([valid[0], None, valid[2]]),
        "INVALID-ELEMENT": json.dumps([valid[0], valid[1] | {"price": {"amount": -1, "currency": "EUR"}}, valid[2]]),
        "REPEATED-SKU": json.dumps([valid[0], valid[1], valid[2] | {"sku": "rejected-0"}]),
    }.get(raw, raw)
    before = rows_by_id(engine)
    response = client.post(PRODUCTS, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["type"] == f"urn:inventra:problem:{kind}"
    assert body["detail"].startswith(detail)
    assert rows_by_id(engine) == before


def varied_products(payload, count, prefix):
    """`count` distinct products whose fields vary: text that COPY must escape, optional
    descriptions, every unit, prices with and without fractions, and attributes."""
    units = ["PIECE", "BOX", "PACK", "PALLET", "GRAM", "KILOGRAM", "MILLILITER", "LITER",
             "CENTIMETER", "METER", "SQUARE_METER", "CUBIC_METER"]
    return [
        payload | {
            "sku": f"{prefix}-{n}",
            "name": f"Product {n} \t tab \\ backslash \n line 'quote' \"double\" Käse 🧀",
            "description": None if n % 3 == 0 else f"Description {n}\r\n",
            "price": {"amount": f"{n}.{n % 10000:04}" if n % 2 else n, "currency": "USD" if n % 5 == 0 else "EUR"},
            "unitOfMeasure": units[n % len(units)],
            "attributes": {} if n % 4 == 0 else {"index": str(n), "note": "\\N and \t"},
        }
        for n in range(count)
    ]


def test_ten_thousand_products_are_created_in_one_request(database, client, payload):
    engine, _ = database
    products = varied_products(payload, 10_000, "BULK")
    response = post_products(client, *products)
    assert response.status_code == 201, response.text
    created = response.json(parse_float=Decimal)
    assert [item["sku"] for item in created] == [f"BULK-{n}" for n in range(10_000)]
    with engine.connect() as connection:
        rows = {row.sku: row for row in connection.execute(text(
            "SELECT *, xmin::text AS tx FROM products WHERE sku LIKE 'BULK-%'"
        ))}
    assert len(rows) == 10_000
    assert len({row.tx for row in rows.values()}) == 1            # one transaction
    assert len({row.created_at for row in rows.values()}) == 1    # one creation time
    # Every stored value matches the request exactly, including text that COPY escapes.
    for product, item in zip(products, created):
        row = rows[item["sku"]]
        assert str(row.id) == item["id"]
        assert (row.name, row.description) == (product["name"], (product["description"] or "").strip() or None)
        assert row.price_amount == Decimal(str(product["price"]["amount"]))
        assert (row.price_currency, row.unit_of_measure) == (product["price"]["currency"], product["unitOfMeasure"])
        assert row.attributes == product["attributes"]
        assert (row.status, row.version, row.deleted_at) == ("ACTIVE", 0, None)
    # Get and List show each product exactly as the create response did.
    for item in created[::997]:
        assert client.get(f"{PRODUCTS}/{item['id']}").json(parse_float=Decimal) == item
    for page in (1, 37, 100):
        listed = client.get(PRODUCTS, params={"search": "BULK-", "sort": "createdAt", "size": 100, "page": page})
        body = listed.json(parse_float=Decimal)
        assert body["totalItems"] == 10_000
        assert body["items"] == created[(page - 1) * 100:page * 100]      # request order


def test_a_large_request_with_one_taken_sku_creates_nothing(database, client, payload):
    engine, _ = database
    before = rows_by_id(engine)
    products = varied_products(payload, 5_000, "BULK")
    products[4_321] = products[4_321] | {"sku": "demo-mouse-001"}
    response = post_products(client, *products)
    assert (response.status_code, response.json()["skus"]) == (409, ["DEMO-MOUSE-001"])
    assert rows_by_id(engine) == before


def test_a_large_request_with_an_invalid_product_creates_nothing(database, client, payload):
    engine, _ = database
    before = rows_by_id(engine)
    products = varied_products(payload, 5_000, "BULK")
    products[-1] = products[-1] | {"unitOfMeasure": "BARREL"}
    response = post_products(client, *products)
    assert response.status_code == 400, response.text
    assert [item["field"] for item in response.json()["errors"]] == ["4999.unitOfMeasure"]
    assert rows_by_id(engine) == before


def test_concurrent_disjoint_batches_all_succeed(database, client, payload):
    engine, _ = database
    requests, size = 8, 25
    barrier = Barrier(requests)

    def create(request):
        barrier.wait(timeout=10)
        return post_products(client, *(payload | {"sku": f"PAR-{request}-{n}"} for n in range(size)))

    with ThreadPoolExecutor(max_workers=requests) as executor:
        responses = list(executor.map(create, range(requests)))
    assert [response.status_code for response in responses] == [201] * requests
    with engine.connect() as connection:
        stored = connection.scalar(text("SELECT count(DISTINCT sku) FROM products WHERE sku LIKE 'PAR-%'"))
    assert stored == requests * size


@pytest.mark.parametrize("isolation", [None, "repeatable read", "serializable"],
                         ids=["server-default", "repeatable-read", "serializable"])
@pytest.mark.parametrize("other_transaction", ["commits", "rolls-back"])
def test_batch_waits_for_a_concurrent_insert_of_its_sku(database, client, payload, isolation, other_transaction):
    """Another transaction has inserted, but not committed, one SKU of the batch. The
    insert waits for it. If it commits, the request reports that SKU as taken (409,
    never a 500) and stores nothing; if it rolls back, the whole batch is created."""
    engine, _ = database
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    settings = [f"search_path={schema}"] + ([f"default_transaction_isolation={isolation}"] if isolation else [])
    name = "inventra-create-waiter"
    waiter = create_engine(engine.url, connect_args={
        "options": " ".join("-c" + setting.replace(" ", "\\ ") for setting in settings),
        "application_name": name,
    })

    def session():
        with Session(waiter, expire_on_commit=False) as value:
            yield value

    app.dependency_overrides[get_session] = session
    batch = [payload | {"sku": sku} for sku in ("WAIT-1", "race-1", "WAIT-2")]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor, engine.connect() as other:
            other.execute(text("""
                INSERT INTO products (id, sku, name, category, price_amount, price_currency, unit_of_measure,
                                      attributes, status, created_at, updated_at)
                VALUES (gen_random_uuid(), 'RACE-1', 'Racing product', 'RACE', 1, 'EUR', 'PIECE', '{}',
                        'ACTIVE', now(), now())
            """))
            pending = executor.submit(post_products, client, *batch)
            wait_until_waiting_for_a_lock(engine, name)
            if other_transaction == "commits":
                other.commit()
            else:
                other.rollback()
            response = pending.result(timeout=30)
    finally:
        waiter.dispose()

    with engine.connect() as connection:
        stored = dict(connection.execute(text(
            "SELECT sku, name FROM products WHERE sku IN ('WAIT-1', 'RACE-1', 'WAIT-2')"
        )).all())
    if other_transaction == "commits":
        assert response.status_code == 409, response.text
        assert response.json()["skus"] == ["RACE-1"]
        assert stored == {"RACE-1": "Racing product"}          # the request stored nothing
    else:
        assert response.status_code == 201, response.text
        assert [item["sku"] for item in response.json()] == ["WAIT-1", "RACE-1", "WAIT-2"]
        assert stored == dict.fromkeys(["WAIT-1", "RACE-1", "WAIT-2"], "Dell XPS")


def test_migration_roundtrip(database):
    engine, config = database
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        connection.rollback()
        command.downgrade(config, "base")
        assert not inspect(connection).has_table("products")
        connection.rollback()
        command.upgrade(config, "head")
        assert inspect(connection).has_table("products")


def test_seed_migration_preserves_existing_products(database):
    engine, config = database
    products = json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "sample-products.json").read_text(encoding="utf-8"),
        parse_float=Decimal,
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        rows = connection.execute(select(Product.__table__)).mappings().all()
        assert len(rows) == len(products) == 115
        expected = {product["sku"]: product for product in products}
        for row in rows:
            source = expected[row["sku"]]
            assert row["name"] == source["name"]
            assert row["price_amount"] == source["price"]["amount"]
            assert row["attributes"] == source["attributes"]
            assert row["id"].version == 7
            assert row["version"] == 0
        original_ids = {row["sku"]: row["id"] for row in rows}
        connection.execute(text("UPDATE products SET name = 'Edited by user' WHERE sku = 'DEMO-LAPTOP-001'"))
        connection.commit()
        command.downgrade(config, "0001")
        command.upgrade(config, "head")
        preserved = connection.execute(select(Product.sku, Product.id)).all()
        assert dict(preserved) == original_ids
        assert connection.scalar(text("SELECT name FROM products WHERE sku = 'DEMO-LAPTOP-001'")) == "Edited by user"


def get_all(client, **params):
    """Walk every page of a listing and return the items plus the first page envelope."""
    first = client.get(PRODUCTS, params=params | {"page": 1})
    assert first.status_code == 200, first.text
    envelope = first.json(parse_float=Decimal)
    items = list(envelope["items"])
    for page in range(2, envelope["totalPages"] + 1):
        response = client.get(PRODUCTS, params=params | {"page": page})
        assert response.status_code == 200, response.text
        assert response.json()["totalItems"] == envelope["totalItems"]
        items += response.json(parse_float=Decimal)["items"]
    return items, envelope


def skus(items):
    return [item["sku"] for item in items]


def matching_samples(search="", category=None, low=None, high=None):
    def matches(product):
        text_ = " ".join([product["sku"], product["name"], product.get("description") or ""]).lower()
        return (
            all(term.lower() in text_ for term in search.split())
            and (category is None or product["category"].lower() == category.lower())
            and (low is None or product["price"]["amount"] >= low)
            and (high is None or product["price"]["amount"] <= high)
        )

    return {product["sku"] for product in SAMPLES if matches(product)}


def test_get_by_id_returns_created_product(database, client, payload):
    engine, _ = database
    created = create_one(client, payload)
    fetched = client.get(f"{PRODUCTS}/{created['id']}")
    assert fetched.status_code == 200, fetched.text
    # Stored values, including the 19-digit price and microsecond timestamps, round-trip exactly.
    data = fetched.json(parse_float=Decimal)
    assert data == created
    with engine.connect() as connection:
        stored_id = connection.scalar(text("SELECT id FROM products WHERE sku = 'LAP-DELL-1'"))
    assert data["id"] == str(stored_id)


def test_get_seeded_product(database, client):
    engine, _ = database
    with engine.connect() as connection:
        product_id = connection.scalar(text("SELECT id FROM products WHERE sku = 'DEMO-MONITOR-001'"))
    response = client.get(f"{PRODUCTS}/{product_id}")
    assert response.status_code == 200, response.text
    data = response.json(parse_float=Decimal)
    source = next(product for product in SAMPLES if product["sku"] == "DEMO-MONITOR-001")
    assert data["id"] == str(product_id)
    assert data["price"] == source["price"]
    # A JSON number literal without insignificant zeros (numeric(19,4) stores 249.0000).
    amount = json.loads(response.text, parse_int=lambda lexeme: ("number", lexeme))["price"]["amount"]
    assert amount == ("number", "249")
    assert data["attributes"] == source["attributes"]
    assert (data["status"], data["version"]) == ("ACTIVE", 0)
    assert client.get(f"{PRODUCTS}/{UUID(int=0)}").status_code == 404


def test_soft_deleted_products_are_hidden_and_preserved(database, client):
    engine, _ = database
    with engine.begin() as connection:
        deleted_id = connection.scalar(text("""
            UPDATE products SET status = 'DELETED', deleted_at = created_at
            WHERE sku = 'DEMO-LAPTOP-001' RETURNING id
        """))
        inactive_id = connection.scalar(text("""
            UPDATE products SET status = 'INACTIVE' WHERE sku = 'DEMO-MOUSE-001' RETURNING id
        """))

    missing = client.get(f"{PRODUCTS}/{deleted_id}")
    assert missing.status_code == 404
    assert missing.json()["type"] == "urn:inventra:problem:product-not-found"
    assert client.get(PRODUCTS, params={"search": "DEMO-LAPTOP-001"}).json()["totalItems"] == 0
    items, envelope = get_all(client, size=100)
    assert envelope["totalItems"] == len(items) == 114
    assert "DEMO-LAPTOP-001" not in skus(items)

    # Soft deletion only hides the row. Other non-deleted statuses remain visible.
    inactive = client.get(f"{PRODUCTS}/{inactive_id}")
    assert inactive.status_code == 200, inactive.text
    assert inactive.json()["status"] == "INACTIVE"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM products")) == 115
    # The SKU stays reserved by the deleted row.
    assert post_products(client, {
        "sku": "demo-laptop-001", "name": "Replacement", "category": "ELECTRONICS",
        "price": {"amount": 1, "currency": "EUR"}, "unitOfMeasure": "PIECE", "attributes": {},
    }).status_code == 409


SORT_SQL = {
    "name": "name", "sku": "sku", "category": "category", "price": "price_amount",
    "createdAt": "created_at", "updatedAt": "updated_at",
}


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("sort", list(SORT_SQL))
def test_pagination_is_stable_for_every_sort(database, client, sort, direction):
    engine, _ = database
    with engine.connect() as connection:
        # Every seeded row shares one created_at, so the tiebreaker decides the createdAt order.
        assert connection.scalar(text("SELECT count(DISTINCT created_at) FROM products")) == 1
        before = connection.execute(text("SELECT * FROM products ORDER BY id")).all()
        column = SORT_SQL[sort]
        expected = [row.sku for row in connection.execute(text(
            f"SELECT sku FROM products ORDER BY {column} {direction}, id {direction}"
        ))]

    items, envelope = get_all(client, size=7, sort=sort, direction=direction)
    assert (envelope["totalItems"], envelope["totalPages"]) == (115, 17)
    assert skus(items) == expected
    assert len(set(skus(items))) == 115
    again, _ = get_all(client, size=7, sort=sort, direction=direction)
    assert skus(again) == expected

    past_end = client.get(PRODUCTS, params={"size": 7, "page": 18, "sort": sort}).json()
    assert (past_end["items"], past_end["totalItems"]) == ([], 115)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM products ORDER BY id")).all() == before


def test_price_and_created_at_sorting_semantics(client):
    items, _ = get_all(client, size=50, sort="price", direction="desc")
    keys = [(item["price"]["amount"], UUID(item["id"])) for item in items]
    assert keys == sorted(keys, reverse=True)
    assert keys[0][0] == max(product["price"]["amount"] for product in SAMPLES)

    items, _ = get_all(client, size=50, sort="createdAt")
    assert [UUID(item["id"]) for item in items] == sorted(UUID(item["id"]) for item in items)


@pytest.mark.parametrize("search", [
    "ring binder blue", "RING   binder", "demo-laptop", "ergonomic", "wireless mouse", "black", "no-such-product",
])
def test_search(client, search):
    items, envelope = get_all(client, search=search, size=10)
    expected = matching_samples(search)
    assert set(skus(items)) == expected
    assert envelope["totalItems"] == len(expected)


def test_search_treats_wildcards_literally(client, payload):
    created = post_products(client, *(
        payload | {"sku": sku, "name": name}
        for sku, name in [("TOWEL-1", "100% Cotton Towel"), ("TEA-1", "Tea_Bags"), ("PATH-1", "C:\\Temp")]
    ))
    assert created.status_code == 201, created.text
    assert skus(client.get(PRODUCTS, params={"search": "%"}).json()["items"]) == ["TOWEL-1"]
    assert skus(client.get(PRODUCTS, params={"search": "_"}).json()["items"]) == ["TEA-1"]
    assert skus(client.get(PRODUCTS, params={"search": "\\"}).json()["items"]) == ["PATH-1"]
    assert skus(client.get(PRODUCTS, params={"search": "'; DROP TABLE products; --"}).json()["items"]) == []


@pytest.mark.parametrize("category", ["ELECTRONICS", "electronics", " Office_Supplies ", "KITCHEN", "UNKNOWN"])
def test_category_filter(client, category):
    items, envelope = get_all(client, category=category, size=100)
    expected = matching_samples(category=category.strip())
    assert set(skus(items)) == expected
    assert envelope["totalItems"] == len(expected)
    assert {item["category"] for item in items} <= {category.strip().upper()}


@pytest.mark.parametrize(("low", "high"), [
    ("24.9", "24.90"), ("10", "50"), ("0", "9.99"), ("500", None), (None, "5"), ("1000000", None),
])
def test_price_filters_are_inclusive(client, low, high):
    params = {key: value for key, value in {"minPrice": low, "maxPrice": high}.items() if value is not None}
    items, envelope = get_all(client, size=100, **params)
    expected = matching_samples(low=low and Decimal(low), high=high and Decimal(high))
    assert set(skus(items)) == expected
    assert envelope["totalItems"] == len(expected)
    if low == high == "24.9":
        assert "DEMO-MOUSE-001" in expected


def test_combined_filters_and_sort(client):
    params = {"search": "black", "category": "electronics", "minPrice": "20", "maxPrice": "100",
              "sort": "price", "direction": "desc"}
    items, envelope = get_all(client, size=2, **params)
    expected = matching_samples("black", "ELECTRONICS", Decimal(20), Decimal(100))
    assert expected and set(skus(items)) == expected
    assert envelope["totalItems"] == len(expected)
    prices = [item["price"]["amount"] for item in items]
    assert prices == sorted(prices, reverse=True)


def test_currency_filter(client, payload):
    create_one(client, payload | {"sku": "usd-1", "price": {"amount": "10", "currency": "USD"}})
    usd = client.get(PRODUCTS, params={"currency": "USD"}).json()
    assert skus(usd["items"]) == ["USD-1"]
    assert client.get(PRODUCTS, params={"currency": "EUR", "size": 1}).json()["totalItems"] == 115
    # Price bounds alone compare amounts across currencies.
    assert "USD-1" in skus(client.get(PRODUCTS, params={"minPrice": 10, "maxPrice": 10}).json()["items"])
    assert client.get(PRODUCTS, params={"minPrice": 10, "maxPrice": 10, "currency": "EUR"}).json()["totalItems"] == 0


def test_timestamps_are_utc_in_any_session_time_zone(database, client, payload):
    engine, _ = database
    created = post_products(client, payload)
    assert created.status_code == 201, created.text
    (created,) = created.json()
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    local = create_engine(
        engine.url, connect_args={"options": f"-c search_path={schema} -c timezone=America/New_York"},
    )

    def session():
        with Session(local, expire_on_commit=False) as value:
            yield value

    app.dependency_overrides[get_session] = session
    try:
        fetched = client.get(f"{PRODUCTS}/{created['id']}").json()
        listed = client.get(PRODUCTS, params={"search": "LAP-DELL-1"}).json()["items"]
    finally:
        local.dispose()
    assert fetched["createdAt"] == created["createdAt"]
    assert fetched["createdAt"].endswith("Z")
    assert listed == [fetched]


def test_every_seeded_product_is_served_with_its_id(database, client):
    # Regression test for the report that GET responses lacked "id".
    engine, _ = database
    with engine.connect() as connection:
        stored = {str(row.id): row.sku for row in connection.execute(text("SELECT id, sku FROM products"))}
    items, _ = get_all(client, size=100)
    assert {item["id"]: item["sku"] for item in items} == stored
    for product_id, sku in stored.items():
        data = client.get(f"{PRODUCTS}/{product_id}").json()
        assert (data["id"], data["sku"]) == (product_id, sku)


def test_running_server_serves_ids_and_this_codes_openapi(database, app_servers):
    """Start the application the way users do (uvicorn in its own process) and call it
    over HTTP. Startup migrations run against the test schema, which is already at head."""
    engine, _ = database
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    server = app_servers(url.render_as_string(hide_password=False))
    server.wait_until_serving()
    with httpx.Client(base_url=server.base_url, timeout=10) as http:
        # The process serves the contract of the code under test.
        assert http.get("/openapi.json").json() == app.openapi()
        listed = http.get(PRODUCTS, params={"size": 5}).json(parse_float=Decimal)
        assert listed["totalItems"] == 115
        for item in listed["items"]:
            fetched = http.get(f"{PRODUCTS}/{item['id']}")
            assert fetched.status_code == 200, fetched.text
            assert fetched.json(parse_float=Decimal) == item


# ---------------------------------------------------------------------------
# Offset pagination while the data changes
#
# What offset pagination guarantees: each response is computed afresh as the slice
# [(page - 1) * size, page * size) of the current ordering of the products that are
# not soft-deleted, and its totals describe that same state. Between requests nothing
# is pinned, so changes before the current position make later pages repeat or omit
# products. These tests pin down both the guarantee and the exact anomalies.
# ---------------------------------------------------------------------------

NAME_ORDER = "SELECT sku FROM products WHERE deleted_at IS NULL ORDER BY name, id"


def name_order(engine):
    """The current ordering for sort=name, computed independently of the API."""
    with engine.connect() as connection:
        return connection.scalars(text(NAME_ORDER)).all()


def page_of(client, page, size=10, **params):
    response = client.get(PRODUCTS, params={"page": page, "size": size, "sort": "name"} | params)
    assert response.status_code == 200, response.text
    body = response.json()
    return skus(body["items"]), body


def create_named(client, payload, sku, name):
    assert post_products(client, payload | {"sku": sku, "name": name}).status_code == 201


def soft_delete(engine, sku):
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE products SET status = 'DELETED', deleted_at = updated_at WHERE sku = :sku"
        ), {"sku": sku})


def update(engine, sku, **values):
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE products SET {assignments}, version = version + 1, "
            "updated_at = updated_at + interval '1 second' WHERE sku = :sku"
        ), {"sku": sku, **values})


# Each scenario: a change made between the page 1 and page 2 requests (size 10,
# sort=name), and the page 2 that results, in terms of the original order `o`.
PAGE_CHANGES = {
    "create-sorting-before": (
        lambda engine, client, payload, o: create_named(client, payload, "NEW-1", "000 Early product"),
        lambda o: [o[9]] + o[10:19],                  # the last item of page 1 repeats
    ),
    "three-creates-sorting-before": (
        lambda engine, client, payload, o: [
            create_named(client, payload, f"NEW-{n}", f"000 Early product {n}") for n in range(3)
        ],
        lambda o: o[7:10] + o[10:17],                 # the shift equals the number of changes
    ),
    "create-sorting-after": (
        lambda engine, client, payload, o: create_named(client, payload, "NEW-1", "zzz Late product"),
        lambda o: o[10:20],                           # no effect on the next page
    ),
    "soft-delete-seen": (
        lambda engine, client, payload, o: soft_delete(engine, o[0]),
        lambda o: o[11:21],                           # o[10] is never shown
    ),
    "soft-delete-not-yet-seen": (
        lambda engine, client, payload, o: soft_delete(engine, o[14]),
        lambda o: o[10:14] + o[15:21],                # it disappears; nothing else is affected
    ),
    "update-moves-seen-product-later": (
        lambda engine, client, payload, o: update(engine, o[0], name="zzz Renamed"),
        lambda o: o[11:21],                           # o[10] is skipped; o[0] will be shown again
    ),
    "update-moves-unseen-product-earlier": (
        lambda engine, client, payload, o: update(engine, o[12], name="000 Renamed"),
        lambda o: [o[9]] + o[10:12] + o[13:20],       # o[9] repeats; o[12] is never shown
    ),
    "update-without-sort-key-change": (
        lambda engine, client, payload, o: [
            update(engine, sku, description="Changed") for sku in (o[0], o[12])
        ],
        lambda o: o[10:20],                           # no effect
    ),
}


@pytest.mark.parametrize("scenario", list(PAGE_CHANGES))
def test_changes_between_page_requests(database, client, payload, scenario):
    engine, _ = database
    change, expected_second_page = PAGE_CHANGES[scenario]
    original = name_order(engine)
    first, _ = page_of(client, 1)
    assert first == original[0:10]

    change(engine, client, payload, original)
    second, body = page_of(client, 2)

    # The guarantee: the slice of the current ordering, with totals for the current state.
    current = name_order(engine)
    assert second == current[10:20]
    assert (body["totalItems"], body["totalPages"]) == (len(current), -(-len(current) // 10))
    # The consequence for a client paging through: the exact repeat or omission.
    assert second == expected_second_page(original)


def test_filter_membership_change_between_requests(database, client):
    """A product that stops matching a filter behaves like a deletion for later pages."""
    engine, _ = database
    params = {"maxPrice": "30", "sort": "price", "size": 5}
    order_sql = ("SELECT sku FROM products WHERE deleted_at IS NULL AND price_amount <= 30 "
                 "ORDER BY price_amount, id")
    with engine.connect() as connection:
        original = connection.scalars(text(order_sql)).all()
    first = skus(client.get(PRODUCTS, params=params | {"page": 1}).json()["items"])
    assert first == original[0:5]

    update(engine, original[0], price_amount=Decimal("99"))          # no longer matches maxPrice=30
    second = client.get(PRODUCTS, params=params | {"page": 2}).json()
    with engine.connect() as connection:
        current = connection.scalars(text(order_sql)).all()
    assert skus(second["items"]) == current[5:10] == original[6:11]  # original[5] is never shown
    assert second["totalItems"] == len(original) - 1


def test_every_page_reflects_the_data_at_request_time(database, client, payload):
    """Walk all pages while something changes before every request."""
    engine, _ = database
    changes = [
        lambda o: create_named(client, payload, "WALK-1", "000 Walk early"),
        lambda o: soft_delete(engine, o[3]),
        lambda o: update(engine, o[40], name="000 Walk renamed"),
        lambda o: create_named(client, payload, "WALK-2", "zzz Walk late"),
        lambda o: soft_delete(engine, o[-1]),
        lambda o: update(engine, o[5], description="Walk"),
        lambda o: soft_delete(engine, o[60]),
    ]
    page, shown = 1, []
    while True:
        current = name_order(engine)
        items, body = page_of(client, page, size=12)
        assert items == current[(page - 1) * 12:page * 12]
        assert body["totalItems"] == len(current)
        assert body["totalPages"] == -(-len(current) // 12)
        shown += items
        if page >= body["totalPages"]:
            break
        if page <= len(changes):
            changes[page - 1](name_order(engine))
        page += 1
    # Across the walk, products were repeated and omitted, which is why a client that
    # needs a complete pass must not change the data, or must tolerate both.
    final = name_order(engine)
    repeated = {sku for sku in shown if shown.count(sku) > 1}
    omitted = set(final) - set(shown)
    assert repeated and omitted


# ---------------------------------------------------------------------------
# Within one request: the count and the page come from one snapshot
# ---------------------------------------------------------------------------

def commit_before_page_query(engine, change):
    """Make `change` commit from another connection just before the next page query."""
    fired = []

    def before_cursor_execute(connection, cursor, statement, parameters, context, executemany):
        if not fired and "FROM products" in statement and "LIMIT" in statement:
            fired.append(True)          # set first: the change below also runs statements
            with engine.begin() as other:
                change(other)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    return fired, lambda: event.remove(engine, "before_cursor_execute", before_cursor_execute)


SNAPSHOT_CHANGES = {
    "create": lambda connection: connection.execute(text("""
        INSERT INTO products (id, sku, name, category, price_amount, price_currency, unit_of_measure,
                              attributes, status, created_at, updated_at)
        VALUES (gen_random_uuid(), 'RACE-1', '000 Racing product', 'RACE', 1, 'EUR', 'PIECE', '{}',
                'ACTIVE', now(), now())
    """)),
    "soft-delete": lambda connection: connection.execute(text("""
        UPDATE products SET status = 'DELETED', deleted_at = updated_at
        WHERE id = (SELECT id FROM products ORDER BY name, id LIMIT 1)
    """)),
}


@pytest.mark.parametrize("scenario", list(SNAPSHOT_CHANGES))
def test_count_and_page_come_from_one_snapshot(database, client, scenario):
    engine, _ = database
    before = name_order(engine)
    fired, remove = commit_before_page_query(engine, SNAPSHOT_CHANGES[scenario])
    try:
        items, body = page_of(client, 2, size=100)
    finally:
        remove()
    assert fired, "the change was not injected during the request"
    after = name_order(engine)
    assert len(after) != len(before)
    # The change committed between the two queries. The response must describe one state,
    # either before or after the change, never a count from one and items from the other.
    assert (body["totalItems"], items) in [(len(before), before[100:200]), (len(after), after[100:200])]
    assert len(items) == body["totalItems"] - 100
    # The next request sees the change.
    assert page_of(client, 2, size=100)[1]["totalItems"] == len(after)


# ---------------------------------------------------------------------------
# Soft-delete Products
# ---------------------------------------------------------------------------

def delete_ids(client, product_ids):
    return client.request("DELETE", PRODUCTS, json={"productIds": [str(product_id) for product_id in product_ids]})


def ids_of(engine, *sku_list):
    """The stored ids of the given SKUs, in the same order."""
    with engine.connect() as connection:
        stored = dict(connection.execute(
            text("SELECT sku, id FROM products WHERE sku = ANY(:skus)"), {"skus": list(sku_list)},
        ).all())
    return [stored[sku] for sku in sku_list]


def rows_by_id(engine):
    """Every stored product row, complete, keyed by id."""
    with engine.connect() as connection:
        return {row.id: row._asdict() for row in connection.execute(text("SELECT * FROM products"))}


CHANGED_BY_DELETE = {"status", "deleted_at", "updated_at", "version"}


def test_delete_marks_products_deleted_and_keeps_them_stored(database, client):
    engine, _ = database
    laptop, mouse = ids_of(engine, "DEMO-LAPTOP-001", "DEMO-MOUSE-001")
    before = rows_by_id(engine)

    response = delete_ids(client, [mouse, laptop])
    assert response.status_code == 204, response.text
    assert response.content == b""

    after = rows_by_id(engine)
    assert after.keys() == before.keys()                  # no row was physically deleted
    for product_id in (laptop, mouse):
        old, new = before[product_id], after[product_id]
        assert new["status"] == "DELETED"
        assert new["deleted_at"] == new["updated_at"] > old["updated_at"]
        assert new["version"] == old["version"] + 1
        assert {key: new[key] for key in new.keys() - CHANGED_BY_DELETE} == \
            {key: old[key] for key in old.keys() - CHANGED_BY_DELETE}
    assert after[laptop]["deleted_at"] == after[mouse]["deleted_at"]   # one time for the batch
    others = before.keys() - {laptop, mouse}
    assert {key: after[key] for key in others} == {key: before[key] for key in others}

    # Get and List no longer return the deleted products.
    for product_id in (laptop, mouse):
        missing = client.get(f"{PRODUCTS}/{product_id}")
        assert missing.status_code == 404
        assert missing.json()["type"] == "urn:inventra:problem:product-not-found"
    items, envelope = get_all(client, size=100)
    assert envelope["totalItems"] == len(items) == 113
    assert {"DEMO-LAPTOP-001", "DEMO-MOUSE-001"}.isdisjoint(skus(items))


def test_created_product_can_be_deleted(database, client, payload):
    engine, _ = database
    created = create_one(client, payload)
    assert delete_ids(client, [created["id"]]).status_code == 204
    assert client.get(f"{PRODUCTS}/{created['id']}").status_code == 404
    with engine.connect() as connection:
        row = connection.execute(text("SELECT * FROM products WHERE sku = 'LAP-DELL-1'")).one()
    assert (row.status, row.version) == ("DELETED", 1)
    assert row.deleted_at == row.updated_at >= row.created_at


@pytest.mark.parametrize("absent", ["never-existed", "already-deleted"])
def test_delete_is_all_or_nothing(database, client, absent):
    engine, _ = database
    laptop, mouse, keyboard = ids_of(engine, "DEMO-LAPTOP-001", "DEMO-MOUSE-001", "DEMO-KEYBOARD-001")
    if absent == "already-deleted":
        assert delete_ids(client, [keyboard]).status_code == 204
        gone = keyboard
    else:
        gone = uuid7()
    before = rows_by_id(engine)

    response = delete_ids(client, [laptop, gone, mouse])
    assert response.status_code == 404, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "urn:inventra:problem:products-not-found"
    assert body["productIds"] == [str(gone)]
    # Nothing changed, not even the listed products that exist.
    assert rows_by_id(engine) == before
    assert [client.get(f"{PRODUCTS}/{product_id}").status_code for product_id in (laptop, mouse)] == [200, 200]


def test_deleting_a_deleted_product_again_is_not_found(database, client):
    engine, _ = database
    (laptop,) = ids_of(engine, "DEMO-LAPTOP-001")
    assert delete_ids(client, [laptop]).status_code == 204
    deleted = rows_by_id(engine)
    again = delete_ids(client, [laptop])
    assert again.status_code == 404
    assert again.json()["productIds"] == [str(laptop)]
    assert rows_by_id(engine) == deleted        # the first deletion's time and version are kept


@pytest.mark.parametrize(("raw", "kind"), [
    ("", "malformed-request"),
    ("null", "malformed-request"),
    ("{}", "validation-error"),
    ('{"productIds": []}', "validation-error"),
    ('{"productIds": null}', "malformed-request"),
], ids=["no-body", "null-body", "no-productIds", "empty-productIds", "null-productIds"])
def test_delete_without_product_ids_is_a_bad_request(database, client, raw, kind):
    engine, _ = database
    before = rows_by_id(engine)
    response = client.request("DELETE", PRODUCTS, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400, response.text
    assert response.json()["type"] == f"urn:inventra:problem:{kind}"
    assert rows_by_id(engine) == before


def test_delete_of_a_row_written_by_a_clock_that_runs_ahead(database, client):
    """Another server whose clock runs ahead created the row. Deleting it at this server's
    earlier time would violate ck_products_deleted_at and ck_products_timestamps."""
    engine, _ = database
    with engine.begin() as connection:
        laptop, future = connection.execute(text("""
            UPDATE products SET created_at = now() + interval '1 hour', updated_at = now() + interval '1 hour'
            WHERE sku = 'DEMO-LAPTOP-001' RETURNING id, updated_at
        """)).one()
    assert delete_ids(client, [laptop]).status_code == 204
    row = rows_by_id(engine)[laptop]
    assert (row["status"], row["deleted_at"], row["updated_at"]) == ("DELETED", future, future)


def test_deleted_products_keep_their_sku(database, client, payload):
    engine, _ = database
    (laptop,) = ids_of(engine, "DEMO-LAPTOP-001")
    assert delete_ids(client, [laptop]).status_code == 204
    conflict = post_products(client, payload | {"sku": "demo-laptop-001"})
    assert conflict.status_code == 409
    assert conflict.json()["skus"] == ["DEMO-LAPTOP-001"]


def test_reads_show_the_stored_status_until_a_product_is_deleted(database, client):
    engine, _ = database
    with engine.begin() as connection:
        connection.execute(text("UPDATE products SET status = 'INACTIVE' WHERE sku = 'DEMO-MOUSE-001'"))
        connection.execute(text("UPDATE products SET status = 'DISCONTINUED' WHERE sku = 'DEMO-KEYBOARD-001'"))
    expected = {"DEMO-LAPTOP-001": "ACTIVE", "DEMO-MOUSE-001": "INACTIVE", "DEMO-KEYBOARD-001": "DISCONTINUED"}
    ids = dict(zip(expected, ids_of(engine, *expected)))

    for sku, status in expected.items():
        assert client.get(f"{PRODUCTS}/{ids[sku]}").json()["status"] == status
    items, _ = get_all(client, size=100)
    shown = {item["sku"]: item["status"] for item in items}
    assert {sku: shown[sku] for sku in expected} == expected
    assert {status for sku, status in shown.items() if sku not in expected} == {"ACTIVE"}

    # Products with any status that is not DELETED can be soft-deleted.
    assert delete_ids(client, list(ids.values())).status_code == 204
    with engine.connect() as connection:
        stored = dict(connection.execute(text(
            "SELECT sku, status FROM products WHERE sku = ANY(:skus)"), {"skus": list(expected)},
        ).all())
    assert stored == dict.fromkeys(expected, "DELETED")
    items, envelope = get_all(client, size=100)
    assert envelope["totalItems"] == 112 and set(expected).isdisjoint(skus(items))


def test_concurrent_overlapping_deletes_have_one_winner(database, client):
    engine, _ = database
    laptop, mouse, keyboard = ids_of(engine, "DEMO-LAPTOP-001", "DEMO-MOUSE-001", "DEMO-KEYBOARD-001")
    barrier = Barrier(2)

    def delete(product_ids):
        barrier.wait(timeout=10)
        return delete_ids(client, product_ids)

    # Both requests list the mouse, in different positions.
    requests = [[laptop, mouse], [mouse, keyboard]]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(delete, requests))
    assert sorted(response.status_code for response in responses) == [204, 404]
    winner = next(ids for ids, response in zip(requests, responses) if response.status_code == 204)
    loser = next(response for response in responses if response.status_code == 404)
    assert loser.json()["productIds"] == [str(mouse)]
    with engine.connect() as connection:
        deleted = set(connection.scalars(text("SELECT id FROM products WHERE status = 'DELETED'")))
    assert deleted == set(winner)               # the losing request deleted nothing


def wait_until_waiting_for_a_lock(engine, application_name, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        # A new transaction per check: pg_stat_activity stays fixed within one.
        with engine.connect() as connection:
            waiting = connection.scalar(text("""
                SELECT count(*) FROM pg_stat_activity
                WHERE application_name = :name AND wait_event_type = 'Lock'
            """), {"name": application_name})
        if waiting:
            return
        assert time.monotonic() < deadline, "the delete request did not wait for the row lock"
        time.sleep(0.05)


@pytest.mark.parametrize("isolation", [None, "repeatable read", "serializable"],
                         ids=["server-default", "repeatable-read", "serializable"])
@pytest.mark.parametrize("other_transaction", ["deletes-it", "rolls-back"])
def test_delete_waits_for_a_locked_product(database, client, isolation, other_transaction):
    """Another transaction holds the row lock of one listed product. The request waits for
    it and then sees its outcome. A product deleted meanwhile is reported as not found
    (never a 500 serialization failure), whatever the default isolation level."""
    engine, _ = database
    laptop, mouse = ids_of(engine, "DEMO-LAPTOP-001", "DEMO-MOUSE-001")
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    settings = [f"search_path={schema}"] + ([f"default_transaction_isolation={isolation}"] if isolation else [])
    name = "inventra-delete-waiter"
    waiter = create_engine(engine.url, connect_args={
        "options": " ".join("-c" + setting.replace(" ", "\\ ") for setting in settings),
        "application_name": name,
    })

    def session():
        with Session(waiter, expire_on_commit=False) as value:
            yield value

    app.dependency_overrides[get_session] = session
    try:
        # The connection closes first on any failure, releasing the lock the request waits for.
        with ThreadPoolExecutor(max_workers=1) as executor, engine.connect() as other:
            other.execute(text("SELECT id FROM products WHERE id = :id FOR UPDATE"), {"id": mouse})
            pending = executor.submit(delete_ids, client, [mouse, laptop])
            wait_until_waiting_for_a_lock(engine, name)
            if other_transaction == "deletes-it":
                other.execute(text(
                    "UPDATE products SET status = 'DELETED', deleted_at = updated_at WHERE id = :id"
                ), {"id": mouse})
                other.commit()
            else:
                other.rollback()
            response = pending.result(timeout=30)
    finally:
        waiter.dispose()

    rows = rows_by_id(engine)
    states = [(rows[product_id]["status"], rows[product_id]["version"]) for product_id in (laptop, mouse)]
    if other_transaction == "deletes-it":
        assert response.status_code == 404, response.text
        assert response.json()["productIds"] == [str(mouse)]
        # The request changed neither product; the other transaction did not bump the version.
        assert states == [("ACTIVE", 0), ("DELETED", 0)]
    else:
        assert response.status_code == 204, response.text
        assert states == [("DELETED", 1), ("DELETED", 1)]


def test_running_server_creates_products_over_http(database, app_servers, payload):
    """The JSON array travels over a real socket to uvicorn, as it does for users."""
    engine, _ = database
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    server = app_servers(url.render_as_string(hide_password=False))
    server.wait_until_serving()
    with httpx.Client(base_url=server.base_url, timeout=10) as http:
        created = http.post(PRODUCTS, json=[payload | {"sku": "http-1"}, payload | {"sku": "http-2"}])
        assert created.status_code == 201, created.text
        items = created.json(parse_float=Decimal)
        assert [item["sku"] for item in items] == ["HTTP-1", "HTTP-2"]
        for item in items:
            assert http.get(f"{PRODUCTS}/{item['id']}").json(parse_float=Decimal) == item
        headers = {"Content-Type": "application/json"}
        for raw, detail in [("", "missing"), ("null", "null"), ("[]", "empty")]:
            rejected = http.post(PRODUCTS, content=raw, headers=headers)
            assert rejected.status_code == 400, rejected.text
            assert detail in rejected.json()["detail"]
        conflict = http.post(PRODUCTS, json=[payload | {"sku": "http-3"}, payload | {"sku": "HTTP-2"}])
        assert (conflict.status_code, conflict.json()["skus"]) == (409, ["HTTP-2"])
        assert http.get(PRODUCTS, params={"size": 1}).json()["totalItems"] == 117

        # 10,000 products in one request, well within a 30-second timeout as gateways
        # commonly apply; a slower response fails with httpx.ReadTimeout.
        started = time.monotonic()
        bulk = http.post(PRODUCTS, json=varied_products(payload, 10_000, "HTTP-BULK"), timeout=30)
        elapsed = time.monotonic() - started
        assert bulk.status_code == 201, bulk.text
        assert len(bulk.json()) == 10_000
        assert elapsed < 30
        assert http.get(PRODUCTS, params={"size": 1}).json()["totalItems"] == 10_117


def test_running_server_soft_deletes_over_http(database, app_servers):
    """The DELETE body travels over a real socket to uvicorn, as it does for users."""
    engine, _ = database
    with engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    server = app_servers(url.render_as_string(hide_password=False))
    server.wait_until_serving()
    with httpx.Client(base_url=server.base_url, timeout=10) as http:
        listed = http.get(PRODUCTS, params={"size": 2}).json()["items"]
        assert [item["status"] for item in listed] == ["ACTIVE", "ACTIVE"]
        ids = [item["id"] for item in listed]
        deleted = http.request("DELETE", PRODUCTS, json={"productIds": ids})
        assert deleted.status_code == 204, deleted.text
        assert [http.get(f"{PRODUCTS}/{product_id}").status_code for product_id in ids] == [404, 404]
        assert http.get(PRODUCTS, params={"size": 1}).json()["totalItems"] == 113
        assert http.request("DELETE", PRODUCTS).status_code == 400          # no body
        again = http.request("DELETE", PRODUCTS, json={"productIds": ids})
        assert (again.status_code, again.json()["productIds"]) == (404, ids)
    with engine.connect() as connection:
        stored = connection.execute(text(
            "SELECT status, deleted_at IS NOT NULL FROM products WHERE id = ANY(:ids)"
        ), {"ids": [UUID(product_id) for product_id in ids]}).all()
    assert stored == [("DELETED", True)] * 2
