"""Pinned FastMCP compatibility boundary and bounded stateless HTTP transport."""

import asyncio
import base64
import hashlib
import hmac
import json
from typing import cast

import mcp.types as types
from fastmcp import FastMCP
from fastmcp.server.low_level import LowLevelServer
from fastmcp.server.server import _lifespan_proxy
from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import jsonrpc_message_adapter
from mcp_types.methods import MONOLITH_NOTIFICATIONS, MONOLITH_REQUESTS
from starlette.responses import JSONResponse

from src.auth.context import call_actor, current_authority, current_caller
from src.builtin.provider import BuiltinProvider
from src.builtin.scope import DENIED_MESSAGE
from src.catalog.provider import CatalogProvider, CatalogTool
from src.config import Settings
from src.storage.bindings import get_binding
from src.storage.executions import ExecutionRepository
from src.storage.task_inputs import TaskInputRepository
from src.tasks.extension import TasksExtension, parse_actor
from src.tasks.models import TASKS_EXTENSION_ID

TASK_METHODS = frozenset({"tasks/get", "tasks/update", "tasks/cancel"})


def _task_request_shape(parsed) -> None:
    """Minimal admission check for the tasks extension methods (not spec methods)."""
    params = parsed.params
    if not isinstance(params, dict) or not isinstance(params.get("taskId"), str):
        raise ValueError("taskId required")
    if parsed.method == "tasks/update" and not isinstance(params.get("inputResponses"), dict):
        raise ValueError("inputResponses must be an object")


class CatalogLowLevel(LowLevelServer):
    tasks_enabled = False

    def get_capabilities(
        self,
        notification_options=None,
        experimental_capabilities=None,
        extensions=None,
        *,
        protocol_version=None,
    ):
        capabilities = types.ServerCapabilities(tools=types.ToolsCapability(list_changed=False))
        if self.tasks_enabled:
            capabilities = capabilities.model_copy(update={"extensions": {TASKS_EXTENSION_ID: {}}})
        return capabilities


