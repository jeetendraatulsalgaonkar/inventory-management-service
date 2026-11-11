import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from iso4217 import Currency
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr, field_validator


def postgres_text(value: str) -> str:
    if "\0" in value or re.search(r"[\ud800-\udfff]", value):
        raise ValueError("must contain valid Unicode text without NUL")
    return value


def nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


Text = Annotated[StrictStr, AfterValidator(postgres_text)]
Nonblank = Annotated[Text, AfterValidator(nonblank)]
AttributeKey = Annotated[Nonblank, Field(max_length=64)]
AttributeValue = Annotated[Text, Field(max_length=255)]


class Unit(StrEnum):
    PIECE = "PIECE"
    BOX = "BOX"
    PACK = "PACK"
    PALLET = "PALLET"
    GRAM = "GRAM"
    KILOGRAM = "KILOGRAM"
    MILLILITER = "MILLILITER"
    LITER = "LITER"
    CENTIMETER = "CENTIMETER"
    METER = "METER"
    SQUARE_METER = "SQUARE_METER"
    CUBIC_METER = "CUBIC_METER"


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(ge=0, max_digits=19, decimal_places=4, allow_inf_nan=False)
    currency: StrictStr = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("amount", mode="before")
    @classmethod
    def decimal_input(cls, value):
        if type(value) not in (int, Decimal, str):
            raise ValueError("must be a decimal amount")
        if isinstance(value, str) and not re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value):
            raise ValueError("must be a decimal amount")
        return value

    @field_validator("amount")
    @classmethod
    def raw_scale(cls, value: Decimal) -> Decimal:
        if value.as_tuple().exponent < -4:
            raise ValueError("must have at most 4 fractional digits")
        return value

    @field_validator("currency")
    @classmethod
    def iso_currency(cls, value: str) -> str:
        Currency(value)
        return value


class CreateProduct(BaseModel):
    model_config = ConfigDict(extra="forbid", loc_by_alias=True)

    sku: Nonblank = Field(max_length=64)
    name: Nonblank = Field(max_length=255)
    description: Text | None = Field(default=None, max_length=2000)
    category: Nonblank = Field(max_length=100)
    price: Price
    unit_of_measure: Unit = Field(alias="unitOfMeasure")
    attributes: dict[AttributeKey, AttributeValue] = Field(max_length=50)

    @field_validator("sku")
    @classmethod
    def canonical_sku(cls, value: str) -> str:
        stripped = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", stripped):
            raise ValueError("must start with a letter or digit and use only letters, digits, '.', '_' or '-'")
        return stripped.upper()


MAX_DELETE_BATCH = 100


class DeleteProducts(BaseModel):
    """Body of the soft-delete request: the IDs of the products to mark as deleted."""

    model_config = ConfigDict(extra="forbid", loc_by_alias=True)

    product_ids: list[UUID] = Field(
        alias="productIds",
        min_length=1,
        max_length=MAX_DELETE_BATCH,
        description=f"IDs of the products to soft-delete: 1 to {MAX_DELETE_BATCH}, each at most once.",
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("product_ids")
    @classmethod
    def distinct(cls, value: list[UUID]) -> list[UUID]:
        # Compare parsed UUIDs, so different spellings of one ID also count as a repeat.
        seen, repeated = set(), []
        for product_id in value:
            if product_id in seen and product_id not in repeated:
                repeated.append(product_id)
            seen.add(product_id)
        if repeated:
            raise ValueError("must not repeat a product ID: " + ", ".join(map(str, repeated)))
        return value


class ProductResponse(CreateProduct):
    id: UUID
    # Create always returns ACTIVE. Reads can also return the other non-deleted
    # statuses allowed by ck_products_status; DELETED products are never returned.
    status: Literal["ACTIVE", "INACTIVE", "DISCONTINUED"]
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    version: int


class ProductSort(StrEnum):
    NAME = "name"
    SKU = "sku"
    CATEGORY = "category"
    PRICE = "price"
    CREATED_AT = "createdAt"
    UPDATED_AT = "updatedAt"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


def blank_to_none(value: str | None) -> str | None:
    return (value or "").strip() or None


Amount = Annotated[Decimal, Field(ge=0, max_digits=19, decimal_places=4, allow_inf_nan=False)]


class ProductQuery(BaseModel):
    """Query parameters of the product list. Unknown parameters are rejected."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(1, ge=1, le=2_147_483_647, description="1-based page number.")
    size: int = Field(20, ge=1, le=100, description="Products per page, at most 100.")
    search: Annotated[Text | None, AfterValidator(blank_to_none)] = Field(
        None,
        max_length=255,
        description=(
            "Case-insensitive search. Every whitespace-separated term must occur in the "
            "SKU, name, or description. Blank means no filter."
        ),
    )
    category: Annotated[Text | None, AfterValidator(blank_to_none)] = Field(
        None, max_length=100, description="Exact category, case-insensitive. Blank means no filter.",
    )
    min_price: Amount | None = Field(None, alias="minPrice", description="Inclusive lower bound on price.amount.")
    max_price: Amount | None = Field(None, alias="maxPrice", description="Inclusive upper bound on price.amount.")
    currency: StrictStr | None = Field(
        None,
        pattern=r"^[A-Z]{3}$",
        description="ISO 4217 price currency. Price bounds compare amounts without conversion.",
    )
    sort: ProductSort = Field(ProductSort.NAME, description="Sort field. Ties are broken by id.")
    direction: SortDirection = Field(SortDirection.ASC, description="Sort direction.")

    @field_validator("currency")
    @classmethod
    def iso_currency(cls, value: str | None) -> str | None:
        if value is not None:
            Currency(value)
        return value


class ProductPage(BaseModel):
    items: list[ProductResponse]
    page: int
    size: int
    total_items: int = Field(alias="totalItems")
    total_pages: int = Field(alias="totalPages")
