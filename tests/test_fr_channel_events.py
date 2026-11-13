"""fr -> MCP channel events endpoint (spec section 3.3), HMAC-only authentication."""

import dataclasses
import json
import time
from datetime import UTC, datetime
from uuid import uuid4, uuid7

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from scripts.grant_runtime_role import grant
from src.asgi import create_app
from src.catalog.models import FrProjectTarget
from src.external.fr import elicitation_request
from src.external.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign
from tests.fixtures import external_tool, settings
from tests.lifespan import running_app
from tests.test_execution import enqueue, setup_runtime

SECRET = "synthetic-fr-channel-secret"  # pragma: allowlist secret
PATH = "/api/channels/fr/events"
TERMINAL = ("succeeded", "failed", "cancelled", "timed_out", "interrupted")


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database)


@pytest.fixture
async def api(database_url, runtime):
    role = "fr_channel_" + uuid7().hex
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    password = "synthetic-role-only"  # pragma: allowlist secret
    with psycopg.connect(dsn) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    grant(database_url, role)
    url = (
        make_url(database_url)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )
    app = create_app(settings(database_url=url, fr_channel_secret=SecretStr(SECRET)))
    try:
        async with running_app(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://127.0.0.1",
            ) as client:
                yield app, client
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def query(runtime, text, *params):
    async with runtime.database.pool.connection() as conn:
        cur = await conn.execute(text, params)
        return await cur.fetchall() if cur.description else []


async def run_state(runtime, run):
    rows = await query(runtime, "SELECT status,status_message FROM mcp_runs WHERE run_id=%s", run)
    return rows[0]


async def make_run(runtime, kind="n8n_workflow", ref=None, status=None):
    if kind == "fr_project":
        tool = external_tool(
            runtime="fr_project", target=FrProjectTarget(kind="fr_project", project_type="demo")
        )
    else:
        tool = external_tool()
    resolved = dataclasses.replace(runtime.resolved, manifest_json=tool.model_dump_json())
    row = await runtime.repository.accept(
        runtime.principal, resolved, {}, None, await runtime.catalog.repository.generation()
    )
    run = row["run_id"]
    if ref is not None:
        await query(runtime, "UPDATE mcp_runs SET external_ref=%s WHERE run_id=%s", Jsonb(ref), run)
    if status in TERMINAL:
        await query(
            runtime, "UPDATE mcp_runs SET status=%s, finished_at=now() WHERE run_id=%s", status, run
        )
    return run


async def external_run(runtime):
    return await make_run(runtime)


async def fr_run(runtime, ref=None, status=None):
    return await make_run(runtime, "fr_project", ref, status)


def requested(approval="ap-1", *, corr=None, project="p-1", **extra):
    payload = {
        "type": "approval.requested",
        "address": "person-1",
        "approval_id": approval,
        "project_id": project,
        "tenant_id": "fr-tenant",
        "kind": "plan",
        "description": "Approve the plan",
        "change_summary": "Adds a table",
        "allowed_decisions": ["approved", "rejected"],
        "expires_at": "2030-01-01T00:00:00Z",
        **extra,
    }
    if corr is not None:
        payload["correlation_id"] = str(corr)
    return payload


def closed(approval="ap-1", final="approved", project="p-1"):
    return {
        "type": "approval.closed",
        "address": "person-1",
        "approval_id": approval,
        "project_id": project,
        "tenant_id": "fr-tenant",
        "final_status": final,
    }


def message(kind, run, text="", **meta):
    return {
        "type": "message",
        "address": "person-1",
        "kind": kind,
        "text": text,
        "correlation_id": str(run),
        "meta": meta,
    }


def outcome(run, outcome_name, name="projects.create", **result):
    return message(
        "action-result",
        run,
        "done",
        actionId="a1",
        actionName=name,
        outcome=outcome_name,
        result=result,
    )


def body_of(payload):
    return json.dumps(payload, separators=(",", ":")).encode()


async def post(client, payload, *, headers=None, body=None):
    raw = body if body is not None else body_of(payload)
    if headers is None:
        headers = sign(raw, SECRET, int(time.time()))
    return await client.post(PATH, content=raw, headers=headers)


