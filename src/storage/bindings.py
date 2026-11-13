"""Tenant-scoped access storage. Transactions include selection, changes and audit."""

import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid7

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from src.auth.licenses import AuthenticationError, LicenseIdentity, digest_token
from src.catalog.models import Attribution
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError
from src.toolkits.models import (
    BindingDescriptor,
    BindingSpec,
    EffectiveTool,
    ToolkitDescriptor,
    ToolkitManifest,
)

Connection = AsyncConnection[dict[str, Any]]


def parse(model, value):
    return model.model_validate_json(json.dumps(value))


def toolkit_descriptor(row: dict) -> ToolkitDescriptor:
    return ToolkitDescriptor(
        manifest=parse(ToolkitManifest, row["manifest"]),
        revision=row["revision"],
        status=row["status"],
        tools=tuple(parse(EffectiveTool, t) for t in row["tools"]),
    )


def binding_descriptor(row: dict) -> BindingDescriptor:
    return BindingDescriptor(
        tenant_id=row["tenant_id"],
        binding_id=row["binding_id"],
        gateway_license_id=row["gateway_license_id"],
        revision=row["revision"],
        revoked=row["revoked"],
        spec=parse(BindingSpec, row["spec"]),
        tools=tuple(parse(EffectiveTool, t) for t in row["tools"]),
    )


async def audit(
    conn: Connection,
    tenant: UUID,
    actor: Attribution,
    entity: UUID,
    revision: int,
    action: str,
    artifact_version: int | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO public.mcp_access_events(event_id,tenant_id,subject,license_id,entity_id,revision,action,artifact_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            uuid7(),
            tenant,
            actor.subject,
            actor.license_id,
            entity,
            revision,
            action,
            artifact_version,
        ),
    )


async def get_toolkit(
    conn: Connection, tenant: UUID, identity: UUID, version: int
) -> ToolkitDescriptor:
    cur = await conn.execute(
        "SELECT v.*,t.revision FROM public.mcp_toolkit_versions v JOIN public.mcp_toolkits t USING(tenant_id,toolkit_id) "
        "WHERE v.tenant_id=%s AND v.toolkit_id=%s AND v.version=%s",
        (tenant, identity, version),
    )
    row = await cur.fetchone()
    if row is None:
        raise CatalogNotFoundError("toolkit version not found")
    return toolkit_descriptor(row)


async def latest_toolkit(
    conn: Connection, tenant: UUID, identity: UUID
) -> ToolkitDescriptor | None:
    """Highest version of a toolkit identity (any status), or None when it does not exist."""
    cur = await conn.execute(
        "SELECT v.*,t.revision FROM public.mcp_toolkit_versions v JOIN public.mcp_toolkits t USING(tenant_id,toolkit_id) "
        "WHERE v.tenant_id=%s AND v.toolkit_id=%s ORDER BY v.version DESC LIMIT 1",
        (tenant, identity),
    )
    row = await cur.fetchone()
    return toolkit_descriptor(row) if row else None


async def get_binding(
    conn: Connection, tenant: UUID, identity: UUID, *, lock: bool = False
) -> dict:
    cur = await conn.execute(
        "SELECT * FROM public.mcp_bindings WHERE tenant_id=%s AND binding_id=%s FOR UPDATE"
        if lock
        else "SELECT * FROM public.mcp_bindings WHERE tenant_id=%s AND binding_id=%s",
        (tenant, identity),
    )
    row = await cur.fetchone()
    if row is None:
        raise CatalogNotFoundError("binding not found")
    return row


