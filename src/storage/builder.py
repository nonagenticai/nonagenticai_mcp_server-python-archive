"""Tenant builder catalog writes and reads (lane 1 contract; see docs/TOOL_BUILDER.md).

Every write goes through a SECURITY DEFINER ``mcp_builder_*`` function (migration 0010) that
rejects the platform tenant and enforces ownership, so a caller bug cannot cross tenants. The
methods accept an optional ``conn`` so a flow (import operations, publish a tool with its
dependencies) can run in one transaction; without it each call is its own transaction.
Database errors surface as ``CatalogConflictError`` / ``CatalogNotFoundError`` /
``QuotaExceededError`` / ``BuilderForbiddenError`` / ``ValueError`` (``builder_errors``).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from src.catalog.models import (
    Attribution,
    ExactReference,
    OperationManifest,
    ServiceManifest,
)
from src.storage.catalog import (
    CatalogConflictError,
    QuotaExceededError,
    builder_errors,
    in_transaction,
    snapshot,
)

Resource = Literal["sources", "operations", "tools", "toolkits"]
Conn = AsyncConnection[dict[str, Any]]

#: Fixed statements only: resource names never reach SQL text.
COUNTS: dict[str, LiteralString] = {
    "sources": "SELECT count(*) AS n FROM public.mcp_sources "
    "WHERE owner_tenant_id=%s AND status='active'",
    "operations": "SELECT count(*) AS n FROM public.mcp_operation_keys k "
    "JOIN public.mcp_services s USING (service_id) WHERE s.owner_tenant_id=%s",
    "tools": "SELECT count(*) AS n FROM public.mcp_tools WHERE tenant_id=%s AND NOT retired",
    "toolkits": "SELECT count(*) AS n FROM public.mcp_toolkits WHERE tenant_id=%s",
}


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    """The newest version of a tenant's service for one of its sources."""

    service_id: UUID
    service_key: str
    revision: int
    version: int
    status: str
    manifest: ServiceManifest


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """The newest version of an operation and the key tools address it by."""

    op_key: str
    operation_id: UUID
    service_id: UUID
    version: int
    status: str
    manifest: OperationManifest


OPERATION_SELECT: LiteralString = (
    "SELECT k.op_key,k.operation_id,k.service_id,o.version,o.status,o.manifest "
    "FROM public.mcp_operation_keys k "
    "JOIN public.mcp_services s ON s.service_id=k.service_id "
    "JOIN LATERAL (SELECT * FROM public.mcp_operation_versions v "
    "  WHERE v.operation_id=k.operation_id ORDER BY v.version DESC LIMIT 1) o ON true "
)


def lock_key(tenant_id: UUID) -> str:
    return f"builder:{tenant_id}"