async def inputs_of(runtime, run):
    return await query(
        runtime, "SELECT source_ref,state,request FROM mcp_task_inputs WHERE run_id=%s", run
    )


async def activity_of(runtime, run):
    return await query(
        runtime,
        "SELECT kind,text,severity,source_ref FROM mcp_run_activity WHERE run_id=%s ORDER BY seq",
        run,
    )


async def ref_of(runtime, run):
    return (await query(runtime, "SELECT external_ref FROM mcp_runs WHERE run_id=%s", run))[0][
        "external_ref"
    ]


async def test_events_unsigned_401(api, runtime):
    _, client = api
    run = await external_run(runtime)
    response = await client.post(PATH, content=body_of(requested(corr=run)))
    assert response.status_code == 401 and response.json() == {"error": "invalid_signature"}
    assert await inputs_of(runtime, run) == []


async def test_bad_signature_401(api, runtime):
    _, client = api
    run = await external_run(runtime)
    raw = body_of(requested(corr=run))
    headers = sign(raw, "wrong-secret", int(time.time()))
    assert (await post(client, None, headers=headers, body=raw)).status_code == 401
    headers = sign(raw, SECRET, int(time.time()))
    assert (await post(client, None, headers=headers, body=raw + b" ")).status_code == 401
    assert await inputs_of(runtime, run) == []


async def test_stale_timestamp_401(api, runtime):
    _, client = api
    run = await external_run(runtime)
    raw = body_of(requested(corr=run))
    headers = sign(raw, SECRET, int(time.time()) - 301)
    response = await post(client, None, headers=headers, body=raw)
    assert response.status_code == 401
    assert TIMESTAMP_HEADER in headers and SIGNATURE_HEADER in headers
    assert await inputs_of(runtime, run) == []


async def test_secret_unset_503(api, runtime):
    app, client = api
    app.state.settings = app.state.settings.model_copy(update={"fr_channel_secret": None})
    run = await external_run(runtime)
    response = await post(client, requested(corr=run))
    assert response.status_code == 503 and response.json() == {"error": "channel_unconfigured"}


async def test_oversize_413(api, runtime):
    _, client = api
    raw = b'{"type":"x","pad":"' + b"a" * 70000 + b'"}'
    response = await post(client, None, body=raw)
    assert response.status_code == 413
    # Chunked upload without Content-Length is capped too.

    async def chunks():
        for _ in range(70):
            yield b"a" * 1000

    response = await client.post(
        PATH, content=chunks(), headers=sign(b"", SECRET, int(time.time()))
    )
    assert response.status_code == 413


async def send(client, payload):
    response = await post(client, payload)
    assert response.status_code == 204 and response.content == b""


async def test_requested_by_correlation_opens_input_with_spec_schema(api, runtime):
    _, client = api
    for run in (await external_run(runtime), await fr_run(runtime)):
        await send(client, requested(corr=run))
        rows = await inputs_of(runtime, run)
        assert len(rows) == 1 and rows[0]["source_ref"] == "ap-1" and rows[0]["state"] == "open"
        assert rows[0]["request"] == elicitation_request("Approve the plan", "Adds a table")
        assert rows[0]["request"]["params"]["message"] == "Approve the plan\n\nAdds a table"


async def test_requested_replay_creates_one_input(api, runtime):
    _, client = api
    run = await external_run(runtime)
    for _ in range(3):
        await send(client, requested(corr=run))
    assert len(await inputs_of(runtime, run)) == 1


async def test_requested_routes_by_project_id(api, runtime):
    _, client = api
    target = await fr_run(runtime, {"kind": "fr", "id": "start-1", "project_id": "p-1"})
    other = await fr_run(runtime, {"kind": "fr", "id": "start-2", "project_id": "p-2"})
    await send(client, requested("ap-1", project="p-1"))
    assert [r["source_ref"] for r in await inputs_of(runtime, target)] == ["ap-1"]
    assert await inputs_of(runtime, other) == []


