"""Actual socket HTTP + MCP SDK acceptance. Dispatch is an injected recorder only."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import httpx
import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from src.auth.context import RequestAuthority, authority
from src.auth.principal import Principal
from src.catalog.public import output_envelope
from src.catalog.validation import validate_instance, validate_schema
from src.protocol import BoundedProtocol
from src.toolkits.models import (
    BindingSpec,
    BoundContext,
    ToolkitReference,
    ToolReference,
    ToolSelection,
)
from tests.fixtures import ACTOR, FILES, TENANT_A, TENANT_B, TOOL, tool
from tests.test_access_http import ADMIN
from tests.test_bindings import LICENSE, PRINCIPAL, profile, selection, toolkit
from tests.test_catalog_http import catalog_api as catalog_api
from tests.test_catalog_http import upload


class Recorder:
    def __init__(self):
        self.calls = []

    async def submit(self, principal, resolved, inputs, idempotency_key):
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        assert not {
            "x-tenant-id",
            "x-mcp-binding",
            "authorization",
            "x-mcp-idempotency-key",
        }.intersection(request.headers)
        self.calls.append((principal, resolved, inputs, idempotency_key))
        # No package source is read, imported or evaluated.
        return {"run_id": str(uuid7()), "status": "succeeded", "result": {}, "error": None}


async def seed(app, client):
    assert (await client.post("/mcp/tools", headers=ADMIN, json=upload())).status_code == 201
    assert (
        await client.post(
            f"/mcp/tools/{TOOL}/versions/1/publish", headers=ADMIN, json={"expected_revision": 1}
        )
    ).status_code == 200
    spec, _, _ = await selection(app.state.access)
    binding, token = await app.state.access.binding_write(PRINCIPAL, spec)
    return binding, {"X-Tenant-Id": "Bearer gateway", "X-MCP-Binding": token}


@asynccontextmanager
async def listening(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


async def rpc(client, url, method, params=None, headers=None, identity=1):
    response = await client.post(
        url,
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
        json={
            "jsonrpc": "2.0",
            "id": identity,
            "method": method,
            **({"params": params} if params is not None else {}),
        },
    )
    if response.status_code != 200:
        return response, {"http_error": response.text}
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        return response, json.loads(data)
    return response, response.json()


async def test_real_sdk_initialize_list_call_and_default_unavailable(catalog_api):
    app, client = catalog_api
    binding, headers = await seed(app, client)
    async with listening(app) as base:
        for endpoint in ["/mcp", "/mcp/"]:
            async with (
                httpx.AsyncClient(headers=headers) as http,
                httpx2.AsyncClient(headers=headers) as sdk_http,
            ):
                async with streamable_http_client(base + endpoint, http_client=sdk_http) as (
                    read,
                    write,
                ):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        capabilities = initialized.capabilities.model_dump(
                            by_alias=True, exclude_none=True
                        )
                        assert capabilities == {"tools": {"listChanged": False}}
                        tools = (await session.list_tools()).tools
                        assert [t.name for t in tools] == ["synthetic_tool"]
                        assert tools[0].input_schema == tool().input_schema
                        assert tools[0].output_schema == output_envelope(tool().result_schema)
                        assert all(
                            s not in str(tools)
                            for s in ["package.main", "gateway_alias", str(binding.binding_id)]
                        )
                        result = await session.call_tool("synthetic_tool", {})
                        assert result.is_error and result.structured_content is None
                        from mcp.types import TextContent

                        assert isinstance(result.content[0], TextContent)
                        assert result.content[0].text == "execution_unavailable"
                        assert (await session.call_tool("hidden", {})).is_error
                # Stateless GET/DELETE do not provide persistent notification sessions.
                assert (await http.get(base + endpoint)).status_code == 405
                assert (await http.delete(base + endpoint)).status_code in {400, 405}
                assert (await http.get(base + "/mcp/sse")).status_code == 404
                assert (await http.get(base + "/mcp/messages")).status_code == 404


async def test_two_bindings_exact_versions_and_retained_wrapper(catalog_api):
    app, client = catalog_api
    binding, headers = await seed(app, client)
    recorder = Recorder()
    app.state.provider.submission = recorder
    # Second toolkit pins version two; version one remains discoverable to its binding.
    assert (
        await client.post(
            f"/mcp/tools/{TOOL}/versions",
            headers=ADMIN,
            json=upload(tool(version=2), expected_revision=2),
        )
    ).status_code == 201
    assert (
        await client.post(
            f"/mcp/tools/{TOOL}/versions/2/publish", headers=ADMIN, json={"expected_revision": 3}
        )
    ).status_code == 200
    manifest = toolkit(
        name="second_toolkit",
        tools=(ToolSelection(tool=ToolReference(tenant_id=TENANT_A, id=TOOL, version=2)),),
    )
    await app.state.access.toolkit_write(PRINCIPAL, manifest)
    await app.state.access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "published", 1)
    spec = binding.spec.model_copy(
        update={"toolkits": (type(binding.spec.toolkits[0])(id=manifest.toolkit_id, version=1),)}
    )
    second, token = await app.state.access.binding_write(PRINCIPAL, spec)
    other_headers = {**headers, "X-MCP-Binding": token}
    async with listening(app) as base, httpx.AsyncClient() as http:
        results = await asyncio.gather(
            *[
                rpc(
                    http,
                    base + "/mcp",
                    "tools/call",
                    {"name": "synthetic_tool", "arguments": {}},
                    {**h, "X-MCP-Idempotency-Key": "host-key-1"},
                )
                for h in [headers, other_headers]
            ]
        )
        assert all(payload["result"]["isError"] is False for _, payload in results), results
        assert {call[1].manifest.version for call in recorder.calls} == {1, 2}
        assert all(call[3] == "host-key-1" and call[2] == {} for call in recorder.calls)
        for params in [
            {"name": "hidden", "arguments": {}},
            {"name": "synthetic_tool", "arguments": {"tenant_id": str(TENANT_B)}},
            {"name": "synthetic_tool", "arguments": {}, "version": "99"},
        ]:
            _, payload = await rpc(http, base + "/mcp", "tools/call", params, headers)
            assert payload["result"]["isError"]
        assert len(recorder.calls) == 2
    principal = Principal(TENANT_A, LICENSE, "caller", binding.binding_id, 1)
    context = authority.set(RequestAuthority(principal, None))
    try:
        retained = await app.state.provider.get_tool("synthetic_tool")
        assert retained is not None
        assert await app.state.provider.get_tool_by_hash("guessed", "synthetic_tool") is None
        await app.state.access.binding_action(PRINCIPAL, binding.binding_id, 1, revoke=True)
        assert (await retained.run({})).is_error
    finally:
        authority.reset(context)
    assert len(recorder.calls) == 2
    with pytest.raises(PermissionError):
        await app.state.provider.list_tools()


async def test_same_public_name_two_tenants_concurrent_and_different_schema(catalog_api):
    app, client = catalog_api
    first, first_headers = await seed(app, client)
    b_operator = Principal(TENANT_B, LICENSE, "tenant-b", management=True)
    b_tool = tool(
        tenant=TENANT_B,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    await app.state.catalog.create_draft(TENANT_B, b_tool, FILES, b_operator.actor)
    await app.state.catalog.transition(
        TENANT_B, TOOL, 1, "published", b_operator.actor, expected_revision=1
    )
    credential = profile(tenant=TENANT_B)
    await app.state.access.profile_write(b_operator, credential)
    manifest = toolkit(tenant=TENANT_B)
    await app.state.access.toolkit_write(b_operator, manifest)
    await app.state.access.toolkit_transition(b_operator, manifest.toolkit_id, 1, "published", 1)

    second, token = await app.state.access.binding_write(
        b_operator,
        BindingSpec(
            credential_profile_id=credential.profile_id,
            toolkits=(ToolkitReference(id=manifest.toolkit_id, version=1),),
            context=BoundContext(tenant_id=TENANT_B),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    b_headers = {"X-Tenant-Id": "Bearer foreign", "X-MCP-Binding": token}
    recorder = Recorder()
    app.state.provider.submission = recorder
    async with listening(app) as base, httpx.AsyncClient() as http:
        for _ in range(2):
            listings = await asyncio.gather(
                *[
                    rpc(http, base + "/mcp", "tools/list", headers=h)
                    for h in [first_headers, b_headers]
                ]
            )
            assert listings[0][1]["result"]["tools"][0]["inputSchema"] == tool().input_schema
            assert listings[1][1]["result"]["tools"][0]["inputSchema"] == b_tool.input_schema
            calls = await asyncio.gather(
                *[
                    rpc(
                        http,
                        base + "/mcp",
                        "tools/call",
                        {"name": "synthetic_tool", "arguments": arguments},
                        h,
                    )
                    for h, arguments in [(first_headers, {}), (b_headers, {"n": 7})]
                ]
            )
            assert all(not result["result"]["isError"] for _, result in calls)
        assert {c[0].tenant_id for c in recorder.calls} == {TENANT_A, TENANT_B}
        assert all(
            c[0].tenant_id == c[1].manifest.tenant_id == c[1].context.tenant_id
            for c in recorder.calls
        )
        assert (
            await http.post(base + "/mcp/tools", headers=first_headers, json=upload())
        ).status_code == 401
        assert (
            await http.post(
                base + "/mcp", headers={**first_headers, "Mcp-Session-Id": "guessed"}, json={}
            )
        ).status_code == 401


async def test_pagination_foreign_forged_stale_and_transport_limits(catalog_api):
    app, client = catalog_api
    binding, headers = await seed(app, client)
    second = tool(tool_id=uuid7(), public_name="another_tool")
    await app.state.catalog.create_draft(TENANT_A, second, FILES, ACTOR)
    await app.state.catalog.transition(
        TENANT_A, second.tool_id, 1, "published", ACTOR, expected_revision=1
    )
    manifest = toolkit(
        name="second_toolkit",
        tools=tuple(
            ToolSelection(tool=ToolReference(tenant_id=TENANT_A, id=t, version=1))
            for t in [TOOL, second.tool_id]
        ),
    )
    await app.state.access.toolkit_write(PRINCIPAL, manifest)
    await app.state.access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "published", 1)

    spec = binding.spec.model_copy(
        update={"toolkits": (ToolkitReference(id=manifest.toolkit_id, version=1),)}
    )
    paged, token = await app.state.access.binding_write(PRINCIPAL, spec)
    h = {**headers, "X-MCP-Binding": token}
    async with listening(app) as base, httpx.AsyncClient() as http:
        _, page = await rpc(http, base + "/mcp", "tools/list", headers=h)
        cursor = page["result"]["nextCursor"]
        assert [t["name"] for t in page["result"]["tools"]] == ["another_tool"]
        _, page = await rpc(http, base + "/mcp", "tools/list", {"cursor": cursor}, h)
        assert [t["name"] for t in page["result"]["tools"]] == ["synthetic_tool"]
        for bad, who in [(cursor + "x", h), ("not-base64", h), (cursor, headers)]:
            _, result = await rpc(http, base + "/mcp", "tools/list", {"cursor": bad}, who)
            assert result["error"]["message"] == "catalog_unavailable_or_invalid_cursor"
        # Publication elsewhere invalidates an old page conservatively.
        draft = tool(tool_id=uuid7(), public_name="draft_only")
        await app.state.catalog.create_draft(TENANT_A, draft, FILES, ACTOR)
        await app.state.catalog.transition(
            TENANT_A, draft.tool_id, 1, "published", ACTOR, expected_revision=1
        )
        _, result = await rpc(http, base + "/mcp", "tools/list", {"cursor": cursor}, h)
        assert "error" in result
        response = await http.post(base + "/mcp", headers=h, content=b"x" * 262145)
        assert response.status_code == 413
        assert (
            await http.post(
                base + "/mcp", headers={**h, "X-MCP-Idempotency-Key": "bad/key"}, json={}
            )
        ).status_code == 401
        app.state.settings = app.state.settings.model_copy(update={"protocol_output_bytes": 16})
        response, _ = await rpc(http, base + "/mcp", "tools/list", headers=h)
        assert response.status_code == 503 and response.json()["error"] == "protocol_unavailable"
        assert (
            await http.post(
                base + "/mcp/mcp",
                headers=ADMIN,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        ).status_code == 404
    assert authority.get() is None


def test_output_schema_rebases_nested_refs_and_preserves_literals():
    schema = {
        "type": ["object", "null"],
        "$defs": {"résult": {"type": "integer"}},
        "properties": {"value": {"$ref": "#/$defs/résult"}},
        "default": {"$ref": "literal-business-data"},
        "additionalProperties": False,
    }
    from tests.fixtures import settings

    envelope = output_envelope(schema)
    validate_schema(envelope, settings())
    assert envelope["$defs"]["tool_result"]["default"] == schema["default"]
    assert (
        envelope["$defs"]["tool_result"]["properties"]["value"]["$ref"]
        == "#/$defs/tool_result/$defs/résult"
    )
    validate_instance(
        envelope,
        {"run_id": str(uuid7()), "status": "succeeded", "result": {"value": 2}, "error": None},
        settings(),
    )
    with pytest.raises(ValueError):
        validate_instance(
            envelope,
            {
                "run_id": str(uuid7()),
                "status": "succeeded",
                "result": {"value": "2"},
                "error": None,
            },
            settings(),
        )
    assert output_envelope({"$ref": "#"})["$defs"]["tool_result"]["$ref"] == "#/$defs/tool_result"


async def test_inflight_admission_disconnect_and_timeout_release():
    from types import SimpleNamespace

    from tests.fixtures import settings

    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(scope, receive, send):
        entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    state = SimpleNamespace(settings=settings(protocol_max_inflight=1))
    transport = BoundedProtocol(slow, state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=transport), base_url="http://127.0.0.1"
    ) as http:
        task = asyncio.create_task(http.get("/"))
        await entered.wait()
        assert (await http.get("/")).status_code == 503
        release.set()
        assert (await task).status_code == 200
        assert transport.active == 0

        release.clear()
        state.settings = state.settings.model_copy(update={"protocol_timeout_seconds": 0.01})
        assert (await http.get("/")).status_code in {408, 503}
        assert transport.active == 0


async def test_two_api_instances_and_root_path_observe_live_revocation(catalog_api):
    from src.asgi import create_app
    from tests.lifespan import running_app
    from tests.test_authentication import info

    app, client = catalog_api
    binding, headers = await seed(app, client)
    replica = create_app(app.state.settings)
    async with running_app(replica):
        await replica.state.licenses.client.aclose()
        replica.state.licenses.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json=info(license_id=str(LICENSE), tenant_id=str(TENANT_A), app={"key": "mcp"}),
                )
            )
        )
        async with (
            listening(app) as first,
            listening(replica) as second,
            httpx.AsyncClient() as http,
        ):
            for base in [first, second, first, second]:
                response, page = await rpc(http, base + "/mcp", "tools/list", headers=headers)
                assert (
                    response.status_code == 200
                    and page["result"]["tools"][0]["name"] == "synthetic_tool"
                )
            await app.state.access.binding_action(PRINCIPAL, binding.binding_id, 1, revoke=True)
            for base in [first, second]:
                response, _ = await rpc(http, base + "/mcp", "tools/list", headers=headers)
                assert response.status_code == 401
    assert replica.state.database.pool.closed and not replica.state.protocol_ready
    # Reverse-proxy root paths must retain the exact same authentication boundary.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path="/gateway"),
        base_url="http://127.0.0.1/gateway",
    ) as http:
        assert (await http.post("/mcp", json={})).status_code == 401
        _, token = await app.state.access.binding_write(PRINCIPAL, binding.spec)
        response, page = await rpc(
            http,
            "http://127.0.0.1/gateway/mcp",
            "tools/list",
            headers={**headers, "X-MCP-Binding": token},
        )
        assert response.status_code == 200 and page["result"]["tools"]


MODERN = "2026-07-28"


def modern_meta(extensions=None):
    return {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "acceptance", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": (
            {"extensions": extensions} if extensions is not None else {}
        ),
    }


async def modern_rpc(client, url, method, params=None, headers=None, extensions=None):
    params = {**(params or {}), "_meta": modern_meta(extensions)}
    return await rpc(
        client,
        url,
        method,
        params,
        {"MCP-Protocol-Version": MODERN, "Mcp-Method": method, **(headers or {})},
    )


async def test_modern_protocol_discover_list_and_call(catalog_api):
    app, client = catalog_api
    _, headers = await seed(app, client)
    recorder = Recorder()
    app.state.provider.submission = recorder
    async with listening(app) as base, httpx.AsyncClient() as http:
        response, payload = await modern_rpc(http, base + "/mcp", "server/discover", {}, headers)
        assert response.status_code == 200, payload
        assert MODERN in payload["result"]["supportedVersions"]
        assert payload["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert "mcp-session-id" not in response.headers
        _, payload = await modern_rpc(http, base + "/mcp", "tools/list", {}, headers)
        assert [t["name"] for t in payload["result"]["tools"]] == ["synthetic_tool"]
        _, payload = await modern_rpc(
            http,
            base + "/mcp",
            "tools/call",
            {"name": "synthetic_tool", "arguments": {}},
            {**headers, "Mcp-Name": "synthetic_tool"},
        )
        assert payload["result"]["resultType"] == "complete"
        assert payload["result"]["isError"] is False
        assert payload["result"]["structuredContent"]["status"] == "succeeded"
        assert len(recorder.calls) == 1
