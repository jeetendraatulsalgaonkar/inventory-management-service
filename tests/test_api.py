import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from fastapi.testclient import TestClient

from inventory_management_service.db import get_session
from inventory_management_service.main import PRODUCTS, app
from inventory_management_service.models import Product


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = lambda: None
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_create_contract(client, payload, monkeypatch):
    now = datetime.now(UTC)
    product_id = uuid7()

    def create(session, request):
        assert request.sku == "LAP-DELL-1"
        assert request.price.amount == Decimal("999999999999999.9999")
        return Product(
            id=product_id, name="Dell XPS", description=None, category="ELECTRONICS",
            status="ACTIVE", created_at=now, updated_at=now, version=0,
            price_amount=request.price.amount,
        )

    monkeypatch.setattr("inventory_management_service.main.create_product", create)
    raw = json.dumps(payload).replace('"999999999999999.9999"', "999999999999999.9999")
    response = client.post(PRODUCTS, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 201, response.text
    data = response.json(parse_float=Decimal)
    assert UUID(data["id"]).version == 7
    assert response.headers["location"] == f"http://testserver{PRODUCTS}/{product_id}"
    assert data["price"]["amount"] == Decimal("999999999999999.9999")
    assert data["unitOfMeasure"] == "PIECE"
    assert data["status"] == "ACTIVE"
    assert data["version"] == 0
    assert data["createdAt"] == data["updatedAt"]
    assert data["description"] is None


@pytest.mark.parametrize("raw", ["", "{", "null", "[]", '{"sku":NaN}'])
def test_malformed(client, raw):
    response = client.post(PRODUCTS, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:inventra:problem:malformed-request"
    assert response.json()["instance"] == PRODUCTS


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"sku": "-invalid"}, "sku"),
        ({"sku": "a" * 65}, "sku"),
        ({"name": "   "}, "name"),
        ({"category": "a" * 101}, "category"),
        ({"description": "a" * 2001}, "description"),
        ({"attributes": {" ": "x"}}, "attributes"),
        ({"attributes": {"x": None}}, "attributes"),
        ({"attributes": {"x": "a" * 256}}, "attributes"),
        ({"attributes": {str(n): "" for n in range(51)}}, "attributes"),
        ({"unitOfMeasure": "piece"}, "unitOfMeasure"),
        ({"status": "ACTIVE"}, "status"),
        ({"price": {"amount": "-1", "currency": "EUR"}}, "price.amount"),
        ({"price": {"amount": "1.00000", "currency": "EUR"}}, "price.amount"),
        ({"price": {"amount": "1000000000000000", "currency": "EUR"}}, "price.amount"),
        ({"price": {"amount": True, "currency": "EUR"}}, "price.amount"),
        ({"price": {"amount": 1, "currency": "ZZZ"}}, "price.currency"),
    ],
)
def test_invalid_fields(client, payload, changes, field):
    response = client.post(PRODUCTS, json=payload | changes)
    assert response.status_code == 400, response.text
    assert any(item["field"].startswith(field) for item in response.json()["errors"])


def test_attributes_required(client, payload):
    del payload["attributes"]
    response = client.post(PRODUCTS, json=payload)
    assert response.status_code == 400
    assert response.json()["type"] == "urn:inventra:problem:validation-error"


def test_unsupported_media(client):
    response = client.post(PRODUCTS, content="{}", headers={"Content-Type": "text/plain"})
    assert response.status_code == 415
    assert response.headers["content-type"] == "application/problem+json"


def test_openapi(client):
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"][PRODUCTS]) == {"post"}
    operation = schema["paths"][PRODUCTS]["post"]
    assert set(operation["responses"]) == {"201", "400", "409", "415", "500"}
    assert operation["requestBody"]["required"]
    assert "PriceResponse" in schema["components"]["schemas"]
    assert "application/problem+json" in operation["responses"]["409"]["content"]
