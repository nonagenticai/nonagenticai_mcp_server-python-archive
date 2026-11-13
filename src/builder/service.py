"""The tenant builder application service: one implementation behind REST and the builder_* tools.

Every method takes the authenticated ``AuthorPrincipal`` first and acts only for
``author.tenant_id``; nothing here accepts a tenant from a request. It orchestrates lane 1's
repositories (sources, builder catalog, quotas), lane 2's document fetch, lane 3's importer and
draft test run, and the existing catalog/toolkit services, and returns JSON-ready dicts.

Writes that must be atomic (publish a tool with the service/operation versions it pins, toolkit
quota + insert) share one transaction through lane 1's ``conn=`` support.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any
from uuid import UUID, uuid7

from pydantic import BaseModel, SecretStr, ValidationError

from src.auth.principal import AuthorPrincipal
from src.builder import packaging
from src.builder.models import (
    BindRequest,
    ImportRequest,
    OperationPatch,
    SpecFetchRequest,
    ToolCreate,
    ToolkitPublishRequest,
    ToolkitRequest,
    ToolOperation,
    ToolPublishRequest,
    ToolTestRequest,
    ToolUpdate,
)
from src.builder.openapi import import_openapi, list_operations
from src.builder.spec import (
    SPEC_TIMEOUT_SECONDS,
    DocumentFetcher,
    RateLimit,
    checked_spec_path,
    digest,
    parse_document,
    title_version,
)
from src.catalog.models import (
    AuthAdapter,
    ExactReference,
    OperationManifest,
    ServiceManifest,
    ToolManifest,
)
from src.catalog.package import PackageFile
from src.catalog.service import CatalogService
from src.config import Settings
from src.execution.models import ExecutionDeniedError
from src.sources.models import (
    RevisionOnly,
    TenantSourceCreate,
    TenantSourceUpdate,
    TokenUpdate,
)
from src.sources.repository import TenantSourceRepository
from src.storage.builder import BuilderCatalogRepository, OperationRecord, enforce_quota
from src.storage.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    QuotaExceededError,
)
from src.toolkits.models import ToolkitManifest, ToolReference, ToolSelection
from src.toolkits.service import AccessService

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_REQUEST_BYTES = 1048576
DEFAULT_SERVICE_RESPONSE_BYTES = 10485760


class BuilderError(Exception):
    """A builder failure with a stable machine code and a message that is safe to show."""

    status = 500
    code = "builder_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BuilderUnavailableError(BuilderError):
    status, code = 503, "builder_unavailable"


class BuilderRateLimitError(BuilderError):
    status, code = 429, "rate_limited"


class BuilderUpstreamError(BuilderError):
    status, code = 502, "upstream_failed"


class BuilderInputError(ValueError):
    """The request body is invalid; the message names fields and rules, never values."""


def validation_message(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_input=False, include_url=False)[:8]:
        where = ".".join(str(p) for p in item["loc"]) or "body"
        parts.append(f"{where}: {item['msg']}")
    return "; ".join(parts)[:1500] or "invalid request"


def parse[M: BaseModel](model: type[M], payload: Any) -> M:
    """Validate a request body (JSON text, bytes or an already-decoded object) as ``model``."""
    try:
        text = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
        return model.model_validate_json(text)
    except ValidationError as error:
        raise BuilderInputError(validation_message(error)) from None
    except (TypeError, ValueError) as error:
        raise BuilderInputError(str(error)[:300] or "invalid request") from None


def authoring[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Validation failures of authored content (schemas, packages, manifests) carry their message
    to the author as a 422; lifecycle conflicts and not-found keep their own statuses."""

    @wraps(func)
    async def run(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except CatalogConflictError, BuilderError, BuilderInputError:
            raise
        except ValueError as error:
            raise BuilderInputError(str(error)[:500] or "invalid request") from None

    return run


class BuilderService:
    def __init__(
        self,
        *,
        settings: Settings,
        pool: Any,
        sources: TenantSourceRepository,
        builder: BuilderCatalogRepository,
        catalog: CatalogService,
        access: AccessService,
        execution: Any = None,
        fetcher: DocumentFetcher | None = None,
        registry: Any = None,
        ensure: Any = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.sources = sources
        self.builder = builder
        self.catalog = catalog
        self.access = access
        self.execution = execution
        self.fetcher = fetcher
        self.registry = registry
        self.ensure = ensure
        self.fetch_limit = RateLimit(settings.builder_spec_fetches_per_hour)

    # --- sources ------------------------------------------------------------------------------

    async def source_create(self, author: AuthorPrincipal, body: TenantSourceCreate) -> dict:
        view = await self.sources.create(author.tenant_id, body, author.actor)
        return view.model_dump(mode="json")

    async def source_update(
        self, author: AuthorPrincipal, key: str, body: TenantSourceUpdate
    ) -> dict:
        view = await self.sources.update(author.tenant_id, key, body, author.actor)
        return view.model_dump(mode="json")

    async def source_set_secret(self, author: AuthorPrincipal, key: str, body: TokenUpdate) -> dict:
        """Write-only: the response is the source view, which never carries a secret."""
        view = await self.sources.set_secret(author.tenant_id, key, body, author.actor)
        return view.model_dump(mode="json")

    async def source_get(self, author: AuthorPrincipal, key: str) -> dict:
        return (await self.sources.get(author.tenant_id, key)).model_dump(mode="json")

    async def source_list(self, author: AuthorPrincipal, limit: int = 50, offset: int = 0) -> dict:
        items = await self.sources.page(author.tenant_id, limit, offset)
        return {
            "items": [i.model_dump(mode="json") for i in items],
            "limit": limit,
            "offset": offset,
        }

    async def source_set_status(
        self, author: AuthorPrincipal, key: str, body: RevisionOnly, status: str
    ) -> dict:
        view = await self.sources.set_status(
            author.tenant_id, key, body.expected_revision, status, author.actor
        )
        return view.model_dump(mode="json")

    async def usage(self, author: AuthorPrincipal) -> dict:
        used = await self.builder.usage(author.tenant_id)
        limits = {
            "sources": self.settings.builder_max_sources,
            "operations": self.settings.builder_max_operations,
            "tools": self.settings.builder_max_tools,
            "toolkits": self.settings.builder_max_toolkits,
        }
        return {"used": used, "limits": limits}

    # --- specs --------------------------------------------------------------------------------

    @staticmethod
    def operations_of(document: dict) -> list[dict]:
        return list_operations(document)

    async def spec_fetch(self, author: AuthorPrincipal, key: str, body: SpecFetchRequest) -> dict:
        """Fetch the source's OpenAPI document through its credential and the pinned egress,
        cache it by content hash and list what can be imported."""
        tenant = author.tenant_id
        view = await self.sources.get(tenant, key)
        if view.status != "active":
            raise CatalogConflictError("the source is disabled")
        if self.fetcher is None or self.registry is None:
            raise BuilderUnavailableError("spec fetching is not available")
        try:
            path = checked_spec_path(body.path, view.spec_path)
        except ValueError as error:
            raise BuilderInputError(str(error)) from None
        if not self.fetch_limit.take(tenant):
            raise BuilderRateLimitError("too many spec fetches this hour")
        limit = self.settings.builder_spec_max_bytes
        try:
            source = await self.registry.get(tenant, key)
            raw = await self.fetcher.fetch_document(source, path, limit, SPEC_TIMEOUT_SECONDS)
        except ExecutionDeniedError:
            raise BuilderUpstreamError(
                "the source is not reachable: it was refused by the egress policy, is disabled "
                "or its credential is unavailable"
            ) from None
        except (ValueError, TimeoutError, OSError) as error:
            logger.info("spec fetch failed for %s: %s", key, type(error).__name__)
            raise BuilderUpstreamError(
                f"spec fetch failed ({type(error).__name__}): check the base_url, spec path, "
                "the source credential and that the document is JSON under the size limit"
            ) from None
        try:
            document = parse_document(raw, limit)
        except ValueError as error:
            raise BuilderInputError(str(error)) from None
        sha = digest(raw)
        await self.sources.put_spec(tenant, key, sha, document, len(raw))
        title, version = title_version(document)
        return {
            "sha256": sha,
            "bytes": len(raw),
            "title": title,
            "version": version,
            "operations": self.operations_of(document),
        }

    async def spec_operations(self, author: AuthorPrincipal, key: str, sha256: str) -> dict:
        spec = await self.sources.get_spec(author.tenant_id, key, sha256)
        if spec is None:
            raise CatalogNotFoundError("spec not found; fetch it first")
        title, version = title_version(spec["document"])
        return {
            "sha256": spec["sha256"],
            "bytes": spec["bytes"],
            "fetched_at": spec["fetched_at"],
            "title": title,
            "version": version,
            "operations": self.operations_of(spec["document"]),
        }

    # --- services and operations --------------------------------------------------------------

    async def ensure_service(self, author: AuthorPrincipal, key: str, *, conn: Any = None):
        """The tenant's service for ``key`` (created as v1, draft, on first use)."""
        tenant = author.tenant_id
        record = await self.builder.service_for_source(tenant, key, conn=conn)
        if record is not None:
            return record
        view = await self.sources.get(tenant, key, conn=conn)
        if view.status != "active":
            raise CatalogConflictError("the source is disabled")
        service_id = uuid7()
        manifest = ServiceManifest(
            service_id=service_id,
            version=1,
            service_key=f"t{service_id.hex[-12:]}_{key}"[:64],
            destination_alias=key,
            auth_adapter=AuthAdapter.SOURCE,
            credential_profile_key=key,
            max_request_bytes=view.max_request_bytes or DEFAULT_SERVICE_REQUEST_BYTES,
            max_response_bytes=view.max_response_bytes or DEFAULT_SERVICE_RESPONSE_BYTES,
        )
        await self.builder.create_service(tenant, manifest, author.actor, conn=conn)
        record = await self.builder.service_for_source(tenant, key, conn=conn)
        assert record is not None
        return record

    @authoring
    async def operations_import(self, author: AuthorPrincipal, key: str, body: ImportRequest):
        """Create draft operations from a cached spec (a re-import of a key adds a version)."""
        tenant = author.tenant_id
        spec = await self.sources.get_spec(tenant, key, body.spec_sha256)
        if spec is None:
            raise CatalogNotFoundError("spec not found; fetch it first")
        done: list[dict] = []
        async with self.pool.connection() as conn, conn.transaction():
            service = await self.ensure_service(author, key, conn=conn)
            reference = ExactReference(id=service.service_id, version=service.version)
            imported = import_openapi(
                spec["document"],
                reference,
                [e if isinstance(e, str) else e.model_dump() for e in body.select],
                self.settings,
                {k: v.model_dump(exclude_none=True) for k, v in body.overrides.items()},
            )
            existing = {r.op_key: r for r in await self.builder.operations(tenant, key, conn=conn)}
            for item in imported:
                manifest: OperationManifest = item.manifest
                prior = existing.get(item.op_key)
                if prior is not None:
                    manifest = OperationManifest.model_validate_json(
                        manifest.model_copy(
                            update={
                                "operation_id": prior.operation_id,
                                "version": prior.version + 1,
                            }
                        ).model_dump_json()
                    )
                await self.builder.create_operation(
                    tenant,
                    manifest,
                    author.actor,
                    op_key=item.op_key,
                    max_operations=self.settings.builder_max_operations,
                    conn=conn,
                )
                done.append(
                    {
                        "op_key": item.op_key,
                        "operation_id": str(manifest.operation_id),
                        "version": manifest.version,
                        "method": manifest.method,
                        "path": manifest.path_template,
                        "effect": manifest.effect,
                    }
                )
        return {"source_key": key, "imported": done}

    @staticmethod
    def operation_view(record: OperationRecord) -> dict:
        m = record.manifest
        return {
            "op_key": record.op_key,
            "operation_id": str(record.operation_id),
            "version": record.version,
            "status": record.status,
            "method": m.method,
            "path_template": m.path_template,
            "effect": m.effect,
            "idempotency": m.idempotency,
            "timeout_seconds": m.timeout_seconds,
            "expected_error_statuses": list(m.expected_error_statuses),
            "empty_response": m.empty_response,
            "response_projection": list(m.response_projection),
            "redact_fields": list(m.redact_fields),
            "input_schema": m.input_schema,
            "output_schema": m.output_schema,
        }

    async def operation_list(self, author: AuthorPrincipal, key: str) -> dict:
        await self.sources.get(author.tenant_id, key)
        records = await self.builder.operations(author.tenant_id, key)
        return {"source_key": key, "operations": [self.operation_view(r) for r in records]}

    @authoring
    async def operation_update(
        self, author: AuthorPrincipal, key: str, op_key: str, body: OperationPatch
    ) -> dict:
        tenant = author.tenant_id
        record = await self.builder.operation_by_key(tenant, key, op_key)
        if record is None:
            raise CatalogNotFoundError("operation not found")
        if record.version != body.expected_version:
            raise CatalogConflictError("the operation changed; re-read it")
        data = record.manifest.model_dump(mode="json")
        data["version"] = record.version + 1
        for name, value in body.model_dump(exclude={"expected_version"}, mode="json").items():
            if value is not None:
                data[name] = value
        manifest = parse(OperationManifest, json.dumps(data))
        await self.builder.create_operation(
            tenant,
            manifest,
            author.actor,
            op_key=None,
            max_operations=self.settings.builder_max_operations,
        )
        updated = await self.builder.operation_by_key(tenant, key, op_key)
        assert updated is not None
        return self.operation_view(updated)

    # --- tools --------------------------------------------------------------------------------

    async def platform_operation(self, tenant: UUID, service: str, op_key: str) -> ExactReference:
        """The newest published version of a platform operation the tenant may use."""
        visible = {
            r["service_key"]: r for r in await self.builder.visible_platform_services(tenant)
        }
        shared = visible.get(service)
        if shared is None:
            raise CatalogNotFoundError(f"platform service {service!r} is not shared with you")
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT k.operation_id,o.version FROM public.mcp_operation_keys k "
                "JOIN LATERAL (SELECT version FROM public.mcp_operation_versions v "
                "  WHERE v.operation_id=k.operation_id AND v.status='published' "
                "  ORDER BY v.version DESC LIMIT 1) o ON true "
                "WHERE k.service_id=%s AND k.op_key=%s",
                (shared["service_id"], op_key),
            )
            row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError(f"operation {service}.{op_key} is not available")
        return ExactReference(id=row["operation_id"], version=row["version"])

    async def resolve_operations(
        self, tenant: UUID, operations: tuple[ToolOperation, ...]
    ) -> dict[str, ExactReference]:
        refs: dict[str, ExactReference] = {}
        for item in operations:
            if item.source_key is not None:
                record = await self.builder.operation_by_key(tenant, item.source_key, item.op_key)
                if record is None:
                    raise CatalogNotFoundError(
                        f"operation {item.script_key} is not imported; import it first"
                    )
                refs[item.script_key] = ExactReference(
                    id=record.operation_id, version=record.version
                )
            else:
                assert item.service is not None
                refs[item.script_key] = await self.platform_operation(
                    tenant, item.service, item.op_key
                )
        return refs

    def runner_image(self) -> str:
        images = self.settings.approved_runner_images
        if not images:
            raise BuilderUnavailableError("no runner image is approved on this deployment")
        return images[0]

    @staticmethod
    def tool_view(descriptor, files: tuple[PackageFile, ...] | None = None) -> dict:
        out = descriptor.model_dump(mode="json")
        if files is not None:
            out["files"] = [f.model_dump() for f in packaging.user_files(files)]
            out["ops"] = sorted(packaging.parse_ops(files))
        return out

    @authoring
    async def tool_create(self, author: AuthorPrincipal, body: ToolCreate) -> dict:
        tenant = author.tenant_id
        refs = await self.resolve_operations(tenant, body.ops)
        files = packaging.build_package(body.files, {k: (r.id, r.version) for k, r in refs.items()})
        manifest = packaging.tool_manifest(
            tool_id=uuid7(),
            tenant_id=tenant,
            version=1,
            fields=packaging.manifest_fields(
                public_name=body.public_name,
                operation=body.operation,
                description=body.description,
                entrypoint=body.entrypoint,
                input_schema=body.input_schema,
                result_schema=body.result_schema,
                context_schema=None,
                effect=body.effect,
                limits=body.limits,
            ),
            files=files,
            refs=refs.values(),
            runner_image_digest=self.runner_image(),
        )
        await self.catalog.validate(manifest, files)
        async with self.pool.connection() as conn, conn.transaction():
            await enforce_quota(conn, tenant, "tools", self.settings.builder_max_tools)
            descriptor = await self.catalog.repository.create(
                tenant, manifest, files, author.actor, conn=conn
            )
        return self.tool_view(descriptor, files)

    @authoring
    async def tool_update(self, author: AuthorPrincipal, tool_id: UUID, body: ToolUpdate) -> dict:
        tenant = author.tenant_id
        latest = await self.catalog.repository.latest_version(tenant, tool_id)
        if latest is None:
            raise CatalogNotFoundError("tool not found")
        old = latest.manifest
        if old.runtime != "kata_python" or old.entrypoint is None:
            # External runtimes (n8n_workflow, fr_project) have no authored package to edit.
            raise BuilderInputError("only kata_python tools can be edited")
        stored = await self.catalog.repository.files(tenant, tool_id, latest.version)
        if body.ops is not None:
            refs = await self.resolve_operations(tenant, body.ops)
        else:
            # Re-pin each operation the tool already uses to its newest version.
            refs = await self.repin(tenant, stored)
        authored = body.files if body.files is not None else packaging.user_files(stored)
        files = packaging.build_package(authored, {k: (r.id, r.version) for k, r in refs.items()})
        manifest = packaging.tool_manifest(
            tool_id=tool_id,
            tenant_id=tenant,
            version=latest.version + 1,
            fields=packaging.manifest_fields(
                public_name=old.public_name,
                operation=old.operation,
                description=body.description or old.description,
                entrypoint=old.entrypoint,
                input_schema=body.input_schema or old.input_schema,
                result_schema=body.result_schema or old.result_schema,
                context_schema=old.context_schema,
                effect=body.effect or old.effect,
                limits=body.limits or old.limits,
            ),
            files=files,
            refs=refs.values(),
            runner_image_digest=self.runner_image(),
        )
        descriptor = await self.catalog.append_draft(
            tenant,
            tool_id,
            manifest,
            files,
            author.actor,
            expected_revision=body.expected_revision,
        )
        return self.tool_view(descriptor, files)

    async def repin(
        self, tenant: UUID, files: tuple[PackageFile, ...]
    ) -> dict[str, ExactReference]:
        """Newest version of every operation in a stored package's ops table."""
        refs: dict[str, ExactReference] = {}
        for script_key, (operation_id, _) in packaging.parse_ops(files).items():
            source_key, _, op_key = script_key.partition(".")
            record = await self.builder.operation_by_key(tenant, source_key, op_key)
            if record is not None and record.operation_id == operation_id:
                refs[script_key] = ExactReference(id=record.operation_id, version=record.version)
            else:
                refs[script_key] = await self.platform_operation(tenant, source_key, op_key)
        return refs

    async def tool_get(self, author: AuthorPrincipal, tool_id: UUID, version: int | None) -> dict:
        tenant = author.tenant_id
        if version is None:
            latest = await self.catalog.repository.latest_version(tenant, tool_id)
            if latest is None:
                raise CatalogNotFoundError("tool not found")
            version = latest.version
        descriptor = await self.catalog.repository.get_version(tenant, tool_id, version)
        files = await self.catalog.repository.files(tenant, tool_id, version)
        return self.tool_view(descriptor, files)

    async def tool_list(self, author: AuthorPrincipal, limit: int = 50, offset: int = 0) -> dict:
        items = await self.catalog.repository.definitions(author.tenant_id, limit, offset)
        return {"items": items, "limit": limit, "offset": offset}

    async def count_test_runs(self, tenant: UUID) -> int:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS n FROM public.mcp_runs WHERE tenant_id=%s AND mode='draft' "
                "AND created_at > clock_timestamp() - interval '1 hour'",
                (tenant,),
            )
            row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    async def tool_test(
        self, author: AuthorPrincipal, tool_id: UUID, version: int, body: ToolTestRequest
    ) -> dict:
        """Run an owned draft or published tool version once, with no binding. A tool whose
        effect is ``writes`` needs ``confirm_writes``."""
        if self.execution is None or not self.settings.execution_enabled:
            raise BuilderUnavailableError("test runs are not available: execution is disabled")
        if await self.count_test_runs(author.tenant_id) >= self.settings.builder_test_runs_per_hour:
            raise BuilderRateLimitError("too many test runs this hour")
        return await self.execution.submit_draft(
            author.tenant_id,
            author.license_id,
            tool_id,
            version,
            body.inputs,
            body.confirm_writes,
        )

    async def run_get(self, author: AuthorPrincipal, run_id: UUID) -> dict:
        """The run envelope plus the call trace (and, for draft runs, redacted error excerpts)."""
        if self.execution is None:
            raise BuilderUnavailableError("runs are not available")
        return await self.execution.draft_run(author.tenant_id, run_id)

    async def own_dependencies(
        self, tenant: UUID, manifest: ToolManifest
    ) -> tuple[list[ExactReference], list[ExactReference]]:
        """The tenant-owned service and operation versions a tool pins that are not yet
        published (platform operations are the platform's to publish)."""
        repository = self.catalog.repository
        services: dict[tuple[UUID, int], ExactReference] = {}
        operations: list[ExactReference] = []
        for reference in manifest.allowed_operations:
            operation, state = await repository.operation(reference)
            if await repository.service_owner(operation.service.id) != tenant:
                continue
            _, service_state = await repository.service(operation.service)
            if service_state != "published":
                services[operation.service.id, operation.service.version] = operation.service
            if state != "published":
                operations.append(reference)
        return list(services.values()), operations

    @authoring
    async def tool_publish(
        self, author: AuthorPrincipal, tool_id: UUID, version: int, body: ToolPublishRequest
    ) -> dict:
        """Publish a tool version together with the draft service and operation versions it
        pins, in ONE transaction (all of it or none)."""
        tenant = author.tenant_id
        repository = self.catalog.repository
        descriptor = await repository.get_version(tenant, tool_id, version)
        files = await repository.files(tenant, tool_id, version)
        await self.catalog.validate(descriptor.manifest, files)
        services, operations = await self.own_dependencies(tenant, descriptor.manifest)
        async with self.pool.connection() as conn, conn.transaction():
            if services or operations:
                await self.builder.publish(
                    tenant, author.actor, services=services, operations=operations, conn=conn
                )
            result = await repository.transition(
                tenant,
                tool_id,
                version,
                "published",
                author.actor,
                expected_revision=body.expected_revision,
                conn=conn,
            )
        return self.tool_view(result)

    async def tool_set_status(
        self, author: AuthorPrincipal, tool_id: UUID, version: int, status: str, revision: int
    ) -> dict:
        result = await self.catalog.transition(
            author.tenant_id, tool_id, version, status, author.actor, expected_revision=revision
        )
        return self.tool_view(result)

    # --- toolkits -----------------------------------------------------------------------------

    async def published_version(self, tenant: UUID, tool_id: UUID) -> int:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT max(version) AS v FROM public.mcp_tool_versions "
                "WHERE tenant_id=%s AND tool_id=%s AND status='published'",
                (tenant, tool_id),
            )
            row = await cursor.fetchone()
        if row is None or row["v"] is None:
            raise CatalogConflictError(f"tool {tool_id} has no published version; publish it first")
        return int(row["v"])

    @authoring
    async def toolkit_create(self, author: AuthorPrincipal, body: ToolkitRequest) -> dict:
        tenant = author.tenant_id
        selections = []
        for entry in body.tools:
            version = entry.version or await self.published_version(tenant, entry.tool_id)
            record = await self.catalog.repository.get_version(tenant, entry.tool_id, version)
            # The selection keeps the tool's own limits; only the binding's ceiling narrows them.
            selections.append(
                ToolSelection(
                    tool=ToolReference(tenant_id=tenant, id=entry.tool_id, version=version),
                    limits=record.manifest.limits,
                )
            )
        manifest = ToolkitManifest(
            toolkit_id=body.toolkit_id or uuid7(),
            tenant_id=tenant,
            version=1,
            name=body.name,
            description=body.description,
            tools=tuple(selections),
        )
        from psycopg.errors import UniqueViolation

        try:
            async with self.pool.connection() as conn, conn.transaction():
                if body.toolkit_id is None:
                    await enforce_quota(
                        conn, tenant, "toolkits", self.settings.builder_max_toolkits
                    )
                result = await self.access.write_toolkit(
                    conn, tenant, author.actor, manifest, body.expected_revision
                )
        except UniqueViolation:
            raise CatalogConflictError("toolkit identity or name already exists") from None
        return result.model_dump(mode="json")

    async def toolkit_publish(
        self, author: AuthorPrincipal, toolkit_id: UUID, version: int, body: ToolkitPublishRequest
    ) -> dict:
        """Publish a toolkit version, then re-bind the tenant's product bindings so they include
        it (decision 4); a re-bind problem is reported, it does not undo the publication."""
        from psycopg.errors import CheckViolation

        try:
            async with self.pool.connection() as conn, conn.transaction():
                result = await self.access.transition_toolkit(
                    conn,
                    author.tenant_id,
                    author.actor,
                    toolkit_id,
                    version,
                    "published",
                    body.expected_revision,
                )
        except CheckViolation:
            raise CatalogConflictError("toolkit dependencies unavailable") from None
        out = result.model_dump(mode="json")
        try:
            out["bindings"] = await self.rebind(author, BindRequest())
        except Exception as error:  # report, never undo the publication
            logger.warning("rebind after toolkit publish failed: %s", type(error).__name__)
            out["bindings"] = {"error": "rebind_failed", "detail": "call toolkit bind again"}
        return out

    async def rebind(self, author: AuthorPrincipal, body: BindRequest) -> dict:
        """Re-provision the tenant's existing product bindings (same token, same licence) so they
        pin its published toolkits. A binding whose licence changed needs bindings/ensure."""
        if self.ensure is None:
            raise BuilderUnavailableError("binding provisioning is not available")
        products = await self.ensure.rebind(
            author.tenant_id, author.actor, author.license_id, body.product
        )
        return {"products": products}


__all__ = [
    "BuilderError",
    "BuilderInputError",
    "BuilderRateLimitError",
    "BuilderService",
    "BuilderUnavailableError",
    "BuilderUpstreamError",
    "QuotaExceededError",
    "SecretStr",
    "parse",
]
