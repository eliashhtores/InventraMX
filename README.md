# InventraMX

Warehouse inventory ledger: inbound/outbound, locations, stock reservations, basic picking fulfillment,
optional lot number/expiry tracking, barcodes, and reporting. Full order management is not included.

## Stack

| Layer         | Technology                                                                 |
| ------------- | -------------------------------------------------------------------------- |
| Backend       | Django 5.2 + Django REST Framework — JSON API and authenticated admin       |
| Database      | PostgreSQL 17, configured through environment variables                    |
| Frontend      | Static HTML5 / CSS3 / Vanilla JS + compiled Tailwind, data loaded via XHR |
| Orchestration | Docker Compose (`web` + `db`)                                              |

```
backend/            Django project (config/) and the inventory app (models, API, tests)
frontend/           Static single-page UI (index.html, css/, js/api.js, js/app.js)
Dockerfile          Image for the web service (gunicorn + WhiteNoise)
docker-compose.yml  web + db services
.env.example        Environment variables used by Docker Compose
```

The `web` container serves the REST API under `/api/` and the static frontend from `/`
(via WhiteNoise), and the authenticated Django admin at `/admin/`.

## Quick start

```bash
cp .env.example .env         # set POSTGRES_PASSWORD before starting
docker compose up --build
```

Open http://localhost:8000. Migrations are applied automatically when the `web` container starts.

Open http://localhost:8000/admin/ to use Django admin. Create an admin account once with:

```bash
docker compose exec web python manage.py createsuperuser
```

Sign in with the username and password you created. The admin lets staff manage warehouses,
locations, and products. Existing stock, stock movements, and reservations can be edited directly
by staff with the corresponding model's change permission. Active superusers can also create and
delete stock, movements, and reservations without any individually assigned permissions;
creation and deletion remain disabled for other staff.
**These are direct database operations:** creating, editing, or deleting movements does not
recalculate stock; stock writes do not create ledger adjustments; reservation writes bypass
availability and lifecycle services.
Model validation and database constraints still apply. Use these edits carefully for manual repairs;
`verify_stock` detects ledger/balance mismatches, and `verify_stock --rebuild` restores balances
from the ledger (it does not repair reservation history).
Reservations also have guarded release/fulfill admin actions (requiring reservation change permission);
use these actions rather than editing status to perform normal lifecycle operations.
For normal inventory operations, create reservations through the API or the Reservations UI.
The web service is bound to localhost by default.

Lookup fields use searchable comboboxes backed by the existing `?search=` APIs. No product,
location, warehouse, lot, or stock catalog is loaded at startup or reloaded after a write.
Focusing a lookup requests at most 20 results; typing is debounced by 300 ms. A result count
asks users to refine searches when more matches exist; lookups never walk all result pages.
Select a result explicitly (mouse or arrow keys + Enter); arbitrary text is not submitted as
an ID. Escape/Tab close the list, and clearing a filter restores all records. Errors are shown
inline and through the standard notification.

Products and locations are searched by SKU/code/name or barcode as supported by each endpoint.
Movement locations must have an active warehouse; tracked-product selection enables a lot
lookup scoped to that product. Reservation lookups include only active inventory with positive
available quantity and can search SKU, product name, location/warehouse code, or lot number.
These eligibility filters run server-side before pagination. Editing a
location or lot hydrates its fixed parent directly from the row, even when it is inactive or
outside the first lookup page. Writes reload only the current table and invalidate affected
lookup selections locally; the next lookup interaction fetches fresh results. CSV exports
still fetch all matching pages only when explicitly requested.

PostgreSQL applies `POSTGRES_PASSWORD` only when its data volume is initialized. If you change the
password in `.env` while reusing an existing `postgres_data` volume, keep the old password so the
web service can connect, or recreate the database with `docker compose down -v` (this deletes all
database data) before starting again.

Frontend HTML, JavaScript, and `css/styles.css` are bind-mounted into the container, so edits are
visible after a browser refresh. The compiled `css/tailwind.css` stays in the image and is not
hidden by these development mounts. This relies on WhiteNoise autorefresh, which is only enabled when
`DJANGO_DEBUG=1`; with `DJANGO_DEBUG=0`, files are indexed once at startup, so restart the `web`
container (`docker compose restart web`) after changing the frontend.

