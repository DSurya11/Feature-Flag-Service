# =============================================================================
# Dockerfile — Feature Flag Service
# Multi-stage build: builder (compile deps) → runtime (lean final image)
#
# Stage separation rationale:
#   The builder stage installs build tools needed to compile any native
#   extension wheels.  Those tools (gcc, libc headers, etc.) are NOT copied
#   to the final runtime image — the final image contains only the installed
#   Python packages and application code.  This keeps the runtime image small
#   and eliminates build-only attack surface.
#
# Base image pinning:
#   Using python:3.12-slim (not python:3-slim or latest).  Unpinned tags are
#   re-pushed upstream whenever a new minor version ships, which means your
#   "reproducible" container silently gets a different interpreter the next
#   time you docker build.  The minor version is pinned explicitly here.
#   NOTE: the dev machine runs Python 3.14, but 3.12-slim is the stable LTS
#   slim variant with broad wheel availability.  If you need 3.14, swap the
#   tag — the rest of the Dockerfile is version-agnostic.
#
# Security:
#   • No secrets (DATABASE_URL, REDIS_URL, JWT_SECRET_KEY) are baked in.
#     All secrets arrive via environment variables at container *run* time,
#     either via `docker run -e` or docker-compose's `env_file:` / `environment:`.
#   • The container runs as a dedicated non-root user (appuser).
#     Running production containers as root is a commonly-flagged security
#     issue; this is a deliberate hardening step.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1 — builder
# Purpose: install Python dependencies (may require build tools).
# Nothing from this stage except site-packages reaches the final image.
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Install build tools that some Python packages need to compile native
# extensions.  gcc is the most common requirement; add others if needed.
# These are intentionally NOT present in the runtime stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# --- Layer-cache optimisation ---
# Copy ONLY requirements.txt first, then run pip install.
# This means: if you change application code but not requirements.txt,
# Docker reuses the cached pip-install layer — no reinstall on every build.
# If you copy all source code before pip install, every code change forces
# a full reinstall, defeating layer caching entirely.
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2 — runtime
# Purpose: minimal image that actually runs the app.
# Copies installed packages from builder; never sees gcc or other build tools.
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Create a dedicated non-root user.
# Running as root inside a container is flagged by container security scanners
# and violates the principle of least privilege.  If the process is somehow
# compromised, a non-root user limits the blast radius significantly.
RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --no-create-home appuser

WORKDIR /app

# Copy only the installed site-packages from the builder — NOT gcc, headers,
# or any other build artefact.  This is the whole point of the multi-stage build.
COPY --from=builder /install /usr/local

# Copy application source code.
# alembic/ and alembic.ini are included so DB migrations can be run from
# inside the container if needed (e.g., a one-off migration job).
COPY app/       ./app/
COPY alembic/   ./alembic/
COPY alembic.ini .

# Hand ownership of the working directory to the non-root user so the process
# can write any temporary files it needs (e.g. SQLite in tests, log files).
RUN chown -R appuser:appgroup /app

# Switch to non-root user for all subsequent RUN/CMD/ENTRYPOINT instructions.
USER appuser

# Document the port the app listens on.
# EXPOSE is metadata only — it does NOT publish the port.  Actual port mapping
# happens in docker-compose.yml (`ports:`) or `docker run -p 8000:8000`.
EXPOSE 8000

# ---------------------------------------------------------------------------
# HEALTHCHECK
# Docker (and docker-compose) uses this to determine container health status,
# visible in `docker ps` as "(healthy)" / "(unhealthy)".
# This is the same /health endpoint used in Step 2, and it directly previews
# what Kubernetes liveness/readiness probes will call in later steps — same
# endpoint, just a different invoking mechanism.
#
# Parameters chosen conservatively for local dev:
#   --interval=30s  check every 30 seconds
#   --timeout=5s    fail the check if /health doesn't respond in 5 seconds
#   --retries=3     mark unhealthy only after 3 consecutive failures
#   --start-period=10s  give the app 10 seconds to boot before checks begin
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

# ---------------------------------------------------------------------------
# CMD — start the app.
#
# Critical: bind to 0.0.0.0, NOT 127.0.0.1 or localhost.
# Inside a container, the loopback interface (127.0.0.1) is isolated — nothing
# outside the container (host, other containers, load balancers) can reach a
# process bound to loopback.  0.0.0.0 binds to all interfaces, including the
# virtual Ethernet interface that connects the container to the Docker network.
# Binding to localhost inside a container is one of the most common first-time
# Docker networking mistakes; it manifests as "connection refused" from outside.
# ---------------------------------------------------------------------------
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
