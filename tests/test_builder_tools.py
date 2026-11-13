# ruff: noqa: F811
"""The builder_* MCP toolkit over /mcp: scope, schemas, secrets, audit, and the authoring flow."""

import json

import psycopg

from src.builder.tools import BUILDER_SERVERS
from src.builtin.scope import tool_names
from tests.test_builder_surface import (  # noqa: F401
    INPUT,
    SCRIPT,
    SECRET,
    SPEC,
    TENANT_A,
    TENANT_HEADERS,
    FakeFetcher,
    FakeRegistry,
    kc_env,
    stack,
)
from tests.test_mcp_catalog import rpc


async def call(stack, name, arguments, headers=None):
    response, body = await rpc(
        stack.client,
        "/mcp",
        "tools/call",
        {"name": name, "arguments": arguments},
        TENANT_HEADERS if headers is None else headers,
    )
    assert response.status_code == 200, body
    return body["result"]


def payload(result):
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


async def test_author_sees_only_builder_tools(stack):
    expected = await tool_names(BUILDER_SERVERS)
    response, body = await rpc(stack.client, "/mcp", "tools/list", {}, TENANT_HEADERS)
    assert response.status_code == 200, body
    tools = body["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == expected and len(names) == 20
    assert all(n.startswith("builder_") for n in names)
    assert {
        "builder_source_create",
        "builder_source_update",
        "builder_source_set_secret",
        "builder_source_list",
        "builder_source_disable",
        "builder_spec_fetch",
        "builder_spec_operations",
        "builder_operations_import",
        "builder_operation_list",
        "builder_operation_update",
        "builder_tool_create",
        "builder_tool_update",
        "builder_tool_test",
        "builder_run_get",
        "builder_tool_publish",
        "builder_toolkit_create",
        "builder_toolkit_publish",
        "builder_toolkit_bind",
    } <= names
    create = next(t for t in tools if t["name"] == "builder_tool_create")
    assert "kit.call" in create["description"] and "api_call_denied" in create["description"]
    assert set(create["inputSchema"]["required"]) >= {"public_name", "files", "effect"}


async def test_member_and_unknown_licences_get_nothing(stack):
    for header in ({"X-Tenant-Id": "Bearer member-a"}, {"X-Tenant-Id": "Bearer nope"}):
        response, _ = await rpc(stack.client, "/mcp", "tools/list", {}, header)
        assert response.status_code == 401


async def test_unknown_and_foreign_tools_are_denied(stack):
    denied = await call(stack, "crm_verify_customer", {"customer_id": "C-1"})
    assert denied["isError"] is True and "not permitted" in denied["content"][0]["text"]


async def test_strict_arguments(stack):
    result = await call(stack, "builder_source_list", {"limit": 5, "tenant_id": str(TENANT_A)})
    assert result["isError"] is True
    result = await call(stack, "builder_source_list", {"limit": 0})
    assert result["isError"] is True


async def test_authoring_over_mcp_never_echoes_secrets(stack, database_url):
    app = stack.app
    app.state.builder.fetcher = FakeFetcher(json.dumps(SPEC).encode())
    app.state.builder.registry = FakeRegistry()
    source = payload(
        await call(
            stack,
            "builder_source_create",
            {
                "source_key": "crm",
                "base_url": "https://crm.example.com/v1",
                "auth": {"kind": "static_bearer"},
                "token": SECRET,
                "spec_path": "/openapi.json",
            },
        )
    )
    assert source["has_token"] is True and SECRET not in json.dumps(source)
    rotated = await call(
        stack,
        "builder_source_set_secret",
        {
            "source_key": "crm",
            "token": "rotated-secret-xyz",
            "expected_revision": source["revision"],
        },
    )
    assert "rotated-secret-xyz" not in json.dumps(rotated)
    listing = await call(stack, "builder_source_list", {})
    assert SECRET not in json.dumps(listing) and "rotated-secret-xyz" not in json.dumps(listing)

    spec = payload(await call(stack, "builder_spec_fetch", {"source_key": "crm"}))
    imported = payload(
        await call(
            stack,
            "builder_operations_import",
            {"source_key": "crm", "spec_sha256": spec["sha256"], "select": ["get_customer"]},
        )
    )
    assert imported["imported"][0]["op_key"] == "get_customer"
    tool = payload(
        await call(
            stack,
            "builder_tool_create",
            {
                "public_name": "find_customer",
                "operation": "customer.find",
                "description": "Find a customer",
                "files": [{"path": "tool.py", "content": SCRIPT}],
                "input_schema": INPUT,
                "effect": "read_only",
                "ops": [{"source_key": "crm", "op_key": "get_customer"}],
            },
        )
    )
    assert tool["status"] == "draft" and tool["ops"] == ["crm.get_customer"]
    published = payload(
        await call(
            stack,
            "builder_tool_publish",
            {"tool_id": tool["tool_id"], "version": 1, "expected_revision": tool["revision"]},
        )
    )
    assert published["status"] == "published"
    # a bad request names the rule, not a value
    bad = await call(stack, "builder_tool_create", {"public_name": "Bad Name", "operation": "x"})
    assert bad["isError"] is True

    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT tool,status,principal_kind,auth_method,tenant_id,details::text "
            "FROM public.mcp_tool_call_audit ORDER BY audit_id"
        ).fetchall()
    assert {r[2] for r in rows} == {"tenant_author"} and {r[3] for r in rows} == {"license"}
    assert {r[4] for r in rows} == {str(TENANT_A)}
    assert "builder_source_set_secret" in {r[0] for r in rows}
    assert all(SECRET not in r[5] and "rotated-secret-xyz" not in r[5] for r in rows)
    assert any(r[1] == "failure" for r in rows)


