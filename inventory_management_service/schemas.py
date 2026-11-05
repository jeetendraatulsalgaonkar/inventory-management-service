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


class ProductResponse(CreateProduct):
    id: UUID
    status: Literal["ACTIVE"]
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    version: int
