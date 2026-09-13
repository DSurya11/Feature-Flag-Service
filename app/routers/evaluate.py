"""
routers/evaluate.py — POST /evaluate endpoint.

This is the hot path — the endpoint every other service calls to determine
whether a feature flag is enabled for a specific user.

Design decisions vs. the CRUD endpoints
----------------------------------------
1.  POST instead of GET: the request body includes an optional `attributes`
    dict (arbitrary key/value pairs for targeted-flag evaluation).  Passing
    nested JSON as a GET query parameter is awkward and non-standard; POST
    with a JSON body is the correct choice once attributes are in scope.

2.  Auth: requires any valid JWT (get_current_user), consistent with the rest
    of the API.  In a real production deployment this endpoint would be called
    by other backend services, not by humans; the auth mechanism would be
    replaced with service-to-service auth (API key or mTLS).  This is
    documented in README.md and noted here so it isn't mistaken for an
    oversight.

3.  Always HTTP 200: the endpoint never returns 4xx/5xx.  The `reason` field
    in the response body communicates what actually happened (flag not found,
    evaluation error, etc.).  This is deliberate — a calling service should
    be able to treat every response identically: parse JSON, read `enabled`,
    move on.  A 500 here would be the worst possible failure mode for a
    system whose entire purpose is being called from arbitrary code paths.

4.  Fail-safe: evaluate_flag() catches all exceptions internally and returns
    a safe default.  The endpoint layer adds a second catch as a belt-and-
    suspenders guard, but in practice evaluate_flag() should never raise.
"""

import logging
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Any, Optional

from app.database import get_db
from app.dependencies import CurrentUser, get_current_user
from app.evaluation import REASON_FAIL_SAFE, evaluate_flag
from app.metrics import flag_evaluation_duration_seconds

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evaluate", tags=["evaluate"])


# ---------------------------------------------------------------------------
# Request / response schemas — local to this router, not shared with CRUD
# ---------------------------------------------------------------------------

class EvaluateRequest(BaseModel):
    """
    Request body for POST /evaluate.

    flag_name + user_id + environment uniquely identify what to evaluate.
    attributes is an optional freeform dict carrying caller-supplied user
    properties (e.g. {"plan": "enterprise", "country": "US"}) used to match
    targeting rules.  It is ignored for boolean and percentage flag types.

    user_id is explicitly typed as str, not int — real user IDs are often
    UUIDs or external-system IDs, not sequential integers.
    """
    flag_name: str = Field(..., description="Name of the feature flag to evaluate.")
    user_id: str = Field(..., description="Caller-supplied user identifier (string, not integer).")
    environment: str = Field(
        ...,
        pattern=r"^(dev|staging|prod)$",
        description="Target environment: dev, staging, or prod.",
    )
    attributes: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional freeform user attributes for targeted flag evaluation. "
            "Keys are attribute names (e.g. 'plan', 'country'); values are strings. "
            "Ignored for boolean and percentage flag types."
        ),
    )


class EvaluateResponse(BaseModel):
    """
    Response body for POST /evaluate.

    HTTP status is always 200 — use `reason` to understand what happened.

    reason values:
      boolean_flag              — flag is boolean type; enabled = default_value
      percentage_rollout        — bucket-based rollout; enabled based on hash
      targeting_rule_matched    — a targeting rule fired for this user
      targeting_rule_default    — targeted flag, no rule matched; enabled = default_value
      flag_not_found            — no flag with this name+environment exists; enabled = False
      evaluation_error_fail_safe — an error occurred; enabled = False (safe default)
    """
    flag_name: str
    user_id: str
    environment: str
    enabled: bool
    reason: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=EvaluateResponse,
    summary="Evaluate a feature flag for a user",
    description=(
        "Evaluate a feature flag and return whether it is enabled for the "
        "specified user. Always returns HTTP 200 — check the `reason` field "
        "to understand the evaluation outcome. "
        "This endpoint is designed to be safe to call from any code path: "
        "it will never return a 5xx, even if the database is unreachable."
    ),
)
def post_evaluate(
    body: EvaluateRequest,
    db: Session = Depends(get_db),
    _current_user: CurrentUser = Depends(get_current_user),
) -> EvaluateResponse:
    """
    POST /evaluate

    Evaluates the named feature flag for the given user in the given
    environment.  Returns HTTP 200 in all cases.

    The calling service should:
      1. Parse the JSON response.
      2. Read `enabled` — true means the feature is on for this user.
      3. Optionally inspect `reason` for logging/debugging.
      4. Never branch on HTTP status — it is always 200.
    """
    logger.info(
        "evaluate: flag=%r user=%r environment=%r attributes_keys=%r",
        body.flag_name,
        body.user_id,
        body.environment,
        list(body.attributes.keys()) if body.attributes else [],
    )

    # evaluate_flag() is the fail-safe boundary — it never raises.
    # The outer try/except is a belt-and-suspenders guard for any unexpected
    # import-time or framework-level exception.
    # The histogram wraps the full call so it measures wall-clock time from
    # cache lookup through response, matching the sub-50ms latency requirement.
    t_start = time.perf_counter()
    try:
        result = evaluate_flag(
            db=db,
            flag_name=body.flag_name,
            user_id=body.user_id,
            environment=body.environment,
            attributes=body.attributes,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "evaluate: unexpected exception escaped evaluate_flag() — "
            "returning fail-safe. Error: %s",
            exc,
        )
        result = {
            "flag_name": body.flag_name,
            "user_id": body.user_id,
            "environment": body.environment,
            "enabled": False,
            "reason": REASON_FAIL_SAFE,
        }
    finally:
        # Record histogram observation — wrapped so it can never raise.
        try:
            flag_evaluation_duration_seconds.observe(time.perf_counter() - t_start)
        except Exception:  # noqa: BLE001
            pass

    logger.info(
        "evaluate: result flag=%r user=%r enabled=%s reason=%r",
        result["flag_name"],
        result["user_id"],
        result["enabled"],
        result["reason"],
    )

    return EvaluateResponse(**result)
