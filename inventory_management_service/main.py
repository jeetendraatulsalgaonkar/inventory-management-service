import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

import simplejson
from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from inventory_management_service.db import get_session
from inventory_management_service.migration import run_migrations
from inventory_management_service.models import Product
from inventory_management_service.schemas import CreateProduct, ProductPage, ProductQuery, ProductResponse
from inventory_management_service.service import (
    DuplicateSku,
    ProductNotFound,
    create_product,
    get_product,
    list_products,
)

@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(run_migrations)
    yield


app = FastAPI(title="Inventra Inventory Management", version="0.1.0", lifespan=lifespan)
PRODUCTS = "/api/v1/products"
PROBLEM_MEDIA = "application/problem+json"
logger = logging.getLogger(__name__)


def problem(request, status, detail, *, kind=None, title=None, headers=None, **extensions):
    return JSONResponse(
        {
            "type": f"urn:inventra:problem:{kind}" if kind else "about:blank",
            "title": title or HTTPStatus(status).phrase,
            "status": status,
            "detail": detail,
            "instance": request.url.path,
            **extensions,
        },
        status_code=status,
        media_type=PROBLEM_MEDIA,
        headers=headers,
    )


class InvalidBody(Exception):
    def __init__(self, errors=None, malformed=True):
        self.errors = errors or []
        self.malformed = malformed


@app.exception_handler(InvalidBody)
async def invalid_body(request, error):
    extensions = {"errors": error.errors} if error.errors else {}
    if error.malformed:
        return problem(
            request, 400, "The request body is missing or has invalid JSON fields or types.",
            kind="malformed-request", title="Malformed request body", **extensions,
        )
    return problem(
        request, 400, f"The request contains {len(error.errors)} invalid field(s).",
        kind="validation-error", title="Validation failed", **extensions,
    )


@app.exception_handler(DuplicateSku)
async def duplicate(request, error):
    return problem(
        request, 409, f"A product with SKU '{error.sku}' already exists.",
        kind="duplicate-sku", title="Duplicate SKU", sku=error.sku,
    )


@app.exception_handler(ProductNotFound)
async def not_found(request, error):
    return problem(
        request, 404, f"No product with ID '{error.product_id}' exists.",
        kind="product-not-found", title="Product not found", productId=str(error.product_id),
    )


@app.exception_handler(RequestValidationError)
async def invalid_parameters(request, error):
    # Only path and query parameters reach FastAPI validation; parse_product
    # validates the create body itself. Report them like invalid body fields.
    violations = sorted(
        (
            {
                "field": ".".join(str(part) for part in item["loc"][1:]) or str(item["loc"][0]),
                "message": item["msg"].removeprefix("Value error, "),
            }
            for item in error.errors()
        ),
        key=lambda item: (item["field"], item["message"]),
    )
    return problem(
        request, 400, f"The request contains {len(violations)} invalid parameter(s).",
        kind="validation-error", title="Validation failed", errors=violations,
    )


@app.exception_handler(HTTPException)
async def http_error(request, error):
    return problem(request, error.status_code, error.detail, headers=error.headers)


@app.exception_handler(Exception)
async def unexpected(request, error):
    logger.error("Request failed: %s %s", request.method, request.url.path, exc_info=error)
    return problem(request, 500, "An unexpected error occurred.")


def reject_constant(token):
    raise ValueError("Non-finite JSON number")


async def parse_product(request: Request) -> CreateProduct:
    body = await request.body()
    media = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if body and media != "application/json" and not (
        media.startswith("application/") and media.endswith("+json")
    ):
        raise HTTPException(415, "Send the product as JSON.")
    try:
        payload = json.loads(body, parse_float=Decimal, parse_constant=reject_constant)
    except (ValueError, RecursionError, InvalidOperation) as error:
        raise InvalidBody() from error
    if not isinstance(payload, dict):
        raise InvalidBody()
    try:
        return CreateProduct.model_validate(payload)
    except ValidationError as error:
        errors = error.errors(include_input=False, include_context=False, include_url=False)
        violations = [
            {
                "field": ".".join(str(part) for part in item["loc"]),
                "message": item["msg"].removeprefix("Value error, "),
            }
            for item in errors
        ]
        # Invalid input can contain lone UTF-16 surrogates in property names.
        for item in violations:
            item["field"] = item["field"].encode("utf-8", "replace").decode("utf-8")
        violations.sort(key=lambda item: (item["field"], item["message"]))
        malformed = any(
            item["type"] == "extra_forbidden" or item["type"].endswith("_type")
            for item in errors
        )
        raise InvalidBody(violations, malformed) from error


def problem_docs(descriptions, **extensions):
    return {
        code: {
            "description": description,
            "content": {
                PROBLEM_MEDIA: {
                    "schema": {
                        "type": "object",
                        "required": ["type", "title", "status", "detail", "instance"],
                        "properties": {
                            "type": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {"type": "integer"},
                            "detail": {"type": "string"},
                            "instance": {"type": "string"},
                            **extensions,
                            "errors": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "field": {"type": "string"},
                                        "message": {"type": "string"},
                                    },
                                },
                            },
                        },
                    }
                }
            },
        }
        for code, description in descriptions.items()
    }


