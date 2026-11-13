"""Three-principal resolver on the full app with PostgreSQL: builtin(principal) ∪ catalog(binding).

* binding (mcp licence + X-MCP-Binding): catalog tools only, never a builtin;
* keycloak service (azp allow-list): exactly its scope;
* care-bus tenant licence (app care-bus, subject_kind=tenant): care-bus + researcher scope;
* forged / unknown credentials: 401; a valid credential on the wrong path: 403.
Builtin calls are audited into mcp_tool_call_audit (0006), which the runtime role may only INSERT.
"""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from src.auth import keycloak
from src.builtin.carebus.client import set_client
from src.builtin.scope import DENIED_MESSAGE
from tests.test_authentication import info
from tests.test_bindings import LICENSE
from tests.test_carebus_mount import CONTRACT
from tests.test_catalog_http import catalog_api as catalog_api
from tests.test_keycloak_auth import _OTHER_KEY, CLIENT, _bearer, _token, kc_env  # noqa: F401
from tests.test_legal_tools import CONTRACT as LEGAL_CONTRACT
from tests.test_mcp_catalog import rpc, seed
from tests.test_research_tools import CONTRACT as RESEARCH_TOOLS_CONTRACT

RESEARCHER = "carebus-researcher"
RESEARCH_CONTRACT = LEGAL_CONTRACT | {"web_scrape"} | RESEARCH_TOOLS_CONTRACT
CAREBUS_TENANT = "0195f48b-d000-7000-8000-0000000000c1"
CAREBUS_LIC = {"X-Tenant-Id": "Bearer carebus-tenant"}


def _carebus(kind: str | None) -> dict:
    out = {
        "active": True,
        "tenant_id": CAREBUS_TENANT,
        "subject_id": "carebus-subject",
        "app": {"key": "care-bus"},
    }
    if kind is not None:
        out["subject_kind"] = kind
    return out


@pytest.fixture
async def api(catalog_api, kc_env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENTS", f"{CLIENT}, other-client, {RESEARCHER}")
    keycloak.get_keycloak_config.cache_clear()
    app, client = catalog_api

    def answer(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["token"]
        if token == "carebus-tenant":
            return httpx.Response(200, json=_carebus("tenant"))
        if token == "carebus-member":
            return httpx.Response(200, json=_carebus("member"))
        if token == "revoked":
            return httpx.Response(200, json={"active": False})
        return httpx.Response(200, json=info(license_id=str(LICENSE), app={"key": "mcp"}))

    await app.state.licenses.client.aclose()
    app.state.licenses.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    yield app, client


async def _names(client, headers) -> set[str]:
    response, body = await rpc(client, "/mcp", "tools/list", {}, headers)
    assert response.status_code == 200, body
    assert body["result"].get("nextCursor") is None
    return {t["name"] for t in body["result"]["tools"]}


async def _call(client, headers, name: str, arguments: dict) -> dict:
    response, body = await rpc(
        client, "/mcp", "tools/call", {"name": name, "arguments": arguments}, headers
    )
    assert response.status_code == 200, body
    return body["result"]


def _audit_rows(database_url: str) -> list[tuple]:
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT tool,status,principal_kind,auth_method,principal,tenant_id,details "
            "FROM public.mcp_tool_call_audit ORDER BY created_at,audit_id"
        ).fetchall()


async def test_binding_principal_sees_catalog_tools_only(api):
    app, client = api
    _, headers = await seed(app, client)
    assert await _names(client, headers) == {"synthetic_tool"}
    # A builtin name is not resolvable through a binding: the catalog refusal, not a builtin run.
    result = await _call(client, headers, "crm_verify_customer", {"customer_id": "C-1"})
    assert result["isError"] is True
    assert result["content"][0]["text"] == "tool_request_denied"


async def test_keycloak_service_sees_exactly_its_scope(api):
    _, client = api
    assert await _names(client, _bearer(_token())) == CONTRACT
    assert await _names(client, _bearer(_token(azp=RESEARCHER))) == RESEARCH_CONTRACT
    assert await _names(client, _bearer(_token(azp="other-client"))) == set()


async def test_carebus_tenant_licence_sees_carebus_scope(api):
    _, client = api
    assert await _names(client, CAREBUS_LIC) == CONTRACT | RESEARCH_CONTRACT
    assert await _names(client, {"X-Tenant-Id": "Bearer carebus-member"}) == set()


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/mcp", {}, 401),
        ("/mcp", {"Authorization": "Bearer not-a-jwt"}, 401),
        ("/mcp", {"Authorization": f"Bearer {_token(key=_OTHER_KEY)}"}, 401),
        ("/mcp", {"Authorization": f"Bearer {_token(azp='portal-frontend')}"}, 401),
        ("/mcp", {"X-Tenant-Id": "Bearer revoked"}, 401),
        ("/mcp", {"X-Tenant-Id": "Bearer gateway"}, 401),  # mcp licence without a binding
        ("/mcp", {"X-Tenant-Id": "0195f48b-d000-7000-8000-000000000001"}, 401),  # bare tenant
        ("/mcp/tools", {"Authorization": f"Bearer {_token()}"}, 403),
        ("/mcp/runs", {"Authorization": f"Bearer {_token()}"}, 403),
        ("/mcp/runs", CAREBUS_LIC, 403),
    ],
)
async def test_forged_unknown_or_misplaced_credentials_are_refused(api, path, headers, status):
    _, client = api
    response, _ = await rpc(client, path, "tools/list", {}, headers)
    assert response.status_code == status


async def test_builtin_calls_are_audited_append_only(api, database_url, monkeypatch):
    _, client = api
    monkeypatch.delenv("DEMO_BACKENDS_URL", raising=False)
    set_client("demo_backends", None)
    result = await _call(client, _bearer(_token()), "crm_verify_customer", {"customer_id": "C-1"})
    # reaches the tool (which reports its own missing config), not the scope refusal
    assert result["isError"] is True
    assert "DEMO_BACKENDS_URL" in result["content"][0]["text"]
    denied = await _call(client, CAREBUS_LIC, "tools_add", {"a": 1, "b": 2})
    assert denied["content"][0]["text"] == DENIED_MESSAGE
    rows = _audit_rows(database_url)
    assert [r[:6] for r in rows] == [
        (
            "crm_verify_customer",
            "failure",
            "keycloak_service",
            "keycloak",
            CLIENT,
            None,
        ),
        (
            "tools_add",
            "denied",
            "carebus_licence",
            "license",
            "carebus-subject",
            CAREBUS_TENANT,
        ),
    ]
    assert rows[1][6]["denied"] is True
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("DELETE FROM public.mcp_tool_call_audit")


async def test_runtime_role_may_only_insert_audit_rows(api, database_url):
    app, _ = api
    runtime = make_url(app.state.settings.database_url.get_secret_value())
    dsn = runtime.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.mcp_tool_call_audit "
            "(tool,status,principal_kind,auth_method,principal,latency_ms) "
            "VALUES ('faq_ask','success','keycloak_service','keycloak','x',1)"
        )
        for statement in (
            "SELECT * FROM public.mcp_tool_call_audit",
            "UPDATE public.mcp_tool_call_audit SET tool='y'",
            "DELETE FROM public.mcp_tool_call_audit",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql.SQL(statement))
