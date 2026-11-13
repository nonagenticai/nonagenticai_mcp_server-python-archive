from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url

from scripts.grant_runtime_role import grant
from src.storage.task_inputs import TaskInputRepository
from tests.fixtures import TENANT_A, TENANT_B
from tests.test_execution import enqueue, setup_runtime

ACCEPT = {"action": "accept", "content": {"decision": "approved"}}
ACTOR = {"sub": "tester"}


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database)


@pytest.fixture
def inputs(runtime):
    return TaskInputRepository(runtime.database.pool)


async def run_id(runtime):
    return (await enqueue(runtime))["run_id"]


async def fetch(runtime, query, *params):
    async with runtime.database.pool.connection() as conn:
        return await (await conn.execute(query, params)).fetchall()


async def states(inputs, run):
    return {r["source_ref"]: r["state"] for r in await inputs.open_for(TENANT_A, run)}


async def test_open_is_idempotent_per_source(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {"q": 1})
    assert key and key.startswith(f"{run}:")
    assert await inputs.open(TENANT_A, run, "ap-1", {"q": 1}) is None
    assert len(await inputs.open_for(TENANT_A, run)) == 1


async def test_open_stores_the_expiry(runtime, inputs):
    run = await run_id(runtime)
    expires = datetime.now(UTC) + timedelta(minutes=15)
    assert await inputs.open(TENANT_A, run, "ap-1", {}, expires_at=expires) is not None
    (row,) = await inputs.open_for(TENANT_A, run)
    assert row["expires_at"] == expires


async def test_open_of_an_expired_approval_leaves_a_closed_tombstone(runtime, inputs):
    run = await run_id(runtime)
    expired = datetime.now(UTC) - timedelta(minutes=1)
    assert await inputs.open(TENANT_A, run, "ap-1", {}, expires_at=expired) is None
    (row,) = await inputs.open_for(TENANT_A, run)
    assert row["state"] == "closed" and row["closed_at"] is not None
    # A later replay (fr re-sending the same event) cannot reopen it either.
    assert await inputs.open(TENANT_A, run, "ap-1", {}) is None
    assert (await inputs.open_for(TENANT_A, run))[0]["state"] == "closed"


async def test_open_for_unknown_or_foreign_run_is_none(runtime, inputs):
    run = await run_id(runtime)
    assert await inputs.open(TENANT_A, uuid4(), "ap-1", {}) is None
    assert await inputs.open(TENANT_B, run, "ap-1", {}) is None
    assert await inputs.open_for(TENANT_B, run) == []


async def test_close_before_open_leaves_no_open_input(runtime, inputs):
    run = await run_id(runtime)
    await inputs.close(TENANT_A, run, "ap-1")
    assert await inputs.open(TENANT_A, run, "ap-1", {"q": 1}) is None
    assert await states(inputs, run) == {"ap-1": "closed"}
    await inputs.close(TENANT_A, run, "ap-1")  # replay is harmless
    await inputs.close(TENANT_A, uuid4(), "x")  # unknown run is a no-op
    await inputs.close(TENANT_B, run, "ap-2")  # foreign run is a no-op
    assert await states(inputs, run) == {"ap-1": "closed"}


async def test_close_after_open_closes_it(runtime, inputs):
    run = await run_id(runtime)
    await inputs.open(TENANT_A, run, "ap-1", {})
    await inputs.close(TENANT_A, run, "ap-1")
    (row,) = await inputs.open_for(TENANT_A, run)
    assert row["state"] == "closed" and row["closed_at"] is not None


