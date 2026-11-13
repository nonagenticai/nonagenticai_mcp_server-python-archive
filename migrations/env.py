"""Explicit Alembic ownership; no application imports or automatic DDL."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

config = context.config

if config.config_file_name is not None:
    # Keep loggers created before an in-process upgrade (tests) working.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# No ORM models exist in this service -- see the module docstring.
target_metadata = None


def _database_url() -> str:
    """Explicit migration target; tests may supply a dedicated Config attribute."""
    url = config.attributes.get("database_url") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required; refusing to guess a migration target")
    # The application connects with psycopg3. Pin SQLAlchemy to the same driver
    # so a bare 'postgresql://' URL does not fall back to psycopg2.
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