async def test_requested_routes_by_approval_id_when_project_null(api, runtime):
    _, client = api
    target = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    other = await fr_run(runtime, {"kind": "fr", "id": "start-2"})
    await send(client, requested("start-1", project=None))
    assert [r["source_ref"] for r in await inputs_of(runtime, target)] == ["start-1"]
    assert await inputs_of(runtime, other) == []


async def test_same_approval_to_many_addresses_one_input(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "s", "project_id": "p-1"})
    for address in ("a", "b", "c"):
        await send(client, requested(address=address))
    assert len(await inputs_of(runtime, run)) == 1


async def test_terminal_and_kata_runs_ignored(api, runtime):
    _, client = api
    done = await fr_run(runtime, {"kind": "fr", "id": "s", "project_id": "p-1"}, "failed")
    kata = (await enqueue(runtime))["run_id"]
    await send(client, requested())
    await send(client, requested("ap-2", corr=kata, project=None))
    await send(client, requested("ap-3", corr=done, project=None))
    assert await inputs_of(runtime, done) == [] and await inputs_of(runtime, kata) == []


async def test_unknown_ids_204_no_input(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "s", "project_id": "p-1"})
    await send(client, requested("ap-9", corr=uuid4()))
    await send(client, requested("ap-9", project="nope"))
    await send(client, requested("ap-9", project=None))
    await send(client, {"type": "approval.requested", "address": "x"})
    assert await inputs_of(runtime, run) == []


async def test_closed_closes_everywhere_and_tombstones(api, runtime):
    _, client = api
    first = await fr_run(runtime, {"kind": "fr", "id": "s1", "project_id": "p-1"})
    second = await external_run(runtime)
    await send(client, requested(corr=second))
    await send(client, requested())
    await send(client, closed())
    for run in (first, second):
        assert [r["state"] for r in await inputs_of(runtime, run)] == ["closed"]
    await send(client, requested(corr=second))
    await send(client, requested())
    for run in (first, second):
        assert [r["state"] for r in await inputs_of(runtime, run)] == ["closed"]


async def test_closed_before_requested_leaves_tombstone(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "s1", "project_id": "p-1"})
    await send(client, closed(final="expired"))
    await send(client, requested())
    assert [r["state"] for r in await inputs_of(runtime, run)] == ["closed"]


@pytest.mark.parametrize("final", ["rejected", "expired", "voided"])
async def test_closed_start_approval_records_start_outcome(api, runtime, final):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    await send(client, closed("start-1", final, project=None))
    assert await ref_of(runtime, run) == {"kind": "fr", "id": "start-1", "start_outcome": final}


