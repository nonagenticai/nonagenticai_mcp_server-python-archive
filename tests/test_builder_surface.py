"""Tenant builder surface on real PostgreSQL: auth separation, quotas, secrets, authoring flow.

Fake licences (by token) and a fake service JWT issuer; the upstream API and the broker fetch are
faked in process. Parts that need lanes 2/3 (real pinned fetch, draft execution) are xfail until
integration.
"""

import base64
import json
from uuid import UUID, uuid4, uuid7

import httpx
import pytest

from src.asgi import create_app
from src.broker.client import Broker
from src.config import PLATFORM_TENANT_ID
from tests.fixtures import settings
from tests.lifespan import running_app
from tests.test_authentication import info
from tests.test_keycloak_auth import AUDIENCE, CLIENT, KC_URL, REALM, _token, kc_env  # noqa: F401
from tests.test_mcp_catalog import rpc

PRODUCT = "other-client"  # authenticated by keycloak AND on the ensure allow-list
TENANT_A = UUID("10000000-0000-4000-8000-0000000000a1")  # licences mints v4 tenants
TENANT_B = UUID("10000000-0000-4000-8000-0000000000b1")
LICENCE_A, LICENCE_B = uuid4(), uuid4()
SECRET = "sekrit-Token-123456"  # pragma: allowlist secret
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
TENANT_HEADERS = {"X-Tenant-Id": "Bearer tenant-a"}

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "CRM", "version": "1"},
    "paths": {
        "/customers/{id}": {
            "get": {
                "operationId": "get_customer",
                "summary": "Get one customer",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}
SCRIPT = """import kit


async def main(inputs, ctx):
    return await kit.guarded(run(inputs, ctx))


async def run(inputs, ctx):
    body = await kit.call_ok(ctx, "crm.get_customer", {"id": inputs["id"]})
    return kit.completed({"customer": body})
"""
INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"id": {"type": "string", "maxLength": 64}},
    "required": ["id"],
}


class FakeFetcher:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.seen: list[tuple[str, int, float]] = []

    async def fetch_document(self, source, path, max_bytes, timeout):
        self.seen.append((path, max_bytes, timeout))
        return self.raw


class FakeRegistry:
    async def get(self, owner, key):
        return object()


def licence_answer(request: httpx.Request) -> httpx.Response:
    token = json.loads(request.content)["token"]
    table = {
        "tenant-a": (TENANT_A, LICENCE_A, "tenant"),
        "member-a": (TENANT_A, uuid4(), "member"),
        "tenant-b": (TENANT_B, LICENCE_B, "tenant"),
        "platform": (PLATFORM_TENANT_ID, uuid4(), "tenant"),
    }
    if token not in table:
        return httpx.Response(200, json={"active": False})
    tenant, licence, kind = table[token]
    return httpx.Response(
        200,
        json=info(tenant_id=str(tenant), license_id=str(licence), subject_kind=kind),
    )


async def start(database_url, kc, **overrides):
    values = dict(
        database_url=database_url,
        license_introspect_url="https://licenses.example/introspect",
        license_app_key="mcp",
        builder_enabled=True,
        source_secret_key=KEY,
        binding_ensure_clients=frozenset({PRODUCT}),
        internal_keycloak_url=KC_URL,
        internal_keycloak_realm=REALM,
        internal_keycloak_audience=AUDIENCE,
        internal_keycloak_allowed_clients=f"{PRODUCT}, {CLIENT}",
        binding_products={"agent": ("coding",)},
        product_bundles={"agent": "agent"},
    )
    return create_app(settings(**(values | overrides)))


class Stack:
    def __init__(self, app, client):
        self.app, self.client = app, client

    async def call(self, method, path, headers=None, **kwargs):
        return await self.client.request(
            method, path, headers=TENANT_HEADERS if headers is None else headers, **kwargs
        )