class CatalogMCP(FastMCP):
    catalog_provider: CatalogProvider
    builtin_provider: BuiltinProvider | None = None
    tasks: TasksExtension | None = None

    def _setup_handlers(self):
        # These are the only framework internals used. Bypass SDK tool-definition
        # caches, generic schema validators and optional extensions entirely.
        self._mcp_server = CatalogLowLevel(
            self,
            name="Executable catalog",
            version="2.0.0",
            lifespan=_lifespan_proxy(fastmcp_server=self),
        )

        async def listing(ctx, params):
            caller = current_caller()
            if caller is not None:
                # builtin(principal): one page, never catalog tools, never a cursor.
                try:
                    if self.builtin_provider is None or (params and params.cursor is not None):
                        raise ValueError("no builtin listing")
                    tools = await self.builtin_provider.list_tools(caller.principal)
                    return types.ListToolsResult(tools=tools)
                except Exception:
                    raise MCPError(
                        code=-32602, message="catalog_unavailable_or_invalid_cursor"
                    ) from None
            try:
                tools = [t for t in await self.list_tools() if isinstance(t, CatalogTool)]
                principal = current_authority().principal
                if principal.binding_id is None:
                    raise PermissionError("binding required")
                provider = self.catalog_provider
                async with provider.resolver.access.repository.pool.connection() as conn:
                    row = await get_binding(conn, principal.tenant_id, principal.binding_id)
                    if row["revision"] != principal.binding_revision:
                        raise ValueError("binding changed")
                    key = bytes.fromhex(row["token_sha256"])
                generation = await provider.resolver.repository.generation()
                identity = json.dumps(
                    [
                        str(principal.tenant_id),
                        str(principal.license_id),
                        str(principal.binding_id),
                        principal.binding_revision,
                        generation,
                        [t._resolved.manifest_json + t._resolved.effective_json for t in tools],
                    ]
                )
                fingerprint = hashlib.sha256(identity.encode()).hexdigest()
                offset = 0
                cursor = params.cursor if params else None
                if cursor is not None:
                    if len(cursor) > 512:
                        raise ValueError("invalid cursor")
                    raw, signature = cursor.split(".")
                    expected = hmac.digest(key, raw.encode(), "sha256").hex()
                    if not hmac.compare_digest(signature, expected):
                        raise ValueError("invalid cursor")
                    data = json.loads(base64.urlsafe_b64decode(raw))
                    if (
                        data["f"] != fingerprint
                        or type(data["o"]) is not int
                        or not 0 < data["o"] < len(tools)
                    ):
                        raise ValueError("stale cursor")
                    offset = data["o"]
                end = offset + provider.resolver.access.settings.catalog_page_size
                next_cursor = None
                if end < len(tools):
                    raw = base64.urlsafe_b64encode(
                        json.dumps({"o": end, "f": fingerprint}).encode()
                    ).decode()
                    next_cursor = raw + "." + hmac.digest(key, raw.encode(), "sha256").hex()
                return types.ListToolsResult(
                    tools=[t.to_mcp_tool() for t in tools[offset:end]], next_cursor=next_cursor
                )
            except Exception:
                raise MCPError(
                    code=-32602, message="catalog_unavailable_or_invalid_cursor"
                ) from None

        async def calling(ctx, params):
            caller = current_caller()
            if caller is not None:
                if self.builtin_provider is None:
                    return types.CallToolResult(
                        content=[types.TextContent(type="text", text=DENIED_MESSAGE)],
                        is_error=True,
                    )
                return await self.builtin_provider.call_tool(
                    caller.principal, params.name, params.arguments or {}
                )
            try:
                # Optional task/version/app extensions are not accepted on wire.
                # mcp 2 drops unknown params, so check the raw mapping.
                if set(ctx.params or {}) - {"name", "arguments", "_meta"}:
                    raise ValueError("unsupported call extension")
                tool = await self.catalog_provider.get_tool(params.name)
                if tool is None:
                    raise PermissionError("unknown tool")
                actor_token = call_actor.set(parse_actor(ctx.params))
                try:
                    result = await tool.run(params.arguments or {})
                finally:
                    call_actor.reset(actor_token)
                if self.tasks is not None:
                    try:
                        created = await self.tasks.task_result(
                            ctx, current_authority().principal, result.structured_content
                        )
                    except Exception:
                        # The run is already accepted: answer with its envelope, never a denial.
                        created = None
                    if created is not None:
                        return created
                return types.CallToolResult(
                    content=result.content,
                    structured_content=result.structured_content,
                    is_error=result.is_error,
                )
            except Exception:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="tool_request_denied")],
                    is_error=True,
                )

        self._mcp_server.add_request_handler("tools/list", types.PaginatedRequestParams, listing)
        self._mcp_server.add_request_handler("tools/call", types.CallToolRequestParams, calling)


def create_protocol(
    provider: CatalogProvider,
    builtin: BuiltinProvider | None = None,
    *,
    settings: Settings | None = None,
    runs: ExecutionRepository | None = None,
    inputs: TaskInputRepository | None = None,
):
    server = CatalogMCP(
        "Executable catalog",
        providers=[provider],
        tasks=False,
        mask_error_details=True,
        dereference_schemas=False,
        strict_input_validation=False,
    )
    server.catalog_provider = provider
    server.builtin_provider = builtin
    if settings is not None and settings.tasks_enabled and runs is not None and inputs is not None:
        server.tasks = TasksExtension(settings, runs, inputs)
        server.add_extension(server.tasks)
        cast(CatalogLowLevel, server._mcp_server).tasks_enabled = True
    return server.http_app(path="/", stateless_http=True, json_response=False)


