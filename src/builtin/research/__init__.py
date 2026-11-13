"""Research tools (care-bus plan 18), mounted under namespace "research" -> research_<tool>.

Scientific literature from discovery-engine and statistics from scientific-processor, both
in-cluster services. Read/compute paths only. The exposed names are a contract with the care-bus
researcher (carebus-researcher), pinned by tests/test_research_tools.py::CONTRACT.
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import literature, stats  # noqa: F401  (registers the tools on research_mcp)
from ._server import research_mcp

RESEARCH_SERVERS: list[tuple[FastMCP, str]] = [(research_mcp, "research")]


def mount_research(mcp: FastMCP) -> None:
    for server, namespace in RESEARCH_SERVERS:
        mcp.mount(server, namespace=namespace)
