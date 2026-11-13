"""Test helpers for the care-bus MCP tools: a recording MockTransport and sync Client wrappers."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastmcp import Client, FastMCP


class Recorder:
    """MockTransport handler keyed by (METHOD, path). A value is (status, json) or an Exception to raise."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict] | Exception]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(
                404, json={"error": {"code": "not_found", "message": f"no route {key}"}}
            )
        value = self.routes[key]
        if isinstance(value, Exception):
            raise value
        status, body = value
        return httpx.Response(status, json=body)

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content or b"{}")


def call_tool(server: FastMCP, name: str, args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server) as client:
            result = await client.call_tool(name, args)
            return result.structured_content

    return asyncio.run(run())


def list_tool_names(server: FastMCP) -> set[str]:
    async def run() -> set[str]:
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(run())
