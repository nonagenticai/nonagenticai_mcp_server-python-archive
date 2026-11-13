"""procedures tool: mounted under namespace "procedures" → procedures_get_procedure."""

from __future__ import annotations

from fastmcp import FastMCP

from .client import carebus_api, path_id

procedures_mcp = FastMCP(name="carebus-procedures")


@procedures_mcp.tool()
async def get_procedure(name: str) -> dict:
    """Steps of the PUBLISHED version of a procedure (PKO/PDDL model from carebus-api)."""
    return await carebus_api().get(
        f"/api/procedures/{path_id('name', name)}", {"status": "PUBLISHED"}
    )
