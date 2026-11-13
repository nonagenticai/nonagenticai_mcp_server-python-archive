"""The one sub-FastMCP every legal tool registers on; mounted under namespace "legal"."""

from __future__ import annotations

from fastmcp import FastMCP

legal_mcp = FastMCP(name="legal")
