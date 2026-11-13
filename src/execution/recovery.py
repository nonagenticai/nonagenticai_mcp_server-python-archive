"""Lease takeover and pod reconciliation. Absence is confirmed before slot release."""

from src.execution.models import TERMINAL, ExecutionDeniedError


class Recovery:
    def __init__(self, repository, backend):
        self.repository, self.backend = repository, backend

    async def reconcile(self, worker):
        rows = []
        async with self.repository.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT r.* FROM public.mcp_runs r JOIN public.mcp_execution_slots s ON s.run_id=r.run_id WHERE r.lease_until<=clock_timestamp() ORDER BY r.lease_until LIMIT 128 FOR UPDATE OF r SKIP LOCKED"
            )
            for row in await cur.fetchall():
                # Fence out all old broker channels before touching their pods.
                cur = await conn.execute(
                    "UPDATE public.mcp_runs SET worker_id=%s,fence=fence+1,lease_until=clock_timestamp()+%s*interval '1 second' WHERE run_id=%s RETURNING *",
                    (worker, self.repository.settings.lease_seconds, row["run_id"]),
                )
                claimed = await cur.fetchone()
                rows.append((row, claimed))
        for old, row in rows:
            absent = False
            try:
                if (
                    old["status"] == "starting"
                    and old["pod_name"] is None
                    and old["started_at"] is None
                    and old["api_calls"] == 0
                    and not old["cancel_requested"]
                ):
                    async with self.repository.pool.connection() as conn, conn.transaction():
                        await self.repository.owned(
                            conn, row["run_id"], worker, row["fence"], allow_cancel=True
                        )
                        await conn.execute(
                            "UPDATE public.mcp_runs SET status='queued',worker_id=NULL,lease_until=NULL,start_deadline=NULL,deadline=NULL WHERE run_id=%s",
                            (row["run_id"],),
                        )
                        await conn.execute(
                            "UPDATE public.mcp_execution_slots SET run_id=NULL,quarantined=false WHERE run_id=%s",
                            (row["run_id"],),
                        )
                        await self.repository.event(conn, row, "requeued_unstarted")
                    continue
                pod = await self.backend.inspect(old["pod_name"]) if old["pod_name"] else None
                if pod and (
                    pod.run_id != str(row["run_id"])
                    or pod.fence != int(old["pod_name"].rsplit("-", 1)[1])
                    or (old["pod_uid"] and old["pod_uid"] != pod.uid)
                ):
                    raise ExecutionDeniedError()
                absent = pod is None or await self.backend.stop(pod)
                async with self.repository.pool.connection() as conn, conn.transaction():
                    await self.repository.owned(
                        conn, row["run_id"], worker, row["fence"], allow_cancel=True
                    )
                    await conn.execute(
                        "UPDATE public.mcp_run_api_calls SET status='outcome_unknown',finished_at=clock_timestamp() WHERE tenant_id=%s AND run_id=%s AND status='intent'",
                        (row["tenant_id"], row["run_id"]),
                    )
                await self.repository.finish(row, "interrupted", code="worker_lost", absent=absent)
            except Exception:
                # Keep ownership/slot until a subsequent reconciliation can prove
                # absence. No delete response or API timeout frees capacity.
                try:
                    await self.repository.finish(
                        row, "interrupted", code="cleanup_unconfirmed", absent=False
                    )
                except ExecutionDeniedError:
                    pass
        # Remove genuinely orphaned managed pods and pods left after terminal runs.
        for pod in await self.backend.list():
            async with self.repository.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT status,lease_until>clock_timestamp() AS live,pod_name,pod_uid FROM public.mcp_runs WHERE run_id=%s",
                    (pod.run_id,),
                )
                row = await cur.fetchone()
            if row is None or (row["status"] in TERMINAL and not row["live"]):
                await self.backend.stop(pod)
