"""
routers/health.py — GET /health

Purpose
-------
Proves end-to-end DB connectivity on every call, not just app liveness.  A
200 response means "the app is up AND the database is reachable"; a 503 means
"the app is up but the database is down."  This distinction is intentional:

  - Kubernetes liveness probe:  200 → pod is healthy, keep it running
  - Kubernetes readiness probe: 503 → pod is not ready, stop sending traffic

Using 503 (Service Unavailable) rather than 500 (Internal Server Error) is the
correct HTTP semantic for "I am running but a dependency I need is unreachable."

The DB failure path is caught and handled explicitly here — a health-check that
itself throws an unhandled exception defeats its purpose.
"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness + readiness probe",
    response_description="Service and database status",
)
def health_check(db: Session = Depends(get_db)) -> JSONResponse:
    """
    Execute a trivial DB query to confirm connectivity.

    Returns
    -------
    200  {"status": "ok",    "database": "connected"}   — all systems nominal
    503  {"status": "error", "database": "unreachable"} — DB unreachable
    """
    try:
        db.execute(text("SELECT 1"))
        logger.info("Health check passed: database is connected")
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "database": "connected"},
        )
    except Exception as exc:  # noqa: BLE001
        # Log the full exception so it appears in container/pod logs, but do
        # not let it propagate — a crashing health endpoint is useless.
        logger.error("Health check failed: database unreachable — %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unreachable"},
        )
