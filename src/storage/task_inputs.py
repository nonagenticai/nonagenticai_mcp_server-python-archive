"""Durable inputs a task is waiting on (fr approvals surfaced as MCP task input requests)."""

import secrets
from datetime import UTC, datetime
from uuid import UUID

from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from src.execution.models import TERMINAL

ACTIONS = frozenset({"accept", "decline", "cancel"})
REASON_REQUIRED = "reason required"
#: Prefix of the message set when fr refuses a forwarded answer (``reopen``).
REFUSED_PREFIX = "fr refused the answer: "


def is_hint(message: object) -> bool:
    """A message telling the client to answer again; it stands until a valid answer clears it."""
    return isinstance(message, str) and (
        message == REASON_REQUIRED or message.startswith(REFUSED_PREFIX)
    )


def key_for(run_id: UUID) -> str:
    return f"{run_id}:{secrets.token_hex(8)}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def rejected_without_reason(response: dict) -> bool:
    content = response.get("content")
    if response["action"] != "accept" or not isinstance(content, dict):
        return False
    if content.get("decision") != "rejected":
        return False
    reason = content.get("reason")
    return not (isinstance(reason, str) and reason.strip())


class TaskInputRepository:
    def __init__(self, pool):
        self.pool = pool

    async def open(
        self,
        tenant: UUID,
        run_id: UUID,
        source_ref: str,
        request: dict,
        expires_at: datetime | None = None,
    ) -> str | None:
        """New input key, or None when (run, source_ref) already exists in any state (a closed
        tombstone included), the run is unknown or belongs to another tenant, or the approval
        already expired (fr would refuse the answer, so the input is closed instead)."""
        if expires_at is not None and _utc(expires_at) <= datetime.now(UTC):
            await self.close(tenant, run_id, source_ref)
            return None
        key = key_for(run_id)
        try:
            async with self.pool.connection() as conn, conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO public.mcp_task_inputs"
                    "(input_key,tenant_id,run_id,source_ref,request,expires_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (run_id,source_ref) DO NOTHING "
                    "RETURNING input_key",
                    (key, tenant, run_id, source_ref, Jsonb(request), expires_at),
                )
                return key if await cur.fetchone() else None
        except ForeignKeyViolation:
            return None

    async def close(self, tenant: UUID, run_id: UUID, source_ref: str) -> None:
        """Close the input; with none yet, leave a closed tombstone so a late open is ignored."""
        try:
            async with self.pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO public.mcp_task_inputs"
                    "(input_key,tenant_id,run_id,source_ref,request,state,closed_at) "
                    "VALUES (%s,%s,%s,%s,'{}'::jsonb,'closed',now()) "
                    "ON CONFLICT (run_id,source_ref) DO UPDATE SET state='closed', closed_at=now() "
                    "WHERE mcp_task_inputs.tenant_id=EXCLUDED.tenant_id "
                    "AND mcp_task_inputs.state<>'closed'",
                    (key_for(run_id), tenant, run_id, source_ref),
                )
        except ForeignKeyViolation:
            return

    async def open_for(self, tenant: UUID, run_id: UUID) -> list[dict]:
        """Every input row of the run in any state; callers filter."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM public.mcp_task_inputs WHERE tenant_id=%s AND run_id=%s "
                "ORDER BY created_at, input_key",
                (tenant, run_id),
            )
            return await cur.fetchall()

    async def answer(
        self, tenant: UUID, run_id: UUID, responses: dict[str, dict], actor: dict
    ) -> int:
        """Record valid responses for open inputs; returns how many were answered.

        Unknown or non-open keys and malformed responses are ignored. A rejection without a
        reason leaves its input open and sets the run's status message."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT status, status_message FROM public.mcp_runs "
                "WHERE tenant_id=%s AND run_id=%s FOR UPDATE",
                (tenant, run_id),
            )
            run = await cur.fetchone()
            if not run or run["status"] in TERMINAL:
                return 0
            cur = await conn.execute(
                "SELECT input_key FROM public.mcp_task_inputs "
                "WHERE run_id=%s AND tenant_id=%s AND state='open' ORDER BY input_key FOR UPDATE",
                (run_id, tenant),
            )
            open_keys = {row["input_key"] for row in await cur.fetchall()}
            answered, needs_reason = 0, False
            for key, response in responses.items():
                if key not in open_keys or not isinstance(response, dict):
                    continue
                if response.get("action") not in ACTIONS:
                    continue
                if rejected_without_reason(response):
                    needs_reason = True
                    continue
                await conn.execute(
                    "UPDATE public.mcp_task_inputs SET state='answered', response=%s, actor=%s, "
                    "answered_at=now() WHERE input_key=%s AND state='open'",
                    (Jsonb(response), Jsonb(actor), key),
                )
                answered += 1
            if needs_reason:
                await conn.execute(
                    "UPDATE public.mcp_runs SET status_message=%s WHERE tenant_id=%s AND run_id=%s",
                    (REASON_REQUIRED, tenant, run_id),
                )
            elif answered and is_hint(run["status_message"]):
                await conn.execute(
                    "UPDATE public.mcp_runs SET status_message=NULL WHERE tenant_id=%s AND run_id=%s",
                    (tenant, run_id),
                )
            return answered

    async def claim_answered(self, limit: int) -> list[dict]:
        """Answered inputs awaiting forwarding to fr, oldest first.

        Delivery is at-least-once: the row locks are released when this returns, so a crash
        between forwarding and `mark_forwarded` re-forwards. fr must dedupe on the approval id
        (`source_ref`). The driver is the single forwarder per run through its own fencing."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM public.mcp_task_inputs WHERE state='answered' "
                "ORDER BY answered_at, input_key LIMIT %s FOR UPDATE SKIP LOCKED",
                (limit,),
            )
            return await cur.fetchall()

    async def reopen(self, input_key: str, message: str) -> None:
        """fr refused a forwarded answer: open the input again and tell the client why."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE public.mcp_task_inputs SET state='open', response=NULL, actor=NULL, "
                "answered_at=NULL WHERE input_key=%s AND state='answered' RETURNING tenant_id, run_id",
                (input_key,),
            )
            item = await cur.fetchone()
            if item:
                await conn.execute(
                    "UPDATE public.mcp_runs SET status_message=%s WHERE tenant_id=%s AND run_id=%s "
                    "AND status <> ALL(%s)",
                    (message[:512], item["tenant_id"], item["run_id"], list(TERMINAL)),
                )

    async def close_everywhere(self, source_ref: str) -> None:
        """fr closed an approval: close it on every run it was surfaced on."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE public.mcp_task_inputs SET state='closed', closed_at=now() "
                "WHERE source_ref=%s AND state<>'closed'",
                (source_ref,),
            )

    async def mark_forwarded(self, input_key: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE public.mcp_task_inputs SET state='forwarded' "
                "WHERE input_key=%s AND state='answered'",
                (input_key,),
            )
