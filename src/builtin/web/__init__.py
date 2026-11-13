"""Web tools (care-bus plan 18), mounted under namespace "web" -> web_scrape."""

from __future__ import annotations

from fastmcp import FastMCP

from .scrape import web_mcp

WEB_SERVERS: list[tuple[FastMCP, str]] = [(web_mcp, "web")]


def mount_web(mcp: FastMCP) -> None:
    for server, namespace in WEB_SERVERS:
        mcp.mount(server, namespace=namespace)
