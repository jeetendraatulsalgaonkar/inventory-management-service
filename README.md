# Inventory Management Service

Inventory Management Service is a product catalog API built with Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2.0, PostgreSQL 18, and Alembic. The first release implements **Create Product**. Python 3.14 supplies the standard-library UUIDv7 generator.

## Run with Docker

From the extracted project directory, with Docker Desktop or an equivalent Docker Engine and Compose running:

```powershell
docker compose up --build -d
docker compose logs -f api
```

Open [Swagger UI](http://localhost:8000/docs). Compose starts PostgreSQL on loopback port 5432, applies the Alembic migration, and serves the API on loopback port 8000. The Compose credentials are for local development. `docker compose down` stops the services and preserves the development database volume. Compose uses the existing `inventra_inventory-db` volume name so the renamed project can reuse local data.

## Run Python directly on Windows

Start a fresh PostgreSQL 18 database. The first command below starts just the development database in Docker. If you already have a local PostgreSQL instance, create an empty database and set the matching connection URL instead.

```powershell
docker compose up -d --wait db
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:DATABASE_URL = 'postgresql+psycopg://inventra:inventra_dev@localhost:5432/inventra'
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn inventory_management_service.main:app --reload
```

On macOS or Linux use `python3.14 -m venv .venv`, `.venv/bin/python`, and `export DATABASE_URL=...`. Run migrations from the project root. Revision `0001` is an Alembic baseline for an empty database. Existing Flyway-managed Inventra databases require a separate adoption plan. The baseline downgrade drops the products table and is intended for disposable databases.

## Create a product

```powershell
$body = @'
{
  "sku": " lap-dell-1 ",
  "name": "Dell XPS 15",
  "description": "15-inch business laptop",
  "category": "ELECTRONICS",
  "price": {"amount": 1499.99, "currency": "EUR"},
  "unitOfMeasure": "PIECE",
  "attributes": {"brand": "Dell"}
}
'@
$result = Invoke-WebRequest -Method Post -Uri http://localhost:8000/api/v1/products -ContentType application/json -Body $body
$result.StatusCode
$result.Headers.Location
$result.Content
```

- **201 Created** returns the persisted product and an absolute `Location` for the future GET endpoint. The body includes a UUIDv7 `id`, canonical `sku`, `status: ACTIVE`, UTC `createdAt` and `updatedAt`, and optimistic-locking `version: 0`.
- **400 Bad Request** covers malformed JSON, missing fields, invalid values, and unknown properties. Field-level errors include `errors` entries with `field` and `message`.
- **409 Conflict** includes the canonical `sku` and uses `urn:inventra:problem:duplicate-sku`. PostgreSQL enforces uniqueness during insert, including when requests race.
- **415 Unsupported Media Type** covers a body sent without a JSON media type. Unexpected failures return a generic **500**. Errors use RFC 9457 `application/problem+json`.

### Validation rules

| Field | Rules |
| --- | --- |
| `sku` | Required, 64 characters maximum before trimming. Starts with an ASCII letter or digit. Remaining characters can also include `.`, `_`, and `-`. Trimmed and uppercased. |
| `name`, `category` | Required, nonblank, at most 255 and 100 characters respectively. Trimmed. Category is free text. |
| `description` | Optional, at most 2,000 characters. Trimmed. Blank text becomes null. |
| `price.amount` | Required, nonnegative, up to 15 integer and 4 fractional digits. Accepts JSON numbers or plain decimal strings. The response emits an exact JSON number. Clients using binary floating-point should use a decimal-aware parser when full precision matters. |
| `price.currency` | Required, uppercase ISO 4217 code, validated against the installed ISO 4217 table. |
| `unitOfMeasure` | Required. `PIECE`, `BOX`, `PACK`, `PALLET`, `GRAM`, `KILOGRAM`, `MILLILITER`, `LITER`, `CENTIMETER`, `METER`, `SQUARE_METER`, or `CUBIC_METER`. |
| `attributes` | Required JSON object with up to 50 entries. Use `{}` for none. Keys are nonblank, at most 64 characters. Values are strings of at most 255 characters. |

## Test

The API contract tests use FastAPI's in-process client and a stubbed create service. Run them from the project root after installing `requirements-dev.txt`.

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not postgres" -q
```

For the full suite entirely in Docker:

```powershell
docker compose --profile test run --build --rm tests
docker compose --profile test rm -s -f test-db
```

This also works on Windows machines where Application Control blocks the native psycopg DLL. To run pytest on the host against the disposable test database, use:

```powershell
docker compose --profile test up -d --wait test-db
$env:INVENTRA_TEST_DATABASE_URL = 'postgresql+psycopg://inventra_test:inventra_test@127.0.0.1:55432/inventra_test'
.\.venv\Scripts\python.exe -m pytest -q
docker compose --profile test rm -s -f test-db
Remove-Item Env:INVENTRA_TEST_DATABASE_URL
```

The PostgreSQL tests cover Alembic upgrade, downgrade and re-upgrade, committed rows, exact prices, normalized duplicate rejection, and concurrent inserts. Each test migrates a fresh schema and drops only that schema at cleanup. The fixture requires an explicit test URL and a dedicated `inventra_test` database and role. The role has permission to create schemas in that database and has neither superuser nor CREATEDB privileges. Without the test URL, pytest reports the PostgreSQL tests as skipped. A configured database failure fails the tests.

## Extension points

- **GET products** can add read routes alongside POST in `inventory_management_service/main.py`, using the existing session dependency and model. Plan GET by UUID, pagination, filtering, and sorting. Add read-specific indexes through a new Alembic revision.
- **Update** can reuse the mapped version column for stale-write detection. Define the request and concurrency contract when implementing the route.
- **Soft delete** can use the nullable `deleted_at` column and the existing status consistency checks. Retain the SKU reservation and exclude deleted products from future reads.
- **Bulk create and bulk delete** can use separate request schemas and service operations. The current service owns the single-create transaction. Extract transaction-neutral insertion logic before implementing atomic batches.

The only registered product operation is POST. The project separates HTTP handling and Pydantic schemas from the create transaction and ORM model.
