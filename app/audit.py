"""
audit.py — Shared helper for writing audit_log rows.

Design decisions
----------------
Single-responsibility: this module owns exactly one thing — serializing a flag
state snapshot to JSON and inserting it into audit_log.  The three mutation
endpoints (create, update, delete) all call this helper so the serialization
format is identical across all audit entries; old_value and new_value are
always produced by the same code path and are always directly comparable.

Transaction ownership stays with the caller.
The helper calls db.add() but never db.commit().  This is the critical design
choice that satisfies the atomicity requirement: the flag mutation and the
audit insert share the same transaction.  If either fails, the session's
exception handling in the route handler rolls both back together.  A helper
that committed independently would break that guarantee and could leave a flag
created without an audit trail (or an audit trail for a write that rolled back).

Serialization format
--------------------
Flag state is serialized as a JSON string (stored in the Text column).  The
fields serialized are exactly those exposed by FlagResponse so that audit
entries are self-contained and readable without joining back to the flags table.
datetime objects are converted to ISO 8601 strings (UTC) for portability.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import AuditLog, Flag

logger = logging.getLogger(__name__)


def _serialize_flag(flag: Flag) -> str:
    """Convert a Flag ORM object to a canonical JSON string.

    The field set matches FlagResponse so that old_value/new_value in the
    audit log are directly comparable to what the API returns to callers.
    Using a fixed field list (rather than flag.__dict__) avoids including
    SQLAlchemy internals like _sa_instance_state.
    """

    def _to_serializable(value: Any) -> Any:
        """Convert types that json.dumps can't handle natively."""
        if isinstance(value, datetime):
            # Always emit UTC ISO 8601 with timezone marker — unambiguous.
            return value.astimezone(timezone.utc).isoformat()
        return value

    data = {
        "id": flag.id,
        "name": flag.name,
        "description": flag.description,
        "flag_type": flag.flag_type,
        "default_value": flag.default_value,
        "rollout_percentage": flag.rollout_percentage,
        "environment": flag.environment,
        "created_at": _to_serializable(flag.created_at),
        "updated_at": _to_serializable(flag.updated_at),
    }
    return json.dumps(data)


def record_audit(
    db: Session,
    *,
    flag: Optional[Flag],
    action: str,
    actor: str,
    old_state: Any,
    new_state: Any,
) -> None:
    """Insert an audit_log row within the caller's existing transaction.

    Parameters
    ----------
    db:
        The active session.  This function MUST NOT call db.commit() — the
        caller controls the transaction boundary.
    flag:
        The Flag object whose id should be stored on the audit row.  Pass
        the flag even for deletes (before the delete is flushed) so that the
        FK is recorded; ON DELETE SET NULL will null it out automatically
        once the delete is committed.  Pass None only if flag_id being null
        is explicitly acceptable (shouldn't happen in practice with current
        endpoints).
    action:
        One of "created", "updated", "deleted" — matches the AuditActionEnum.
    actor:
        The username of the authenticated user who triggered the change.
    old_state:
        Flag ORM object representing state *before* the change.  None for
        "created" actions (there was no previous state).
    new_state:
        Flag ORM object representing state *after* the change.  None for
        "deleted" actions.
    """
    old_value = _serialize_flag(old_state) if old_state is not None else None
    new_value = _serialize_flag(new_state) if new_state is not None else None

    entry = AuditLog(
        flag_id=flag.id if flag is not None else None,
        action=action,
        actor=actor,
        old_value=old_value,
        new_value=new_value,
    )
    db.add(entry)

    logger.debug(
        "Audit entry queued: action=%s actor=%s flag_id=%s",
        action,
        actor,
        flag.id if flag else None,
    )
