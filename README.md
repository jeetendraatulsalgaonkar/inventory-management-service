# Inventory Management Service

Inventory Management Service is a product catalog API built with Python 3.14, FastAPI, Pydantic v2, SQLAlchemy 2.0, PostgreSQL 18, and Alembic. It implements **Create Products** (one or more per request), **Get Product by ID**, **List Products** with search, filters, sorting, and pagination, and **Soft-delete Products**. Python 3.14 supplies the standard-library UUIDv7 generator. The schema uses no PostgreSQL 17/18-specific features; the full test suite also passes on PostgreSQL 16.

## Run Python directly on Windows

Install Python 3.14 and PostgreSQL 18, and start the local PostgreSQL service. Using `psql` or pgAdmin as a PostgreSQL administrator, create a development role and an empty database once:

```sql
CREATE ROLE inventra LOGIN PASSWORD 'inventra_dev';
CREATE DATABASE inventra OWNER inventra;
```

These credentials are for local development. From the project root, run the following PowerShell commands, adjusting `DATABASE_URL` if your local credentials or port differ:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:DATABASE_URL = 'postgresql+psycopg://inventra:inventra_dev@127.0.0.1:5432/inventra?connect_timeout=5'
.\.venv\Scripts\python.exe -m uvicorn inventory_management_service.main:app --reload
```

Open [Swagger UI](http://localhost:8000/docs) to try requests, [ReDoc](http://localhost:8000/redoc) for the API reference, or [OpenAPI JSON](http://localhost:8000/openapi.json) to import into an API client. Stop the server with Ctrl+C. Set `DATABASE_URL` again whenever starting from a new terminal.

On macOS or Linux use `python3.14 -m venv .venv`, `.venv/bin/python`, and `export DATABASE_URL=...` (see the next section for a complete walkthrough). Run migrations from the project root. Revision `0001` is an Alembic baseline for an empty database. Existing Flyway-managed Inventra databases require a separate adoption plan. The baseline downgrade drops the products table and is intended for disposable databases.

## Run on Linux without Docker or root (PostgreSQL 16)

These commands create a throwaway PostgreSQL cluster owned by your user under `/tmp/inventra-pg`, so they need neither Docker nor `sudo`. They were used to verify this project with Python 3.14 and PostgreSQL 16 on Ubuntu 24.04. Adjust `PG_BIN` if `pg_config --bindir` reports a different directory, and pick another port if 5432 is taken.

```bash
# One-time: PostgreSQL cluster, development database, and dedicated test database/role.
export PG_BIN=/usr/lib/postgresql/16/bin PGDATA=/tmp/inventra-pg/data
mkdir -p /tmp/inventra-pg
echo postgres > /tmp/inventra-pg/pwfile
"$PG_BIN/initdb" -D "$PGDATA" -U postgres --auth=scram-sha-256 --pwfile=/tmp/inventra-pg/pwfile -E UTF8 --locale=C.UTF-8
"$PG_BIN/pg_ctl" -D "$PGDATA" -l /tmp/inventra-pg/server.log -w start \
  -o "-c listen_addresses=127.0.0.1 -c port=5432 -c unix_socket_directories=/tmp/inventra-pg"
export PGPASSWORD=postgres
psql -h 127.0.0.1 -U postgres -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE inventra LOGIN PASSWORD 'inventra_dev'" \
  -c "CREATE DATABASE inventra OWNER inventra" \
  -c "CREATE DATABASE inventra_test"
psql -h 127.0.0.1 -U postgres -d inventra_test -v ON_ERROR_STOP=1 -f tests/init-test-db.sql
unset PGPASSWORD

# One-time: Python environment (from the project root).
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt

# Verify: database-independent unit tests, then the full verification against PostgreSQL.
.venv/bin/python -m pytest -m "not postgres" -q
INVENTRA_TEST_DATABASE_URL='postgresql+psycopg://inventra_test:inventra_test@127.0.0.1:5432/inventra_test' \
  .venv/bin/python -m pytest --require-postgres
echo "exit code: $?"    # 0 only if every test, including every PostgreSQL test, ran and passed

