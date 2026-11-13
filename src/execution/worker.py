"""Separate worker entry point. All secrets and Kubernetes permissions live here."""

import asyncio
import logging
import signal
from contextlib import contextmanager
from uuid import uuid7

import psycopg
import uvicorn
from psycopg_pool import PoolTimeout

from src.auth.licenses import LicenseClient
from src.broker.client import Broker
from src.catalog.resolver import CatalogResolver
from src.config import Settings
from src.credentials.minter import LicenceMinterCredentialResolver
from src.credentials.resolver import FileCredentialResolver
from src.execution.controller import Controller
from src.execution.health import worker_app
from src.execution.kata import KataBackend
from src.execution.recovery import Recovery
from src.execution.scheduler import Scheduler
from src.execution.service import ExecutionService
from src.sources.registry import SourceRegistry
from src.storage.bindings import BindingRepository
from src.storage.catalog import CatalogRepository
from src.storage.database import Database
from src.storage.executions import ExecutionRepository
from src.storage.schema import verify_schema
from src.toolkits.service import AccessService

logger = logging.getLogger(__name__)

#: Failures of a DB round trip that a later attempt can survive: the connection was closed
#: under the pool, the handle is unusable, or the pool could not hand one out in time. A
#: worker exits on anything else instead of retrying a fault it cannot fix.
TRANSIENT_DB_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout)

#: After this many consecutive transient failures (~5 minutes at the 5 s cap) the worker exits
#: instead of retrying forever: Kubernetes restarts it and the crash is visible in its status.
TRANSIENT_RETRY_LIMIT = 60


class HealthServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The executor owns signals and pod cleanup; probes never replace handlers.
        yield


def credential_resolver(settings):
    """Licence-minted per-tenant credentials when enabled, else the mounted bundle file."""
    if settings.licence_minter_enabled:
        return LicenceMinterCredentialResolver(settings)
    return FileCredentialResolver(settings.credential_bundle_file)


async def run(settings=None):
    settings = settings or Settings()  # ty: ignore[missing-argument]
    if not settings.execution_enabled or not (
        settings.credential_bundle_file or settings.licence_minter_enabled
    ):
        raise ValueError("worker requires explicit execution enablement and worker configuration")
    database, licenses = Database(settings), LicenseClient(settings)
    backend = broker = credentials = None
    active = set()
    health_task = health_server = None
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await database.open()
        async with database.pool.connection() as conn:
            await verify_schema(conn, settings.platform_tenant_id, require_audit=False)
        credentials = credential_resolver(settings)
        credentials.read()
        repository = ExecutionRepository(database.pool, settings)
        scheduler = Scheduler(repository)
        await scheduler.verify_capacity()
        resolver = CatalogResolver(AccessService(BindingRepository(database.pool), settings))
        service = ExecutionService(repository, resolver)
        backend = KataBackend(settings)
        await backend.ready()
        health_server = HealthServer(
            uvicorn.Config(
                worker_app(repository, scheduler, backend, credentials),
                # Internal probe listener is restricted by the worker NetworkPolicy.
                host="0.0.0.0",  # nosec B104
                port=settings.worker_probe_port,
                log_level="warning",
                access_log=False,
            )
        )
        # The worker owns shutdown signals, including cleanup of its active pods.
        health_task = asyncio.create_task(health_server.serve())
        broker = Broker(
            service,
            CatalogRepository(database.pool),
            credentials,
            licenses,
            SourceRegistry(database.pool, settings),
        )
        controller = Controller(
            repository, service, broker, backend, CatalogRepository(database.pool)
        )
        recovery = Recovery(repository, backend)
        worker = uuid7()
        next_reconcile = 0
        delay = 0.2
        failures = 0
        while not stopped.is_set():
            # A transient database failure must not kill the process: log it, back off, retry.
            try:
                if loop.time() >= next_reconcile:
                    await recovery.reconcile(worker)
                    await repository.expire_payloads()
                    next_reconcile = loop.time() + settings.reconcile_seconds
                for task in tuple(active):
                    if task.done():
                        try:
                            task.result()
                        except Exception as exc:  # noqa: BLE001 - a failed run is not a worker exit
                            logger.error("execution task failed: %s", type(exc).__name__)
                        active.remove(task)
                # Bounds local task count as well as durable global capacity.
                if len(active) < min(settings.worker_max_inflight, settings.execution_global_slots):
                    row = await scheduler.claim(worker)
                    if row:
                        active.add(asyncio.create_task(controller.execute(row)))
                        delay = 0.2
                        failures = 0
                        continue
                delay = 0.2
                failures = 0
            except TRANSIENT_DB_ERRORS as exc:
                failures += 1
                if failures >= TRANSIENT_RETRY_LIMIT:
                    logger.error("database still unavailable after %d retries; exiting", failures)
                    raise
                logger.warning(
                    "transient database error: %s; retrying in %.1f s", type(exc).__name__, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            try:
                await asyncio.wait_for(stopped.wait(), timeout=0.2)
            except TimeoutError:
                pass
    finally:
        if health_server:
            health_server.should_exit = True
        if health_task:
            await health_task
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if broker:
            await broker.close()
        if backend:
            await backend.close()
        close = getattr(credentials, "close", None)
        if close:
            await close()
        await licenses.close()
        await database.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
