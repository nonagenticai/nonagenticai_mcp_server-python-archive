"""Drives non-Kata runs (n8n workflows, fr projects) through their external systems.

One process is one worker id. Adapters speak HTTP only; this module is the single writer of
``mcp_runs`` for external runtimes. A run is dispatched at most once: a ``dispatch`` event is
committed before ``adapter.start`` and a run found ``starting`` with that event but no
``external_ref`` is ``interrupted``, never re-sent. External runs never take an execution slot.

``worker_max_inflight`` bounds runs being dispatched at this moment; a ``running`` external run
(an fr project can run for days) costs one poll per tick and never a dispatch slot.
``external_active_max`` caps every external run this process owns, and ``external_concurrency``
bounds adapter calls in flight in the single-replica event loop.

Logs carry run ids and exception class names only: no settings, request or response bodies,
inputs or exception messages (transport errors embed URLs).
"""

import asyncio
import logging
import signal
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid7

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from src.config import Settings
from src.execution.models import TERMINAL, ExecutionDeniedError
from src.external.adapter import AnswerForwarder, ExternalAdapter, ExternalStartError, ExternalState
from src.external.fr import FrAdapter, FrCommandAdapter
from src.external.n8n import N8nAdapter
from src.storage.database import Database
from src.storage.executions import ExecutionRepository
from src.storage.schema import verify_schema
from src.storage.task_inputs import REASON_REQUIRED, REFUSED_PREFIX, TaskInputRepository
from src.tasks.activity import record as record_activity

logger = logging.getLogger(__name__)

ACTIVE = ("starting", "running", "cancelling")
MESSAGE_LIMIT = 512


class DriverConfigError(ValueError):
    """The driver is not allowed to start with this configuration."""


