import json
import logging
import uuid

import httpx
import jwt
import pytest
from pydantic import SecretStr

from src.external.adapter import ExternalRef, ExternalStartError
from src.external.n8n import WEBHOOK_TOKEN_TTL_SECONDS, N8nAdapter, webhook_token
from tests.fixtures import settings

KEY = "sk-n8n-very-secret"
RUN_ID = uuid.uuid4()
TENANT = uuid.uuid4()


def make_settings(**overrides):
    values = {
        "n8n_base_url": "http://n8n.test",
        "n8n_webhook_base_url": "http://hooks.test",
        "n8n_api_key": SecretStr(KEY),
        **overrides,
    }
    return settings(**values)


def make_row(ref_id="42", result_node="MCP Result"):
    manifest = {
        "target": {"kind": "n8n_workflow", "webhook_path": "my-flow", "result_node": result_node}
    }
    return {
        "run_id": RUN_ID,
        "tenant_id": TENANT,
        "inputs": {"a": 1},
        "external_ref": {"kind": "n8n", "id": ref_id} if ref_id else None,
        "authority": {"manifest": json.dumps(manifest)},
    }


def adapter(handler, **overrides):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return N8nAdapter(client, make_settings(**overrides))


def run_data(node, items):
    return {"resultData": {"runData": {node: [{"data": {"main": [[{"json": i} for i in items]]}}]}}}


def execution(status, data=None):
    return httpx.Response(200, json={"id": "42", "status": status, "data": data})


def success(node="MCP Result", items=({"answer": 42},)):
    return execution("success", run_data(node, list(items)))


async def test_start_reads_execution_id():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"executionId": "42"})

    ref = await adapter(handler).start(make_row(None))
    assert ref == ExternalRef("n8n", "42")
    assert seen["url"] == "http://hooks.test/webhook/my-flow"
    assert seen["body"] == {
        "mcp_run_id": str(RUN_ID),
        "tenant_id": str(TENANT),
        "inputs": {"a": 1},
    }


HOOK_SECRET = (
    "hook-secret-0123456789abcdef0123456789abcdef"  # pragma: allowlist secret -- test fixture
)


async def test_start_sends_signed_webhook_jwt_only_when_configured():
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"executionId": "42"})

    await adapter(handler).start(make_row(None))
    await adapter(handler, n8n_webhook_secret=SecretStr(HOOK_SECRET)).start(make_row(None))
    assert "authorization" not in seen[0]
    scheme, token = seen[1]["authorization"].split(" ")
    assert scheme == "Bearer"
    claims = jwt.decode(token, HOOK_SECRET, algorithms=["HS256"])
    assert claims["sub"] == str(RUN_ID)
    assert claims["exp"] - claims["iat"] == WEBHOOK_TOKEN_TTL_SECONDS
    # n8n saves request headers in the execution record: the key itself must never be sent.
    assert all(HOOK_SECRET not in value for headers in seen for value in headers.values())
    assert "x-mcp-webhook-token" not in seen[1]


def test_webhook_token_expires():
    token = webhook_token(HOOK_SECRET, "run", now=1_000)
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, HOOK_SECRET, algorithms=["HS256"])
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            webhook_token(HOOK_SECRET, "run"),
            "other-key-0123456789abcdef0123456789",
            algorithms=["HS256"],
        )


def test_webhook_secret_never_in_settings_repr():
    assert HOOK_SECRET not in repr(make_settings(n8n_webhook_secret=SecretStr(HOOK_SECRET)))


@pytest.mark.parametrize("body", [{}, {"executionId": ""}, {"executionId": 7}, [1]])
async def test_start_without_execution_id_raises(body):
    with pytest.raises(ExternalStartError) as error:
        await adapter(lambda r: httpx.Response(200, json=body)).start(make_row(None))
    assert error.value.code == "external_start_failed"


async def test_start_non_json_body_raises():
    with pytest.raises(ExternalStartError) as error:
        await adapter(lambda r: httpx.Response(200, text="<html>")).start(make_row(None))
    assert error.value.code == "external_start_failed"


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_start_error_status_raises(status):
    with pytest.raises(ExternalStartError) as error:
        await adapter(lambda r: httpx.Response(status, json={"executionId": "42"})).start(
            make_row(None)
        )
    assert error.value.code == "external_start_failed"


async def test_start_5xx_raises():
    with pytest.raises(ExternalStartError) as error:
        await adapter(lambda r: httpx.Response(502)).start(make_row(None))
    assert error.value.code == "external_start_failed"


async def test_start_transport_error_raises():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(ExternalStartError) as error:
        await adapter(handler).start(make_row(None))
    assert error.value.code == "external_start_failed"


async def test_start_unconfigured_raises():
    with pytest.raises(ExternalStartError) as error:
        await adapter(lambda r: httpx.Response(200), n8n_webhook_base_url=None).start(
            make_row(None)
        )
    assert error.value.code == "external_unconfigured"


@pytest.mark.parametrize(
    ("n8n_status", "expected", "code"),
    [
        ("new", "running", None),
        ("running", "running", None),
        ("waiting", "running", None),
        ("success", "succeeded", None),
        ("error", "failed", "external_failed"),
        ("crashed", "failed", "external_failed"),
        ("canceled", "cancelled", None),
        ("unknown", "running", None),
    ],
)
async def test_status_mapping(n8n_status, expected, code):
    state = await adapter(lambda r: execution(n8n_status, {})).poll(make_row())
    assert state.status == expected
    assert state.code == code


