"""Per-call audit of builtin tool calls (ported from main's tests/test_tool_call_audit.py).

Same semantics as main's ToolCallAuditMiddleware: one row per call; success / failure / denied;
the original error text is returned unchanged; cancellation is a failure row; the write is
bounded and shielded and an audit failure never breaks the call. Rows go to
mcp_tool_call_audit (0006) instead of audit_logs; tenant/auth_method/principal are in details.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import anyio
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

from src.auth.principal import CARE_BUS_LICENCE, KEYCLOAK_SERVICE, CallerPrincipal
from src.builtin import audit
from src.builtin.provider import BuiltinProvider
from src.builtin.scope import DENIED_MESSAGE
from tests.builtin_helpers import RecordingSink

TENANT = "0195f48b-d000-7000-8000-000000000001"
LICENCE_CALLER = CallerPrincipal(CARE_BUS_LICENCE, "subject-1", "licence:care-bus", TENANT)
KC_CALLER = CallerPrincipal(KEYCLOAK_SERVICE, "carebus-tool-executor", "carebus-tool-executor")


class _AllowDemo:
    async def allowed(self, scope: str) -> frozenset[str]:
        return frozenset({"demo_ok", "demo_boom", "demo_hang", "demo_empty"})


class _SlowSink(RecordingSink):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def write(
        self, principal: CallerPrincipal, tool: str, status: str, details: dict[str, Any]
    ) -> None:
        await asyncio.sleep(self.delay)
        await super().write(principal, tool, status, details)


def _provider(sink: Any) -> BuiltinProvider:
    sub = FastMCP(name="s")

    @sub.tool()
    def ok(x: int) -> dict:
        return {"x": x}

    @sub.tool()
    def boom() -> dict:
        raise ToolError("[permanent] 404 from GET /api/orders/X: not found")

    @sub.tool()
    async def hang() -> dict:
        await asyncio.sleep(10)
        return {}

    @sub.tool()
    def empty() -> dict:
        raise RuntimeError()

    root = FastMCP(name="t", mask_error_details=False)
    root.mount(sub, namespace="demo")
    provider = BuiltinProvider(server=root, sink=sink)
    provider.scopes = _AllowDemo()  # ty: ignore[invalid-assignment]
    return provider


def _call(provider: BuiltinProvider, name: str, args: dict, caller=LICENCE_CALLER):
    return asyncio.run(provider.call_tool(caller, name, args))


def test_success_writes_one_execute_row():
    sink = RecordingSink()
    out = _call(_provider(sink), "demo_ok", {"x": 1})
    assert out.isError is False and out.structuredContent == {"x": 1}
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert (row["resource_id"], row["status"]) == ("demo_ok", "success")
    assert row["details"]["ok"] is True and row["details"]["tool"] == "demo_ok"
    assert isinstance(row["details"]["latency_ms"], int)


def test_failure_writes_row_and_returns_original_error():
    sink = RecordingSink()
    out = _call(_provider(sink), "demo_boom", {})
    assert out.isError is True
    assert isinstance(out.content[0], TextContent)
    assert out.content[0].text.startswith("[permanent] 404")
    assert len(sink.rows) == 1
    assert sink.rows[0]["status"] == "failure"
    assert sink.rows[0]["details"]["ok"] is False
    assert sink.rows[0]["details"]["error"].startswith("[permanent] 404")


def test_out_of_scope_call_is_denied_and_audited():
    sink = RecordingSink()
    provider = BuiltinProvider(sink=sink)
    out = asyncio.run(
        provider.call_tool(
            CallerPrincipal(KEYCLOAK_SERVICE, "other-client", "other-client"),
            "crm_verify_customer",
            {"customer_id": "C-1"},
        )
    )
    assert out.isError is True
    assert isinstance(out.content[0], TextContent) and out.content[0].text == DENIED_MESSAGE
    assert [(r["resource_id"], r["status"]) for r in sink.rows] == [
        ("crm_verify_customer", "denied")
    ]
    assert sink.rows[0]["details"]["denied"] is True
    assert sink.rows[0]["details"]["principal"] == "other-client"


def test_audit_write_failure_never_breaks_the_tool_call():
    class _Broken(RecordingSink):
        async def write(
            self, principal: CallerPrincipal, tool: str, status: str, details: dict[str, Any]
        ) -> None:
            raise RuntimeError("db down")

    out = _call(_provider(_Broken()), "demo_ok", {"x": 2})
    assert out.structuredContent == {"x": 2}


def test_asyncio_cancelled_call_writes_one_failure_row():
    sink = _SlowSink(0.01)

    async def run():
        task = asyncio.create_task(_provider(sink).call_tool(LICENCE_CALLER, "demo_hang", {}))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)  # let a shielded write finish

    asyncio.run(run())
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["status"] == "failure"
    assert row["details"]["ok"] is False
    assert row["details"]["cancelled"] is True
    assert row["details"]["error"] == "CancelledError"


def test_anyio_scope_cancel_writes_one_failure_row():
    sink = _SlowSink(0.01)

    async def run():
        provider = _provider(sink)
        with anyio.move_on_after(0.05):
            await provider.call_tool(LICENCE_CALLER, "demo_hang", {})
        await asyncio.sleep(0.1)  # the scope re-cancels every await: only a shielded write lands

    asyncio.run(run())
    assert [r["status"] for r in sink.rows] == ["failure"]
    assert sink.rows[0]["details"]["cancelled"] is True


def test_exception_with_empty_message_is_a_failure():
    sink = RecordingSink()
    out = _call(_provider(sink), "demo_empty", {})
    assert out.isError is True
    assert len(sink.rows) == 1
    assert sink.rows[0]["status"] == "failure"
    assert sink.rows[0]["details"]["error"]
    assert "cancelled" not in sink.rows[0]["details"]


def test_slow_audit_write_does_not_delay_the_tool_response():
    sink = _SlowSink(5.0)
    started = time.monotonic()
    out = _call(_provider(sink), "demo_ok", {"x": 3})
    elapsed = time.monotonic() - started
    assert out.structuredContent == {"x": 3}
    assert elapsed < 1.0, f"tool call took {elapsed:.2f}s behind a 5 s audit write"


def test_caller_tenant_and_principal_land_in_details():
    sink = RecordingSink()
    _call(_provider(sink), "demo_ok", {"x": 1})
    details = sink.rows[0]["details"]
    assert details["tenant_id"] == TENANT
    assert details["auth_method"] == "license"
    assert details["principal"] == "subject-1"
    assert sink.rows[0]["principal_kind"] == CARE_BUS_LICENCE


def test_no_tenant_means_no_tenant_key():
    sink = RecordingSink()
    _call(_provider(sink), "demo_ok", {"x": 1}, caller=KC_CALLER)
    assert "tenant_id" not in sink.rows[0]["details"]
    assert sink.rows[0]["details"]["auth_method"] == "keycloak"


def test_no_sink_is_a_noop():
    out = _call(_provider(None), "demo_ok", {"x": 1})
    assert out.structuredContent == {"x": 1}
    assert asyncio.run(audit.record(None, KC_CALLER, "x", latency_ms=1, error=None)) is None
