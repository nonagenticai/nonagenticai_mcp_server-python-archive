"""Worker entrypoint lifecycle and strict role split with disposable infrastructure."""

import asyncio
import json
import signal
from typing import LiteralString, cast
from uuid import uuid7

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from scripts.grant_runtime_role import grant, grant_execution
from src.catalog.resolver import CatalogResolver
from src.execution.scheduler import Scheduler
from src.execution.service import ExecutionService
from src.storage.bindings import BindingRepository
from src.storage.database import Database
from src.storage.executions import ExecutionRepository
from src.toolkits.service import AccessService
from tests.fixtures import TENANT_A, operation, settings
from tests.test_execution import FakeBackend, enqueue, setup_runtime


async def test_runtime_roles_admit_claim_and_journal_without_cross_privileges(
    database, database_url
):
    runtime = await setup_runtime(database)
    parsed = make_url(database_url)
    api_role = "execution_api_" + uuid7().hex
    worker_role = "execution_worker_" + uuid7().hex
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    password = "synthetic-role-only"  # pragma: allowlist secret
    with psycopg.connect(dsn) as conn:
        for role in (api_role, worker_role):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
    grant(database_url, api_role)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(worker_role))
        )
        grant_execution(conn, worker_role, worker=True)
    api_db = Database(
        runtime.config.model_copy(
            update={
                "database_url": SecretStr(
                    parsed.set(username=api_role, password=password).render_as_string(
                        hide_password=False
                    )
                )
            }
        )
    )
    worker_db = Database(
        runtime.config.model_copy(
            update={
                "database_url": SecretStr(
                    parsed.set(username=worker_role, password=password).render_as_string(
                        hide_password=False
                    )
                )
            }
        )
    )
    try:
        await api_db.open()
        await worker_db.open()
        resolver = CatalogResolver(AccessService(BindingRepository(api_db.pool), runtime.config))
        api_repo = ExecutionRepository(api_db.pool, runtime.config)
        result = await ExecutionService(api_repo, resolver).submit(
            runtime.principal, runtime.resolved, {}, None
        )
        await api_repo.cancel(TENANT_A, result["run_id"])
        await ExecutionService(api_repo, resolver).submit(
            runtime.principal, runtime.resolved, {}, None
        )
        worker_repo = ExecutionRepository(worker_db.pool, runtime.config)
        row = await Scheduler(worker_repo).claim(uuid7())
        assert row and row["run_id"].version == 7 and row["worker_id"].version == 7
        worker_resolver = CatalogResolver(
            AccessService(BindingRepository(worker_db.pool), runtime.config)
        )
        assert (await ExecutionService(worker_repo, worker_resolver).live(row))[
            0
        ].tool == runtime.resolved.effective.tool
        sequence = await worker_repo.journal_intent(row, operation(), "a" * 64, None, uuid7())
        await worker_repo.journal_outcome(row, sequence, "succeeded")
        permit = await worker_repo.permit(row, operation().service.id)
        assert permit.version == 7
        await worker_repo.release_permit(row, permit)
        await worker_repo.finish(row, "failed", code="fixture_done", absent=True)
        for db, statement in [
            (api_db, "UPDATE mcp_runs SET fence=fence+1"),
            (api_db, "INSERT INTO mcp_execution_slots(slot_id) VALUES(3)"),
            (worker_db, "UPDATE mcp_bindings SET revoked=true,revision=revision+1"),
            (worker_db, "CREATE TABLE forbidden(id int)"),
        ]:
            with pytest.raises(InsufficientPrivilege):
                async with db.pool.connection() as conn:
                    await conn.execute(cast(LiteralString, statement))
    finally:
        await api_db.close()
        await worker_db.close()
        with psycopg.connect(dsn) as conn:
            for role in (api_role, worker_role):
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_worker_lifecycle_never_imports_uploaded_code(database, tmp_path, monkeypatch):
    from src.execution import worker

    runtime = await setup_runtime(database)
    await enqueue(runtime)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema_version": 1, "entries": []}))
    bundle.chmod(0o600)
    config = runtime.config.model_copy(
        update={
            "database_url": SecretStr(database.pool.conninfo),
            "credential_bundle_file": bundle,
        }
    )
    callbacks = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: callbacks.__setitem__(sig, cb))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)

    class Backend(FakeBackend):
        def __init__(self, settings):
            super().__init__()

        async def close(self):
            pass

    class Controller:
        def __init__(self, *args):
            pass

        async def execute(self, row):
            callbacks[signal.SIGTERM]()

    class Health:
        should_exit = False

        def __init__(self, *args):
            pass

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr(worker, "KataBackend", Backend)
    monkeypatch.setattr(worker, "Controller", Controller)
    monkeypatch.setattr(worker, "HealthServer", Health)
    await asyncio.wait_for(worker.run(config), 5)
    assert signal.SIGTERM in callbacks
    with pytest.raises(ValueError):
        await worker.run(settings())


async def test_worker_survives_a_transient_database_failure(database, tmp_path, monkeypatch):
    """The executor exited on exactly this error in production; it must retry instead."""
    from src.execution import worker

    runtime = await setup_runtime(database)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"schema_version": 1, "entries": []}))
    bundle.chmod(0o600)
    config = runtime.config.model_copy(
        update={
            "database_url": SecretStr(database.pool.conninfo),
            "credential_bundle_file": bundle,
        }
    )
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: None)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)

    class Backend(FakeBackend):
        async def close(self):
            pass

    class Controller:
        def __init__(self, *args):
            pass

        async def execute(self, row):
            raise AssertionError("no run can be claimed while the database is failing")

    class Health:
        should_exit = False

        def __init__(self, *args):
            pass

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0)

    monkeypatch.setattr(worker, "KataBackend", Backend)
    monkeypatch.setattr(worker, "Controller", Controller)
    monkeypatch.setattr(worker, "HealthServer", Health)
    attempts = 0

    async def failing_claim(self, worker_id):
        nonlocal attempts
        attempts += 1
        raise psycopg.OperationalError(
            "consuming input failed: server closed the connection unexpectedly"
        )

    monkeypatch.setattr(Scheduler, "claim", failing_claim)
    task = asyncio.create_task(worker.run(config))
    await asyncio.sleep(0.6)
    assert not task.done(), "a transient database error must not exit the worker"
    assert attempts >= 2, "the worker must retry after backing off"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
