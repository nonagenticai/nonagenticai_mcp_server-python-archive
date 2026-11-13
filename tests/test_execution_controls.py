"""Shared ceilings, safe draining, payload erasure and startup deadlines."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from psycopg.errors import RaiseException

from scripts.configure_execution_capacity import configure
from src.broker.limits import RateLimits
from src.execution.controller import Controller
from src.execution.models import ExecutionDeniedError, envelope
from src.execution.scheduler import Scheduler
from src.storage.executions import ExecutionRepository
from tests.fixtures import TENANT_A, service, settings
from tests.test_execution import FakeBackend, claim, enqueue, make_broker, setup_runtime


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database)


async def test_different_services_share_global_permits_and_denials_commit(runtime):
    row = await claim(runtime)
    config = runtime.config.model_copy(update={"api_concurrency_global": 1})
    first = ExecutionRepository(runtime.database.pool, config)
    other_worker = ExecutionRepository(runtime.database.pool, config)
    permit = await first.permit(row, service(1).service_id)
    with pytest.raises(ExecutionDeniedError):
        await other_worker.permit(row, service(2).service_id)
    await first.release_permit(row, permit)
    await other_worker.permit(row, service(2).service_id)
    events = await first.events(TENANT_A, row["run_id"], 0, 100)
    assert any(e["kind"] == "api_concurrency_denied" for e in events)


async def test_deployment_service_and_tenant_rate_ceilings_are_shared(runtime, tmp_path):
    row = await claim(runtime)
    path = tmp_path / "limits.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "limits": [
                    {
                        "service_id": str(service().service_id),
                        "tenant_id": str(TENANT_A),
                        "calls_per_minute": 1,
                        "concurrency": 1,
                    }
                ],
            }
        )
    )
    config = runtime.config.model_copy(update={"api_rate_limits_file": path})
    first, second = [ExecutionRepository(runtime.database.pool, config) for _ in range(2)]
    permit = await first.permit(row, service().service_id)
    await first.release_permit(row, permit)
    with pytest.raises(ExecutionDeniedError):
        await second.permit(row, service().service_id)
    # Another service has its own minute allowance.
    await second.permit(row, service(2).service_id)
    events = await first.events(TENANT_A, row["run_id"], 0, 100)
    assert any(e["kind"] == "api_rate_denied" for e in events)


def test_rate_configuration_is_bounded_and_closed(tmp_path):
    path = tmp_path / "limits.json"
    for payload in [
        b'{"schema_version":2,"limits":[]}',
        b'{"schema_version":1,"limits":[],"url":"https://evil.example"}',
        b"x" * 262145,
    ]:
        path.write_bytes(payload)
        with pytest.raises(ValueError):
            RateLimits.read(path)
    item = {"service_id": str(uuid7()), "calls_per_minute": 2, "concurrency": 1}
    path.write_text(json.dumps({"schema_version": 1, "limits": [item, item]}))
    with pytest.raises(ValueError, match="duplicate"):
        RateLimits.read(path)


async def test_capacity_shrink_drains_owned_and_quarantined_slots(runtime):
    row = await claim(runtime)
    # Move the owned slot to the excess portion before operator shrink.
    async with runtime.database.pool.connection() as conn:
        await conn.execute(
            "UPDATE mcp_execution_slots SET slot_id=3 WHERE run_id=%s", (row["run_id"],)
        )
    configure(runtime.database.pool.conninfo, 1)
    repo = ExecutionRepository(
        runtime.database.pool, runtime.config.model_copy(update={"execution_global_slots": 1})
    )
    await Scheduler(repo).verify_capacity()
    async with runtime.database.pool.connection() as conn:
        owned = await (
            await conn.execute("SELECT draining,run_id FROM mcp_execution_slots WHERE slot_id=3")
        ).fetchone()
    assert owned == {"draining": True, "run_id": row["run_id"]}
    await repo.finish(row, "failed", code="fixture", absent=False)
    configure(runtime.database.pool.conninfo, 1)
    async with runtime.database.pool.connection() as conn:
        assert (
            await (
                await conn.execute("SELECT quarantined FROM mcp_execution_slots WHERE slot_id=3")
            ).fetchone()
        )["quarantined"]
    await repo.finish(row, "failed", absent=True)
    await enqueue(runtime)
    next_row = await Scheduler(repo).claim(uuid7())
    async with runtime.database.pool.connection() as conn:
        assert (
            await (
                await conn.execute(
                    "SELECT slot_id FROM mcp_execution_slots WHERE run_id=%s", (next_row["run_id"],)
                )
            ).fetchone()
        )["slot_id"] == 1
    configure(runtime.database.pool.conninfo, 2)
    await runtime.scheduler.verify_capacity()


async def test_payload_retention_preserves_receipts_and_immutable_history(runtime):
    row = await claim(runtime)
    await runtime.repository.finish(
        row, "failed", result={"sensitive": "fixture"}, code="fixture", absent=True
    )
    # Operator-controlled synthetic clock aging, never disable guards.
    async with runtime.database.pool.connection() as conn:
        await conn.execute(
            "UPDATE mcp_runs SET finished_at=clock_timestamp()-interval '8 days' WHERE run_id=%s",
            (row["run_id"],),
        )
    assert await runtime.repository.expire_payloads() == 1
    assert await runtime.repository.expire_payloads() == 0
    expired = await runtime.repository.get(TENANT_A, row["run_id"])
    assert expired["inputs"] == expired["authority"] == expired["profile_snapshot"] == {}
    assert expired["result"] is None and envelope(expired)["payload_expired"]
    assert expired["input_hash"] == row["input_hash"] and expired["tool_id"] == row["tool_id"]
    assert await runtime.repository.events(TENANT_A, row["run_id"], 0, 100)
    with pytest.raises(RaiseException):
        async with runtime.database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_runs SET payload_expired_at=NULL WHERE run_id=%s", (row["run_id"],)
            )
    with pytest.raises(RaiseException):
        async with runtime.database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_runs SET inputs='{}'::jsonb || '{\"forged\":true}'::jsonb WHERE run_id=%s",
                (row["run_id"],),
            )


async def test_retention_never_erases_active_or_quarantined_work(runtime):
    row = await claim(runtime)
    assert await runtime.repository.expire_payloads() == 0
    await runtime.repository.finish(row, "failed", code="fixture", absent=False)
    async with runtime.database.pool.connection() as conn:
        await conn.execute(
            "UPDATE mcp_runs SET finished_at=clock_timestamp()-interval '8 days' WHERE run_id=%s",
            (row["run_id"],),
        )
    assert await runtime.repository.expire_payloads() == 0
    assert not envelope(await runtime.repository.get(TENANT_A, row["run_id"]))["payload_expired"]


async def test_finish_marks_abandoned_intent_unknown_before_releasing_ownership(runtime):
    from tests.fixtures import operation

    row = await claim(runtime)
    await runtime.repository.journal_intent(row, operation(), "a" * 64, None, uuid7())
    await runtime.repository.finish(row, "interrupted", code="channel_lost", absent=True)
    async with runtime.database.pool.connection() as conn:
        assert (
            await (
                await conn.execute(
                    "SELECT status FROM mcp_run_api_calls WHERE run_id=%s", (row["run_id"],)
                )
            ).fetchone()
        )["status"] == "outcome_unknown"


async def test_silent_runner_is_bounded_by_start_deadline(runtime, tmp_path, monkeypatch):
    row = await claim(runtime)
    row["start_deadline"] = datetime.now(UTC) + timedelta(seconds=0.15)
    backend = FakeBackend()

    async def silent():
        await asyncio.sleep(30)

    monkeypatch.setattr(backend.channel, "receive", silent)
    broker, _, _, _ = await make_broker(runtime, tmp_path)
    try:
        await asyncio.wait_for(
            Controller(
                runtime.repository, runtime.execution, broker, backend, runtime.catalog.repository
            ).execute(row),
            3,
        )
        assert (await runtime.repository.get(TENANT_A, row["run_id"]))["status"] == "timed_out"
        assert not backend.pods
    finally:
        await broker.close()


@pytest.mark.parametrize(
    "override",
    [
        {"queue_global_max": 1, "queue_per_tenant_max": 2},
        {"max_frame_bytes": 2048, "max_output_bytes": 1024},
        {"sync_wait_seconds": 15, "protocol_timeout_seconds": 15},
    ],
)
def test_inconsistent_server_ceilings_fail_startup(override):
    with pytest.raises(ValueError):
        settings(**override)