Tailwind is compiled and minified in a separate Node Docker build stage using `npm ci` and the
committed lockfile. Only the generated CSS is copied from that stage into the Python image;
Node and build dependencies are not needed at runtime. The page loads both stylesheets locally
and does not require the Tailwind CDN or an internet connection. Building images still requires
access to package registries (unless their layers are already cached).

After adding or changing utility classes in HTML/JavaScript, run
`docker compose up -d --build web` to regenerate the image's Tailwind CSS. All HTML and JavaScript
sources are scanned, including complete utility class strings used by dynamic UI states.
Custom components remain in `frontend/css/styles.css`, loaded after Tailwind to preserve overrides.
For a frontend served outside Docker, run `npm ci && npm run build` in `frontend/` first
(Node 22 or newer); `npm run watch` regenerates the local CSS during development. Generated CSS
and `node_modules` are ignored by Git and excluded from the Docker build context.

Backend code is copied into the image, not bind-mounted. After backend changes, rebuild and
recreate the web service with `docker compose up -d --build web`; a restart alone does not update
the API code. If the dashboard reports that the warehouse breakdown is unavailable, the live
frontend is newer than the running backend. Rebuild the web service, then reload the page.

### Configuration

The application uses Mexican Spanish (`LANGUAGE_CODE = "es-mx"`, `USE_I18N = True`) for Django/DRF
validation and admin messages. Custom inventory/API errors use Spanish source messages wrapped
in Django's `gettext` / `gettext_lazy`, with named placeholders for dynamic values. No additional
translation catalog compilation is needed. There is no request-language negotiation: browser
language preferences do not change API errors away from the Spanish UI. API field names, enum
values, HTTP statuses, and error response shapes remain unchanged.

| Variable                 | Default                       | Description                           |
| ------------------------ | ----------------------------- | ------------------------------------- |
| `DJANGO_DEBUG`           | `1` (Compose)                 | Debug mode. Set to `0` in production. |
| `DJANGO_SECRET_KEY`      | —                             | Required when `DJANGO_DEBUG=0`.       |
| `DJANGO_ALLOWED_HOSTS`   | `localhost,127.0.0.1,0.0.0.0` | Comma-separated host names.           |
| `POSTGRES_DB`            | `inventramx`                  | Database name.                        |
| `POSTGRES_USER`          | `inventramx`                  | Database user.                        |
| `POSTGRES_PASSWORD`      | required                      | Database password; set in `.env`.     |
| `POSTGRES_HOST` / `PORT` | `db` / `5432`                 | Database host/port (set by Compose).  |
| `WEB_PORT`               | `8000`                        | Host port published on `127.0.0.1`.   |

> **Note:** the API currently has no authentication and is intended for local/trusted use. For that
> reason Compose publishes the web port on the loopback interface (`127.0.0.1`) only.

## Tests

```bash
docker compose exec web python manage.py test
```

CI (`.github/workflows/ci.yml`) runs the same test suite against a PostgreSQL service on every push
and pull request.

## Stock reconciliation

Verify every product/location/lot balance against the movement ledger:

```bash
docker compose exec web python manage.py verify_stock
```

Verification does not change data. It reports each mismatch (product and location IDs, stored
quantity, expected quantity, and lot ID when tracked), including missing stock rows, and exits nonzero
if any are found. Different lots are reconciled independently, even if the combined SKU balance matches.
Rows without movements must have quantity zero.

To repair quantities and recreate missing stock rows from the ledger:

```bash
docker compose exec web python manage.py verify_stock --rebuild
```

Rebuilding is atomic and leaves the ledger unchanged. Negative ledger totals cause either mode to
fail without changing stock; investigate the ledger rather than clamping these totals to zero.
Both modes briefly lock the stock and movement tables against writes to avoid racing with live
movements, and lock reservations against changes. Active reservations exceeding a ledger balance
cause either mode to fail without changing data; release excess holds before rebuilding. Reads
remain available; run during a quiet period for large inventories.

## Demo inventory data

Create sample warehouses, locations, products, and stock movement history with:

```bash
docker compose exec web python manage.py seed_demo_data --warehouses 2 --products 6 --seed 7
```

The default dataset includes:

