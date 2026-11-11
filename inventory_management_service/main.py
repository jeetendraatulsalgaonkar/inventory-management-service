import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from inventory_management_service.db import get_session
from inventory_management_service.migration import run_migrations
from inventory_management_service.models import Product
from inventory_management_service.schemas import (
    CreateProduct,
    CreateProducts,
    DeleteProducts,
    ProductPage,
    ProductQuery,
    ProductResponse,
)
from inventory_management_service.service import (
    DuplicateSkus,
    ProductNotFound,
    ProductsNotFound,
    create_products,
    get_product,
    list_products,
    soft_delete_products,
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
    """A 400 for the request body. ``malformed`` selects the problem type; ``detail``
    replaces the default detail of that type."""

    def __init__(self, errors=None, malformed=True, detail=None):
        self.errors = errors or []
        self.malformed = malformed
        self.detail = detail


@app.exception_handler(InvalidBody)
async def invalid_body(request, error):
    extensions = {"errors": error.errors} if error.errors else {}
    if error.malformed:
        return problem(
            request, 400, error.detail or "The request body is missing or has invalid JSON fields or types.",
            kind="malformed-request", title="Malformed request body", **extensions,
        )
    return problem(
        request, 400, error.detail or f"The request contains {len(error.errors)} invalid field(s).",
        kind="validation-error", title="Validation failed", **extensions,
    )


@app.exception_handler(DuplicateSkus)
async def duplicate(request, error):
    return problem(
        request, 409,
        f"Products with these SKUs already exist: {', '.join(error.skus)}. No product was created.",
        kind="duplicate-sku", title="Duplicate SKU", skus=error.skus,
    )


@app.exception_handler(ProductNotFound)
async def not_found(request, error):
    return problem(
        request, 404, f"No product with ID '{error.product_id}' exists.",
        kind="product-not-found", title="Product not found", productId=str(error.product_id),
    )


@app.exception_handler(ProductsNotFound)
async def products_not_found(request, error):
    # Like Get, this does not distinguish deleted products from ones that never existed.
    return problem(
        request, 404,
        f"{len(error.product_ids)} requested product(s) do not exist or are already deleted. "
        "No product was deleted.",
        kind="products-not-found", title="Products not found",
        productIds=[str(product_id) for product_id in error.product_ids],
    )


@app.exception_handler(RequestValidationError)
async def invalid_parameters(request, error):
    # Only path and query parameters reach FastAPI validation; parse_body validates
    # request bodies itself. Report them like invalid body fields.
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


MISSING = object()


async def read_body(request: Request, content: str) -> bytes:
    """Return the raw body, rejecting a body without a JSON media type (415).
    ``content`` names what the client sends, for the 415 detail."""
    body = await request.body()
    media = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if body and media != "application/json" and not (
        media.startswith("application/") and media.endswith("+json")
    ):
        raise HTTPException(415, f"Send the {content} as JSON.")
    return body


def parse_json(body: bytes, invalid_detail: str | None = None):
    """Return the parsed JSON body, or MISSING if it is empty or only whitespace."""
    if not body.strip():
        return MISSING
    try:
        return json.loads(body, parse_float=Decimal, parse_constant=reject_constant)
    except (ValueError, RecursionError, InvalidOperation) as error:
        raise InvalidBody(detail=invalid_detail) from error


def location_key(loc):
    """Sort key for error locations: list indexes numerically, so 2 precedes 10."""
    return tuple((0, part, "") if isinstance(part, int) else (1, 0, str(part)) for part in loc)


def validation_failure(error: ValidationError, message=None) -> InvalidBody:
    """Translate a Pydantic error into a 400 listing every invalid field.

    ``message(item)`` can replace the message of an error item by returning a string.
    """
    errors = error.errors(include_input=False, include_context=False, include_url=False)
    errors.sort(key=lambda item: (location_key(item["loc"]), item["msg"]))
    violations = [
        {
            # Invalid input can contain lone UTF-16 surrogates in property names.
            "field": ".".join(str(part) for part in item["loc"]).encode("utf-8", "replace").decode("utf-8"),
            "message": (message and message(item)) or item["msg"].removeprefix("Value error, "),
        }
        for item in errors
    ]
    malformed = any(
        item["type"] == "extra_forbidden" or item["type"].endswith("_type")
        for item in errors
    )
    return InvalidBody(violations, malformed)


async def parse_body[Model: BaseModel](request: Request, model: type[Model], content: str) -> Model:
    """Validate a JSON object body. A missing body, JSON null, or any other non-object
    is malformed. ``content`` names what the client sends, for the 415 detail."""
    payload = parse_json(await read_body(request, content))
    if not isinstance(payload, dict):
        raise InvalidBody()
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise validation_failure(error) from error


def json_type(value) -> str:
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    return "number"


def product_item_message(item) -> str | None:
    # A list element that is not an object: name it without internal model names.
    if len(item["loc"]) == 1 and item["type"] == "model_type":
        return "must be a product object"
    return None


# A request can hold any number of products, so a rejection could list millions of
# invalid fields. The response lists the first ones, in index order, and counts all.
MAX_LISTED_ERRORS = 1000
EXPECTED_PRODUCTS = "Send a JSON array of at least 1 product."


def validate_products(body: bytes) -> list[CreateProduct]:
    """Parse and validate the body of a create request: a non-empty JSON array of
    products with distinct SKUs. Every rejection names its cause.

    This is CPU-bound work proportional to the number of products, so it runs in a
    worker thread instead of the event loop, which keeps serving other requests.
    """
    payload = parse_json(body, f"The request body is not valid JSON. {EXPECTED_PRODUCTS}")
    if payload is MISSING:
        raise InvalidBody(detail=f"The request body is missing. {EXPECTED_PRODUCTS}")
    if payload is None:
        raise InvalidBody(detail=f"The request body is null. {EXPECTED_PRODUCTS}")
    if isinstance(payload, dict):
        raise InvalidBody(detail=(
            "The request body must be a JSON array of products, not a single product object. "
            "To create one product, send an array that contains it."
        ))
    if not isinstance(payload, list):
        raise InvalidBody(detail=f"The request body must be a JSON array of products, not a JSON {json_type(payload)}.")
    if not payload:
        raise InvalidBody(malformed=False, detail=f"The product list is empty. {EXPECTED_PRODUCTS}")
    try:
        products = CreateProducts.model_validate(payload)
    except ValidationError as error:
        failure = validation_failure(error, product_item_message)
    else:
        repeats = products.repeated_skus()
        if not repeats:
            return products.root
        failure = InvalidBody([
            {"field": f"{index}.sku", "message": f"repeats the SKU '{sku}' of product {first}"}
            for index, first, sku in repeats
        ], malformed=False)
    count = len(failure.errors)
    invalid_products = len({item["field"].partition(".")[0] for item in failure.errors})
    failure.detail = f"The request contains {count} invalid field(s) in {invalid_products} product(s)."
    if count > MAX_LISTED_ERRORS:
        failure.errors = failure.errors[:MAX_LISTED_ERRORS]
        failure.detail += f" The first {MAX_LISTED_ERRORS} are listed."
    failure.detail += " No product was created."
    raise failure


async def parse_products(request: Request) -> list[CreateProduct]:
    return await run_in_threadpool(validate_products, await read_body(request, "products"))


async def parse_product_ids(request: Request) -> DeleteProducts:
    return await parse_body(request, DeleteProducts, "product IDs")


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


# Parsing the body ourselves preserves every digit before validation. Move the
# generated request schema definitions into OpenAPI's shared component registry.
request_schema = CreateProducts.model_json_schema(ref_template="#/components/schemas/{model}")
request_definitions = request_schema.pop("$defs", {})


@app.post(
    PRODUCTS,
    status_code=201,
    response_model=list[ProductResponse],
    responses={
        201: {"description": "Every product was created. The items are in request order."},
        **problem_docs({
            400: "Missing, null, or empty body, not an array, repeated SKUs, or invalid fields. "
                 "Nothing was created.",
        }),
        **problem_docs(
            {409: "A SKU is already used, also by a soft-deleted product. Nothing was created."},
            skus={"type": "array", "items": {"type": "string"}},
        ),
        **problem_docs({415: "Unsupported media type", 500: "Unexpected server error"}),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": request_schema}},
        }
    },
    tags=["Products"],
    summary="Create products",
    description=(
        "Creates any number of products (at least one), sent as a JSON array. The request is atomic: "
        "if any product is invalid or its SKU is already used, nothing is created. The response "
        "lists the created products in request order; each can be read at /api/v1/products/{id}."
    ),
)
def post_products(
    commands: Annotated[list[CreateProduct], Depends(parse_products)],
    session: Annotated[Session, Depends(get_session)],
):
    products = create_products(session, commands)
    # Several resources are created, so there is no single Location.
    return json_response(products_json(products), status_code=201)


