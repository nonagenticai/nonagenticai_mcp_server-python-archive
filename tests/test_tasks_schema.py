import dataclasses
from uuid import uuid7

import pytest
from psycopg.errors import CheckViolation, RaiseException, UniqueViolation
from psycopg.types.json import Jsonb

from src.execution.scheduler import Scheduler
from tests.fixtures import TENANT_A, external_tool
from tests.test_execution import claim, enqueue, setup_runtime


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database)


async def sql(runtime, query, *params):
    async with runtime.database.pool.connection() as conn:
        return await conn.execute(query, params)


async def test_runtime_defaults_and_is_immutable(runtime):
    row = await enqueue(runtime)
    assert row["runtime"] == "kata_python"
    with pytest.raises(RaiseException, match="runtime is immutable"):
        await sql(
            runtime, "UPDATE mcp_runs SET runtime='n8n_workflow' WHERE run_id=%s", row["run_id"]
        )


async def test_external_ref_only_grows(runtime):
    row = await enqueue(runtime)
    run = row["run_id"]
    update = "UPDATE mcp_runs SET external_ref=%s WHERE run_id=%s"
    await sql(runtime, update, Jsonb({"kind": "n8n", "id": "42"}), run)
    # Identical value is a no-op; adding a key is allowed.
    await sql(runtime, update, Jsonb({"kind": "n8n", "id": "42"}), run)
    await sql(runtime, update, Jsonb({"kind": "n8n", "id": "42", "project_id": "p"}), run)
    for value in (
        Jsonb({"kind": "n8n", "id": "43", "project_id": "p"}),
        Jsonb({"kind": "n8n", "id": "42"}),
        Jsonb({}),
        None,
    ):
        with pytest.raises(RaiseException, match="external reference only grows"):
            await sql(runtime, update, value, run)


async def test_actor_is_immutable_after_insert(runtime):
    row = await enqueue(runtime)
    with pytest.raises(RaiseException, match="run snapshot is immutable"):
        await sql(
            runtime,
            "UPDATE mcp_runs SET actor=%s WHERE run_id=%s",
            Jsonb({"sub": "someone"}),
            row["run_id"],
        )


async def test_status_message_is_bounded_and_mutable(runtime):
    row = await enqueue(runtime)
    await sql(runtime, "UPDATE mcp_runs SET status_message='ok' WHERE run_id=%s", row["run_id"])
    await sql(runtime, "UPDATE mcp_runs SET status_message='x' WHERE run_id=%s", row["run_id"])
    with pytest.raises(CheckViolation):
        await sql(
            runtime,
            "UPDATE mcp_runs SET status_message=%s WHERE run_id=%s",
            "x" * 513,
            row["run_id"],
        )


async def add_input(runtime, run_id, source_ref):
    await sql(
        runtime,
        "INSERT INTO mcp_task_inputs(input_key,tenant_id,run_id,source_ref,request) "
        "VALUES (%s,%s,%s,%s,%s)",
        str(uuid7()),
        TENANT_A,
        run_id,
        source_ref,
        Jsonb({"q": 1}),
    )


async def test_task_input_unique_per_source(runtime):
    row = await enqueue(runtime)
    await add_input(runtime, row["run_id"], "approval-1")
    with pytest.raises(UniqueViolation):
        await add_input(runtime, row["run_id"], "approval-1")
    await add_input(runtime, row["run_id"], "approval-2")


async def test_task_input_requires_object_request_and_own_run(runtime):
    row = await enqueue(runtime)
    with pytest.raises(CheckViolation):
        await sql(
            runtime,
            "INSERT INTO mcp_task_inputs(input_key,tenant_id,run_id,source_ref,request) "
            "VALUES ('k',%s,%s,'s','[]')",
            TENANT_A,
            row["run_id"],
        )


async def test_scheduler_never_claims_external_runtime(runtime):
    resolved = dataclasses.replace(
        runtime.resolved, manifest_json=external_tool().model_dump_json()
    )
    row = await runtime.repository.accept(
        runtime.principal, resolved, {}, None, await runtime.catalog.repository.generation()
    )
    assert row["runtime"] == "n8n_workflow"
    assert await Scheduler(runtime.repository).claim(uuid7()) is None
    assert (await runtime.repository.get(TENANT_A, row["run_id"]))["status"] == "queued"


async def test_payload_expiration_still_works_with_new_columns(runtime):
    row = await claim(runtime)
    await runtime.repository.finish(row, "failed", result={"s": 1}, code="fixture", absent=True)
    await sql(runtime, "UPDATE mcp_runs SET status_message='done' WHERE run_id=%s", row["run_id"])
    await sql(
        runtime,
        "UPDATE mcp_runs SET finished_at=clock_timestamp()-interval '8 days' WHERE run_id=%s",
        row["run_id"],
    )
    assert await runtime.repository.expire_payloads() == 1
    expired = await runtime.repository.get(TENANT_A, row["run_id"])
    assert expired["payload_expired_at"] is not None and expired["status_message"] == "done"
