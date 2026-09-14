"""
routers/flags.py — Flag CRUD endpoints and audit history.

Endpoints
---------
  POST   /flags               — create a flag (admin only)
  GET    /flags               — list flags with optional filters (any auth'd user)
  GET    /flags/{id}          — single flag (any auth'd user)
  PATCH  /flags/{id}          — partial update (admin only)
  DELETE /flags/{id}          — delete flag (admin only)
  GET    /flags/{id}/history  — audit log for a flag (any auth'd user)

Atomicity
---------
Every mutation (create / update / delete) flushes the flag change AND the
audit_log insert inside a single transaction before calling db.commit().
If either db.add() fails, the except block calls db.rollback() and re-raises
an HTTP 500, ensuring no half-writes are visible to readers.

Error translation
-----------------
  IntegrityError (duplicate name+environment) → HTTP 409 (not a raw 500)
  Missing flag                                → HTTP 404
  Pydantic cross-field failures               → HTTP 422 (FastAPI default)

Known limitation — deleted-flag history
----------------------------------------
audit_log.flag_id is set to NULL by the ON DELETE SET NULL constraint when
a flag is deleted.  This endpoint's GET /flags/{id}/history lookup is by
flag_id, so history for deleted flags is no longer queryable through this
endpoint.  The audit rows themselves are preserved in the DB — they just have
flag_id = NULL.  Querying deleted-flag history would require a separate path
(e.g. lookup by flag name stored redundantly on the audit row), which is out
of scope.  This is documented as a known limitation in README.md, not a bug.
"""

import logging
import types
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.cache import invalidate_cached_flag
from app.database import get_db
from app.dependencies import CurrentUser, get_current_user, require_admin
from app.metrics import flag_mutations_total
from app.models import AuditLog, Flag, TargetingRule
from app.schemas import AuditLogEntry, FlagCreate, FlagResponse, FlagUpdate, TargetingRuleCreate, TargetingRuleResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/flags", tags=["flags"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_flag_or_404(db: Session, flag_id: int) -> Flag:
    """Fetch a flag by PK or raise HTTP 404 with a clear message."""
    flag = db.get(Flag, flag_id)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Flag with id {flag_id} not found.",
        )
    return flag


def _snapshot(flag: Flag) -> types.SimpleNamespace:
    """Return a plain attribute snapshot of a Flag for use as old_state.

    We copy scalar fields into a SimpleNamespace rather than a bare
    Flag.__new__(Flag) because SQLAlchemy ORM instances require _sa_instance_state
    to be initialised (done by __init__) before any descriptor-managed attribute
    can be set.  Bypassing __init__ via __new__ leaves that sentinel missing, and
    the first setattr triggers an AttributeError from SQLAlchemy's attribute layer.

    SimpleNamespace is a plain Python object with no ORM machinery — _serialize_flag
    reads the same attribute names (id, name, …) and works identically.
    """
    snap = types.SimpleNamespace()
    for col in (
        "id", "name", "description", "flag_type", "default_value",
        "rollout_percentage", "environment", "created_at", "updated_at",
    ):
        setattr(snap, col, getattr(flag, col))
    return snap


def _merge_and_validate(existing: Flag, update: FlagUpdate) -> FlagCreate:
    """Merge provided update fields over the existing flag state and validate.

    The cross-field rule (percentage ↔ rollout_percentage) is validated against
    the *merged* result, not just the fields in the request body.  This catches
    e.g. a request that only sets flag_type="percentage" without providing
    rollout_percentage, where the existing rollout_percentage is None — that
    merged state is invalid and must be rejected.

    Returns a FlagCreate so that Pydantic re-runs all validators on the merged
    state, raising ValidationError (→ HTTP 422) if anything is inconsistent.
    """
    provided = update.provided_fields()

    merged = {
        "name":               provided.get("name",               existing.name),
        "description":        provided.get("description",        existing.description),
        "flag_type":          provided.get("flag_type",          existing.flag_type),
        "default_value":      provided.get("default_value",      existing.default_value),
        "rollout_percentage": provided.get("rollout_percentage", existing.rollout_percentage),
        "environment":        provided.get("environment",        existing.environment),
    }

    # FlagCreate.model_validate raises ValidationError on cross-field failures.
    # FastAPI translates unhandled ValidationError → HTTP 422 automatically.
    return FlagCreate.model_validate(merged)


