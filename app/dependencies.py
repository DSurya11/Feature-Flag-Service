"""
dependencies.py — Reusable FastAPI dependencies for authentication and RBAC.

Two-layer dependency design
---------------------------
  get_current_user  — any authenticated user (valid JWT required)
  require_admin     — admin role only (layered on top of get_current_user)

The two-layer approach (rather than a single dependency with an allowed_roles
parameter) keeps route signatures self-documenting: you can tell a route's
access level from its Depends() without reading its body.

HTTP status codes used here are intentional:
  401 Unauthorized — "we don't know who you are" (missing / invalid token)
  403 Forbidden    — "we know who you are, and you're not allowed" (wrong role)

The WWW-Authenticate: Bearer header on 401 responses is required by RFC 6750
and tells HTTP clients / OpenAPI tooling that Bearer token auth is expected.
"""

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.auth import decode_access_token

logger = logging.getLogger(__name__)

# OAuth2PasswordBearer extracts the token from the Authorization: Bearer header.
# tokenUrl points to the login endpoint so FastAPI's Swagger UI "Authorize"
# button knows where to POST credentials.
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


@dataclass(frozen=True)
class CurrentUser:
    """Lightweight identity object returned by auth dependencies."""
    username: str
    role: str


def get_current_user(token: str = Depends(_oauth2_scheme)) -> CurrentUser:
    """Dependency: resolve a valid JWT to the caller's identity.

    Raises HTTP 401 (with WWW-Authenticate: Bearer) on any auth failure:
      - Missing Authorization header (OAuth2PasswordBearer raises this itself)
      - Invalid token signature
      - Expired token
      - Malformed payload (missing sub / role claims)
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = decode_access_token(token)
    except ValueError:
        raise credentials_exception

    username: str = claims.get("sub", "")
    role: str = claims.get("role", "")
    if not username or not role:
        raise credentials_exception

    return CurrentUser(username=username, role=role)


def require_admin(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Dependency: allow only admin-role users through.

    Layered on top of get_current_user — a valid JWT is still required first.
    Raises HTTP 403 (not 401) because the caller is authenticated but not
    authorised.  Returns the same CurrentUser so route handlers can inspect
    the identity if needed.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return current_user