async def test_waiting_sets_message():
    state = await adapter(lambda r: execution("waiting")).poll(make_row())
    assert state.message == "waiting for approval in fr"
    state = await adapter(lambda r: execution("running")).poll(make_row())
    assert state.message is None


async def test_poll_request_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-N8N-API-KEY")
        return execution("running")

    await adapter(handler).poll(make_row())
    assert (
        seen["url"]
        == "http://n8n.test/api/v1/executions/42?includeData=true&ignoreDataSizeLimit=true"
    )
    assert seen["key"] == KEY


async def test_result_from_last_item_of_node():
    state = await adapter(lambda r: success(items=[{"a": 1}, {"a": 2}])).poll(make_row())
    assert state.status == "succeeded"
    assert state.result == {"a": 2}


async def test_custom_result_node():
    state = await adapter(lambda r: success("Out", [{"x": 1}])).poll(make_row(result_node="Out"))
    assert state.result == {"x": 1}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"resultData": {"runData": {}}},
        {"resultData": {"runData": {"Other": []}}},
        {"resultData": {"runData": {"MCP Result": []}}},
        run_data("MCP Result", []),
    ],
)
async def test_result_missing_gives_empty(data):
    state = await adapter(lambda r: execution("success", data)).poll(make_row())
    assert state.status == "succeeded"
    assert state.result == {}


@pytest.mark.parametrize(
    ("body", "code"),
    [
        # n8n 2.46.1: data omitted (should not happen with includeData) or too large to display.
        ({"id": "42", "status": "success"}, "external_result_invalid"),
        ({"id": "42", "status": "success", "data": None}, "external_result_invalid"),
        (
            {"id": "42", "status": "success", "data": {}, "dataTooLargeToDisplay": True},
            "external_result_invalid",
        ),
        (
            {
                "id": "42",
                "status": "success",
                "data": {"redactionInfo": {"isRedacted": True, "reason": "policy"}},
            },
            "external_result_redacted",
        ),
    ],
)
async def test_unreadable_success_fails(body, code):
    state = await adapter(lambda r: httpx.Response(200, json=body)).poll(make_row())
    assert state.status == "failed"
    assert state.code == code


async def test_non_object_result_fails():
    state = await adapter(lambda r: success(items=[[1, 2]])).poll(make_row())
    assert state.status == "failed"
    assert state.code == "external_result_invalid"


async def test_oversize_result_fails():
    state = await adapter(lambda r: success(items=[{"blob": "x" * (64 * 1024)}])).poll(make_row())
    assert state.status == "failed"
    assert state.code == "external_result_invalid"


async def test_oversize_body_aborts():
    def handler(request):
        return httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1))

    state = await adapter(handler).poll(make_row())
    assert state.status == "failed"
    assert state.code == "external_result_invalid"


async def test_poll_404_is_missing():
    state = await adapter(lambda r: httpx.Response(404)).poll(make_row())
    assert state.status == "failed"
    assert state.code == "external_missing"


async def test_unknown_limit_reached_fails():
    n8n = adapter(lambda r: execution("unknown"), n8n_unknown_status_limit=3)
    row = make_row()
    assert (await n8n.poll(row)).status == "running"
    assert (await n8n.poll(row)).status == "running"
    state = await n8n.poll(row)
    assert state.status == "failed"
    assert state.code == "external_status_unknown"


async def test_unknown_counter_resets_on_known_status():
    statuses = iter(["unknown", "unknown", "running", "unknown", "unknown"])
    n8n = adapter(lambda r: execution(next(statuses)), n8n_unknown_status_limit=3)
    row = make_row()
    for _ in range(5):
        assert (await n8n.poll(row)).status == "running"


async def test_poll_unconfigured_fails():
    state = await adapter(lambda r: execution("running"), n8n_api_key=None).poll(make_row())
    assert state.status == "failed"
    assert state.code == "external_unconfigured"


async def test_cancel_posts_stop():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-N8N-API-KEY")
        return httpx.Response(200, json={})

    await adapter(handler).cancel(make_row())
    assert seen == {
        "method": "POST",
        "url": "http://n8n.test/api/v1/executions/42/stop",
        "key": KEY,
    }


async def test_cancel_404_ignored():
    await adapter(lambda r: httpx.Response(404)).cancel(make_row())


async def test_cancel_without_ref_is_noop():
    def handler(request):
        raise AssertionError("no request expected")

    await adapter(handler).cancel(make_row(None))


def test_adapter_has_no_forward():
    # Every task input is an fr approval; answers go to the fr adapter for any runtime.
    assert not hasattr(adapter(lambda r: httpx.Response(404)), "forward")


async def test_api_key_header_sent_and_not_logged(caplog):
    seen = []

    def handler(request):
        seen.append(request.headers.get("X-N8N-API-KEY"))
        if request.method == "POST" and "stop" not in request.url.path:
            return httpx.Response(200, json={"executionId": "42"})
        return success(items=[{"secretbody": 1}])

    n8n = adapter(handler)
    with caplog.at_level(logging.DEBUG):
        await n8n.start(make_row(None))
        await n8n.poll(make_row())
        await n8n.cancel(make_row())
    assert KEY in seen
    assert KEY not in caplog.text
    assert "secretbody" not in caplog.text
