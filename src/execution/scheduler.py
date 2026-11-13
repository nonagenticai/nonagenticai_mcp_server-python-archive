"""Cluster-wide fair claims. Slot ownership survives controller crashes."""

import json

from src.execution.models import ExecutionDeniedError


class Scheduler:
    def __init__(self, repository):
        self.repository = repository
        self.settings = repository.settings

    async def verify_capacity(self):
        async with self.repository.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n,min(slot_id) AS first,max(slot_id) AS last FROM public.mcp_execution_slots WHERE NOT draining"
            )
            row = await cur.fetchone()
            if row != {
                "n": self.settings.execution_global_slots,
                "first": 1,
                "last": self.settings.execution_global_slots,
            }:
                raise ExecutionDeniedError("operator must provision exact shared capacity")

    async def claim(self, worker):
        async with self.repository.pool.connection() as conn, conn.transaction():
            # One short global claim lock makes tenant/global decisions atomic. Pod/HTTP
            # operations never occur while this or any row lock is held.
            await conn.execute("SELECT pg_advisory_xact_lock(72441002)")
            cur = await conn.execute(
                "UPDATE public.mcp_runs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'timed_out' END,finished_at=clock_timestamp() "
                "WHERE status='queued' AND (cancel_requested OR queue_deadline<=clock_timestamp()) RETURNING *"
            )
            for expired in await cur.fetchall():
                await self.repository.event(conn, expired, expired["status"])
            cur = await conn.execute(
                "SELECT slot_id FROM public.mcp_execution_slots WHERE run_id IS NULL AND NOT quarantined AND NOT draining ORDER BY slot_id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            slot = await cur.fetchone()
            if not slot:
                return None
            cur = await conn.execute(
                "SELECT r.* FROM public.mcp_runs r JOIN public.mcp_tenant_execution_locks t USING(tenant_id) "
                "WHERE r.status='queued' AND r.runtime='kata_python' AND NOT r.cancel_requested AND r.queue_deadline>clock_timestamp() AND "
                "(SELECT count(*) FROM public.mcp_execution_slots s JOIN public.mcp_runs a ON a.run_id=s.run_id WHERE a.tenant_id=r.tenant_id)<%s "
                "ORDER BY t.last_claimed_at,r.created_at,r.run_id LIMIT 1 FOR UPDATE OF r,t SKIP LOCKED",
                (self.settings.execution_per_tenant_slots,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            await conn.execute(
                "UPDATE public.mcp_tenant_execution_locks SET last_claimed_at=clock_timestamp() WHERE tenant_id=%s",
                (row["tenant_id"],),
            )
            limits = json.loads(row["authority"]["effective"])["limits"]
            cur = await conn.execute(
                "UPDATE public.mcp_runs SET status='starting',worker_id=%s,fence=fence+1,lease_until=clock_timestamp()+%s*interval '1 second',heartbeat_at=clock_timestamp(),start_deadline=clock_timestamp()+%s*interval '1 second',deadline=clock_timestamp()+%s*interval '1 second' WHERE run_id=%s RETURNING *",
                (
                    worker,
                    self.settings.lease_seconds,
                    self.settings.run_start_timeout_seconds,
                    self.settings.run_start_timeout_seconds
                    + min(limits["wall_seconds"], self.settings.run_max_seconds),
                    row["run_id"],
                ),
            )
            row = await cur.fetchone()
            await conn.execute(
                "UPDATE public.mcp_execution_slots SET run_id=%s WHERE slot_id=%s",
                (row["run_id"], slot["slot_id"]),
            )
            await self.repository.event(conn, row, "starting")
            return row