async def binding_members(conn: Connection, binding: BindingDescriptor) -> None:
    tenant, identity = binding.tenant_id, binding.binding_id
    await conn.execute(
        "DELETE FROM public.mcp_binding_tools WHERE tenant_id=%s AND binding_id=%s",
        (tenant, identity),
    )
    await conn.execute(
        "DELETE FROM public.mcp_binding_toolkits WHERE tenant_id=%s AND binding_id=%s",
        (tenant, identity),
    )
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO public.mcp_binding_toolkits(tenant_id,binding_id,toolkit_id,version,"
            "owner_tenant_id) VALUES (%s,%s,%s,%s,%s)",
            [
                (tenant, identity, r.id, r.version, r.tenant_id or tenant)
                for r in binding.spec.toolkits
            ],
        )
        await cur.executemany(
            "INSERT INTO public.mcp_binding_tools VALUES (%s,%s,%s,%s,%s,%s)",
            [
                (tenant, identity, t.tool.tenant_id, t.tool.id, t.tool.version, t.public_name)
                for t in binding.tools
            ],
        )


class BindingRepository:
    def __init__(self, pool: AsyncConnectionPool[Connection]) -> None:
        self.pool = pool

    async def inspect(self, tenant: UUID, identity: UUID) -> BindingDescriptor:
        async with self.pool.connection() as conn:
            return binding_descriptor(await get_binding(conn, tenant, identity))

    async def authenticate(self, identity: LicenseIdentity, token: str) -> BindingDescriptor:
        hashed = digest_token(token)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM public.mcp_bindings WHERE tenant_id=%s AND token_sha256=%s",
                (identity.tenant_id, hashed),
            )
            row = await cur.fetchone()
            if (
                row is None
                or not hmac.compare_digest(row["token_sha256"], hashed)
                or row["gateway_license_id"] != identity.license_id
                or not row["enabled"]
                or row["revoked"]
                or row["expires_at"] <= datetime.now(UTC)
            ):
                raise AuthenticationError("binding denied")
            return binding_descriptor(row)

    async def check_session(
        self, session: str, identity: LicenseIdentity, binding: BindingDescriptor, token: str
    ) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM public.mcp_protocol_sessions WHERE session_sha256=%s",
                (digest_token(session),),
            )
            row = await cur.fetchone()
            if (
                row is None
                or row["tenant_id"] != identity.tenant_id
                or row["binding_id"] != binding.binding_id
                or row["gateway_license_id"] != identity.license_id
                or not hmac.compare_digest(row["token_sha256"], digest_token(token))
                or row["expires_at"] <= datetime.now(UTC)
            ):
                raise AuthenticationError("session denied")

    async def pin_session(
        self,
        session: str,
        identity: LicenseIdentity,
        binding: BindingDescriptor,
        token: str,
        ttl: int,
    ) -> None:
        expires = min(binding.spec.expires_at, datetime.now(UTC) + timedelta(seconds=ttl))
        if identity.valid_until != float("inf"):
            expires = min(expires, datetime.fromtimestamp(identity.valid_until, UTC))
        async with self.pool.connection() as conn, conn.transaction():
            # Bounded opportunistic cleanup. No background session cache or cross-replica affinity.
            await conn.execute(
                "DELETE FROM public.mcp_protocol_sessions WHERE session_sha256 IN "
                "(SELECT session_sha256 FROM public.mcp_protocol_sessions WHERE expires_at<=clock_timestamp() LIMIT 100)"
            )
            cur = await conn.execute(
                "INSERT INTO public.mcp_protocol_sessions VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING session_sha256",
                (
                    digest_token(session),
                    identity.tenant_id,
                    binding.binding_id,
                    identity.license_id,
                    digest_token(token),
                    expires,
                ),
            )
            if await cur.fetchone() is None:
                raise AuthenticationError("session identity already exists")

    async def delete_session(self, session: str, tenant: UUID, binding: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.mcp_protocol_sessions WHERE session_sha256=%s AND tenant_id=%s AND binding_id=%s",
                (digest_token(session), tenant, binding),
            )


def tools_json(tools: tuple[EffectiveTool, ...]) -> Jsonb:
    return Jsonb([t.model_dump(mode="json") for t in tools])


def check_revision(row: dict, expected: int) -> None:
    if row["revision"] != expected:
        raise CatalogConflictError("expected revision conflict")
