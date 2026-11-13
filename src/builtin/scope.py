"""Least privilege per caller: every builtin caller sees and calls ONLY its scope's tools.

Ported from main's src/tool_scope.py with the same tables and fail-closed rules:

Keycloak-JWT service account, by `azp` (PRINCIPAL_SCOPES):
* carebus-tool-executor -> the 13-name care-bus contract (crm_*, order_*, delivery_*, rules_*,
  procedures_*, faq_ask);
* carebus-researcher    -> legal_*, web_* and research_*.

Licence token (X-Tenant-Id: Bearer, no X-MCP-Binding), by the introspected licence's `app.key`:
* `care-bus` -> the care-bus tools plus the researcher set, but only for a TENANT-LEVEL licence
  (introspection `subject_kind == "tenant"`); a person's per-login licence ("member"), or a payload
  without the field, gets nothing.

Any other azp, or a non-tenant care-bus licence -> NOTHING (empty list; every call denied with
DENIED_MESSAGE and audited as 'denied'). Binding principals (mcp licence + X-MCP-Binding) never
reach this table: they see catalog tools only. Main's `mcp` licence rule ("everything but the
care-bus tools") has no equivalent here because the claude_* / tools_* servers are removed and an
mcp licence without a binding is refused (401).

Each allowed set is derived from the (server, namespace) pairs the mount_* helpers mount -- each
mounted name is f"{namespace}_{tool}" -- never from a hand-written list, and is cached per scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastmcp import FastMCP

from src.builder.tools import BUILDER_SERVERS

from .carebus import CAREBUS_SERVERS
from .legal import LEGAL_SERVERS
from .research import RESEARCH_SERVERS
from .web import WEB_SERVERS

DENIED_MESSAGE = "[permanent] tool not permitted for this caller"

RESEARCHER_SERVERS: tuple[tuple[FastMCP, str], ...] = (
    *LEGAL_SERVERS,
    *WEB_SERVERS,
    *RESEARCH_SERVERS,
)

# Keycloak azp -> the (server, namespace) groups that principal may use.
PRINCIPAL_SCOPES: dict[str, tuple[tuple[FastMCP, str], ...]] = {
    "carebus-tool-executor": tuple(CAREBUS_SERVERS),
    "carebus-researcher": RESEARCHER_SERVERS,
}

#: The licence app whose tenant-level callers get the care-bus tools. Issued only by owner
#: care-bus (licenses APP_ISSUER_OVERRIDES, migration 067).
CAREBUS_LICENCE_APP = "care-bus"

#: Introspection `subject_kind` of a tenant-level licence (licenses #305).
TENANT_SUBJECT_KIND = "tenant"

#: The tool builder: a tenant-level mcp licence without a binding authors with builder_* only.
MCP_LICENCE_APP = "mcp"
MCP_LICENCE_SCOPE = "licence:" + MCP_LICENCE_APP

# Licence app.key -> the groups its callers may use. Prefixed so no azp can name one.
LICENCE_SCOPE_PREFIX = "licence:"
LICENCE_SCOPES: dict[str, tuple[tuple[FastMCP, str], ...]] = {
    CAREBUS_LICENCE_APP: (*CAREBUS_SERVERS, *RESEARCHER_SERVERS),
    MCP_LICENCE_APP: tuple(BUILDER_SERVERS),
}

SCOPES: dict[str, tuple[tuple[FastMCP, str], ...]] = {
    **PRINCIPAL_SCOPES,
    **{LICENCE_SCOPE_PREFIX + app: servers for app, servers in LICENCE_SCOPES.items()},
}


async def tool_names(servers: Sequence[tuple[FastMCP, str]]) -> frozenset[str]:
    names: set[str] = set()
    for server, namespace in servers:
        names.update(f"{namespace}_{tool.name}" for tool in await server.list_tools())
    return frozenset(names)


async def carebus_tool_names() -> frozenset[str]:
    return await tool_names(CAREBUS_SERVERS)


def licence_app_key(info: dict[str, Any] | None) -> str | None:
    """The introspected licence's app key (`app.key`), or None when the payload has none."""
    app = (info or {}).get("app")
    key = app.get("key") if isinstance(app, dict) else None
    return key if isinstance(key, str) and key else None


def licence_scope(info: dict[str, Any]) -> str:
    """Scope key of a licence caller: only a tenant-level licence of a scoped app gets one."""
    app = licence_app_key(info)
    if app in LICENCE_SCOPES and info.get("subject_kind") == TENANT_SUBJECT_KIND:
        return LICENCE_SCOPE_PREFIX + app
    return ""


class ScopeTable:
    """Allowed builtin names per scope key, computed once from the mounts."""

    def __init__(self) -> None:
        self._allowed: dict[str, frozenset[str]] = {}

    async def allowed(self, scope: str) -> frozenset[str]:
        if scope not in self._allowed:
            servers = SCOPES.get(scope)
            self._allowed[scope] = await tool_names(servers) if servers else frozenset()
        return self._allowed[scope]
