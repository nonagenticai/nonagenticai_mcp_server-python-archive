"""Builtin provider: list/call the in-process builtin tools for one CallerPrincipal.

The resolver's builtin half (docs/AUTHENTICATION_BINDINGS.md): tools/list returns exactly the
caller's scope, in one page; tools/call of anything else returns MCP isError DENIED_MESSAGE and is
audited as 'denied'. Tool results and ToolError texts ("[retryable] ...", "[permanent] ...") pass
through unchanged, exactly as main's FastMCP server returned them, because the care-bus executor
reads that prefix.
"""

from __future__ import annotations

import asyncio
import time

import mcp.types as types
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from pydantic import ValidationError

from src.auth.principal import CallerPrincipal
from src.builtin import audit, create_builtin_server
from src.builtin.scope import DENIED_MESSAGE, ScopeTable


def _safe_text(error: Exception) -> str:
    """The error text for the caller and the audit row. A pydantic ValidationError normally quotes
    the rejected input (``input_value=...``), which may be a secret: name the rule, never the value."""
    if isinstance(error, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in item['loc']) or 'arguments'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False, include_context=False)[
                :8
            ]
        ]
        return "invalid arguments: " + "; ".join(parts)[:1000]
    return str(error) or type(error).__name__


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


class BuiltinProvider:
    def __init__(self, server: FastMCP | None = None, sink: audit.AuditSink | None = None) -> None:
        self.server = server or create_builtin_server()
        self.scopes = ScopeTable()
        self.sink = sink

    async def list_tools(self, principal: CallerPrincipal) -> list[types.Tool]:
        allowed = await self.scopes.allowed(principal.scope)
        tools = await self.server.list_tools()
        return sorted(
            (t.to_mcp_tool(name=t.name) for t in tools if t.name in allowed),
            key=lambda t: t.name,
        )

    async def call_tool(
        self, principal: CallerPrincipal, name: str, arguments: dict
    ) -> types.CallToolResult:
        started = time.monotonic()
        if name not in await self.scopes.allowed(principal.scope):
            await audit.record(
                self.sink, principal, name, latency_ms=0, error=DENIED_MESSAGE, denied=True
            )
            return _error(DENIED_MESSAGE)
        error: str | None = None
        cancelled = False
        try:
            result = (await self.server.call_tool(name, arguments)).to_mcp_result()
        except asyncio.CancelledError:
            cancelled = True
            error = "CancelledError"
            raise
        except NotFoundError:
            error = f"Unknown tool: {name!r}"
            return _error(error)
        except Exception as e:  # ToolError / ValidationError: text unchanged, as main's server
            error = _safe_text(e)
            return _error(error)
        finally:
            await audit.record(
                self.sink,
                principal,
                name,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=error,
                cancelled=cancelled,
            )
        if isinstance(result, types.CallToolResult):
            return result
        if isinstance(result, tuple):
            content, structured = result
            return types.CallToolResult(
                content=list(content), structured_content=structured, is_error=False
            )
        return types.CallToolResult(content=list(result), is_error=False)
