"""Lane A: expected upstream statuses, per-destination CA, 300 s caps, internal verifier."""

import json
import ssl

import httpx
import pytest
from pydantic import ValidationError

from runner.protocol import ProtocolError, encode, validate
from runner.sdk import API, ApiStatusError
from src.broker.client import Broker
from src.broker.models import UpstreamStatusError
from src.catalog.models import ToolLimits
from src.execution.controller import Controller
from src.execution.models import ExecutionDeniedError
from src.sources.guard import checked_base_url
from src.storage.bindings_ensure import MAX_LIMITS
from tests.fixtures import operation, settings
from tests.test_binding_ensure import ensure_api, service_headers  # noqa: F401
from tests.test_execution import (
    TENANT_A,
    FakeBackend,
    claim,
    make_broker,
    runtime,  # noqa: F401 -- fixture
)
from tests.test_keycloak_auth import kc_env  # noqa: F401

# --- manifest validation ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [(401,), (403,), (407,), (200,), (500,), (404, 404), (399,)])
def test_expected_statuses_rejected(bad):
    with pytest.raises(ValidationError):
        operation(expected_error_statuses=bad)


def test_expected_statuses_and_timeout_cap():
    assert operation(expected_error_statuses=(404, 409, 422)).expected_error_statuses == (
        404,
        409,
        422,
    )
    assert operation().expected_error_statuses == ()
    assert operation(timeout_seconds=300).timeout_seconds == 300
    with pytest.raises(ValidationError):
        operation(timeout_seconds=301)


def test_settings_caps_allow_300():
    config = settings(api_timeout_seconds=300, sync_wait_seconds=300, protocol_timeout_seconds=330)
    assert config.sync_wait_seconds == 300
    with pytest.raises(ValidationError):
        settings(sync_wait_seconds=300, protocol_timeout_seconds=300)


def test_binding_limit_ceiling():
    assert (MAX_LIMITS.wall_seconds, MAX_LIMITS.api_calls) == (300, 100)
    assert MAX_LIMITS.queue_seconds == 240
    assert (MAX_LIMITS.frame_bytes, MAX_LIMITS.output_bytes) == (262144, 1048576)
    assert isinstance(MAX_LIMITS, ToolLimits)


# --- runner protocol + sdk ----------------------------------------------------------------------


def result_frame(error):
    return {"type": "api_result", "sequence": 1, "result": None, "error": error}


def test_protocol_accepts_http_status_error_only_in_shape():
    ok = {"code": "http_status", "status": 404, "body": {"detail": "x"}}
    validate(result_frame(ok))
    validate(result_frame({**ok, "body": None}))
    validate(result_frame({"code": "api_call_denied"}))
    for bad in (
        {**ok, "status": 500},
        {**ok, "status": "404"},
        {**ok, "body": "text"},
        {"code": "http_status", "status": 404},
        {**ok, "extra": 1},
        {"code": "secret"},
    ):
        with pytest.raises(ProtocolError):
            validate(result_frame(bad))


class Pipe:
    def __init__(self, reply):
        self.reply = reply

    def write(self, data):
        pass

    def flush(self):
        pass

    def readline(self, _):
        return self.reply


async def call_with(error):
    reply = encode(result_frame(error), 65536)
    return await API(Pipe(reply), Pipe(reply), 65536).call(
        "0190" + "0" * 4 + "-0000-7000-8000-" + "0" * 12, {}
    )


async def test_sdk_raises_api_status_error():
    with pytest.raises(ApiStatusError) as caught:
        await call_with({"code": "http_status", "status": 409, "body": {"a": 1}})
    assert caught.value.status == 409 and caught.value.body == {"a": 1}
    with pytest.raises(RuntimeError, match="api_call_denied"):
        await call_with({"code": "api_call_denied"})


# --- broker + controller ----------------------------------------------------------------------


def expecting(broker, statuses):
    original = broker.catalog.operation

    async def patched(reference):
        op, status = await original(reference)
        return op.model_copy(update={"expected_error_statuses": statuses}), status

    catalog = broker.catalog
    broker.catalog = type(
        "C",
        (),
        {
            "operation": staticmethod(patched),
            "service": catalog.service,
            "service_owner": catalog.service_owner,
            "may_use_service": catalog.may_use_service,
        },
    )()


