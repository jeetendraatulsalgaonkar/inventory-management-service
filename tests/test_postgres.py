from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
from threading import Barrier
from uuid import UUID

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
        product = session.scalars(select(Product).where(Product.sku == "LAP-DELL-1")).one()
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
        assert len(session.scalars(select(Product).where(Product.sku == "LAP-DELL-1")).all()) == 1


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


def test_get_by_location_returns_created_product(database, client, payload):
    engine, _ = database
    created = client.post(PRODUCTS, json=payload)
    assert created.status_code == 201, created.text
    fetched = client.get(created.headers["location"])
    assert fetched.status_code == 200, fetched.text
    # Stored values, including the 19-digit price and microsecond timestamps, round-trip exactly.
    data = fetched.json(parse_float=Decimal)
    assert data == created.json(parse_float=Decimal)
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
    assert client.post(PRODUCTS, json={
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
    for sku, name in [("TOWEL-1", "100% Cotton Towel"), ("TEA-1", "Tea_Bags"), ("PATH-1", "C:\\Temp")]:
        created = client.post(PRODUCTS, json=payload | {"sku": sku, "name": name})
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
    created = client.post(PRODUCTS, json=payload | {"sku": "usd-1", "price": {"amount": "10", "currency": "USD"}})
    assert created.status_code == 201, created.text
    usd = client.get(PRODUCTS, params={"currency": "USD"}).json()
    assert skus(usd["items"]) == ["USD-1"]
    assert client.get(PRODUCTS, params={"currency": "EUR", "size": 1}).json()["totalItems"] == 115
    # Price bounds alone compare amounts across currencies.
    assert "USD-1" in skus(client.get(PRODUCTS, params={"minPrice": 10, "maxPrice": 10}).json()["items"])
    assert client.get(PRODUCTS, params={"minPrice": 10, "maxPrice": 10, "currency": "EUR"}).json()["totalItems"] == 0


def test_timestamps_are_utc_in_any_session_time_zone(database, client, payload):
    engine, _ = database
    created = client.post(PRODUCTS, json=payload)
    assert created.status_code == 201, created.text
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
        fetched = client.get(created.headers["location"]).json()
        listed = client.get(PRODUCTS, params={"search": "LAP-DELL-1"}).json()["items"]
    finally:
        local.dispose()
    assert fetched["createdAt"] == created.json()["createdAt"]
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
    assert client.post(PRODUCTS, json=payload | {"sku": sku, "name": name}).status_code == 201


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
