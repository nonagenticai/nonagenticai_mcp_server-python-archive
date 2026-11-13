"""delivery tools: mounted under namespace "delivery" → delivery_track_shipment."""

from __future__ import annotations

from fastmcp import FastMCP

from .client import demo_backends, path_id

delivery_mcp = FastMCP(name="carebus-delivery")


@delivery_mcp.tool()
async def track_shipment(shipment_id: str) -> dict:
    """Carrier status, scan events and ETA for a shipment."""
    return await demo_backends().get(f"/api/shipments/{path_id('shipment_id', shipment_id)}")