# Run the API. Startup applies migrations and seeds the 115 sample products.
export DATABASE_URL='postgresql+psycopg://inventra:inventra_dev@127.0.0.1:5432/inventra?connect_timeout=5'
.venv/bin/python -m uvicorn inventory_management_service.main:app --reload
```

Then open [Swagger UI](http://localhost:8000/docs). In a new terminal, export `PG_BIN` and `PGDATA` again. Stop the cluster with `"$PG_BIN/pg_ctl" -D "$PGDATA" stop`, or remove it completely with `rm -rf /tmp/inventra-pg` after stopping it. If the pytest run reports skipped tests, `INVENTRA_TEST_DATABASE_URL` was not set.

## Startup migrations and sample products

Application startup runs `alembic upgrade head` before accepting requests. The database role must have permission to apply schema changes.

- **Ordering.** No request is answered until the migrations have committed. A single uvicorn process opens its port only after startup. With `--workers` or `--reload`, the parent process holds the port earlier, but requests wait until a worker has finished starting.
- **Failure.** If a migration fails, the whole upgrade rolls back (all revisions run in one transaction), the process exits with a non-zero status, and the port is never opened. For example, a database with a `products` table that these migrations did not create fails with `relation "products" already exists` and is left unchanged.
- **Concurrency.** Concurrent application processes serialize startup migrations with a PostgreSQL advisory lock. Exactly one applies them, and the others find the database at head. The migration transaction always runs at READ COMMITTED, even if the database or role defaults to REPEATABLE READ or SERIALIZABLE. With those defaults, a waiting process would not see the migration committed while it waited, and would fail at startup.
- **Repeated startups** change nothing: existing product IDs, edited values, soft deletions, and removed sample products stay as they are.

Revision `0002` executes `migrations/sql/0002_sample_products.sql`, inserting all 115 sample products (the original 15 plus 100 additional color variants). The SQL uses `ON CONFLICT (sku) DO NOTHING`, preserving existing product data. Alembic records the revision so later starts do not seed again. This seeds every database upgraded to head, including new databases. Downgrading to `0001` preserves seed rows because they may have been edited or referenced; downgrading to base drops the products table.

The catalog is also available in `data/sample-products.json`, with a standalone copy of the insert script at `data/sample-products.sql`. Keep the versioned migration SQL unchanged after deployment; add a new migration for future catalog changes. Include the `migrations` directory and `alembic.ini` when deploying the Python application.

To apply migrations without starting the API:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

## Create products

`POST /api/v1/products` creates any number of products: there is no upper limit, so a request with 10,000 or 100,000 products succeeds like one with a single product. The body is a JSON array of product objects, even for a single product:

```powershell
$body = @'
[
  {
    "sku": " lap-dell-1 ",
    "name": "Dell XPS 15",
    "description": "15-inch business laptop",
    "category": "ELECTRONICS",
    "price": {"amount": 1499.99, "currency": "EUR"},
    "unitOfMeasure": "PIECE",
    "attributes": {"brand": "Dell"}
  },
  {
    "sku": "dock-usb-c-1",
    "name": "USB-C Dock",
    "category": "ELECTRONICS",
    "price": {"amount": "89.99", "currency": "EUR"},
    "unitOfMeasure": "PIECE",
    "attributes": {}
  }
]
'@
$result = Invoke-WebRequest -Method Post -Uri http://localhost:8000/api/v1/products -ContentType application/json -Body $body
$result.StatusCode
$result.Content
```

```bash
curl -s -X POST http://localhost:8000/api/v1/products -H 'Content-Type: application/json' \
  -d '[{"sku":"lap-dell-2","name":"Dell XPS 13","category":"ELECTRONICS","price":{"amount":1199.5,"currency":"EUR"},"unitOfMeasure":"PIECE","attributes":{}}]'