async def test_tool_test_over_mcp(stack):
    result = await call(
        stack,
        "builder_tool_test",
        {"tool_id": "0195f48b-d000-7000-8000-000000000009", "version": 1},
    )
    # No such tool for this tenant (or execution is off in this stack): a clean tool error, with
    # no traceback or internals in the text.
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "Traceback" not in text and "psycopg" not in text


async def test_over_long_secret_is_never_echoed(stack, database_url, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    long_secret = "LEAKPREFIX" + "x" * 4200  # pragma: allowlist secret
    created = payload(
        await call(
            stack,
            "builder_source_create",
            {
                "source_key": "crm",
                "base_url": "https://crm.example.com/v1",
                "auth": {"kind": "static_bearer"},
                "token": SECRET,
            },
        )
    )
    mcp = await call(
        stack,
        "builder_source_set_secret",
        {"source_key": "crm", "token": long_secret, "expected_revision": created["revision"]},
    )
    assert mcp["isError"] is True and "LEAKPREFIX" not in json.dumps(mcp)
    rest = await stack.call(
        "PUT",
        "/mcp/builder/sources/crm/secret",
        json={"token": long_secret, "expected_revision": created["revision"]},
    )
    assert rest.status_code >= 400 and "LEAKPREFIX" not in rest.text
    too_long_create = await stack.call(
        "POST",
        "/mcp/builder/sources",
        json={
            "source_key": "other",
            "base_url": "https://other.example.com/v1",
            "auth": {"kind": "static_bearer"},
            "token": long_secret,
        },
    )
    assert too_long_create.status_code >= 400 and "LEAKPREFIX" not in too_long_create.text
    mcp_create = await call(
        stack,
        "builder_source_create",
        {
            "source_key": "other",
            "base_url": "https://other.example.com/v1",
            "auth": {"kind": "static_bearer"},
            "token": long_secret,
        },
    )
    assert mcp_create["isError"] is True and "LEAKPREFIX" not in json.dumps(mcp_create)

    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT a::text FROM public.mcp_tool_call_audit a").fetchall()
    assert len(rows) >= 3 and all("LEAKPREFIX" not in r[0] for r in rows)
    assert "LEAKPREFIX" not in caplog.text
