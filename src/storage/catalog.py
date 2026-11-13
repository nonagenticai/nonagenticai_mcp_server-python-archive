"""Transactional complete snapshots. Every tenant operation requires explicit ownership."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.catalog.models import (
    Attribution,
    ExactReference,
    OperationManifest,
    ServiceManifest,
    ToolManifest,
    VersionDescriptor,
)
from src.catalog.package import PackageFile


class CatalogConflictError(ValueError):
    """Identity, expected revision, or lifecycle conflict."""


class SourceUnavailableError(CatalogConflictError):
    """A service names a tool source that is not registered and active, or disagrees with it."""


class CatalogNotFoundError(LookupError):
    """No version owned by this tenant (also used for known foreign IDs)."""


class QuotaExceededError(CatalogConflictError):
    """A per-tenant builder quota (``MCP_BUILDER_MAX_*``) is exhausted."""

    def __init__(self, resource: str = "resource") -> None:
        super().__init__(f"{resource} quota exceeded")
        self.resource = resource


class BuilderForbiddenError(PermissionError):
    """The builder refuses this tenant (the platform tenant never authors through it)."""


#: SQLSTATEs raised by the ``mcp_builder_*`` functions (migration 0010).
BUILDER_CONFLICT, BUILDER_NOT_FOUND, BUILDER_QUOTA, BUILDER_FORBIDDEN = (
    "MB001",
    "MB002",
    "MB003",
    "MB004",
)


@asynccontextmanager
async def builder_errors(resource: str = "resource") -> AsyncIterator[None]:
    """Translate database errors of builder writes into the catalog exception types.

    MB001/23505 -> CatalogConflictError, MB002 -> CatalogNotFoundError, MB003 ->
    QuotaExceededError, MB004 -> BuilderForbiddenError, 23514 -> ValueError. Only the primary
    message (ours, or a constraint name) is surfaced, never row detail.
    """
    try:
        yield
    except psycopg.Error as exc:
        message = exc.diag.message_primary or "invalid request"
        match exc.sqlstate:
            case "MB001":
                raise CatalogConflictError(message) from None
            case "MB002":
                raise CatalogNotFoundError(message) from None
            case "MB003":
                raise QuotaExceededError(resource) from None
            case "MB004":
                raise BuilderForbiddenError(message) from None
            case "23505":
                raise CatalogConflictError(f"{resource} already exists") from None
            case "23514" | "23503":
                raise ValueError(message) from None
            case _:
                raise


@asynccontextmanager
async def in_transaction(
    pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]],
    conn: AsyncConnection[dict[str, Any]] | None = None,
) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
    """A transaction on ``conn`` (a savepoint when the caller already holds one) or on a fresh
    pooled connection. Lets multi-step builder flows (publish a tool and its dependencies) run
    atomically across repositories."""
    if conn is not None:
        async with conn.transaction():
            yield conn
    else:
        async with pool.connection() as own, own.transaction():
            yield own


def snapshot(value: Any) -> Jsonb:
    return Jsonb(value.model_dump(mode="json"))


def descriptor(row: dict) -> VersionDescriptor:
    return VersionDescriptor(
        tenant_id=row["tenant_id"],
        tool_id=row["tool_id"],
        version=row["version"],
        revision=row["revision"],
        status=row["status"],
        manifest=ToolManifest.model_validate_json(json.dumps(row["manifest"])),
        created_by=Attribution.model_validate_json(json.dumps(row["created_by"])),
        source=ExactReference(id=row["source_tool_id"], version=row["source_version"])
        if row["source_tool_id"]
        else None,
    )


class CatalogRepository:
    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]]) -> None:
        self.pool = pool

    async def get_version(self, tenant_id: UUID, tool_id: UUID, version: int) -> VersionDescriptor:
        async with self.pool.connection() as conn:
            row = await self._version(conn, tenant_id, tool_id, version)
            return descriptor(row)

    async def _version(
        self, conn: AsyncConnection[dict[str, Any]], tenant_id: UUID, tool_id: UUID, version: int
    ) -> dict:
        cursor = await conn.execute(
            "SELECT v.*, t.revision FROM public.mcp_tool_versions v JOIN public.mcp_tools t "
            "USING (tenant_id,tool_id) WHERE v.tenant_id=%s AND v.tool_id=%s AND v.version=%s",
            (tenant_id, tool_id, version),
        )
        row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError("tenant-owned version not found")
        return row

    async def latest_version(self, tenant_id: UUID, tool_id: UUID) -> VersionDescriptor | None:
        """Highest version of an owned identity, or None when the identity does not exist."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT v.*, t.revision FROM public.mcp_tool_versions v JOIN public.mcp_tools t "
                "USING (tenant_id,tool_id) WHERE v.tenant_id=%s AND v.tool_id=%s "
                "ORDER BY v.version DESC LIMIT 1",
                (tenant_id, tool_id),
            )
            row = await cursor.fetchone()
            return descriptor(row) if row else None

    async def files(self, tenant_id: UUID, tool_id: UUID, version: int) -> tuple[PackageFile, ...]:
        async with self.pool.connection() as conn:
            await self._version(conn, tenant_id, tool_id, version)
            cursor = await conn.execute(
                "SELECT path,content FROM public.mcp_tool_version_files "
                "WHERE tenant_id=%s AND tool_id=%s AND version=%s ORDER BY path",
                (tenant_id, tool_id, version),
            )
            return tuple(PackageFile(**r) for r in await cursor.fetchall())

    async def create(
        self,
        tenant_id: UUID,
        manifest: ToolManifest,
        files: tuple[PackageFile, ...],
        actor: Attribution,
        *,
        conn: AsyncConnection[dict[str, Any]] | None = None,
    ) -> VersionDescriptor:
        if manifest.tenant_id != tenant_id or manifest.version != 1:
            raise CatalogConflictError("identity ownership or first version mismatch")
        try:
            async with in_transaction(self.pool, conn) as conn:
                await conn.execute(
                    "INSERT INTO public.mcp_tools(tenant_id,tool_id,public_name,created_by) VALUES (%s,%s,%s,%s)",
                    (tenant_id, manifest.tool_id, manifest.public_name, snapshot(actor)),
                )
                await self._insert(conn, manifest, files, actor)
                result = descriptor(await self._version(conn, tenant_id, manifest.tool_id, 1))
                await self._audit(conn, result, actor, "tool.created")
            return result
        except UniqueViolation:
            raise CatalogConflictError("tool identity or public name already exists") from None

    async def _insert(
        self,
        conn: AsyncConnection[dict[str, Any]],
        manifest: ToolManifest,
        files: tuple[PackageFile, ...],
        actor: Attribution,
        source: ExactReference | None = None,
    ) -> None:
        await conn.execute(
            "INSERT INTO public.mcp_tool_versions(tenant_id,tool_id,version,manifest,package_sha256,"
            "file_count,operation_count,created_by,updated_by,source_tool_id,source_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                manifest.tenant_id,
                manifest.tool_id,
                manifest.version,
                snapshot(manifest),
                manifest.package_sha256,
                len(files),
                len(manifest.allowed_operations),
                snapshot(actor),
                snapshot(actor),
                source.id if source else None,
                source.version if source else None,
            ),
        )
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO public.mcp_tool_version_files(tenant_id,tool_id,version,path,content) "
                "VALUES (%s,%s,%s,%s,%s)",
                [
                    (manifest.tenant_id, manifest.tool_id, manifest.version, f.path, f.content)
                    for f in files
                ],
            )
            await cursor.executemany(
                "INSERT INTO public.mcp_tool_version_operations(tenant_id,tool_id,version,operation_id,operation_version) "
                "VALUES (%s,%s,%s,%s,%s)",
                [
                    (manifest.tenant_id, manifest.tool_id, manifest.version, r.id, r.version)
                    for r in manifest.allowed_operations
                ],
            )

    async def append(
        self,
        tenant_id: UUID,
        tool_id: UUID,
        content: ToolManifest,
        files: tuple[PackageFile, ...],
        actor: Attribution,
        *,
        expected_revision: int | None,
        source: ExactReference | None = None,
        exact_version: bool = False,
        conn: AsyncConnection[dict[str, Any]] | None = None,
    ) -> VersionDescriptor:
        """Content's artifact version is a template; allocate MAX+1 under parent lock."""
        if content.tenant_id != tenant_id or content.tool_id != tool_id:
            raise CatalogConflictError("content identity differs from owned target")
        async with in_transaction(self.pool, conn) as conn:
            parent = await self._lock(conn, tenant_id, tool_id, expected_revision)
            if parent["retired"] or parent["public_name"] != content.public_name:
                raise CatalogConflictError("retired identity or public name mismatch")
            cursor = await conn.execute(
                "SELECT coalesce(max(version),0)+1 AS next FROM public.mcp_tool_versions WHERE tenant_id=%s AND tool_id=%s",
                (tenant_id, tool_id),
            )
            row = await cursor.fetchone()
            assert row is not None
            if exact_version and content.version != row["next"]:
                raise CatalogConflictError("manifest version must be the next version")
            manifest = content.model_copy(update={"version": row["next"]})
            await self._insert(conn, manifest, files, actor, source)
            await conn.execute(
                "UPDATE public.mcp_tools SET revision=revision+1 WHERE tenant_id=%s AND tool_id=%s",
                (tenant_id, tool_id),
            )
            result = descriptor(await self._version(conn, tenant_id, tool_id, manifest.version))
            await self._audit(conn, result, actor, "tool.version_created")
        return result

    async def _lock(
        self,
        conn: AsyncConnection[dict[str, Any]],
        tenant_id: UUID,
        tool_id: UUID,
        expected_revision: int | None,
    ) -> dict:
        cursor = await conn.execute(
            "SELECT * FROM public.mcp_tools WHERE tenant_id=%s AND tool_id=%s FOR UPDATE",
            (tenant_id, tool_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError("tenant-owned identity not found")
        if expected_revision is not None and row["revision"] != expected_revision:
            raise CatalogConflictError("stale expected revision")
        return row

    async def transition(
        self,
        tenant_id: UUID,
        tool_id: UUID,
        version: int,
        status: str,
        actor: Attribution,
        *,
        expected_revision: int,
        conn: AsyncConnection[dict[str, Any]] | None = None,
    ) -> VersionDescriptor:
        async with in_transaction(self.pool, conn) as conn:
            parent = await self._lock(conn, tenant_id, tool_id, expected_revision)
            row = await self._version(conn, tenant_id, tool_id, version)
            if status not in {"published", "disabled", "retired"} or (
                row["status"],
                status,
            ) not in {
                ("draft", "published"),
                ("draft", "disabled"),
                ("draft", "retired"),
                ("published", "disabled"),
                ("published", "retired"),
                ("disabled", "retired"),
            }:
                raise CatalogConflictError("invalid lifecycle transition")
            if status == "published":
                if parent["retired"]:
                    raise CatalogConflictError("identity retired")
                cursor = await conn.execute(
                    "SELECT o.status AS operation_status,s.status AS service_status "
                    "FROM public.mcp_tool_version_operations r "
                    "JOIN public.mcp_operation_versions o ON (o.operation_id,o.version)=(r.operation_id,r.operation_version) "
                    "JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version) "
                    "WHERE r.tenant_id=%s AND r.tool_id=%s AND r.version=%s ",
                    (tenant_id, tool_id, version),
                )
                if any(
                    r["operation_status"] != "published" or r["service_status"] != "published"
                    for r in await cursor.fetchall()
                ):
                    raise CatalogConflictError("operation or service is not published")
            await conn.execute(
                "UPDATE public.mcp_tool_versions SET status=%s,updated_by=%s,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND tool_id=%s AND version=%s",
                (status, snapshot(actor), tenant_id, tool_id, version),
            )
            await conn.execute(
                "UPDATE public.mcp_tools SET revision=revision+1 WHERE tenant_id=%s AND tool_id=%s",
                (tenant_id, tool_id),
            )
            result = descriptor(await self._version(conn, tenant_id, tool_id, version))
            await self._audit(conn, result, actor, "tool." + status)
        return result

    @staticmethod
    async def _audit(conn, result: VersionDescriptor, actor: Attribution, action: str) -> None:
        from src.storage.bindings import audit

        await audit(
            conn, result.tenant_id, actor, result.tool_id, result.revision, action, result.version
        )

    async def definitions(self, tenant_id: UUID, limit: int, offset: int) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT v.tool_id,v.version,t.public_name,t.revision,v.status "
                "FROM public.mcp_tool_versions v JOIN public.mcp_tools t USING(tenant_id,tool_id) "
                "WHERE v.tenant_id=%s ORDER BY v.tool_id,v.version LIMIT %s OFFSET %s",
                (tenant_id, limit, offset),
            )
            return await cur.fetchall()

    async def retire(self, tenant_id: UUID, tool_id: UUID, *, expected_revision: int) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            parent = await self._lock(conn, tenant_id, tool_id, expected_revision)
            if parent["retired"]:
                raise CatalogConflictError("identity already retired")
            await conn.execute(
                "UPDATE public.mcp_tools SET retired=true,revision=revision+1 WHERE tenant_id=%s AND tool_id=%s",
                (tenant_id, tool_id),
            )

    async def operation(self, reference: ExactReference) -> tuple[OperationManifest, str]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT manifest,status FROM public.mcp_operation_versions WHERE operation_id=%s AND version=%s",
                (reference.id, reference.version),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CatalogNotFoundError("exact operation not found")

            return OperationManifest.model_validate_json(json.dumps(row["manifest"])), row["status"]

    async def service(self, reference: ExactReference) -> tuple[ServiceManifest, str]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT manifest,status FROM public.mcp_service_versions WHERE service_id=%s AND version=%s",
                (reference.id, reference.version),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CatalogNotFoundError("exact service not found")

            return ServiceManifest.model_validate_json(json.dumps(row["manifest"])), row["status"]

    async def service_owner(self, service_id: UUID) -> UUID:
        """Owner of a service identity: the platform tenant or the authoring tenant. The broker
        resolves the source as ``(owner, destination_alias)``."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT owner_tenant_id FROM public.mcp_services WHERE service_id=%s", (service_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError("service not found")
        return row["owner_tenant_id"]

    async def may_use_service(self, tenant_id: UUID, reference: ExactReference) -> bool:
        """The reference rule the database enforces on tool versions: own services, granted
        platform services, and licence-kind platform services. The broker re-checks it per call."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT public.mcp_tenant_may_use_service(%s,%s,%s) AS ok",
                (tenant_id, reference.id, reference.version),
            )
            row = await cursor.fetchone()
        return bool(row and row["ok"])

    async def generation(self) -> int:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT generation FROM public.mcp_catalog_state WHERE singleton"
            )
            row = await cursor.fetchone()
            assert row is not None
            return row["generation"]


