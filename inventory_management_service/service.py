from datetime import UTC, datetime
from uuid import uuid7

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inventory_management_service.models import Product
from inventory_management_service.schemas import CreateProduct


class DuplicateSku(Exception):
    def __init__(self, sku: str):
        self.sku = sku


def create_product(session: Session, command: CreateProduct) -> Product:
    timestamp = datetime.now(UTC)
    product = Product(
        id=uuid7(),
        sku=command.sku,
        name=command.name.strip(),
        description=(command.description or "").strip() or None,
        category=command.category.strip(),
        price_amount=command.price.amount,
        price_currency=command.price.currency,
        unit_of_measure=command.unit_of_measure.value,
        attributes=command.attributes,
        status="ACTIVE",
        created_at=timestamp,
        updated_at=timestamp,
    )
    try:
        with session.begin():
            session.add(product)
    except IntegrityError as error:
        original = error.orig
        if (
            getattr(original, "sqlstate", None) == "23505"
            and original.diag.constraint_name == "uk_products_sku"
        ):
            raise DuplicateSku(command.sku) from error
        raise
    return product
