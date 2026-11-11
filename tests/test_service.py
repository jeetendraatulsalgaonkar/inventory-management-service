"""Unit tests for the service layer. No database is used.

The services run against FakeSession (see conftest.py), which records every call.
Statements are inspected structurally with SQLAlchemy's ClauseElement.compare(),
which ignores SQL formatting and parameter naming. Only rules that must hold even
when PostgreSQL is unavailable are checked here. For creation: every product of a
request, however many, streamed with one COPY in one transaction, one creation time,
ids in request order, and the translation of a unique violation into every taken
SKU. For reads: the soft-delete filter, identical
criteria for the count and the page, the id tiebreaker, and the snapshot. For soft
deletion: all or nothing, the state each product is left in, its timestamps, and the
locking query. What the SQL actually returns (search, case-insensitive category,
price bounds, paging, and ordering) and how deletions behave under concurrency are
verified against PostgreSQL in test_postgres.py.
"""

import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid7

import psycopg
import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import String, any_, literal
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.elements import BooleanClauseList

from inventory_management_service.models import Product
from inventory_management_service.schemas import CreateProduct, ProductQuery
from inventory_management_service.service import (
    DuplicateSkus,
    ProductNotFound,
    ProductsNotFound,
    ProductSlice,
    COPY_COLUMNS,
    COPY_PRODUCTS,
    create_products,
    get_product,
    list_products,
    soft_delete_products,
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
    clause = re.search(r"ORDER BY (.*?)(?: LIMIT| OFFSET| FOR UPDATE|$)", sql, re.S).group(1)
    return [tuple(key.split()) for key in clause.split(",")]


def query(**params) -> ProductQuery:
    return ProductQuery.model_validate(params)


def integrity_error(error_class=psycopg.errors.UniqueViolation, constraint="uk_products_sku"):
    """A psycopg integrity error, as COPY raises it, naming the violated constraint."""

    class Violation(error_class):
        @property
        def diag(self):
            return SimpleNamespace(constraint_name=constraint)

    return Violation(f"violates constraint {constraint}")


# ---------------------------------------------------------------------------
# create_products
# ---------------------------------------------------------------------------

def commands(payload, *skus):
    """Validated create commands that differ only in their SKUs."""
    return [CreateProduct.model_validate(payload | {"sku": sku}) for sku in skus]


def many_commands(payload, count):
    """`count` commands with the canonical SKUs SKU-0, SKU-1, ...: one validated command,
    copied with a new SKU, which keeps tests of large requests fast."""
    command = CreateProduct.model_validate(payload)
    return [command.model_copy(update={"sku": f"SKU-{n}"}) for n in range(count)]


def copied(fake_session):
    """The rows of every COPY, as dicts keyed by column."""
    return [dict(zip(COPY_COLUMNS, row)) for row in fake_session.copied]


def test_create_maps_the_command_to_a_new_product(fake_session, payload):
    command = CreateProduct.model_validate(payload)
    before = datetime.now(UTC)
    (product,) = create_products(fake_session, [command])

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
    # Stored: the row written by COPY holds exactly the returned values.
    (row,) = copied(fake_session)
    attributes = row.pop("attributes")
    assert isinstance(attributes, Jsonb) and attributes.obj == {"brand": "Dell"}
    assert row == {column: getattr(product, column) for column in row}


def test_create_copies_every_product_in_one_transaction(fake_session, payload):
    products = create_products(fake_session, commands(payload, "A-1", "B-2", "C-3"))
    assert [product.sku for product in products] == ["A-1", "B-2", "C-3"]    # request order
    assert [row["sku"] for row in copied(fake_session)] == ["A-1", "B-2", "C-3"]
    kinds = [event[0] for event in fake_session.events]
    assert kinds == ["begin", "connection", "copy", "commit"]     # one COPY, one transaction
    (copy,) = [event for event in fake_session.events if event[0] == "copy"]
    assert copy[1] == COPY_PRODUCTS
    assert fake_session.statements == []            # no query when nothing conflicts


def test_copy_writes_every_column_that_create_sets():
    assert COPY_PRODUCTS == (
        "COPY products (id, sku, name, description, category, price_amount, price_currency, "
        "unit_of_measure, attributes, status, created_at, updated_at, version) FROM STDIN"
    )
    # Every mapped column except deleted_at, which stays NULL for a new product.
    assert set(COPY_COLUMNS) == {column.name for column in Product.__table__.columns} - {"deleted_at"}


@pytest.mark.parametrize("count", [1, 100, 101, 10_000])
def test_create_has_no_batch_limit(fake_session, payload, count):
    products = create_products(fake_session, many_commands(payload, count))
    assert len(products) == len(fake_session.copied) == count
    # However many products, one COPY in one transaction.
    assert [event[0] for event in fake_session.events] == ["begin", "connection", "copy", "commit"]


def test_create_gives_a_request_one_time_and_ids_in_request_order(fake_session, payload):
    products = create_products(fake_session, many_commands(payload, 5000))
    # One creation time for the request; UUIDv7 ids then keep the request order when
    # products are sorted by createdAt (ties broken by id) or by id.
    assert len({(product.created_at, product.updated_at) for product in products}) == 1
    ids = [product.id for product in products]
    assert len(set(ids)) == 5000
    assert ids == sorted(ids)


def test_create_gives_products_of_separate_requests_distinct_ids(fake_session, payload):
    ids = [create_products(fake_session, commands(payload, "A-1"))[0].id for _ in range(50)]
    assert len(set(ids)) == 50
    assert ids == sorted(ids)                          # UUIDv7 sorts by creation time


def test_create_trims_the_description(fake_session, payload):
    command = CreateProduct.model_validate(payload | {"description": "  15-inch laptop  "})
    assert create_products(fake_session, [command])[0].description == "15-inch laptop"


def test_create_keeps_a_missing_description_null(fake_session, payload):
    del payload["description"]
    assert create_products(fake_session, [CreateProduct.model_validate(payload)])[0].description is None


def test_create_maps_each_command_separately(fake_session, payload):
    first = CreateProduct.model_validate(payload)
    second = CreateProduct.model_validate(payload | {
        "sku": "box-1", "name": "Box", "description": " Cardboard ", "category": "PACKAGING",
        "price": {"amount": "0.5", "currency": "USD"}, "unitOfMeasure": "BOX", "attributes": {},
    })
    a, b = create_products(fake_session, [first, second])
    assert (a.sku, a.name, a.description, a.unit_of_measure) == ("LAP-DELL-1", "Dell XPS", None, "PIECE")
    assert (b.sku, b.name, b.description, b.category) == ("BOX-1", "Box", "Cardboard", "PACKAGING")
    assert (b.price_amount, b.price_currency, b.unit_of_measure, b.attributes) == (Decimal("0.5"), "USD", "BOX", {})
    assert [row["attributes"].obj for row in copied(fake_session)] == [{"brand": "Dell"}, {}]


def test_create_of_no_products_touches_nothing(fake_session):
    assert create_products(fake_session, []) == []
    assert fake_session.events == []


def test_create_refuses_repeated_skus_without_touching_the_database(fake_session, payload):
    # The API rejects these with 400. Called directly, the service must not report
    # its own repeat as a conflict with stored data.
    with pytest.raises(ValueError, match="distinct"):
        create_products(fake_session, commands(payload, "A-1", "B-2", " a-1 "))
    assert fake_session.events == []


def sku_lookup_matches(statement, skus):
    """The lookup selects the SKU of every stored product with one of `skus`, deleted or not,
    passing the SKUs as one array parameter."""
    return (
        [column.name for column in statement.selected_columns] == ["sku"]
        and statement.whereclause.compare(Product.sku == any_(literal(skus, postgresql.ARRAY(String))))
    )


def test_create_translates_a_duplicate_sku_into_every_taken_sku(fake_session, payload):
    fake_session.commit_error = integrity_error()
    # The lookup after the rollback finds two of the requested SKUs, in any order.
    fake_session.results.append(["D-4", "B-2"])
    with pytest.raises(DuplicateSkus) as raised:
        create_products(fake_session, commands(payload, "A-1", "B-2", "C-3", "d-4"))
    assert raised.value.skus == ["B-2", "D-4"]        # canonical SKUs, in request order
    assert fake_session.copied == []                  # the COPY's transaction rolled back
    assert isinstance(raised.value.__cause__, psycopg.errors.UniqueViolation)
    # The COPY rolled back; the lookup ran in a new transaction, after it.
    kinds = [event[0] for event in fake_session.events]
    assert kinds == ["begin", "connection", "copy", "rollback", "begin", "scalars", "commit"]
    (lookup,) = fake_session.statements
    # Soft-deleted products keep their SKU reserved, so the lookup does not filter them out.
    assert sku_lookup_matches(lookup, ["A-1", "B-2", "C-3", "D-4"])
    assert not sku_lookup_matches(lookup, ["A-1", "B-2", "C-3"])


def test_create_looks_up_any_number_of_skus_with_one_parameter(fake_session, payload):
    # PostgreSQL accepts at most 65,535 bind parameters per statement.
    skus = [f"SKU-{n}" for n in range(70_000)]
    fake_session.commit_error = integrity_error()
    fake_session.results.append(["SKU-69999"])
    with pytest.raises(DuplicateSkus) as raised:
        create_products(fake_session, many_commands(payload, len(skus)))
    assert raised.value.skus == ["SKU-69999"]
    (lookup,) = fake_session.statements
    compiled = lookup.compile(dialect=postgresql.dialect())
    assert len(compiled.params) == 1
    assert list(compiled.params.values()) == [skus]


def test_create_reraises_a_sku_violation_when_no_sku_is_taken(fake_session, payload):
    # Products are never physically deleted, so the lookup should find the conflict.
    # If it does not, the failure is unexpected and must not be hidden.
    error = integrity_error()
    fake_session.commit_error = error
    fake_session.results.append([])
    with pytest.raises(psycopg.IntegrityError) as raised:
        create_products(fake_session, commands(payload, "A-1"))
    assert raised.value is error


@pytest.mark.parametrize(("error_class", "constraint"), [
    (psycopg.errors.UniqueViolation, "pk_products"),                 # unique violation of another constraint
    (psycopg.errors.CheckViolation, "ck_products_name_not_blank"),   # check violation
    (psycopg.errors.NotNullViolation, None),                         # not-null violation
    (psycopg.IntegrityError, None),                                  # driver error without a SQLSTATE
])
def test_create_reraises_other_integrity_errors(fake_session, payload, error_class, constraint):
    fake_session.commit_error = integrity_error(error_class, constraint)
    with pytest.raises(error_class):
        create_products(fake_session, commands(payload, "A-1", "B-2"))
    assert fake_session.statements == []              # no SKU lookup


@pytest.mark.parametrize("error", [
    psycopg.OperationalError("server closed the connection"),       # raised by COPY on the driver
    OperationalError("COMMIT", {}, Exception("server closed the connection")),
], ids=["driver", "sqlalchemy"])
def test_create_propagates_database_failures(fake_session, payload, error):
    fake_session.commit_error = error
    with pytest.raises(type(error)):
        create_products(fake_session, commands(payload, "A-1"))
    assert fake_session.statements == []


def test_create_propagates_a_failed_sku_lookup(fake_session, payload):
    fake_session.commit_error = integrity_error()
    lost = OperationalError("SELECT", {}, Exception("server closed the connection"))
    fake_session.results.append(lost)
    with pytest.raises(OperationalError) as raised:
        create_products(fake_session, commands(payload, "A-1"))
    assert raised.value is lost
    assert ("commit",) not in fake_session.events


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


# ---------------------------------------------------------------------------
# soft_delete_products
# ---------------------------------------------------------------------------

CREATED = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)


