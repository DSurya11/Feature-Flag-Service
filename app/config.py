"""
config.py — single source of truth for all environment-driven settings.

Every new setting belongs here. Route handlers, middleware, and other modules
import `settings` from this module rather than calling os.environ.get() ad hoc.

load_dotenv() is called exactly once here; any module that imports `settings`
automatically ensures .env is loaded before the first os.environ read, with no
risk of loading it multiple times or forgetting it in a new file.

Growth path: new settings (Redis, metrics, etc.) each become a single new
    field here — no new load_dotenv() calls, no scattered os.environ.get() calls
    in unrelated modules.
"""

import os

from dotenv import load_dotenv

# Load .env for local dev; a harmless no-op when real env vars already exist
# (Docker / K8s / CI sets them directly). Must happen before any os.environ read.
load_dotenv()


class Settings:
    """
    All environment-driven configuration in one place.

    Required settings raise RuntimeError at import time if missing — the
    application intentionally fails loudly rather than starting in a broken
    state with a wrong or missing value.
    """

    def __init__(self) -> None:
        # ------------------------------------------------------------------ #
        # Required settings — missing values are fatal at startup.            #
        # ------------------------------------------------------------------ #

        # Full PostgreSQL connection string, e.g.:
        #   postgresql://user:pass@host/dbname?sslmode=require
        self.database_url: str = self._require("DATABASE_URL")

        # Redis connection string, e.g.:
        #   redis://localhost:6379        (local / Docker)
        #   rediss://:<token>@host:6380  (Upstash TLS)
        # Required for the same reason as DATABASE_URL: the app must not
        # start silently without its cache layer (see main.py lifespan).
        self.redis_url: str = self._require("REDIS_URL")

        # Secret used to sign JWTs.  Must be a long, random string — generate
        # with: python -c "import secrets; print(secrets.token_hex(32))"
        # Never commit the real value; store it in .env locally and in your
        # deployment secret manager in production.
        self.jwt_secret_key: str = self._require("JWT_SECRET_KEY")

        # ------------------------------------------------------------------ #
        # Optional settings — safe defaults are provided.                     #
        # ------------------------------------------------------------------ #

        # Logging verbosity: DEBUG | INFO | WARNING | ERROR | CRITICAL
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()

        # JWT signing algorithm.  HS256 (symmetric) is sufficient for a
        # single-service deployment where we are not distributing a public
        # key to external services.  Switch to RS256 if/when tokens must be
        # verified by a separate service without sharing the secret.
        self.jwt_algorithm: str = os.environ.get("JWT_ALGORITHM", "HS256")

        # Token lifetime in minutes.  30 minutes is a reasonable default for
        # an internal admin tool: short enough to limit the blast radius of a
        # leaked token, long enough not to disrupt normal workflows.
        # Role changes take effect after the current token expires — this is
        # the deliberate trade-off for avoiding a DB hit on every request.
        self.jwt_expire_minutes: int = int(
            os.environ.get("JWT_EXPIRE_MINUTES", "30")
        )

        # How long (in seconds) a cached flag is considered fresh before the
        # next /evaluate call re-fetches from Postgres on a cache miss.
        # 10 seconds matches the original spec's "refresh every ~10s" target.
        self.cache_ttl_seconds: int = int(
            os.environ.get("CACHE_TTL_SECONDS", "10")
        )

    # ---------------------------------------------------------------------- #
    # Helpers                                                                  #
    # ---------------------------------------------------------------------- #

    @staticmethod
    def _require(key: str) -> str:
        """Return the env-var value or raise with a clear diagnostic message."""
        value = os.environ.get(key)
        if not value:
            raise RuntimeError(
                f"Required environment variable '{key}' is not set. "
                "Ensure it is present in your .env file (local dev) or "
                "injected by your deployment environment (Docker / K8s / CI)."
            )
        return value


# ---------------------------------------------------------------------------
# Module-level singleton — import as:  from app.config import settings
# ---------------------------------------------------------------------------
settings = Settings()
