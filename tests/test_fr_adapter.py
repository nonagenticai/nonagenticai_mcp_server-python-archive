import json
import re
import time
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.catalog.models import FrCommandTarget, FrProjectTarget
from src.external.adapter import ExternalRef, ExternalStartError, ExternalState
from src.external.fr import (
    CLOSED_KEYS,
    PROJECT_KEYS,
    PROJECT_VIEW_MAX_BYTES,
    FrAdapter,
    FrCommandAdapter,
    close_view,
    decision_text,
    elicitation_request,
    project_view,
)
from src.external.signing import sign, verify
from src.platform.toolkit_source import load_all, tool_manifest
from tests.fixtures import external_tool, settings

SECRET = "s3cret"  # pragma: allowlist secret -- test-only HMAC key
RUN = UUID("30000000-0000-7000-8000-000000000001")
TENANT = UUID("10000000-0000-7000-8000-000000000001")
ACTOR = {"channel": "mcp", "address": "care-bus:t:u1"}
MANIFEST = json.dumps({"target": {"kind": "fr_project", "project_type": "feature"}})
ROOT = Path(__file__).resolve().parents[1] / "toolkits"


def row(**extra):
    return {
        "run_id": RUN,
        "tenant_id": TENANT,
        "inputs": {"title": "T", "description": "D", "ignored": "x"},
        "actor": ACTOR,
        "status": "running",
        "external_ref": {"kind": "fr", "id": "appr-1", "project_id": "proj-1"},
        "authority": {"manifest": MANIFEST},
        **extra,
    }