def active_product(**changes) -> Product:
    """A stored product that is not deleted, as the locking query returns it."""
    values = dict(
        id=uuid7(), sku="LAP-DELL-1", status="ACTIVE", created_at=CREATED,
        updated_at=CREATED + timedelta(days=1), deleted_at=None, version=3,
    )
    return Product(**values | changes)


def run_delete(fake_session, requested, found):
    """Soft-delete the `requested` IDs while the locking query returns the `found` rows."""
    fake_session.results.append(found)
    return soft_delete_products(fake_session, requested)


def test_delete_marks_every_product_deleted_in_one_transaction(fake_session):
    products = [active_product(), active_product(status="INACTIVE"), active_product(status="DISCONTINUED")]
    before = datetime.now(UTC)
    run_delete(fake_session, [product.id for product in products], products)
    after = datetime.now(UTC)

    kinds = [event[0] for event in fake_session.events]
    assert kinds.count("begin") == 1
    assert kinds[-1] == "commit"
    # Only the status and the timestamps change: rows are kept, not removed.
    for product in products:
        assert product.status == "DELETED"
        assert before <= product.deleted_at <= after
        assert product.updated_at == product.deleted_at
        assert product.created_at == CREATED
        assert product.version == 3                     # left to the mapper, which increments it on flush
    # One deletion time for the whole batch.
    assert len({product.deleted_at for product in products}) == 1


