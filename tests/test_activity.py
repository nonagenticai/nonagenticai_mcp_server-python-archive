"""The run activity feed against disposable PostgreSQL: dedupe, ordering, the fr metadata block."""

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

import pytest
from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from src.catalog.models import FrProjectTarget
from src.tasks.activity import ACTIVITY_LIMIT, TEXT_LIMIT, fr_meta, recent, record, wire
from src.tasks.models import FR_META_KEY
from tests.fixtures import TENANT_A, TENANT_B, external_tool
from tests.test_execution import enqueue, setup_runtime

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
TWO_HOURS_AGO = NOW - timedelta(hours=2)


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database)


async def fetch(runtime, query, *params):
    async with runtime.database.pool.connection() as conn:
        cur = await conn.execute(query, params)
        return await cur.fetchall() if cur.description else []


async def run_id(runtime):
    return (await enqueue(runtime))["run_id"]


async def fr_run(runtime, project="proj-1"):
    tool = external_tool(
        runtime="fr_project", target=FrProjectTarget(kind="fr_project", project_type="demo")
    )
    resolved = dataclasses.replace(runtime.resolved, manifest_json=tool.model_dump_json())
    row = await runtime.repository.accept(
        runtime.principal, resolved, {}, None, await runtime.catalog.repository.generation()
    )
    reference = {"kind": "fr", "id": "start-1"}
    if project is not None:
        reference["project_id"] = project
    await fetch(
        runtime,
        "UPDATE mcp_runs SET external_ref=%s WHERE run_id=%s",
        Jsonb(reference),
        row["run_id"],
    )
    return row["run_id"]


async def test_record_appends_and_returns_a_sequence(runtime):
    run = await run_id(runtime)
    pool = runtime.database.pool
    first = await record(pool, TENANT_A, run, "started", "started")
    second = await record(pool, TENANT_A, run, "phase", "planning")
    assert isinstance(first, int) and isinstance(second, int) and second > first
    rows = await recent(pool, TENANT_A, run)
    assert [(r["seq"], r["kind"], r["text"]) for r in rows] == [
        (first, "started", "started"),
        (second, "phase", "planning"),
    ]
    assert rows[0]["severity"] == "low" and rows[1]["severity"] == "low"
    assert rows[0]["created_at"] is not None


async def test_record_skips_a_repeat_of_the_newest_row(runtime):
    run = await run_id(runtime)
    pool = runtime.database.pool
    assert await record(pool, TENANT_A, run, "phase", "planning") is not None
    assert await record(pool, TENANT_A, run, "phase", "planning") is None
    # Same text, another kind is a different fact.
    assert await record(pool, TENANT_A, run, "refused", "planning") is not None
    # A repeat of a row that is no longer the newest is a real event (the phase came back).
    assert await record(pool, TENANT_A, run, "phase", "planning") is not None
    assert len(await recent(pool, TENANT_A, run)) == 3


async def test_two_approvals_with_one_description_are_two_rows(runtime):
    """The dedupe is about a REPEATED POLL, not about equal text from different sources.

    Two approvals can legitimately carry the same description ("Confirm the brief?"); keying the
    dedupe on kind+text alone silently dropped the second, and with it its `approval_waiting`
    signal.
    """
    run = await run_id(runtime)
    pool = runtime.database.pool
    assert await record(pool, TENANT_A, run, "approval_requested", "Confirm?", source_ref="ap-1")
    assert await record(pool, TENANT_A, run, "approval_requested", "Confirm?", source_ref="ap-2")
    # The same source again is still the same fact.
    assert (
        await record(pool, TENANT_A, run, "approval_requested", "Confirm?", source_ref="ap-2")
        is None
    )
    assert len(await recent(pool, TENANT_A, run)) == 2


async def test_record_is_scoped_to_the_tenant_and_an_existing_run(runtime):
    run = await run_id(runtime)
    pool = runtime.database.pool
    assert await record(pool, TENANT_B, run, "phase", "x") is None
    assert await record(pool, TENANT_A, uuid4(), "phase", "x") is None
    assert await recent(pool, TENANT_A, run) == []
    assert await recent(pool, TENANT_B, run) == []


async def test_record_normalises_and_bounds_the_text(runtime):
    run = await run_id(runtime)
    pool = runtime.database.pool
    await record(pool, TENANT_A, run, "phase", "  two\nlines\tand  spaces  ")
    await record(pool, TENANT_A, run, "failed", "x" * 5000)
    await record(pool, TENANT_A, run, "started", "")
    rows = await recent(pool, TENANT_A, run)
    assert rows[0]["text"] == "two lines and spaces"
    assert len(rows[1]["text"]) == TEXT_LIMIT
    assert rows[2]["text"] == "started"  # an empty text falls back to the kind


