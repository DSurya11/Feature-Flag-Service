"""
models.py — SQLAlchemy ORM models for all four tables.

All enums are backed by Postgres native enum types (not plain VARCHAR) so the
database itself enforces valid values regardless of what the application layer
does.  All timestamps are timezone-aware (stored as TIMESTAMPTZ in Postgres)
to avoid ambiguity across environments and timezones.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base

# ---------------------------------------------------------------------------
# Enum types — Postgres native enums enforce values at the DB level.
# ---------------------------------------------------------------------------

UserRoleEnum = Enum("admin", "viewer", name="userrole")
FlagTypeEnum = Enum("boolean", "percentage", "targeted", name="flagtype")
EnvironmentEnum = Enum("dev", "staging", "prod", name="environment")
OperatorEnum = Enum("equals", "in", "not_equals", name="operator")
AuditActionEnum = Enum("created", "updated", "deleted", name="auditaction")


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False, index=True)
    # Column is intentionally named hashed_password — never plaintext.
    # Hashing (bcrypt via passlib) is handled in the auth layer (Step 3).
    hashed_password = Column(String, nullable=False)
    role = Column(UserRoleEnum, nullable=False, server_default="viewer")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------

class Flag(Base):
    __tablename__ = "flags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # name is NOT unique alone — the composite (name, environment) is unique
    # (same flag name can exist independently across dev / staging / prod).
    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)
    flag_type = Column(FlagTypeEnum, nullable=False)
    # default_value is the fail-safe returned when evaluation cannot reach
    # cache or DB — must always have a value, defaults to False (off).
    default_value = Column(Boolean, nullable=False, server_default="false")
    rollout_percentage = Column(Integer, nullable=True)
    environment = Column(EnvironmentEnum, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    targeting_rules = relationship(
        "TargetingRule",
        back_populates="flag",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    audit_logs = relationship(
        "AuditLog",
        back_populates="flag",
        passive_deletes=True,
    )

    __table_args__ = (
        # Composite unique: same name can exist in different environments.
        UniqueConstraint("name", "environment", name="uq_flags_name_environment"),
        # DB-level guard: rollout_percentage must be 0–100 when set.
        CheckConstraint(
            "rollout_percentage IS NULL OR "
            "(rollout_percentage >= 0 AND rollout_percentage <= 100)",
            name="ck_flags_rollout_percentage_range",
        ),
    )


# ---------------------------------------------------------------------------
# targeting_rules
# ---------------------------------------------------------------------------

class TargetingRule(Base):
    __tablename__ = "targeting_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # CASCADE: deleting a flag removes all its targeting rules.
    flag_id = Column(
        Integer,
        ForeignKey("flags.id", ondelete="CASCADE"),
        nullable=False,
    )
    # e.g. "plan", "user_id", "country"
    attribute = Column(String, nullable=False)
    operator = Column(OperatorEnum, nullable=False)
    # Stored as string; application layer parses as needed.
    # For "in" operator, value is a comma-separated list, e.g. "enterprise,pro"
    value = Column(String, nullable=False)

    flag = relationship("Flag", back_populates="targeting_rules")


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # NULLABLE + SET NULL: audit history must survive flag deletion.
    # When a flag is deleted, flag_id becomes NULL so the row is preserved.
    flag_id = Column(
        Integer,
        ForeignKey("flags.id", ondelete="SET NULL"),
        nullable=True,  # Required by SET NULL — column must allow null.
    )
    action = Column(AuditActionEnum, nullable=False)
    # Store username string, not user FK — log must remain readable even if
    # the user account is later deleted.
    actor = Column(String, nullable=False)
    # Serialized (JSON string) snapshot of flag state before/after the change.
    old_value = Column(Text, nullable=True)  # NULL for "created" actions
    new_value = Column(Text, nullable=True)  # NULL for "deleted" actions
    timestamp = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    flag = relationship("Flag", back_populates="audit_logs")