async def lock_tenant(conn: Conn, tenant_id: UUID) -> None:
    """Serialize a tenant's builder writes for the rest of the transaction. The ``mcp_builder_*``
    functions take the same lock before they count a quota."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key(tenant_id),))


async def usage(conn: Conn, tenant_id: UUID) -> dict[str, int]:
    """Current counts for the four quota resources."""
    result: dict[str, int] = {}
    for name, statement in COUNTS.items():
        cursor = await conn.execute(statement, (tenant_id,))
        row = await cursor.fetchone()
        result[name] = int(row["n"]) if row else 0
    return result


async def enforce_quota(conn: Conn, tenant_id: UUID, resource: Resource, limit: int) -> None:
    """Take the tenant lock, then refuse when ``resource`` already has ``limit`` rows. Call it
    inside the transaction that inserts the new row (tools, toolkits; sources and operations are
    checked by their own functions)."""
    await lock_tenant(conn, tenant_id)
    cursor = await conn.execute(COUNTS[resource], (tenant_id,))
    row = await cursor.fetchone()
    if row is not None and int(row["n"]) >= limit:
        raise QuotaExceededError(resource)


def reference_rows(references: Sequence[ExactReference]) -> str:
    return json.dumps([{"id": str(r.id), "version": r.version} for r in references])


class BuilderCatalogRepository:
    """Runtime-role access to the builder's services and operations."""

    def __init__(self, pool: AsyncConnectionPool[Conn]) -> None:
        self.pool = pool

    # --- writes -------------------------------------------------------------------------------

    async def create_service(
        self,
        tenant_id: UUID,
        manifest: ServiceManifest,
        actor: Attribution,
        *,
        conn: Conn | None = None,
    ) -> None:
        """Version 1 of the service for a tenant source: adapter ``source``, 1:1 with the source
        named by ``destination_alias`` (CatalogNotFoundError when it is not an active own source,
        CatalogConflictError when it already has a service)."""
        async with in_transaction(self.pool, conn) as c, builder_errors("service"):
            await c.execute(
                "SELECT public.mcp_builder_service_create(%s,%s,%s)",
                (tenant_id, snapshot(manifest), snapshot(actor)),
            )

    async def append_service(
        self,
        tenant_id: UUID,
        manifest: ServiceManifest,
        actor: Attribution,
        *,
        expected_revision: int,
        conn: Conn | None = None,
    ) -> None:
        """Version n+1 of an own service (same key and source, ``version`` must be next)."""
        async with in_transaction(self.pool, conn) as c, builder_errors("service"):
            await c.execute(
                "SELECT public.mcp_builder_service_append(%s,%s,%s,%s)",
                (tenant_id, snapshot(manifest), expected_revision, snapshot(actor)),
            )

    async def create_operation(
        self,
        tenant_id: UUID,
        manifest: OperationManifest,
        actor: Attribution,
        *,
        op_key: str | None,
        max_operations: int,
        conn: Conn | None = None,
    ) -> None:
        """A draft operation version. ``op_key`` is required for version 1 (it names the
        operation for ever) and optional afterwards; ``manifest.version`` must be the next one.
        QuotaExceededError when the tenant already has ``max_operations`` keyed operations."""
        async with in_transaction(self.pool, conn) as c, builder_errors("operation"):
            await c.execute(
                "SELECT public.mcp_builder_operation_create(%s,%s,%s,%s,%s)",
                (tenant_id, snapshot(manifest), op_key, snapshot(actor), max_operations),
            )

    async def publish(
        self,
        tenant_id: UUID,
        actor: Attribution,
        *,
        services: Sequence[ExactReference] = (),
        operations: Sequence[ExactReference] = (),
        conn: Conn | None = None,
    ) -> None:
        """Publish own service versions, then operation versions (idempotent for published
        ones). Pass ``conn`` and call ``CatalogRepository.transition(..., conn=conn)`` afterwards
        to publish a tool and its dependencies in one transaction."""
        async with in_transaction(self.pool, conn) as c, builder_errors("publication"):
            await c.execute(
                "SELECT public.mcp_builder_publish(%s,%s::jsonb,%s::jsonb,%s)",
                (tenant_id, reference_rows(services), reference_rows(operations), snapshot(actor)),
            )

    async def set_status(
        self,
        tenant_id: UUID,
        kind: Literal["service", "operation"],
        reference: ExactReference,
        status: Literal["published", "disabled", "retired"],
        actor: Attribution,
        *,
        conn: Conn | None = None,
    ) -> None:
        async with in_transaction(self.pool, conn) as c, builder_errors(kind):
            await c.execute(
                "SELECT public.mcp_builder_set_status(%s,%s,%s,%s,%s,%s)",
                (tenant_id, kind, reference.id, reference.version, status, snapshot(actor)),
            )

    # --- reads --------------------------------------------------------------------------------

    async def service_for_source(
        self, tenant_id: UUID, source_key: str, *, conn: Conn | None = None
    ) -> ServiceRecord | None:
        """The tenant's service for ``source_key`` (newest version), or None."""
        async with in_transaction(self.pool, conn) as c:
            cursor = await c.execute(
                "SELECT s.service_id,s.service_key,s.revision,v.version,v.status,v.manifest "
                "FROM public.mcp_services s "
                "JOIN public.mcp_service_versions v1 ON v1.service_id=s.service_id AND v1.version=1 "
                "JOIN LATERAL (SELECT * FROM public.mcp_service_versions x "
                "  WHERE x.service_id=s.service_id ORDER BY x.version DESC LIMIT 1) v ON true "
                "WHERE s.owner_tenant_id=%s AND v1.manifest->>'destination_alias'=%s",
                (tenant_id, source_key),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return ServiceRecord(
            row["service_id"],
            row["service_key"],
            row["revision"],
            row["version"],
            row["status"],
            ServiceManifest.model_validate_json(json.dumps(row["manifest"])),
        )

    @staticmethod
    def operation_record(row: dict[str, Any]) -> OperationRecord:
        return OperationRecord(
            row["op_key"],
            row["operation_id"],
            row["service_id"],
            row["version"],
            row["status"],
            OperationManifest.model_validate_json(json.dumps(row["manifest"])),
        )

    async def operations(
        self, tenant_id: UUID, source_key: str, *, conn: Conn | None = None
    ) -> list[OperationRecord]:
        """Keyed operations (newest version each) of the tenant's service for ``source_key``,
        ordered by key. Empty when the source has no service yet."""
        service = await self.service_for_source(tenant_id, source_key, conn=conn)
        if service is None:
            return []
        async with in_transaction(self.pool, conn) as c:
            cursor = await c.execute(
                OPERATION_SELECT + "WHERE s.owner_tenant_id=%s AND k.service_id=%s "
                "ORDER BY k.op_key",
                (tenant_id, service.service_id),
            )
            return [self.operation_record(r) for r in await cursor.fetchall()]

    async def operation_by_key(
        self, tenant_id: UUID, source_key: str, op_key: str, *, conn: Conn | None = None
    ) -> OperationRecord | None:
        """Resolve ``(source_key, op_key)`` to the newest version of an own operation, or None."""
        service = await self.service_for_source(tenant_id, source_key, conn=conn)
        if service is None:
            return None
        async with in_transaction(self.pool, conn) as c:
            cursor = await c.execute(
                OPERATION_SELECT + "WHERE s.owner_tenant_id=%s AND k.service_id=%s AND k.op_key=%s",
                (tenant_id, service.service_id, op_key),
            )
            row = await cursor.fetchone()
        return self.operation_record(row) if row else None

    async def visible_platform_services(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Platform services (newest published version) a tenant tool may reference:
        ``[{service_id, service_key, version, via}]`` with ``via`` ``grant`` or ``licence``."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT s.service_id,s.service_key,v.version,"
                "CASE WHEN EXISTS (SELECT 1 FROM public.mcp_service_grants g "
                "  WHERE g.service_id=s.service_id AND g.grantee_tenant_id=%s) "
                "THEN 'grant' ELSE 'licence' END AS via "
                "FROM public.mcp_services s "
                "JOIN LATERAL (SELECT version FROM public.mcp_service_versions x "
                "  WHERE x.service_id=s.service_id AND x.status='published' "
                "  ORDER BY x.version DESC LIMIT 1) v ON true "
                "WHERE s.owner_tenant_id=public.mcp_platform_tenant() "
                "AND public.mcp_tenant_may_use_service(%s,s.service_id,v.version) "
                "ORDER BY s.service_key",
                (tenant_id, tenant_id),
            )
            return await cursor.fetchall()

    async def usage(self, tenant_id: UUID) -> dict[str, int]:
        async with self.pool.connection() as conn:
            return await usage(conn, tenant_id)


__all__ = [
    "BuilderCatalogRepository",
    "CatalogConflictError",
    "OperationRecord",
    "ServiceRecord",
    "enforce_quota",
    "lock_tenant",
    "usage",
]
