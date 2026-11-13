from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastmcp.exceptions import ToolError

from src.builtin.carebus.client import ServiceClient
from tests.carebus_helpers import Recorder

BASE = "http://demo.test"


def _client(rec: Recorder) -> ServiceClient:
    return ServiceClient(BASE, "tok-123", transport=rec.transport)


def test_get_sends_bearer_and_returns_json():
    rec = Recorder({("GET", "/api/orders/ORD-58790"): (200, {"order_id": "ORD-58790"})})
    out = asyncio.run(_client(rec).get("/api/orders/ORD-58790"))
    assert out == {"order_id": "ORD-58790"}
    assert rec.requests[0].headers["authorization"] == "Bearer tok-123"


def test_timeout_is_retryable():
    rec = Recorder({("GET", "/api/shipments/SHP-1"): httpx.ReadTimeout("slow")})
    with pytest.raises(ToolError, match=r"^\[retryable\] "):
        asyncio.run(_client(rec).get("/api/shipments/SHP-1"))


def test_connect_error_is_retryable():
    rec = Recorder({("GET", "/api/orders/X"): httpx.ConnectError("refused")})
    with pytest.raises(ToolError, match=r"^\[retryable\] "):
        asyncio.run(_client(rec).get("/api/orders/X"))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_429_and_5xx_are_retryable(status):
    rec = Recorder(
        {
            ("GET", "/api/orders/X"): (
                status,
                {"error": {"code": "x", "message": "busy"}},
            )
        }
    )
    with pytest.raises(ToolError, match=r"^\[retryable\] .*busy"):
        asyncio.run(_client(rec).get("/api/orders/X"))


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422])
def test_other_4xx_are_permanent_and_carry_backend_message(status):
    rec = Recorder(
        {
            ("POST", "/api/payments/PAY-1/refunds"): (
                status,
                {"error": {"code": "c", "message": "exceeds captured amount"}},
            )
        }
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] .*exceeds captured amount"):
        asyncio.run(_client(rec).post("/api/payments/PAY-1/refunds", {"amount": 999}))


def test_no_token_sends_no_authorization_header():
    rec = Recorder({("GET", "/health"): (200, {"ok": True})})
    asyncio.run(ServiceClient(BASE, None, transport=rec.transport).get("/health"))
    assert "authorization" not in rec.requests[0].headers


def test_default_timeout_is_2_5_seconds():
    assert ServiceClient(BASE, None).timeout == 2.5


def test_timeout_is_wall_clock_per_call_not_per_phase():
    # A server that trickles its response never trips an httpx per-phase timeout;
    # the call as a whole must still give up after `timeout` seconds.
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return httpx.Response(200, json={"ok": True})

    client = ServiceClient(BASE, None, transport=httpx.MockTransport(slow), timeout=0.2)
    started = time.monotonic()
    with pytest.raises(
        ToolError, match=r"^\[retryable\] timeout after \d+ ms calling GET /api/slow$"
    ):
        asyncio.run(client.get("/api/slow"))
    assert time.monotonic() - started < 0.6


def test_unreachable_backend_error_does_not_leak_the_internal_url(caplog):
    rec = Recorder({("GET", "/api/orders/X"): httpx.ConnectError("refused")})
    with pytest.raises(ToolError) as exc:
        asyncio.run(_client(rec).get("/api/orders/X"))
    message = str(exc.value)
    assert message.startswith("[retryable] cannot reach backend for GET /api/orders/X")
    assert BASE not in message and "demo.test" not in message
    assert BASE in caplog.text  # the URL goes to the log instead
