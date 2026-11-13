"""The one sub-FastMCP every research tool registers on; mounted under namespace "research"."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .client import auth_headers, get_client

research_mcp = FastMCP(name="research")


def _json(resp: Any, what: str) -> Any:
    try:
        return resp.json()
    except ValueError as e:
        raise ToolError(f"[retryable] non-JSON answer from {what}") from e


async def call_json(
    service: str,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    params: dict | None = None,
    not_found: str | None = None,
) -> Any:
    """One JSON call to a research backend, with that backend's identity header (if any)."""
    resp = await get_client(service).request(
        method,
        path,
        json=body,
        params=params,
        not_found=not_found,
        headers={"Accept": "application/json", **(auth_headers(service) or {})},
    )
    return _json(resp, f"{method} {path}")