- Product/location barcodes stored as strings with leading zeroes, plus untracked inventory.
- Tracked SKUs `DEMO-EMB-002`, `DEMO-CIN-001`, and `DEMO-LIM-001` (when included by
  `--products`), each with three product-specific lot numbers: two future expiry dates
  and one expired date. Every new lot has an expiry; expiry is descriptive, not a movement restriction.
- Receipts, transfers, outbound shipments, and adjustments, seeded in atomic multi-line
  batches with backdated `occurred_at` and current audit timestamps.
- Active, released, and fulfilled reservations, including fully reserved shipping stock
  with positive on-hand and zero available quantity. Fulfillment retains the stock's lot.
- With at least two warehouses, an inter-warehouse transfer and an empty Guadalajara SKU
  still stocked in Monterrey. With at least two products as well, the last SKU also has a
  global reorder shortage, illustrated by negative adjustments without consuming holds.

The command is safe to rerun with the same counts: stable references preserve quantities,
lot metadata, movement dates, and reservation history, even with a different `--seed`.
Existing names, barcodes, and tracking modes are not overwritten. Legacy demo products stay
untracked; the command prints a notice when that prevents the new tracked examples.
The creation sequence is Product → Lot → zero-quantity inventory rows → Movements. All lots
and inventory identities exist before movement recording populates balances. Existing undated
demo lots are preserved on reruns; use `--clear` to recreate them with dates after reviewing its
cleanup safeguards.
Inactive demo parents are rejected rather than silently reactivated. All seed/clear operations
commit together or roll back together; the ledger remains verifiable with `verify_stock`.

Counts and opening stock randomness can be configured. Add `--clear` to remove the command's
demo records (across all configured demo SKUs and warehouses) before recreating them:

```bash
docker compose exec web python manage.py seed_demo_data --warehouses 2 --products 6 --seed 7 --clear
```

Clearing recognizes its reserved `DEMO-SEED:` movement/reservation reference namespace and
`DEMO-SEED-001` through `DEMO-SEED-003` lot numbers only within demo inventory. Do not use
these identifiers for manual records. It removes its own reservations before movements,
stock, and lots, but refuses non-demo movements, user lots or reservations (including closed
ones), extra locations, or links to non-demo inventory. Unrelated inventory is left untouched,
even if its reference starts with the demo prefix.

## REST API

All endpoints are under `/api/` and return JSON. List endpoints are paginated
(`?limit=&offset=`, max 1000) and support `?search=`.

| Endpoint                | Methods                       | Filters                                     |
| ----------------------- | ----------------------------- | ------------------------------------------- |
| `/api/warehouses/`      | GET, POST, PUT, PATCH, DELETE | `is_active` (`true`/`false`), `search`         |
| `/api/locations/`       | GET, POST, PUT, PATCH, DELETE | `warehouse`, `location_type`, `is_active`, `warehouse_is_active` (`true`/`false`), `barcode` (exact), `search` |
| `/api/products/`        | GET, POST, PUT, PATCH, DELETE | `is_active`, `lot_tracked` (`true`/`false`), `barcode` (exact) |
| `/api/lots/`            | GET, POST, PUT, PATCH, DELETE | `product`, `lot_number` (exact), `search` |
| `/api/stock/`           | GET (read-only)               | `product`, `location`, `warehouse`, `lot`, `reservable`, `search` |
| `/api/movements/`       | GET, POST (append-only)       | `movement_type`, `product`, `lot`, `created_at_after`, `created_at_before`, `occurred_at_after`, `occurred_at_before`, `warehouse`, `location`, `reference`, `ordering` |
| `/api/movements/batch/` | POST                          | Atomic, ordered array of movements          |
| `/api/reservations/`    | GET, POST                     | `status`, `stock_item`, `product`, `location`, `warehouse`, `lot`, `reference` (exact), `search` |
| `/api/reservations/{id}/release/` | POST                  | Release an active hold                      |
| `/api/reservations/{id}/fulfill/` | POST                  | Atomically record its outbound movement     |
| `/api/reports/summary/` | GET                           | Global totals, movement counts, low-stock products, warehouse breakdown |
| `/api/health/`          | GET                           | Liveness + database check                   |

Unknown values for choice filters (`location_type`, `movement_type`) or non-numeric ids return
`400 Bad Request`.

`reservable=true` restricts stock to active products, locations, and warehouses with
`available_quantity > 0`; `false` or omission leaves the regular on-hand stock list unchanged.
Both stock tables and exports retain their existing filters and include fully reserved stock.

