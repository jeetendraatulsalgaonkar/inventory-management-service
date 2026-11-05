import json
import logging
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import Annotated

import simplejson
from fastapi import Depends, FastAPI, Request, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from inventory_management_service.db import get_session
from inventory_management_service.schemas import CreateProduct, ProductResponse
from inventory_management_service.service import DuplicateSku, create_product

app = FastAPI(title="Inventra Inventory Management", version="0.1.0")
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


problem_responses = {
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
                        "sku": {"type": "string"},
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
    for code, description in {
        400: "Malformed body or validation failure",
        409: "Duplicate normalized SKU",
        415: "Unsupported media type",
        500: "Unexpected server error",
    }.items()
}

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
    result = ProductResponse(
        **command.model_dump(by_alias=True, exclude={"name", "description", "category"}),
        id=product.id,
        name=product.name,
        description=product.description,
        category=product.category,
        status=product.status,
        createdAt=product.created_at,
        updatedAt=product.updated_at,
        version=product.version,
    )
    data = result.model_dump(mode="json", by_alias=True)
    # Keep the decimal exact and emit a JSON number, as in the original API.
    data["price"]["amount"] = Decimal(format(product.price_amount.normalize(), "f"))
    base = str(request.base_url).rstrip("/")
    return Response(
        simplejson.dumps(data, use_decimal=True),
        status_code=201,
        media_type="application/json",
        headers={"Location": f"{base}{PRODUCTS}/{product.id}"},
    )


default_openapi = app.openapi


def openapi():
    schema = default_openapi()
    components = schema["components"]["schemas"]
    for name, definition in request_definitions.items():
        # Price has a distinct response representation. Unit is shared.
        components.setdefault(name, definition)
    components["ProductResponse"]["properties"]["price"] = {
        "$ref": "#/components/schemas/PriceResponse"
    }
    components["PriceResponse"] = {
        "type": "object",
        "required": ["amount", "currency"],
        "properties": {
            "amount": {"type": "number", "minimum": 0},
            "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        },
    }
    components["Price"] = request_definitions["Price"]
    return schema


app.openapi = openapi