def test_delete_locks_the_requested_products_that_are_not_deleted(fake_session):
    ids = [uuid7(), uuid7()]
    run_delete(fake_session, ids, [active_product(id=product_id) for product_id in ids])
    (statement,) = fake_session.statements
    assert has_criterion(statement, Product.id.in_(ids))
    assert not has_criterion(statement, Product.id.in_(ids[:1]))
    assert has_criterion(statement, NOT_DELETED)
    # Locking in id order keeps concurrent requests with overlapping IDs free of deadlocks.
    assert order_keys(statement) == [("products.id",)]
    assert str(statement.compile(dialect=postgresql.dialect())).endswith(" FOR UPDATE")


def test_delete_runs_at_read_committed(fake_session):
    product = active_product()
    run_delete(fake_session, [product.id], [product])
    kinds = [event[0] for event in fake_session.events]
    # Chosen before the first query, when it can still apply to the transaction.
    assert ("connection", {"isolation_level": "READ COMMITTED"}) in fake_session.events[:kinds.index("scalars")]


@pytest.mark.parametrize("found", [[], [1], [0, 2]], ids=["none-found", "one-found", "two-found"])
def test_delete_with_missing_products_changes_nothing(fake_session, found):
    # The query skips deleted rows, so missing and already deleted products look alike.
    products = [active_product() for _ in range(3)]
    with pytest.raises(ProductsNotFound) as raised:
        run_delete(fake_session, [product.id for product in products], [products[index] for index in found])
    # Every missing ID is reported, in request order.
    assert raised.value.product_ids == [product.id for index, product in enumerate(products) if index not in found]
    assert ("commit",) not in fake_session.events
    for product in products:
        assert (product.status, product.deleted_at, product.updated_at) == ("ACTIVE", None, CREATED + timedelta(days=1))