@pytest.mark.parametrize(
    ("response", "expected_body"),
    [
        (httpx.Response(404, json={"detail": "nope"}), {"detail": "nope"}),
        (httpx.Response(404, content=b"<html>"), None),
        (httpx.Response(404, content=b"x" * 20000), None),
    ],
)
async def test_broker_expected_status(runtime, tmp_path, response, expected_body):  # noqa: F811
    row = await claim(runtime)
    broker, seen, _, _ = await make_broker(runtime, tmp_path, lambda r: response)
    expecting(broker, (404,))
    try:
        with pytest.raises(UpstreamStatusError) as caught:
            await broker.call(row, str(runtime.refs[0].id), {})
        assert caught.value.status == 404 and caught.value.body == expected_body
        async with runtime.database.pool.connection() as conn:
            journal = await (await conn.execute("SELECT status FROM mcp_run_api_calls")).fetchall()
            events = await (
                await conn.execute("SELECT * FROM mcp_run_events WHERE kind='broker_denied'")
            ).fetchall()
        assert [j["status"] for j in journal] == ["failed"] and not events
    finally:
        await broker.close()


async def test_broker_unexpected_or_undeclared_status_still_denied(runtime, tmp_path):  # noqa: F811
    row = await claim(runtime)
    broker, _, _, _ = await make_broker(runtime, tmp_path, lambda r: httpx.Response(404))
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
    finally:
        await broker.close()
    broker, _, _, _ = await make_broker(runtime, tmp_path, lambda r: httpx.Response(500))
    expecting(broker, (404,))
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
    finally:
        await broker.close()


async def test_broker_status_body_with_credential_is_nulled(runtime, tmp_path):  # noqa: F811
    row = await claim(runtime)
    broker, _, tokens, _ = await make_broker(
        runtime, tmp_path, lambda r: httpx.Response(409, json={"echo": tokens_box[0]})
    )
    tokens_box = [tokens["service_1"]]
    expecting(broker, (409,))
    try:
        with pytest.raises(UpstreamStatusError) as caught:
            await broker.call(row, str(runtime.refs[0].id), {})
        assert caught.value.body is None
    finally:
        await broker.close()


async def test_controller_sends_http_status_frame(runtime, tmp_path):  # noqa: F811
    row = await claim(runtime)
    api = encode(
        {
            "type": "api_call",
            "sequence": 1,
            "operation_id": str(runtime.refs[0].id),
            "arguments": {},
        },
        65536,
    )
    frames = (
        encode({"type": "ready", "protocol": 1}, 65536)
        + api
        + encode({"type": "result", "result": {}}, 65536)
    )
    backend = FakeBackend([(frames, b"")])
    broker, _, _, _ = await make_broker(
        runtime, tmp_path, lambda r: httpx.Response(404, json={"detail": "gone"})
    )
    expecting(broker, (404,))
    try:
        await Controller(
            runtime.repository, runtime.execution, broker, backend, runtime.catalog.repository
        ).execute(row)
        finished = await runtime.repository.get(TENANT_A, row["run_id"])
        assert finished["status"] == "succeeded"
        reply = next(f for f in backend.channel.sent if f["type"] == "api_result")
        assert reply["result"] is None
        assert reply["error"] == {"code": "http_status", "status": 404, "body": {"detail": "gone"}}
    finally:
        await broker.close()


# --- per-destination CA -------------------------------------------------------------------------


async def test_destination_ca_file_gets_its_own_verifying_client(runtime, tmp_path):  # noqa: F811
    ca = tmp_path / "ca.pem"
    import certifi

    ca.write_text(open(certifi.where()).read())
    from src.sources.models import SourceCreate

    body = {
        "source_key": "x",
        "base_url": "https://x.internal",
        "auth": {"kind": "licence", "app_key": "x"},
    }
    assert SourceCreate.model_validate_json(
        json.dumps({**body, "ca_file": str(ca)})
    ).ca_file == str(ca)
    assert SourceCreate.model_validate_json(json.dumps(body)).ca_file is None
    broker, _, _, _ = await make_broker(runtime, tmp_path)
    plain = Broker(
        broker.execution, broker.catalog, broker.credentials, broker.licenses, broker.sources
    )
    try:
        with_ca = plain.client_for(str(ca))
        assert with_ca is plain.client_for(str(ca)) and with_ca is not plain.client_for(None)
        assert plain.client_for(None) is plain.client
        # Injected clients (tests) are used for every destination.
        assert broker.client_for(str(ca)) is broker.client
        assert isinstance(ssl.create_default_context(cafile=str(ca)), ssl.SSLContext)
    finally:
        await plain.close()
        await broker.close()