async def mount(app, client):
    await app.state.licenses.client.aclose()
    app.state.licenses.client = httpx.AsyncClient(transport=httpx.MockTransport(licence_answer))
    return Stack(app, client)


@pytest.fixture
async def stack(database_url, kc_env):  # noqa: F811
    app = await start(database_url, kc_env)
    async with running_app(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield await mount(app, client)


def source_body(key="crm", **extra):
    return {
        "source_key": key,
        "base_url": f"https://{key}.example.com/v1",
        "auth": {"kind": "static_bearer"},
        "token": SECRET,
        "spec_path": "/openapi.json",
        **extra,
    }


# --- auth separation ---------------------------------------------------------------------------


async def test_disabled_builder_is_404_everywhere(database_url, kc_env):  # noqa: F811
    app = await start(database_url, kc_env, builder_enabled=False)
    async with running_app(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            s = await mount(app, client)
            assert (await s.call("GET", "/mcp/builder/usage")).status_code == 404
            assert (await s.call("GET", "/mcp/builder/usage", headers={})).status_code == 404
            path = f"/mcp/internal/tenants/{TENANT_A}/builder/usage"
            assert (await s.call("GET", path)).status_code == 404
            # and an mcp licence without a binding is still the old refusal on /mcp
            response, _ = await rpc(client, "/mcp", "tools/list", {}, TENANT_HEADERS)
            assert response.status_code == 401


async def test_author_auth_matrix(stack):
    get = "/mcp/builder/usage"
    assert (await stack.call("GET", get)).status_code == 200
    assert (await stack.call("GET", get, headers={})).status_code == 401
    assert (await stack.call("GET", get, headers={"X-Tenant-Id": "Bearer nope"})).status_code == 401
    # a member's per-login licence never authors
    assert (
        await stack.call("GET", get, headers={"X-Tenant-Id": "Bearer member-a"})
    ).status_code == 401
    # operator and binding headers make the request ambiguous
    for extra in ({"X-MCP-Admin-Key": "x" * 40}, {"X-MCP-Binding": "b" * 40}):
        response = await stack.call("GET", get, headers=TENANT_HEADERS | extra)
        assert response.status_code == 401, extra
    # the platform tenant does not author
    forbidden = await stack.call("GET", get, headers={"X-Tenant-Id": "Bearer platform"})
    assert forbidden.status_code == 403
    # a service JWT alone is not an author
    jwt = {"Authorization": f"Bearer {_token(azp=PRODUCT)}"}
    assert (await stack.call("GET", get, headers=jwt)).status_code == 401


async def test_operator_routes_never_accept_an_author(stack):
    # an author licence on a management route is not an operator (needs the admin key)
    response = await stack.call("GET", "/mcp/sources")
    assert response.status_code in {401, 403}


def product_headers(azp=PRODUCT, licence="tenant-a", licence_id=LICENCE_A):
    return {
        "Authorization": f"Bearer {_token(azp=azp)}",
        "X-Tenant-Id": f"Bearer {licence}",
        "X-MCP-Licence-Id": str(licence_id),
    }


async def test_product_path_matrix(stack):
    path = f"/mcp/internal/tenants/{TENANT_A}/builder/usage"
    assert (await stack.call("GET", path, headers=product_headers())).status_code == 200
    # azp authenticated by keycloak but not on the allow-list
    not_listed = product_headers(azp="carebus-tool-executor")
    assert (await stack.call("GET", path, headers=not_listed)).status_code == 403
    # another tenant's path with this tenant's licence
    other = f"/mcp/internal/tenants/{TENANT_B}/builder/usage"
    assert (await stack.call("GET", other, headers=product_headers())).status_code == 403
    # wrong licence id for the token
    wrong = product_headers(licence_id=uuid4())
    assert (await stack.call("GET", path, headers=wrong)).status_code == 403
    # a member's licence is authentic but not the tenant-level one; no licence; bad jwt
    assert (
        await stack.call("GET", path, headers=product_headers(licence="member-a"))
    ).status_code == 403
    only_jwt = {"Authorization": f"Bearer {_token(azp=PRODUCT)}"}
    assert (await stack.call("GET", path, headers=only_jwt)).status_code == 401
    assert (
        await stack.call("GET", path, headers={"Authorization": "Bearer junk"})
    ).status_code == 401
    # binding / admin headers are refused here too
    mixed = product_headers() | {"X-MCP-Binding": "b" * 40}
    assert (await stack.call("GET", path, headers=mixed)).status_code == 401


async def asgi_post(app, path, headers, chunks):
    """Drive the ASGI app with a body source that counts how often it is read."""
    reads, sent = 0, []

    async def receive():
        nonlocal reads
        reads += 1
        if not chunks:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, reads


async def test_product_route_authenticates_before_reading_the_body(stack):
    path = f"/mcp/internal/tenants/{TENANT_A}/builder/sources"
    big = [b"x" * 65536] * 8
    for headers in ({}, {"Authorization": "Bearer junk"}):
        status, reads = await asgi_post(stack.app, path, headers, list(big))
        assert (status, reads) == (401, 0), headers
    unlisted = product_headers(azp="carebus-tool-executor")
    status, reads = await asgi_post(stack.app, path, unlisted, list(big))
    assert (status, reads) == (403, 0)
    # an authenticated service is read (and bounded) before the licence is checked
    status, reads = await asgi_post(stack.app, path, product_headers(), [b"{}"])
    assert reads >= 1 and status != 401


async def test_product_licence_in_the_body(stack):
    path = f"/mcp/internal/tenants/{TENANT_A}/builder/sources"
    headers = {"Authorization": f"Bearer {_token(azp=PRODUCT)}"}
    body = source_body("viaproduct") | {
        "licence_id": str(LICENCE_A),
        "licence_token": "tenant-a",
    }
    response = await stack.call("POST", path, headers=headers, json=body)
    assert response.status_code == 201, response.text
    assert response.json()["owner_tenant_id"] == str(TENANT_A)
    assert SECRET not in response.text
    # the tenant sees it directly; tenant B does not
    assert (await stack.call("GET", "/mcp/builder/sources/viaproduct")).status_code == 200
    other = await stack.call(
        "GET", "/mcp/builder/sources/viaproduct", headers={"X-Tenant-Id": "Bearer tenant-b"}
    )
    assert other.status_code == 404


# --- sources, quotas, secrets ------------------------------------------------------------------


async def test_source_secret_is_write_only(stack):
    created = await stack.call("POST", "/mcp/builder/sources", json=source_body())
    assert created.status_code == 201, created.text
    view = created.json()
    assert view["has_token"] is True and SECRET not in created.text
    rotated = await stack.call(
        "PUT",
        "/mcp/builder/sources/crm/secret",
        json={"token": "another-secret-value", "expected_revision": view["revision"]},
    )
    assert rotated.status_code == 200 and "another-secret-value" not in rotated.text
    for path in ("/mcp/builder/sources/crm", "/mcp/builder/sources"):
        text = (await stack.call("GET", path)).text
        assert SECRET not in text and "another-secret-value" not in text
    bad = await stack.call("POST", "/mcp/builder/sources", json=source_body("bad") | {"extra": 1})
    assert bad.status_code == 422 and SECRET not in bad.text


async def test_internal_host_and_http_are_refused(stack):
    for url in ("http://crm.example.com", "https://10.0.0.1", "https://db.internal"):
        response = await stack.call(
            "POST", "/mcp/builder/sources", json=source_body("x") | {"base_url": url}
        )
        assert response.status_code == 422, url


async def test_quotas(database_url, kc_env):  # noqa: F811
    app = await start(database_url, kc_env, builder_max_sources=1, builder_max_tools=1)
    async with running_app(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            s = await mount(app, client)
            assert (
                await s.call("POST", "/mcp/builder/sources", json=source_body())
            ).status_code == 201
            second = await s.call("POST", "/mcp/builder/sources", json=source_body("two"))
            assert second.status_code == 409 and second.json()["error"] == "quota_exceeded"
            tool = tool_body("first_tool")
            assert (await s.call("POST", "/mcp/builder/tools", json=tool)).status_code == 201
            over = await s.call("POST", "/mcp/builder/tools", json=tool_body("second_tool"))
            assert over.status_code == 409 and over.json()["resource"] == "tools"
            # another tenant has its own quota
            other = {"X-Tenant-Id": "Bearer tenant-b"}
            ok = await s.call(
                "POST", "/mcp/builder/tools", headers=other, json=tool_body("first_tool")
            )
            assert ok.status_code == 201
            usage = (await s.call("GET", "/mcp/builder/usage")).json()
            assert usage["used"]["tools"] == 1 and usage["limits"]["tools"] == 1


def tool_body(name="find_customer", ops=(), effect="read_only"):
    return {
        "public_name": name,
        "operation": "customer.find",
        "description": "Find a customer",
        "files": [{"path": "tool.py", "content": SCRIPT}],
        "input_schema": INPUT,
        "effect": effect,
        "ops": list(ops),
    }


# --- authoring flow ----------------------------------------------------------------------------


async def test_end_to_end_authoring_flow(stack):
    app = stack.app
    fetcher = FakeFetcher(json.dumps(SPEC).encode())
    app.state.builder.fetcher, app.state.builder.registry = fetcher, FakeRegistry()
    assert (await stack.call("POST", "/mcp/builder/sources", json=source_body())).status_code == 201

    fetched = await stack.call("POST", "/mcp/builder/sources/crm/spec/fetch", json={})
    assert fetched.status_code == 200, fetched.text
    sha = fetched.json()["sha256"]
    assert fetcher.seen[0][0] == "/openapi.json"
    listed = fetched.json()["operations"]
    assert [o["operationId"] for o in listed] == ["get_customer"] and listed[0]["importable"]
    again = await stack.call("GET", f"/mcp/builder/sources/crm/spec/{sha}/operations")
    assert again.status_code == 200 and again.json()["sha256"] == sha

    imported = await stack.call(
        "POST",
        "/mcp/builder/sources/crm/operations/import",
        json={"spec_sha256": sha, "select": ["get_customer"]},
    )
    assert imported.status_code == 201, imported.text
    op_key = imported.json()["imported"][0]["op_key"]
    assert op_key == "get_customer"
    ops = (await stack.call("GET", "/mcp/builder/sources/crm/operations")).json()["operations"]
    assert ops[0]["status"] == "draft" and ops[0]["effect"] == "read_only"

    # a tool must use imported operations only
    ghost = tool_body(ops=[{"source_key": "crm", "op_key": "nope"}])
    assert (await stack.call("POST", "/mcp/builder/tools", json=ghost)).status_code == 404
    # the host owns kit.py / ops.py
    host = tool_body() | {"files": [{"path": "ops.py", "content": "OPS = {}"}]}
    assert (await stack.call("POST", "/mcp/builder/tools", json=host)).status_code == 422

    body = tool_body(ops=[{"source_key": "crm", "op_key": op_key}]) | {
        "limits": {"queue_seconds": 240}
    }
    created = await stack.call("POST", "/mcp/builder/tools", json=body)
    assert created.status_code == 201, created.text
    tool = created.json()
    assert tool["status"] == "draft" and tool["ops"] == ["crm.get_customer"]
    assert [f["path"] for f in tool["files"]] == ["tool.py"]
    tool_id = tool["tool_id"]
    manifest = tool["manifest"]
    assert manifest["tenant_id"] == str(TENANT_A) and len(manifest["allowed_operations"]) == 1

    # a new version keeps the script and re-pins the operation
    revised = await stack.call(
        "POST",
        f"/mcp/builder/tools/{tool_id}/versions",
        json={"expected_revision": tool["revision"], "description": "Find a customer by id"},
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["version"] == 2 and revised.json()["ops"] == ["crm.get_customer"]

    # publishing takes the draft service and operation with it, atomically
    published = await stack.call(
        "POST",
        f"/mcp/builder/tools/{tool_id}/versions/2/publish",
        json={"expected_revision": revised.json()["revision"]},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    ops = (await stack.call("GET", "/mcp/builder/sources/crm/operations")).json()["operations"]
    assert ops[0]["status"] == "published"

    kit = await stack.call(
        "POST",
        "/mcp/builder/toolkits",
        json={
            "name": "crm_kit",
            "description": "CRM tools",
            "tools": [{"tool_id": tool_id}],
        },
    )
    assert kit.status_code == 201, kit.text
    toolkit = kit.json()
    # The selection keeps the selected tool's own limits, never the 60 s default.
    assert toolkit["manifest"]["tools"][0]["limits"]["queue_seconds"] == 240
    pub = await stack.call(
        "POST",
        f"/mcp/builder/toolkits/{toolkit['manifest']['toolkit_id']}/versions/1/publish",
        json={"expected_revision": toolkit["revision"]},
    )
    assert pub.status_code == 200, pub.text
    assert pub.json()["status"] == "published"
    assert pub.json()["bindings"] == {"products": []}

    # tenant isolation: tenant B sees none of it
    other = {"X-Tenant-Id": "Bearer tenant-b"}
    assert (
        await stack.call("GET", f"/mcp/builder/tools/{tool_id}", headers=other)
    ).status_code == 404
    assert (await stack.call("GET", "/mcp/builder/tools", headers=other)).json()["items"] == []

    # decision 4: ensure includes the tenant's own published toolkit, and needs no credentials
    # for its tenant-source service
    ensured = await app.state.binding_ensure.ensure(
        TENANT_A, "agent", PRODUCT, licence_id=LICENCE_A, licence_token="tenant-a"
    )
    names = [t["name"] for t in ensured["toolkits"]]
    assert names == ["crm_kit"], ensured
    assert ensured["status"] == "created" and "token" in ensured
    rebound = await stack.call("POST", "/mcp/builder/toolkits/bind", json={})
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["products"][0]["status"] == "unchanged"
    assert "token" not in rebound.text


async def test_spec_must_be_json_and_is_rate_limited(stack):
    app = stack.app
    app.state.builder.fetcher, app.state.builder.registry = (
        FakeFetcher(b"openapi: 3.1.0"),
        FakeRegistry(),
    )
    await stack.call("POST", "/mcp/builder/sources", json=source_body())
    response = await stack.call("POST", "/mcp/builder/sources/crm/spec/fetch", json={})
    assert response.status_code == 422 and "YAML" in response.text
    bad = await stack.call("POST", "/mcp/builder/sources/crm/spec/fetch", json={"path": "/../x"})
    assert bad.status_code == 422
    app.state.builder.fetch_limit.limit = 1
    app.state.builder.fetch_limit.events.clear()
    app.state.builder.fetcher = FakeFetcher(json.dumps(SPEC).encode())
    assert (
        await stack.call("POST", "/mcp/builder/sources/crm/spec/fetch", json={})
    ).status_code == 200
    limited = await stack.call("POST", "/mcp/builder/sources/crm/spec/fetch", json={})
    assert limited.status_code == 429


async def test_asgi_wires_the_broker_document_fetcher(stack):
    assert isinstance(stack.app.state.builder.fetcher, Broker)


async def test_tool_test_and_run_get(stack):
    from src.execution.service import ExecutionService

    assert hasattr(ExecutionService, "submit_draft") and hasattr(ExecutionService, "draft_run")
    response = await stack.call(
        "POST",
        f"/mcp/builder/tools/{uuid7()}/versions/1/test",
        json={"inputs": {}},
    )
    assert response.status_code in {404, 503}
