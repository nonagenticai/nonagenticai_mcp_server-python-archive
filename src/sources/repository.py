"""Platform-role persistence of tool sources. Ciphertext is write-only: no read path returns it."""

import json
from typing import Any, LiteralString
from urllib.parse import urlsplit
from uuid import UUID, uuid7

from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from src.catalog.models import Attribution
from src.sources.crypto import encrypt_token
from src.sources.guard import enforce
from src.sources.models import (
    SourceBody,
    SourceCreate,
    SourceUpdate,
    SourceView,
    TenantSourceCreate,
    TenantSourceUpdate,
    TokenUpdate,
    checked_secret,
    needs_secret,
)
from src.storage.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    SourceUnavailableError,
    builder_errors,
    in_transaction,
)

Conn = AsyncConnection[dict[str, Any]]
PLATFORM: LiteralString = "owner_tenant_id=public.mcp_platform_tenant()"
VIEW_COLUMNS = sql.SQL(
    "source_id,owner_tenant_id,source_key,revision,status,base_url,allow_internal_http,auth,"
    "(token_ciphertext IS NOT NULL) AS has_token,token_set_at::text AS token_set_at,spec_path,"
    "idempotency_header,ca_file,max_request_bytes,max_response_bytes,created_by,updated_by,"
    "created_at::text AS created_at,updated_at::text AS updated_at"
)
#: What the broker needs to call a source (ciphertext included: decrypt only in the worker).
RESOLVE_COLUMNS: LiteralString = (
    "source_id,owner_tenant_id,source_key,revision,status,base_url,allow_internal_http,auth,"
    "token_ciphertext,spec_path,idempotency_header,ca_file,max_request_bytes,max_response_bytes"
)


async def resolve_row(conn: Conn, owner_tenant_id: UUID, source_key: str) -> dict[str, Any] | None:
    """The source ``(owner, key)`` as the broker consumes it, or None. The owner is the calling
    service's ``owner_tenant_id`` (platform services resolve platform sources, a tenant's
    service resolves that tenant's source); a key alone never identifies a source."""
    cursor = await conn.execute(
        sql.SQL(
            "SELECT {cols} FROM public.mcp_sources WHERE owner_tenant_id=%s AND source_key=%s"
        ).format(cols=sql.SQL(RESOLVE_COLUMNS)),
        (owner_tenant_id, source_key),
    )
    return await cursor.fetchone()


class SourceSecretUnavailableError(RuntimeError):
    """No ``MCP_SOURCE_SECRET_KEY``: static tokens cannot be stored."""


def view(row: dict[str, Any]) -> SourceView:
    return SourceView.model_validate_json(json.dumps(row, default=str))


