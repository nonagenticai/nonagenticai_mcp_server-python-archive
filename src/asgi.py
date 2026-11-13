"""Authenticated catalog/access application. Execution remains unavailable."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from src.auth.licenses import LicenseClient
from src.auth.management import ManagementAuthorization
from src.auth.protocol import AuthenticationMiddleware
from src.builder import runtime as builder_runtime
from src.builder.service import BuilderError, BuilderInputError
from src.builtin.audit import DatabaseAuditSink
from src.builtin.provider import BuiltinProvider
from src.catalog.provider import CatalogProvider
from src.catalog.resolver import CatalogResolver
from src.catalog.service import CatalogService, PlatformCatalogService
from src.catalog.submission import UnavailableSubmission
from src.config import Settings
from src.credentials.minter import LicenceMinterCredentialResolver
from src.execution.models import ExecutionDeniedError
from src.execution.service import ExecutionService
from src.protocol import BoundedProtocol, ExactProtocol, create_protocol
from src.sources.repository import SourceRepository
from src.storage.bindings import BindingRepository
from src.storage.bindings_ensure import BindingEnsureService
from src.storage.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogRepository,
    PlatformCatalogRepository,
    QuotaExceededError,
    SourceUnavailableError,
)
from src.storage.database import Database
from src.storage.executions import ExecutionRepository
from src.storage.schema import verify_schema
from src.storage.task_inputs import TaskInputRepository
from src.toolkits.service import AccessService


def create_app(
    settings: Settings | None = None, database_factory: Callable[[Settings], Database] = Database
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configuration = settings or Settings()  # ty: ignore[missing-argument]
        database = database_factory(configuration)
        application.state.settings = configuration
        application.state.database = database
        application.state.catalog = CatalogService(CatalogRepository(database.pool), configuration)
        application.state.bindings = BindingRepository(database.pool)
        application.state.access = AccessService(application.state.bindings, configuration)
        application.state.management = ManagementAuthorization(configuration)
        application.state.licenses = LicenseClient(configuration)
        # Backend licences minted per tenant (off by default); also proves the tenant's mcp
        # licence for bindings/ensure when the caller does not name it.
        application.state.licence_minter = (
            LicenceMinterCredentialResolver(configuration)
            if configuration.licence_minter_enabled
            else None
        )
        application.state.binding_ensure = BindingEnsureService(
            application.state.access,
            application.state.licenses,
            application.state.licence_minter,
        )
        platform_database = None
        try:
            await database.open()
            if configuration.platform_database_url:
                platform_database = Database(
                    configuration.model_copy(
                        update={"database_url": configuration.platform_database_url}
                    )
                )
                await platform_database.open()
                application.state.platform_catalog = PlatformCatalogService(
                    PlatformCatalogRepository(platform_database.pool), application.state.catalog
                )
                application.state.platform_pool = platform_database.pool
                application.state.platform_sources = SourceRepository(
                    platform_database.pool,
                    configuration.source_allowed_hosts,
                    configuration.source_secret_key,
                )
            # Failure leaves probes available, never starts a catalog task or repairs DDL.
            await check_ready(application)
            application.state.catalog_resolver = CatalogResolver(application.state.access)
            application.state.execution = ExecutionService(
                ExecutionRepository(database.pool, configuration),
                application.state.catalog_resolver,
            )
            # MCP tasks: open questions per run (fr approvals), answered via tasks/update.
            application.state.task_inputs = TaskInputRepository(database.pool)
            application.state.submission = (
                application.state.execution
                if configuration.execution_enabled
                else UnavailableSubmission()
            )
            application.state.provider = CatalogProvider(
                application.state.catalog_resolver, application.state.submission
            )
            # Builtin provider (src/builtin): the 34 first-party tools for non-binding callers,
            # audited per call into mcp_tool_call_audit (0006).
            application.state.builtin = BuiltinProvider(sink=DatabaseAuditSink(database.pool))
            transport = create_protocol(
                application.state.provider,
                application.state.builtin,
                settings=configuration,
                runs=application.state.execution.repository,
                inputs=application.state.task_inputs,
            )
            application.state.protocol_app = transport
            # Tool builder (docs/TOOL_BUILDER.md): the service behind /mcp/builder/*, the product
            # twin and the builder_* tools. Spec fetching uses the broker's pinned egress.
            for noisy in ("httpx", "httpcore"):
                if logging.getLogger(noisy).getEffectiveLevel() < logging.WARNING:
                    logging.getLogger(noisy).setLevel(logging.WARNING)
            builder_runtime.install(application.state, fetcher=document_fetcher(application))
            application.state.protocol_app = transport
            application.state.protocol_ready = False
            protocol_target.app = transport
            async with transport.router.lifespan_context(transport):
                application.state.protocol_ready = True
                try:
                    yield
                finally:
                    application.state.protocol_ready = False
        finally:
            builder_runtime.uninstall(application.state)
            fetcher = getattr(application.state, "document_broker", None)
            if fetcher is not None:
                await fetcher.close()
            if platform_database:
                await platform_database.close()
            if application.state.licence_minter:
                await application.state.licence_minter.close()
            await application.state.licenses.close()
            await database.close()

    application = FastAPI(
        title="Executable toolkit access",
        version="2.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(AuthenticationMiddleware, state=application.state)
    from src.routers.bindings import router
    from src.routers.builder import product_router as builder_product_router
    from src.routers.builder import router as builder_router
    from src.routers.catalog import router as catalog_router
    from src.routers.executions import router as execution_router
    from src.routers.fr_channel import router as fr_channel_router
    from src.routers.internal import router as internal_router
    from src.routers.operations import router as operations_router
    from src.routers.sources import router as sources_router

    application.include_router(router)
    application.include_router(catalog_router)
    application.include_router(execution_router)
    application.include_router(operations_router)
    application.include_router(sources_router)
    application.include_router(internal_router)
    application.include_router(builder_router)
    application.include_router(builder_product_router)
    application.include_router(fr_channel_router)

    async def builder_failure(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, BuilderError)
        return JSONResponse(
            {"error": error.code, "detail": error.message},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    async def invalid_request(request: Request, error: Exception) -> JSONResponse:
        if isinstance(error, QuotaExceededError):
            return JSONResponse(
                {"error": "quota_exceeded", "resource": error.resource},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        if isinstance(error, BuilderInputError):
            return JSONResponse(
                {"error": "invalid_request", "detail": str(error)},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        if isinstance(error, CatalogNotFoundError):
            status, message = 404, "not_found"
        elif isinstance(error, SourceUnavailableError):
            status, message = 409, "source_unavailable"
        elif isinstance(error, (CatalogConflictError, CheckViolation, UniqueViolation)):
            status, message = 409, "revision_or_lifecycle_conflict"
        elif isinstance(error, (PermissionError, ExecutionDeniedError)):
            status, message = 403, "forbidden"
        else:
            status, message = 422, "invalid_request"
        return JSONResponse(
            {"error": message}, status_code=status, headers={"Cache-Control": "no-store"}
        )

    application.add_exception_handler(BuilderError, builder_failure)
    for error_type in (
        QuotaExceededError,
        BuilderInputError,
        ValueError,
        PermissionError,
        ExecutionDeniedError,
        CatalogNotFoundError,
        RequestValidationError,
        CheckViolation,
        ForeignKeyViolation,
        UniqueViolation,
    ):
        application.add_exception_handler(error_type, invalid_request)

    @application.get("/health")
    @application.get("/health/live")
    async def live() -> dict[str, str]:
        return {
            "status": "alive",
            "execution": "enabled"
            if getattr(application.state, "settings", None)
            and application.state.settings.execution_enabled
            else "unavailable",
        }

    @application.get("/api/health")
    async def api_health() -> dict[str, str]:
        # care-bus-api's overview probe: {MCP_URL minus /mcp}/api/health (main #41). Public.
        return {"status": "healthy"}

    @application.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        valid = bool(getattr(request.app.state, "protocol_ready", False)) and await check_ready(
            request.app
        )
        return JSONResponse(
            status_code=200 if valid else 503,
            content={
                "status": "ready" if valid else "not_ready",
                "execution": "enabled"
                if request.app.state.settings.execution_enabled
                else "unavailable",
            },
        )

    # Route the exact endpoint without a redirect (and without losing headers).
    # Mount last so all management routes remain reachable.
    protocol_target = BoundedProtocol(None, application.state)
    from starlette.routing import Route

    application.router.routes.append(
        Route("/mcp", endpoint=ExactProtocol(protocol_target), methods=["GET", "POST", "DELETE"])
    )
    application.mount("/mcp", protocol_target)
    return application


def document_fetcher(application: FastAPI):
    """The broker's pinned-egress document fetch for spec fetching (None while the builder
    is disabled)."""
    if not application.state.settings.builder_enabled:
        return None
    from src.broker.client import Broker
    from src.sources.registry import SourceRegistry

    state = application.state
    broker = Broker(
        state.execution,
        state.catalog.repository,
        state.licence_minter,
        state.licenses,
        SourceRegistry(state.database.pool, state.settings),
    )
    state.document_broker = broker
    return broker


async def check_ready(application: FastAPI) -> bool:
    try:
        async with asyncio.timeout(application.state.settings.health_timeout_seconds):
            async with application.state.database.pool.connection() as connection:
                await verify_schema(connection, application.state.settings.platform_tenant_id)
        return True
    except Exception:
        # No SQL, DSNs, exception text, catalog names, or credentials in public errors.
        return False


def configure_app_logging() -> None:
    """Make this app's own INFO logs visible: uvicorn configures only its own loggers, so ``src.*``
    records (e.g. the fr channel event diagnostics) were dropped below WARNING. Ids and counts only
    are logged at INFO (never bodies). Keeps propagation, so test log capture still sees them."""
    src = logging.getLogger("src")
    if not src.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        src.addHandler(handler)
    if src.level == logging.NOTSET or src.level > logging.INFO:
        src.setLevel(logging.INFO)


configure_app_logging()
app = create_app()
