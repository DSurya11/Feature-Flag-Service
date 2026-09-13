"""
alembic/env.py — Alembic migration environment.

Key design decisions:
  - DATABASE_URL is read exclusively from the environment (via .env in local
    dev, real env vars elsewhere).  It is NEVER read from alembic.ini so that
    credentials cannot accidentally be committed.
  - Both app.database.Base and app.models are imported so that Alembic's
    autogenerate can discover all table metadata automatically.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Make sure the project root is on sys.path so "app.*" imports resolve when
# Alembic is invoked from the project root (e.g. `alembic upgrade head`).
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Load .env — harmless no-op when real env vars already exist.
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Inject DATABASE_URL into Alembic config at runtime.
# Raises KeyError immediately if the variable is not set — same fail-fast
# behaviour as the application itself.
# ---------------------------------------------------------------------------
_raw_url: str = os.environ["DATABASE_URL"]
# Decode percent-encoding in the database name (psycopg2 doesn't URL-decode dbname).
_parsed = urlparse(_raw_url)
database_url: str = urlunparse(_parsed._replace(path=unquote(_parsed.path)))
config = context.config
# configparser uses % for interpolation; escape any remaining literal % chars
# (e.g. encoded special chars in passwords) by doubling them.
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

# ---------------------------------------------------------------------------
# Set up Python logging from alembic.ini.
# ---------------------------------------------------------------------------
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Import Base and all models so autogenerate picks up every table.
# The models import registers them on Base.metadata via the ORM machinery.
# ---------------------------------------------------------------------------
from app.database import Base  # noqa: E402
import app.models  # noqa: E402, F401 — side-effect import registers all models

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Migration runners
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without a live connection.
    Useful for reviewing what will be applied before touching the database.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Include schemas so Postgres enum types are handled correctly.
        include_schemas=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode — connects to the live database.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # NullPool: don't hold connections between migrations
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            # Compare server defaults so autogenerate catches func.now() changes.
            compare_server_defaults=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
