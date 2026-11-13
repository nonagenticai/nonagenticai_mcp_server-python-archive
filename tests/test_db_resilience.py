"""A lost connection is a transient error the loop retries; the pool replaces it."""

import psycopg
import pytest
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout
from pydantic import SecretStr

from src.config import Settings
from src.execution.worker import TRANSIENT_DB_ERRORS
from src.storage.database import Database


def test_the_transient_tuple_classifies_connection_failures_only() -> None:
    for error in (
        OperationalError("server closed the connection"),
        InterfaceError("connection is closed"),
        PoolTimeout("no connection available"),
    ):
        assert isinstance(error, TRANSIENT_DB_ERRORS)
    assert not isinstance(ValueError("a fault retrying cannot fix"), TRANSIENT_DB_ERRORS)


async def test_a_backend_terminated_under_the_pool_is_transient_then_replaced(database_url):
    settings = Settings(database_url=SecretStr(database_url), pool_min_size=1, pool_max_size=1)
    db = Database(settings)
    await db.open()
    try:
        # A real connection must exist in the pool before it can be killed.
        async with db.pool.connection() as conn:
            row = await (await conn.execute("SELECT pg_backend_pid() AS pid")).fetchone()
            assert row is not None
            killed = row["pid"]
        with psycopg.connect(settings.dsn, autocommit=True) as killer:
            assert killer.execute(
                "SELECT pg_terminate_backend(%s) AS killed", (killed,)
            ).fetchone() == (True,)
        # The dead handle surfaces exactly as the executor saw it in production: a transient
        # OperationalError, which the worker loop now retries instead of exiting.
        with pytest.raises(psycopg.OperationalError):
            async with db.pool.connection() as conn:
                await conn.execute("SELECT 1")
        # The pool discards a connection whose transaction status is UNKNOWN, so the retry is
        # served by a live backend rather than the same dead handle.
        async with db.pool.connection() as conn:
            row = await (await conn.execute("SELECT pg_backend_pid() AS pid")).fetchone()
            assert row is not None
            assert row["pid"] != killed
    finally:
        await db.close()