async def test_explicit_severity_and_source_ref_are_stored(runtime):
    run = await run_id(runtime)
    pool = runtime.database.pool
    await record(pool, TENANT_A, run, "phase", "p", severity="critical", source_ref="ap-1")
    (row,) = await recent(pool, TENANT_A, run)
    assert (row["severity"], row["source_ref"]) == ("critical", "ap-1")


async def test_recent_is_newest_last_and_limited(runtime):
    run = await run_id(runtime)
    pool = runtime.database.pool
    for number in range(5):
        await record(pool, TENANT_A, run, "phase", f"p{number}")
    rows = await recent(pool, TENANT_A, run, limit=3)
    assert [r["text"] for r in rows] == ["p2", "p3", "p4"]  # the newest three, oldest first
    assert ACTIVITY_LIMIT >= 50


async def test_wire_shape_hides_tenant_and_source_ref(runtime):
    run = await run_id(runtime)
    pool = runtime.database.pool
    await record(pool, TENANT_A, run, "approval_requested", "Approve?", source_ref="ap-1")
    rows = await recent(pool, TENANT_A, run)
    (entry,) = wire(rows)
    assert set(entry) == {"seq", "at", "kind", "text", "severity"}
    assert entry["kind"] == "approval_requested" and entry["severity"] == "high"
    assert datetime.fromisoformat(entry["at"]).utcoffset() == timedelta(0)


async def test_fr_meta_is_only_for_fr_runs(runtime):
    kata = await run_id(runtime)
    row = await runtime.repository.get(TENANT_A, kata)
    assert await fr_meta(runtime.database.pool, TENANT_A, row, runtime.config) is None


async def test_fr_meta_carries_project_activity_and_signals(runtime):
    run = await fr_run(runtime)
    pool = runtime.database.pool
    await record(pool, TENANT_A, run, "started", "started")
    await record(pool, TENANT_A, run, "phase", "build")
    row = await runtime.repository.get(TENANT_A, run)
    meta = await fr_meta(pool, TENANT_A, row, runtime.config)
    assert set(meta) == {"project_id", "activity", "signals"}
    assert meta["project_id"] == "proj-1"
    assert [entry["kind"] for entry in meta["activity"]] == ["started", "phase"]
    assert [entry["text"] for entry in meta["activity"]] == ["started", "build"]
    assert meta["signals"] == []
    assert FR_META_KEY == "ai.nonagentic/fr"


async def test_fr_meta_project_id_is_null_until_fr_reports_it(runtime):
    started = await fr_run(runtime, project=None)
    row = await runtime.repository.get(TENANT_A, started)
    meta = await fr_meta(runtime.database.pool, TENANT_A, row, runtime.config)
    assert meta == {"project_id": None, "activity": [], "signals": []}


async def test_fr_meta_signal_uses_the_rows_and_the_threshold(runtime):
    run = await fr_run(runtime)
    pool = runtime.database.pool
    await record(pool, TENANT_A, run, "phase", "build")
    await fetch(
        runtime,
        "UPDATE mcp_run_activity SET created_at=%s WHERE run_id=%s",
        TWO_HOURS_AGO,
        run,
    )
    row = await runtime.repository.get(TENANT_A, run)
    meta = await fr_meta(pool, TENANT_A, row, runtime.config, now=NOW)
    (signal,) = meta["signals"]
    assert signal["code"] == "phase_stalled" and signal["severity"] == "medium"


async def test_a_waiting_approval_older_than_the_wire_window_still_signals(runtime):
    """A busy project pushes its open approval past ACTIVITY_LIMIT; the signal must not go quiet.

    The wire page stays 50 rows, but signals read the wider window, so an approval requested
    two hours ago still produces `approval_waiting`.
    """
    run = await fr_run(runtime)
    pool = runtime.database.pool
    await record(pool, TENANT_A, run, "approval_requested", "approve the brief", source_ref="ap-1")
    await fetch(
        runtime,
        "UPDATE mcp_run_activity SET created_at=%s WHERE run_id=%s AND kind='approval_requested'",
        TWO_HOURS_AGO,
        run,
    )
    for n in range(ACTIVITY_LIMIT + 5):
        await record(pool, TENANT_A, run, "phase", f"step {n}")
    row = await runtime.repository.get(TENANT_A, run)
    meta = await fr_meta(pool, TENANT_A, row, runtime.config, now=NOW)
    assert len(meta["activity"]) == ACTIVITY_LIMIT
    assert "approval_waiting" in [signal["code"] for signal in meta["signals"]]


async def test_an_activity_row_requires_an_existing_run(runtime):
    """The feed is a child of mcp_runs: no row can outlive its run id."""
    with pytest.raises(ForeignKeyViolation):
        async with runtime.database.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO public.mcp_run_activity(tenant_id,run_id,kind,text,severity) "
                "VALUES (%s,%s,'phase','x','low')",
                (TENANT_A, uuid7()),
            )
    assert await recent(runtime.database.pool, TENANT_A, uuid7()) == []
