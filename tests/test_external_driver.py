"""External-run driver against disposable PostgreSQL with a scripted adapter (no HTTP)."""

import asyncio
import dataclasses
from collections import defaultdict, deque
from uuid import uuid7

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from scripts.grant_runtime_role import grant_execution
from src.catalog.models import FrCommandTarget, FrProjectTarget
from src.external.adapter import ExternalRef, ExternalStartError, ExternalState
from src.external.driver import ExternalDriver, build_adapters
from src.external.fr import FrCommandAdapter
from src.storage.database import Database
from src.storage.executions import ExecutionRepository
from src.storage.task_inputs import TaskInputRepository
from tests.fixtures import TENANT_A, external_tool, settings
from tests.test_execution import claim, enqueue, setup_runtime

ACCEPT = {"action": "accept", "content": {"decision": "approved"}}


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database, worker_max_inflight=4)


class FakeAdapter:
    """Records every call; start/poll results are scripted per run (or defaults)."""

    def __init__(self, kind="n8n"):
        self.kind = kind
        self.calls = []
        self.start_results = deque()
        self.polls = defaultdict(deque)
        self.cancel_error = None
        self.forward_error = None
        self.forward_result = None
        #: Seconds to spend inside the call; a slow adapter must not outlive the run's lease.
        self.start_delay = 0.0
        self.poll_delay = 0.0

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)

    async def start(self, row):
        self.calls.append(("start", row["run_id"]))
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        result = self.start_results.popleft() if self.start_results else None
        if isinstance(result, BaseException):
            raise result
        return result or ExternalRef(self.kind, f"ext-{row['run_id']}")

    async def poll(self, row):
        self.calls.append(("poll", row["run_id"]))
        if self.poll_delay:
            await asyncio.sleep(self.poll_delay)
        queue = self.polls[row["run_id"]]
        result = queue.popleft() if queue else ExternalState("running")
        if isinstance(result, BaseException):
            raise result
        return result

    async def cancel(self, row):
        self.calls.append(("cancel", row["run_id"]))
        if self.cancel_error:
            raise self.cancel_error

    async def forward(self, row, item):
        self.calls.append(("forward", row["run_id"], item["input_key"]))
        if self.forward_error:
            raise self.forward_error
        return self.forward_result


def make_driver(runtime, n8n=None, fr=None, *, worker=None, **overrides):
    n8n = n8n or FakeAdapter("n8n")
    fr = fr or FakeAdapter("fr")
    return ExternalDriver(
        runtime.repository,
        TaskInputRepository(runtime.database.pool),
        {"n8n_workflow": n8n, "fr_project": fr},
        runtime.config.model_copy(update=overrides),
        worker or uuid7(),
    )


async def submit(runtime, /, **tool_updates):
    resolved = dataclasses.replace(
        runtime.resolved, manifest_json=external_tool(**tool_updates).model_dump_json()
    )
    return await runtime.repository.accept(
        runtime.principal, resolved, {}, None, await runtime.catalog.repository.generation()
    )


async def submit_fr(runtime):
    return await submit(
        runtime,
        runtime="fr_project",
        target=FrProjectTarget(kind="fr_project", project_type="demo"),
    )


async def get(runtime, row):
    return await runtime.repository.get(TENANT_A, row["run_id"])


async def sql_exec(runtime, query, *params):
    async with runtime.database.pool.connection() as conn:
        return await conn.execute(query, params)


async def events(runtime, row):
    async with runtime.database.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT kind FROM mcp_run_events WHERE run_id=%s ORDER BY event_id", (row["run_id"],)
        )
        return [r["kind"] for r in await cur.fetchall()]


async def expire_lease(runtime, row):
    await sql_exec(
        runtime,
        "UPDATE mcp_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
        row["run_id"],
    )


