"""
main.py — FastAPI application entry point.

Responsibilities (and only these — no business logic lives here):
  1. Configure logging once, before anything else runs.
  2. Define the lifespan context manager that:
       a. Confirms DB connectivity at startup — refuses to boot if unreachable.
       b. Confirms Redis connectivity at startup — refuses to boot if unreachable.
       c. Logs clean shutdown on exit.
  3. Instantiate the FastAPI app with the lifespan handler attached.
  4. Register routers.

Why lifespan instead of @app.on_event("startup")?
  @app.on_event is deprecated as of FastAPI 0.93.  The lifespan context manager
  is the current recommended pattern; it also makes startup/shutdown symmetric
  and easy to test in isolation.

Why fail-fast on DB *and* Redis unreachability at boot?
  Both are hard dependencies of this service.  DB: every evaluation needs flag
  data.  Redis: without the cache layer, every evaluation hits Postgres directly
  — under real load this would silently overload Neon without any visible signal
  until it became a crisis.  The same policy applies to both: fail loudly at
  startup so Kubernetes never routes traffic to a pod in a degraded state.
"""

import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import text

from app.cache import redis_client
from app.config import settings
from app.database import SessionLocal
from app.routers import auth, evaluate, flags, health, metrics

# ---------------------------------------------------------------------------
# Logging — configured before the app object is created so that any import-
# time log calls (rare but possible) are captured with the right format.
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """
    Set up the root logger once.  All module-level loggers (via
    logging.getLogger(__name__)) inherit this configuration automatically.
    """
    log_level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


_configure_logging()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup DB connectivity check
# ---------------------------------------------------------------------------

def _assert_db_reachable() -> None:
    """
    Open a session and run SELECT 1.  If anything fails, log a clear diagnostic
    and raise so that the lifespan handler propagates the error and uvicorn
    exits — no routes are ever served in a broken state.
    """
    logger.info("Startup: verifying database connectivity…")
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        logger.info("Startup: database connectivity confirmed ✓")
    except Exception as exc:
        logger.critical(
            "Startup: FAILED — cannot reach the database. "
            "The application will not start. "
            "Check DATABASE_URL and network access. Error: %s",
            exc,
        )
        # Re-raise so the lifespan context propagates the failure and the
        # process exits with a non-zero code (uvicorn surfaces this clearly).
        raise RuntimeError("Database unreachable at startup") from exc
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Startup Redis connectivity check
# ---------------------------------------------------------------------------

def _assert_redis_reachable() -> None:
    """
    Ping Redis.  If the ping fails, log a clear diagnostic and raise so that
    the lifespan handler propagates the failure and the process exits with a
    non-zero code.

    Why fail-fast here?
    Without Redis, every /evaluate call falls back to hitting Postgres directly.
    That silent degradation defeats the entire purpose of the caching layer and
    could overload Neon under real traffic without anyone noticing until it's a
    crisis.  Failing loudly at startup surfaces the misconfiguration immediately
    (bad REDIS_URL, Redis container not running, network policy blocking the
    port) so it can be fixed before any traffic is served.
    """
    logger.info("Startup: verifying Redis connectivity…")
    try:
        redis_client.ping()
        logger.info("Startup: Redis connectivity confirmed ✓")
    except Exception as exc:
        logger.critical(
            "Startup: FAILED — cannot reach Redis. "
            "The application will not start. "
            "Check REDIS_URL and ensure Redis is running. Error: %s",
            exc,
        )
        raise RuntimeError("Redis unreachable at startup") from exc


# ---------------------------------------------------------------------------
# Lifespan — startup + shutdown lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ---- startup ----
    logger.info("feature-flag-service starting up…")
    _assert_db_reachable()          # aborts boot if DB is down
    _assert_redis_reachable()       # aborts boot if Redis is down
    logger.info("feature-flag-service is ready to serve requests.")

    yield  # app runs here

    # ---- shutdown ----
    logger.info("feature-flag-service shutting down.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Feature Flag Service",
    description=(
        "Internal service for managing and evaluating feature flags "
        "across dev / staging / prod environments."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Router registration — all route logic lives in routers/, never in this file
# ---------------------------------------------------------------------------

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(flags.router)
app.include_router(evaluate.router)
app.include_router(metrics.router)