problem_responses = problem_docs({
    400: "Malformed body or validation failure",
    409: "Duplicate normalized SKU",
    415: "Unsupported media type",
    500: "Unexpected server error",
}, sku={"type": "string"})

# Parsing the body ourselves preserves every digit before validation. Move the
# generated request schema definitions into OpenAPI's shared component registry.
request_schema = CreateProduct.model_json_schema(ref_template="#/components/schemas/{model}")
request_definitions = request_schema.pop("$defs", {})


@app.post(
    PRODUCTS,
    status_code=201,
    response_model=ProductResponse,
    responses={
        201: {"headers": {"Location": {"schema": {"type": "string", "format": "uri"}}}},
        **problem_responses,
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": request_schema}},
        }
    },
    tags=["Products"],
)
def post_product(
    command: Annotated[CreateProduct, Depends(parse_product)],
    session: Annotated[Session, Depends(get_session)],
    request: Request,
):
    product = create_product(session, command)
    base = str(request.base_url).rstrip("/")
    return json_response(
        product_data(product),
        status_code=201,
        headers={"Location": f"{base}{PRODUCTS}/{product.id}"},
    )


def exact_amount(amount: Decimal) -> Decimal:
    """Drop insignificant zeros (249.0000 -> 249) without losing any digit."""
    return Decimal(format(amount.normalize(), "f"))


timestamp_json = TypeAdapter(datetime)


def product_data(product: Product) -> dict:
    """Serialize a product for every response: create, get by ID, and list items.

    Sharing one serializer keeps the representations identical. The id comes first
    so that clients and Swagger UI show the identifier before the product details.
    """
    return {
        "id": str(product.id),
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        # Keep the decimal exact and emit a JSON number, as in the original API.
        "price": {"amount": exact_amount(product.price_amount), "currency": product.price_currency},
        "unitOfMeasure": product.unit_of_measure,
        "attributes": product.attributes,
        "status": product.status,
        # The driver returns timestamptz in the session time zone; the API emits UTC.
        "createdAt": timestamp_json.dump_python(product.created_at.astimezone(UTC), mode="json"),
        "updatedAt": timestamp_json.dump_python(product.updated_at.astimezone(UTC), mode="json"),
        "version": product.version,
    }


def json_response(data, status_code=200, headers=None) -> Response:
    # simplejson writes Decimal values as exact JSON numbers.
    return Response(
        simplejson.dumps(data, use_decimal=True),
        status_code=status_code,
        media_type="application/json",
        headers=headers,
    )


def product_query(query: Annotated[ProductQuery, Query()]) -> ProductQuery:
    if query.min_price is not None and query.max_price is not None and query.min_price > query.max_price:
        raise RequestValidationError([{
            "type": "value_error",
            "loc": ("query", "minPrice"),
            "msg": "must not be greater than maxPrice",
            "input": str(query.min_price),
        }])
    return query


read_problem_responses = problem_docs({
    400: "Invalid path or query parameter",
    500: "Unexpected server error",
})


@app.get(
    PRODUCTS,
    response_model=ProductPage,
    responses=read_problem_responses,
    tags=["Products"],
    summary="List products",
    description=(
        "Returns one page of products that are not soft-deleted. Filters combine with AND. "
        "Sorting is stable: ties are broken by id, so pages neither overlap nor skip products."
    ),
)
def get_products(
    query: Annotated[ProductQuery, Depends(product_query)],
    session: Annotated[Session, Depends(get_session)],
):
    result = list_products(session, query)
    return json_response({
        "items": [product_data(product) for product in result.items],
        "page": query.page,
        "size": query.size,
        "totalItems": result.total,
        "totalPages": -(-result.total // query.size),
    })


@app.get(
    PRODUCTS + "/{productId}",
    response_model=ProductResponse,
    responses=problem_docs({
        400: "Malformed product ID",
        404: "Product does not exist or is deleted",
        500: "Unexpected server error",
    }, productId={"type": "string", "format": "uuid"}),
    tags=["Products"],
    summary="Get a product by ID",
)
def get_product_by_id(
    product_id: Annotated[UUID, Path(alias="productId", description="Product UUID.")],
    session: Annotated[Session, Depends(get_session)],
):
    return json_response(product_data(get_product(session, product_id)))


default_openapi = app.openapi


def openapi():
    schema = default_openapi()
    components = schema["components"]["schemas"]
    for name, definition in request_definitions.items():
        # Price has a distinct response representation. Unit is shared.
        components.setdefault(name, definition)
    product_schema = components["ProductResponse"]
    product_schema["properties"]["price"] = {"$ref": "#/components/schemas/PriceResponse"}
    # ProductResponse inherits the request fields first. List id first, matching
    # product_data, so Swagger UI examples have the same order as real responses.
    properties = product_schema["properties"]
    product_schema["properties"] = {"id": properties.pop("id"), **properties}
    product_schema["required"] = ["id", *(name for name in product_schema["required"] if name != "id")]
    components["PriceResponse"] = {
        "type": "object",
        "required": ["amount", "currency"],
        "properties": {
            "amount": {"type": "number", "minimum": 0},
            "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        },
    }
    components["Price"] = request_definitions["Price"]
    # Parameter validation failures are reported as 400 problems, not FastAPI's 422.
    for path in schema["paths"].values():
        for operation in path.values():
            operation["responses"].pop("422", None)
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)
    return schema


app.openapi = openapi
