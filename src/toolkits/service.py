"""Privileged access lifecycle; secrets and execution are outside this service."""

import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid7

from psycopg.errors import CheckViolation, UniqueViolation

from src.auth.licenses import digest_token
from src.auth.principal import Principal
from src.catalog.models import Attribution, AuthAdapter
from src.config import Settings
from src.storage.bindings import (
    BindingRepository,
    Connection,
    audit,
    binding_descriptor,
    binding_members,
    check_revision,
    get_binding,
    get_toolkit,
    parse,
    tools_json,
)
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError, snapshot
from src.toolkits.models import (
    BindingDescriptor,
    BindingSpec,
    CredentialProfile,
    ToolkitDescriptor,
    ToolkitManifest,
    owned_toolkits,
)
from src.toolkits.resolver import SelectionResolver


class ContextVerifier:
    """Fail closed until an approved ownership/provenance service is configured.

    Only tenant scope is self-verifying at this stage. API request flags can never
    attest ownership. Later host integrations must implement actual verification.
    """

    async def verify(self, tenant: UUID, spec: BindingSpec) -> None:
        if spec.context.tenant_id != tenant or set(spec.context.public_values()) != {"tenant_id"}:
            raise ValueError(
                "resource and stage context requires host ownership/provenance verification"
            )


