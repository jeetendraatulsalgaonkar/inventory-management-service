"""HTTP contract tests for the product API. No database is used.

The session dependency is replaced, and the service functions are stubbed where
main.py looks them up. These tests pin down status codes, headers, the product
representation, validation rules, and problem documents for Create Product, Get
Product by ID, List Products, and Soft-delete Products. The service logic is unit
tested in test_service.py, and test_postgres.py runs the whole stack against PostgreSQL.
"""

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid7

import pytest
from fastapi.testclient import TestClient

from inventory_management_service.db import get_session
from inventory_management_service.main import PRODUCTS, app
from inventory_management_service.models import Product
from inventory_management_service.schemas import MAX_DELETE_BATCH, ProductSort, SortDirection, Unit
from inventory_management_service.service import DuplicateSku, ProductNotFound, ProductsNotFound, ProductSlice

PROBLEM_MEDIA = "application/problem+json"
SEEDED_ID = "01a1b052-ec7c-76db-a862-e0da4baa5110"
# Every product representation has exactly these members. JSON objects are
# unordered, so the tests compare member sets, never member order.
PRODUCT_FIELDS = {
    "id", "sku", "name", "description", "category", "price", "unitOfMeasure",
    "attributes", "status", "createdAt", "updatedAt", "version",
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("inventory_management_service.main.run_migrations", lambda: None)
    app.dependency_overrides[get_session] = lambda: None
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def stored_product(**changes) -> Product:
    """A fully populated product, as the services return it."""
    created = datetime(2025, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)
    values = dict(
        id=uuid7(), sku="LAP-DELL-1", name="Dell XPS", description=None, category="ELECTRONICS",
        price_amount=Decimal("1499.9900"), price_currency="EUR", unit_of_measure="PIECE",
        attributes={"brand": "Dell"}, status="ACTIVE", created_at=created, updated_at=created,
        deleted_at=None, version=0,
    )
    return Product(**values | changes)


class ServiceStub:
    """Stand-in for a service function. Records each call, then returns or raises.

    ``result`` is either the value to return or a function of the call argument.
    """

    def __init__(self, result):
        self.result = result
        self.error = None
        self.calls = []

    def __call__(self, session, argument):
        self.calls.append(argument)
        if self.error is not None:
            raise self.error
        return self.result(argument) if callable(self.result) else self.result


@pytest.fixture
def services(monkeypatch):
    """Stub the four product services with realistic default results."""
    stubs = SimpleNamespace(
        create=ServiceStub(lambda command: stored_product(sku=command.sku)),
        get=ServiceStub(lambda product_id: stored_product(id=product_id)),
        list=ServiceStub(ProductSlice([], 0)),
        delete=ServiceStub(None),
    )
    monkeypatch.setattr("inventory_management_service.main.create_product", stubs.create)
    monkeypatch.setattr("inventory_management_service.main.get_product", stubs.get)
    monkeypatch.setattr("inventory_management_service.main.list_products", stubs.list)
    monkeypatch.setattr("inventory_management_service.main.soft_delete_products", stubs.delete)
    return stubs


def post_raw(client, raw, content_type="application/json"):
    """POST an exact request body, bypassing client-side JSON encoding."""
    headers = {"Content-Type": content_type} if content_type is not None else {}
    return client.post(PRODUCTS, content=raw, headers=headers)


def assert_problem(response, status, kind=None):
    """Assert an RFC 9457 problem document and return its body."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA
    body = response.json()
    assert body["type"] == (f"urn:inventra:problem:{kind}" if kind else "about:blank")
    assert body["status"] == status
    assert {"title", "detail", "instance"} <= set(body)
    return body


def error_fields(body):
    return [item["field"] for item in body.get("errors", [])]


class Number(str):
    """The exact source text of a JSON number. JSON strings never become Number."""


# json.loads hooks that keep every JSON number as its literal text.
NUMBER_LEXEMES = {"parse_int": Number, "parse_float": Number}


# ---------------------------------------------------------------------------
# Create Product: success
# ---------------------------------------------------------------------------

def test_create_returns_created_product_and_location(client, payload, services):
    product = stored_product()
    services.create.result = product
    response = client.post(PRODUCTS, json=payload)
    assert response.status_code == 201, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["location"] == f"http://testserver{PRODUCTS}/{product.id}"
    data = response.json(parse_float=Decimal)
    assert data == {
        "id": str(product.id),
        "sku": "LAP-DELL-1",
        "name": "Dell XPS",
        "description": None,
        "category": "ELECTRONICS",
        "price": {"amount": Decimal("1499.99"), "currency": "EUR"},
        "unitOfMeasure": "PIECE",
        "attributes": {"brand": "Dell"},
        "status": "ACTIVE",
        "createdAt": "2025-03-01T12:00:00.123456Z",
        "updatedAt": "2025-03-01T12:00:00.123456Z",
        "version": 0,
    }


def test_create_passes_canonical_command_to_service(client, payload, services):
    # A raw JSON number keeps all 19 digits; it is never parsed as a binary float.
    raw = json.dumps(payload).replace('"999999999999999.9999"', "999999999999999.9999")
    assert post_raw(client, raw).status_code == 201
    (command,) = services.create.calls
    assert command.sku == "LAP-DELL-1"
    assert command.price.amount == Decimal("999999999999999.9999")
    assert command.price.currency == "EUR"
    assert command.unit_of_measure is Unit.PIECE
    assert command.attributes == {"brand": "Dell"}


def test_create_normalizes_input_end_to_end(client, payload, fake_session):
    """Run the real create service on a fake session: request to response, no stubs."""
    app.dependency_overrides[get_session] = lambda: fake_session
    response = client.post(PRODUCTS, json=payload)
    assert response.status_code == 201, response.text
    data = response.json(parse_float=Decimal)
    (product,) = fake_session.added
    assert data["id"] == str(product.id)
    assert UUID(data["id"]).version == 7
    assert response.headers["location"].endswith(f"{PRODUCTS}/{data['id']}")
    assert data["sku"] == "LAP-DELL-1"
    assert data["name"] == "Dell XPS"
    assert data["description"] is None
    assert data["category"] == "ELECTRONICS"
    assert data["price"] == {"amount": Decimal("999999999999999.9999"), "currency": "EUR"}
    assert (data["status"], data["version"]) == ("ACTIVE", 0)
    assert data["createdAt"] == data["updatedAt"]
    assert data["createdAt"].endswith("Z")


@pytest.mark.parametrize("content_type", [
    "application/json; charset=utf-8",
    "APPLICATION/JSON",
    "application/merge-patch+json",
    "application/vnd.inventra.product+json",
])
def test_create_accepts_json_media_types(client, payload, services, content_type):
    assert post_raw(client, json.dumps(payload), content_type).status_code == 201


@pytest.mark.parametrize("changes", [
    {"sku": "A" * 64},
    {"sku": "a.b_c-d"},
    {"sku": "9LIVES"},
    {"name": "n" * 255},
    {"name": "Käse 🧀"},
    {"description": None},
    {"description": "d" * 2000},
    {"category": "c" * 100},
    {"price": {"amount": 0, "currency": "EUR"}},
    {"price": {"amount": "0", "currency": "EUR"}},
    {"price": {"amount": 7, "currency": "EUR"}},
    {"price": {"amount": 1.5, "currency": "EUR"}},
    {"price": {"amount": "0.0001", "currency": "EUR"}},
    {"price": {"amount": "+1.25", "currency": "EUR"}},
    {"price": {"amount": "999999999999999.9999", "currency": "JPY"}},
    {"attributes": {}},
    {"attributes": {str(n): "" for n in range(50)}},
    {"attributes": {"k" * 64: "v" * 255}},
    {"attributes": {"origin": "Zürich"}},
], ids=lambda changes: json.dumps(changes, ensure_ascii=False)[:40])
def test_create_accepts_boundary_values(client, payload, services, changes):
    response = client.post(PRODUCTS, json=payload | changes)
    assert response.status_code == 201, response.text
    assert len(services.create.calls) == 1


def test_create_description_is_optional(client, payload, services):
    del payload["description"]
    assert client.post(PRODUCTS, json=payload).status_code == 201
    assert services.create.calls[0].description is None


@pytest.mark.parametrize("unit", list(Unit))
def test_create_accepts_every_unit_of_measure(client, payload, services, unit):
    assert client.post(PRODUCTS, json=payload | {"unitOfMeasure": unit.value}).status_code == 201
    assert services.create.calls[0].unit_of_measure is unit


# ---------------------------------------------------------------------------
# Create Product: malformed requests (400 malformed-request)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "", "   ", "{", '{"sku": }', "null", "[]", '"product"', "42", "true",
    '{"sku": NaN}', '{"sku": Infinity}', '{"sku": -Infinity}',
    b'{"sku": "\xff"}',                       # invalid UTF-8
    "[" * 100_000 + "]" * 100_000,            # nesting deep enough to hit the recursion limit
], ids=lambda raw: repr(raw)[:20])
def test_create_rejects_malformed_json(client, services, raw):
    body = assert_problem(post_raw(client, raw), 400, "malformed-request")
    assert body["title"] == "Malformed request body"
    assert body["instance"] == PRODUCTS
    assert services.create.calls == []


def test_create_without_content_type_or_body_is_malformed(client, services):
    assert_problem(post_raw(client, "", content_type=None), 400, "malformed-request")


@pytest.mark.parametrize(("changes", "field"), [
    ({"sku": 5}, "sku"),
    ({"name": ["x"]}, "name"),
    ({"description": 1}, "description"),
    ({"category": {}}, "category"),
    ({"price": 5}, "price"),
    ({"price": {"amount": 1, "currency": 978}}, "price.currency"),
    ({"attributes": []}, "attributes"),
    ({"attributes": {"x": None}}, "attributes.x"),
    ({"attributes": {"x": 1}}, "attributes.x"),
    # Unknown properties, including server-assigned and misspelled fields.
    ({"id": SEEDED_ID}, "id"),
    ({"status": "ACTIVE"}, "status"),
    ({"version": 5}, "version"),
    ({"createdAt": "2025-01-01T00:00:00Z"}, "createdAt"),
    ({"unit_of_measure": "PIECE"}, "unit_of_measure"),
    ({"price": {"amount": 1, "currency": "EUR", "tax": 1}}, "price.tax"),
])
def test_create_rejects_wrong_types_and_unknown_properties(client, payload, services, changes, field):
    body = assert_problem(client.post(PRODUCTS, json=payload | changes), 400, "malformed-request")
    assert field in error_fields(body)
    assert services.create.calls == []


# ---------------------------------------------------------------------------
# Create Product: invalid values (400 validation-error)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("changes", "field"), [
    ({"sku": "-invalid"}, "sku"),
    ({"sku": "_A"}, "sku"),
    ({"sku": "A B"}, "sku"),
    ({"sku": "a/b"}, "sku"),
    ({"sku": "É1"}, "sku"),
    ({"sku": "   "}, "sku"),
    ({"sku": "a" * 65}, "sku"),
    ({"sku": " " + "A" * 64}, "sku"),               # the limit applies before trimming
    ({"name": "   "}, "name"),
    ({"name": "n" * 256}, "name"),
    ({"name": "a\0b"}, "name"),
    ({"category": "  "}, "category"),
    ({"category": "a" * 101}, "category"),
    ({"description": "a" * 2001}, "description"),
    ({"description": "a\0"}, "description"),
    ({"price": {"amount": "-1", "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": -0.01, "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": "1.00000", "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": "1000000000000000", "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": "1e3", "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": " 1", "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": "NaN", "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": "abc", "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": None, "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": True, "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": [1], "currency": "EUR"}}, "price.amount"),
    ({"price": {"amount": 1, "currency": "eur"}}, "price.currency"),
    ({"price": {"amount": 1, "currency": "ZZZ"}}, "price.currency"),
    ({"price": {"amount": 1, "currency": "EURO"}}, "price.currency"),
    ({"price": {"amount": 1, "currency": ""}}, "price.currency"),
    ({"unitOfMeasure": "piece"}, "unitOfMeasure"),
    ({"unitOfMeasure": "UNIT"}, "unitOfMeasure"),
    ({"unitOfMeasure": None}, "unitOfMeasure"),
    ({"attributes": {" ": "x"}}, "attributes"),
    ({"attributes": {"": "x"}}, "attributes"),
    ({"attributes": {"k" * 65: "v"}}, "attributes"),
    ({"attributes": {"x": "a" * 256}}, "attributes.x"),
    ({"attributes": {"x": "a\0"}}, "attributes.x"),
    ({"attributes": {str(n): "" for n in range(51)}}, "attributes"),
], ids=lambda value: json.dumps(value, ensure_ascii=False)[:40] if isinstance(value, dict) else value)
def test_create_rejects_invalid_values(client, payload, services, changes, field):
    body = assert_problem(client.post(PRODUCTS, json=payload | changes), 400, "validation-error")
    assert body["title"] == "Validation failed"
    assert any(name.startswith(field) for name in error_fields(body)), body
    assert services.create.calls == []


@pytest.mark.parametrize("field", ["sku", "name", "category", "price", "unitOfMeasure", "attributes"])
def test_create_requires_field(client, payload, services, field):
    del payload[field]
    body = assert_problem(client.post(PRODUCTS, json=payload), 400, "validation-error")
    assert body["errors"] == [{"field": field, "message": "Field required"}]


@pytest.mark.parametrize("field", ["amount", "currency"])
def test_create_requires_price_field(client, payload, services, field):
    del payload["price"][field]
    body = assert_problem(client.post(PRODUCTS, json=payload), 400, "validation-error")
    assert body["errors"] == [{"field": f"price.{field}", "message": "Field required"}]


def test_create_reports_every_invalid_field_sorted(client, payload, services):
    changes = {"sku": "-", "name": " ", "price": {"amount": -1, "currency": "ZZZ"}}
    body = assert_problem(client.post(PRODUCTS, json=payload | changes), 400, "validation-error")
    assert error_fields(body) == ["name", "price.amount", "price.currency", "sku"]
    assert body["detail"] == "The request contains 4 invalid field(s)."


def test_create_type_errors_make_the_whole_request_malformed(client, payload, services):
    # One wrong JSON type classifies the request as malformed, but every error is still listed.
    body = assert_problem(client.post(PRODUCTS, json=payload | {"sku": 5, "name": " "}), 400, "malformed-request")
    assert error_fields(body) == ["name", "sku"]


def test_create_rejects_lone_surrogates(client, payload, services):
    # Lone UTF-16 surrogates are valid JSON escapes but cannot be stored as text.
    raw = json.dumps(payload | {"name": "PLACEHOLDER"}).replace("PLACEHOLDER", "\\ud800")
    assert error_fields(assert_problem(post_raw(client, raw), 400, "validation-error")) == ["name"]

    raw = json.dumps(payload | {"attributes": {"PLACEHOLDER": "x"}}).replace("PLACEHOLDER", "\\udfff")
    response = post_raw(client, raw)
    body = assert_problem(response, 400, "validation-error")
    assert error_fields(body)[0].startswith("attributes.")
    response.content.decode("utf-8")   # the error document itself is valid UTF-8


# ---------------------------------------------------------------------------
# Create Product: media type, conflict, unexpected failure, methods
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content_type", [
    "text/plain", "application/xml", "application/x-www-form-urlencoded", "multipart/form-data", None,
])
def test_create_rejects_non_json_media_types(client, payload, services, content_type):
    body = assert_problem(post_raw(client, json.dumps(payload), content_type), 415)
    assert body["title"] == "Unsupported Media Type"
    assert body["detail"] == "Send the product as JSON."
    assert services.create.calls == []


def test_create_duplicate_sku(client, payload, services):
    services.create.error = DuplicateSku("LAP-DELL-1")
    response = client.post(PRODUCTS, json=payload)
    assert assert_problem(response, 409, "duplicate-sku") == {
        "type": "urn:inventra:problem:duplicate-sku",
        "title": "Duplicate SKU",
        "status": 409,
        "detail": "A product with SKU 'LAP-DELL-1' already exists.",
        "instance": PRODUCTS,
        "sku": "LAP-DELL-1",
    }
    assert "location" not in response.headers


def test_create_unexpected_failure_is_generic_and_logged(client, payload, services, caplog):
    services.create.error = RuntimeError("connection to 10.0.0.5 failed: password=secret")
    with caplog.at_level(logging.ERROR, logger="inventory_management_service.main"):
        response = client.post(PRODUCTS, json=payload)
    assert assert_problem(response, 500) == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "detail": "An unexpected error occurred.",
        "instance": PRODUCTS,
    }
    assert "secret" not in response.text
    # The details go to the log instead of the client.
    logged = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert [str(error) for error in logged] == ["connection to 10.0.0.5 failed: password=secret"]


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_unsupported_methods_on_the_collection(client, method):
    assert_problem(client.request(method, PRODUCTS), 405)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_unsupported_methods_on_a_product(client, method):
    response = client.request(method, f"{PRODUCTS}/{SEEDED_ID}")
    assert_problem(response, 405)
    assert response.headers["allow"] == "GET"


def test_unknown_route_is_a_problem_document(client):
    assert_problem(client.get("/api/v1/unknown"), 404)


# ---------------------------------------------------------------------------
# Get Product by ID
# ---------------------------------------------------------------------------

def test_get_product_contract(client, services):
    # The driver returns timestamptz in the session time zone; the API must emit UTC.
    local = timezone(timedelta(hours=2))
    product = stored_product(
        price_amount=Decimal("999999999999999.9999"),
        created_at=datetime(2025, 3, 1, 14, 0, tzinfo=local),
        updated_at=datetime(2025, 3, 2, 14, 0, 0, 5, tzinfo=local),
        version=3,
    )
    services.get.result = product
    response = client.get(f"{PRODUCTS}/{product.id}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert services.get.calls == [product.id]
    data = response.json(parse_float=Decimal)
    assert data == {
        "id": str(product.id),
        "sku": "LAP-DELL-1",
        "name": "Dell XPS",
        "description": None,
        "category": "ELECTRONICS",
        "price": {"amount": Decimal("999999999999999.9999"), "currency": "EUR"},
        "unitOfMeasure": "PIECE",
        "attributes": {"brand": "Dell"},
        "status": "ACTIVE",
        "createdAt": "2025-03-01T12:00:00Z",
        "updatedAt": "2025-03-02T12:00:00.000005Z",
        "version": 3,
    }


@pytest.mark.parametrize("spelling", [
    SEEDED_ID,
    SEEDED_ID.upper(),
    SEEDED_ID.replace("-", ""),
    "{" + SEEDED_ID + "}",
    "urn:uuid:" + SEEDED_ID,
])
def test_get_product_accepts_uuid_spellings(client, services, spelling):
    response = client.get(f"{PRODUCTS}/{spelling}")
    assert response.status_code == 200
    assert services.get.calls == [UUID(SEEDED_ID)]
    # Whatever the spelling in the path, the body carries the canonical id.
    assert response.json()["id"] == SEEDED_ID


@pytest.mark.parametrize("status", ["ACTIVE", "INACTIVE", "DISCONTINUED"])
def test_get_product_returns_every_non_deleted_status(client, services, status):
    services.get.result = stored_product(status=status)
    assert client.get(f"{PRODUCTS}/{SEEDED_ID}").json()["status"] == status


def test_get_product_preserves_text_and_attributes(client, services):
    services.get.result = stored_product(
        name="Käse 🧀", description="Aged 12 months", attributes={"origin": "Zürich", "note": ""},
    )
    data = client.get(f"{PRODUCTS}/{SEEDED_ID}").json()
    assert data["name"] == "Käse 🧀"
    assert data["description"] == "Aged 12 months"
    assert data["attributes"] == {"origin": "Zürich", "note": ""}


def test_get_product_not_found(client, services):
    product_id = uuid7()
    services.get.error = ProductNotFound(product_id)
    response = client.get(f"{PRODUCTS}/{product_id}")
    assert assert_problem(response, 404, "product-not-found") == {
        "type": "urn:inventra:problem:product-not-found",
        "title": "Product not found",
        "status": 404,
        "detail": f"No product with ID '{product_id}' exists.",
        "instance": f"{PRODUCTS}/{product_id}",
        "productId": str(product_id),
    }


@pytest.mark.parametrize("product_id", [
    "not-a-uuid", "123", SEEDED_ID[:-1], SEEDED_ID + "0", SEEDED_ID + "x", "g" * 32, "%00",
])
def test_get_product_rejects_malformed_id(client, services, product_id):
    body = assert_problem(client.get(f"{PRODUCTS}/{product_id}"), 400, "validation-error")
    assert error_fields(body) == ["productId"]
    assert body["detail"] == "The request contains 1 invalid parameter(s)."
    assert services.get.calls == []


def test_get_product_unexpected_failure_is_generic(client, services):
    services.get.error = RuntimeError("connection lost: secret details")
    response = client.get(f"{PRODUCTS}/{SEEDED_ID}")
    assert assert_problem(response, 500)["detail"] == "An unexpected error occurred."
    assert "secret" not in response.text


# ---------------------------------------------------------------------------
# List Products
# ---------------------------------------------------------------------------

def test_list_defaults_and_envelope(client, services):
    items = [stored_product(sku="A-1"), stored_product(sku="B-2", price_amount=Decimal("0.5000"))]
    services.list.result = ProductSlice(items, 41)
    response = client.get(PRODUCTS)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    (query,) = services.list.calls
    assert (query.page, query.size, query.sort, query.direction) == (1, 20, ProductSort.NAME, SortDirection.ASC)
    assert (query.search, query.category, query.min_price, query.max_price, query.currency) == (None,) * 5
    data = response.json(parse_float=Decimal)
    assert set(data) == {"items", "page", "size", "totalItems", "totalPages"}
    assert (data["page"], data["size"], data["totalItems"], data["totalPages"]) == (1, 20, 41, 3)
    assert [item["sku"] for item in data["items"]] == ["A-1", "B-2"]
    assert [item["id"] for item in data["items"]] == [str(item.id) for item in items]
    assert data["items"][1]["price"]["amount"] == Decimal("0.5")


@pytest.mark.parametrize(("total", "size", "pages"), [(0, 20, 0), (1, 20, 1), (20, 20, 1), (21, 20, 2), (115, 7, 17)])
def test_list_total_pages(client, services, total, size, pages):
    services.list.result = ProductSlice([], total)
    response = client.get(PRODUCTS, params={"size": size, "page": 99})
    assert response.status_code == 200
    assert response.json() == {"items": [], "page": 99, "size": size, "totalItems": total, "totalPages": pages}


def test_list_parses_all_parameters(client, services):
    response = client.get(PRODUCTS, params={
        "page": "3", "size": "100", "search": "  ring binder ", "category": " office_supplies ",
        "minPrice": "0", "maxPrice": "24.9", "currency": "EUR", "sort": "createdAt", "direction": "desc",
    })
    assert response.status_code == 200, response.text
    (query,) = services.list.calls
    assert (query.page, query.size) == (3, 100)
    assert query.search == "ring binder"
    assert query.category == "office_supplies"
    assert query.min_price == Decimal("0") and query.max_price == Decimal("24.9")
    assert query.currency == "EUR"
    assert (query.sort, query.direction) == (ProductSort.CREATED_AT, SortDirection.DESC)


@pytest.mark.parametrize("sort", list(ProductSort))
@pytest.mark.parametrize("direction", list(SortDirection))
def test_list_accepts_every_sort(client, services, sort, direction):
    assert client.get(PRODUCTS, params={"sort": sort.value, "direction": direction.value}).status_code == 200
    (query,) = services.list.calls
    assert (query.sort, query.direction) == (sort, direction)


@pytest.mark.parametrize("blank", ["", "   "])
def test_list_blank_text_filters_are_ignored(client, services, blank):
    assert client.get(PRODUCTS, params={"search": blank, "category": blank}).status_code == 200
    (query,) = services.list.calls
    assert query.search is None and query.category is None


def test_list_equal_price_bounds_are_allowed(client, services):
    assert client.get(PRODUCTS, params={"minPrice": "24.9", "maxPrice": "24.90"}).status_code == 200


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"page": "0"}, "page"),
        ({"page": "-1"}, "page"),
        ({"page": "abc"}, "page"),
        ({"page": "2147483648"}, "page"),
        ({"size": "0"}, "size"),
        ({"size": "101"}, "size"),
        ({"minPrice": "-0.01"}, "minPrice"),
        ({"minPrice": "ten"}, "minPrice"),
        ({"maxPrice": "NaN"}, "maxPrice"),
        ({"maxPrice": "Infinity"}, "maxPrice"),
        ({"maxPrice": "1.00001"}, "maxPrice"),
        ({"maxPrice": "1000000000000000"}, "maxPrice"),
        ({"minPrice": "10", "maxPrice": "9.99"}, "minPrice"),
        ({"currency": "eur"}, "currency"),
        ({"currency": "ZZZ"}, "currency"),
        ({"sort": "weight"}, "sort"),
        ({"sort": "Name"}, "sort"),
        ({"direction": "up"}, "direction"),
        ({"search": "a" * 256}, "search"),
        ({"search": "bad\0text"}, "search"),
        ({"category": "a" * 101}, "category"),
        ({"categroy": "ELECTRONICS"}, "categroy"),
    ],
)
def test_list_rejects_invalid_parameters(client, services, params, field):
    body = assert_problem(client.get(PRODUCTS, params=params), 400, "validation-error")
    assert body["instance"] == PRODUCTS
    assert error_fields(body) == [field]
    assert services.list.calls == []


def test_list_reports_every_invalid_parameter(client, services):
    body = assert_problem(client.get(PRODUCTS, params={"page": "0", "size": "500", "sort": "weight"}), 400, "validation-error")
    assert error_fields(body) == ["page", "size", "sort"]
    assert body["detail"] == "The request contains 3 invalid parameter(s)."


def test_list_unexpected_failure_is_generic(client, services):
    services.list.error = RuntimeError("connection lost: secret details")
    response = client.get(PRODUCTS)
    assert assert_problem(response, 500)["detail"] == "An unexpected error occurred."
    assert "secret" not in response.text


# ---------------------------------------------------------------------------
# Soft-delete Products
# ---------------------------------------------------------------------------

OTHER_ID = "01a1b052-ec7d-74d0-bf78-a283a884700e"


def delete_ids(client, product_ids):
    """DELETE the collection with {"productIds": product_ids} as the JSON body."""
    return client.request("DELETE", PRODUCTS, json={"productIds": product_ids})


def delete_raw(client, raw, content_type="application/json"):
    """DELETE the collection with an exact request body, bypassing client-side JSON encoding."""
    headers = {"Content-Type": content_type} if content_type is not None else {}
    return client.request("DELETE", PRODUCTS, content=raw, headers=headers)


def test_delete_returns_no_content(client, services):
    response = delete_ids(client, [SEEDED_ID, OTHER_ID])
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert "content-type" not in response.headers
    # The service receives parsed UUIDs, in request order.
    assert services.delete.calls == [[UUID(SEEDED_ID), UUID(OTHER_ID)]]


def test_delete_accepts_uuid_spellings(client, services):
    ids = [uuid7() for _ in range(4)]
    spellings = [str(ids[0]).upper(), ids[1].hex, "{" + str(ids[2]) + "}", "urn:uuid:" + str(ids[3])]
    assert delete_ids(client, spellings).status_code == 204
    assert services.delete.calls == [ids]


@pytest.mark.parametrize("count", [1, 2, MAX_DELETE_BATCH])
def test_delete_accepts_one_to_the_maximum_number_of_ids(client, services, count):
    ids = [uuid7() for _ in range(count)]
    assert delete_ids(client, [str(product_id) for product_id in ids]).status_code == 204
    assert services.delete.calls == [ids]


@pytest.mark.parametrize("content_type", [
    "application/json; charset=utf-8", "APPLICATION/JSON", "application/vnd.inventra.delete+json",
])
def test_delete_accepts_json_media_types(client, services, content_type):
    raw = json.dumps({"productIds": [SEEDED_ID]})
    assert delete_raw(client, raw, content_type).status_code == 204
    assert services.delete.calls == [[UUID(SEEDED_ID)]]


@pytest.mark.parametrize(("raw", "content_type"), [
    ("", None),                                 # no body at all
    ("", "application/json"),                   # JSON declared, but no body
    ("null", "application/json"),               # a JSON null body
    (" null\n", "application/json"),
], ids=["no-body", "empty-json-body", "null", "null-with-whitespace"])
def test_delete_rejects_a_missing_or_null_body(client, services, raw, content_type):
    body = assert_problem(delete_raw(client, raw, content_type), 400, "malformed-request")
    assert body["title"] == "Malformed request body"
    assert body["detail"] == "The request body is missing or has invalid JSON fields or types."
    assert body["instance"] == PRODUCTS
    assert services.delete.calls == []


@pytest.mark.parametrize(("document", "message"), [
    ({}, "Field required"),
    ({"productIds": []}, "List should have at least 1 item after validation, not 0"),
], ids=["no-productIds", "empty-productIds"])
def test_delete_rejects_a_request_without_product_ids(client, services, document, message):
    body = assert_problem(client.request("DELETE", PRODUCTS, json=document), 400, "validation-error")
    assert body["errors"] == [{"field": "productIds", "message": message}]
    assert body["detail"] == "The request contains 1 invalid field(s)."
    assert services.delete.calls == []


@pytest.mark.parametrize("raw", [
    "   ", "{", '{"productIds": }', "[]", f'["{SEEDED_ID}"]', '"product"', "42", "true",
    '{"productIds": NaN}', b'{"productIds": ["\xff"]}',
], ids=lambda raw: repr(raw)[:24])
def test_delete_rejects_malformed_json(client, services, raw):
    # The body must be an object with productIds; a bare array of IDs is malformed.
    assert_problem(delete_raw(client, raw), 400, "malformed-request")
    assert services.delete.calls == []


@pytest.mark.parametrize(("document", "field"), [
    ({"productIds": None}, "productIds"),
    ({"productIds": SEEDED_ID}, "productIds"),              # one ID instead of a list
    ({"productIds": {"id": SEEDED_ID}}, "productIds"),
    ({"productIds": [1]}, "productIds.0"),
    ({"productIds": [SEEDED_ID, None]}, "productIds.1"),
    ({"productIds": [[SEEDED_ID]]}, "productIds.0"),
    # Unknown properties, including misspelled ones.
    ({"productIds": [SEEDED_ID], "force": True}, "force"),
    ({"product_ids": [SEEDED_ID]}, "product_ids"),
    ({"ids": [SEEDED_ID]}, "ids"),
])
def test_delete_rejects_wrong_types_and_unknown_properties(client, services, document, field):
    body = assert_problem(client.request("DELETE", PRODUCTS, json=document), 400, "malformed-request")
    assert field in error_fields(body)
    assert services.delete.calls == []


@pytest.mark.parametrize(("ids", "field"), [
    (["not-a-uuid"], "productIds.0"),
    ([""], "productIds.0"),
    ([SEEDED_ID, SEEDED_ID[:-1]], "productIds.1"),
    ([SEEDED_ID + "0"], "productIds.0"),
    (["g" * 32], "productIds.0"),
])
def test_delete_rejects_invalid_ids(client, services, ids, field):
    body = assert_problem(delete_ids(client, ids), 400, "validation-error")
    assert error_fields(body) == [field]
    assert services.delete.calls == []


def test_delete_reports_every_invalid_id(client, services):
    body = assert_problem(delete_ids(client, ["x", SEEDED_ID, "y"]), 400, "validation-error")
    assert error_fields(body) == ["productIds.0", "productIds.2"]
    assert body["detail"] == "The request contains 2 invalid field(s)."


def test_delete_rejects_more_than_the_maximum_number_of_ids(client, services):
    ids = [str(uuid7()) for _ in range(MAX_DELETE_BATCH + 1)]
    body = assert_problem(delete_ids(client, ids), 400, "validation-error")
    assert body["errors"] == [{
        "field": "productIds",
        "message": f"List should have at most {MAX_DELETE_BATCH} items after validation, not {MAX_DELETE_BATCH + 1}",
    }]
    assert services.delete.calls == []


@pytest.mark.parametrize("repeat", [SEEDED_ID, SEEDED_ID.upper(), "urn:uuid:" + SEEDED_ID])
def test_delete_rejects_repeated_ids(client, services, repeat):
    # Any spelling of an ID that is already listed is a repeat.
    body = assert_problem(delete_ids(client, [SEEDED_ID, OTHER_ID, repeat]), 400, "validation-error")
    assert body["errors"] == [{"field": "productIds", "message": f"must not repeat a product ID: {SEEDED_ID}"}]
    assert services.delete.calls == []


def test_delete_names_each_repeated_id_once(client, services):
    body = assert_problem(
        delete_ids(client, [SEEDED_ID, OTHER_ID, SEEDED_ID, OTHER_ID, SEEDED_ID]), 400, "validation-error",
    )
    assert body["errors"] == [
        {"field": "productIds", "message": f"must not repeat a product ID: {SEEDED_ID}, {OTHER_ID}"},
    ]


@pytest.mark.parametrize("content_type", [
    "text/plain", "application/xml", "application/x-www-form-urlencoded", "multipart/form-data", None,
])
def test_delete_rejects_non_json_media_types(client, services, content_type):
    body = assert_problem(delete_raw(client, json.dumps({"productIds": [SEEDED_ID]}), content_type), 415)
    assert body["title"] == "Unsupported Media Type"
    assert body["detail"] == "Send the product IDs as JSON."
    assert services.delete.calls == []


def test_delete_not_found_lists_every_missing_id(client, services):
    missing = [uuid7(), uuid7()]
    services.delete.error = ProductsNotFound(missing)
    response = delete_ids(client, [SEEDED_ID, *map(str, missing)])
    assert assert_problem(response, 404, "products-not-found") == {
        "type": "urn:inventra:problem:products-not-found",
        "title": "Products not found",
        "status": 404,
        "detail": "2 requested product(s) do not exist or are already deleted. No product was deleted.",
        "instance": PRODUCTS,
        "productIds": [str(product_id) for product_id in missing],
    }


def test_delete_unexpected_failure_is_generic(client, services):
    services.delete.error = RuntimeError("connection lost: secret details")
    response = delete_ids(client, [SEEDED_ID])
    assert assert_problem(response, 500)["detail"] == "An unexpected error occurred."
    assert "secret" not in response.text


def test_delete_marks_products_deleted_end_to_end(client, fake_session):
    """Run the real soft-delete service on a fake session: request to stored state, no stubs."""
    products = [stored_product(sku="A-1"), stored_product(sku="B-2")]
    fake_session.results.append(sorted(products, key=lambda product: product.id))  # the locked rows
    app.dependency_overrides[get_session] = lambda: fake_session
    response = delete_ids(client, [str(product.id) for product in reversed(products)])
    assert response.status_code == 204, response.text
    assert ("commit",) in fake_session.events
    for product in products:
        assert product.status == "DELETED"
        assert product.deleted_at == product.updated_at > product.created_at


def test_delete_with_a_missing_product_changes_nothing_end_to_end(client, fake_session):
    present, missing = stored_product(), uuid7()
    fake_session.results.append([present])      # the lookup finds only one of the two
    app.dependency_overrides[get_session] = lambda: fake_session
    body = assert_problem(delete_ids(client, [str(missing), str(present.id)]), 404, "products-not-found")
    assert body["productIds"] == [str(missing)]
    assert ("commit",) not in fake_session.events
    assert (present.status, present.deleted_at, present.updated_at) == ("ACTIVE", None, present.created_at)


# ---------------------------------------------------------------------------
# One product representation across create, get, and list
# ---------------------------------------------------------------------------

ENDPOINTS = ["create", "get", "list"]


def represent(client, services, payload, endpoint, product, **json_hooks):
    """Return the product JSON that one endpoint produces for `product`.

    By default numbers parse as Decimal; pass NUMBER_LEXEMES to see their exact text.
    """
    if endpoint == "create":
        services.create.result = product
        response = client.post(PRODUCTS, json=payload)
        expected_status = 201
    elif endpoint == "get":
        services.get.result = product
        response = client.get(f"{PRODUCTS}/{product.id}")
        expected_status = 200
    else:
        services.list.result = ProductSlice([product], 1)
        response = client.get(PRODUCTS)
        expected_status = 200
    assert response.status_code == expected_status, response.text
    body = json.loads(response.text, **(json_hooks or {"parse_float": Decimal}))
    return body["items"][0] if endpoint == "list" else body


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_id_is_present_and_identifies_the_product(client, services, payload, endpoint):
    # Regression test for the report that GET responses lacked "id".
    product = stored_product()
    data = represent(client, services, payload, endpoint, product)
    assert set(data) == PRODUCT_FIELDS
    assert data["id"] == str(product.id)              # canonical, lowercase, hyphenated


def test_every_endpoint_returns_the_same_representation(client, services, payload):
    product = stored_product(description="Business laptop", attributes={"ram": "16 GB"}, version=2)
    create, get, listed = (represent(client, services, payload, endpoint, product) for endpoint in ENDPOINTS)
    assert create == get == listed


@pytest.mark.parametrize("endpoint", ["get", "list"])
@pytest.mark.parametrize("status", ["ACTIVE", "INACTIVE", "DISCONTINUED"])
def test_reads_show_the_product_status(client, services, payload, endpoint, status):
    # Both GET operations show the stored status of every product they return.
    data = represent(client, services, payload, endpoint, stored_product(status=status))
    assert data["status"] == status


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(("stored", "emitted"), [
    ("249.0000", "249"), ("0.0000", "0"), ("24.9000", "24.9"), ("0.0001", "0.0001"),
    ("1000.0000", "1000"), ("999999999999999.9999", "999999999999999.9999"),
])
def test_amount_is_an_exact_json_number(client, services, payload, endpoint, stored, emitted):
    product = stored_product(price_amount=Decimal(stored))
    amount = represent(client, services, payload, endpoint, product, **NUMBER_LEXEMES)["price"]["amount"]
    # A JSON number (not a string) whose literal keeps every significant digit and
    # drops insignificant zeros, as the create endpoint always has.
    assert isinstance(amount, Number)
    assert amount == emitted


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_timestamps_are_utc(client, services, payload, endpoint):
    local = timezone(timedelta(hours=-5))
    product = stored_product(
        created_at=datetime(2025, 1, 1, 19, 30, tzinfo=local),
        updated_at=datetime(2025, 1, 2, 8, 0, 0, 250000, tzinfo=local),
    )
    data = represent(client, services, payload, endpoint, product)
    assert data["createdAt"] == "2025-01-02T00:30:00Z"
    assert data["updatedAt"] == "2025-01-02T13:00:00.250000Z"


# ---------------------------------------------------------------------------
# OpenAPI document
# ---------------------------------------------------------------------------

def test_openapi(client):
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"][PRODUCTS]) == {"get", "post", "delete"}
    operation = schema["paths"][PRODUCTS]["post"]
    assert set(operation["responses"]) == {"201", "400", "409", "415", "500"}
    assert operation["requestBody"]["required"]
    assert "PriceResponse" in schema["components"]["schemas"]
    assert "application/problem+json" in operation["responses"]["409"]["content"]
    assert "Location" in operation["responses"]["201"]["headers"]


def test_openapi_product_schema_documents_id(client):
    product = client.get("/openapi.json").json()["components"]["schemas"]["ProductResponse"]
    assert set(product["properties"]) == PRODUCT_FIELDS
    assert set(product["required"]) == PRODUCT_FIELDS - {"description"}
    assert (product["properties"]["id"]["type"], product["properties"]["id"]["format"]) == ("string", "uuid")
    assert set(product["properties"]["status"]["enum"]) == {"ACTIVE", "INACTIVE", "DISCONTINUED"}


def test_openapi_read_operations(client):
    schema = client.get("/openapi.json").json()
    listing = schema["paths"][PRODUCTS]["get"]
    assert set(listing["responses"]) == {"200", "400", "500"}
    assert {parameter["name"] for parameter in listing["parameters"]} == {
        "page", "size", "search", "category", "minPrice", "maxPrice", "currency", "sort", "direction",
    }
    assert listing["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProductPage"
    }
    single = schema["paths"][f"{PRODUCTS}/{{productId}}"]
    assert set(single) == {"get"}
    assert set(single["get"]["responses"]) == {"200", "400", "404", "500"}
    assert single["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProductResponse"
    }
    assert "application/problem+json" in single["get"]["responses"]["404"]["content"]
    components = schema["components"]["schemas"]
    assert "HTTPValidationError" not in components
    assert components["ProductResponse"]["properties"]["price"] == {"$ref": "#/components/schemas/PriceResponse"}
    assert set(components["ProductPage"]["required"]) == {"items", "page", "size", "totalItems", "totalPages"}


def test_openapi_delete_operation(client):
    operation = client.get("/openapi.json").json()["paths"][PRODUCTS]["delete"]
    assert set(operation["responses"]) == {"204", "400", "404", "415", "500"}
    assert "content" not in operation["responses"]["204"]
    for code in ("400", "404", "415", "500"):
        assert "application/problem+json" in operation["responses"][code]["content"]
    not_found = operation["responses"]["404"]["content"]["application/problem+json"]["schema"]
    assert not_found["properties"]["productIds"] == {"type": "array", "items": {"type": "string", "format": "uuid"}}
    assert operation["requestBody"]["required"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert (body["required"], body["additionalProperties"]) == (["productIds"], False)
    ids = body["properties"]["productIds"]
    assert (ids["type"], ids["minItems"], ids["maxItems"], ids["uniqueItems"]) == ("array", 1, MAX_DELETE_BATCH, True)
    assert ids["items"] == {"type": "string", "format": "uuid"}
