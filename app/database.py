"""
database.py — SQLAlchemy engine, session factory, declarative Base, and the
FastAPI dependency that yields a scoped DB session per request.

Design decisions:
  - DATABASE_URL is sourced exclusively from app.config.settings, which is the
    single place that calls load_dotenv() and validates required env vars.  Do
    not add a second load_dotenv() call here — that would make env-loading order
    non-deterministic and harder to reason about.
  - The engine is a module-level singleton.  Never create a new engine per
    request — connection-pool overhead makes that a latency killer.
  - pool_pre_ping=True silently replaces stale connections (e.g. after a Neon
    cold-start) before handing them to callers.
"""

from urllib.parse import unquote, urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# config.py is the single place that calls load_dotenv() and validates env vars.
# Importing settings here guarantees .env is loaded before the URL is read.
from app.config import settings

_raw_url: str = settings.database_url

def _normalize_db_url(url: str) -> str:
    """
    Decode percent-encoding in the database name portion of the URL.
    psycopg2 does not URL-decode the dbname component, so a name like
    'feature%20flag%20service' would be passed verbatim to Postgres and
    fail.  Only the path is decoded to avoid mangling encoded chars in
    passwords or other components.
    """
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=unquote(parsed.path)))

DATABASE_URL: str = _normalize_db_url(_raw_url)

# Single engine instance reused across the entire application lifetime.
engine = create_engine(
    DATABASE_URL,
    # Echo SQL to stdout only when explicitly requested — keep it off by default
    # so secrets in query params don't leak into logs.
    echo=False,
    # pool_pre_ping sends a lightweight "SELECT 1" before handing a connection
    # out of the pool, ensuring stale connections (e.g. after a Neon cold-start)
    # are silently replaced rather than surfaced as errors to callers.
    pool_pre_ping=True,
)

# Session factory — autocommit=False so every request runs inside a transaction
# that must be explicitly committed or rolled back.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Single shared Base that every model class inherits from.  Alembic's env.py
# imports this to drive autogenerate.
Base = declarative_base()


def get_db():
    """
    FastAPI dependency: yields a database session and guarantees it is closed
    after the request finishes, even if an exception is raised.

    Usage:
        @router.get("/example")
        def example(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
