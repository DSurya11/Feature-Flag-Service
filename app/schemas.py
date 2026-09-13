"""
schemas.py — Pydantic request/response models for the Flag CRUD API.

Design rationale
----------------
These are deliberately kept separate from app/models.py (SQLAlchemy ORM models):

  1. Decoupling: the DB schema and the API contract can evolve independently.
     Adding an internal column doesn't leak into API responses; changing a
     response field doesn't require a migration.

  2. Validation at the boundary: Pydantic enforces constraints (enum values,
     range limits, cross-field rules) before a query ever hits the database,
     giving callers a clean 422 with a descriptive error rather than an opaque
     DB exception.

  3. No accidental field leakage: explicit field declarations make it impossible
     to accidentally expose an internal column that was added to the ORM model.

Cross-field validation (flag_type / rollout_percentage)
-------------------------------------------------------
  - flag_type == "percentage"  → rollout_percentage MUST be present (0–100)
  - flag_type != "percentage"  → rollout_percentage MUST be absent / null

Enforced in:
  - FlagCreate  — on the inbound request body
  - FlagUpdate  — by the endpoint after merging provided fields with the
                  existing DB state (the merged dict is re-validated by
                  constructing a FlagCreate from it)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# FlagCreate — POST /flags request body
# ---------------------------------------------------------------------------

class FlagCreate(BaseModel):
    name: str
    description: Optional[str] = None
    flag_type: str = Field(..., pattern=r"^(boolean|percentage|targeted)$")
    default_value: bool
    rollout_percentage: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Required when flag_type='percentage'; must be absent otherwise.",
    )
    environment: str = Field(..., pattern=r"^(dev|staging|prod)$")

    @model_validator(mode="after")
    def validate_rollout_percentage_consistency(self) -> "FlagCreate":
        """Cross-field guard: percentage flags need rollout_percentage; others must not."""
        if self.flag_type == "percentage":
            if self.rollout_percentage is None:
                raise ValueError(
                    "rollout_percentage is required when flag_type is 'percentage'."
                )
        else:
            if self.rollout_percentage is not None:
                raise ValueError(
                    f"rollout_percentage must be null/absent when flag_type is "
                    f"'{self.flag_type}' (only valid for 'percentage' flags)."
                )
        return self


# ---------------------------------------------------------------------------
# FlagUpdate — PATCH /flags/{id} request body
# ---------------------------------------------------------------------------

class FlagUpdate(BaseModel):
    """All fields are optional — only supplied fields are applied.

    Cross-field validation (rollout_percentage / flag_type) is NOT evaluated
    here in isolation because the request may supply only one of the two fields.
    The endpoint merges the provided fields with the existing DB state and then
    re-validates the merged result using FlagCreate, which enforces the rule on
    the full combined picture.
    """

    name: Optional[str] = None
    description: Optional[str] = None
    flag_type: Optional[str] = Field(
        default=None, pattern=r"^(boolean|percentage|targeted)$"
    )
    default_value: Optional[bool] = None
    rollout_percentage: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
    )
    environment: Optional[str] = Field(
        default=None, pattern=r"^(dev|staging|prod)$"
    )

    def provided_fields(self) -> dict[str, Any]:
        """Return only the fields that were explicitly set in the request body.

        Uses model_fields_set (populated by Pydantic from the parsed JSON)
        so that None supplied explicitly is distinguished from 'field absent'.
        """
        return {
            field: getattr(self, field)
            for field in self.model_fields_set
        }


# ---------------------------------------------------------------------------
# FlagResponse — used as the response body for all flag read/write endpoints
# ---------------------------------------------------------------------------

class FlagResponse(BaseModel):
    """Mirror of the flags DB row — all columns, nothing hidden.

    Flags are not secret data; there is no sensitive information to redact.
    The model is kept explicit (no __all__ shortcut) so that adding a DB
    column doesn't silently change the API surface.
    """

    id: int
    name: str
    description: Optional[str]
    flag_type: str
    default_value: bool
    rollout_percentage: Optional[int]
    environment: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# AuditLogEntry — response body for GET /flags/{id}/history
# ---------------------------------------------------------------------------

class AuditLogEntry(BaseModel):
    """Represents a single row from the audit_log table.

    old_value / new_value are JSON strings stored in the DB (Text columns).
    They are surfaced as raw strings here so callers can parse them as JSON if
    needed; converting them to dict would require an additional parsing step
    that may fail for historical rows, and the contract is clearer as strings.
    """

    id: int
    action: str
    actor: str
    old_value: Optional[str]
    new_value: Optional[str]
    timestamp: datetime

    model_config = {"from_attributes": True}
