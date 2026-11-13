"""Short transactions for immutable acceptance, fenced ownership and bounded inspection."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid7

from psycopg.types.json import Jsonb

from src.broker.limits import RateLimits
from src.execution.models import TERMINAL, ExecutionDeniedError, QueueFullError
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError, QuotaExceededError


def encoded(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def value_hash(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ExecutionRepository:
    def __init__(self, pool, settings):
        self.pool, self.settings = pool, settings
        self.rate_limits = RateLimits.read(settings.api_rate_limits_file)

    async def event(self, conn, row, kind):
        # No untrusted logs or credentials are persisted. Each run has a bounded lifecycle.
        await conn.execute(
            "INSERT INTO public.mcp_run_events(tenant_id,run_id,kind) SELECT %s,%s,%s WHERE (SELECT count(*) FROM public.mcp_run_events WHERE tenant_id=%s AND run_id=%s)<256",
            (row["tenant_id"], row["run_id"], kind, row["tenant_id"], row["run_id"]),
        )

    async def accept(self, principal, resolved, inputs, key, generation, channel_actor=None):
        from src.storage.bindings import get_binding

        ref = resolved.effective.tool
        digest = value_hash(inputs)
        async with self.pool.connection() as conn, conn.transaction():
            # Serialize bounded admission across API replicas; no in-memory queue.
            await conn.execute("SELECT pg_advisory_xact_lock(72441001)")
            binding = await get_binding(conn, principal.tenant_id, principal.binding_id, lock=True)
            if (
                binding["revision"] != principal.binding_revision
                or binding["revoked"]
                or not binding["enabled"]
                or binding["expires_at"] <= datetime.now(UTC)
            ):
                raise ExecutionDeniedError()
            if binding["gateway_license_id"] != principal.license_id:
                raise ExecutionDeniedError()
            cur = await conn.execute(
                "SELECT manifest FROM public.mcp_credential_profiles WHERE tenant_id=%s AND profile_id=%s FOR SHARE",
                (principal.tenant_id, binding["credential_profile_id"]),
            )
            profile = await cur.fetchone()
            if not profile:
                raise ExecutionDeniedError()
            cur = await conn.execute("SELECT public.mcp_lock_execution_authority() AS generation")
            if (await cur.fetchone())["generation"] != generation:
                raise CatalogConflictError("authority changed during acceptance")
            if key:
                cur = await conn.execute(
                    "SELECT * FROM public.mcp_runs WHERE tenant_id=%s AND binding_id=%s AND owner_tenant_id=%s AND tool_id=%s AND tool_version=%s AND idempotency_key=%s",
                    (
                        principal.tenant_id,
                        principal.binding_id,
                        ref.tenant_id,
                        ref.id,
                        ref.version,
                        key,
                    ),
                )
                prior = await cur.fetchone()
                if prior:
                    if prior["input_hash"] != digest:
                        raise CatalogConflictError("idempotency input conflict")
                    return prior
            cur = await conn.execute(
                "SELECT count(*) AS total,count(*) FILTER(WHERE tenant_id=%s) AS tenant FROM public.mcp_runs WHERE status='queued'",
                (principal.tenant_id,),
            )
            counts = await cur.fetchone()
            if (
                counts["total"] >= self.settings.queue_global_max
                or counts["tenant"] >= self.settings.queue_per_tenant_max
            ):
                raise QueueFullError()
            run_id = uuid7()
            cur = await conn.execute(
                "INSERT INTO public.mcp_runs(run_id,tenant_id,binding_id,binding_revision,gateway_license_id,credential_profile_id,profile_snapshot,owner_tenant_id,tool_id,tool_version,authority,inputs,input_hash,idempotency_key,created_by,queue_deadline,runtime,actor) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    run_id,
                    principal.tenant_id,
                    principal.binding_id,
                    principal.binding_revision,
                    principal.license_id,
                    binding["credential_profile_id"],
                    Jsonb(profile["manifest"]),
                    ref.tenant_id,
                    ref.id,
                    ref.version,
                    Jsonb(
                        {
                            "manifest": resolved.manifest_json,
                            "effective": resolved.effective_json,
                            "context": resolved.context_json,
                        }
                    ),
                    Jsonb(inputs),
                    digest,
                    key,
                    Jsonb(principal.actor.model_dump(mode="json")),
                    datetime.now(UTC)
                    + timedelta(
                        seconds=min(
                            resolved.effective.limits.queue_seconds,
                            self.settings.run_queue_timeout_seconds,
                        )
                    ),
                    resolved.manifest.runtime,
                    Jsonb(channel_actor) if channel_actor is not None else None,
                ),
            )
            row = await cur.fetchone()
            await conn.execute(
                "INSERT INTO public.mcp_tenant_execution_locks(tenant_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (principal.tenant_id,),
            )
            await self.event(conn, row, "queued")
            return row

    async def accept_draft(
        self, tenant, license_id, resolved, profile, inputs, key, generation, actor
    ):
        """Queue an author's test run: no binding, an ephemeral profile snapshot, own tenant only.

        Serialized with ``accept`` on the same admission lock, so the queue bounds and the hourly
        test-run limit are race-free."""
        ref = resolved.effective.tool
        digest = value_hash(inputs)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(72441001)")
            cur = await conn.execute("SELECT public.mcp_lock_execution_authority() AS generation")
            if (await cur.fetchone())["generation"] != generation:
                raise CatalogConflictError("authority changed during acceptance")
            if key:
                cur = await conn.execute(
                    "SELECT * FROM public.mcp_runs WHERE mode='draft' AND tenant_id=%s AND tool_id=%s AND tool_version=%s AND idempotency_key=%s",
                    (tenant, ref.id, ref.version, key),
                )
                prior = await cur.fetchone()
                if prior:
                    if prior["input_hash"] != digest:
                        raise CatalogConflictError("idempotency input conflict")
                    return prior
            cur = await conn.execute(
                "SELECT count(*) AS n FROM public.mcp_runs WHERE mode='draft' AND tenant_id=%s AND created_at>clock_timestamp()-interval '1 hour'",
                (tenant,),
            )
            if (await cur.fetchone())["n"] >= self.settings.builder_test_runs_per_hour:
                raise QuotaExceededError("test runs per hour")
            cur = await conn.execute(
                "SELECT count(*) AS total,count(*) FILTER(WHERE tenant_id=%s) AS tenant FROM public.mcp_runs WHERE status='queued'",
                (tenant,),
            )
            counts = await cur.fetchone()
            if (
                counts["total"] >= self.settings.queue_global_max
                or counts["tenant"] >= self.settings.queue_per_tenant_max
            ):
                raise QueueFullError()
            run_id = uuid7()
            cur = await conn.execute(
                "INSERT INTO public.mcp_runs(run_id,mode,tenant_id,binding_id,binding_revision,gateway_license_id,credential_profile_id,profile_snapshot,owner_tenant_id,tool_id,tool_version,authority,inputs,input_hash,idempotency_key,created_by,queue_deadline) "
                "VALUES (%s,'draft',%s,NULL,NULL,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    run_id,
                    tenant,
                    license_id,
                    Jsonb(profile.model_dump(mode="json")),
                    tenant,
                    ref.id,
                    ref.version,
                    Jsonb(
                        {
                            "manifest": resolved.manifest_json,
                            "effective": resolved.effective_json,
                            "context": resolved.context_json,
                        }
                    ),
                    Jsonb(inputs),
                    digest,
                    key,
                    Jsonb(actor.model_dump(mode="json")),
                    datetime.now(UTC)
                    + timedelta(
                        seconds=min(
                            resolved.effective.limits.queue_seconds,
                            self.settings.run_queue_timeout_seconds,
                        )
                    ),
                ),
            )
            row = await cur.fetchone()
            await conn.execute(
                "INSERT INTO public.mcp_tenant_execution_locks(tenant_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (tenant,),
            )
            await self.event(conn, row, "queued")
            return row

    async def get_draft(self, tenant, run_id):
        """A draft run of this tenant; a bound run or another tenant's run is not found."""
        row = await self.get(tenant, run_id)
        if row["mode"] != "draft":
            raise CatalogNotFoundError("run not found")
        return row

    async def trace(self, tenant, run_id):
        """The run's upstream calls in order: what was sent where, and what came back."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT sequence,operation_id,operation_version,method,path,effect,status,http_status,duration_ms,reason,error_excerpt,started_at,finished_at "
                "FROM public.mcp_run_api_calls WHERE tenant_id=%s AND run_id=%s ORDER BY sequence",
                (tenant, run_id),
            )
            return await cur.fetchall()

    async def get(self, tenant, run_id):
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM public.mcp_runs WHERE tenant_id=%s AND run_id=%s", (tenant, run_id)
            )
            row = await cur.fetchone()
            if not row:
                raise CatalogNotFoundError("run not found")
            return row

    async def events(self, tenant, run_id, after, limit):
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT event_id,kind,created_at FROM public.mcp_run_events WHERE tenant_id=%s AND run_id=%s AND event_id>%s ORDER BY event_id LIMIT %s",
                (tenant, run_id, after, limit),
            )
            return await cur.fetchall()

    async def cancel(self, tenant, run_id):
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT * FROM public.mcp_runs WHERE tenant_id=%s AND run_id=%s FOR UPDATE",
                (tenant, run_id),
            )
            row = await cur.fetchone()
            if not row:
                raise CatalogNotFoundError("run not found")
            if row["status"] not in TERMINAL and not row["cancel_requested"]:
                # Only worker may transition active runs. API's sole update is the flag.
                await conn.execute(
                    "UPDATE public.mcp_runs SET cancel_requested=true WHERE tenant_id=%s AND run_id=%s",
                    (tenant, run_id),
                )
                await self.event(conn, row, "cancel_requested")
            return {**row, "cancel_requested": True}

    async def owned(self, conn, run_id, worker, fence, *, allow_cancel=False):
        cur = await conn.execute(
            "SELECT * FROM public.mcp_runs WHERE run_id=%s AND worker_id=%s AND fence=%s AND lease_until>clock_timestamp() FOR UPDATE",
            (run_id, worker, fence),
        )
        row = await cur.fetchone()
        if not row or (
            not allow_cancel
            and (
                row["cancel_requested"]
                or row["status"] not in {"starting", "running"}
                or row["deadline"] <= datetime.now(UTC)
            )
        ):
            raise ExecutionDeniedError()
        return row

    async def heartbeat(self, run_id, worker, fence):
        async with self.pool.connection() as conn, conn.transaction():
            row = await self.owned(conn, run_id, worker, fence, allow_cancel=True)
            await conn.execute(
                "UPDATE public.mcp_runs SET lease_until=clock_timestamp()+%s*interval '1 second',heartbeat_at=clock_timestamp() WHERE run_id=%s",
                (self.settings.lease_seconds, run_id),
            )
            await conn.execute(
                "UPDATE public.mcp_api_permits SET expires_at=clock_timestamp()+%s*interval '1 second' WHERE run_id=%s AND fence=%s",
                (self.settings.lease_seconds, run_id, fence),
            )
            return row

    async def pod(self, row, name, uid=None):
        async with self.pool.connection() as conn, conn.transaction():
            await self.owned(conn, row["run_id"], row["worker_id"], row["fence"])
            await conn.execute(
                "UPDATE public.mcp_runs SET pod_name=%s,pod_uid=coalesce(%s,pod_uid) WHERE run_id=%s",
                (name, uid, row["run_id"]),
            )

    async def running(self, row):
        async with self.pool.connection() as conn, conn.transaction():
            await self.owned(conn, row["run_id"], row["worker_id"], row["fence"])
            cur = await conn.execute(
                "UPDATE public.mcp_runs SET status='running',started_at=clock_timestamp(),deadline=least(deadline,clock_timestamp()+%s*interval '1 second') WHERE run_id=%s AND status='starting' RETURNING started_at,deadline",
                (
                    min(
                        json.loads(row["authority"]["effective"])["limits"]["wall_seconds"],
                        self.settings.run_max_seconds,
                    ),
                    row["run_id"],
                ),
            )
            started = await cur.fetchone()
            if started:
                row.update(started)
            await self.event(conn, row, "running")

    async def finish(self, row, status, *, result=None, code=None, absent=False):
        async with self.pool.connection() as conn, conn.transaction():
            current = await self.owned(
                conn, row["run_id"], row["worker_id"], row["fence"], allow_cancel=True
            )
            if current["status"] not in TERMINAL:
                if current["cancel_requested"]:
                    if current["status"] != "cancelling":
                        await conn.execute(
                            "UPDATE public.mcp_runs SET status='cancelling' WHERE run_id=%s",
                            (row["run_id"],),
                        )
                    status, result, code = ("cancelled" if absent else "interrupted"), None, None
                await conn.execute(
                    "UPDATE public.mcp_runs SET status=%s,result=%s,error=%s,finished_at=clock_timestamp() WHERE run_id=%s",
                    (
                        status,
                        Jsonb(result),
                        Jsonb({"code": code, "message": code.replace("_", " ")}) if code else None,
                        row["run_id"],
                    ),
                )
                await self.event(conn, row, status)
            if absent:
                # Channel loss/cancellation can leave an intent without an outcome.
                # Finalize it before freeing ownership, even on graceful shutdown.
                await conn.execute(
                    "UPDATE public.mcp_run_api_calls SET status='outcome_unknown',finished_at=clock_timestamp() WHERE tenant_id=%s AND run_id=%s AND status='intent'",
                    (row["tenant_id"], row["run_id"]),
                )
                await conn.execute(
                    "UPDATE public.mcp_execution_slots SET run_id=NULL,quarantined=false WHERE run_id=%s",
                    (row["run_id"],),
                )
                await conn.execute(
                    "DELETE FROM public.mcp_api_permits WHERE run_id=%s", (row["run_id"],)
                )
                await conn.execute(
                    "UPDATE public.mcp_runs SET worker_id=NULL,lease_until=NULL WHERE run_id=%s",
                    (row["run_id"],),
                )
            else:
                await conn.execute(
                    "UPDATE public.mcp_execution_slots SET quarantined=true WHERE run_id=%s",
                    (row["run_id"],),
                )
                await self.event(conn, row, "slot_quarantined")

    async def journal_intent(
        self,
        row,
        operation,
        request_hash,
        key,
        license_id,
        call_limit=None,
        *,
        method=None,
        path=None,
    ):
        """Commit the intent before network I/O. ``method`` and ``path`` (the concrete request
        path, no query and no credential) are written here and are immutable afterwards."""
        async with self.pool.connection() as conn, conn.transaction():
            current = await self.owned(conn, row["run_id"], row["worker_id"], row["fence"])
            limit = min(
                json.loads(row["authority"]["effective"])["limits"]["api_calls"],
                self.settings.max_api_calls_per_run,
            )
            if call_limit is not None:
                limit = min(limit, call_limit)
            if current["api_calls"] >= limit:
                raise ExecutionDeniedError()
            cur = await conn.execute(
                "SELECT count(*) AS n FROM public.mcp_run_api_calls WHERE tenant_id=%s AND run_id=%s AND operation_id=%s AND operation_version=%s",
                (row["tenant_id"], row["run_id"], operation.operation_id, operation.version),
            )
            if (await cur.fetchone())["n"] >= operation.pagination_limit:
                raise ExecutionDeniedError()
            sequence = current["api_calls"] + 1
            await conn.execute(
                "UPDATE public.mcp_runs SET api_calls=%s WHERE run_id=%s", (sequence, row["run_id"])
            )
            await conn.execute(
                "INSERT INTO public.mcp_run_api_calls(tenant_id,run_id,sequence,operation_id,operation_version,request_hash,idempotency_key,license_id,effect,method,path) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    row["tenant_id"],
                    row["run_id"],
                    sequence,
                    operation.operation_id,
                    operation.version,
                    request_hash,
                    key,
                    license_id,
                    operation.effect,
                    method,
                    None if path is None else path[:1024],
                ),
            )
            return sequence

    async def journal_outcome(
        self,
        row,
        sequence,
        status,
        *,
        http_status=None,
        duration_ms=None,
        reason=None,
        error_excerpt=None,
    ):
        """Finalize an intent. Only the outcome columns are updatable (migration 0010), so
        ``method`` and ``path`` belong to ``journal_intent``. ``error_excerpt`` (redacted, at most
        2 KB) is written by the broker for draft runs only."""
        async with self.pool.connection() as conn, conn.transaction():
            await self.owned(conn, row["run_id"], row["worker_id"], row["fence"], allow_cancel=True)
            await conn.execute(
                "UPDATE public.mcp_run_api_calls SET status=%s,finished_at=clock_timestamp(),http_status=%s,duration_ms=%s,reason=%s,error_excerpt=%s WHERE tenant_id=%s AND run_id=%s AND sequence=%s AND status='intent'",
                (
                    status,
                    http_status,
                    duration_ms,
                    reason,
                    error_excerpt[:2048] if error_excerpt else None,
                    row["tenant_id"],
                    row["run_id"],
                    sequence,
                ),
            )

    async def permit(self, row, service_id):
        async with self.pool.connection() as conn, conn.transaction():
            await self.owned(conn, row["run_id"], row["worker_id"], row["fence"])
            # A common lock serializes the global count across different services.
            await conn.execute("SELECT pg_advisory_xact_lock(72441003)")
            await conn.execute(
                "DELETE FROM public.mcp_api_permits WHERE expires_at<=clock_timestamp()"
            )
            await conn.execute(
                "DELETE FROM public.mcp_api_rate_windows WHERE window_start<date_trunc('minute',clock_timestamp())-interval '1 minute'"
            )
            calls, concurrency = self.rate_limits.ceiling(
                row["tenant_id"],
                service_id,
                self.settings.api_max_calls_per_minute,
                min(self.settings.api_max_inflight, self.settings.api_concurrency_per_service),
            )
            cur = await conn.execute(
                "SELECT count(*) AS total,count(*) FILTER(WHERE service_id=%s) AS service FROM public.mcp_api_permits",
                (service_id,),
            )
            counts = await cur.fetchone()
            if (
                counts["total"] >= self.settings.api_concurrency_global
                or counts["service"] >= concurrency
            ):
                await self.event(conn, row, "api_concurrency_denied")
                denied = True
            else:
                denied = False
            cur = await conn.execute(
                "SELECT calls FROM public.mcp_api_rate_windows WHERE tenant_id=%s AND service_id=%s AND window_start=date_trunc('minute',clock_timestamp())",
                (row["tenant_id"], service_id),
            )
            counter = await cur.fetchone()
            if counter and counter["calls"] >= calls:
                await self.event(conn, row, "api_rate_denied")
                denied = True
            if denied:
                # Commit the fixed denial event; raising within this transaction
                # would roll it back together with the failed permit.
                permit_id = None
            else:
                permit_id = uuid7()
                await conn.execute(
                    "INSERT INTO public.mcp_api_rate_windows VALUES (%s,%s,date_trunc('minute',clock_timestamp()),1) ON CONFLICT(tenant_id,service_id,window_start) DO UPDATE SET calls=mcp_api_rate_windows.calls+1",
                    (row["tenant_id"], service_id),
                )
                await conn.execute(
                    "INSERT INTO public.mcp_api_permits VALUES (%s,%s,%s,%s,%s,clock_timestamp()+%s*interval '1 second')",
                    (
                        permit_id,
                        row["tenant_id"],
                        row["run_id"],
                        row["fence"],
                        service_id,
                        self.settings.lease_seconds,
                    ),
                )
        if permit_id is None:
            raise ExecutionDeniedError()
        return permit_id

    async def expire_payloads(self):
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT public.mcp_expire_run_payloads(%s,128) AS expired",
                (self.settings.execution_retention_days,),
            )
            return (await cur.fetchone())["expired"]

    async def release_permit(self, row, permit_id):
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.mcp_api_permits WHERE permit_id=%s AND run_id=%s AND fence=%s",
                (permit_id, row["run_id"], row["fence"]),
            )