async def test_closed_approved_start_records_nothing(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    await send(client, closed("start-1", "approved", project=None))
    assert await ref_of(runtime, run) == {"kind": "fr", "id": "start-1"}


async def test_closed_rejected_non_start_records_nothing(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1", "project_id": "p-1"})
    await send(client, closed("plan-1", "rejected"))
    await send(client, closed("start-1", "rejected"))
    assert "start_outcome" not in await ref_of(runtime, run)


async def test_message_approval_request_opens_input(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    await send(
        client,
        message(
            "approval-request",
            run,
            "Start the project?",
            approvalId="start-1",
            address="x",
            actionId="a1",
        ),
    )
    rows = await inputs_of(runtime, run)
    assert [(r["source_ref"], r["state"]) for r in rows] == [("start-1", "open")]
    assert rows[0]["request"] == elicitation_request("Start the project?")


async def test_message_poll_correlation_ignored(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    payload = message("approval-request", run, "Q", approvalId="start-1")
    payload["correlation_id"] = f"{run}:poll"
    await send(client, payload)
    payload = message("text", run, "hello")
    payload["correlation_id"] = f"{run}:poll"
    await send(client, payload)
    assert await inputs_of(runtime, run) == []
    assert (await run_state(runtime, run))["status_message"] is None


async def test_a_feed_failure_still_opens_the_input(api, runtime, monkeypatch):
    """The activity feed is advisory. fr does not retry, and the event fans out to every linked
    run, so a feed error must not 500 the event or abort the remaining runs' inputs."""
    from src.routers import fr_channel

    async def boom(*args, **kwargs):
        raise RuntimeError("the feed is down")

    monkeypatch.setattr(fr_channel, "record_activity", boom)
    _, client = api
    run = await fr_run(runtime)
    await send(client, requested(corr=run))
    rows = await inputs_of(runtime, run)
    assert len(rows) == 1 and rows[0]["state"] == "open"


async def test_self_echo_messages_are_not_logged_at_info(api, runtime, caplog):
    """fr echoes every command we dispatch as a message whose correlation_id is "<run>:<suffix>".

    ``_correlated`` already refuses a non-UUID correlation, so the short-circuit's real effect is
    the logging: without it every echo wrote a ``run=False`` INFO line — live that was 12,172
    lines in 72 h. The test asserts the line is not written, which IS what the guard adds.
    """
    import logging

    from src.routers import fr_channel

    request = object()  # the guard returns before the request is touched
    with caplog.at_level(logging.INFO, logger="src.routers.fr_channel"):
        for suffix in ("poll", "abc123", "cancel"):
            await fr_channel._message(
                request, {"correlation_id": f"0199e5a0-0000-7000-8000-000000000000:{suffix}"}
            )
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []
    await fr_channel._message(request, {"correlation_id": "not-a-run-id", "kind": "text"})


async def test_requested_adopts_the_project_id_the_approval_names(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    # fr correlates the conveyor's own approvals by run id (F8); the project id rides along.
    await send(client, requested("ap-1", corr=run, project="p-7"))
    assert await ref_of(runtime, run) == {
        "kind": "fr",
        "id": "start-1",
        "project_id": "p-7",
    }
    assert [r["source_ref"] for r in await inputs_of(runtime, run)] == ["ap-1"]


async def test_requested_does_not_overwrite_a_known_project_id(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1", "project_id": "p-1"})
    await send(client, requested("ap-1", corr=run, project="p-2"))
    assert (await ref_of(runtime, run))["project_id"] == "p-1"


async def test_requested_records_one_activity_row_per_run(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    for address in ("a", "b", "c"):
        await send(client, requested("ap-1", corr=run, address=address))
    assert await activity_of(runtime, run) == [
        {
            "kind": "approval_requested",
            "text": "Approve the plan",
            "severity": "high",
            "source_ref": "ap-1",
        }
    ]


async def test_requested_activity_text_is_bounded(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    await send(client, requested(corr=run, description="d" * 900))
    (row,) = await activity_of(runtime, run)
    assert len(row["text"]) == 512


async def test_expired_approval_opens_no_input_and_records_nothing(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1", "project_id": "p-1"})
    await send(client, requested("ap-1", expires_at="2020-01-01T00:00:00Z"))
    assert await inputs_of(runtime, run) == []
    assert await activity_of(runtime, run) == []


async def test_approval_expiry_is_stored_on_the_input(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    await send(client, requested("ap-1", corr=run, expires_at="2030-01-01T00:00:00Z"))
    rows = await query(
        runtime,
        "SELECT expires_at FROM mcp_task_inputs WHERE run_id=%s",
        run,
    )
    assert rows[0]["expires_at"] == datetime(2030, 1, 1, tzinfo=UTC)


async def test_unparsable_expiry_is_treated_as_no_expiry(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    await send(client, requested("ap-1", corr=run, expires_at="whenever"))
    assert [r["state"] for r in await inputs_of(runtime, run)] == ["open"]


async def test_closed_records_an_activity_row(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "s1", "project_id": "p-1"})
    await send(client, requested("ap-1"))
    await send(client, closed("ap-1", "rejected"))
    assert [row["kind"] for row in await activity_of(runtime, run)] == [
        "approval_requested",
        "approval_closed",
    ]
    assert (await activity_of(runtime, run))[1] == {
        "kind": "approval_closed",
        "text": "approval closed: rejected",
        "severity": "info",
        "source_ref": "ap-1",
    }


async def test_action_result_approved_records_project_id(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "start-1"})
    await send(client, outcome(run, "approved", project_id="p-9", replayed=False))
    assert await ref_of(runtime, run) == {"kind": "fr", "id": "start-1", "project_id": "p-9"}


async def test_action_result_approved_with_null_ref(api, runtime):
    _, client = api
    run = await fr_run(runtime)
    await send(client, outcome(run, "approved", project_id="p-9"))
    assert await ref_of(runtime, run) == {"kind": "fr", "project_id": "p-9"}


async def test_action_result_rejected_and_error(api, runtime):
    _, client = api
    rejected = await fr_run(runtime, {"kind": "fr", "id": "s"})
    failed = await fr_run(runtime, {"kind": "fr", "id": "s"})
    await send(client, outcome(rejected, "rejected"))
    await send(
        client,
        message(
            "action-error", failed, "boom", actionId="a", actionName="projects.create", error="x"
        ),
    )
    assert (await ref_of(runtime, rejected))["start_outcome"] == "rejected"
    assert (await ref_of(runtime, failed))["start_outcome"] == "failed"


async def test_first_report_wins(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "s"})
    await send(client, outcome(run, "approved", project_id="p-1"))
    await send(client, outcome(run, "approved", project_id="p-2"))
    await send(client, outcome(run, "rejected"))
    assert await ref_of(runtime, run) == {"kind": "fr", "id": "s", "project_id": "p-1"}
    other = await fr_run(runtime, {"kind": "fr", "id": "s"})
    await send(client, outcome(other, "rejected"))
    await send(client, outcome(other, "approved", project_id="p-2"))
    assert (await ref_of(runtime, other)).get("project_id") is None
    assert (await ref_of(runtime, other))["start_outcome"] == "rejected"


async def test_other_action_name_ignored(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "s"})
    await send(client, outcome(run, "approved", name="tasks.create", project_id="p-1"))
    await send(client, message("action-error", run, "x", actionName="other", error="e"))
    assert await ref_of(runtime, run) == {"kind": "fr", "id": "s"}


async def test_start_outcome_ignored_for_n8n_run(api, runtime):
    _, client = api
    run = await external_run(runtime)
    await send(client, outcome(run, "approved", project_id="p-1"))
    assert await ref_of(runtime, run) is None


async def test_text_and_report_set_status_message_bounded(api, runtime):
    _, client = api
    run = await external_run(runtime)
    await send(client, message("text", run, "implementing"))
    assert (await run_state(runtime, run))["status_message"] == "implementing"
    await send(client, message("report", run, "m" * 600))
    assert (await run_state(runtime, run))["status_message"] == "m" * 512


async def test_message_on_terminal_run_is_noop(api, runtime):
    _, client = api
    run = await fr_run(runtime, {"kind": "fr", "id": "s"}, "failed")
    await send(client, message("text", run, "late"))
    await send(client, outcome(run, "approved", project_id="p-1"))
    assert (await run_state(runtime, run))["status_message"] is None
    assert await ref_of(runtime, run) == {"kind": "fr", "id": "s"}


async def test_kata_run_message_ignored(api, runtime):
    _, client = api
    kata = (await enqueue(runtime))["run_id"]
    await send(client, message("text", kata, "x"))
    assert (await run_state(runtime, kata))["status_message"] is None


async def test_project_status_ignored(api, runtime):
    _, client = api
    run = await external_run(runtime)
    await send(client, {"type": "project.status", "correlation_id": str(run), "phase": "x"})
    assert (await run_state(runtime, run))["status_message"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "mystery"},
        {"type": "approval.requested"},
        {"type": "approval.closed", "approval_id": 5},
        {"type": "message", "correlation_id": "nope"},
        {"type": "message", "kind": "text", "correlation_id": str(uuid4()), "meta": 3},
        [1, 2],
    ],
)
async def test_malformed_or_unknown_events_204(api, payload):
    _, client = api
    assert (await post(client, payload)).status_code == 204
    assert (await post(client, None, body=b"not json")).status_code == 204


def test_app_logging_makes_src_info_visible():
    import logging

    from src.asgi import configure_app_logging

    configure_app_logging()
    src = logging.getLogger("src")
    assert src.handlers and src.getEffectiveLevel() <= logging.INFO
    assert logging.getLogger("src.routers.fr_channel").isEnabledFor(logging.INFO)