```

The request is atomic: the products are stored in one transaction, so either every product is created or none is, however many there are. They share one `createdAt`, and their UUIDv7 ids increase in request order, so sorting by `createdAt` or `id` keeps the request order. See [Large requests](#large-requests) for how large requests are processed and how long they take.

- **201 Created** returns a JSON array of the created products, in request order. Each has a UUIDv7 `id`, canonical `sku`, `status: ACTIVE`, UTC `createdAt` and `updatedAt`, and optimistic-locking `version: 0`. Read a product at `/api/v1/products/{id}` ([Get a product](#get-a-product)). The response has no `Location` header, because a request can create several resources.
- **400 Bad Request**: nothing was created. The `detail` names the cause:

  | Request | `type` | `detail` |
  | --- | --- | --- |
  | No body, or only whitespace | `malformed-request` | `The request body is missing. Send a JSON array of at least 1 product.` |
  | JSON `null` | `malformed-request` | `The request body is null. Send a JSON array of at least 1 product.` |
  | Invalid JSON, including `NaN` and `Infinity` | `malformed-request` | `The request body is not valid JSON. ...` |
  | A JSON object, such as a single product without the array (the former request format) | `malformed-request` | `The request body must be a JSON array of products, not a single product object. ...` |
  | A JSON string, number, or boolean | `malformed-request` | `The request body must be a JSON array of products, not a JSON string.` (or `number`, `boolean`) |
  | `[]` | `validation-error` | `The product list is empty. Send a JSON array of at least 1 product.` |
  | Invalid products | `malformed-request` if any error is a wrong JSON type, an element that is not an object, or an unknown property; otherwise `validation-error` | `The request contains 3 invalid field(s) in 2 product(s). No product was created.` |
  | Two products with the same SKU after trimming and uppercasing | `validation-error` | As for invalid products, with `{"field": "2.sku", "message": "repeats the SKU 'LAP-DELL-1' of product 0"}` |

  The number of products never causes a 400. The `errors` entries name each field by the product's zero-based index in the array, such as `0.sku`, `1.price.amount`, or `3` for an element that is not an object. They are sorted by index, then by field. At most the first 1,000 are listed, so that a large rejected request does not produce a huge response; the `detail` still counts all of them and then says `The first 1000 are listed.`
- **409 Conflict** with `urn:inventra:problem:duplicate-sku` when a SKU is already used, also by a soft-deleted product. `skus` lists every such canonical SKU in request order, not only the first one found. Nothing was created. PostgreSQL enforces uniqueness during insert, including when requests race: a request that inserts a SKU that a concurrent request has inserted but not yet committed waits for it, then returns this 409 if that request committed, at every default isolation level.
- **415 Unsupported Media Type** covers a body sent without a JSON media type. Unexpected failures return a generic **500**. Errors use RFC 9457 `application/problem+json`.

### Large requests

Every step is streamlined for large requests:

- **Storage.** All rows are streamed to PostgreSQL with a single `COPY` in one transaction, instead of one `INSERT` per product or per batch. `COPY` enforces the same constraints, unique index, and row locks as `INSERT`. Measured against the alternatives for 10,000 products, `COPY` took 0.19 s, the previous ORM inserts 2.9 s, and multi-row `INSERT` statements of 100 products each 5.0 s; for 100,000 products, 2.0 s against 23 to 53 s. Splitting the request into separately committed batches would not be faster, and a failure halfway would leave part of the request stored.
- **Concurrency.** Parsing and validating the body is CPU-bound work in proportion to its size. It runs in a worker thread, like the database work, so the event loop keeps serving other requests while a large request is processed. Concurrent requests run in parallel; the tests create eight batches at the same time.
- **Validation** uses precompiled patterns and a fast path for currency codes. It checks every product and repeated SKUs across the whole request before anything is stored.
- **Serialization.** Create, get, and list share one serializer that assembles each product from values encoded by the standard library's C JSON encoder. It keeps exact JSON-number prices, and its output is byte for byte the same as before.
- **Conflicts.** The SKU lookup after a unique violation passes all SKUs as one array parameter, so it works for any number of products. PostgreSQL accepts at most 65,535 bind parameters per statement.

End-to-end times over HTTP against one uvicorn process and PostgreSQL 16, measured on a 4-core development VM with shared storage. The range covers repeated runs:

| Products | Request body | Response | Time | Server memory (peak RSS) |
| --- | --- | --- | --- | --- |
| 10,000 | 2.4 MB | 4.1 MB | 0.6 to 1.1 s | 150 MB |
| 50,000 | 12 MB | 21 MB | 4.1 to 4.6 s | 330 MB |
| 100,000 | 25 MB | 41 MB | 11 to 14 s | 580 MB |
| 200,000 | 49 MB | 82 MB | about 36 s | 1 GB |

Up to about 50,000 products, the time grows in proportion to the number of products. Beyond that, Python's garbage collector spends more time on the larger heap. While the VM's disk was contended, some runs took several times longer. Before these changes, 10,000 products took about 3.8 s and 100,000 about 58 s in-process, without HTTP. A test posts 10,000 products to a running server with a 30-second client timeout.

A synchronous request is still limited by server memory, about 5 KB per product at peak, and by the timeouts of proxies and clients. Clients with millions of products, or deployments behind short proxy timeouts, should send them in several requests of, for example, 10,000 to 50,000 products. Each request is atomic on its own. See [Extension points](#extension-points) for an asynchronous import.

### Validation rules

The fields of each product follow these rules:

| Field | Rules |
| --- | --- |
| `sku` | Required, 64 characters maximum before trimming. Starts with an ASCII letter or digit. Remaining characters can also include `.`, `_`, and `-`. Trimmed and uppercased. |
| `name`, `category` | Required, nonblank, at most 255 and 100 characters respectively. Trimmed. Category is free text. |
| `description` | Optional, at most 2,000 characters. Trimmed. Blank text becomes null. |
| `price.amount` | Required, nonnegative, up to 15 integer and 4 fractional digits. Accepts JSON numbers or plain decimal strings. The response emits an exact JSON number. Clients using binary floating-point should use a decimal-aware parser when full precision matters. |
| `price.currency` | Required, uppercase ISO 4217 code, validated against the installed ISO 4217 table. |
| `unitOfMeasure` | Required. `PIECE`, `BOX`, `PACK`, `PALLET`, `GRAM`, `KILOGRAM`, `MILLILITER`, `LITER`, `CENTIMETER`, `METER`, `SQUARE_METER`, or `CUBIC_METER`. |
| `attributes` | Required JSON object with up to 50 entries. Use `{}` for none. Keys are nonblank, at most 64 characters. Values are strings of at most 255 characters. |

## Get a product

`GET /api/v1/products/{productId}` returns the product with the same representation as each item of the create response: `id` first, then the product fields, with an exact JSON-number price, canonical SKU, UTC timestamps, and `version`. Create, get, and list share one serializer, so the three representations are identical. `status` can be `ACTIVE`, `INACTIVE`, or `DISCONTINUED`; create always returns `ACTIVE`.

```bash
B=http://localhost:8000/api/v1/products
# A seeded product (sample IDs are fixed by the seed migration).
curl -s "$B/01a1b052-ec7c-76db-a862-e0da4baa5110"
# Read a product that a create request returned.
ID=$(curl -s -X POST "$B" -H 'Content-Type: application/json' \
  -d '[{"sku":"lap-dell-3","name":"Dell XPS 13","category":"ELECTRONICS","price":{"amount":1199.5,"currency":"EUR"},"unitOfMeasure":"PIECE","attributes":{}}]' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["id"])')
