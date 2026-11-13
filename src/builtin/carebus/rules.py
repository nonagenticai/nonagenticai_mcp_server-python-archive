"""business-rules tools: mounted under namespace "rules" → rules_check_return_window, rules_check_warranty, rules_check_payment_issue."""

from __future__ import annotations

from fastmcp import FastMCP

from .client import demo_backends

rules_mcp = FastMCP(name="carebus-rules")


@rules_mcp.tool()
async def check_return_window(
    order_id: str, category: str, delivered_on: str | None = None
) -> dict:
    """RET-01: returns are allowed for 30 days after delivery. category and delivered_on are accepted because step args send them; the rule reads both from the order."""
    return await demo_backends().get("/api/rules/return-window", {"order_id": order_id})


@rules_mcp.tool()
async def check_warranty(order_id: str, category: str, delivered_on: str | None = None) -> dict:
    """WAR-01: 24 months of warranty after delivery. category and delivered_on are accepted, not forwarded."""
    return await demo_backends().get("/api/rules/warranty", {"order_id": order_id})


@rules_mcp.tool()
async def check_payment_issue(order_id: str, error_type: str) -> dict:
    """PAY-04: two equal captures within 5 minutes count as a duplicate charge."""
    return await demo_backends().get(
        "/api/rules/payment-issue", {"order_id": order_id, "error_type": error_type}
    )