Deleting a warehouse, location or product that is referenced elsewhere returns `409 Conflict`;
deactivate it (`PATCH {"is_active": false}`) instead.

### Barcodes and optional lot tracking

Products and locations have an optional `barcode` string (maximum 100 characters), unique within
each model; a product and a location may share the same barcode. Omitted, null, and empty values
are allowed; empty barcodes are stored as null, so multiple records can have no barcode. Leading
zeros are preserved. Use `?barcode=000123` for exact, case-sensitive scanner lookup; general
`search` also includes barcodes. Product and location forms and admin expose the field.
Scanner device integration and barcode image/label generation are not included.

`Product.lot_tracked` defaults to `false`. Ordinary SKUs do not need lot numbers or expiry data
and must omit `lot` (or send null) on movements. For a tracked SKU, create a lot first:

```bash
curl -X POST localhost:8000/api/products/ -H 'Content-Type: application/json' \
  -d '{"sku": "TRACKED-1", "name": "Tracked item", "barcode": "000123", "lot_tracked": true}'
curl -X POST localhost:8000/api/lots/ -H 'Content-Type: application/json' \
  -d '{"product": 1, "lot_number": "LOT-2026-01", "expiry_date": "2027-01-31"}'
curl -X POST localhost:8000/api/movements/ -H 'Content-Type: application/json' \
  -d '{"movement_type": "inbound", "product": 1, "lot": 1, "to_location": 1, "quantity": 25}'
```

Replace example IDs with the IDs returned by your API. Lots belong to one tracked product, with
a required `lot_number` unique within that product.
`expiry_date` is required for every new lot, using `YYYY-MM-DD`, in the API, admin, and demo
command. Product and lot creation remain separate: create a tracked product, then its dated lot,
before recording stock or movements. Existing undated lots remain unchanged and usable; expiry
cannot be removed from a dated lot. The database column remains nullable for legacy data.
Every movement of a tracked SKU,
including transfers, adjustments, and batch lines, must specify a lot belonging to that SKU.
A transfer keeps the same lot at its destination. Stock uniqueness is now product/location/lot;
the null lot represents untracked inventory, with only one untracked row per product/location.

Reservations target a specific stock row and therefore a specific lot. Fulfillment retains that
lot on its outbound movement; another lot's stock cannot cover a shortage or consume its holds.
Product totals and warehouse summaries still sum all lots. Stock, movement, and reservation
responses expose `lot` (the numeric ID), `lot_number`, and `expiry_date`; filter by numeric `lot` ID
or search lot numbers.

The Números de lote UI and admin manage lots. The movement form requires a product-specific lot only when
the selected SKU is tracked; inventory/history/reservation tables display traceability metadata.
Expiry is descriptive: there is no automatic FEFO allocation, expiry blocking,
or lot relabeling/splitting/merging.

Tracking mode may only change before stock, lots, movement history, or reservation history exist.
A lot's product cannot change; its lot number and expiry become immutable once stock or movements
reference it. Used lots and reservation-linked stock are protected from deletion. Existing products
remain untracked after migration; existing stock IDs, movements, reservations, and quantities are
preserved, with null lot IDs. This migration does not infer historical lots.

Migration `0007` renames existing lot codes to `lot_number`, preserving lot IDs, expiry dates,
and stock/movement/reservation links. It removes the former `batch` field and its stored values.
API clients must use `lot_number` instead of `code` on lots and `lot_code` on traceability responses.
The multi-line movement batch endpoint is unchanged.

### Stock movements

Stock levels change only by recording movements; each one is applied atomically and rejected
if it would make stock negative. `quantity` is always a positive integer.

| `movement_type` | `from_location` | `to_location`  | Effect                           |
| --------------- | --------------- | -------------- | -------------------------------- |
| `inbound`       | —               | required       | Receive stock                    |
| `outbound`      | required        | —              | Pick / ship stock                |
| `transfer`      | required        | required       | Move stock between locations     |
| `adjustment`    | one of the two  | one of the two | Increase (to) or decrease (from) |

Transfers may move stock between locations in **different warehouses**; this is intentional.