# ---------------------------------------------------------------------------
# POST /flags — create
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=FlagResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new feature flag",
)
def create_flag(
    body: FlagCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
) -> Flag:
    """Create a new feature flag.

    Enforces (name, environment) uniqueness at the application layer (HTTP 409)
    before the DB constraint has a chance to surface an IntegrityError (which
    would be an opaque HTTP 500 without this guard).

    The flag insert and the audit_log insert share a single transaction:
    either both commit or neither does.
    """
    # Check for existing flag with same (name, environment) — return 409
    # before touching the DB with an INSERT that would raise IntegrityError.
    existing = (
        db.query(Flag)
        .filter(Flag.name == body.name, Flag.environment == body.environment)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A flag named '{body.name}' already exists in the "
                f"'{body.environment}' environment."
            ),
        )

    flag = Flag(
        name=body.name,
        description=body.description,
        flag_type=body.flag_type,
        default_value=body.default_value,
        rollout_percentage=body.rollout_percentage,
        environment=body.environment,
    )

    try:
        db.add(flag)
        db.flush()  # Assigns flag.id without committing — needed before audit insert.

        record_audit(
            db,
            flag=flag,
            action="created",
            actor=current_user.username,
            old_state=None,
            new_state=flag,
        )

        db.commit()
        db.refresh(flag)
    except IntegrityError:
        # Race condition: another request committed the same (name, env) between
        # our SELECT check above and our INSERT.  Translate to 409.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A flag named '{body.name}' already exists in the "
                f"'{body.environment}' environment (concurrent write)."
            ),
        )
    except Exception:
        db.rollback()
        logger.exception("Unexpected error creating flag name=%s env=%s", body.name, body.environment)
        raise

    logger.info(
        "Flag created: id=%s name=%s env=%s actor=%s",
        flag.id, flag.name, flag.environment, current_user.username,
    )

    # Invalidate any stale cache entry for this (name, environment) pair.
    # In practice no cache key exists yet for a newly created flag, but
    # invalidating unconditionally keeps the write path consistent and
    # guards against a theoretical race where a concurrent /evaluate call
    # happened to cache a "flag_not_found" state (which we don't cache, but
    # this is belt-and-suspenders for correctness).
    invalidate_cached_flag(flag.name, flag.environment)

    # Increment mutation counter alongside cache invalidation — wrapped so
    # that a metrics failure can never propagate into the write path.
    try:
        flag_mutations_total.labels(action="created").inc()
    except Exception:  # noqa: BLE001
        pass

    return flag


# ---------------------------------------------------------------------------
# GET /flags — list (with optional filters)
# ---------------------------------------------------------------------------

@router.get(
    "",
    response_model=List[FlagResponse],
    summary="List all feature flags",
)
def list_flags(
    environment: Optional[str] = Query(
        default=None,
        pattern=r"^(dev|staging|prod)$",
        description="Filter by environment.",
    ),
    flag_type: Optional[str] = Query(
        default=None,
        pattern=r"^(boolean|percentage|targeted)$",
        description="Filter by flag type.",
    ),
    db: Session = Depends(get_db),
    _current_user: CurrentUser = Depends(get_current_user),
) -> List[Flag]:
    """Return all feature flags, optionally filtered by environment and/or flag_type.

    Accessible to any authenticated user (viewer or admin).
    Filters are applied at the DB level, not in Python, to avoid loading the
    full table into memory when only a subset is needed.
    """
    query = db.query(Flag)
    if environment is not None:
        query = query.filter(Flag.environment == environment)
    if flag_type is not None:
        query = query.filter(Flag.flag_type == flag_type)
    return query.all()


# ---------------------------------------------------------------------------
# GET /flags/{id} — single flag
# ---------------------------------------------------------------------------

@router.get(
    "/{flag_id}",
    response_model=FlagResponse,
    summary="Retrieve a single feature flag",
)
def get_flag(
    flag_id: int,
    db: Session = Depends(get_db),
    _current_user: CurrentUser = Depends(get_current_user),
) -> Flag:
    """Return a single flag by id, or HTTP 404 if it doesn't exist."""
    return _get_flag_or_404(db, flag_id)


# ---------------------------------------------------------------------------
# PATCH /flags/{id} — partial update
# ---------------------------------------------------------------------------

