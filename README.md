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
├── k8s/
│   ├── namespace.yaml         # feature-flag namespace
│   ├── secret-template.yaml   # placeholder — real secrets applied imperatively
│   ├── redis-deployment.yaml  # Redis (single replica, no PV)
│   ├── redis-service.yaml     # ClusterIP → redis.feature-flag.svc.cluster.local
│   ├── api-deployment.yaml    # API (replicas:2, SHA-pinned image, probes)
│   └── api-service.yaml       # ClusterIP → feature-flag-api:8000
├── alembic/             # DB migrations
├── tests/
├── .env.example
├── requirements.txt
└── docker-compose.yml
```

---

## Step 10 — Kubernetes (`kind` cluster)

### Prerequisites

- [`kind`](https://kind.sigs.k8s.io/) and `kubectl` installed
- Docker running
- A GitHub PAT with **`read:packages` scope only** (for GHCR image pull)

### 1. Create the cluster

```bash
kind create cluster --name feature-flag-cluster
kubectl cluster-info --context kind-feature-flag-cluster
```

### 2. Apply the namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

### 3. Create secrets (imperatively — never committed)

```bash
# GHCR image pull credentials (read:packages PAT)
kubectl create secret docker-registry ghcr-pull-secret \
  --namespace feature-flag \
  --docker-server=ghcr.io \
  --docker-username=DSurya11 \
  --docker-password=<YOUR_PAT_read_packages_only> \
  --docker-email=any@email.com

# App credentials
kubectl create secret generic feature-flag-secrets \
  --namespace feature-flag \
  --from-literal=DATABASE_URL='<your-neon-url>' \
  --from-literal=REDIS_URL='redis://redis.feature-flag.svc.cluster.local:6379' \
  --from-literal=JWT_SECRET_KEY='<your-hex-secret>'
```

> **Secrets strategy:** Secrets are applied manually (imperatively) because this project does not yet have Sealed Secrets or External Secrets Operator — both are listed as stretch goals. The imperative approach means the cluster holds real values; Git holds only the placeholder `secret-template.yaml`. ESO/Sealed Secrets would replace this in a more mature setup.

### 4. Deploy Redis and the API

```bash
kubectl apply -f k8s/redis-deployment.yaml
kubectl apply -f k8s/redis-service.yaml
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/api-service.yaml
```

### 5. Verify

**All pods running:**
```bash
kubectl get pods -n feature-flag
# Expected: 2 feature-flag-api pods + 1 redis pod, STATUS=Running, READY=1/1
```

**Health check via port-forward (proves DB connectivity from inside the cluster):**
```bash
kubectl port-forward svc/feature-flag-api 8000:8000 -n feature-flag &
curl -s localhost:8000/health | python3 -m json.tool
# Expected: {"status": "ok", "database": "connected"}
kill %1
```

**Multi-replica Redis cache consistency (the flagship test):**

Get a token first:
```bash
kubectl port-forward svc/feature-flag-api 8000:8000 -n feature-flag &
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=<user>&password=<pass>' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
kill %1
```

Run 20 `/evaluate` calls from **inside** the cluster so kube-proxy's iptables rules apply (real round-robin, not port-forward's single-pod binding):
```bash
kubectl run curl-test --rm -it \
  --image=curlimages/curl \
  --restart=Never \
  -n feature-flag -- \
  sh -c 'for i in $(seq 1 20); do \
    curl -s -X POST http://feature-flag-api:8000/evaluate \
      -H "Authorization: Bearer '$TOKEN'" \
      -H "Content-Type: application/json" \
      -d "{\"flag_name\":\"<your-flag>\",\"user_id\":\"test-user-123\",\"environment\":\"prod\"}"; \
    echo; done'
```

Check (a) all 20 responses are identical (shared Redis cache hit) and (b) both pods handled requests:
```bash
kubectl logs -l app=feature-flag-api -n feature-flag --all-containers | grep "test-user-123"
# Must show log lines from BOTH pods — otherwise load-balancing didn't occur
```

**Pod self-healing:**
```bash
kubectl delete pod -n feature-flag -l app=feature-flag-api --wait=false
kubectl get pods -n feature-flag -w
# Replacement pods appear within seconds — no manual intervention
```

**Readiness failure without crash loop (broken secret test):**
```bash
kubectl delete secret feature-flag-secrets -n feature-flag
kubectl create secret generic feature-flag-secrets \
  --namespace feature-flag \
  --from-literal=DATABASE_URL='postgresql://invalid:invalid@localhost/bogus' \
  --from-literal=REDIS_URL='redis://redis.feature-flag.svc.cluster.local:6379' \
  --from-literal=JWT_SECRET_KEY='test'
kubectl rollout restart deployment/feature-flag-api -n feature-flag
kubectl get pods -n feature-flag -w
# READY column: 0/1 (readiness probe failing → traffic stopped)
# STATUS:       Running (NOT CrashLoopBackOff — liveness failureThreshold=5 not yet crossed)
# Restore: re-apply the real secret and rollout restart again
```

### Probe design — liveness vs. readiness

Both probes target `GET /health` (returns 503 when DB is unreachable):

| Probe | `failureThreshold` | Effect of failure |
|---|---|---|
| Readiness | 3 (30 s) | Stop routing traffic — DB is down, pod can't serve requests |
| Liveness | 5 (50 s) | Restart pod — more lenient because restarting won't fix an external DB outage |

**Known simplification:** a production setup would have a separate `/livez` endpoint that checks only process health (not DB connectivity) for liveness. The lenient-threshold approach is the honest trade-off for a portfolio project — documented here rather than hidden.

### Known simplifications

| Simplification | Production equivalent |
|---|---|
| Redis: single-replica Deployment, no PV | StatefulSet + PVC + Redis Sentinel/Cluster |
| Secrets: applied imperatively | External Secrets Operator or Sealed Secrets |
| No Ingress | ingress-nginx or cloud load balancer |
| Liveness uses `/health` (DB check) | Separate `/livez` endpoint (process-only check) |

## Step 11 — Terraform

Terraform manages the existing Neon project via `terraform import`, adopting an already-provisioned resource rather than creating a new one — this mirrors real-world 'brownfield' infrastructure adoption. Terraform state is stored locally for this project; a production setup would use a remote backend (e.g. Terraform Cloud or S3 with locking) for team/CI use. Terraform's scope here covers the Neon project itself; a full AWS RDS-based setup would additionally require VPC, subnet, and security-group resources, which are out of scope given the Neon-based architecture chosen in Step 1 for cost reasons.

