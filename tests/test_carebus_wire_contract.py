"""care-bus wire contract: replay care-bus's exact /mcp request shapes against the full app.

A deployed care-bus client talks to /mcp only through rmcp 3.5.0's `StreamableHttpClientTransport` over reqwest,
started with `().serve(transport)` (legacy `initialize` lifecycle, `allow_stateless` default true).
Both deployed consumers use it: the tool-executor and the researcher. What that puts on the wire (rmcp 3.5.0
src/transport/common/reqwest/streamable_http_client.rs, src/transport/streamable_http_client.rs):

* every request is POST /mcp with `Accept: text/event-stream, application/json` and
  `Content-Type: application/json`; the `initialize` POST carries no `MCP-Protocol-Version`, every
  later POST carries the version the server answered; `Mcp-Session-Id` only if the server issued
  one (no id -> stateless: no GET stream, no DELETE);
* `initialize.params.protocolVersion` is rmcp's LATEST (`2026-07-28`); the answer must name a
  version that still has an initialize handshake (< 2026-07-28) or the client errors;
* `notifications/initialized` must be answered 202/204, an empty 2xx, or JSON -- an SSE answer is
  a fatal "expect accepted or json";
* responses may be JSON or SSE (empty `data:` events skipped); a non-2xx response is a transport
  error unless it is a JSON-RPC error body; 401/403 with `WWW-Authenticate` become AuthRequired /
  InsufficientScope, any other 401/403 "HTTP 401 ..." -- all of which care-bus answers by renewing
  its credentials once (mcp_http.rs `auth_refusal`), then `auth_rejected` (permanent);
* `tools/list` is paginated by rmcp `list_all_tools` (follows `nextCursor` until absent);
* a tool result is read as `structuredContent`, else the text parsed as JSON; `isError` text is
  classified by its `[retryable] ` / `[permanent] ` prefix (mcp_http.rs `tool_result_to_value`).

Auth: the current image sends `X-Tenant-Id: Bearer <tenant care-bus licence>` +
`X-Strata-Token: <strata licence>` and no Authorization; images before care-bus#36 sent
`Authorization: Bearer <Keycloak client_credentials JWT>` (azp carebus-tool-executor /
carebus-researcher), which gitops still configures (MCP_TOKEN_URL / MCP_CLIENT_*). Both are
replayed. carebus-api's overview probe sends GET `{MCP_URL minus /mcp}/api/health` with the same
two licence headers and needs a 2xx.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.builtin.carebus.client import ServiceClient, set_client
from tests.test_builtin_resolver import RESEARCHER, _carebus
from tests.test_builtin_resolver import api as api  # noqa: F401 -- fixture
from tests.test_carebus_mount import CONTRACT
from tests.test_catalog_http import catalog_api as catalog_api  # noqa: F401 -- fixture
from tests.test_keycloak_auth import _bearer, _token
from tests.test_keycloak_auth import kc_env as kc_env  # noqa: F401 -- fixture

#: rmcp 3.5.0 `ProtocolVersion::LATEST`, sent in `initialize` by `().serve(..)`.
RMCP_LATEST = "2026-07-28"
#: rmcp's `NO_INITIALIZE`: an initialize answer naming this or later is unusable.
NO_INITIALIZE = "2026-07-28"
#: rmcp's Accept, in rmcp's order.
RMCP_ACCEPT = "text/event-stream, application/json"

LICENCE = {"X-Tenant-Id": "Bearer carebus-tenant", "X-Strata-Token": "strata-tenant-token"}
OUTAGE = {"X-Tenant-Id": "Bearer licence-outage", "X-Strata-Token": "strata-tenant-token"}
REVOKED = {"X-Tenant-Id": "Bearer revoked", "X-Strata-Token": "strata-tenant-token"}


class Transport:
    """What rmcp's reqwest StreamableHttpClient does with one POST and its response."""

    def __init__(self, client: httpx.AsyncClient, auth: dict[str, str]) -> None:
        self.client, self.auth = client, auth
        self.session: str | None = None
        self.version: str | None = None
        self.ids = 0

    def headers(self) -> dict[str, str]:
        out = {"Accept": RMCP_ACCEPT, "Content-Type": "application/json", **self.auth}
        if self.version:
            out["MCP-Protocol-Version"] = self.version
        if self.session:
            out["Mcp-Session-Id"] = self.session
        return out

    async def post(self, message: dict) -> httpx.Response:
        return await self.client.post(
            "/mcp", headers=self.headers(), content=json.dumps(message).encode()
        )

    async def request(self, method: str, params: dict | None = None) -> dict:
        identity = self.ids
        self.ids += 1
        message = {"jsonrpc": "2.0", "id": identity, "method": method}
        if params is not None:
            message["params"] = params
        response = await self.post(message)
        assert response.status_code not in (202, 204), "a request needs a reply"
        assert 200 <= response.status_code < 300, (response.status_code, response.text)
        return reply(response, identity)

    async def notify(self, method: str) -> None:
        response = await self.post({"jsonrpc": "2.0", "method": method})
        content_type = response.headers.get("content-type", "")
        # expect_accepted_or_json: Accepted (202/204, or empty 2xx) or a JSON body; never SSE.
        empty = response.headers.get("content-length") == "0"
        assert (
            response.status_code in (202, 204)
            or (200 <= response.status_code < 300 and empty)
            or (200 <= response.status_code < 300 and content_type.startswith("application/json"))
        ), (response.status_code, content_type, response.text)

    async def initialize(self) -> dict:
        response = await self.post(
            {
                "jsonrpc": "2.0",
                "id": self.ids,
                "method": "initialize",
                "params": {
                    "protocolVersion": RMCP_LATEST,
                    "capabilities": {},
                    "clientInfo": {"name": "rmcp", "version": "3.5.0"},
                },
            }
        )
        assert response.status_code == 200, (response.status_code, response.text)
        result = reply(response, self.ids)["result"]
        self.ids += 1
        # allow_stateless: no session id is fine; one would be echoed on every later request.
        self.session = response.headers.get("mcp-session-id")
        self.version = result["protocolVersion"]
        assert self.version < NO_INITIALIZE, self.version
        await self.notify("notifications/initialized")
        return result

    async def list_all_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(100):
            body = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(body["result"]["tools"])
            cursor = body["result"].get("nextCursor")
            if cursor is None:
                return tools
        raise AssertionError("tools/list never ended")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        body = await self.request("tools/call", {"name": name, "arguments": arguments})
        assert "error" not in body, body
        return body["result"]