# The standard library's C encoder, with the same output as json.dumps defaults.
encode = json.JSONEncoder().encode


def exact_amount(amount: Decimal) -> str:
    """The amount as an exact JSON number literal, without insignificant zeros
    (249.0000 -> 249), never in exponent notation, and without losing any digit."""
    return format(amount.normalize(), "f")


@lru_cache(maxsize=4096)
def timestamp_json(value: datetime) -> str:
    # The driver returns timestamptz in the session time zone; the API emits UTC.
    # Cached: the products of one create request, for example, share one timestamp.
    return encode(value.astimezone(UTC).isoformat().replace("+00:00", "Z"))


def product_json(product: Product) -> str:
    """Serialize a product for every response: create, get by ID, and list items.

    Sharing one serializer keeps the representations identical. The id comes first
    so that clients and Swagger UI show the identifier before the product details.
    The JSON is assembled from values encoded by the standard library's C encoder,
    which keeps large responses fast. The price amount is written as an exact JSON
    number literal, which no standard encoder can do for a Decimal.
    """
    return (
        f'{{"id": "{product.id}", "sku": {encode(product.sku)}, '
        f'"name": {encode(product.name)}, "description": {encode(product.description)}, '
        f'"category": {encode(product.category)}, '
        f'"price": {{"amount": {exact_amount(product.price_amount)}, "currency": {encode(product.price_currency)}}}, '
        f'"unitOfMeasure": {encode(product.unit_of_measure)}, "attributes": {encode(product.attributes)}, '
        f'"status": {encode(product.status)}, "createdAt": {timestamp_json(product.created_at)}, '
        f'"updatedAt": {timestamp_json(product.updated_at)}, "version": {int(product.version)}}}'
    )