@router.patch(
    "/{flag_id}",
    response_model=FlagResponse,
    summary="Partially update a feature flag",
)
def update_flag(
    flag_id: int,
    body: FlagUpdate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
) -> Flag:
    """Partially update a feature flag.

    Only fields explicitly included in the request body are changed; omitted
    fields retain their current value.  The cross-field rule
    (percentage ↔ rollout_percentage) is re-evaluated against the *merged*
    state (existing + provided) so a partial request can't produce an
    inconsistent flag.

    The update and audit insert share a single transaction.
    """
    flag = _get_flag_or_404(db, flag_id)

    # Capture the current state BEFORE any changes for the audit old_value.
    old_snap = _snapshot(flag)

    # Merge + validate; raises 422 via FastAPI if the merged state is invalid.
    validated = _merge_and_validate(flag, body)

    # Apply only the provided fields, not the full merged set — we only want
    # to touch columns the caller explicitly supplied so that DB defaults
    # (e.g. updated_at triggers) and other columns are untouched.
    provided = body.provided_fields()
    for field, value in provided.items():
        setattr(flag, field, value)

    # Explicitly set updated_at to current UTC so it's reliably updated even
    # if the ORM onupdate hook doesn't fire before flush.
    flag.updated_at = datetime.now(timezone.utc)

    try:
        db.flush()  # Write the update; keep the transaction open.

        record_audit(
            db,
            flag=flag,
            action="updated",
            actor=current_user.username,
            old_state=old_snap,
            new_state=flag,
        )

        db.commit()
        db.refresh(flag)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A flag named '{flag.name}' already exists in the "
                f"'{flag.environment}' environment."
            ),
        )
    except Exception:
        db.rollback()
        logger.exception("Unexpected error updating flag id=%s", flag_id)
        raise

    logger.info(
        "Flag updated: id=%s actor=%s fields=%s",
        flag.id, current_user.username, list(provided.keys()),
    )

    # Suppress the unused variable warning — validated is used for its
    # side-effect (raising ValidationError on cross-field failures).
    _ = validated

    # Cache invalidation — must happen after a successful commit.
    # If name or environment changed, both the old key and the new key must
    # be invalidated:
    #   - Old key: stale entry under the previous (name, environment) would
    #     linger until TTL expiry and serve wrong data for any evaluate call
    #     using the old name/env.
    #   - New key: forces the next /evaluate call to re-fetch fresh data from
    #     Postgres rather than picking up any pre-existing entry under the
    #     new key (unlikely but possible if a flag was previously deleted and
    #     recreated with the same name).
    old_name = old_snap.name
    old_env  = old_snap.environment
    new_name = flag.name
    new_env  = flag.environment

    invalidate_cached_flag(old_name, old_env)  # always invalidate old key
    if (new_name, new_env) != (old_name, old_env):
        # Name or environment changed — also invalidate the new key.
        invalidate_cached_flag(new_name, new_env)

    # Increment mutation counter — wrapped so it can never propagate.
    try:
        flag_mutations_total.labels(action="updated").inc()
    except Exception:  # noqa: BLE001
        pass

    return flag


# ---------------------------------------------------------------------------
# DELETE /flags/{id}
# ---------------------------------------------------------------------------

@router.delete(
    "/{flag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a feature flag",
)
def delete_flag(
    flag_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
) -> None:
    """Delete a feature flag.

    Cascade behaviour (both enforced by the DB schema from Step 1):
      - targeting_rules rows with this flag_id are hard-deleted (ON DELETE CASCADE).
      - audit_log rows (including the one inserted here) have their flag_id set
        to NULL (ON DELETE SET NULL) so the history is preserved post-deletion.

    The audit insert and the flag deletion share one transaction.  The audit row
    is added (with flag_id still valid) before the DELETE is flushed so that the
    FK value is recorded.  After commit, the DB's SET NULL trigger fires and
    nullifies flag_id on all audit rows for this flag — including the "deleted"
    entry — which is correct and expected behavior.
    """
    flag = _get_flag_or_404(db, flag_id)

    # Snapshot before deletion — this is the old_value for the audit entry.
    old_snap = _snapshot(flag)

    try:
        # Insert audit row BEFORE deleting the flag row.  At this point flag.id
        # is still valid, so the FK can be written.  After commit the ON DELETE
        # SET NULL will null it — that's fine and expected.
        record_audit(
            db,
            flag=flag,
            action="deleted",
            actor=current_user.username,
            old_state=old_snap,
            new_state=None,
        )

        db.delete(flag)   # Cascades to targeting_rules; SET NULL on audit_log.
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Unexpected error deleting flag id=%s", flag_id)
        raise

    logger.info(
        "Flag deleted: id=%s name=%s env=%s actor=%s",
        flag_id, old_snap.name, old_snap.environment, current_user.username,
    )

    # Invalidate the cache entry using the pre-deletion name+environment
    # snapshot.  The flag row is gone from Postgres at this point, so the
    # next /evaluate call would get flag_not_found — but without invalidation
    # it would incorrectly serve the cached value until TTL expires.
    invalidate_cached_flag(old_snap.name, old_snap.environment)

    # Increment mutation counter — wrapped so it can never propagate.
    try:
        flag_mutations_total.labels(action="deleted").inc()
    except Exception:  # noqa: BLE001
        pass

    # HTTP 204 — no response body.
    return None