async def test_answer_ignores_unknown_and_answered_keys(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    assert await inputs.answer(TENANT_A, run, {key: ACCEPT, "nope": ACCEPT}, ACTOR) == 1
    assert await inputs.answer(TENANT_A, run, {key: ACCEPT, "nope": ACCEPT}, ACTOR) == 0
    (row,) = await inputs.open_for(TENANT_A, run)
    assert row["state"] == "answered" and row["response"] == ACCEPT
    assert row["actor"] == ACTOR and row["answered_at"] is not None


async def test_invalid_responses_are_ignored(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    bad = {"action": "maybe"}
    assert await inputs.answer(TENANT_A, run, {key: bad}, ACTOR) == 0
    assert await inputs.answer(TENANT_A, run, {key: "accept"}, ACTOR) == 0  # type: ignore[dict-item]
    assert await states(inputs, run) == {"ap-1": "open"}


async def test_decline_is_answered_as_is(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    assert await inputs.answer(TENANT_A, run, {key: {"action": "decline"}}, ACTOR) == 1
    (row,) = await inputs.open_for(TENANT_A, run)
    assert row["response"] == {"action": "decline"}


async def message(runtime, run):
    rows = await fetch(runtime, "SELECT status_message FROM mcp_runs WHERE run_id=%s", run)
    return rows[0]["status_message"]


async def test_reject_without_reason_stays_open(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    rejected = {"action": "accept", "content": {"decision": "rejected", "reason": "  "}}
    assert await inputs.answer(TENANT_A, run, {key: rejected}, ACTOR) == 0
    assert await states(inputs, run) == {"ap-1": "open"}
    assert await message(runtime, run) == "reason required"
    rejected["content"]["reason"] = "too risky"
    assert await inputs.answer(TENANT_A, run, {key: rejected}, ACTOR) == 1
    assert await message(runtime, run) is None


async def test_partial_answer_keeps_others_open(runtime, inputs):
    run = await run_id(runtime)
    k1 = await inputs.open(TENANT_A, run, "ap-1", {})
    k2 = await inputs.open(TENANT_A, run, "ap-2", {})
    k3 = await inputs.open(TENANT_A, run, "ap-3", {})
    rejected = {"action": "accept", "content": {"decision": "rejected"}}
    assert await inputs.answer(TENANT_A, run, {k1: ACCEPT, k2: rejected}, ACTOR) == 1
    assert await states(inputs, run) == {"ap-1": "answered", "ap-2": "open", "ap-3": "open"}
    # Still-unresolved rejection keeps the message even though another input was answered.
    assert await message(runtime, run) == "reason required"
    assert k3


async def test_cross_tenant_answer_is_noop(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    assert await inputs.answer(TENANT_B, run, {key: ACCEPT}, ACTOR) == 0
    assert await inputs.answer(TENANT_A, uuid4(), {key: ACCEPT}, ACTOR) == 0
    assert await states(inputs, run) == {"ap-1": "open"}


async def test_answer_on_terminal_run_is_noop(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    await fetch(runtime, "UPDATE mcp_runs SET status='cancelled' WHERE run_id=%s RETURNING 1", run)
    assert await inputs.answer(TENANT_A, run, {key: ACCEPT}, ACTOR) == 0
    assert await states(inputs, run) == {"ap-1": "open"}


async def test_claim_answered_then_mark_forwarded(runtime, inputs):
    run = await run_id(runtime)
    k1 = await inputs.open(TENANT_A, run, "ap-1", {})
    await inputs.open(TENANT_A, run, "ap-2", {})
    assert await inputs.claim_answered(10) == []
    await inputs.answer(TENANT_A, run, {k1: ACCEPT}, ACTOR)
    claimed = await inputs.claim_answered(10)
    assert [r["input_key"] for r in claimed] == [k1]
    assert [r["input_key"] for r in await inputs.claim_answered(10)] == [k1]  # at-least-once
    await inputs.mark_forwarded(k1)
    assert await inputs.claim_answered(10) == []
    assert (await states(inputs, run))["ap-1"] == "forwarded"
    await inputs.mark_forwarded(k1)  # no-op
    await inputs.mark_forwarded("missing")


async def input_row(runtime, key):
    return (await fetch(runtime, "SELECT * FROM mcp_task_inputs WHERE input_key=%s", key))[0]


async def test_reopen_answered_input(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    await inputs.answer(TENANT_A, run, {key: ACCEPT}, ACTOR)
    await inputs.reopen(key, "fr refused: " + "x" * 600)
    row = await input_row(runtime, key)
    assert row["state"] == "open" and row["response"] is None and row["actor"] is None
    assert row["answered_at"] is None
    assert await message(runtime, run) == ("fr refused: " + "x" * 600)[:512]
    # It can be answered again.
    assert await inputs.answer(TENANT_A, run, {key: ACCEPT}, ACTOR) == 1


async def test_reopen_is_noop_unless_answered(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    await inputs.reopen(key, "nope")
    await inputs.reopen("missing", "nope")
    assert (await input_row(runtime, key))["state"] == "open"
    assert await message(runtime, run) is None
    await inputs.close(TENANT_A, run, "ap-1")
    await inputs.reopen(key, "nope")
    assert (await input_row(runtime, key))["state"] == "closed"
    assert await message(runtime, run) is None


async def test_reopen_on_terminal_run_leaves_message(runtime, inputs):
    run = await run_id(runtime)
    key = await inputs.open(TENANT_A, run, "ap-1", {})
    await inputs.answer(TENANT_A, run, {key: ACCEPT}, ACTOR)
    await fetch(runtime, "UPDATE mcp_runs SET status='cancelled' WHERE run_id=%s RETURNING 1", run)
    await inputs.reopen(key, "late")
    assert (await input_row(runtime, key))["state"] == "open"
    assert await message(runtime, run) is None


async def test_close_everywhere_closes_every_run(runtime, inputs):
    first, second = await run_id(runtime), await run_id(runtime)
    k1 = await inputs.open(TENANT_A, first, "ap-1", {})
    await inputs.open(TENANT_A, second, "ap-1", {})
    await inputs.open(TENANT_A, second, "ap-2", {})
    await inputs.answer(TENANT_A, first, {k1: ACCEPT}, ACTOR)
    await inputs.close_everywhere("ap-1")
    assert await states(inputs, first) == {"ap-1": "closed"}
    assert await states(inputs, second) == {"ap-1": "closed", "ap-2": "open"}
    await inputs.close_everywhere("ap-1")  # replay is harmless
    await inputs.close_everywhere("unknown")
    assert await states(inputs, second) == {"ap-1": "closed", "ap-2": "open"}


async def test_runtime_role_grants_suffice(runtime, database_url):
    role = "task_inputs_" + uuid7().hex
    password = "synthetic-role-only"  # pragma: allowlist secret
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    grant(database_url, role)
    url = (
        make_url(database_url)
        .set(drivername="postgresql", username=role, password=password)
        .render_as_string(hide_password=False)
    )
    run = await run_id(runtime)
    async with AsyncConnectionPool(
        url,
        min_size=0,
        max_size=2,
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
        open=False,
    ) as pool:
        api = TaskInputRepository(pool)
        key = await api.open(TENANT_A, run, "ap-1", {"q": 1})
        assert key
        await api.close(TENANT_A, run, "ap-2")
        rejected = {"action": "accept", "content": {"decision": "rejected"}}
        assert await api.answer(TENANT_A, run, {key: rejected}, ACTOR) == 0
        assert await message(runtime, run) == "reason required"
        assert await api.answer(TENANT_A, run, {key: ACCEPT}, ACTOR) == 1
        assert await message(runtime, run) is None
