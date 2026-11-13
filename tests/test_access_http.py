"""Application and mounted ASGI tests exercise the same authentication boundary.

The transport fixture is deliberately not a FastMCP substitute: real provider/
transport acceptance belongs to stage 3. Here it proves mounted ASGI cannot bypass
identity, reconnect/session checks, management authorization, or revocation.
"""

import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from src.asgi import create_app
from src.auth.principal import Principal
from src.auth.protocol import header
from src.dependencies import get_operator
from src.toolkits.models import BindingSpec, BoundContext, ToolkitReference
from tests.fixtures import ACTOR, FILES, TENANT_A, TENANT_B, TOOL, settings, tool
from tests.test_authentication import info
from tests.test_bindings import LICENSE, PRINCIPAL, profile, selection, toolkit


@pytest.fixture
async def http_api(database_url, tmp_path):
    key = tmp_path / "admin"
    key.write_text("a" * 32)
    app = create_app(
        settings(
            database_url=database_url,
            license_introspect_url="https://licenses.example/introspect",
            license_app_key="mcp",
            admin_key_file=key,
            admin_tenant_ids={TENANT_A},
            max_management_bytes=4096,
        )
    )

    @app.api_route("/mcp/sse/test", methods=["GET", "POST", "DELETE"])
    async def transport(request: Request):
        principal = request.state.principal
        assert isinstance(principal, Principal) and not principal.management
        with pytest.raises(FrozenInstanceError):
            principal.subject = "forged"
        if request.headers.get("x-test-stream"):

            async def stream():
                yield b"event: message\ndata: {}\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")
        return JSONResponse(
            {"binding_id": str(principal.binding_id)},
            headers={}
            if request.headers.get("mcp-session-id")
            else {"mcp-session-id": "transport-session"},
        )

    from tests.lifespan import running_app

    # Synthetic phase-2 transport must precede the real protocol mount.
    app.router.routes.insert(0, app.router.routes.pop())
    async with running_app(app):
        await app.state.licenses.client.aclose()

        def introspect(req):
            payload = json.loads(req.content)
            token = payload["token"]
            if token == "outage":
                raise httpx.ConnectError("private-dsn-and-credential")
            return httpx.Response(
                200,
                json=info(
                    license_id=str(LICENSE),
                    tenant_id=str(TENANT_B if token == "foreign" else TENANT_A),
                    app={"key": "wrong" if token == "wrong-app" else "mcp"},
                ),
            )

        app.state.licenses.client = httpx.AsyncClient(transport=httpx.MockTransport(introspect))
        await app.state.catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
        await app.state.catalog.transition(
            TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield app, client


ADMIN = {"X-Tenant-Id": "Bearer gateway", "X-MCP-Admin-Key": "a" * 32}


async def test_http_operator_toolkit_profile_and_binding_lifecycle(http_api):
    app, client = http_api
    credential = profile()
    response = await client.post(
        "/mcp/credential-profiles", headers=ADMIN, json=credential.model_dump(mode="json")
    )
    assert response.status_code == 201 and response.json()["credentials_verified"] is False
    path = f"/mcp/credential-profiles/{credential.profile_id}"
    assert (await client.post(path + "/validate", headers=ADMIN)).json()["metadata_valid"]
    assert (
        await client.put(
            path,
            headers=ADMIN,
            json={"profile": credential.model_dump(mode="json"), "expected_revision": 1},
        )
    ).status_code == 200
    manifest = toolkit()
    response = await client.post(
        "/mcp/toolkits", headers=ADMIN, json=manifest.model_dump(mode="json")
    )
    assert response.status_code == 201
    path = f"/mcp/toolkits/{manifest.toolkit_id}/versions"
    response = await client.post(path + "/1/publish", headers=ADMIN, json={"expected_revision": 1})
    assert response.status_code == 200
    assert (await client.get("/mcp/toolkits", headers=ADMIN)).json()["items"][0][
        "name"
    ] == manifest.name
    assert (await client.get(path + "/1", headers=ADMIN)).json()["status"] == "published"
    assert (
        await client.post(
            path,
            headers=ADMIN,
            json={"manifest": manifest.model_dump(mode="json"), "expected_revision": 2},
        )
    ).json()["manifest"]["version"] == 2
    spec = BindingSpec(
        credential_profile_id=credential.profile_id,
        toolkits=(ToolkitReference(id=manifest.toolkit_id, version=1),),
        context=BoundContext(tenant_id=TENANT_A),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    response = await client.post("/mcp/bindings", headers=ADMIN, json=spec.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    payload = response.json()
    token = payload["token"]
    binding_id = payload["binding"]["binding_id"]
    path = f"/mcp/bindings/{binding_id}"
    agent = {"X-Tenant-Id": "Bearer gateway", "X-MCP-Binding": token}
    assert (await client.get("/mcp/available-tools", headers=agent)).json()["tools"][0][
        "name"
    ] == "synthetic_tool"
    # Off-cluster through the ingress: the edge replaces X-Tenant-Id with a bare tenant id, so
    # the licence arrives as Authorization: Bearer; it is authenticated, not trusted.
    edge = {"X-Tenant-Id": str(TENANT_A), "Authorization": "Bearer gateway", "X-MCP-Binding": token}
    assert (await client.get("/mcp/available-tools", headers=edge)).status_code == 200
    forged = {**edge, "Authorization": "Bearer foreign"}
    assert (await client.get("/mcp/available-tools", headers=forged)).status_code == 401
    assert (
        await client.get("/mcp/available-tools", headers={"X-MCP-Binding": token})
    ).status_code == 401
    # Management stays X-Tenant-Id only: a bearer in Authorization is not an operator licence.
    management = {k: v for k, v in ADMIN.items() if k.lower() != "x-tenant-id"}
    assert (
        await client.get("/mcp/toolkits", headers={**management, "Authorization": "Bearer gateway"})
    ).status_code == 401
    assert (await client.post("/mcp", headers=agent, json={})).json()[
        "error"
    ] == "invalid_protocol_request"
    assert (await client.get(path, headers=ADMIN)).status_code == 200
    assert token not in (await client.get(path, headers=ADMIN)).text
    assert (await client.get(path + "/effective-tools", headers=ADMIN)).status_code == 200
    assert (
        await client.patch(
            path, headers=ADMIN, json={"spec": spec.model_dump(mode="json"), "expected_revision": 1}
        )
    ).json()["revision"] == 2
    stale = await client.patch(
        path, headers=ADMIN, json={"spec": spec.model_dump(mode="json"), "expected_revision": 1}
    )
    assert stale.status_code == 409
    assert (
        await client.post(path + "/rotate-token", headers=ADMIN, json={"expected_revision": 2})
    ).json()["token"] != token
    assert (await client.get("/mcp/available-tools", headers=agent)).status_code == 401
    assert (
        await client.post(path + "/revoke", headers=ADMIN, json={"expected_revision": 3})
    ).json()["revoked"] is True
    assert (
        await client.post(
            f"/mcp/toolkits/{manifest.toolkit_id}/versions/1/disable",
            headers=ADMIN,
            json={"expected_revision": 3},
        )
    ).status_code == 200
    assert (await client.get("/mcp/toolkits?limit=101", headers=ADMIN)).status_code == 422
    assert (await client.get(f"/mcp/bindings/{uuid7()}", headers=ADMIN)).status_code == 404


async def test_http_mounted_session_sse_reconnect_delete_and_revocation(http_api):
    app, client = http_api
    spec, _, _ = await selection(app.state.access)
    binding, token = await app.state.access.binding_write(PRINCIPAL, spec)
    agent = {"X-Tenant-Id": "Bearer gateway", "X-MCP-Binding": token}
    response = await client.post("/mcp/sse/test", headers=agent, json={})
    assert response.status_code == 200 and response.headers["mcp-session-id"] == "transport-session"
    session = {**agent, "Mcp-Session-Id": "transport-session"}
    assert (
        await client.get("/mcp/sse/test", headers={**session, "x-test-stream": "true"})
    ).text.startswith("event: message")
    second, second_token = await app.state.access.binding_write(PRINCIPAL, spec)
    switched = {**session, "X-MCP-Binding": second_token}
    assert (await client.get("/mcp/sse/test", headers=switched)).status_code == 401
    assert (
        await client.get("/mcp/sse/test", headers={**agent, "Mcp-Session-Id": "guessed"})
    ).status_code == 401
    assert (await client.delete("/mcp/sse/test", headers=session)).status_code == 200
    assert (await client.get("/mcp/sse/test", headers=session)).status_code == 401
    assert (await client.post("/mcp/sse/test", headers=agent, json={})).status_code == 200
    await app.state.access.binding_action(PRINCIPAL, binding.binding_id, 1, revoke=True)
    assert (await client.get("/mcp/sse/test", headers=session)).status_code == 401
    assert (await client.get("/mcp/sse/test", headers=agent)).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/mcp/sse",
        "/mcp/messages",
        "/mcp/available-tools",
        "/mcp/runs",
        "/mcp/bindings",
        "/mcp/tools",
        "/mcp/services",
    ],
)
async def test_every_protocol_and_rest_request_requires_own_credentials(http_api, path):
    _, client = http_api
    for method in ["GET", "POST", "DELETE"]:
        for headers in [
            {},
            {"Authorization": "Bearer forwarded-jwt"},
            {"X-Tenant-Id": "Bearer gateway"},
            {"X-Tenant-Id": str(TENANT_A), "X-MCP-Admin-Key": "a" * 32, "X-MCP-Binding": "guess"},
        ]:
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 401
    assert (await client.get("/health")).status_code == 200


async def test_binding_is_not_operator_and_invalid_headers_or_authority_fail_closed(http_api):
    app, client = http_api
    spec, _, _ = await selection(app.state.access)
    binding, token = await app.state.access.binding_write(PRINCIPAL, spec)
    agent = {"X-Tenant-Id": "Bearer gateway", "X-MCP-Binding": token}
    assert (await client.post("/mcp/toolkits", headers=agent, json={})).status_code == 401
    for gateway in ["wrong-app", "foreign", "outage"]:
        response = await client.get(
            "/mcp/available-tools", headers={**agent, "X-Tenant-Id": "Bearer " + gateway}
        )
        assert response.status_code == (503 if gateway == "outage" else 401)
        assert "private-dsn" not in response.text
    assert (
        await client.get("/mcp/toolkits", headers={**ADMIN, "X-MCP-Admin-Key": "wrong"})
    ).status_code == 401
    assert (
        await client.get("/mcp/toolkits", headers={**ADMIN, "X-Tenant-Id": "Bearer foreign"})
    ).status_code == 401
    duplicate = [(k, v) for k, v in ADMIN.items()] + [("x-tenant-id", "Bearer gateway")]
    assert (await client.get("/mcp/toolkits", headers=duplicate)).status_code == 401
    duplicate = [(k, v) for k, v in agent.items()] + [("x-mcp-binding", token)]
    assert (await client.get("/mcp/available-tools", headers=duplicate)).status_code == 401
    response = await client.post(
        "/mcp/toolkits", headers=ADMIN, json={"unknown": "sensitive-value"}
    )
    assert response.status_code == 422 and "sensitive-value" not in response.text
    response = await client.post("/mcp/toolkits", headers=ADMIN, content=b"x" * 4097)
    assert response.status_code == 413
    payload = toolkit(tenant=TENANT_B).model_dump(mode="json")
    assert (await client.post("/mcp/toolkits", headers=ADMIN, json=payload)).status_code == 403
    # The key/header path is separate from native/forwarded Authorization.
    assert (
        await client.get("/mcp/toolkits", headers={**ADMIN, "Authorization": "Bearer unrelated"})
    ).status_code == 200


async def test_http_identity_mismatch_requests_are_rejected(http_api):
    _, client = http_api
    manifest = toolkit()
    payload = {"manifest": manifest.model_dump(mode="json"), "expected_revision": 1}
    assert (
        await client.post(f"/mcp/toolkits/{uuid7()}/versions", headers=ADMIN, json=payload)
    ).status_code == 422
    credential = profile()
    payload = {"profile": credential.model_dump(mode="json"), "expected_revision": 1}
    assert (
        await client.put(f"/mcp/credential-profiles/{uuid7()}", headers=ADMIN, json=payload)
    ).status_code == 422
    assert (
        await client.get("/mcp/bindings/10000000-0000-4000-8000-000000000001", headers=ADMIN)
    ).status_code == 422


def test_dependency_does_not_accept_anonymous_or_binding_principals():
    from fastapi import HTTPException

    for state in [{}, {"principal": Principal(TENANT_A, LICENSE, "subject")}]:
        request = Request({"type": "http", "state": state})
        with pytest.raises(HTTPException):
            get_operator(request)


@pytest.mark.parametrize(
    "name,value",
    [
        (b"x-mcp-binding", b"bad token"),
        (b"x-mcp-binding", b"x" * 4097),
        (b"mcp-session-id", b"\x00"),
        (b"x-tenant-id", b"Bearer \xff"),
    ],
)
def test_header_parser_rejects_ambiguous_values(name, value):
    from src.auth.licenses import AuthenticationError

    with pytest.raises(AuthenticationError):
        header({"headers": [(name, value)]}, name)
