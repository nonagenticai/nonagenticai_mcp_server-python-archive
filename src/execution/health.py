"""Internal worker probes and low-cardinality operational metrics."""

import asyncio

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from src.storage.schema import verify_schema


def worker_app(repository, scheduler, backend, credentials):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        try:
            async with asyncio.timeout(8):
                credentials.read()
                async with repository.pool.connection() as conn:
                    await verify_schema(
                        conn, repository.settings.platform_tenant_id, require_audit=False
                    )
                await scheduler.verify_capacity()
                await backend.ready()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.get("/metrics")
    async def metrics():
        async with repository.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FILTER(WHERE status='queued') AS queued,count(*) FILTER(WHERE status='starting') AS starting,count(*) FILTER(WHERE status='running') AS running,coalesce(extract(epoch FROM clock_timestamp()-min(created_at) FILTER(WHERE status='queued')),0) AS queue_age_seconds FROM public.mcp_runs WHERE status IN ('queued','starting','running')"
            )
            values = dict(await cur.fetchone())
            cur = await conn.execute(
                "SELECT count(*) FILTER(WHERE run_id IS NOT NULL) AS slots_used,count(*) FILTER(WHERE quarantined) AS slots_quarantined,count(*) FILTER(WHERE draining) AS slots_draining FROM public.mcp_execution_slots"
            )
            values.update(await cur.fetchone())
            cur = await conn.execute(
                "SELECT coalesce(avg(extract(epoch FROM finished_at-started_at)),0) AS api_duration_seconds FROM (SELECT started_at,finished_at FROM public.mcp_run_api_calls ORDER BY started_at DESC LIMIT 10000) recent WHERE finished_at IS NOT NULL"
            )
            values.update(await cur.fetchone())
            cur = await conn.execute(
                "SELECT coalesce(avg(extract(epoch FROM finished_at-started_at)),0) AS run_duration_seconds,coalesce(avg(extract(epoch FROM started_at-created_at)),0) AS cold_start_seconds FROM (SELECT created_at,started_at,finished_at FROM public.mcp_runs ORDER BY run_id DESC LIMIT 10000) recent WHERE started_at IS NOT NULL"
            )
            values.update(await cur.fetchone())
            # A bounded recent sample avoids an unbounded scan of immutable history.
            cur = await conn.execute(
                "SELECT status,count(*) AS n FROM (SELECT status FROM public.mcp_runs ORDER BY run_id DESC LIMIT 10000) recent GROUP BY status"
            )
            lines = [f"mcp_execution_{key} {value}" for key, value in values.items()]
            for row in await cur.fetchall():
                lines.append(f'mcp_execution_recent_runs{{status="{row["status"]}"}} {row["n"]}')
            cur = await conn.execute(
                "SELECT kind,count(*) AS n FROM (SELECT kind FROM public.mcp_run_events ORDER BY event_id DESC LIMIT 10000) recent WHERE kind IN ('broker_denied','api_concurrency_denied','api_rate_denied','slot_quarantined','orphan_cleaned','requeued_unstarted','interrupted','cancel_requested') GROUP BY kind"
            )
            for row in await cur.fetchall():
                lines.append(f'mcp_execution_recent_events{{kind="{row["kind"]}"}} {row["n"]}')
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return app
