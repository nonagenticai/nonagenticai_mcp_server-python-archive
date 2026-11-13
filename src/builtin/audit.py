"""One mcp_tool_call_audit row per builtin tool call (ported from main's src/tool_call_audit.py).

Same semantics as main: every way out of the tool that is not a return is a failure, cancellation
included (a cancelled call may already have caused its side effect); a call refused by scope is
'denied'. The write runs as its own task, bounded by AUDIT_WRITE_TIMEOUT_S and shielded from the
call's cancellation, so a slow database can neither hold the tool response past the executor's
budget nor lose the row of a cancelled call. An audit failure is logged and swallowed: it never
fails the tool call. Table: migration 0006_tool_call_audit (runtime role: INSERT only).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from psycopg_pool import AsyncConnectionPool

from src.auth.principal import CallerPrincipal

logger = logging.getLogger(__name__)

AUDIT_WRITE_TIMEOUT_S = 0.5
MAX_ERROR_CHARS = 2000

# Strong references to in-flight audit writes (the event loop only keeps weak ones).
_pending_writes: set[asyncio.Task] = set()


class AuditSink(Protocol):
    async def write(
        self, principal: CallerPrincipal, tool: str, status: str, details: dict[str, Any]
    ) -> None: ...


class DatabaseAuditSink:
    """Inserts into public.mcp_tool_call_audit through the API's runtime pool."""

    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self.pool = pool

    async def write(
        self, principal: CallerPrincipal, tool: str, status: str, details: dict[str, Any]
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO public.mcp_tool_call_audit "
                "(tool,status,principal_kind,auth_method,principal,tenant_id,latency_ms,details) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    tool[:256],
                    status,
                    principal.kind,
                    principal.auth_method,
                    principal.subject[:256],
                    principal.tenant_id[:256] if principal.tenant_id else None,
                    int(details["latency_ms"]),
                    json.dumps(details, ensure_ascii=False, default=str),
                ),
            )


async def _write(sink: AuditSink, principal: CallerPrincipal, tool: str, status: str, details):
    try:
        await asyncio.wait_for(
            sink.write(principal, tool, status, details), timeout=AUDIT_WRITE_TIMEOUT_S
        )
    except TimeoutError:
        logger.warning(
            "audit write for tool %s timed out after %.1f s", tool, AUDIT_WRITE_TIMEOUT_S
        )
    except Exception:
        logger.exception("audit write failed for tool %s", tool)


async def record(
    sink: AuditSink | None,
    principal: CallerPrincipal,
    tool: str,
    *,
    latency_ms: int,
    error: str | None,
    cancelled: bool = False,
    denied: bool = False,
) -> None:
    """Write one row (bounded, shielded, never raises)."""
    if sink is None:
        return
    failed = error is not None or cancelled or denied
    details: dict[str, Any] = {
        "tool": tool,
        "latency_ms": max(0, latency_ms),
        "ok": not failed,
        "error": error[:MAX_ERROR_CHARS] if error else error,
        "auth_method": principal.auth_method,
        "principal": principal.subject,
    }
    if principal.tenant_id:
        details["tenant_id"] = principal.tenant_id
    if cancelled:
        details["cancelled"] = True
    if denied:
        details["denied"] = True
    status = "denied" if denied else "failure" if failed else "success"
    task = asyncio.ensure_future(_write(sink, principal, tool, status, details))
    _pending_writes.add(task)
    task.add_done_callback(_pending_writes.discard)
    # shield: a cancelled call still gets its row; the write is bounded, never raises.
    await asyncio.shield(task)
