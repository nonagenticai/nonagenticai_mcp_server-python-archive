"""Least privilege per caller, end to end over HTTP: AuthenticationMiddleware -> /mcp transport.

Ported from main #41/#43/#46 onto the builtin provider. A Keycloak-JWT caller lists and calls only
its principal's tools (carebus-tool-executor: the 13 care-bus tools; carebus-researcher: legal_* +
web_* + research_*; any other azp: none) and may not use /mcp/... REST routes. A licence caller
without X-MCP-Binding is scoped by its licence's app: `care-bus` (tenant-level) gets the care-bus +
researcher tools, a non-tenant care-bus licence nothing.

Not preserved from main (removed by design, see docs/AUTHENTICATION_BINDINGS.md): the `mcp`
licence "everything but care-bus" surface (an mcp licence now needs X-MCP-Binding and sees catalog
tools only, so without one it is 401), the claude_* / tools_* servers, and resources/prompts.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.auth import keycloak
from src.builtin.scope import (
    DENIED_MESSAGE,
    PRINCIPAL_SCOPES,
    carebus_tool_names,
    tool_names,
)
from tests.builtin_helpers import ACCEPT, RecordingSink, builtin_stack, parse
from tests.carebus_helpers import Recorder
from tests.test_carebus_mount import CONTRACT
from tests.test_keycloak_auth import (  # noqa: F401
    CLIENT,
    TENANT,
    _bearer,
    _licences,
    _token,
    kc_env,
)
from tests.test_legal_tools import CONTRACT as LEGAL_CONTRACT
from tests.test_research_tools import CONTRACT as RESEARCH_TOOLS_CONTRACT


def _lic(app: str | None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"active": True, "tenant_id": TENANT, "subject_id": "lic", **extra}
    if app is not None:
        out["app"] = {"key": app, "name": app}
    return out


# Introspection answers by token: the licence's app decides the scope.
LICENCES: dict[str, dict[str, Any]] = {
    "lic-1": _lic("mcp"),
    "l": _lic("mcp"),
    "lic-carebus": _lic("care-bus", subject_kind="tenant", subject_keycloak_sub=None),
    "lic-carebus-person": _lic(
        "care-bus",
        subject_kind="member",
        subject_keycloak_sub="0195f48b-0000-7000-8000-00000000abcd",
    ),
    # a per-login licence whose Keycloak id does not qualify: introspection's sub is null too
    "lic-carebus-member-nullsub": _lic(
        "care-bus", subject_kind="member", subject_keycloak_sub=None
    ),
    # a licenses build before #305: no subject_kind at all
    "lic-carebus-no-kind": _lic("care-bus", subject_keycloak_sub=None),
    "lic-carebus-odd-kind": _lic("care-bus", subject_kind="Tenant"),
    "lic-other": _lic("w2v"),
    "lic-noapp": _lic(None),
}
MCP_LIC = {"X-Tenant-Id": "Bearer lic-1"}
CAREBUS_LIC = {"X-Tenant-Id": "Bearer lic-carebus"}
RESEARCHER = "carebus-researcher"
RESEARCH_CONTRACT = LEGAL_CONTRACT | {"web_scrape"} | RESEARCH_TOOLS_CONTRACT


@pytest.fixture
def audit_db() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def client(kc_env, audit_db, monkeypatch) -> Iterator[TestClient]:  # noqa: F811
    # Keycloak lets three clients in; the scope map knows only two of them ("other-client" is
    # authenticated but has no scope).
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENTS", f"{CLIENT}, other-client, {RESEARCHER}")
    keycloak.get_keycloak_config.cache_clear()
    with TestClient(builtin_stack(_licences(LICENCES), audit_db)) as c:
        yield c


def _rpc(client: TestClient, headers: dict[str, str], method: str, params: dict) -> dict:
    r = client.post(
        "/mcp",
        headers={**ACCEPT, **headers},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert r.status_code == 200, r.text
    return parse(r)


def _none_listed(out: dict, key: str) -> bool:
    """This server advertises tools only: resources/prompts are absent (error) or empty."""
    return "error" in out or out["result"][key] == []


def _names(client: TestClient, headers: dict[str, str]) -> set[str]:
    return {t["name"] for t in _rpc(client, headers, "tools/list", {})["result"]["tools"]}


def test_allowed_set_is_derived_from_mount_carebus_and_equals_the_contract():
    import asyncio

    assert asyncio.run(carebus_tool_names()) == CONTRACT


def test_jwt_caller_lists_exactly_the_13_carebus_tools(client):
    assert _names(client, _bearer(_token())) == CONTRACT


def test_researcher_lists_exactly_legal_web_and_research_tools(client):
    assert _names(client, _bearer(_token(azp=RESEARCHER))) == RESEARCH_CONTRACT


def test_researcher_scope_is_derived_from_the_mounts():
    import asyncio

    assert asyncio.run(tool_names(PRINCIPAL_SCOPES[RESEARCHER])) == RESEARCH_CONTRACT


def test_unknown_azp_lists_no_tools(client):
    assert _names(client, _bearer(_token(azp="other-client"))) == set()


def test_unknown_azp_call_is_denied_and_audited(client, audit_db):
    out = _rpc(
        client,
        _bearer(_token(azp="other-client")),
        "tools/call",
        {"name": "crm_verify_customer", "arguments": {"customer_id": "C-1"}},
    )
    assert out["result"]["isError"] is True
    assert out["result"]["content"][0]["text"] == DENIED_MESSAGE
    assert [(r["resource_id"], r["status"]) for r in audit_db.rows] == [
        ("crm_verify_customer", "denied")
    ]
    assert audit_db.rows[0]["details"]["principal"] == "other-client"


@pytest.mark.parametrize(
    ("azp", "tool", "arguments"),
    [
        (RESEARCHER, "crm_verify_customer", {"customer_id": "C-1"}),
        (RESEARCHER, "claude_claude_code", {"prompt": "id"}),
        (CLIENT, "legal_eli_act", {"eli": "DU/1964/59"}),
        (CLIENT, "web_scrape", {"url": "https://example.com/"}),
        (CLIENT, "research_stats_trend", {"data": []}),
        ("other-client", "research_literature_search", {"query": "x"}),
    ],
)
def test_principals_cannot_call_each_others_tools(client, azp, tool, arguments):
    out = _rpc(
        client,
        _bearer(_token(azp=azp)),
        "tools/call",
        {"name": tool, "arguments": arguments},
    )
    assert out["result"]["content"][0]["text"] == DENIED_MESSAGE


def test_researcher_legal_call_is_not_refused(client):
    out = _rpc(
        client,
        _bearer(_token(azp=RESEARCHER)),
        "tools/call",
        {"name": "legal_eli_act", "arguments": {"eli": "../admin"}},
    )
    # reaches the tool (which rejects the id itself), not the scope refusal
    text = out["result"]["content"][0]["text"]
    assert text != DENIED_MESSAGE
    assert text.startswith("[permanent] invalid eli")


def test_researcher_research_call_is_not_refused(client):
    out = _rpc(
        client,
        _bearer(_token(azp=RESEARCHER)),
        "tools/call",
        {"name": "research_literature_record", "arguments": {"id": "../admin"}},
    )
    # reaches the tool (which rejects the id itself), not the scope refusal
    text = out["result"]["content"][0]["text"]
    assert text != DENIED_MESSAGE
    assert text.startswith("[permanent] invalid id")


@pytest.mark.parametrize("token", ["lic-1", "lic-other", "lic-noapp"])
def test_non_carebus_licence_without_binding_is_401(client, token):
    # main: mcp -> everything but care-bus, others -> nothing. Here an mcp licence needs
    # X-MCP-Binding (catalog only); a licence of any other app has no builtin scope.
    r = client.post(
        "/mcp",
        headers={**ACCEPT, "X-Tenant-Id": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert r.status_code == 401


def test_mcp_licence_cannot_call_faq_ask_or_any_carebus_tool(client, audit_db):
    for name, args in [
        ("faq_ask", {"question": "q", "leaf_ids": ["7f1c9a2e-0000-4000-8000-0000000000a1"]}),
        ("crm_verify_customer", {"customer_id": "C-1"}),
    ]:
        r = client.post(
            "/mcp",
            headers={**ACCEPT, **MCP_LIC},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        assert r.status_code == 401
    assert audit_db.rows == []


def test_carebus_licence_lists_exactly_carebus_and_researcher_tools(client):
    assert _names(client, CAREBUS_LIC) == CONTRACT | RESEARCH_CONTRACT


@pytest.mark.parametrize(
    "token",
    [
        "lic-carebus-person",
        "lic-carebus-member-nullsub",
        "lic-carebus-no-kind",
        "lic-carebus-odd-kind",
    ],
)
def test_other_unknown_or_person_licence_gets_no_tools(client, token):
    headers = {"X-Tenant-Id": f"Bearer {token}"}
    assert _names(client, headers) == set()
    out = _rpc(client, headers, "tools/call", {"name": "tools_add", "arguments": {"a": 1, "b": 2}})
    assert out["result"]["content"][0]["text"] == DENIED_MESSAGE
    assert _none_listed(_rpc(client, headers, "resources/list", {}), "resources")


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("claude_claude_code", {"prompt": "id"}),
        ("tools_add", {"a": 1, "b": 2}),
    ],
)
def test_carebus_licence_cannot_call_claude_or_tools(client, tool, arguments):
    out = _rpc(client, CAREBUS_LIC, "tools/call", {"name": tool, "arguments": arguments})
    assert out["result"]["content"][0]["text"] == DENIED_MESSAGE


def test_carebus_licence_gets_no_resources_or_prompts(client):
    assert _none_listed(_rpc(client, CAREBUS_LIC, "prompts/list", {}), "prompts")
    assert _none_listed(_rpc(client, CAREBUS_LIC, "resources/list", {}), "resources")
    read = _rpc(client, CAREBUS_LIC, "resources/read", {"uri": "secret://config"})
    assert "error" in read  # no resources exist on this server at all


def test_jwt_call_to_claude_tool_is_refused_and_audited_as_denied(client, audit_db):
    out = _rpc(
        client,
        _bearer(_token()),
        "tools/call",
        {"name": "claude_claude_code", "arguments": {"prompt": "id"}},
    )
    result = out["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == DENIED_MESSAGE
    assert [(r["resource_id"], r["status"]) for r in audit_db.rows] == [
        ("claude_claude_code", "denied")
    ]
    assert audit_db.rows[0]["details"]["denied"] is True
    assert audit_db.rows[0]["details"]["auth_method"] == "keycloak"


def test_jwt_carebus_tool_call_is_not_refused(client, monkeypatch):
    monkeypatch.delenv("DEMO_BACKENDS_URL", raising=False)
    from src.builtin.carebus.client import set_client

    set_client("demo_backends", None)
    out = _rpc(
        client,
        _bearer(_token()),
        "tools/call",
        {"name": "crm_verify_customer", "arguments": {"customer_id": "C-1"}},
    )
    # reaches the tool (which then reports its own missing config), not the scope refusal
    assert out["result"]["content"][0]["text"] != DENIED_MESSAGE
    assert "DEMO_BACKENDS_URL" in out["result"]["content"][0]["text"]


@pytest.mark.parametrize("tenant_claim", [None, TENANT])
def test_jwt_caller_cannot_use_admin_rest_routes(client, tenant_claim):
    token = _token(tenant_id=tenant_claim) if tenant_claim else _token()
    r = client.get("/mcp/available-tools", headers=_bearer(token))
    assert r.status_code == 403


# --- resources / prompts ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "key"),
    [
        ("resources/list", "resources"),
        ("resources/templates/list", "resourceTemplates"),
        ("prompts/list", "prompts"),
    ],
)
def test_jwt_caller_lists_no_resources_or_prompts(client, method, key):
    assert _none_listed(_rpc(client, _bearer(_token()), method, {}), key)


def test_jwt_caller_cannot_read_resource_or_get_prompt(client):
    jwt_h = _bearer(_token())
    assert "error" in _rpc(client, jwt_h, "resources/read", {"uri": "secret://config"})
    assert "error" in _rpc(client, jwt_h, "prompts/get", {"name": "internal_prompt"})


# --- per-tenant strata for the care-bus tools (X-Strata-Token) --------------------------------

RAG = "http://strata-rag.test"
ASK_OK = {"answer": "a", "citations": [], "results": []}


@pytest.fixture
def rag() -> Iterator[Recorder]:
    from src.builtin.carebus.client import ServiceClient, set_client

    rec = Recorder({("POST", "/api/ask"): (200, ASK_OK)})
    set_client("strata_rag", ServiceClient(RAG, "pinned-static-token", transport=rec.transport))
    yield rec
    set_client("strata_rag", None)


def _ask(client: TestClient, headers: dict[str, str]) -> dict:
    return _rpc(client, headers, "tools/call", {"name": "faq_ask", "arguments": {"question": "q"}})


@pytest.mark.parametrize("value", ["tenant-strata-licence", "Bearer tenant-strata-licence"])
def test_carebus_licence_faq_ask_searches_its_tenant_with_the_forwarded_token(client, rag, value):
    out = _ask(client, {**CAREBUS_LIC, "X-Strata-Token": value})
    assert out["result"].get("isError") is not True, out
    sent = rag.requests[-1]
    assert sent.headers["authorization"] == "Bearer tenant-strata-licence"
    assert sent.headers["x-tenant-id"] == TENANT
    assert "pinned-static-token" not in str(sent.headers)


def test_carebus_licence_without_strata_token_fails_closed(client, rag):
    out = _ask(client, CAREBUS_LIC)
    text = out["result"]["content"][0]["text"]
    assert out["result"]["isError"] is True
    assert text.startswith("[permanent] X-Strata-Token")
    assert rag.requests == []  # never the pinned tenant


def test_keycloak_faq_ask_keeps_the_pinned_token(client, rag):
    out = _ask(client, {**_bearer(_token()), "X-Strata-Token": "ignored"})
    assert out["result"].get("isError") is not True, out
    sent = rag.requests[-1]
    assert sent.headers["authorization"] == "Bearer pinned-static-token"
    assert "x-tenant-id" not in sent.headers