# ---------------------------------------------------------------------------
# GET /flags/{id}/history — audit log
# ---------------------------------------------------------------------------

@router.get(
    "/{flag_id}/history",
    response_model=List[AuditLogEntry],
    summary="Retrieve audit history for a feature flag",
)
def get_flag_history(
    flag_id: int,
    db: Session = Depends(get_db),
    _current_user: CurrentUser = Depends(get_current_user),
) -> List[AuditLog]:
    """Return audit log entries for a flag, newest first.

    KNOWN LIMITATION: after a flag is deleted, audit_log.flag_id is set to
    NULL by the ON DELETE SET NULL constraint.  This endpoint queries by
    flag_id, so it can only return history for flags that still exist.  If the
    flag has been deleted, the endpoint returns HTTP 404 (the flag is gone)
    rather than silently returning an empty list.  Historical audit rows are
    preserved in the DB with flag_id=NULL but are not queryable through this
    path.  A future extension could add a separate query path (e.g. by flag
    name stored redundantly on the audit row) to support deleted-flag history.
    """
    # Verify the flag exists — 404 if not.  This is intentional: we don't
    # silently return [] for a deleted flag; we signal clearly that the flag
    # is gone.  See the docstring for the known-limitation note.
    _get_flag_or_404(db, flag_id)

    entries = (
        db.query(AuditLog)
        .filter(AuditLog.flag_id == flag_id)
        .order_by(AuditLog.timestamp.desc())
        .all()
    )
    return entries


# ---------------------------------------------------------------------------
# POST /flags/{id}/rules — add targeting rule
# ---------------------------------------------------------------------------

@router.post(
    "/{flag_id}/rules",
    response_model=TargetingRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a targeting rule to a feature flag",
)
def create_targeting_rule(
    flag_id: int,
    body: TargetingRuleCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
) -> TargetingRule:
    flag = _get_flag_or_404(db, flag_id)
    if flag.flag_type != "targeted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Targeting rules can only be added to flags of type 'targeted'.",
        )

    rule = TargetingRule(
        flag_id=flag.id,
        attribute=body.attribute,
        operator=body.operator,
        value=body.value,
    )
    
    old_snap = _snapshot(flag)

    try:
        db.add(rule)
        db.flush()

        record_audit(
            db,
            flag=flag,
            action="updated",
            actor=current_user.username,
            old_state=old_snap,
            new_state=flag,
        )

        db.commit()
        db.refresh(rule)
    except Exception:
        db.rollback()
        logger.exception("Unexpected error creating targeting rule for flag id=%s", flag_id)
        raise

    logger.info("Targeting rule created: id=%s flag_id=%s actor=%s", rule.id, flag.id, current_user.username)
    invalidate_cached_flag(flag.name, flag.environment)

    try:
        flag_mutations_total.labels(action="updated").inc()
    except Exception:  # noqa: BLE001
        pass

    return rule


# ---------------------------------------------------------------------------
# DELETE /flags/{id}/rules/{rule_id} — delete targeting rule
# ---------------------------------------------------------------------------

@router.delete(
    "/{flag_id}/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a targeting rule from a feature flag",
)
def delete_targeting_rule(
    flag_id: int,
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
) -> None:
    flag = _get_flag_or_404(db, flag_id)
    
    rule = db.query(TargetingRule).filter(TargetingRule.id == rule_id, TargetingRule.flag_id == flag_id).first()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Targeting rule {rule_id} not found for flag {flag_id}.",
        )

    old_snap = _snapshot(flag)

    try:
        db.delete(rule)
        db.flush()

        record_audit(
            db,
            flag=flag,
            action="updated",
            actor=current_user.username,
            old_state=old_snap,
            new_state=flag,
        )

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Unexpected error deleting targeting rule id=%s", rule_id)
        raise

    logger.info("Targeting rule deleted: id=%s flag_id=%s actor=%s", rule_id, flag.id, current_user.username)
    invalidate_cached_flag(flag.name, flag.environment)

    try:
        flag_mutations_total.labels(action="updated").inc()
    except Exception:  # noqa: BLE001
        pass

    return None