curl -s "$B/$ID"
```

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/products/01a1b052-ec7c-76db-a862-e0da4baa5110
```

- **200 OK** returns the product.
- **404 Not Found** uses `urn:inventra:problem:product-not-found` and includes `productId`. [Soft-deleted products](#soft-delete-products) (`deleted_at` set, status `DELETED`) return the same 404 as products that never existed.
- **400 Bad Request** uses `urn:inventra:problem:validation-error` when the ID is not a UUID, with an `errors` entry for `productId`.

## List products

`GET /api/v1/products` returns one page of products that are not soft-deleted:

```json
{"items": [{"id": "01a1b052-ec7d-74d0-bf78-a2852fecfcd6", "sku": "DEMO-MONITOR-001", "...": "..."}], "page": 1, "size": 20, "totalItems": 115, "totalPages": 6}
```

`items` use the Get Product representation. A page past the end returns `200` with empty `items` and the real totals. Within one response, the count and the page are read from one snapshot, so `totalItems`, `totalPages`, and `items` describe the same state even if products are created, changed, or deleted while the request runs.

| Parameter | Default | Rules |
| --- | --- | --- |
| `page` | `1` | 1-based page number, 1 to 2,147,483,647. |
| `size` | `20` | Products per page, 1 to 100. |
| `search` | none | Case-insensitive. Every whitespace-separated term must occur in the SKU, name, or description, so `ring binder blue` finds `A4 Ring Binder - Blue`. `%`, `_` and `\` match literally. At most 255 characters. Blank means no filter. |
| `category` | none | Exact category, case-insensitive and trimmed, so `electronics` matches `ELECTRONICS`. Blank means no filter. |
| `minPrice`, `maxPrice` | none | Inclusive bounds on `price.amount`, nonnegative, up to 15 integer and 4 fractional digits. `minPrice` must not exceed `maxPrice`. |
| `currency` | none | Uppercase ISO 4217 code. Price bounds compare amounts without currency conversion, so combine them with `currency` when the catalog has several currencies. |
| `sort` | `name` | `name`, `sku`, `category`, `price`, `createdAt`, or `updatedAt`. |
| `direction` | `asc` | `asc` or `desc`. |

Ties in the sort order are always broken by product `id` in the same direction, so the order is total and deterministic. This matters for the sample catalog because all 115 seeded products share one `createdAt`. Text sorting follows the database collation.

Paging uses offsets. Page `n` returns rows `(n - 1) × size + 1` to `n × size` of the ordering as it is when that page is requested. Nothing is kept between requests. This guarantees:

- **Unchanged data:** paging from page 1 to `totalPages` returns every matching product exactly once, in order, and repeating a request returns the same page.
- **Changes between page requests:** pages can repeat or omit products, by as many positions as there were changes before the current position.
  - A product that *enters* the part already paged through moves later pages back, so the last product already shown appears again. It may have been created, updated to sort there, or updated to start matching the filters. If an update moved it there, it is not shown at all.
  - A product that *leaves* the part already paged through moves later pages forward, so the next product is omitted. It may have been soft-deleted, updated to sort later, or updated to stop matching the filters. If an update moved it later, it is shown again.
  - Changes that affect only later positions do not shift later pages, and neither do updates that change neither the sort key nor filter membership. With `sort=updatedAt`, every update changes the sort key.
  - `totalItems` and `totalPages` describe the data at each request, so they can change between pages.

A client that needs one complete pass without repeats or omissions must page through while the data does not change. Otherwise it should de-duplicate by `id` and accept that products can be missed. Gap-free paging under concurrent changes would need keyset pagination, which is an API change.

Filters combine with AND. Unknown parameters, such as a misspelled `categroy`, are rejected instead of being ignored. Invalid parameters return **400** `urn:inventra:problem:validation-error` with one `errors` entry per parameter; unexpected failures return a generic **500**.

```bash
B=http://localhost:8000/api/v1/products
curl -s "$B?size=5"                                               # first 5 by name
curl -s "$B?page=2&size=5"                                        # next 5
curl -s "$B?search=ring%20binder"                                 # all ring binder variants
curl -s "$B?category=electronics&sort=price&direction=desc"       # most expensive electronics first
curl -s "$B?minPrice=10&maxPrice=50&currency=EUR&sort=price"      # 10.00 to 50.00 EUR, cheapest first
curl -s "$B?search=black&category=ELECTRONICS&maxPrice=100"       # combined filters
curl -s "$B?size=500"                                             # 400: size must be at most 100
```

To print a compact table:

```bash
curl -s "$B?size=100" | python3 -c 'import json, sys
for p in json.load(sys.stdin)["items"]: print(p["sku"], p["name"], p["price"]["amount"], sep="\t")'
```

```powershell
$page = Invoke-RestMethod 'http://localhost:8000/api/v1/products?category=electronics&sort=price&direction=desc&size=5'
$page.totalItems
$page.items | Format-Table sku, name, @{n='price'; e={$_.price.amount}}
```

In Swagger UI, expand **GET /api/v1/products**, choose **Try it out**, and fill in the parameters; `sort` and `direction` are drop-down lists.

## Product status

Every product has a `status`. It is stored in the `products.status` column (`varchar(16) NOT NULL`, created by revision `0001`), mapped as `Product.status`, and included in every product representation: the create response, Get a product, and each item of List products.

| Status | Meaning |
| --- | --- |
| `ACTIVE` | Every created product starts as `ACTIVE`. |
| `INACTIVE`, `DISCONTINUED` | Allowed by the database and shown by Get and List. No endpoint sets them yet. |
| `DELETED` | Set by [soft delete](#soft-delete-products). Get and List never return these products. |

The database accepts only these values (`ck_products_status`) and ties `DELETED` to the deletion time: `status` is `DELETED` exactly when `deleted_at` is set (`ck_products_deleted_state`), and `deleted_at` is never earlier than `created_at` (`ck_products_deleted_at`). `deleted_at` is internal and not part of the API representation.

## Soft-delete products

`DELETE /api/v1/products` marks products as deleted. The JSON body lists their IDs in `productIds`:

```json
{"productIds": ["01a1b052-ec7c-76db-a862-e0da4baa5110", "01a1b052-ec7d-74d0-bf78-a283a884700e"]}
```

Nothing is physically deleted. In one transaction, each listed product gets status `DELETED`, `deleted_at` and `updatedAt` set to the deletion time, and `version` incremented; every other column keeps its value. The deletion time is never earlier than the product's previous `updatedAt`, even if another server's clock runs ahead. Afterwards:

- Get a product returns 404 for the product, and List products neither returns nor counts it.
- Its SKU stays reserved: creating a product with the same SKU returns 409.
- The row stays in the `products` table.

The request is all or nothing. If any listed product does not exist or is already deleted, nothing is deleted, and the 404 response lists those IDs. As with Get, deleted products and products that never existed are indistinguishable.

```bash
B=http://localhost:8000/api/v1/products
# Soft-delete two seeded products (sample IDs are fixed by the seed migration).
curl -si -X DELETE "$B" -H 'Content-Type: application/json' \
  -d '{"productIds": ["01a1b052-ec7c-76db-a862-e0da4baa5110", "01a1b052-ec7d-74d0-bf78-a283a884700e"]}'
# 204 No Content. Reads no longer return them:
curl -s -o /dev/null -w '%{http_code}\n' "$B/01a1b052-ec7c-76db-a862-e0da4baa5110"    # 404
curl -s "$B?size=1" | python3 -c 'import json, sys; print(json.load(sys.stdin)["totalItems"])'    # 113 after a fresh seed
# Repeating the request deletes nothing and reports both IDs (404):
curl -s -X DELETE "$B" -H 'Content-Type: application/json' \
  -d '{"productIds": ["01a1b052-ec7c-76db-a862-e0da4baa5110", "01a1b052-ec7d-74d0-bf78-a283a884700e"]}'
# Without product IDs, the request is rejected (400):
curl -s -X DELETE "$B" -H 'Content-Type: application/json' -d '{"productIds": []}'
# The rows are still stored, marked DELETED:
PGPASSWORD=inventra_dev psql -h 127.0.0.1 -U inventra -d inventra \
  -c "SELECT sku, status, deleted_at, version FROM products WHERE status = 'DELETED'"
```

```powershell
$body = '{"productIds": ["01a1b052-ec7c-76db-a862-e0da4baa5110"]}'
$result = Invoke-WebRequest -Method Delete -Uri http://localhost:8000/api/v1/products -ContentType application/json -Body $body
$result.StatusCode    # 204
```

- **204 No Content**: every listed product is now marked `DELETED`.
- **400 Bad Request** with `urn:inventra:problem:malformed-request` when the body is missing, JSON `null`, or not a JSON object (a bare array of IDs is not accepted), when `productIds` is `null`, is not an array, or holds a non-string, and for unknown properties.
- **400 Bad Request** with `urn:inventra:problem:validation-error` when `productIds` is missing or empty, has more than 100 IDs, repeats an ID, or holds a string that is not a UUID. Each `errors` entry names the field, such as `productIds` or `productIds.2`.
- **404 Not Found** with `urn:inventra:problem:products-not-found`: `productIds` lists the IDs that do not exist or are already deleted, in request order. Nothing was deleted.
- **415 Unsupported Media Type** when a body is sent without a JSON media type. Unexpected failures return a generic **500**.

IDs accept the same spellings as the Get path: any letter case, with or without hyphens, in braces, or with a `urn:uuid:` prefix. A request holds at most 100 IDs, the size of the largest list page; delete larger sets in several requests.

Concurrent requests that list the same product delete it once: one succeeds, and the other returns 404 for that product and deletes nothing. Rows are locked in id order, so such requests cannot deadlock. A request that waits for another transaction's lock on a listed product sees that transaction's outcome even if the database defaults to REPEATABLE READ or SERIALIZABLE, because the deletion always runs at READ COMMITTED.

Some HTTP clients and proxies do not forward a DELETE body; such a request fails with 400. In Swagger UI, expand **DELETE /api/v1/products**, choose **Try it out**, and replace the example ID with IDs from the list endpoint.

## Test

The unit tests need no database:

- `tests/test_api.py` covers the HTTP contract of create, get, list, and soft delete through FastAPI's in-process client with stubbed services: success responses, every validation rule and its boundary values, malformed JSON, unknown properties, media types, 404/405/409/415/500 problem documents, parameter validation, and the OpenAPI document. For create, it covers requests of 1 to 10,000 products returned in request order without a `Location`; validation in a worker thread instead of the event loop; the specific 400 for a missing, whitespace-only, `null`, or empty body, a single object instead of an array, scalars, and invalid JSON; elements that are not objects; errors reported per product index in numeric order, capped at the first 1,000 with the total counted; one invalid product, even the last of 20,000, rejecting the whole request; SKUs repeated after normalization, even far apart; the 409 listing every taken SKU; and a serializer that round-trips any text. It also checks that all three read and create endpoints return the same product representation, including `id` and `status`, with exact JSON-number prices and UTC timestamps. For soft delete, it covers the 204 response, missing and `null` bodies, missing, `null`, empty, oversized, and repeated ID lists, invalid IDs, and the 404 that lists every missing ID, and it runs the real service on a `FakeSession` end to end. Tests compare JSON member sets, not member order.
- `tests/test_service.py` runs the service layer against an in-memory `FakeSession` (in `tests/conftest.py`): how create commands become the rows of one `COPY` in one transaction, for up to 10,000 products, with one creation time and ids in request order, the translation of a unique violation into every taken SKU (looked up after the rollback with one array parameter, even for 70,000 SKUs, deleted products included), re-raising other integrity and database errors, not-found handling, and the rules that must hold even without PostgreSQL. These are the soft-delete filter, identical criteria for the count and the page, the `id` tiebreaker, and the REPEATABLE READ snapshot. For soft deletion, they check all or nothing with missing IDs reported in request order, the fields it changes and keeps, one deletion time per request that never precedes a product's previous `updated_at`, the READ COMMITTED transaction, and the row-lock query in id order. Statements are compared structurally, not as SQL text. What the filters, search, ordering, and paging actually return is verified against PostgreSQL.
- `tests/test_startup.py` checks that startup runs the migrations and stops when they fail. Its PostgreSQL tests start the real application (uvicorn in separate processes) against isolated schemas. They cover a fresh database, repeated restarts after products were edited, soft-deleted, or removed, an upgrade from release `0001` that already holds products (including one with a sample SKU), four concurrent startups under each isolation level, that no request is answered while migrations are in progress (one process and `--workers 2`), and failed migrations that roll back without opening the port. A fixture holds the migration lock so that the tests control when startup may proceed.

Run them from the project root after installing `requirements-dev.txt`.

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not postgres" -q
```

For the full suite, create a separate local test database once as a PostgreSQL administrator:

```sql
CREATE DATABASE inventra_test;
```

Then run the role setup script as that administrator (replace `postgres` with your administrator username if needed):

```powershell
psql -h localhost -U postgres -d inventra_test -f tests/init-test-db.sql
```

### Full verification

Run the complete suite against that database with `--require-postgres`:

```powershell
$env:INVENTRA_TEST_DATABASE_URL = 'postgresql+psycopg://inventra_test:inventra_test@localhost:5432/inventra_test'
.\.venv\Scripts\python.exe -m pytest --require-postgres
$LASTEXITCODE
Remove-Item Env:INVENTRA_TEST_DATABASE_URL
```

```bash
INVENTRA_TEST_DATABASE_URL='postgresql+psycopg://inventra_test:inventra_test@127.0.0.1:5432/inventra_test' \
  .venv/bin/python -m pytest --require-postgres
echo $?
```

This is the command to use when a passing result must mean that the PostgreSQL integration tests ran. A plain `pytest` run still skips them when no test database is configured and still exits 0, as before. Before any test starts, `--require-postgres` checks the configuration, read from the environment or `.env`, and refuses to run if:

- `INVENTRA_TEST_DATABASE_URL` is missing or is not a valid URL;
- the URL does not use `postgresql+psycopg` with a database and role both named `inventra_test` (an accidentally supplied development URL is rejected here);
- `DATABASE_URL` also names `inventra_test`;
- PostgreSQL does not answer within 5 seconds;
- the server reports a different database or role, the role has SUPERUSER or CREATEDB, or it lacks CREATE on the database;
- only part of the suite would run: individual tests are selected, tests are deselected with `-k`, `-m`, or `--lf`, or a test module is not collected. Naming the project or the `tests` folder is fine.

After the run, it fails if any test failed, any PostgreSQL test was skipped, no PostgreSQL test ran, or a test schema it created still exists. A summary line reports the outcome.

| Exit code | Meaning |
| --- | --- |
| 0 | Every test ran and passed, including every PostgreSQL integration test, and all test schemas were removed. |
| 1 | A test failed, or the verification was incomplete (skipped PostgreSQL tests or leftover schemas). |
| 4 | Refused before running: configuration missing, invalid, or unsafe, the database is unreachable, or only part of the suite was selected. |

The verification touches only the test database: each PostgreSQL test creates its own `test_<random>` schema and drops it. `DATABASE_URL` is removed from the test process after the checks, so no test can reach the development database through the application. One test starts the application with uvicorn on a free port, pointed at its test schema, and stops that process itself. A server you have running on port 8000 is not affected.

The PostgreSQL tests cover Alembic upgrade, downgrade and re-upgrade, committed rows, exact prices, normalized duplicate rejection, and concurrent inserts. For batch creation, they check that a batch is stored in one transaction with one creation time and ids in request order, that a batch with a SKU already used by a seeded, soft-deleted, or earlier-created product stores nothing and lists every taken SKU, that every rejected request (missing, `null`, empty, non-array, invalid, or repeated-SKU) leaves every row unchanged, that 10,000 varied products, including text that `COPY` must escape, are stored exactly as sent in one transaction and read back identically, that a request of 5,000 products with one taken SKU or one invalid product stores nothing, that concurrent disjoint batches all succeed and overlapping ones have one winner, that a batch waiting for another transaction's uncommitted insert of one of its SKUs returns 409 if that transaction commits and 201 if it rolls back, at every default isolation level, and that POST works over HTTP against uvicorn, including 10,000 products within a 30-second client timeout. For reads, they cover create-then-get round trips, hiding soft-deleted products, gap-free and duplicate-free paging over unchanged data for every sort field and direction over the seeded catalog, search (including literal `%`, `_` and `\`), category, price and currency filters checked against `data/sample-products.json`, and UTC output under a non-UTC session time zone. For soft delete, they check that deleted rows stay stored with only `status`, `deleted_at`, `updated_at`, and `version` changed, that Get and List hide them, that a request with an unknown or already deleted ID and a request without product IDs leave every row unchanged, that SKUs stay reserved, that rows written by a server whose clock runs ahead can be deleted, that Get and List show each stored status, that concurrent requests delete a product once, that a request waiting for a locked product reports it as not found if it was deleted meanwhile, at every default isolation level, and that DELETE works over HTTP against uvicorn. For paging under change, they apply creations, soft deletions, sort-key updates, other updates, and filter-membership changes between page requests. Each resulting page must equal the slice of the current ordering, and the tests check the exact repeat or omission that follows. A commit injected between the count and the page query of one request shows that a response never mixes two states. Each test migrates a fresh schema and drops only that schema at cleanup. The fixture requires an explicit test URL and a dedicated `inventra_test` database and role. The role has permission to create schemas in that database and has neither superuser nor CREATEDB privileges. Without the test URL, a plain pytest run reports the PostgreSQL tests as skipped; `--require-postgres` refuses to run instead.

IDE test runners such as PyCharm do not inherit variables set in a terminal. Instead of setting `INVENTRA_TEST_DATABASE_URL` in each run configuration, you can put it in a `.env` file in the project root. The file is git-ignored, and a variable set in the environment takes precedence:

```
INVENTRA_TEST_DATABASE_URL=postgresql+psycopg://inventra_test:inventra_test@localhost:5432/inventra_test
```

Only the tests read `.env`; the application still needs `DATABASE_URL` in its environment. A configured database failure fails the tests. To use the full verification from PyCharm, add `--require-postgres` to the pytest run configuration's *Additional Arguments*, with the project or the `tests` folder as the target.

### Coverage

`requirements-dev.txt` includes pytest-cov. Add `--cov` to the full verification to measure line and branch coverage of the `inventory_management_service` package. `.coveragerc` configures the measurement and fails the run with exit code 1 unless total coverage is more than 95%:

```powershell
$env:INVENTRA_TEST_DATABASE_URL = 'postgresql+psycopg://inventra_test:inventra_test@localhost:5432/inventra_test'
.\.venv\Scripts\python.exe -m pytest --require-postgres --cov
$LASTEXITCODE
Remove-Item Env:INVENTRA_TEST_DATABASE_URL
```

```bash
INVENTRA_TEST_DATABASE_URL='postgresql+psycopg://inventra_test:inventra_test@127.0.0.1:5432/inventra_test' \
  .venv/bin/python -m pytest --require-postgres --cov
echo $?
```

The report lists the lines and branches that no test ran. The full suite currently covers about 99%, and the unit tests alone (`pytest -m "not postgres" --cov`) about 97%. Coverage is measured in the pytest process only, so the engine and session factory in `db.py`, which run in the uvicorn processes that some tests start, are reported as missed. A partial run, such as a single test file, covers less and can fail the threshold. For an HTML report as well, add `--cov-report=term --cov-report=html` and open `htmlcov/index.html`.

## Extension points

- **Read indexes.** The list queries do not add indexes; the sample catalog is small, and each list request counts all matching rows. For large catalogs, add a new Alembic revision with partial indexes `WHERE deleted_at IS NULL` on the sort keys plus `id`, for example `(name, id)`, and on `lower(category)`. Substring search needs a `pg_trgm` GIN index to avoid sequential scans. For deep paging, consider keyset (cursor) pagination on the same `(sort key, id)` order.
- **Update** can reuse the mapped version column for stale-write detection. Define the request and concurrency contract when implementing the route.
- **Restore** of soft-deleted products would clear `deleted_at` and set a non-deleted status in one update, as `ck_products_deleted_state` requires. Their SKUs are still reserved, so restoring cannot create duplicates.
- **Asynchronous import.** Create accepts any number of products, but a synchronous request is bounded by server memory and proxy timeouts. For millions of products, an import job would accept the body with `202 Accepted`, store it with the same `COPY`, and report progress at a status URL. A deployment that needs hard request-size limits can cap the body size at the reverse proxy. Soft delete still accepts at most 100 IDs per request.

The registered product operations are POST, GET by ID, GET list, and DELETE (soft delete). The project separates HTTP handling and Pydantic schemas (`main.py`, `schemas.py`) from the transactions and queries (`service.py`) and the ORM model.