# --- /mcp/internal verifier ---------------------------------------------------------------------

INTERNAL = {
    "internal_keycloak_url": "https://auth.example.test",
    "internal_keycloak_realm": "internal",
    "internal_keycloak_audience": "mcp-internal",
    "internal_keycloak_allowed_clients": "provisioner, other",
}


def test_internal_settings_validation():
    config = settings(
        binding_ensure_clients=frozenset({"provisioner"}),
        license_app_key="mcp",
        license_introspect_url="https://l.example/i",
        **INTERNAL,
    )
    assert config.internal_clients == {"provisioner", "other"}
    assert config.internal_audiences == ("mcp-internal",)
    with pytest.raises(ValidationError):  # ensure client outside the internal allow-list
        settings(
            binding_ensure_clients=frozenset({"stranger"}),
            license_app_key="mcp",
            license_introspect_url="https://l.example/i",
            **INTERNAL,
        )
    with pytest.raises(ValidationError):  # ensure clients without any internal config
        settings(
            binding_ensure_clients=frozenset({"provisioner"}),
            license_app_key="mcp",
            license_introspect_url="https://l.example/i",
        )
    with pytest.raises(ValidationError):  # partial
        settings(internal_keycloak_url="https://auth.example.test")
    with pytest.raises(ValidationError):  # plain http
        settings(**{**INTERNAL, "internal_keycloak_url": "http://auth.example.test"})
    assert settings().internal_clients == frozenset()


def test_internal_settings_read_env(monkeypatch):
    monkeypatch.setenv("MCP_INTERNAL_KEYCLOAK_URL", "https://auth.example.test")
    monkeypatch.setenv("MCP_INTERNAL_KEYCLOAK_REALM", "internal")
    monkeypatch.setenv("MCP_INTERNAL_KEYCLOAK_AUDIENCE", "a,b")
    monkeypatch.setenv("MCP_INTERNAL_KEYCLOAK_ALLOWED_CLIENTS", "c")
    from src.auth import keycloak
    from src.config import Settings

    config = keycloak.internal_config(Settings(database_url="postgresql://u/d"))
    assert config is not None and config.audiences == ("a", "b")
    assert config.issuer == "https://auth.example.test/realms/internal"
    for name in list(INTERNAL):
        monkeypatch.delenv("MCP_" + name.upper())
    assert keycloak.internal_config(Settings(database_url="postgresql://u/d")) is None


async def test_internal_routes_use_only_the_internal_realm(ensure_api):  # noqa: F811
    app, client = ensure_api
    body = {"product": "agent", "licence_id": "x", "licence_token": "gateway"}
    url = f"/mcp/internal/tenants/{TENANT_A}/bindings/ensure"
    # Same token passes KEYCLOAK_* (realm "portal") but the internal verifier trusts another realm.
    app.state.settings = app.state.settings.model_copy(update={"internal_keycloak_realm": "other"})
    assert (await client.post(url, json=body, headers=service_headers())).status_code == 401
    # And /mcp/internal never falls back to KEYCLOAK_* when the internal config is absent.
    app.state.settings = app.state.settings.model_copy(update={"internal_keycloak_url": None})
    assert (await client.post(url, json=body, headers=service_headers())).status_code == 503


@pytest.mark.parametrize(
    "base_url",
    ["https://api.nonagentic.ai/coding", "https://api.nonagentic.ai/coding/", "https://x.internal"],
)
def test_destination_accepts_a_fixed_path_prefix(base_url):
    assert checked_base_url(base_url) == base_url


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.nonagentic.ai/coding/../licenses",
        "https://api.nonagentic.ai/Coding",
        "https://api.nonagentic.ai//coding",
        "https://api.nonagentic.ai/coding?x=1",
        "https://api.nonagentic.ai/co%2fding",
    ],
)
def test_destination_rejects_noncanonical_paths(base_url):
    with pytest.raises(ValueError):
        checked_base_url(base_url)
