"""Unit tests for the service layer. No database is used.

The services run against FakeSession (see conftest.py), which records every call.
Statements are inspected structurally with SQLAlchemy's ClauseElement.compare(),
which ignores SQL formatting and parameter naming. Only rules that must hold even
when PostgreSQL is unavailable are checked here: the soft-delete filter, identical
criteria for the count and the page, the id tiebreaker, and the snapshot. What the
SQL actually returns (search, case-insensitive category, price bounds, paging, and
ordering) is verified against PostgreSQL in test_postgres.py.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid7

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.sql.elements import BooleanClauseList

from inventory_management_service.models import Product
from inventory_management_service.schemas import CreateProduct, ProductQuery
from inventory_management_service.service import (
    DuplicateSku,
    ProductNotFound,
    ProductSlice,
    create_product,
    get_product,
    list_products,
)

SORT_COLUMNS = {
    "name": "name", "sku": "sku", "category": "category", "price": "price_amount",
    "createdAt": "created_at", "updatedAt": "updated_at",
}


NOT_DELETED = Product.deleted_at.is_(None)


def criteria(statement):
    """The AND-ed WHERE criteria of a statement, in no particular order."""
    where = statement.whereclause
    return list(where.clauses) if isinstance(where, BooleanClauseList) else [where]


def has_criterion(statement, expected):
    # compare() is structural and includes bound values, so formatting is irrelevant.
    return any(criterion.compare(expected) for criterion in criteria(statement))


def order_keys(statement):
    """ORDER BY as [(column, direction)]. Select has no public accessor for it."""
    sql = str(statement.compile(dialect=postgresql.dialect()))
    clause = re.search(r"ORDER BY (.*?)(?: LIMIT| OFFSET|$)", sql, re.S).group(1)
    return [tuple(key.split()) for key in clause.split(",")]


def query(**params) -> ProductQuery:
    return ProductQuery.model_validate(params)


def integrity_error(sqlstate, constraint):
    """An IntegrityError whose driver error looks like psycopg's."""
    original = SimpleNamespace(sqlstate=sqlstate, diag=SimpleNamespace(constraint_name=constraint))
    return IntegrityError("INSERT INTO products ...", {}, original)


# ---------------------------------------------------------------------------
# create_product
# ---------------------------------------------------------------------------

def test_create_maps_the_command_to_a_new_product(fake_session, payload):
    command = CreateProduct.model_validate(payload)
    before = datetime.now(UTC)
    product = create_product(fake_session, command)

    # Persisted: added inside a transaction (FakeSession enforces that) that committed.
    assert fake_session.added == [product]
    assert ("commit",) in fake_session.events
    assert product.id.version == 7
    assert product.sku == "LAP-DELL-1"
    assert product.name == "Dell XPS"                 # trimmed
    assert product.description is None                # blank becomes null
    assert product.category == "ELECTRONICS"          # trimmed
    assert product.price_amount == Decimal("999999999999999.9999")
    assert product.price_currency == "EUR"
    assert product.unit_of_measure == "PIECE"
    assert product.attributes == {"brand": "Dell"}
    assert product.status == "ACTIVE"
    assert product.deleted_at is None
    assert product.version == 0
    assert product.created_at == product.updated_at
    assert product.created_at.tzinfo is UTC
    assert before <= product.created_at <= datetime.now(UTC)


def test_create_trims_the_description(fake_session, payload):
    command = CreateProduct.model_validate(payload | {"description": "  15-inch laptop  "})
    assert create_product(fake_session, command).description == "15-inch laptop"


def test_create_keeps_a_missing_description_null(fake_session, payload):
    del payload["description"]
    assert create_product(fake_session, CreateProduct.model_validate(payload)).description is None


def test_create_assigns_distinct_time_ordered_ids(fake_session, payload):
    command = CreateProduct.model_validate(payload)
    ids = [create_product(fake_session, command).id for _ in range(50)]
    assert len(set(ids)) == 50
    assert ids == sorted(ids)                          # UUIDv7 sorts by creation time


def test_create_translates_a_duplicate_sku(fake_session, payload):
    fake_session.commit_error = integrity_error("23505", "uk_products_sku")
    with pytest.raises(DuplicateSku) as raised:
        create_product(fake_session, CreateProduct.model_validate(payload))
    assert raised.value.sku == "LAP-DELL-1"           # the canonical SKU
    assert isinstance(raised.value.__cause__, IntegrityError)