def test_delete_reports_missing_ids_in_request_order(fake_session):
    first, second, third = uuid7(), uuid7(), uuid7()
    with pytest.raises(ProductsNotFound) as raised:
        run_delete(fake_session, [third, first, second], [active_product(id=first)])
    assert raised.value.product_ids == [third, second]


def test_delete_never_moves_updated_at_backwards(fake_session):
    # Another server whose clock runs ahead last wrote this row "in the future".
    future = datetime.now(UTC) + timedelta(hours=1)
    ahead, current = active_product(updated_at=future), active_product()
    before = datetime.now(UTC)
    run_delete(fake_session, [ahead.id, current.id], [ahead, current])
    assert ahead.deleted_at == ahead.updated_at == future
    assert before <= current.deleted_at == current.updated_at <= datetime.now(UTC)


def test_delete_time_is_taken_after_the_rows_are_locked(fake_session):
    product = active_product()
    locked = []

    def rows():
        # The lock can wait for concurrent transactions; the deletion time must follow it.
        time.sleep(0.002)
        locked.append(datetime.now(UTC))
        yield product

    run_delete(fake_session, [product.id], rows())
    assert product.deleted_at >= locked[0]


def test_delete_propagates_database_failures(fake_session):
    product = active_product()
    fake_session.commit_error = OperationalError("UPDATE", {}, Exception("server closed the connection"))
    with pytest.raises(OperationalError):
        run_delete(fake_session, [product.id], [product])
