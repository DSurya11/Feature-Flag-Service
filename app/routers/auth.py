"""
routers/auth.py — Authentication endpoints.

POST /auth/login
  Accepts an OAuth2 password-flow form body (username + password).
  Using FastAPI's OAuth2PasswordRequestForm makes this endpoint compatible
  with Swagger UI's built-in "Authorize" button — which matters for
  interview-facing API docs.

Security note: login failures always return the same generic message
("Incorrect username or password") regardless of whether the username
was not found or the password was wrong.  Distinguishing the two would
allow an attacker to enumerate valid usernames via timing or message
differences.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import create_access_token, verify_password
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenResponse(BaseModel):
    """Standard OAuth2 bearer token response shape."""

    access_token: str
    token_type: str = "bearer"


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Obtain a bearer token",
    description=(
        "Exchange valid credentials for a signed JWT. "
        "Use the returned token in the `Authorization: Bearer <token>` header "
        "on subsequent requests, or click **Authorize** above to set it globally "
        "in Swagger UI."
    ),
)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Validate credentials and issue a JWT on success."""
    # Generic error used for both "user not found" and "wrong password" — never
    # reveal which case applies, as that would allow username enumeration.
    _auth_failure = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # 1. Look up user by username.
    user: User | None = (
        db.query(User).filter(User.username == form_data.username).first()
    )
    if user is None:
        logger.warning("Login attempt for unknown username.")
        raise _auth_failure

    # 2. Verify the submitted password against the stored bcrypt hash.
    if not verify_password(form_data.password, user.hashed_password):
        logger.warning("Login attempt with wrong password for user '%s'.", user.username)
        raise _auth_failure

    # 3. Issue a JWT with sub, role, and exp embedded.
    token = create_access_token(username=user.username, role=user.role)
    logger.info("User '%s' authenticated successfully.", user.username)

    return TokenResponse(access_token=token)