def products_json(products) -> str:
    return "[" + ", ".join(map(product_json, products)) + "]"


def json_response(content: str, status_code=200, headers=None) -> Response:
    return Response(content, status_code=status_code, media_type="application/json", headers=headers)


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
        "Ties in the sort order are broken by id, so the order is deterministic: while the "
        "data does not change, consecutive pages neither repeat nor omit products. Each page "
        "is computed when it is requested, so products created, updated, or soft-deleted "
        "between requests can make later pages repeat or omit products."
    ),
)
def get_products(
    query: Annotated[ProductQuery, Depends(product_query)],
    session: Annotated[Session, Depends(get_session)],
):
    result = list_products(session, query)
    return json_response(
        f'{{"items": {products_json(result.items)}, "page": {query.page}, "size": {query.size}, '
        f'"totalItems": {result.total}, "totalPages": {-(-result.total // query.size)}}}'
    )


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
    return json_response(product_json(get_product(session, product_id)))


delete_request_schema = DeleteProducts.model_json_schema(ref_template="#/components/schemas/{model}")


@app.delete(
    PRODUCTS,
    status_code=204,
    responses={
        204: {"description": "Every listed product was marked as deleted"},
        **problem_docs({400: "Missing or null body, no product IDs, or another invalid request"}),
        **problem_docs(
            {404: "A listed product does not exist or is already deleted. Nothing was deleted."},
            productIds={"type": "array", "items": {"type": "string", "format": "uuid"}},
        ),
        **problem_docs({415: "Unsupported media type", 500: "Unexpected server error"}),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": delete_request_schema}},
        }
    },
    tags=["Products"],
    summary="Soft-delete products",
    description=(
        "Marks every listed product as deleted: its status becomes DELETED. The products stay "
        "stored, but Get and List no longer return them, and their SKUs stay reserved. The "
        "request is atomic: if any listed product does not exist or is already deleted, "
        "nothing is deleted and the 404 response lists those IDs in productIds."
    ),
)
def delete_products(
    command: Annotated[DeleteProducts, Depends(parse_product_ids)],
    session: Annotated[Session, Depends(get_session)],
):
    soft_delete_products(session, command.product_ids)
    return Response(status_code=204)


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
    # product_json, so Swagger UI examples have the same order as real responses.
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
