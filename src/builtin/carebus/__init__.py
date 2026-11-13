"""Care-bus MCP tools (care-bus integration). Mounted names = f"{namespace}_{tool}" (FastMCP 3.4.7)."""

from __future__ import annotations

from fastmcp import FastMCP

from .crm import crm_mcp
from .delivery import delivery_mcp
from .faq import faq_mcp
from .order import order_mcp
from .procedures import procedures_mcp
from .rules import rules_mcp

CAREBUS_SERVERS: list[tuple[FastMCP, str]] = [
    (crm_mcp, "crm"),
    (order_mcp, "order"),
    (delivery_mcp, "delivery"),
    (rules_mcp, "rules"),
    (procedures_mcp, "procedures"),
    (faq_mcp, "faq"),
]


def mount_carebus(mcp: FastMCP) -> None:
    for server, namespace in CAREBUS_SERVERS:
        # FastMCP 3.4.7: mount(server, namespace=...). `prefix=` is deprecated and emits FastMCPDeprecationWarning.
        mcp.mount(server, namespace=namespace)
