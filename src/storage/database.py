"""One bounded async pool per application; no database/schema auto-creation."""

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self.pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            settings.dsn,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            timeout=settings.pool_timeout_seconds,
            max_waiting=settings.pool_max_waiting,
            # Recycle before pgbouncer/postgres can close a connection underneath us. A pool
            # ``check=`` callback is deliberately NOT used: psycopg_pool leaks the checked-out
            # connection if the checking task is cancelled, which deadlocks worker shutdown
            # (tests/test_execution.py::test_controller_worker_shutdown_cleans_up_then_propagates_cancel).
            # A connection lost anyway surfaces as psycopg.OperationalError, which the worker
            # loop and the external driver both retry; the pool discards it on return.
            max_idle=settings.pool_max_idle_seconds,
            max_lifetime=settings.pool_max_lifetime_seconds,
            reconnect_timeout=settings.pool_reconnect_timeout_seconds,
            open=False,
            kwargs={
                "row_factory": dict_row,
                # All traffic goes through pgbouncer transaction pooling: a server-side prepared
                # statement outlives a schema change on pgbouncer's server connection.
                "prepare_threshold": None,
                "connect_timeout": 3,
                "options": f"-c statement_timeout={settings.database_statement_timeout_ms}",
            },
        )

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        await self.pool.close()
