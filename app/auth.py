"""
auth.py — Password hashing and JWT create/verify.

Deliberately kept in one module so the entire authentication mechanism is
readable in one place.  Route handlers import from here; they never call
jose or passlib directly.

Design notes
------------
* Bcrypt is the sole hashing scheme.  passlib's CryptContext handles
  algorithm-agility transparently if we ever need to migrate, but for now
  bcrypt is the right default (adaptive cost, widely audited).
* Role is embedded in the JWT at login time and NOT re-fetched from the DB
  on every subsequent request.  This is a deliberate trade-off: if a user's
  role changes, the new role takes effect only after their current token
  expires.  With a 30-minute default lifetime this is an acceptable delay for
  an internal admin tool, and it avoids a DB hit on every authenticated route.
* JWT_SECRET_KEY is required (no default) — the application refuses to start
  without it, matching the fail-fast pattern used for DATABASE_URL.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing — bcrypt (direct, no passlib wrapper)
#
# passlib 1.7.4 is incompatible with bcrypt >=4 (it probes __about__ which
# no longer exists).  Using bcrypt directly avoids that stale dependency while
# keeping the same bcrypt algorithm and the same public API surface.
# ---------------------------------------------------------------------------


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash of *plaintext*.

    Only the returned hash must ever be stored.  The caller must not log,
    return, or persist the plaintext value.
    """
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True iff *plaintext* matches the stored *hashed* value."""
    return bcrypt.checkpw(plaintext.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# JWT — create and verify
# ---------------------------------------------------------------------------

def create_access_token(username: str, role: str) -> str:
    """Build, sign, and return a JWT for the given identity.

    Claims:
      sub  — username (subject)
      role — "admin" | "viewer", copied from DB at login time
      exp  — now + JWT_EXPIRE_MINUTES
    """
    expire = datetime.now(tz=timezone.utc) + timedelta(
        minutes=settings.jwt_expire_minutes
    )
    payload: dict[str, Any] = {
        "sub": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify *token* signature and expiration; return its claims.

    Raises
    ------
    ValueError
        If the token is missing required claims, expired, or has an invalid
        signature — a single, catchable application exception, not a raw
        library exception.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        # Wrap the library exception so callers don't depend on jose's API.
        raise ValueError(f"Invalid or expired token: {exc}") from exc

    # Validate required claims are present in the payload.
    if "sub" not in claims or "role" not in claims:
        raise ValueError("Token is missing required claims (sub, role).")

    return claims
