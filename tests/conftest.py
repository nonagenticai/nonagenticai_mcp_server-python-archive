"""Disposable PostgreSQL only. Runtime DATABASE_URL is never a test target."""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid7

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from pydantic import SecretStr

from src.config import Settings
from src.storage.database import Database
from tests.ephemeral_db import docker_available, start_ephemeral_postgres


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[str]:
    explicit = os.getenv("TEST_DATABASE_URL") or os.getenv("TEST_POSTGRES_URL")
    if explicit:
        yield explicit.replace("postgresql+psycopg://", "postgresql://", 1)
        return
    if not docker_available():
        pytest.fail(
            "Docker or an explicit TEST_DATABASE_URL with database-create and role-create permissions is required"
        )
    pg = start_ephemeral_postgres()
    try:
        yield pg.dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    finally:
        pg.stop()


def migration_config(dsn: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = dsn
    return config


@pytest.fixture
def empty_database(postgres_server: str) -> Iterator[str]:
    name = "mcp_test_" + uuid7().hex
    with psycopg.connect(postgres_server, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    # Preserve explicit SSL/connection query options while targeting only our new database.
    from sqlalchemy.engine import make_url

    dsn = (
        make_url(postgres_server)
        .set(drivername="postgresql+psycopg", database=name)
        .render_as_string(hide_password=False)
    )

    try:
        yield dsn
    finally:
        with psycopg.connect(postgres_server, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def database_url(empty_database: str) -> str:
    command.upgrade(migration_config(empty_database), "head")
    return empty_database


@pytest.fixture
async def database(database_url: str) -> AsyncIterator[Database]:
    db = Database(Settings(database_url=SecretStr(database_url), pool_min_size=0))
    await db.open()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture(autouse=True)
def _gated_legal_sources_visible(monkeypatch):
    """Gated legal tools (UOKiK, hidden unless LEGAL_UOKIK_ENABLED) are visible in tests by default;
    test_uokik_tools_are_hidden_unless_enabled checks the gate itself."""
    from src.builtin.legal import apply_source_gates

    monkeypatch.setenv("LEGAL_UOKIK_ENABLED", "true")
    apply_source_gates()
    yield
