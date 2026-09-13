# Feature Flag Service

Internal FastAPI service for managing and evaluating feature flags across `dev`, `staging`, and `prod` environments.

---

## Setup

```bash
cp .env.example .env          # fill in DATABASE_URL and JWT_SECRET_KEY
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head           # run DB migrations
uvicorn app.main:app --reload
```

Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## API Overview

| Method   | Path                     | Auth    | Description                              |
|----------|--------------------------|---------|------------------------------------------|
| `POST`   | `/auth/register`         | —       | Register a new user                      |
| `POST`   | `/auth/login`            | —       | Obtain a JWT (Bearer token)              |
| `GET`    | `/health`                | —       | Liveness check                           |
| `POST`   | `/flags`                 | admin   | Create a feature flag                    |
| `GET`    | `/flags`                 | any     | List flags (`?environment=` `?flag_type=`)|
| `GET`    | `/flags/{id}`            | any     | Get a single flag                        |
| `PATCH`  | `/flags/{id}`            | admin   | Partially update a flag                  |
| `DELETE` | `/flags/{id}`            | admin   | Delete a flag                            |
| `GET`    | `/flags/{id}/history`    | any     | Audit history for a flag (newest first)  |
| `POST`   | `/evaluate`              | any     | Evaluate a flag for a user               |
| `GET`    | `/metrics`               | **none**| Prometheus metrics (no auth — see below) |

---

## Flag Types

| `flag_type`    | `rollout_percentage` | Notes                                         |
|----------------|----------------------|-----------------------------------------------|
| `boolean`      | must be absent       | Simple on/off flag                            |
| `targeted`     | must be absent       | Evaluated against targeting rules             |
| `percentage`   | **required** (0–100) | Deterministic hash-based rollout              |

---

## Audit Log

Every mutation (`POST`, `PATCH`, `DELETE`) writes an `audit_log` row atomically in the same transaction as the flag change. Each row records:

- `action` — `created`, `updated`, or `deleted`
- `actor` — username of the authenticated user
- `old_value` — JSON snapshot of the flag state before the change (`null` for `created`)
- `new_value` — JSON snapshot after the change (`null` for `deleted`)

### Known Limitation — Deleted-flag audit history

When a flag is deleted, the database's `ON DELETE SET NULL` constraint sets `audit_log.flag_id = NULL` on all audit rows for that flag (including the `deleted` entry). Because `GET /flags/{id}/history` queries by `flag_id`, it cannot retrieve history for deleted flags — it will return HTTP 404 since the flag no longer exists.

The audit rows **are** preserved in the database with `flag_id = NULL`; they are simply not queryable through this endpoint. A future extension could store the flag `name` redundantly on each audit row to enable lookup by name post-deletion. This is a known design trade-off, not a bug.

---

## Observability — Prometheus Metrics (`GET /metrics`)

The service exposes four Prometheus metrics at `GET /metrics` in standard text exposition format (consumed directly by a Prometheus scraper):

| Metric | Type | Labels | Operational question answered |
|--------|------|--------|-------------------------------|
| `flag_evaluations_total` | Counter | `reason`, `environment` | Is fail-safe firing? Are callers referencing deleted/misspelled flags? |
| `flag_evaluations_cache_result_total` | Counter | `result` (`hit`\|`miss`\|`redis_unavailable`) | Is the cache layer actually working under real traffic? |
| `flag_evaluation_duration_seconds` | Histogram | _(none)_ | Are we meeting the sub-50ms latency requirement? |
| `flag_mutations_total` | Counter | `action` (`created`\|`updated`\|`deleted`) | How much flag churn is happening? Useful for incident correlation. |

### `/metrics` Auth — Deliberate No-Auth Decision

`GET /metrics` requires **no authentication**. This is an explicit, deliberate scope decision:

- Prometheus's scraper is a separate infrastructure component that cannot hold a JWT and operates on a fixed scrape interval.
- Forcing auth here would require a second auth mechanism (e.g. a static bearer token) just for the scraper — out of scope for this portfolio project.
- In a real production deployment, the `/metrics` port would be firewalled to cluster-internal traffic only (network policy, not application-layer auth), which is the standard Kubernetes pattern for Prometheus scraping.

This mirrors the `/evaluate` auth limitation note: both are real production gaps that are documented explicitly rather than silently ignored.

---

## Error Responses

| Scenario                              | HTTP Status | Notes                                      |
|---------------------------------------|-------------|--------------------------------------------|
| Missing / invalid JWT                 | 401         | `WWW-Authenticate: Bearer` header included |
| Valid JWT, wrong role                 | 403         | —                                          |
| Resource not found                    | 404         | Message identifies what was missing        |
| Duplicate `(name, environment)` pair  | 409         | DB `IntegrityError` translated, never 500  |
| Pydantic / cross-field validation     | 422         | FastAPI default; validator message included|

---

## Cascade Behaviour (Step 1 schema)

- `targeting_rules` → `ON DELETE CASCADE`: deleting a flag removes all its targeting rules automatically.
- `audit_log.flag_id` → `ON DELETE SET NULL`: audit history is **preserved** after flag deletion; `flag_id` becomes `NULL`.
- `audit_log.actor` stores the username string (not a FK) so history survives user-account deletion.

---

## Project Structure

```
feature-flag-service/
├── app/
│   ├── main.py          # App entry point, lifespan, router registration
│   ├── config.py        # Settings (pydantic-settings, .env loading)
│   ├── database.py      # Engine, SessionLocal, Base, get_db dependency
│   ├── models.py        # SQLAlchemy ORM models
│   ├── schemas.py       # Pydantic request/response schemas
│   ├── auth.py          # bcrypt hashing + JWT create/verify
│   ├── audit.py         # Shared audit_log insert helper
│   ├── dependencies.py  # get_current_user, require_admin FastAPI deps
│   └── routers/
│       ├── auth.py      # POST /auth/register, POST /auth/login
│       ├── health.py    # GET /health
│       └── flags.py     # Flag CRUD + GET /flags/{id}/history
├── alembic/             # DB migrations
├── tests/
├── .env.example
├── requirements.txt
└── docker-compose.yml
```