`occurred_at` records when the physical movement happened. Supply an ISO-8601 timestamp when
creating a movement to enter a late receipt; omitting it defaults to the current time. Null or
invalid timestamps return `400 Bad Request`. `created_at` remains the read-only, server-generated
audit timestamp recording when the entry was created. Both timestamps are returned by the API
and displayed in the movement history and admin. The movement form accepts a local date/time
and converts it to UTC before submission; leaving it blank uses the current time.

Existing movements are backfilled with `occurred_at = created_at` during migration. Backdating
does not replay historical balances or defer stock changes: the current stock is updated
immediately, with the same insufficient-stock checks. Entries remain immutable after creation.

Movement history supports inclusive ISO-8601 datetime bounds through `created_at_after` /
`created_at_before` (audit time) and `occurred_at_after` / `occurred_at_before` (event time).
Use `Z` or an explicit UTC offset; timestamps without an offset use the
configured Django time zone. Reversed ranges and invalid timestamps return `400 Bad Request`.
`warehouse` and `location` accept positive numeric IDs and match either the source or destination,
including cross-warehouse transfers. `reference` is a case-insensitive substring match.
All filters combine with AND and can be used alongside `search`.

The movement-history UI provides SKU/reference search, a type filter, and an inclusive date range
on `occurred_at`. Dates cover whole days in the browser's local time zone and are sent as UTC bounds.
Reversed ranges show an error instead of querying or exporting.

Movement history and stock tables have **Exportar CSV** buttons. Each export includes all pages
matching the current filters (including the stock warehouse/search filters), not just the visible
page. Columns match the tables; timestamps use the API's ISO-8601 values. Files use UTF-8 with a BOM,
quoted fields, and CRLF rows. Formula-like text is prefixed with an apostrophe for spreadsheet safety.
An empty result exports only the headers; failed requests show an error and do not download a partial file.
Exports are fetched page by page, not as a transactionally consistent database snapshot.

Use `ordering` with comma-separated `created_at`, `occurred_at`, `id`, or `quantity`; prefix a field with `-` for
descending order. The default remains `-created_at,-id`. An ID tie-breaker is appended if absent,
using the direction of the first ordering field, for stable pagination. Unsupported ordering
fields return `400 Bad Request`. Empty filter values are ignored.

```bash
curl -G localhost:8000/api/movements/ \
  --data-urlencode 'product=1' \
  --data-urlencode 'created_at_after=2026-11-01T00:00:00Z' \
  --data-urlencode 'warehouse=1' \
  --data-urlencode 'reference=PO-' \
  --data-urlencode 'ordering=created_at'
```

A product is reported as **low stock** when its `reorder_level` is greater than `0` and its total
quantity on hand is at or below that level. `reorder_level = 0` disables the alert for the product.

The summary report's `products` count includes only active products, matching the population eligible
for low-stock alerts (including active products with alerts disabled). The global `low_stock` list
is ordered by quantity then SKU and capped at 50 rows; the top-level `low_stock_count` is the full,
uncapped number of active products with a positive reorder level whose on-hand quantity is at or
below that level. It is zero when no products qualify. The dashboard labels the product count as
active and shows the global alert count and any truncation.

The summary report also includes `warehouse_breakdown`,
ordered by warehouse code. Each entry contains `id`, `code`, `name`, `is_active`, `total_units`,
`low_stock_count`, and `low_stock`. Local low-stock rows have the same shape as the global list
(`id`, `sku`, `name`, `total_quantity`, `reorder_level`), but `total_quantity` sums only that
warehouse's locations. The existing product threshold is applied independently in every warehouse;
there is no per-warehouse threshold or product-assortment configuration.

All warehouses are included, even inactive ones or those with no locations. A monitored product
with no stock row in a warehouse counts as zero, so a full Guadalajara warehouse cannot hide an
empty Monterrey warehouse. Only active products with a positive reorder level produce alerts.
Warehouse totals include all on-hand stock, including inactive products/locations, consistent with
the global inventory total. Reservations do not reduce these on-hand totals or trigger alerts.
Each local list is sorted by quantity then SKU and capped at 50 rows; `low_stock_count` reports the
uncapped count. The dashboard displays each warehouse's total, alerts, and any truncation.

```bash
curl -X POST localhost:8000/api/movements/ -H 'Content-Type: application/json' \
  -d '{"movement_type": "inbound", "product": 1, "to_location": 1, "quantity": 25, "reference": "PO-1001", "occurred_at": "2026-11-04T10:00:00-06:00"}'
```