async def test_start_saves_ref_and_runs(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    await make_driver(runtime, adapter).tick()
    run = await get(runtime, row)
    assert run["status"] == "running"
    assert run["external_ref"] == {"kind": "n8n", "id": f"ext-{row['run_id']}"}
    assert run["started_at"] is not None and run["worker_id"] is not None
    assert adapter.count("start") == 1
    assert await events(runtime, row) == ["queued", "starting", "dispatch", "running"]


async def test_start_error_fails_run_with_code(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    adapter.start_results.append(ExternalStartError("external_start_failed"))
    await make_driver(runtime, adapter).tick()
    run = await get(runtime, row)
    assert run["status"] == "failed" and run["error"]["code"] == "external_start_failed"
    assert run["worker_id"] is None and run["external_ref"] is None


async def test_crash_after_dispatch_is_interrupted_not_redispatched(runtime):
    row = await submit(runtime)
    # A prior driver wrote the dispatch event, then died before saving external_ref.
    await sql_exec(
        runtime,
        "UPDATE mcp_runs SET status='starting',worker_id=%s,fence=fence+1,"
        "lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
        uuid7(),
        row["run_id"],
    )
    async with runtime.database.pool.connection() as conn:
        await runtime.repository.event(conn, row, "dispatch")
    adapter = FakeAdapter()
    await make_driver(runtime, adapter).tick()
    run = await get(runtime, row)
    assert run["status"] == "interrupted" and run["error"]["code"] == "external_start_unknown"
    assert run["external_ref"] is None and run["worker_id"] is None
    assert adapter.calls == []


async def test_start_transport_error_is_interrupted_and_never_redispatched(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    adapter.start_results.append(httpx.ReadTimeout("timeout"))
    driver = make_driver(runtime, adapter)
    await driver.tick()
    run = await get(runtime, row)
    assert run["status"] == "interrupted" and run["error"]["code"] == "external_start_unknown"
    await driver.tick()
    assert adapter.count("start") == 1
    assert (await get(runtime, row))["status"] == "interrupted"


async def test_starting_without_dispatch_event_is_started_by_second_driver(runtime):
    row = await submit(runtime)
    await sql_exec(
        runtime,
        "UPDATE mcp_runs SET status='starting',worker_id=%s,fence=fence+1,"
        "lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
        uuid7(),
        row["run_id"],
    )
    adapter = FakeAdapter()
    second = uuid7()
    await make_driver(runtime, adapter, worker=second).tick()
    run = await get(runtime, row)
    assert run["status"] == "running" and run["worker_id"] == second
    assert run["fence"] == 2 and adapter.count("start") == 1


async def test_running_run_with_expired_lease_is_adopted_and_polled(runtime):
    row = await submit(runtime)
    first_adapter = FakeAdapter()
    await make_driver(runtime, first_adapter).tick()
    started = await get(runtime, row)
    await expire_lease(runtime, started)
    adapter = FakeAdapter()
    adapter.polls[row["run_id"]].append(ExternalState("succeeded", result={"ok": 1}))
    second = uuid7()
    await make_driver(runtime, adapter, worker=second).tick()
    run = await get(runtime, row)
    assert run["status"] == "succeeded" and run["result"] == {"ok": 1}
    assert run["fence"] == started["fence"] + 1
    assert adapter.count("start") == 0 and adapter.count("poll") == 1


async def test_poll_success_finishes_with_result(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    adapter.polls[row["run_id"]].append(ExternalState("succeeded", result={"answer": 42}))
    await driver.tick()
    run = await get(runtime, row)
    assert run["status"] == "succeeded" and run["result"] == {"answer": 42}
    assert run["worker_id"] is None and run["finished_at"] is not None


async def test_poll_failure_maps_code_with_default(runtime):
    first, second = await submit(runtime), await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    adapter.polls[first["run_id"]].append(ExternalState("failed", code="workflow_error"))
    adapter.polls[second["run_id"]].append(ExternalState("failed"))
    await driver.tick()
    assert (await get(runtime, first))["error"]["code"] == "workflow_error"
    run = await get(runtime, second)
    assert run["status"] == "failed" and run["error"]["code"] == "external_failed"


async def test_cancel_requested_calls_adapter_then_cancelled(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    await runtime.repository.cancel(TENANT_A, row["run_id"])
    await driver.tick()
    assert adapter.count("cancel") == 1
    assert (await get(runtime, row))["status"] == "cancelling"
    adapter.polls[row["run_id"]].append(ExternalState("cancelled"))
    await driver.tick()
    run = await get(runtime, row)
    assert run["status"] == "cancelled" and run["worker_id"] is None
    assert adapter.count("cancel") == 1


async def test_cancel_error_retries_next_tick(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    await runtime.repository.cancel(TENANT_A, row["run_id"])
    adapter.cancel_error = httpx.ConnectError("down")
    await driver.tick()
    assert (await get(runtime, row))["status"] == "running"
    adapter.cancel_error = None
    await driver.tick()
    assert (await get(runtime, row))["status"] == "cancelling"
    assert adapter.count("cancel") == 2


async def test_external_side_cancel_without_request_ends_cancelled(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    adapter.polls[row["run_id"]].append(ExternalState("cancelled"))
    await driver.tick()
    run = await get(runtime, row)
    assert run["status"] == "cancelled" and run["cancel_requested"] is False
    assert await events(runtime, row) == [
        "queued",
        "starting",
        "dispatch",
        "running",
        "cancelling",
        "cancelled",
    ]


async def test_deadline_times_out_and_cancels(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    await sql_exec(
        runtime,
        "UPDATE mcp_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
        row["run_id"],
    )
    adapter.cancel_error = httpx.ConnectError("down")  # best effort: swallowed
    polls_before = adapter.count("poll")
    await driver.tick()
    run = await get(runtime, row)
    assert run["status"] == "timed_out" and run["worker_id"] is None
    assert adapter.count("cancel") == 1 and adapter.count("poll") == polls_before


async def test_deadline_uses_external_run_max_seconds(runtime):
    row = await submit(runtime)
    await make_driver(runtime, external_run_max_seconds=120).tick()
    run = await get(runtime, row)
    async with runtime.database.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT extract(epoch FROM deadline-clock_timestamp()) AS left FROM mcp_runs WHERE run_id=%s",
            (row["run_id"],),
        )
        remaining = (await cur.fetchone())["left"]
    assert run["status"] == "running"
    assert 100 < remaining <= 120 and runtime.config.run_max_seconds < 100


async def test_queued_external_runs_expire_in_driver(runtime):
    # queue_deadline is immutable, so accept one run with a one-second queue timeout.
    quick = ExecutionRepository(
        runtime.database.pool, runtime.config.model_copy(update={"run_queue_timeout_seconds": 1})
    )
    resolved = dataclasses.replace(
        runtime.resolved, manifest_json=external_tool().model_dump_json()
    )
    late = await quick.accept(
        runtime.principal, resolved, {}, None, await runtime.catalog.repository.generation()
    )
    cancelled = await submit(runtime)
    await asyncio.sleep(1.2)
    await runtime.repository.cancel(TENANT_A, cancelled["run_id"])
    adapter = FakeAdapter()
    await make_driver(runtime, adapter).tick()
    assert (await get(runtime, late))["status"] == "timed_out"
    assert (await get(runtime, cancelled))["status"] == "cancelled"
    assert "timed_out" in await events(runtime, late)
    assert adapter.calls == []


async def test_status_message_follows_external_state(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    adapter.polls[row["run_id"]].append(ExternalState("running", message="waiting for approval"))
    await driver.tick()
    assert (await get(runtime, row))["status_message"] == "waiting for approval"
    # No message from the adapter never erases one set elsewhere.
    await driver.tick()
    assert (await get(runtime, row))["status_message"] == "waiting for approval"


async def test_failing_poll_does_not_block_other_runs(runtime):
    broken, healthy = await submit(runtime), await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    adapter.polls[broken["run_id"]].append(httpx.ReadTimeout("secret-looking-detail"))
    adapter.polls[healthy["run_id"]].append(ExternalState("succeeded", result={}))
    await driver.tick()
    assert (await get(runtime, broken))["status"] == "running"
    assert (await get(runtime, healthy))["status"] == "succeeded"
    await driver.tick()
    assert adapter.count("poll") >= 3


async def test_inflight_limit_bounds_claims(runtime):
    rows = [await submit(runtime) for _ in range(3)]
    driver = make_driver(runtime, worker_max_inflight=2)
    await driver.tick()
    statuses = sorted([(await get(runtime, r))["status"] for r in rows])
    assert statuses == ["queued", "running", "running"]


async def test_running_run_does_not_hold_a_dispatch_slot(runtime):
    """A project that runs for days must not stop the next start from being dispatched."""
    first, second = await submit(runtime), await submit(runtime)
    driver = make_driver(runtime, worker_max_inflight=1)
    await driver.tick()
    assert (await get(runtime, first))["status"] == "running"
    assert (await get(runtime, second))["status"] == "queued"
    await driver.tick()
    assert (await get(runtime, second))["status"] == "running"


async def test_external_active_max_bounds_owned_runs(runtime):
    rows = [await submit(runtime) for _ in range(2)]
    driver = make_driver(runtime, worker_max_inflight=8, external_active_max=1)
    await driver.tick()
    assert sorted([(await get(runtime, r))["status"] for r in rows]) == ["queued", "running"]
    await driver.tick()
    assert sorted([(await get(runtime, r))["status"] for r in rows]) == ["queued", "running"]


async def test_recovery_claims_before_new_work(runtime):
    """A run abandoned mid-dispatch is adopted before a queued run takes the dispatch room."""
    abandoned = await submit(runtime)
    await sql_exec(
        runtime,
        "UPDATE public.mcp_runs SET status='starting', worker_id=%s, "
        "lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
        uuid7(),
        abandoned["run_id"],
    )
    queued = await submit(runtime)
    driver = make_driver(runtime, worker_max_inflight=1)
    await driver.tick()
    assert (await get(runtime, abandoned))["status"] == "running"
    assert (await get(runtime, queued))["status"] == "queued"


async def test_one_shot_result_finishes_in_the_same_tick(runtime):
    """A channel command carries its answer in the ref: no poll, no waiting for a tick."""
    row = await submit(runtime)
    adapter = FakeAdapter()
    adapter.start_results.append(
        ExternalRef("fr_command", "project-1", result={"id": "project-1", "status": "active"})
    )
    driver = make_driver(runtime, adapter)
    await driver.tick()
    saved = await get(runtime, row)
    assert saved["status"] == "succeeded"
    assert saved["result"] == {"id": "project-1", "status": "active"}


async def test_a_slow_start_keeps_its_lease(runtime):
    """A start may take longer than the lease (300 s client timeout, 30 s lease)."""
    row = await submit(runtime)
    adapter = FakeAdapter()
    adapter.start_delay = 11
    driver = make_driver(runtime, adapter, lease_seconds=10, heartbeat_seconds=1)
    await driver.tick()
    saved = await get(runtime, row)
    assert saved["status"] == "running" and saved["external_ref"]["id"]


async def test_a_slow_poll_keeps_its_lease_and_applies_the_result(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter, lease_seconds=10, heartbeat_seconds=1)
    await driver.tick()
    adapter.poll_delay = 11
    adapter.polls[row["run_id"]].append(ExternalState("succeeded", result={"done": True}))
    await driver.tick()
    saved = await get(runtime, row)
    assert saved["status"] == "succeeded" and saved["result"] == {"done": True}


async def test_a_fr_command_run_is_accepted_and_completes(runtime):
    """The widened runtime CHECK admits fr_command and the one-shot result is stored."""
    row = await submit(
        runtime,
        runtime="fr_command",
        target=FrCommandTarget(kind="fr_command", command="projects.get"),
    )
    adapter = FakeAdapter("fr")
    adapter.start_results.append(
        ExternalRef("fr_command", "project-1", project_id="project-1", result={"id": "project-1"})
    )
    driver = ExternalDriver(
        runtime.repository,
        TaskInputRepository(runtime.database.pool),
        {"fr_command": adapter},
        runtime.config,
        uuid7(),
    )
    await driver.tick()
    saved = await get(runtime, row)
    assert saved["status"] == "succeeded" and saved["result"] == {"id": "project-1"}


def test_every_external_runtime_has_an_adapter():
    client = httpx.AsyncClient()
    try:
        adapters = build_adapters(client, settings())
        assert set(adapters) == {"n8n_workflow", "fr_project", "fr_command"}
        assert isinstance(adapters["fr_command"], FrCommandAdapter)
    finally:
        asyncio.run(client.aclose())


async def test_answered_inputs_forwarded_once(runtime):
    row = await submit_fr(runtime)
    fr = FakeAdapter("fr")
    driver = make_driver(runtime, fr=fr)
    await driver.tick()
    inputs = TaskInputRepository(runtime.database.pool)
    key = await inputs.open(TENANT_A, row["run_id"], "approval-1", {"q": 1})
    assert key
    assert await inputs.answer(
        TENANT_A, row["run_id"], {key: {"action": "accept", "content": {}}}, {"sub": "u"}
    )
    fr.forward_error = httpx.ConnectError("down")
    await driver.tick()
    assert (await inputs.open_for(TENANT_A, row["run_id"]))[0]["state"] == "answered"
    fr.forward_error = None
    await driver.tick()
    await driver.tick()
    assert (await inputs.open_for(TENANT_A, row["run_id"]))[0]["state"] == "forwarded"
    assert fr.count("forward") == 2  # one failed attempt, one delivery; none after


async def test_answers_for_terminal_runs_are_dropped_not_forwarded(runtime):
    row = await submit_fr(runtime)
    fr = FakeAdapter("fr")
    driver = make_driver(runtime, fr=fr)
    await driver.tick()
    inputs = TaskInputRepository(runtime.database.pool)
    key = await inputs.open(TENANT_A, row["run_id"], "approval-1", {"q": 1})
    assert key
    await inputs.answer(TENANT_A, row["run_id"], {key: {"action": "decline"}}, {"sub": "u"})
    fr.polls[row["run_id"]].append(ExternalState("succeeded", result={}))
    await driver.tick()
    assert (await get(runtime, row))["status"] == "succeeded"
    assert fr.count("forward") == 0
    assert (await inputs.open_for(TENANT_A, row["run_id"]))[0]["state"] == "forwarded"


async def test_kata_runs_untouched(runtime):
    queued = await enqueue(runtime)
    claimed = await claim(runtime)
    await expire_lease(runtime, claimed)
    external = await submit(runtime)
    adapter = FakeAdapter()
    await make_driver(runtime, adapter).tick()
    assert (await get(runtime, queued))["status"] in {"queued", "starting"}
    kata = await get(runtime, claimed)
    assert kata["status"] == "starting" and kata["worker_id"] == claimed["worker_id"]
    assert kata["fence"] == claimed["fence"]
    assert all(call[1] == external["run_id"] for call in adapter.calls)
    async with runtime.database.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT run_id FROM mcp_execution_slots WHERE run_id IS NOT NULL ORDER BY slot_id"
        )
        held = {r["run_id"] for r in await cur.fetchall()}
    assert external["run_id"] not in held and claimed["run_id"] in held


async def test_driver_runs_as_worker_runtime_role(runtime, database_url):
    parsed = make_url(database_url)
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    role = "external_driver_" + uuid7().hex
    password = "synthetic-role-only"  # pragma: allowlist secret
    with psycopg.connect(dsn) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        grant_execution(conn, role, worker=True)
    worker_db = Database(
        runtime.config.model_copy(
            update={
                "database_url": SecretStr(
                    parsed.set(username=role, password=password).render_as_string(
                        hide_password=False
                    )
                )
            }
        )
    )
    try:
        await worker_db.open()
        plain, fr_run = await submit(runtime), await submit_fr(runtime)
        n8n, fr = FakeAdapter("n8n"), FakeAdapter("fr")
        driver = ExternalDriver(
            ExecutionRepository(worker_db.pool, runtime.config),
            TaskInputRepository(worker_db.pool),
            {"n8n_workflow": n8n, "fr_project": fr},
            runtime.config,
            uuid7(),
        )
        await driver.tick()
        inputs = TaskInputRepository(runtime.database.pool)
        key = await inputs.open(TENANT_A, fr_run["run_id"], "approval-1", {"q": 1})
        assert key
        await inputs.answer(
            TENANT_A, fr_run["run_id"], {key: {"action": "accept", "content": {}}}, {"sub": "u"}
        )
        n8n.polls[plain["run_id"]].append(ExternalState("running", message="working"))
        await driver.tick()
        n8n.polls[plain["run_id"]].append(ExternalState("succeeded", result={"ok": True}))
        fr.polls[fr_run["run_id"]].append(ExternalState("cancelled"))
        await driver.tick()
        done = await get(runtime, plain)
        assert done["status"] == "succeeded" and done["status_message"] == "working"
        assert (await get(runtime, fr_run))["status"] == "cancelled"
        assert fr.count("forward") == 1
        assert (await inputs.open_for(TENANT_A, fr_run["run_id"]))[0]["state"] == "forwarded"
    finally:
        await worker_db.close()
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_refused_answer_reopens_input_with_message(runtime):
    row = await submit_fr(runtime)
    fr = FakeAdapter("fr")
    driver = make_driver(runtime, fr=fr)
    await driver.tick()
    inputs = TaskInputRepository(runtime.database.pool)
    key = await inputs.open(TENANT_A, row["run_id"], "approval-1", {"q": 1})
    assert key
    await inputs.answer(TENANT_A, row["run_id"], {key: ACCEPT}, {"channel": "mcp", "address": "a"})
    fr.forward_result = "actor_unresolved"
    await driver.tick()
    (item,) = await inputs.open_for(TENANT_A, row["run_id"])
    assert item["state"] == "open" and item["response"] is None
    assert (await get(runtime, row))["status_message"] == "fr refused the answer: actor_unresolved"
    # A progress message from the next poll does not hide the refusal.
    fr.polls[row["run_id"]].append(ExternalState("running", message="active"))
    await driver.tick()
    assert (await get(runtime, row))["status_message"] == "fr refused the answer: actor_unresolved"
    # The human answers again and fr takes it this time; the valid answer clears the hint.
    fr.forward_result = None
    await inputs.answer(TENANT_A, row["run_id"], {key: ACCEPT}, {"channel": "mcp", "address": "a"})
    assert (await get(runtime, row))["status_message"] is None
    await driver.tick()
    assert (await inputs.open_for(TENANT_A, row["run_id"]))[0]["state"] == "forwarded"
    assert fr.count("forward") == 2


async def test_n8n_run_answers_go_through_fr_adapter(runtime):
    row = await submit(runtime)  # n8n_workflow runtime
    n8n, fr = FakeAdapter("n8n"), FakeAdapter("fr")
    driver = make_driver(runtime, n8n=n8n, fr=fr)
    await driver.tick()
    inputs = TaskInputRepository(runtime.database.pool)
    key = await inputs.open(TENANT_A, row["run_id"], "approval-n8n", {"q": 1})
    assert key
    await inputs.answer(TENANT_A, row["run_id"], {key: ACCEPT}, {"channel": "mcp", "address": "a"})
    await driver.tick()
    assert fr.count("forward") == 1 and n8n.count("forward") == 0
    assert (await inputs.open_for(TENANT_A, row["run_id"]))[0]["state"] == "forwarded"


async def test_start_merges_into_external_ref_written_by_an_event(runtime):
    row = await submit_fr(runtime)
    fr = FakeAdapter("fr")
    started = asyncio.Event()
    proceed = asyncio.Event()
    original = fr.start

    async def slow_start(r):
        ref = await original(r)
        started.set()
        await proceed.wait()
        return ref

    fr.start = slow_start
    driver = make_driver(runtime, fr=fr)
    ticking = asyncio.create_task(driver.tick())
    await started.wait()
    # An fr event lands while the start call is in flight.
    await sql_exec(
        runtime,
        "UPDATE mcp_runs SET external_ref=%s WHERE run_id=%s",
        Jsonb({"kind": "fr", "project_id": "proj-9"}),
        row["run_id"],
    )
    proceed.set()
    await ticking
    run = await get(runtime, row)
    assert run["status"] == "running"
    assert run["external_ref"] == {
        "kind": "fr",
        "id": f"ext-{row['run_id']}",
        "project_id": "proj-9",
    }


async def feed(runtime, row):
    async with runtime.database.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT kind,text,severity,source_ref FROM mcp_run_activity "
            "WHERE run_id=%s ORDER BY seq",
            (row["run_id"],),
        )
        return [dict(entry) for entry in await cur.fetchall()]


async def test_feed_records_the_start_and_each_new_phase_once(runtime):
    row = await submit(runtime)
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    assert [(e["kind"], e["text"], e["severity"]) for e in await feed(runtime, row)] == [
        ("started", "The run has started.", "low")
    ]
    adapter.polls[row["run_id"]].append(ExternalState("running", message="planning"))
    await driver.tick()
    await driver.tick()  # same phase: no second row
    assert [e["text"] for e in await feed(runtime, row)] == ["The run has started.", "planning"]
    adapter.polls[row["run_id"]].append(ExternalState("running", message="build"))
    await driver.tick()
    assert [e["kind"] for e in await feed(runtime, row)] == ["started", "phase", "phase"]


async def test_feed_records_delivery_failure_and_cancellation(runtime):
    delivered, failed, cancelled = (
        await submit(runtime),
        await submit(runtime),
        await submit(runtime),
    )
    adapter = FakeAdapter()
    driver = make_driver(runtime, adapter)
    await driver.tick()
    adapter.polls[delivered["run_id"]].append(ExternalState("succeeded", result={"ok": 1}))
    adapter.polls[failed["run_id"]].append(ExternalState("failed", code="project_failed"))
    adapter.polls[cancelled["run_id"]].append(ExternalState("cancelled"))
    await driver.tick()
    assert [e["kind"] for e in await feed(runtime, delivered)][-1] == "delivered"
    (failure,) = [e for e in await feed(runtime, failed) if e["kind"] == "failed"]
    assert (failure["text"], failure["severity"]) == ("project_failed", "critical")
    (refusal,) = [e for e in await feed(runtime, cancelled) if e["kind"] == "refused"]
    assert (refusal["text"], refusal["severity"]) == ("cancelled", "medium")


async def test_a_standing_hint_does_not_flood_the_feed(runtime):
    """The answer-again hint wins the status message, so the phase update is refused: a refused
    update records nothing, however many ticks repeat it."""
    row = await submit_fr(runtime)
    fr = FakeAdapter("fr")
    driver = make_driver(runtime, fr=fr)
    await driver.tick()
    inputs = TaskInputRepository(runtime.database.pool)
    key = await inputs.open(TENANT_A, row["run_id"], "approval-1", {"q": 1})
    await inputs.answer(TENANT_A, row["run_id"], {key: ACCEPT}, {"channel": "mcp", "address": "a"})
    fr.forward_result = "actor_unresolved"
    await driver.tick()
    fr.forward_result = None
    before = [e for e in await feed(runtime, row) if e["kind"] == "phase"]
    for _ in range(3):
        fr.polls[row["run_id"]].append(ExternalState("running", message="planning"))
        await driver.tick()
    after = [e for e in await feed(runtime, row) if e["kind"] == "phase"]
    assert after == before == []
