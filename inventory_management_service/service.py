from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inventory_management_service.models import Product
from inventory_management_service.schemas import CreateProduct, ProductQuery, ProductSort, SortDirection


class DuplicateSku(Exception):
    def __init__(self, sku: str):
        self.sku = sku


class ProductNotFound(Exception):
    def __init__(self, product_id: UUID):
        self.product_id = product_id


class ProductsNotFound(Exception):
    """Products of a batch that do not exist or are already deleted, in request order."""

    def __init__(self, product_ids: list[UUID]):
        self.product_ids = product_ids


@dataclass(frozen=True)
class ProductSlice:
    items: list[Product]
    total: int


SORT_COLUMNS = {
    ProductSort.NAME: Product.name,
    ProductSort.SKU: Product.sku,
    ProductSort.CATEGORY: Product.category,
    ProductSort.PRICE: Product.price_amount,
    ProductSort.CREATED_AT: Product.created_at,
    ProductSort.UPDATED_AT: Product.updated_at,
}


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


def get_product(session: Session, product_id: UUID) -> Product:
    with session.begin():
        product = session.scalar(
            select(Product).where(Product.id == product_id, Product.deleted_at.is_(None))
        )
    if product is None:
        # Soft-deleted products are indistinguishable from missing ones.
        raise ProductNotFound(product_id)
    return product


def list_products(session: Session, query: ProductQuery) -> ProductSlice:
    conditions = [Product.deleted_at.is_(None)]
    for term in (query.search or "").split():
        conditions.append(or_(
            Product.sku.icontains(term, autoescape=True),
            Product.name.icontains(term, autoescape=True),
            Product.description.icontains(term, autoescape=True),
        ))
    if query.category is not None:
        conditions.append(func.lower(Product.category) == func.lower(query.category))
    if query.min_price is not None:
        conditions.append(Product.price_amount >= query.min_price)
    if query.max_price is not None:
        conditions.append(Product.price_amount <= query.max_price)
    if query.currency is not None:
        conditions.append(Product.price_currency == query.currency)

    column = SORT_COLUMNS[query.sort]
    # id is unique, so the order is total and deterministic. Offsets are applied to the
    # data as it is at each request; changes between requests can shift later pages.
    if query.direction is SortDirection.DESC:
        order = (column.desc(), Product.id.desc())
    else:
        order = (column.asc(), Product.id.asc())
    offset = (query.page - 1) * query.size

    with session.begin():
        # One snapshot for the count and the page keeps this response's totals consistent.
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        total = session.scalar(select(func.count()).select_from(Product).where(*conditions))
        items = []
        if offset < total:
            items = list(session.scalars(
                select(Product).where(*conditions).order_by(*order).offset(offset).limit(query.size)
            ))
    return ProductSlice(items, total)


def soft_delete_products(session: Session, product_ids: list[UUID]) -> None:
    """Mark every listed product as deleted in one transaction: all of them or none.

    The rows stay stored. Each gets status DELETED and deleted_at, which
    ck_products_deleted_state requires together, so Get and List stop returning it
    while its SKU stays reserved. If any listed product does not exist or is already
    deleted, nothing changes and ProductsNotFound lists every such ID.
    """
    with session.begin():
        # READ COMMITTED whatever the database or role default: a request that waits
        # for a row that a concurrent request deletes then finds it deleted (404)
        # instead of failing with a serialization error.
        session.connection(execution_options={"isolation_level": "READ COMMITTED"})
        # Lock the rows in id order, so concurrent requests with overlapping IDs
        # cannot deadlock.
        products = list(session.scalars(
            select(Product)
            .where(Product.id.in_(product_ids), Product.deleted_at.is_(None))
            .order_by(Product.id)
            .with_for_update()
        ))
        found = {product.id for product in products}
        missing = [product_id for product_id in product_ids if product_id not in found]
        if missing:
            # Raising inside the transaction rolls it back and releases the locks.
            raise ProductsNotFound(missing)
        timestamp = datetime.now(UTC)
        for product in products:
            # Never move updated_at backwards, even if another server's clock was ahead
            # when it last wrote the row. This also keeps deleted_at >= created_at.
            deleted_at = max(timestamp, product.updated_at)
            product.status = "DELETED"
            product.deleted_at = deleted_at
            product.updated_at = deleted_at
        # The commit flushes one UPDATE per product, and the mapper increments version.