### Atomic movement batches

Submit a multi-line receipt, shipment, or mixed set of movements with one request:

```bash
curl -X POST localhost:8000/api/movements/batch/ -H 'Content-Type: application/json' \
  -d '[
    {"movement_type": "inbound", "product": 1, "to_location": 1, "quantity": 25, "reference": "PO-1002"},
    {"movement_type": "inbound", "product": 2, "to_location": 1, "quantity": 10, "reference": "PO-1002"}
  ]'
```

The body must be a nonempty array of the same payloads accepted by the single-movement endpoint,
including optional `occurred_at`. A successful request returns `201` with the created movements
in input order, not a paginated envelope. There is no separate line-count cap; existing request-size
limits still apply.

All lines are validated before writing. Missing stock rows are ensured in product/location order,
then all affected stock rows are locked once in primary-key order and held until the single
transaction commits. Lines apply in request order: earlier receipts can fund later outbound lines,
but a later receipt cannot fund an earlier outbound line. Repeated product/location pairs share
the running balance.

Any failure rolls back the entire batch, including ledger entries, stock changes, and newly created
stock rows. Validation and insufficient-stock errors return `400`; line errors are an array aligned
with the request (zero-based index), with empty objects for other lines, for example
`[{}, {"quantity": "Insufficient stock at MX1/A-01: 5 available."}]`. Invalid top-level payloads
return a `non_field_errors` object. Single-movement requests keep their existing response/error shape
and use the same locking and stock-update path.

### Reservations and basic picking

On-hand stock remains `StockItem.quantity`, reconciled against the movement ledger. Reservations
do not create movements or change on-hand quantities:

```
reserved_quantity = sum(quantity of active reservations for this stock item)
available_quantity = quantity - reserved_quantity
```

Stock list/detail responses include `quantity`, `reserved_quantity`, and `available_quantity`.
Fully reserved stock remains visible with zero available quantity. Product list/detail responses
include `total_quantity`, `total_reserved`, and `total_available` across all locations. Low-stock
alerts and the dashboard's inventory total continue to use on-hand quantities, not availability.

Create a hold against a stock item ID returned by `/api/stock/`:

```bash
curl -X POST localhost:8000/api/reservations/ -H 'Content-Type: application/json' \
  -d '{"stock_item": 1, "quantity": 3, "reference": "ORDER-1001"}'
curl -X POST localhost:8000/api/reservations/1/fulfill/ -H 'Content-Type: application/json' -d '{}'
# Or cancel the hold instead of fulfilling it:
# curl -X POST localhost:8000/api/reservations/1/release/ -H 'Content-Type: application/json' -d '{}'
```

Creation returns `201` with an `active` reservation; quantity must be a positive integer no greater
than available stock, and an external order/reference string is required. Multiple holds may share
a reference. Product, location, and warehouse must be active.

Release returns `200`, changes status to `released`, and frees availability without touching the
ledger; it is allowed even if the product/location/warehouse has since been deactivated. Fulfill
returns `200`, changes status to `fulfilled`, and atomically creates a single outbound movement
for the entire held quantity with the same reference. The response's `movement` is its ledger ID.
Fulfillment requires active inventory and sufficient on-hand stock to preserve other holds.
Either action accepts an empty JSON object. A repeat action or an attempt to transition a closed
reservation returns `400`; use the reservation ID to check its state after an uncertain response.
Reservation fields cannot be edited or deleted via the API. The admin allows direct edits to
existing reservations as described above; superusers may also create and delete them.

Service-based reservation creation, release, fulfillment, and movement recording serialize using stock-row locks.
Ordinary outbound movements, transfers, and negative adjustments may only consume available stock,
including in batches. Fulfillment consumes its own hold, never another reservation's allocation.
Each operation is atomic; a failure leaves status, quantities, and ledger unchanged.

The Reservations UI creates holds and offers **Liberar** (release) and **Surtir** (fulfill); inventory
and product tables show on-hand, reserved, and available amounts. Admin release/fulfill actions
operate per reservation and report individual failures; selecting multiple rows is not a batch
transaction.

This is a stock-allocation primitive for basic picking, not an order model or a complete pick/pack
workflow. There is no expiry, partial fulfillment, multi-line reservation batch, or reservation
transfer yet. To move held stock, release the hold, transfer stock, then create a new reservation.
