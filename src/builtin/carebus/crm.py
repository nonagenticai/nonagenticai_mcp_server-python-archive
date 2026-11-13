"""crm tools: mounted under namespace "crm" → crm_verify_customer, crm_get_start_data."""

from __future__ import annotations

from fastmcp import FastMCP

from .client import demo_backends, path_id

crm_mcp = FastMCP(name="carebus-crm")


@crm_mcp.tool()
async def verify_customer(customer_id: str) -> dict:
    """Verify a customer id and return name, tier and verification status."""
    c = await demo_backends().get(f"/api/customers/{path_id('customer_id', customer_id)}")
    return {
        "customer_id": c["customer_id"],
        "name": c["name"],
        "tier": c["tier"],
        "verified": True,
    }


@crm_mcp.tool()
async def get_start_data(customer_id: str) -> dict:
    """Most recent orders, shipments, payments and communications (last 90 days)."""
    return await demo_backends().get(
        f"/api/customers/{path_id('customer_id', customer_id)}/start-data"
    )