def adapter(handler, **overrides):
    cfg = settings(
        fr_base_url="http://fr.test",
        fr_channel_secret=SecretStr(SECRET),
        **overrides,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FrAdapter(client, cfg)


def test_sign_verify_roundtrip():
    headers = sign(b'{"a":1}', SECRET, 1000)
    assert headers["X-Channel-Signature"].startswith("sha256=")
    assert headers["X-Channel-Timestamp"] == "1000"
    assert re.fullmatch(r"[0-9a-f-]{36}", headers["Idempotency-Key"])
    assert verify(b'{"a":1}', headers, SECRET, 1000)
    lowered = {k.lower(): v for k, v in headers.items()}
    assert verify(b'{"a":1}', lowered, SECRET, 1299)


def test_verify_rejects_skew_and_tamper():
    headers = sign(b"body", SECRET, 1000)
    assert not verify(b"body", headers, SECRET, 1301)
    assert not verify(b"body", headers, SECRET, 699)
    assert not verify(b"body!", headers, SECRET, 1000)
    assert not verify(b"body", headers, "other", 1000)
    assert not verify(b"body", {}, SECRET, 1000)
    assert not verify(
        b"body", {"X-Channel-Signature": headers["X-Channel-Signature"]}, SECRET, 1000
    )
    assert not verify(b"body", {**headers, "X-Channel-Timestamp": "abc"}, SECRET, 1000)
    assert not verify(b"body", {**headers, "X-Channel-Signature": "sha256=zz"}, SECRET, 1000)


def recorder(response):
    """Handler that records every request body and answers with ``response``."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return response(request) if callable(response) else response

    return sent, handler


def ok(payload):
    return httpx.Response(200, json=payload)


PENDING = {"status": "pending-approval", "actionId": "act-1", "approvalId": "appr-1"}
PENDING_ROW = {"kind": "fr", "id": "appr-1"}


async def test_start_posts_projects_create_signed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["headers"] = dict(request.headers)
        return ok(PENDING)

    inputs = {
        "title": "  Ship\n the   thing ",
        "description": "D",
        "problem_statement": "P",
        "desired_outcome": "O",
        "repository_url": "https://example.test/r.git",
        "ignored": "x",
        "empty": "",
    }
    ref = await adapter(handler).start(row(inputs=inputs, external_ref=None))
    assert ref == ExternalRef("fr", "appr-1")
    assert seen["url"] == "http://fr.test/api/webhooks/mcp/incoming"
    assert verify(seen["body"], seen["headers"], SECRET, int(time.time()))
    body = json.loads(seen["body"])
    assert body["address"] == "care-bus:t:u1"
    assert body["correlation_id"] == str(RUN)
    assert set(body) == {"address", "text", "correlation_id"}
    command, _, rest = body["text"].partition(" ")
    assert command == "projects.create"
    assert json.loads(rest) == {
        "title": "Ship the thing",
        "project_type": "feature",
        "description": "D",
        "problem_statement": "P",
        "desired_outcome": "O",
        "repository_url": "https://example.test/r.git",
    }


async def test_start_omits_absent_passthrough():
    sent, handler = recorder(ok(PENDING))
    await adapter(handler).start(row(inputs={"title": "T"}))
    assert json.loads(sent[0]["text"].removeprefix("projects.create ")) == {
        "title": "T",
        "project_type": "feature",
    }


async def test_start_immediate_result_is_the_project():
    result = {"status": "result", "correlationId": "c", "result": {"project_id": "pid-9"}}
    ref = await adapter(lambda r: ok(result)).start(row())
    assert ref == ExternalRef("fr", "pid-9", project_id="pid-9")
    assert ref.stored() == {"kind": "fr", "id": "pid-9", "project_id": "pid-9"}


def test_external_ref_stored_drops_none():
    assert ExternalRef("fr", "a").stored() == {"kind": "fr", "id": "a"}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"actor": None}, "actor_required"),
        ({"actor": {"channel": "mcp"}}, "actor_required"),
        ({"inputs": {"description": "D"}}, "title_required"),
        ({"inputs": {"title": "  \n "}}, "title_required"),
        ({"inputs": {"title": "T", "description": "d" * 4001}}, "inputs_too_large"),
    ],
)
async def test_start_local_validation(overrides, code):
    sent, handler = recorder(ok(PENDING))
    with pytest.raises(ExternalStartError) as err:
        await adapter(handler).start(row(**overrides))
    assert err.value.code == code
    assert sent == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (ok({"status": "unknown-sender"}), "actor_unresolved"),
        (ok({"status": "no-action"}), "fr_no_action"),
        (ok({"status": "error", "code": "AppError"}), "apperror"),
        (ok({"status": "error"}), "fr_error"),
        (httpx.Response(503, json={"error": "dispatch_off"}), "fr_unavailable"),
        (httpx.Response(500), "fr_unavailable"),
        (httpx.Response(401), "fr_rejected"),
        (httpx.Response(400), "fr_rejected"),
        (ok({"status": "result", "result": {}}), "external_start_failed"),
        (ok({"status": "pending-approval"}), "external_start_failed"),
        (ok({"status": "other"}), "external_start_failed"),
        (ok({"status": "result", "result": {"project_id": 5}}), "external_start_failed"),
        (httpx.Response(200, content=b"<html>"), "external_result_invalid"),
        (ok([1]), "external_result_invalid"),
    ],
)
async def test_start_errors_raise_sanitized_code(response, code):
    with pytest.raises(ExternalStartError) as err:
        await adapter(lambda r: response).start(row())
    assert err.value.code == code
    assert re.fullmatch(r"[a-z_]{1,64}", err.value.code)


async def test_start_unconfigured():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(ExternalStartError) as err:
        await FrAdapter(client, settings()).start(row())
    assert err.value.code == "external_unconfigured"


async def test_actor_may_be_json_text_and_manifest_json_text():
    sent, handler = recorder(ok(PENDING))
    await adapter(handler).start(row(actor=json.dumps(ACTOR)))
    assert sent[0]["address"] == "care-bus:t:u1"


# poll


async def test_poll_cancelling_is_cancelled_without_http():
    sent, handler = recorder(ok({}))
    state = await adapter(handler).poll(row(status="cancelling"))
    assert state == ExternalState("cancelled")
    assert sent == []


async def test_poll_waits_for_start_approval():
    sent, handler = recorder(ok({}))
    state = await adapter(handler).poll(row(external_ref=PENDING_ROW))
    assert state.status == "running"
    assert state.message == "waiting for the project start to be approved"
    assert sent == []


@pytest.mark.parametrize(
    ("outcome", "status", "code"),
    [
        ("rejected", "cancelled", None),
        ("voided", "cancelled", None),
        ("expired", "failed", "project_start_expired"),
        ("failed", "failed", "project_start_failed"),
    ],
)
async def test_poll_start_outcomes_without_http(outcome, status, code):
    sent, handler = recorder(ok({}))
    ref = {**PENDING_ROW, "start_outcome": outcome}
    state = await adapter(handler).poll(row(external_ref=ref))
    assert (state.status, state.code) == (status, code)
    assert sent == []


def project(**fields):
    return ok({"status": "result", "correlationId": "c", "result": fields})


async def test_poll_reads_project_as_the_actor():
    sent, handler = recorder(project(status="active"))
    await adapter(handler).poll(row())
    assert sent == [
        {
            "address": "care-bus:t:u1",
            "text": 'projects.get {"id":"proj-1"}',
            "correlation_id": f"{RUN}:poll",
        }
    ]


async def test_poll_delivered_returns_result_keys():
    delivered = {
        "id": "proj-1",
        "name": "N",
        "project_type": "feature",
        "status": "delivered",
        "url": "https://fr.test/p/1",
        "delivered_at": "2026-01-01T00:00:00Z",
        "delivery_evidence": "pr#1",
        "deliverables": [{"title": "a"}],
        "unlisted_extra": "dropped",
    }
    state = await adapter(lambda r: project(**delivered)).poll(row())
    assert state.status == "succeeded"
    assert state.result == {k: v for k, v in delivered.items() if k != "unlisted_extra"}


async def test_poll_delivered_omits_none_keys():
    state = await adapter(lambda r: project(id="p", status="delivered", url=None)).poll(row())
    assert state.result == {"id": "p", "status": "delivered"}


@pytest.mark.parametrize(
    ("fields", "status", "code", "message"),
    [
        ({"status": "cancelled"}, "cancelled", None, None),
        ({"status": "archived"}, "cancelled", None, None),
        ({"status": "failed"}, "failed", "project_failed", None),
        ({"status": "active"}, "running", None, "active"),
        ({"status": "active", "phase": "build"}, "running", None, "build"),
        ({"status": "active", "phase": "p" * 600}, "running", None, "p" * 512),
    ],
)
async def test_poll_maps_project_status(fields, status, code, message):
    state = await adapter(lambda r: project(**fields)).poll(row())
    assert (state.status, state.code, state.message) == (status, code, message)


@pytest.mark.parametrize(
    "response",
    [
        ok({"status": "unknown-sender"}),
        ok({"status": "result", "result": "text"}),
        ok({"status": "result", "result": None}),
        ok({"status": "result"}),
    ],
)
async def test_poll_refusal_or_bad_result_raises_runtime_error(response):
    with pytest.raises(RuntimeError):
        await adapter(lambda r: response).poll(row())


async def test_poll_unknown_sender_code():
    with pytest.raises(RuntimeError, match="actor_unresolved"):
        await adapter(lambda r: ok({"status": "unknown-sender"})).poll(row())


# cancel


async def test_cancel_rejects_pending_start_as_the_actor():
    sent, handler = recorder(ok({"status": "result", "result": {}}))
    await adapter(handler).cancel(row(external_ref=PENDING_ROW))
    assert sent == [
        {
            "address": "care-bus:t:u1",
            "text": "reject appr-1 cancelled by the client",
            "correlation_id": f"{RUN}:cancel",
        }
    ]


async def test_cancel_with_project_sends_nothing():
    sent, handler = recorder(ok({}))
    await adapter(handler).cancel(row())
    assert sent == []


async def test_cancel_swallows_refusal():
    sent, handler = recorder(ok({"status": "no-action"}))
    await adapter(handler).cancel(row(external_ref=PENDING_ROW))
    assert len(sent) == 1


# forward


def item(response, **extra):
    return {
        "input_key": f"{RUN}:abcd",
        "source_ref": "appr-1",
        "request": {},
        "response": response,
        "actor": {"channel": "mcp", "address": "care-bus:t:u2"},
        **extra,
    }


@pytest.mark.parametrize(
    ("content", "text"),
    [
        ({"decision": "approved"}, "approve appr-1"),
        ({"decision": "approved", "comment": ""}, "approve appr-1"),
        ({"decision": "approved", "comment": "ok\n  go  ahead"}, "approve appr-1 ok go ahead"),
        ({"decision": "rejected", "reason": "too\nrisky"}, "reject appr-1 too risky"),
    ],
)
@pytest.mark.parametrize("action", ["accept"])
async def test_forward_relays_decision_as_item_actor(action, content, text):
    sent, handler = recorder(ok({"status": "result", "result": {}}))
    result = await adapter(handler).forward(row(), item({"action": action, "content": content}))
    assert result is None
    assert sent == [
        {
            "address": "care-bus:t:u2",
            "text": text,
            "correlation_id": f"{RUN}:{RUN}:abcd",
        }
    ]


@pytest.mark.parametrize(
    "response",
    [
        {"action": "accept", "content": {"decision": "rejected"}},
        {"action": "accept", "content": {"decision": "rejected", "reason": "  "}},
        {"action": "accept", "content": {"decision": "maybe"}},
        {"action": "accept", "content": {}},
        {"action": "accept"},
        {"action": "decline", "content": {"decision": "approved"}},
        {"action": "cancel", "content": {"decision": "approved"}},
        {},
    ],
)
async def test_forward_sends_nothing_when_nothing_to_relay(response):
    sent, handler = recorder(ok({}))
    assert await adapter(handler).forward(row(), item(response)) is None
    assert sent == []


async def test_forward_without_item_actor_sends_nothing():
    sent, handler = recorder(ok({}))
    entry = item({"action": "accept", "content": {"decision": "approved"}}, actor=None)
    assert await adapter(handler).forward(row(), entry) is None
    assert sent == []


async def test_forward_unknown_sender_returns_code():
    entry = item({"action": "accept", "content": {"decision": "approved"}})
    code = await adapter(lambda r: ok({"status": "unknown-sender"})).forward(row(), entry)
    assert code == "actor_unresolved"


# elicitation + decision_text


def test_elicitation_request_shape():
    assert elicitation_request("Deploy?", "touches db") == {
        "method": "elicitation/create",
        "params": {
            "message": "Deploy?\n\ntouches db",
            "requestedSchema": {
                "type": "object",
                "required": ["decision"],
                "properties": {
                    "decision": {"type": "string", "enum": ["approved", "rejected"]},
                    "reason": {"type": "string"},
                    "comment": {"type": "string"},
                },
            },
        },
    }


@pytest.mark.parametrize("summary", [None, "", 5])
def test_elicitation_request_without_summary(summary):
    request = elicitation_request("Deploy?", summary)
    assert request["params"]["message"] == "Deploy?"
    assert "deblock_action" not in request["params"]["requestedSchema"]["properties"]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"decision": "approved"}, "approve a1"),
        ({"decision": "approved", "comment": " fine\tby me "}, "approve a1 fine by me"),
        ({"decision": "rejected", "reason": "no"}, "reject a1 no"),
        ({"decision": "rejected"}, None),
        ({"decision": "rejected", "reason": ""}, None),
        ({"decision": "other"}, None),
        ({}, None),
    ],
)
def test_decision_text(content, expected):
    assert decision_text("a1", content) == expected


async def test_envelope_carries_actor_job_and_on_behalf_of():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "pending-approval", "approvalId": "ap-1"})

    actor = {
        "channel": "mcp",
        "address": f"agent:{TENANT}",
        "job": "01900000-0000-7000-8000-000000000001",
        "on_behalf_of": "kc-sub-1",
    }
    await adapter(handler).start(row(actor=actor, external_ref=None, status="starting"))
    assert sent[0]["address"] == f"agent:{TENANT}"
    assert sent[0]["job"] == "01900000-0000-7000-8000-000000000001"
    assert sent[0]["on_behalf_of"] == "kc-sub-1"
    assert "channel" not in sent[0]


async def test_envelope_without_optional_actor_fields_is_unchanged():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "pending-approval", "approvalId": "ap-1"})

    await adapter(handler).start(row(external_ref=None, status="starting"))
    assert set(sent[0]) == {"address", "text", "correlation_id"}


async def test_answer_uses_the_answering_actor_context():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "result", "result": {}})

    item = {
        "source_ref": "ap-9",
        "input_key": "k",
        "response": {"action": "accept", "content": {"decision": "approved"}},
        "actor": {"channel": "mcp", "address": f"agent:{TENANT}", "on_behalf_of": "kc-sub-2"},
    }
    assert await adapter(handler).forward(row(status="running"), item) is None
    assert sent[0]["address"] == f"agent:{TENANT}" and sent[0]["on_behalf_of"] == "kc-sub-2"
    assert "job" not in sent[0]


PLATFORM_MANIFEST = json.dumps(
    {
        "tenant_id": "01900000-0000-7000-8000-00000000f001",
        "target": {"kind": "fr_project", "project_type": "it"},
    }
)


async def test_platform_owned_tool_acts_as_the_calling_tenants_agent_member():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "pending-approval", "approvalId": "ap-1"})

    actor = {"channel": "mcp", "address": f"agent:{TENANT}", "job": str(RUN)}
    await adapter(handler).start(
        row(actor=actor, authority={"manifest": PLATFORM_MANIFEST}, external_ref=None)
    )
    assert sent[0]["address"] == f"agent:{TENANT}"
    assert '"project_type":"it"' in sent[0]["text"]


async def test_agent_address_of_another_tenant_is_refused_without_sending():
    sent = []
    actor = {"channel": "mcp", "address": "agent:01900000-0000-7000-8000-000000000bad"}
    fr = adapter(lambda request: sent.append(request) or httpx.Response(200, json={}))
    with pytest.raises(ExternalStartError) as error:
        await fr.start(
            row(actor=actor, authority={"manifest": PLATFORM_MANIFEST}, external_ref=None)
        )
    assert error.value.code == "actor_tenant_mismatch" and sent == []
    item = {
        "source_ref": "ap-1",
        "input_key": "k",
        "response": {"action": "accept", "content": {"decision": "approved"}},
        "actor": actor,
    }
    assert await fr.forward(row(), item) == "actor_tenant_mismatch" and sent == []


@pytest.mark.parametrize(
    "inputs, expected",
    [({"title": "T"}, "it"), ({"title": "T", "project_type": "bug-hunt_2"}, "bug-hunt_2")],
)
async def test_project_type_input_overrides_the_target_default(inputs, expected):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "pending-approval", "approvalId": "ap-1"})

    await adapter(handler).start(
        row(inputs=inputs, authority={"manifest": PLATFORM_MANIFEST}, external_ref=None)
    )
    payload = json.loads(sent[0]["text"].removeprefix("projects.create "))
    assert payload["project_type"] == expected and set(payload) == {"title", "project_type"}


@pytest.mark.parametrize("bad", ["IT", "a b", "", "x" * 129, 7])
async def test_invalid_project_type_input_fails_the_start(bad):
    fr = adapter(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ExternalStartError) as error:
        await fr.start(row(inputs={"title": "T", "project_type": bad}, external_ref=None))
    assert error.value.code == "invalid_project_type"


# fr_command (fr_get_project): one signed read, replays the stored answer

COMMAND_MANIFEST = json.dumps({"target": {"kind": "fr_command", "command": "projects.get"}})
PROJECT = {
    "id": "proj-1",
    "name": "N",
    "project_type": "feature",
    "status": "active",
    "phase": "build",
    "url": "https://fr.test/p/1",
    "delivered_at": None,
    "delivery_evidence": None,
    "deliverables": None,
    "unlisted_extra": "dropped",
}
PROJECT_VIEW = {
    "id": "proj-1",
    "name": "N",
    "project_type": "feature",
    "status": "active",
    "phase": "build",
    "url": "https://fr.test/p/1",
}


def command_row(**extra):
    return {
        "run_id": RUN,
        "tenant_id": TENANT,
        "inputs": {"id": "proj-1"},
        "actor": ACTOR,
        "status": "starting",
        "external_ref": None,
        "authority": {"manifest": COMMAND_MANIFEST},
        **extra,
    }


def command_adapter(handler, **overrides):
    cfg = settings(
        fr_base_url="http://fr.test",
        fr_channel_secret=SecretStr(SECRET),
        **overrides,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FrCommandAdapter(client, cfg)


def project_result(**fields):
    return ok({"status": "result", "correlationId": "c", "result": fields})


async def test_get_project_sends_projects_get_and_returns_the_view():
    sent, handler = recorder(project_result(**PROJECT))
    ref = await command_adapter(handler).start(command_row())
    assert sent == [
        {
            "address": "care-bus:t:u1",
            "text": 'projects.get {"id":"proj-1"}',
            "correlation_id": str(RUN),
        }
    ]
    assert ref == ExternalRef("fr_command", "proj-1", project_id="proj-1", result=PROJECT_VIEW)
    # The one-shot answer rides on the reference: a crash between start and finish loses nothing.
    assert ref.stored()["result"] == PROJECT_VIEW


async def test_get_project_reads_the_target_command_from_a_text_manifest():
    sent, handler = recorder(project_result(**PROJECT))
    await command_adapter(handler).start(
        command_row(authority={"manifest": COMMAND_MANIFEST.encode()}, actor=json.dumps(ACTOR))
    )
    assert sent[0]["text"] == 'projects.get {"id":"proj-1"}'


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"actor": None}, "actor_required"),
        ({"actor": {"channel": "mcp"}}, "actor_required"),
        ({"inputs": {}}, "invalid_project_id"),
        ({"inputs": {"id": 7}}, "invalid_project_id"),
        ({"inputs": {"id": ""}}, "invalid_project_id"),
        (
            {
                "authority": {
                    "manifest": json.dumps(
                        {"target": {"kind": "fr_command", "command": "projects.list"}}
                    )
                }
            },
            "unsupported_command",
        ),
    ],
)
async def test_get_project_local_validation(overrides, code):
    sent, handler = recorder(project_result(**PROJECT))
    with pytest.raises(ExternalStartError) as error:
        await command_adapter(handler).start(command_row(**overrides))
    assert error.value.code == code
    assert sent == []


async def test_get_project_refuses_another_tenants_agent_address():
    sent, handler = recorder(project_result(**PROJECT))
    actor = {"channel": "mcp", "address": "agent:01900000-0000-7000-8000-000000000bad"}
    with pytest.raises(ExternalStartError) as error:
        await command_adapter(handler).start(command_row(actor=actor))
    assert error.value.code == "actor_tenant_mismatch"
    assert sent == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (ok({"status": "unknown-sender"}), "actor_unresolved"),
        (ok({"status": "no-action"}), "fr_no_action"),
        (ok({"status": "error", "code": "AppError"}), "apperror"),
        (httpx.Response(503, json={"error": "dispatch_off"}), "fr_unavailable"),
        (httpx.Response(401), "fr_rejected"),
        (ok({"status": "pending-approval", "approvalId": "a"}), "external_result_invalid"),
        (ok({"status": "result", "result": "text"}), "external_result_invalid"),
        (ok({"status": "result"}), "external_result_invalid"),
        (httpx.Response(200, content=b"<html>"), "external_result_invalid"),
    ],
)
async def test_get_project_errors_map_to_start_codes(response, code):
    with pytest.raises(ExternalStartError) as error:
        await command_adapter(lambda request: response).start(command_row())
    assert error.value.code == code


async def test_get_project_poll_replays_the_stored_answer_without_http():
    sent, handler = recorder(ok({}))
    ref = {"kind": "fr_command", "id": "proj-1", "project_id": "proj-1", "result": PROJECT_VIEW}
    state = await command_adapter(handler).poll(command_row(external_ref=ref, status="running"))
    assert state == ExternalState("succeeded", result=PROJECT_VIEW)
    assert sent == []


async def test_get_project_poll_without_an_answer_reads_the_project():
    sent, handler = recorder(project_result(status="active", phase="build"))
    ref = {"kind": "fr_command", "id": "proj-1", "project_id": "proj-1"}
    state = await command_adapter(handler).poll(command_row(external_ref=ref, status="running"))
    assert state == ExternalState("running", message="build")
    assert sent[0]["text"] == 'projects.get {"id":"proj-1"}'
    assert sent[0]["correlation_id"] == f"{RUN}:poll"


async def test_get_project_cancel_sends_nothing():
    sent, handler = recorder(ok({}))
    assert await command_adapter(handler).cancel(command_row()) is None
    assert sent == []


# catalog + toolkit source accept the read command


def test_catalog_accepts_a_fr_command_tool():
    target = FrCommandTarget(kind="fr_command", command="projects.get")
    manifest = external_tool(runtime="fr_command", target=target)
    assert manifest.target == target
    assert manifest.effect == "read_only" and manifest.entrypoint is None
    with pytest.raises(ValidationError):
        FrCommandTarget.model_validate({"kind": "fr_command", "command": "projects.list"})
    with pytest.raises(ValidationError, match="kind"):
        external_tool(
            runtime="fr_command",
            target=FrProjectTarget(kind="fr_project", project_type="it"),
        )


def test_fr_toolkit_source_loads_the_read_tool_without_a_runner():
    fr = next(source for source in load_all(ROOT) if source.name == "fr")
    manifest = tool_manifest(fr, fr.tool("fr_get_project"), TENANT, None)
    assert manifest.runtime == "fr_command" and manifest.effect == "read_only"
    assert manifest.target == FrCommandTarget(kind="fr_command", command="projects.get")
    assert manifest.entrypoint is None and manifest.runner_image_digest is None
    assert manifest.allowed_operations == ()
    assert (manifest.limits.wall_seconds, manifest.limits.api_calls) == (60, 2)


# fr_command (fr_close_project): one signed write, the closed project back

CLOSE_MANIFEST = json.dumps({"target": {"kind": "fr_command", "command": "projects.close"}})
CLOSED = {
    "id": "proj-1",
    "name": "N",
    "project_type": "feature",
    "status": "delivered",
    "phase": "closed",
    "url": "https://fr.test/p/1",
    "delivered_at": "2026-01-01T00:00:00Z",
    "delivery_evidence": "pr#1",
    "deliverables": [{"title": "a"}],
    "closed": True,
    "already_closed": False,
    "rm_pm_project_id": "rm-7",
    "unlisted_extra": "dropped",
}
CLOSED_VIEW = {k: v for k, v in CLOSED.items() if k != "unlisted_extra"}


def close_row(**extra):
    return command_row(authority={"manifest": CLOSE_MANIFEST}, **extra)


async def test_close_project_sends_projects_close_and_returns_the_closed_project():
    sent, handler = recorder(project_result(**CLOSED))
    ref = await command_adapter(handler).start(close_row())
    assert sent == [
        {
            "address": "care-bus:t:u1",
            "text": 'projects.close {"id":"proj-1"}',
            "correlation_id": str(RUN),
        }
    ]
    assert ref == ExternalRef("fr_command", "proj-1", project_id="proj-1", result=CLOSED_VIEW)
    assert ref.stored()["result"]["already_closed"] is False


async def test_close_project_absent_fields_are_omitted():
    sent, handler = recorder(project_result(id="proj-1", status="delivered"))
    ref = await command_adapter(handler).start(close_row())
    assert ref.result == {"id": "proj-1", "status": "delivered"}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (ok({"status": "unknown-sender"}), "actor_unresolved"),
        (ok({"status": "no-action"}), "fr_no_action"),
        (ok({"status": "pending-approval", "approvalId": "a"}), "external_result_invalid"),
        (ok({"status": "result", "result": "text"}), "external_result_invalid"),
        (httpx.Response(503, json={"error": "dispatch_off"}), "fr_unavailable"),
        (httpx.Response(401), "fr_rejected"),
    ],
)
async def test_close_project_errors_map_to_start_codes(response, code):
    with pytest.raises(ExternalStartError) as error:
        await command_adapter(lambda request: response).start(close_row())
    assert error.value.code == code


async def test_close_project_is_not_retried_by_poll():
    """The one-shot answer rides on the reference: a poll replays it, never closes twice."""
    sent, handler = recorder(ok({}))
    ref = {"kind": "fr_command", "id": "proj-1", "project_id": "proj-1", "result": CLOSED_VIEW}
    state = await command_adapter(handler).poll(close_row(external_ref=ref, status="running"))
    assert state == ExternalState("succeeded", result=CLOSED_VIEW)
    assert sent == []


def test_catalog_accepts_the_close_command_only():
    target = FrCommandTarget(kind="fr_command", command="projects.close")
    manifest = external_tool(runtime="fr_command", target=target, effect="writes")
    assert manifest.target == target and manifest.effect == "writes"
    with pytest.raises(ValidationError):
        FrCommandTarget.model_validate({"kind": "fr_command", "command": "projects.cancel"})
    with pytest.raises(ValidationError):
        FrCommandTarget.model_validate({"kind": "fr_command", "command": "projects.create"})


def test_fr_toolkit_source_loads_the_close_tool_without_a_runner():
    fr = next(source for source in load_all(ROOT) if source.name == "fr")
    assert {tool.name for tool in fr.tools} == {
        "start_fr_project",
        "fr_get_project",
        "fr_close_project",
    }
    manifest = tool_manifest(fr, fr.tool("fr_close_project"), TENANT, None)
    assert manifest.runtime == "fr_command" and manifest.effect == "writes"
    assert manifest.target == FrCommandTarget(kind="fr_command", command="projects.close")
    assert manifest.entrypoint is None and manifest.runner_image_digest is None
    assert manifest.allowed_operations == ()
    assert manifest.limits.wall_seconds == 60
    assert manifest.limits.queue_seconds == 240
    assert manifest.limits.api_calls == 2
    assert manifest.input_schema["required"] == ["id"]


def encoded_size(value):
    return len(json.dumps(value, default=str, separators=(",", ":")).encode())


def test_project_view_is_bounded_and_drops_bulk_fields_first():
    huge = {
        "id": "proj-1",
        "status": "delivered",
        "delivery_evidence": "e" * 40_000,
        "deliverables": [{"title": "x" * 500} for _ in range(400)],
    }
    view = project_view(huge)
    assert view["id"] == "proj-1" and view["status"] == "delivered"
    assert "deliverables" not in view  # dropped first
    assert encoded_size(view) <= PROJECT_VIEW_MAX_BYTES


def test_project_view_drops_evidence_before_identity():
    huge = {"id": "proj-1", "status": "delivered", "delivery_evidence": "e" * 70_000}
    view = project_view(huge)
    assert "delivery_evidence" not in view
    assert view == {"id": "proj-1", "status": "delivered"}


def test_project_view_truncates_a_huge_scalar_as_a_last_resort():
    huge = {"id": "proj-1", "name": "n" * 200_000, "status": "delivered", "url": "u" * 200_000}
    view = project_view(huge)
    assert encoded_size(view) <= PROJECT_VIEW_MAX_BYTES
    assert view["id"] == "proj-1" and view["status"] == "delivered"
    assert len(view["name"]) < 200_000 and len(view["url"]) < 200_000


def test_close_view_uses_the_close_keys_and_the_same_bound():
    assert set(close_view(CLOSED)) == set(CLOSED_KEYS)
    bounded = close_view({**CLOSED, "delivery_evidence": "e" * 70_000})
    assert encoded_size(bounded) <= PROJECT_VIEW_MAX_BYTES
    assert bounded["id"] == "proj-1" and "delivery_evidence" not in bounded


def test_closed_keys_superset_of_project_keys():
    assert set(PROJECT_KEYS) <= set(CLOSED_KEYS)
    assert set(CLOSED_KEYS) - set(PROJECT_KEYS) == {
        "closed",
        "already_closed",
        "rm_pm_project_id",
    }