class PlatformCatalogRepository:
    """Separate operator path; never supplied as a tenant/agent dependency."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]]) -> None:
        self.pool = pool

    async def latest_service(self, service_id: UUID) -> dict | None:
        """Newest service version as {version, revision, status, manifest}, or None."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT v.version,v.status,v.manifest,s.revision FROM public.mcp_service_versions v "
                "JOIN public.mcp_services s USING (service_id) WHERE v.service_id=%s "
                "ORDER BY v.version DESC LIMIT 1",
                (service_id,),
            )
            return await cursor.fetchone()

    async def latest_operation(self, operation_id: UUID) -> dict | None:
        """Newest operation version as {version, status, manifest}, or None."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT version,status,manifest FROM public.mcp_operation_versions "
                "WHERE operation_id=%s ORDER BY version DESC LIMIT 1",
                (operation_id,),
            )
            return await cursor.fetchone()

    async def add_service(
        self, manifest: ServiceManifest, actor: Attribution, *, expected_revision: int | None = None
    ) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            if manifest.version == 1:
                await conn.execute(
                    "INSERT INTO public.mcp_services(service_id,service_key,created_by) VALUES (%s,%s,%s)",
                    (manifest.service_id, manifest.service_key, snapshot(actor)),
                )
            else:
                cursor = await conn.execute(
                    "SELECT revision,service_key FROM public.mcp_services WHERE service_id=%s FOR UPDATE",
                    (manifest.service_id,),
                )
                row = await cursor.fetchone()
                if (
                    row is None
                    or expected_revision != row["revision"]
                    or row["service_key"] != manifest.service_key
                ):
                    raise CatalogConflictError("service identity or revision mismatch")
                cursor = await conn.execute(
                    "SELECT max(version)+1 AS next FROM public.mcp_service_versions WHERE service_id=%s",
                    (manifest.service_id,),
                )
                next_row = await cursor.fetchone()
                if next_row is None or manifest.version != next_row["next"]:
                    raise CatalogConflictError("service version must append")
                await conn.execute(
                    "UPDATE public.mcp_services SET revision=revision+1 WHERE service_id=%s",
                    (manifest.service_id,),
                )
            await conn.execute(
                "INSERT INTO public.mcp_service_versions(service_id,version,manifest,created_by,updated_by) VALUES (%s,%s,%s,%s,%s)",
                (
                    manifest.service_id,
                    manifest.version,
                    snapshot(manifest),
                    snapshot(actor),
                    snapshot(actor),
                ),
            )

    async def add_operation(self, manifest: OperationManifest, actor: Attribution) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (str(manifest.operation_id),),
            )
            cursor = await conn.execute(
                "SELECT coalesce(max(version),0)+1 AS next FROM public.mcp_operation_versions WHERE operation_id=%s",
                (manifest.operation_id,),
            )
            row = await cursor.fetchone()
            if row is None or manifest.version != row["next"]:
                raise CatalogConflictError("operation version must append")
            await conn.execute(
                "INSERT INTO public.mcp_operation_versions(operation_id,version,service_id,service_version,manifest,created_by,updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    manifest.operation_id,
                    manifest.version,
                    manifest.service.id,
                    manifest.service.version,
                    snapshot(manifest),
                    snapshot(actor),
                    snapshot(actor),
                ),
            )

    async def transition(
        self, kind: str, reference: ExactReference, status: str, actor: Attribution
    ) -> None:
        # SQL identifiers come only from this closed operator-owned mapping.
        from psycopg import sql

        table, identity = {
            "service": ("mcp_service_versions", "service_id"),
            "operation": ("mcp_operation_versions", "operation_id"),
        }[kind]
        if status not in {"published", "disabled", "retired"}:
            raise CatalogConflictError("invalid lifecycle state")
        async with self.pool.connection() as conn, conn.transaction():
            if kind == "service" and status == "published":
                await self.require_source(conn, reference)
            if kind == "operation" and status == "published":
                cursor = await conn.execute(
                    "SELECT s.status FROM public.mcp_operation_versions o JOIN public.mcp_service_versions s "
                    "ON (s.service_id,s.version)=(o.service_id,o.service_version) "
                    "WHERE o.operation_id=%s AND o.version=%s FOR SHARE OF s",
                    (reference.id, reference.version),
                )
                row = await cursor.fetchone()
                if row is None or row["status"] != "published":
                    raise CatalogConflictError("service must be published")
            cursor = await conn.execute(
                sql.SQL(
                    "UPDATE public.{} SET status=%s,updated_by=%s,updated_at=clock_timestamp() WHERE {}=%s AND version=%s RETURNING version"
                ).format(sql.Identifier(table), sql.Identifier(identity)),
                (status, snapshot(actor), reference.id, reference.version),
            )
            if await cursor.fetchone() is None:
                raise CatalogNotFoundError("exact platform version not found")

    @staticmethod
    async def require_source(conn, reference: ExactReference) -> None:
        """Publishing needs ``destination_alias`` to name an active source that agrees with the
        service's licence application and auth adapter (the source owns URL and credential)."""
        cursor = await conn.execute(
            "SELECT manifest FROM public.mcp_service_versions WHERE service_id=%s AND version=%s",
            (reference.id, reference.version),
        )
        found = await cursor.fetchone()
        if found is None:
            raise CatalogNotFoundError("exact platform version not found")
        manifest = ServiceManifest.model_validate_json(json.dumps(found["manifest"]))
        cursor = await conn.execute(
            "SELECT status,auth FROM public.mcp_sources "
            "WHERE owner_tenant_id=public.mcp_platform_tenant() AND source_key=%s FOR SHARE",
            (manifest.destination_alias,),
        )
        source = await cursor.fetchone()
        if source is None or source["status"] != "active":
            raise SourceUnavailableError("service source is not registered and active")
        auth = source["auth"]
        if auth["kind"] == "licence" and (
            auth["app_key"] != manifest.license_app_key
            or {"bearer": "license_bearer", "x_tenant_id_bearer": "license_header"}[
                auth.get("scheme", "bearer")
            ]
            != manifest.auth_adapter
        ):
            raise SourceUnavailableError("service disagrees with its source")

    async def grant_service(self, service_id: UUID, grantee: UUID, actor: Attribution) -> None:
        """Share a platform service (static-credential ones need this) with ``grantee``.
        Idempotent; bumps the catalog generation."""
        async with self.pool.connection() as conn, builder_errors("grant"):
            await conn.execute(
                "INSERT INTO public.mcp_service_grants(service_id,grantee_tenant_id,granted_by) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (service_id, grantee, snapshot(actor)),
            )

    async def revoke_service(self, service_id: UUID, grantee: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.mcp_service_grants WHERE service_id=%s AND grantee_tenant_id=%s",
                (service_id, grantee),
            )

    async def add_operation_key(self, service_id: UUID, op_key: str, operation_id: UUID) -> None:
        """Name a platform operation so tenant tools can address it by ``(source, op_key)``."""
        async with self.pool.connection() as conn, builder_errors("operation key"):
            await conn.execute(
                "INSERT INTO public.mcp_operation_keys(service_id,op_key,operation_id) "
                "VALUES (%s,%s,%s)",
                (service_id, op_key, operation_id),
            )

    async def grant_share(
        self, owner: UUID, tool_id: UUID, grantee: UUID, actor: Attribution
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO public.mcp_tool_shares(owner_tenant_id,tool_id,grantee_tenant_id,granted_by) VALUES (%s,%s,%s,%s)",
                (owner, tool_id, grantee, snapshot(actor)),
            )