class ExactProtocol:
    """Normalize the exact endpoint internally, without widening mount aliases."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = {
            **scope,
            "root_path": scope.get("root_path", "") + "/mcp",
            "path": scope["path"] + "/",
            "raw_path": (scope["path"] + "/").encode(),
        }
        await self.app(scope, receive, send)


class BoundedProtocol:
    """Buffers bounded requests/responses before exposure; admits no waiting queue.

    SSE is preserved on the wire. This stage has bounded, immediate responses and
    no durable execution; buffering cannot acknowledge a partially accepted run.
    """

    def __init__(self, app, state):
        self.app, self.state = app, state
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings: Settings = self.state.settings
        # Builtin callers keep main's per-tool budgets (faq 18 s, research up to 90 s).
        timeout = (
            settings.builtin_timeout_seconds
            if current_caller() is not None
            else settings.protocol_timeout_seconds
        )
        try:
            async with asyncio.timeout(timeout):
                await self.handle(scope, receive, send)
        except TimeoutError:
            await JSONResponse({"error": "protocol_timeout"}, status_code=408)(scope, receive, send)

    async def handle(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The parent registers REST before mounting. Descendants are never aliases.
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        if path not in {"", "/"}:
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
            return
        # Authentication has already consumed these. Do not expose bearer values
        # through FastMCP's own HTTP-request context or optional framework state.
        private_headers = {
            b"x-tenant-id",
            b"x-mcp-binding",
            b"x-mcp-admin-key",
            b"x-mcp-platform-admin-key",
            b"authorization",
            b"cookie",
            b"x-mcp-idempotency-key",
            b"x-strata-token",
        }
        scope = {
            **scope,
            "headers": [
                (k, v) for k, v in scope.get("headers", []) if k.lower() not in private_headers
            ],
        }
        settings: Settings = self.state.settings
        if self.active >= settings.protocol_max_inflight:
            await JSONResponse({"error": "protocol_overloaded"}, status_code=503)(
                scope, receive, send
            )
            return
        self.active += 1
        try:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > settings.protocol_input_bytes:
                    await JSONResponse({"error": "request_too_large"}, status_code=413)(
                        scope, receive, send
                    )
                    return
                if not message.get("more_body", False):
                    break
            if scope["method"] == "POST":
                try:
                    parsed = jsonrpc_message_adapter.validate_json(bytes(body))
                    if isinstance(parsed, types.JSONRPCRequest) and parsed.method in TASK_METHODS:
                        _task_request_shape(parsed)
                    elif isinstance(parsed, types.JSONRPCRequest):
                        MONOLITH_REQUESTS[parsed.method].model_validate(
                            parsed.model_dump(by_alias=True, exclude={"jsonrpc", "id"})
                        )
                    elif isinstance(parsed, types.JSONRPCNotification):
                        MONOLITH_NOTIFICATIONS[parsed.method].model_validate(
                            parsed.model_dump(by_alias=True, exclude={"jsonrpc"})
                        )
                    else:
                        raise ValueError("client responses are unsupported")
                except Exception:
                    await JSONResponse({"error": "invalid_protocol_request"}, status_code=400)(
                        scope, receive, send
                    )
                    return
            sent_body = False

            async def buffered_receive():
                nonlocal sent_body
                if sent_body:
                    return await receive()
                sent_body = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            messages = []
            total = 0
            overflow = False

            async def buffered_send(message):
                nonlocal total, overflow
                if message["type"] == "http.response.body":
                    total += len(message.get("body", b""))
                if total > settings.protocol_output_bytes or len(messages) >= 128:
                    overflow = True
                    messages.clear()
                if not overflow:
                    messages.append(message)

            try:
                async with asyncio.timeout(
                    settings.builtin_timeout_seconds
                    if current_caller() is not None
                    else settings.protocol_timeout_seconds
                ):
                    await self.app(scope, buffered_receive, buffered_send)
            except Exception:
                await JSONResponse({"error": "protocol_unavailable"}, status_code=503)(
                    scope, receive, send
                )
                return
            if overflow:
                await JSONResponse({"error": "protocol_unavailable"}, status_code=503)(
                    scope, receive, send
                )
                return
            for message in messages:
                await send(message)
        finally:
            self.active -= 1