class SourceRepository:
    def __init__(
        self,
        pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]],
        allowed_hosts: tuple[str, ...],
        secret_key: SecretStr | None,
    ) -> None:
        self.pool = pool
        self.allowed_hosts = allowed_hosts
        self.secret_key = secret_key

    def seal(self, source_id: UUID, token: SecretStr) -> bytes:
        if self.secret_key is None:
            raise SourceSecretUnavailableError("source secret key is not configured")
        return encrypt_token(self.secret_key, source_id, token)

    @staticmethod
    async def event(conn, row, name: str, actor: Attribution) -> None:
        await conn.execute(
            "INSERT INTO public.mcp_source_events(source_id,source_key,revision,event,actor) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                row["source_id"],
                row["source_key"],
                row["revision"],
                name,
                Jsonb(actor.model_dump(mode="json")),
            ),
        )

    async def create(self, body: SourceCreate, actor: Attribution) -> SourceView:
        enforce(body.base_url, body.allow_internal_http, self.allowed_hosts)
        source_id = uuid7()
        sealed = self.seal(source_id, body.token) if body.token is not None else None
        async with self.pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                sql.SQL(
                    "INSERT INTO public.mcp_sources(source_id,source_key,base_url,allow_internal_http,"
                    "auth,token_ciphertext,token_set_at,spec_path,idempotency_header,ca_file,"
                    "max_request_bytes,max_response_bytes,created_by,updated_by) VALUES "
                    "(%s,%s,%s,%s,%s,%s,CASE WHEN %s::bytea IS NULL THEN NULL ELSE clock_timestamp() END,"
                    "%s,%s,%s,%s,%s,%s,%s) RETURNING {cols}"
                ).format(cols=VIEW_COLUMNS),
                (
                    source_id,
                    body.source_key,
                    body.base_url,
                    body.allow_internal_http,
                    Jsonb(body.auth.model_dump(mode="json")),
                    sealed,
                    sealed,
                    body.spec_path,
                    body.idempotency_header,
                    body.ca_file,
                    body.max_request_bytes,
                    body.max_response_bytes,
                    Jsonb(actor.model_dump(mode="json")),
                    Jsonb(actor.model_dump(mode="json")),
                ),
            )
            row = await cursor.fetchone()
            assert row is not None
            await self.event(conn, row, "created", actor)
            return view(row)

    async def page(self, limit: int, offset: int) -> list[SourceView]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_sources WHERE " + PLATFORM + " "
                    "ORDER BY source_key LIMIT %s OFFSET %s"
                ).format(cols=VIEW_COLUMNS),
                (limit, offset),
            )
            return [view(r) for r in await cursor.fetchall()]

    async def get(self, key: str) -> SourceView:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_sources WHERE " + PLATFORM + " AND source_key=%s"
                ).format(cols=VIEW_COLUMNS),
                (key,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError("source not found")
        return view(row)

    @staticmethod
    async def locked(conn, key: str, expected: int) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT source_id,source_key,revision,status,auth,"
            "(token_ciphertext IS NOT NULL) AS has_token FROM public.mcp_sources "
            "WHERE owner_tenant_id=public.mcp_platform_tenant() AND source_key=%s FOR UPDATE",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError("source not found")
        if row["revision"] != expected:
            raise CatalogConflictError("source revision mismatch")
        return row

    async def update(self, key: str, body: SourceUpdate, actor: Attribution) -> SourceView:
        enforce(body.base_url, body.allow_internal_http, self.allowed_hosts)
        async with self.pool.connection() as conn, conn.transaction():
            current = await self.locked(conn, key, body.expected_revision)
            stored = needs_secret(body.auth)
            if not stored and body.token is not None:
                raise ValueError("this source kind carries no stored secret")
            # Switching to a secret-bearing kind needs a secret now; staying keeps the stored one.
            if stored and body.token is None and not current["has_token"]:
                raise ValueError("a source of this kind needs its secret")
            token_sql = sql.SQL("token_ciphertext=token_ciphertext,token_set_at=token_set_at")
            params: list[Any] = []
            if not stored:
                token_sql = sql.SQL("token_ciphertext=NULL,token_set_at=NULL")
            elif body.token is not None:
                token_sql = sql.SQL("token_ciphertext=%s,token_set_at=clock_timestamp()")
                params.append(self.seal(current["source_id"], body.token))
            cursor = await conn.execute(
                sql.SQL(
                    "UPDATE public.mcp_sources SET revision=revision+1,base_url=%s,"
                    "allow_internal_http=%s,auth=%s,spec_path=%s,idempotency_header=%s,ca_file=%s,"
                    "max_request_bytes=%s,max_response_bytes=%s,"
                    "updated_by=%s,updated_at=clock_timestamp(),{token} WHERE source_id=%s "
                    "RETURNING {cols}"
                ).format(token=token_sql, cols=VIEW_COLUMNS),
                (
                    body.base_url,
                    body.allow_internal_http,
                    Jsonb(body.auth.model_dump(mode="json")),
                    body.spec_path,
                    body.idempotency_header,
                    body.ca_file,
                    body.max_request_bytes,
                    body.max_response_bytes,
                    Jsonb(actor.model_dump(mode="json")),
                    *params,
                    current["source_id"],
                ),
            )
            row = await cursor.fetchone()
            assert row is not None
            await self.event(conn, row, "updated", actor)
            return view(row)

    async def set_token(self, key: str, body: TokenUpdate, actor: Attribution) -> SourceView:
        async with self.pool.connection() as conn, conn.transaction():
            current = await self.locked(conn, key, body.expected_revision)
            if current["auth"]["kind"] in {"licence", "none"}:
                raise ValueError("this source kind carries no stored secret")
            checked_secret(current["auth"]["kind"], body.token.get_secret_value())
            cursor = await conn.execute(
                sql.SQL(
                    "UPDATE public.mcp_sources SET revision=revision+1,token_ciphertext=%s,"
                    "token_set_at=clock_timestamp(),updated_by=%s,updated_at=clock_timestamp() "
                    "WHERE source_id=%s RETURNING {cols}"
                ).format(cols=VIEW_COLUMNS),
                (
                    self.seal(current["source_id"], body.token),
                    Jsonb(actor.model_dump(mode="json")),
                    current["source_id"],
                ),
            )
            row = await cursor.fetchone()
            assert row is not None
            await self.event(conn, row, "token_set", actor)
            return view(row)

    async def set_status(
        self, key: str, expected: int, status: str, actor: Attribution
    ) -> SourceView:
        async with self.pool.connection() as conn, conn.transaction():
            current = await self.locked(conn, key, expected)
            if current["status"] == status:
                raise CatalogConflictError("source already in that state")
            cursor = await conn.execute(
                sql.SQL(
                    "UPDATE public.mcp_sources SET revision=revision+1,status=%s,updated_by=%s,"
                    "updated_at=clock_timestamp() WHERE source_id=%s RETURNING {cols}"
                ).format(cols=VIEW_COLUMNS),
                (status, Jsonb(actor.model_dump(mode="json")), current["source_id"]),
            )
            row = await cursor.fetchone()
            assert row is not None
            await self.event(conn, row, "disabled" if status == "disabled" else "enabled", actor)
            return view(row)


class TenantSourceRepository:
    """A tenant's own sources (runtime role; writes via ``mcp_builder_source_*``).

    Every method takes the owning ``tenant_id`` and can only see or change that tenant's rows;
    platform sources are never visible here. Registration enforces what the database cannot:
    allowed ports and, for licence-kind sources, the operator host allow-list (a minted licence
    never goes to an arbitrary host). Egress re-checks hosts on every call.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool[Conn],
        secret_key: SecretStr | None,
        *,
        max_sources: int,
        licence_hosts: tuple[str, ...],
        allowed_ports: tuple[int, ...] = (443,),
        spec_keep: int = 3,
        spec_max_total_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.pool = pool
        self.secret_key = secret_key
        self.max_sources = max_sources
        self.licence_hosts = licence_hosts
        self.allowed_ports = allowed_ports
        self.spec_keep = spec_keep
        self.spec_max_total_bytes = spec_max_total_bytes

    @classmethod
    def from_settings(
        cls, pool: AsyncConnectionPool[Conn], settings: Any
    ) -> TenantSourceRepository:
        return cls(
            pool,
            settings.source_secret_key,
            max_sources=settings.builder_max_sources,
            licence_hosts=settings.source_allowed_hosts,
            allowed_ports=settings.builder_allowed_ports,
            spec_max_total_bytes=settings.builder_spec_max_total_bytes_per_tenant,
        )

    def seal(self, source_id: UUID, token: SecretStr) -> bytes:
        if self.secret_key is None:
            raise SourceSecretUnavailableError("source secret key is not configured")
        return encrypt_token(self.secret_key, source_id, token)

    def check_origin(self, body: TenantSourceCreate | TenantSourceUpdate) -> None:
        urls = [body.base_url]
        token_url = getattr(body.auth, "token_url", None)
        if token_url:
            urls.append(token_url)
        for value in urls:
            port = urlsplit(value).port or 443
            if port not in self.allowed_ports:
                raise ValueError("port is not allowed")
        if body.auth.kind == "licence":
            enforce(body.base_url, False, self.licence_hosts)

    @staticmethod
    def parameters(body: TenantSourceCreate | TenantSourceUpdate) -> tuple[Any, ...]:
        return (
            Jsonb(body.auth.model_dump(mode="json")),
            body.spec_path,
            body.idempotency_header,
            body.max_request_bytes,
            body.max_response_bytes,
        )

    async def create(
        self,
        tenant_id: UUID,
        body: TenantSourceCreate,
        actor: Attribution,
        *,
        conn: Conn | None = None,
    ) -> SourceView:
        """QuotaExceededError at ``max_sources`` active sources; CatalogConflictError when the
        tenant already has that key."""
        self.check_origin(body)
        source_id = uuid7()
        sealed = self.seal(source_id, body.token) if body.token is not None else None
        auth, spec_path, idem, max_req, max_resp = self.parameters(body)
        async with in_transaction(self.pool, conn) as c, builder_errors("source"):
            cursor = await c.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_builder_source_create("
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                ).format(cols=VIEW_COLUMNS),
                (
                    tenant_id,
                    source_id,
                    body.source_key,
                    body.base_url,
                    auth,
                    sealed,
                    spec_path,
                    idem,
                    max_req,
                    max_resp,
                    Jsonb(actor.model_dump(mode="json")),
                    self.max_sources,
                ),
            )
            row = await cursor.fetchone()
        assert row is not None
        return view(row)

    async def update(
        self,
        tenant_id: UUID,
        key: str,
        body: TenantSourceUpdate,
        actor: Attribution,
        *,
        conn: Conn | None = None,
    ) -> SourceView:
        """Replace the source's settings; an omitted ``token`` keeps the stored secret (kinds
        that carry none clear it). Switching to a secret-bearing kind needs ``token``."""
        self.check_origin(body)
        auth, spec_path, idem, max_req, max_resp = self.parameters(body)
        async with in_transaction(self.pool, conn) as c, builder_errors("source"):
            current = await self.owned(c, tenant_id, key)
            sealed = self.seal(current["source_id"], body.token) if body.token is not None else None
            if needs_secret(body.auth) and sealed is None and not current["has_token"]:
                raise ValueError("a source of this kind needs its secret")
            cursor = await c.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_builder_source_update("
                    "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                ).format(cols=VIEW_COLUMNS),
                (
                    tenant_id,
                    key,
                    body.expected_revision,
                    body.base_url,
                    auth,
                    sealed,
                    spec_path,
                    idem,
                    max_req,
                    max_resp,
                    Jsonb(actor.model_dump(mode="json")),
                ),
            )
            row = await cursor.fetchone()
        assert row is not None
        return view(row)

    async def set_secret(
        self,
        tenant_id: UUID,
        key: str,
        body: TokenUpdate,
        actor: Attribution,
        *,
        conn: Conn | None = None,
    ) -> SourceView:
        async with in_transaction(self.pool, conn) as c, builder_errors("source"):
            current = await self.owned(c, tenant_id, key)
            checked_secret(current["auth"]["kind"], body.token.get_secret_value())
            cursor = await c.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_builder_source_set_secret(%s,%s,%s,%s,%s)"
                ).format(cols=VIEW_COLUMNS),
                (
                    tenant_id,
                    key,
                    body.expected_revision,
                    self.seal(current["source_id"], body.token),
                    Jsonb(actor.model_dump(mode="json")),
                ),
            )
            row = await cursor.fetchone()
        assert row is not None
        return view(row)

    async def set_status(
        self,
        tenant_id: UUID,
        key: str,
        expected_revision: int,
        status: str,
        actor: Attribution,
        *,
        conn: Conn | None = None,
    ) -> SourceView:
        """``status`` is ``disabled`` or ``active``; disabling keeps the key reserved."""
        async with in_transaction(self.pool, conn) as c, builder_errors("source"):
            cursor = await c.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_builder_source_set_status(%s,%s,%s,%s,%s)"
                ).format(cols=VIEW_COLUMNS),
                (tenant_id, key, expected_revision, status, Jsonb(actor.model_dump(mode="json"))),
            )
            row = await cursor.fetchone()
        assert row is not None
        return view(row)

    @staticmethod
    async def owned(conn: Conn, tenant_id: UUID, key: str) -> dict[str, Any]:
        cursor = await conn.execute(
            "SELECT source_id,auth,(token_ciphertext IS NOT NULL) AS has_token "
            "FROM public.mcp_sources WHERE owner_tenant_id=%s AND source_key=%s",
            (tenant_id, key),
        )
        row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError("source not found")
        return row

    async def get(self, tenant_id: UUID, key: str, *, conn: Conn | None = None) -> SourceView:
        async with in_transaction(self.pool, conn) as c:
            cursor = await c.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_sources WHERE owner_tenant_id=%s AND source_key=%s"
                ).format(cols=VIEW_COLUMNS),
                (tenant_id, key),
            )
            row = await cursor.fetchone()
        if row is None:
            raise CatalogNotFoundError("source not found")
        return view(row)

    async def page(self, tenant_id: UUID, limit: int, offset: int) -> list[SourceView]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                sql.SQL(
                    "SELECT {cols} FROM public.mcp_sources WHERE owner_tenant_id=%s "
                    "ORDER BY source_key LIMIT %s OFFSET %s"
                ).format(cols=VIEW_COLUMNS),
                (tenant_id, limit, offset),
            )
            return [view(r) for r in await cursor.fetchall()]

    # --- cached specs (last three per source) --------------------------------------------------

    async def put_spec(
        self,
        tenant_id: UUID,
        key: str,
        sha256: str,
        document: dict[str, Any],
        size: int,
        *,
        conn: Conn | None = None,
    ) -> None:
        async with in_transaction(self.pool, conn) as c, builder_errors("spec"):
            current = await self.owned(c, tenant_id, key)
            await c.execute(
                "SELECT public.mcp_builder_spec_put(%s,%s,%s,%s,%s,%s,%s)",
                (
                    tenant_id,
                    current["source_id"],
                    sha256,
                    size,
                    Jsonb(document),
                    self.spec_keep,
                    self.spec_max_total_bytes,
                ),
            )

    async def get_spec(
        self, tenant_id: UUID, key: str, sha256: str | None = None
    ) -> dict[str, Any] | None:
        """The cached ``{sha256, bytes, fetched_at, document}`` with ``sha256``, or the newest
        one; None when absent."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT p.sha256,p.bytes,p.fetched_at::text AS fetched_at,p.document "
                "FROM public.mcp_source_specs p JOIN public.mcp_sources s "
                "ON s.source_id=p.source_id AND s.owner_tenant_id=p.owner_tenant_id "
                "WHERE s.owner_tenant_id=%s AND s.source_key=%s AND (%s::text IS NULL OR p.sha256=%s) "
                "ORDER BY p.fetched_at DESC LIMIT 1",
                (tenant_id, key, sha256, sha256),
            )
            return await cursor.fetchone()


__all__ = [
    "SourceBody",
    "SourceRepository",
    "SourceUnavailableError",
    "TenantSourceRepository",
    "resolve_row",
    "view",
]
