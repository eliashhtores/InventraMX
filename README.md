# InventraMX
Warehouse Management System (WMS): inventory control, inbound/outbound, locations, picking and reporting.

## Stack

| Layer          | Technology                                                                 |
| -------------- | -------------------------------------------------------------------------- |
| Backend        | Django 5.2 + Django REST Framework — JSON API only (no template rendering) |
| Database       | PostgreSQL 17, configured through environment variables                    |
| Frontend       | Static HTML5 / CSS3 / Vanilla JS + Tailwind (CDN), data loaded via XHR     |
| Orchestration  | Docker Compose (`web` + `db`)                                              |

```
backend/            Django project (config/) and the inventory app (models, API, tests)
frontend/           Static single-page UI (index.html, css/, js/api.js, js/app.js)
Dockerfile          Image for the web service (gunicorn + WhiteNoise)
docker-compose.yml  web + db services
.env.example        Environment variables used by Docker Compose
```

The `web` container serves the REST API under `/api/` and the static frontend from `/`
(via WhiteNoise), so the UI and the API share the same origin.

## Quick start

```bash
cp .env.example .env        # optional — sensible defaults are built in
docker compose up --build
```

Open http://localhost:8000. Migrations are applied automatically when the `web` container starts.

The `frontend/` directory is bind-mounted into the container, so edits to HTML/CSS/JS are visible
after a browser refresh.

### Configuration

| Variable                 | Default                         | Description                                  |
| ------------------------ | ------------------------------- | -------------------------------------------- |
| `DJANGO_DEBUG`           | `1` (Compose)                   | Debug mode. Set to `0` in production.        |
| `DJANGO_SECRET_KEY`      | —                               | Required when `DJANGO_DEBUG=0`.              |
| `DJANGO_ALLOWED_HOSTS`   | `localhost,127.0.0.1,0.0.0.0`   | Comma-separated host names.                  |
| `POSTGRES_DB`            | `inventramx`                    | Database name.                               |
| `POSTGRES_USER`          | `inventramx`                    | Database user.                               |
| `POSTGRES_PASSWORD`      | `inventramx`                    | Database password.                           |
| `POSTGRES_HOST` / `PORT` | `db` / `5432`                   | Database host/port (set by Compose).         |
| `WEB_PORT`               | `8000`                          | Host port published for the web service.     |

> **Note:** the API currently has no authentication and is intended for local/trusted use.

## Tests

```bash
docker compose exec web python manage.py test
```

## REST API

All endpoints are under `/api/` and return JSON. List endpoints are paginated
(`?limit=&offset=`, max 1000) and support `?search=`.

| Endpoint                   | Methods                      | Filters                                     |
| -------------------------- | ---------------------------- | ------------------------------------------- |
| `/api/warehouses/`         | GET, POST, PUT, PATCH, DELETE |                                            |
| `/api/locations/`          | GET, POST, PUT, PATCH, DELETE | `warehouse`, `location_type`, `is_active`  |
| `/api/products/`           | GET, POST, PUT, PATCH, DELETE | `is_active`                                |
| `/api/stock/`              | GET (read-only)              | `product`, `location`, `warehouse`          |
| `/api/movements/`          | GET, POST (append-only)      | `movement_type`, `product`                  |
| `/api/reports/summary/`    | GET                          | Totals, movement counts, low-stock products |
| `/api/health/`             | GET                          | Liveness + database check                   |

Deleting a warehouse, location or product that is referenced elsewhere returns `409 Conflict`;
deactivate it (`PATCH {"is_active": false}`) instead.

### Stock movements

Stock levels change only by recording movements; each one is applied atomically and rejected
if it would make stock negative. `quantity` is always a positive integer.

| `movement_type` | `from_location` | `to_location` | Effect                         |
| --------------- | --------------- | ------------- | ------------------------------ |
| `inbound`       | —               | required      | Receive stock                  |
| `outbound`      | required        | —             | Pick / ship stock              |
| `transfer`      | required        | required      | Move stock between locations   |
| `adjustment`    | one of the two  | one of the two| Increase (to) or decrease (from) |

```bash
curl -X POST localhost:8000/api/movements/ -H 'Content-Type: application/json' \
  -d '{"movement_type": "inbound", "product": 1, "to_location": 1, "quantity": 25, "reference": "PO-1001"}'
```
