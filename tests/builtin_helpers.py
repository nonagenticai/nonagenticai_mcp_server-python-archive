"""A DB-free /mcp stack for builtin-provider tests: AuthenticationMiddleware -> BoundedProtocol
-> CatalogMCP (stateless Streamable HTTP), with the real BuiltinProvider and a recording sink.

The catalog half is never reached by builtin callers (they carry no RequestAuthority), so it is a
stub here; tests/test_builtin_resolver.py covers the full app with PostgreSQL.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from fastmcp.server.providers import Provider
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from src.auth.principal import CallerPrincipal
from src.auth.protocol import AuthenticationMiddleware
from src.builtin.provider import BuiltinProvider
from src.config import Settings
from src.protocol import BoundedProtocol, ExactProtocol, create_protocol

ACCEPT = {"Accept": "application/json, text/event-stream"}


class RecordingSink:
    """Rows shaped like main's audit_logs fake (resource_id / status / details)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def write(
        self, principal: CallerPrincipal, tool: str, status: str, details: dict[str, Any]
    ) -> None:
        self.rows.append(
            {
                "resource_id": tool,
                "status": status,
                "details": details,
                "principal_kind": principal.kind,
            }
        )


class _NoCatalog(Provider):
    async def _list_tools(self):  # never reached by builtin callers
        raise PermissionError("catalog not available in this test stack")

    async def _get_tool(self, name, version=None):
        return None


def builtin_stack(licenses: Any, sink: RecordingSink) -> Starlette:
    settings = Settings(database_url=SecretStr("postgresql://unused@localhost/unused"))
    builtin = BuiltinProvider(sink=sink)
    transport = create_protocol(_NoCatalog(), builtin)  # ty: ignore[invalid-argument-type]
    state = SimpleNamespace(settings=settings, licenses=licenses, builtin=builtin)
    target = BoundedProtocol(transport, state)

    @asynccontextmanager
    async def lifespan(app):
        async with transport.router.lifespan_context(transport):
            yield

    inner = Starlette(
        routes=[
            Route("/mcp", endpoint=ExactProtocol(target), methods=["GET", "POST", "DELETE"]),
            Mount("/mcp", app=target),
        ],
        lifespan=lifespan,
    )
    inner.add_middleware(AuthenticationMiddleware, state=state)
    return inner


def parse(response) -> dict:
    """JSON-RPC body of a JSON or single-event SSE response."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        return json.loads(data)
    return response.json()
