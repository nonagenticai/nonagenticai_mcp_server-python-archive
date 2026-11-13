"""Legal research tools (care-bus plan 18), mounted under namespace "legal" -> legal_<tool>.

Public sources only: Sejm ELI (Polish acts), SAOS (Polish judgments), CELLAR (EU law), KRS
(court register) and the MF VAT whitelist, plus three sites without an API that are read from
their public HTML/JSON front-ends: KIO rulings (orzeczenia.uzp.gov.pl), UOKiK President's
decisions (decyzje.uokik.gov.pl) and the Dolnośląskie official journal (edzienniki.duw.pl). The exposed names are a contract with the care-bus
researcher (carebus-researcher), pinned by tests/test_legal_tools.py::CONTRACT.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from . import (  # noqa: F401  (registers the tools on legal_mcp)
    duw,
    eli,
    eurlex,
    kio,
    krs,
    saos,
    uokik,
    vat,
)
from ._server import legal_mcp

LEGAL_SERVERS: list[tuple[FastMCP, str]] = [(legal_mcp, "legal")]

# uokik.gov.pl's robots.txt disallows AI agents (ClaudeBot, anthropic-ai, GPTBot). It does not cover
# decyzje.uokik.gov.pl or our User-Agent, but the owner's intent is clear, so the UOKiK tools stay
# hidden unless LEGAL_UOKIK_ENABLED=true (owner decision pending, care-bus plan 18).
UOKIK_TOOLS = {"uokik_search", "uokik_document"}


def uokik_enabled() -> bool:
    return os.environ.get("LEGAL_UOKIK_ENABLED", "").strip().lower() in {"1", "true", "yes"}


_uokik_shown: bool | None = None


def apply_source_gates() -> None:
    """Show or hide the gated tools on legal_mcp per env. Each enable/disable stacks a visibility
    transform on the shared server (the later one wins), so one is added only when the state changes."""
    global _uokik_shown
    shown = uokik_enabled()
    if shown == _uokik_shown:
        return
    if shown:
        legal_mcp.enable(names=UOKIK_TOOLS)
    else:
        legal_mcp.disable(names=UOKIK_TOOLS)
    _uokik_shown = shown


def mount_legal(mcp: FastMCP) -> None:
    apply_source_gates()
    for server, namespace in LEGAL_SERVERS:
        mcp.mount(server, namespace=namespace)
