"""First-party in-process builtin provider (ported from main #41-#46).

The 34 care-bus / researcher tools (src/builtin/{carebus,legal,research,web}) keep their public
names: each mounted name is f"{namespace}_{tool}". They are offered only to non-binding
principals whose scope lists them (src/builtin/scope.py), never to binding principals, and every
call is audited (src/builtin/audit.py). See docs/AUTHENTICATION_BINDINGS.md.
"""

from __future__ import annotations

from fastmcp import FastMCP

from src.builder.tools import mount_builder

from .carebus import mount_carebus
from .legal import mount_legal
from .research import mount_research
from .web import mount_web


def create_builtin_server() -> FastMCP:
    """One in-process FastMCP holding every builtin tool. Never served on its own."""
    server = FastMCP(name="builtin", mask_error_details=False)
    mount_carebus(server)
    mount_legal(server)
    mount_web(server)
    mount_research(server)
    mount_builder(server)
    return server