class AccessService:
    def __init__(
        self,
        repository: BindingRepository,
        settings: Settings,
        verifier: ContextVerifier | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.resolver = SelectionResolver(settings)
        self.verifier = verifier or ContextVerifier()

    @staticmethod
    def operator(principal: Principal) -> Attribution:
        if not principal.management:
            raise PermissionError("management authorization required")
        return principal.actor

    async def toolkit_write(
        self, principal: Principal, manifest: ToolkitManifest, expected: int | None = None
    ) -> ToolkitDescriptor:
        actor = self.operator(principal)
        try:
            async with self.repository.pool.connection() as conn, conn.transaction():
                return await self.write_toolkit(
                    conn, principal.tenant_id, actor, manifest, expected
                )
        except UniqueViolation:
            raise CatalogConflictError("toolkit identity or name already exists") from None

    async def write_toolkit(
        self,
        conn: Connection,
        tenant: UUID,
        actor: Attribution,
        manifest: ToolkitManifest,
        expected: int | None = None,
    ) -> ToolkitDescriptor:
        """Toolkit draft create/append inside the caller's transaction (routes, platform seed)."""
        manifest = ToolkitManifest.model_validate_json(manifest.model_dump_json())
        if manifest.tenant_id != tenant:
            raise PermissionError("tenant mismatch")
        if expected is None:
            if manifest.version != 1:
                raise ValueError("first toolkit version must be one")
            await conn.execute(
                "INSERT INTO public.mcp_toolkits(tenant_id,toolkit_id,name,created_by) VALUES (%s,%s,%s,%s)",
                (tenant, manifest.toolkit_id, manifest.name, snapshot(actor)),
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM public.mcp_toolkits WHERE tenant_id=%s AND toolkit_id=%s FOR UPDATE",
                (tenant, manifest.toolkit_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise CatalogNotFoundError("toolkit not found")
            check_revision(row, expected)
            if row["name"] != manifest.name:
                raise ValueError("toolkit name is immutable")
            cur = await conn.execute(
                "SELECT max(version)+1 AS next FROM public.mcp_toolkit_versions WHERE tenant_id=%s AND toolkit_id=%s",
                (tenant, manifest.toolkit_id),
            )
            next_row = await cur.fetchone()
            assert next_row is not None
            manifest = manifest.model_copy(update={"version": next_row["next"]})
            await conn.execute(
                "UPDATE public.mcp_toolkits SET revision=revision+1 WHERE tenant_id=%s AND toolkit_id=%s",
                (tenant, manifest.toolkit_id),
            )
        tools = await self.resolver.flatten(conn, manifest)
        await conn.execute(
            "INSERT INTO public.mcp_toolkit_versions(tenant_id,toolkit_id,version,manifest,tools,created_by,updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant,
                manifest.toolkit_id,
                manifest.version,
                snapshot(manifest),
                tools_json(tools),
                snapshot(actor),
                snapshot(actor),
            ),
        )
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO public.mcp_toolkit_version_tools VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        tenant,
                        manifest.toolkit_id,
                        manifest.version,
                        t.tool.tenant_id,
                        t.tool.id,
                        t.tool.version,
                        t.public_name,
                    )
                    for t in tools
                ],
            )
            await cur.executemany(
                "INSERT INTO public.mcp_toolkit_inclusions VALUES (%s,%s,%s,%s,%s)",
                [
                    (tenant, manifest.toolkit_id, manifest.version, r.id, r.version)
                    for r in manifest.includes
                ],
            )
        result = await get_toolkit(conn, tenant, manifest.toolkit_id, manifest.version)
        await audit(conn, tenant, actor, manifest.toolkit_id, result.revision, "toolkit.draft")
        return result

    async def toolkit_transition(
        self, principal: Principal, identity: UUID, version: int, status: str, expected: int
    ) -> ToolkitDescriptor:
        actor = self.operator(principal)
        try:
            async with self.repository.pool.connection() as conn, conn.transaction():
                return await self.transition_toolkit(
                    conn, principal.tenant_id, actor, identity, version, status, expected
                )
        except CheckViolation:
            raise CatalogConflictError("toolkit dependencies unavailable") from None

    async def transition_toolkit(
        self,
        conn: Connection,
        tenant: UUID,
        actor: Attribution,
        identity: UUID,
        version: int,
        status: str,
        expected: int,
    ) -> ToolkitDescriptor:
        """Toolkit lifecycle change inside the caller's transaction."""
        cur = await conn.execute(
            "SELECT * FROM public.mcp_toolkits WHERE tenant_id=%s AND toolkit_id=%s FOR UPDATE",
            (tenant, identity),
        )
        row = await cur.fetchone()
        if row is None:
            raise CatalogNotFoundError("toolkit not found")
        check_revision(row, expected)
        original = await get_toolkit(conn, tenant, identity, version)
        if (original.status, status) not in {
            ("draft", "published"),
            ("draft", "disabled"),
            ("published", "disabled"),
        }:
            raise CatalogConflictError("invalid toolkit transition")
        if status == "published":
            if await self.resolver.flatten(conn, original.manifest) != original.tools:
                raise CatalogConflictError("toolkit dependency mismatch")
        await conn.execute(
            "UPDATE public.mcp_toolkit_versions SET status=%s,updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND toolkit_id=%s AND version=%s",
            (status, snapshot(actor), tenant, identity, version),
        )
        await conn.execute(
            "UPDATE public.mcp_toolkits SET revision=revision+1 WHERE tenant_id=%s AND toolkit_id=%s",
            (tenant, identity),
        )
        result = await get_toolkit(conn, tenant, identity, version)
        await audit(conn, tenant, actor, identity, result.revision, "toolkit." + status)
        return result

    async def profile_write(
        self, principal: Principal, profile: CredentialProfile, expected: int | None = None
    ) -> dict:
        actor = self.operator(principal)
        try:
            async with self.repository.pool.connection() as conn, conn.transaction():
                return await self.write_profile(conn, principal.tenant_id, actor, profile, expected)
        except UniqueViolation:
            raise CatalogConflictError(
                "credential profile identity or name already exists"
            ) from None

    async def write_profile(
        self,
        conn: Connection,
        tenant: UUID,
        actor: Attribution,
        profile: CredentialProfile,
        expected: int | None = None,
    ) -> dict:
        """Profile write inside the caller's transaction (operator routes, bindings/ensure)."""
        profile = CredentialProfile.model_validate_json(profile.model_dump_json())
        if profile.tenant_id != tenant:
            raise PermissionError("tenant mismatch")
        if profile.gateway.app_key != self.settings.license_app_key:
            raise ValueError("gateway credential must reference the MCP application")
        if expected is None:
            await conn.execute(
                "INSERT INTO public.mcp_credential_profiles(tenant_id,profile_id,name,manifest,created_by,updated_by) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    tenant,
                    profile.profile_id,
                    profile.name,
                    snapshot(profile),
                    snapshot(actor),
                    snapshot(actor),
                ),
            )
            revision = 1
        else:
            row = await self.profile_row(conn, tenant, profile.profile_id, lock=True)
            check_revision(row, expected)
            if row["name"] != profile.name:
                raise ValueError("profile name is immutable")
            revision = expected + 1
            await conn.execute(
                "UPDATE public.mcp_credential_profiles SET manifest=%s,revision=revision+1,updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND profile_id=%s",
                (
                    snapshot(profile),
                    snapshot(actor),
                    tenant,
                    profile.profile_id,
                ),
            )
        await conn.execute(
            "DELETE FROM public.mcp_credential_refs WHERE tenant_id=%s AND profile_id=%s",
            (tenant, profile.profile_id),
        )
        refs = [("__gateway", profile.gateway)] + [(ref.service, ref) for ref in profile.services]
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO public.mcp_credential_refs VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    (
                        tenant,
                        profile.profile_id,
                        service,
                        ref.alias,
                        ref.license_id,
                        ref.app_key,
                    )
                    for service, ref in refs
                ],
            )
        await audit(
            conn,
            tenant,
            actor,
            profile.profile_id,
            revision,
            "credential_profile.changed",
        )
        return {"profile": profile, "revision": revision, "credentials_verified": False}

    @staticmethod
    async def profile_row(
        conn: Connection, tenant: UUID, identity: UUID, *, lock: bool = False, share: bool = False
    ) -> dict:
        cur = await conn.execute(
            "SELECT * FROM public.mcp_credential_profiles WHERE tenant_id=%s AND profile_id=%s FOR UPDATE"
            if lock
            else "SELECT * FROM public.mcp_credential_profiles WHERE tenant_id=%s AND profile_id=%s FOR SHARE"
            if share
            else "SELECT * FROM public.mcp_credential_profiles WHERE tenant_id=%s AND profile_id=%s",
            (tenant, identity),
        )
        row = await cur.fetchone()
        if row is None:
            raise CatalogNotFoundError("credential profile not found")
        return row

    async def validate_profile(
        self, conn: Connection, tenant: UUID, spec: BindingSpec, tools, *, share: bool = True
    ) -> CredentialProfile:
        # Serializes reference edits with authority issuance, without exposing values.
        row = await self.profile_row(conn, tenant, spec.credential_profile_id, share=share)
        profile = parse(CredentialProfile, row["manifest"])
        if profile.gateway.app_key != self.settings.license_app_key:
            raise ValueError("gateway credential has wrong application")
        refs = {r.service: r for r in profile.services}
        loaded = await self.resolver.tools_bulk(
            conn, tenant, [entry.tool for entry in tools], bound=True
        )
        # Every service behind the selection in one read, not one read per operation.
        wanted: dict[tuple[UUID, int], None] = {}
        for entry in tools:
            for op in loaded[entry.tool].allowed_operations:
                wanted.setdefault((op.id, op.version), None)
        services: dict[tuple[UUID, int], dict] = {}
        if wanted:
            cur = await conn.execute(
                "SELECT o.operation_id,o.version,s.manifest FROM public.mcp_operation_versions o "
                "JOIN public.mcp_service_versions s "
                "ON (s.service_id,s.version)=(o.service_id,o.service_version) "
                "WHERE (o.operation_id,o.version) IN (SELECT * FROM unnest(%s::uuid[],%s::int[]))",
                ([key[0] for key in wanted], [key[1] for key in wanted]),
            )
            for row in await cur.fetchall():
                services[(row["operation_id"], row["version"])] = row["manifest"]
        for entry in tools:
            manifest = loaded[entry.tool]
            for op in manifest.allowed_operations:
                service = services.get((op.id, op.version))
                assert service is not None
                if service.get("auth_adapter") == AuthAdapter.SOURCE:
                    # A tenant source carries its own credential (docs/TOOL_BUILDER.md).
                    continue
                ref = refs.get(service["service_key"])
                if ref is None or ref.app_key != service["license_app_key"]:
                    raise ValueError("credential profile lacks required application reference")
        return profile

    async def binding_write(
        self,
        principal: Principal,
        spec: BindingSpec,
        identity: UUID | None = None,
        expected: int | None = None,
    ) -> tuple[BindingDescriptor, str | None]:
        actor = self.operator(principal)
        async with self.repository.pool.connection() as conn, conn.transaction():
            return await self.write_binding(
                conn, principal.tenant_id, actor, spec, identity, expected
            )

    async def write_binding(
        self,
        conn: Connection,
        tenant: UUID,
        actor: Attribution,
        spec: BindingSpec,
        identity: UUID | None = None,
        expected: int | None = None,
    ) -> tuple[BindingDescriptor, str | None]:
        """Binding create/update inside the caller's transaction. A new token only on create."""
        spec = owned_toolkits(BindingSpec.model_validate_json(spec.model_dump_json()), tenant)
        if spec.expires_at <= datetime.now(UTC):
            raise ValueError("binding expiry must be in the future")
        await self.verifier.verify(tenant, spec)
        token = secrets.token_urlsafe(32) if identity is None else None
        identity = identity or uuid7()
        old = None
        if expected is not None:
            old = await get_binding(conn, tenant, identity, lock=True)
            check_revision(old, expected)
            if old["revoked"]:
                raise CatalogConflictError("binding is revoked")
        elif token is None:
            raise ValueError("binding updates require an expected revision")
        tools = await self.resolver.binding(conn, tenant, spec, lock=True)
        profile = await self.validate_profile(conn, tenant, spec, tools)
        if old and profile.gateway.license_id != old["gateway_license_id"]:
            raise ValueError("gateway license identity is immutable; issue a new binding")
        if old:
            await conn.execute(
                "UPDATE public.mcp_bindings SET spec=%s,tools=%s,credential_profile_id=%s,enabled=%s,expires_at=%s,revision=revision+1,updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND binding_id=%s",
                (
                    snapshot(spec),
                    tools_json(tools),
                    spec.credential_profile_id,
                    spec.enabled,
                    spec.expires_at,
                    snapshot(actor),
                    tenant,
                    identity,
                ),
            )
        else:
            assert token is not None
            await conn.execute(
                "INSERT INTO public.mcp_bindings(tenant_id,binding_id,gateway_license_id,credential_profile_id,token_sha256,spec,tools,enabled,expires_at,created_by,updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    tenant,
                    identity,
                    profile.gateway.license_id,
                    spec.credential_profile_id,
                    digest_token(token),
                    snapshot(spec),
                    tools_json(tools),
                    spec.enabled,
                    spec.expires_at,
                    snapshot(actor),
                    snapshot(actor),
                ),
            )
        result = binding_descriptor(await get_binding(conn, tenant, identity))
        await binding_members(conn, result)
        await audit(conn, tenant, actor, identity, result.revision, "binding.changed")
        return result, token

    async def binding_action(
        self, principal: Principal, identity: UUID, expected: int, *, revoke: bool = False
    ) -> tuple[BindingDescriptor, str | None]:
        actor = self.operator(principal)
        async with self.repository.pool.connection() as conn, conn.transaction():
            return await self.act_binding(
                conn, principal.tenant_id, actor, identity, expected, revoke=revoke
            )

    async def act_binding(
        self,
        conn: Connection,
        tenant: UUID,
        actor: Attribution,
        identity: UUID,
        expected: int,
        *,
        revoke: bool = False,
    ) -> tuple[BindingDescriptor, str | None]:
        """Rotate (new token) or revoke inside the caller's transaction; sessions are dropped."""
        token = None if revoke else secrets.token_urlsafe(32)
        row = await get_binding(conn, tenant, identity, lock=True)
        check_revision(row, expected)
        if row["revoked"]:
            raise CatalogConflictError("binding is revoked")
        await conn.execute(
            "UPDATE public.mcp_bindings SET token_sha256=%s,revoked=%s,revision=revision+1,updated_by=%s,updated_at=clock_timestamp() WHERE tenant_id=%s AND binding_id=%s",
            (
                row["token_sha256"] if token is None else digest_token(token),
                revoke,
                snapshot(actor),
                tenant,
                identity,
            ),
        )
        await conn.execute(
            "DELETE FROM public.mcp_protocol_sessions WHERE tenant_id=%s AND binding_id=%s",
            (tenant, identity),
        )
        result = binding_descriptor(await get_binding(conn, tenant, identity))
        await audit(
            conn,
            tenant,
            actor,
            identity,
            result.revision,
            "binding.revoked" if revoke else "binding.rotated",
        )
        return result, token

    async def effective(self, binding: BindingDescriptor):
        async with self.repository.pool.connection() as conn:
            profile = await self.validate_profile(
                conn, binding.tenant_id, binding.spec, binding.tools, share=False
            )
            if profile.gateway.license_id != binding.gateway_license_id:
                raise CatalogConflictError("gateway profile changed")
            current = await self.resolver.binding(conn, binding.tenant_id, binding.spec)
            if current != binding.tools:
                raise CatalogConflictError("effective selection changed")
            return current