def reply(response: httpx.Response, identity: int) -> dict:
    """The JSON-RPC response to `identity` from a JSON or SSE body, as rmcp reads it."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = response.json()
        assert body.get("id") == identity, body
        return body
    assert content_type.startswith("text/event-stream"), f"unexpected content type {content_type}"
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue  # rmcp skips empty data (e.g. a priming event)
        message = json.loads(payload)
        if message.get("id") == identity and ("result" in message or "error" in message):
            return message
    raise AssertionError(f"no response to {identity} in the SSE body: {response.text!r}")


def value(result: dict):
    """mcp_http.rs tool_result_to_value: (ok, value-or-(retryable, message))."""
    text = "\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        if text.startswith("[retryable] "):
            return False, (True, text[12:])
        if text.startswith("[permanent] "):
            return False, (False, text[12:])
        return False, (False, text)
    if result.get("structuredContent") is not None:
        return True, result["structuredContent"]
    try:
        return True, json.loads(text)
    except ValueError:
        return True, text


@pytest.fixture
async def wire(api, monkeypatch):  # noqa: F811
    app, client = api

    def answer(request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["token"]
        if token == "carebus-tenant":
            return httpx.Response(200, json=_carebus("tenant"))
        if token == "licence-outage":
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(200, json={"active": False})

    await app.state.licenses.client.aclose()
    app.state.licenses.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    seen: list[httpx.Request] = []

    def backend(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        order = request.url.path.rsplit("/", 1)[-1]
        if order == "ORD-404":
            return httpx.Response(404, json={"error": {"code": "nf", "message": "no such order"}})
        if order == "ORD-503":
            return httpx.Response(503, json={"error": {"code": "busy", "message": "busy"}})
        return httpx.Response(200, json={"order_id": order, "status": "shipped"})

    set_client(
        "demo_backends",
        ServiceClient("http://demo.test", "demo-token", transport=httpx.MockTransport(backend)),
    )
    try:
        yield app, client, seen
    finally:
        set_client("demo_backends", None)


def caller_headers(kind: str) -> dict[str, str]:
    if kind == "tenant-licence":
        return LICENCE
    if kind == "kc-researcher":
        return _bearer(_token(azp=RESEARCHER))
    return _bearer(_token())  # azp carebus-tool-executor


@pytest.mark.parametrize("kind", ["tenant-licence", "kc-tool-executor"])
async def test_executor_session_initialize_list_call(wire, kind):
    _, client, seen = wire
    transport = Transport(client, caller_headers(kind))
    init = await transport.initialize()
    assert "tools" in init["capabilities"]
    names = {t["name"] for t in await transport.list_all_tools()}
    if kind == "tenant-licence":  # care-bus tools plus the researcher set
        assert names > CONTRACT
    else:
        assert names == CONTRACT
    # mcp_tools_from_list_result: name, description, inputSchema
    for tool in await transport.list_all_tools():
        assert isinstance(tool["name"], str) and isinstance(tool["inputSchema"], dict)

    ok, got = value(await transport.call_tool("order_get_order", {"order_id": "ORD-58213"}))
    assert ok and got["order_id"] == "ORD-58213"  # live_mcp.rs asserts exactly this
    assert seen[-1].headers["authorization"] == "Bearer demo-token"

    ok, got = value(await transport.call_tool("order_get_order", {"order_id": "ORD-404"}))
    assert not ok and got[0] is False and "no such order" in got[1]
    ok, got = value(await transport.call_tool("order_get_order", {"order_id": "ORD-503"}))
    assert not ok and got[0] is True

    # unknown / out-of-scope tool: a tool error (permanent), never a transport failure
    ok, got = value(await transport.call_tool("tools_add", {"a": 1, "b": 2}))
    assert not ok and got[0] is False


@pytest.mark.parametrize("kind", ["tenant-licence", "kc-researcher"])
async def test_researcher_session_lists_its_tools_and_calls_one(wire, kind):
    """crates/carebus-api/src/research/tools.rs: tools/list filtered by the profile allow-list."""
    _, client, _ = wire
    transport = Transport(client, caller_headers(kind))
    await transport.initialize()
    names = {t["name"] for t in await transport.list_all_tools()}
    assert {"web_scrape"} <= names
    if kind == "kc-researcher":
        assert not names & CONTRACT
    ok, got = value(await transport.call_tool("tools_add", {"a": 1, "b": 2}))
    assert not ok and got[0] is False


async def test_stateless_no_session_id(wire):
    """No Mcp-Session-Id: rmcp (allow_stateless) opens no GET stream and sends no DELETE."""
    _, client, _ = wire
    transport = Transport(client, LICENCE)
    await transport.initialize()
    assert transport.session is None


async def test_refused_licence_is_401_with_challenge(wire):
    """401 + WWW-Authenticate -> rmcp AuthRequired -> care-bus renews once, then auth_rejected."""
    _, client, _ = wire
    response = await Transport(client, REVOKED).post(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_licence_authority_outage_is_retryable_not_auth(wire):
    """503 with a non-JSON-RPC body: rmcp UnexpectedServerResponse("HTTP 503 ...") -> care-bus
    `retryable`, session dropped -- not a 401 that would burn a credential renewal and end in a
    permanent auth_rejected."""
    _, client, _ = wire
    response = await Transport(client, OUTAGE).post(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    )
    assert response.status_code == 503
    assert "www-authenticate" not in response.headers
    assert "jsonrpc" not in response.json()


@pytest.mark.parametrize("headers", [LICENCE, {}])
async def test_overview_probe_api_health(wire, headers):
    _, client, _ = wire
    response = await client.get("/api/health", headers=headers)
    assert response.status_code == 200