class ExternalDriver:
    def __init__(
        self,
        repository: ExecutionRepository,
        inputs: TaskInputRepository,
        adapters: Mapping[str, ExternalAdapter],
        settings: Settings,
        worker: UUID,
    ):
        self.repository, self.inputs, self.adapters = repository, inputs, adapters
        self.settings, self.worker = settings, worker
        # Bounds adapter calls (start, poll) in flight; a long-running project is one poll each
        # tick, never a dispatch slot.
        self._calls = asyncio.Semaphore(settings.external_concurrency)

    async def tick(self) -> None:
        await self._expire_queued()
        # Recovery first: runs a dead driver abandoned are this worker's work now, and they
        # must count against dispatch room before any new run is claimed.
        recovered = await self._recover()
        claimed = await self._claim()
        await asyncio.gather(*(self._isolated(self._start, row) for row in recovered + claimed))
        await self._drive()
        await self._forward()

    # -- helpers ---------------------------------------------------------------------------

    async def _renewing(self, row, call):
        """Await one adapter call while keeping the run's lease alive.

        An adapter call may take up to ``api_timeout_seconds`` (300 s) while the lease is
        ``lease_seconds`` (30 s). Without this heartbeat a slow fr answer makes the driver lose
        a run it still owns — and fr may already have created the project — while a slow poll
        drops a terminal result. Cancellation of this coroutine cancels the call too.
        """
        task = asyncio.ensure_future(call)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self.settings.heartbeat_seconds)
                if done:
                    return task.result()
                try:
                    await self.repository.heartbeat(row["run_id"], self.worker, row["fence"])
                except ExecutionDeniedError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                except Exception as exc:
                    logger.warning(
                        "external heartbeat failed for %s: %s", row["run_id"], type(exc).__name__
                    )
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _isolated(self, step, row):
        """Run one run's step; a lost lease skips it and any other error never leaks out."""
        try:
            async with self._calls:
                await step(row)
        except ExecutionDeniedError:
            logger.info("external run %s no longer owned; skipped", row["run_id"])
        except Exception as exc:
            logger.warning("external run %s step failed: %s", row["run_id"], type(exc).__name__)

    async def _activity(self, row, kind: str, text: str) -> None:
        """One feed row for the platform's ``tasks/get``; a feed failure never fails the run."""
        try:
            await record_activity(self.repository.pool, row["tenant_id"], row["run_id"], kind, text)
        except Exception as exc:
            logger.warning("activity record failed for %s: %s", row["run_id"], type(exc).__name__)

    # -- 0. queued expiry ------------------------------------------------------------------

    async def _expire_queued(self) -> None:
        async with self.repository.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "UPDATE public.mcp_runs SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'timed_out' END,"
                "finished_at=clock_timestamp() "
                "WHERE status='queued' AND runtime<>'kata_python' "
                "AND (cancel_requested OR queue_deadline<=clock_timestamp()) RETURNING *"
            )
            for expired in await cur.fetchall():
                await self.repository.event(conn, expired, expired["status"])

    # -- 1. claim --------------------------------------------------------------------------

    async def _claim(self) -> list[dict]:
        settings = self.settings
        async with self.repository.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT count(*) FILTER (WHERE status='starting') AS starting, count(*) AS active "
                "FROM public.mcp_runs "
                "WHERE worker_id=%s AND runtime<>'kata_python' AND status = ANY(%s)",
                (self.worker, list(ACTIVE)),
            )
            counts = await cur.fetchone()
            # ``worker_max_inflight`` bounds runs being *dispatched*, not runs that already
            # started: an external project can run for days and only costs a poll each tick.
            # ``external_active_max`` is the load guard for everything this process owns.
            room = min(
                settings.worker_max_inflight - counts["starting"],
                settings.external_active_max - counts["active"],
            )
            if room <= 0:
                return []
            cur = await conn.execute(
                "SELECT run_id FROM public.mcp_runs WHERE status='queued' AND runtime<>'kata_python' "
                "AND NOT cancel_requested AND queue_deadline>clock_timestamp() "
                "ORDER BY created_at,run_id LIMIT %s FOR UPDATE SKIP LOCKED",
                (room,),
            )
            claimed = []
            for picked in await cur.fetchall():
                cur = await conn.execute(
                    "UPDATE public.mcp_runs SET status='starting',worker_id=%s,fence=fence+1,"
                    "lease_until=clock_timestamp()+%s*interval '1 second',heartbeat_at=clock_timestamp(),"
                    "start_deadline=clock_timestamp()+%s*interval '1 second',"
                    "deadline=clock_timestamp()+%s*interval '1 second' WHERE run_id=%s RETURNING *",
                    (
                        self.worker,
                        settings.lease_seconds,
                        settings.run_start_timeout_seconds,
                        settings.external_run_max_seconds,
                        picked["run_id"],
                    ),
                )
                row = await cur.fetchone()
                await self.repository.event(conn, row, "starting")
                claimed.append(row)
            return claimed

    # -- 2. start --------------------------------------------------------------------------

    async def _start(self, row) -> None:
        adapter = self.adapters.get(row["runtime"])
        if adapter is None:
            await self.repository.finish(row, "failed", code="external_unconfigured", absent=True)
            return
        async with self.repository.pool.connection() as conn, conn.transaction():
            current = await self.repository.owned(
                conn, row["run_id"], row["worker_id"], row["fence"], allow_cancel=True
            )
            if current["cancel_requested"]:
                cancelled = True
            else:
                cancelled = False
                # Committed before any network I/O: recovery can tell "maybe sent" from "never sent".
                await self.repository.event(conn, row, "dispatch")
        if cancelled:
            await self.repository.finish(row, "cancelled", absent=True)
            return
        try:
            ref = await self._renewing(row, adapter.start(row))
        except ExternalStartError as exc:
            await self.repository.finish(row, "failed", code=exc.code, absent=True)
            return
        except Exception as exc:
            # Timeout or transport failure: the work may have started. Never re-dispatch.
            logger.warning(
                "external start outcome unknown for %s: %s", row["run_id"], type(exc).__name__
            )
            await self.repository.finish(
                row, "interrupted", code="external_start_unknown", absent=True
            )
            return
        async with self.repository.pool.connection() as conn, conn.transaction():
            # allow_cancel: a cancel that arrived during start must not lose the reference.
            await self.repository.owned(
                conn, row["run_id"], row["worker_id"], row["fence"], allow_cancel=True
            )
            await conn.execute(
                # Merge: an fr event may already have recorded what the start produced.
                "UPDATE public.mcp_runs SET external_ref=COALESCE(external_ref,'{}'::jsonb)||%s,"
                "status='running',started_at=clock_timestamp() WHERE run_id=%s AND status='starting'",
                (Jsonb(ref.stored()), row["run_id"]),
            )
            await self.repository.event(conn, row, "running")
        # One feed row per start, never per tick: the platform's progress panel starts here. The
        # text is what the end user reads verbatim, so it is a sentence, not the kind's name.
        started = (
            "The project has started."
            if row.get("runtime") == "fr_project"
            else "The run has started."
        )
        await self._activity(row, "started", started)
        if ref.result is not None:
            # A one-shot channel command (fr_command) has its answer already: finish in the
            # same tick. The result is also in external_ref, so a crash before this call
            # completes on the next tick instead of polling an external project.
            await self.repository.finish(row, "succeeded", result=ref.result, absent=True)

    # -- 3. recovery -----------------------------------------------------------------------

    async def _recover(self) -> list[dict]:
        """Adopt external runs whose lease lapsed. Returns never-dispatched runs to start."""
        adopted = []
        async with self.repository.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT run_id FROM public.mcp_runs WHERE status = ANY(%s) AND runtime<>'kata_python' "
                "AND (lease_until IS NULL OR lease_until<=clock_timestamp()) "
                "ORDER BY created_at,run_id LIMIT 128 FOR UPDATE SKIP LOCKED",
                (list(ACTIVE),),
            )
            for picked in await cur.fetchall():
                cur = await conn.execute(
                    "UPDATE public.mcp_runs SET worker_id=%s,fence=fence+1,"
                    "lease_until=clock_timestamp()+%s*interval '1 second',heartbeat_at=clock_timestamp() "
                    "WHERE run_id=%s RETURNING *",
                    (self.worker, self.settings.lease_seconds, picked["run_id"]),
                )
                row = await cur.fetchone()
                dispatched = False
                if row["status"] == "starting":
                    cur = await conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM public.mcp_run_events "
                        "WHERE tenant_id=%s AND run_id=%s AND kind='dispatch') AS sent",
                        (row["tenant_id"], row["run_id"]),
                    )
                    dispatched = (await cur.fetchone())["sent"]
                adopted.append((row, dispatched))
        to_start = []
        for row, dispatched in adopted:
            if row["status"] != "starting":
                continue  # running/cancelling: the drive step keeps polling (idempotent)
            if row["external_ref"] is None and dispatched:
                await self._isolated(self._interrupt_unknown, row)
            elif row["external_ref"] is None:
                to_start.append(row)
        return to_start

    async def _interrupt_unknown(self, row) -> None:
        await self.repository.finish(row, "interrupted", code="external_start_unknown", absent=True)

    # -- 4. drive --------------------------------------------------------------------------

    async def _drive(self) -> None:
        async with self.repository.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id,fence FROM public.mcp_runs WHERE worker_id=%s AND runtime<>'kata_python' "
                "AND status IN ('running','cancelling') AND lease_until>clock_timestamp() "
                "ORDER BY created_at,run_id",
                (self.worker,),
            )
            mine = await cur.fetchall()
        fresh = []
        # Renew every lease first so a slow external call cannot expire the others.
        for run in mine:
            try:
                fresh.append(
                    await self.repository.heartbeat(run["run_id"], self.worker, run["fence"])
                )
            except ExecutionDeniedError:
                logger.info("external run %s lost its lease; skipped", run["run_id"])
            except Exception as exc:
                logger.warning(
                    "external heartbeat failed for %s: %s", run["run_id"], type(exc).__name__
                )
        await asyncio.gather(*(self._isolated(self._drive_one, row) for row in fresh))

    async def _drive_one(self, row) -> None:
        adapter = self.adapters[row["runtime"]]
        if row["deadline"] <= datetime.now(UTC):
            try:
                await adapter.cancel(row)
            except Exception as exc:
                logger.warning(
                    "best-effort cancel failed for %s: %s", row["run_id"], type(exc).__name__
                )
            await self.repository.finish(row, "timed_out", absent=True)
            return
        if row["cancel_requested"] and row["status"] == "running":
            await adapter.cancel(row)  # an error retries on the next tick
            row = await self._set_cancelling(row)
        state = await self._renewing(row, adapter.poll(row))
        await self._apply(row, state)

    async def _set_cancelling(self, row) -> dict:
        async with self.repository.pool.connection() as conn, conn.transaction():
            await self.repository.owned(
                conn, row["run_id"], row["worker_id"], row["fence"], allow_cancel=True
            )
            cur = await conn.execute(
                "UPDATE public.mcp_runs SET status='cancelling' WHERE run_id=%s AND status='running' RETURNING *",
                (row["run_id"],),
            )
            changed = await cur.fetchone()
            if changed:
                await self.repository.event(conn, changed, "cancelling")
            return changed or row

    async def _apply(self, row, state: ExternalState) -> None:
        if state.status == "running":
            message = state.message[:MESSAGE_LIMIT] if state.message else None
            if message and message != row["status_message"]:
                async with self.repository.pool.connection() as conn, conn.transaction():
                    await self.repository.owned(
                        conn, row["run_id"], row["worker_id"], row["fence"], allow_cancel=True
                    )
                    # An answer-again hint ("reason required", "fr refused the answer: …")
                    # stands until a valid answer clears it.
                    cur = await conn.execute(
                        "UPDATE public.mcp_runs SET status_message=%s WHERE run_id=%s "
                        "AND status_message IS DISTINCT FROM %s "
                        "AND COALESCE(status_message,'') NOT LIKE %s RETURNING run_id",
                        (message, row["run_id"], REASON_REQUIRED, REFUSED_PREFIX + "%"),
                    )
                    changed = await cur.fetchone()
                if changed:
                    # A new fr phase is one feed row. When the update was refused (a hint
                    # stands) nothing is recorded, so a hint never floods the feed.
                    await self._activity(row, "phase", message)
        elif state.status == "succeeded":
            await self.repository.finish(row, "succeeded", result=state.result, absent=True)
            await self._activity(row, "delivered", "delivered")
        elif state.status == "failed":
            await self.repository.finish(
                row, "failed", code=state.code or "external_failed", absent=True
            )
            await self._activity(row, "failed", state.code or "failed")
        else:
            if row["status"] == "running" and not row["cancel_requested"]:
                # running -> cancelled is not a legal transition; go through cancelling.
                row = await self._set_cancelling(row)
            await self.repository.finish(row, "cancelled", absent=True)
            # The contract has no "cancelled" kind; a withdrawn run is reported as refused.
            await self._activity(row, "refused", "cancelled")

    # -- 5. forward answers ----------------------------------------------------------------

    async def _forward(self) -> None:
        items = await self.inputs.claim_answered(limit=self.settings.worker_max_inflight * 4)
        forwarder = cast(AnswerForwarder | None, self.adapters.get("fr_project"))
        for item in items:
            try:
                run = await self.repository.get(item["tenant_id"], item["run_id"])
                if run["status"] not in TERMINAL:
                    if forwarder is None:
                        continue
                    refused = await forwarder.forward(run, item)
                    if refused is not None:
                        # The human can answer again (e.g. after linking their address in fr).
                        await self.inputs.reopen(item["input_key"], REFUSED_PREFIX + refused)
                        continue
                await self.inputs.mark_forwarded(item["input_key"])
            except Exception as exc:
                logger.warning(
                    "forwarding input for run %s failed: %s", item["run_id"], type(exc).__name__
                )


class HealthServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The driver loop owns signals; probes never replace handlers.
        yield


class Liveness:
    def __init__(self, window: float):
        self.window, self.last_ok = window, None

    def mark(self) -> None:
        self.last_ok = time.monotonic()

    @property
    def ready(self) -> bool:
        return self.last_ok is not None and time.monotonic() - self.last_ok <= self.window


def probe_app(liveness: Liveness) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        if liveness.ready:
            return {"status": "ready"}
        return JSONResponse({"status": "not_ready"}, status_code=503)

    return app


def build_adapters(client: httpx.AsyncClient, settings: Settings) -> dict[str, ExternalAdapter]:
    """Every external runtime the driver can own. A runtime absent here fails its runs with
    ``external_unconfigured``, so this mapping is the runtime contract."""
    return {
        "n8n_workflow": N8nAdapter(client, settings),
        "fr_project": FrAdapter(client, settings),
        "fr_command": FrCommandAdapter(client, settings),
    }


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings()  # ty: ignore[missing-argument]
    if not settings.execution_enabled:
        raise DriverConfigError("external driver requires explicit execution enablement")
    database = Database(settings)
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings.api_timeout_seconds))
    health_task = health_server = None
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await database.open()
        async with database.pool.connection() as conn:
            await verify_schema(conn, settings.platform_tenant_id, require_audit=False)
        worker = uuid7()
        driver = ExternalDriver(
            ExecutionRepository(database.pool, settings),
            TaskInputRepository(database.pool),
            build_adapters(client, settings),
            settings,
            worker,
        )
        liveness = Liveness(settings.external_poll_seconds * 3)
        health_server = HealthServer(
            uvicorn.Config(
                probe_app(liveness),
                # Internal probe listener is restricted by NetworkPolicy.
                host="0.0.0.0",  # nosec B104
                port=settings.external_probe_port,
                log_level="warning",
                access_log=False,
            )
        )
        health_task = asyncio.create_task(health_server.serve())
        logger.info("external driver %s started", worker)
        while not stopped.is_set():
            try:
                await driver.tick()
                liveness.mark()
            except Exception as exc:
                logger.error("external driver tick failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stopped.wait(), timeout=settings.external_poll_seconds)
            except TimeoutError:
                pass
    finally:
        if health_server:
            health_server.should_exit = True
        if health_task:
            await health_task
        await client.aclose()
        await database.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run())
    except DriverConfigError as exc:
        logger.error("refusing to start: %s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