@pytest.mark.parametrize(("sqlstate", "constraint"), [
    ("23505", "pk_products"),                          # unique violation of another constraint
    ("23514", "ck_products_name_not_blank"),           # check violation
    ("23502", None),                                   # not-null violation
    (None, None),                                      # driver error without a SQLSTATE
])
def test_create_reraises_other_integrity_errors(fake_session, payload, sqlstate, constraint):
    fake_session.commit_error = integrity_error(sqlstate, constraint)
    with pytest.raises(IntegrityError):
        create_product(fake_session, CreateProduct.model_validate(payload))


def test_create_propagates_database_failures(fake_session, payload):
    fake_session.commit_error = OperationalError("INSERT", {}, Exception("server closed the connection"))
    with pytest.raises(OperationalError):
        create_product(fake_session, CreateProduct.model_validate(payload))


# ---------------------------------------------------------------------------
# get_product
# ---------------------------------------------------------------------------

def test_get_returns_the_product(fake_session):
    product = Product(id=uuid7())
    fake_session.results.append(product)
    assert get_product(fake_session, product.id) is product


def test_get_looks_up_the_requested_id_and_excludes_deleted_products(fake_session):
    product_id = uuid7()
    fake_session.results.append(Product(id=product_id))
    get_product(fake_session, product_id)
    (statement,) = fake_session.statements
    assert has_criterion(statement, Product.id == product_id)
    assert not has_criterion(statement, Product.id == uuid7())
    assert has_criterion(statement, NOT_DELETED)


def test_get_raises_not_found_for_missing_or_deleted_products(fake_session):
    # The query excludes deleted rows, so both cases arrive as "no row".
    product_id = uuid7()
    fake_session.results.append(None)
    with pytest.raises(ProductNotFound) as raised:
        get_product(fake_session, product_id)
    assert raised.value.product_id == product_id


# ---------------------------------------------------------------------------
# list_products
# ---------------------------------------------------------------------------

def run_list(fake_session, total, rows=(), **params):
    fake_session.results += [total, list(rows)]
    return list_products(fake_session, query(**params))


def test_list_reads_count_and_page_from_one_snapshot(fake_session):
    run_list(fake_session, total=1, rows=[Product(id=uuid7())])
    kinds = [event[0] for event in fake_session.events]
    # One transaction holds both queries (FakeSession rejects queries outside one) ...
    assert kinds.count("begin") == 1
    assert kinds.count("scalar") + kinds.count("scalars") == 2
    # ... and REPEATABLE READ is chosen before its first query, when it can still apply.
    first_query = min(kinds.index("scalar"), kinds.index("scalars"))
    assert ("connection", {"isolation_level": "REPEATABLE READ"}) in fake_session.events[:first_query]


def test_list_returns_the_page_and_total(fake_session):
    rows = [Product(id=uuid7()), Product(id=uuid7())]
    assert run_list(fake_session, total=41, rows=rows) == ProductSlice(rows, 41)


@pytest.mark.parametrize("params", [
    {},
    {"search": "   ", "category": ""},
    {"search": "ring binder", "category": "Office_Supplies", "minPrice": "10", "maxPrice": "50.5", "currency": "EUR"},
])
def test_list_count_and_page_use_identical_criteria(fake_session, params):
    # totalItems must describe the same rows that are paged through.
    run_list(fake_session, total=1, **params)
    count, page = fake_session.statements
    assert has_criterion(count, NOT_DELETED)
    assert count.whereclause.compare(page.whereclause)


def test_list_without_filters_only_excludes_deleted_products(fake_session):
    run_list(fake_session, total=0, search="   ", category="")
    count = fake_session.statements[0]
    assert count.whereclause.compare(NOT_DELETED)


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("sort", list(SORT_COLUMNS))
def test_list_breaks_ties_by_id_in_the_sort_direction(fake_session, sort, direction):
    run_list(fake_session, total=1, sort=sort, direction=direction)
    keyword = direction.upper()
    assert order_keys(fake_session.statements[1]) == [
        (f"products.{SORT_COLUMNS[sort]}", keyword), ("products.id", keyword),
    ]
