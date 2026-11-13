"""Pure run -> task mapping (no database)."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from mcp_types import CallToolResult, InputRequests
from pydantic import SecretStr, TypeAdapter

from src.config import Settings
from src.execution.models import envelope
from src.tasks.mapping import create_task_result, task_result, task_status, task_view
from src.tasks.models import TASKS_EXTENSION_ID

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SETTINGS = Settings(database_url=SecretStr("postgresql://unused@localhost/unused"))


def run_row(status="queued", **over):
    row = {
        "run_id": uuid7(),
        "status": status,
        "result": None,
        "error": None,
        "created_at": T0,
        "started_at": None,
        "finished_at": None,
        "payload_expired_at": None,
        "cancel_requested": False,
        "runtime": "kata_python",
        "status_message": None,
    }
    row.update(over)
    return row


def an_input(key="k1", state="open", created=T0, answered=None):
    return {
        "input_key": key,
        "run_id": None,
        "source_ref": "ref-" + key,
        "request": {
            "method": "elicitation/create",
            "params": {
                "message": "Approve?",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"decision": {"type": "string"}},
                },
            },
        },
        "state": state,
        "created_at": created,
        "answered_at": answered,
    }


def test_extension_id():
    assert TASKS_EXTENSION_ID == "io.modelcontextprotocol/tasks"


@pytest.mark.parametrize(
    ("run_status", "open_inputs", "expected"),
    [
        ("queued", 0, "working"),
        ("starting", 0, "working"),
        ("running", 0, "working"),
        ("cancelling", 0, "working"),
        ("queued", 1, "input_required"),
        ("starting", 2, "input_required"),
        ("running", 1, "input_required"),
        ("cancelling", 1, "working"),
        ("succeeded", 0, "completed"),
        ("succeeded", 1, "completed"),
        ("failed", 0, "completed"),
        ("timed_out", 0, "completed"),
        ("interrupted", 1, "completed"),
        ("cancelled", 0, "cancelled"),
        ("cancelled", 1, "cancelled"),
    ],
)
def test_status_mapping(run_status, open_inputs, expected):
    assert task_status(run_row(run_status), open_inputs) == expected


@pytest.mark.parametrize(
    ("run_status", "is_error"),
    [("succeeded", False), ("failed", True), ("timed_out", True), ("interrupted", True)],
)
def test_completed_result_is_calltoolresult(run_status, is_error):
    row = run_row(run_status, result={"a": 1} if not is_error else None)
    out = task_result(row)
    assert out["structuredContent"] == envelope(row)
    assert out["isError"] is is_error
    assert out["content"] == [
        {"type": "text", "text": json.dumps(envelope(row), ensure_ascii=False, allow_nan=False)}
    ]
    CallToolResult.model_validate(out)


def test_view_completed_carries_result():
    row = run_row(
        "failed", finished_at=T0 + timedelta(seconds=5), error={"code": "x", "message": "m"}
    )
    view = task_view(row, [], SETTINGS)
    assert view["status"] == "completed"
    assert view["result"]["isError"] is True
    assert "inputRequests" not in view


def test_view_working_has_no_result_or_inputs():
    view = task_view(run_row("running"), [], SETTINGS)
    assert view["status"] == "working"
    assert "result" not in view
    assert "inputRequests" not in view
    assert "statusMessage" not in view


def test_view_cancelled_has_no_result():
    assert "result" not in task_view(run_row("cancelled"), [], SETTINGS)


def test_view_input_required_lists_only_open_inputs():
    inputs = [
        an_input("a"),
        an_input("b", state="answered", answered=T0),
        an_input("c", state="closed"),
    ]
    view = task_view(run_row("running"), inputs, SETTINGS)
    assert view["status"] == "input_required"
    assert list(view["inputRequests"]) == ["a"]
    TypeAdapter(InputRequests).validate_python(view["inputRequests"])


def test_view_cancelling_with_open_input_is_working_without_requests():
    view = task_view(run_row("cancelling"), [an_input("a")], SETTINGS)
    assert view["status"] == "working"
    assert "inputRequests" not in view


def test_view_has_no_meta_key():
    for row, inputs in [
        (run_row("running"), [an_input()]),
        (run_row("succeeded"), []),
        (run_row("queued"), []),
    ]:
        assert "_meta" not in task_view(row, inputs, SETTINGS)
        assert "_meta" not in create_task_result(row, SETTINGS)


def test_fr_meta_rides_under_the_meta_key_on_both_shapes():
    meta = {"project_id": "p-1", "activity": [], "signals": []}
    view = task_view(run_row("running"), [], SETTINGS, meta)
    assert view["_meta"] == {"ai.nonagentic/fr": meta}
    created = create_task_result(run_row("queued"), SETTINGS, meta)
    assert created["_meta"] == {"ai.nonagentic/fr": meta}
    assert created["resultType"] == "task" and view["resultType"] == "complete"
    # The block never becomes a top-level field: rmcp drops unknown top-level task fields.
    assert "project_id" not in view and "activity" not in view


def test_an_expired_input_is_not_an_open_question():
    expired = {**an_input("a"), "expires_at": T0}
    future = {**an_input("b"), "expires_at": T0 + timedelta(days=3650)}
    view = task_view(run_row("running"), [expired, future], SETTINGS)
    assert view["status"] == "input_required"
    assert list(view["inputRequests"]) == ["b"]


def test_a_run_whose_only_input_expired_is_working():
    expired = {**an_input("a"), "expires_at": T0}
    view = task_view(run_row("running"), [expired], SETTINGS)
    assert view["status"] == "working"
    assert "inputRequests" not in view


def test_view_timestamps_ttl_and_poll_kata():
    row = run_row(
        "succeeded", started_at=T0 + timedelta(seconds=1), finished_at=T0 + timedelta(seconds=9)
    )
    view = task_view(row, [], SETTINGS)
    assert view["taskId"] == str(row["run_id"])
    assert view["createdAt"] == T0.isoformat()
    assert view["lastUpdatedAt"] == (T0 + timedelta(seconds=9)).isoformat()
    assert view["ttlMs"] == SETTINGS.execution_retention_days * 86_400_000
    assert view["pollIntervalMs"] == SETTINGS.task_poll_interval_ms


def test_poll_interval_external():
    settings = Settings(
        database_url=SecretStr("postgresql://unused@localhost/unused"), external_poll_seconds=7.5
    )
    view = task_view(run_row("running", runtime="n8n_workflow"), [], settings)
    assert view["pollIntervalMs"] == 7500
    assert isinstance(view["pollIntervalMs"], int)


def test_last_updated_considers_inputs():
    late = T0 + timedelta(minutes=5)
    row = run_row("running", started_at=T0 + timedelta(seconds=1))
    view = task_view(
        row,
        [
            an_input("a", created=T0 + timedelta(minutes=1)),
            an_input("b", state="answered", answered=late),
        ],
        SETTINGS,
    )
    assert view["lastUpdatedAt"] == late.isoformat()


def test_timestamps_are_utc():
    from datetime import timezone

    ams = timezone(timedelta(hours=2))
    row = run_row("running", created_at=datetime(2026, 10, 30, 14, 0, tzinfo=ams))
    assert task_view(row, [], SETTINGS)["createdAt"] == T0.isoformat()


def test_status_message_included_when_set():
    view = task_view(run_row("running", status_message="reason required"), [], SETTINGS)
    assert view["statusMessage"] == "reason required"


def test_create_task_result_shape():
    row = run_row("queued")
    out = create_task_result(row, SETTINGS)
    assert set(out) == {
        "resultType",
        "taskId",
        "status",
        "createdAt",
        "lastUpdatedAt",
        "ttlMs",
        "pollIntervalMs",
    }
    assert out["resultType"] == "task"
    assert out["status"] == "working"
    with_msg = create_task_result(run_row("queued", status_message="hi"), SETTINGS)
    assert with_msg["statusMessage"] == "hi"


def test_view_result_type_complete_and_exactly_one_payload():
    for row, inputs in [
        (run_row("running"), []),
        (run_row("running"), [an_input()]),
        (run_row("succeeded"), []),
        (run_row("cancelled"), []),
    ]:
        view = task_view(row, inputs, SETTINGS)
        assert view["resultType"] == "complete"
        assert len({"result", "error", "inputRequests"} & set(view)) <= 1
        assert "error" not in view
