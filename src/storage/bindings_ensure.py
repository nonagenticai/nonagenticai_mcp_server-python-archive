"""POST /mcp/internal/tenants/{t}/bindings/ensure: one binding per (tenant, product).

A product's provisioning (agent, care-bus approval) calls this with a Keycloak service JWT and
the tenant's mcp licence it holds (``licence_id`` plus ``licence_token``). The token is
introspected: it must be the path tenant's tenant-level mcp licence with exactly that id, or
nothing is written. mcp never mints its own (``app=mcp``) licence.

The product -> toolkit-name table is declarative (Settings.binding_products). Each named toolkit
resolves to the **platform tenant's** latest published version of that name (first-party
toolkits are platform-owned) and is pinned in the binding; a toolkit the platform has not
published, or whose backend credentials cannot be established, is skipped with a reason. Backend
credential references carry the product's licences bundle (Settings.product_bundles) so the
minter never meets ``ambiguous_bundle``.

Everything is written in ONE transaction serialized on the (tenant, product) ensure row, which
records the binding's product: the credential profile (gateway = the tenant's mcp licence,
services = the backend licences the selected tools need), the binding, the ensure row and an
access event. The binding token is returned only when it is issued (create, or an explicit
rotate); PostgreSQL stores its digest only. A re-ensure reports ``token_already_issued`` and
never returns the token again.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid7

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from src.auth.licenses import AuthenticationError, AuthorityUnavailableError
from src.catalog.models import Attribution, ToolLimits
from src.credentials.minter import MinterUnavailableError
from src.execution.models import ExecutionDeniedError
from src.storage.bindings import Connection, audit, get_binding, parse
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError
from src.toolkits.models import (
    BindingSpec,
    BoundContext,
    CredentialProfile,
    CredentialReference,
    ToolkitReference,
)
from src.toolkits.service import AccessService

NOT_PUBLISHED = "not_published"
NAME_COLLISION = "tool_name_collision"
#: Binding-level ceiling: tool manifests (and their selections) narrow below it.
MAX_LIMITS = ToolLimits(
    wall_seconds=300, queue_seconds=240, api_calls=100, frame_bytes=262144, output_bytes=1048576
)
CREDENTIALS_UNAVAILABLE = "service_credentials_unavailable"
GATEWAY_SERVICE = "gateway"


def profile_name(product: str) -> str:
    return "ensure_" + product.replace("-", "_")


def actor_for(azp: str, licence: UUID) -> Attribution:
    return Attribution(subject=f"service:{azp}"[:256], license_id=licence)


class BindingEnsureService:
    def __init__(self, access: AccessService, licenses=None, minter=None) -> None:
        self.access = access
        self.settings = access.settings
        self.pool = access.repository.pool
        self.licenses = licenses
        self.minter = minter

    async def gateway_licence(self, tenant: UUID, licence_id: UUID, licence_token: str) -> UUID:
        """The product-held mcp licence, verified by introspection; fails closed.

        It must authenticate for the mcp application as the path tenant's tenant-level
        (``subject_kind: tenant``) licence whose id is ``licence_id``.
        """
        app = self.settings.license_app_key
        if self.licenses is None or not app:
            raise AuthorityUnavailableError("license authentication is not configured")
        try:
            identity = await self.licenses.authenticate(licence_token, app)
        except AuthenticationError:
            raise PermissionError("the mcp licence did not authenticate") from None
        if (
            identity.tenant_id != tenant
            or not identity.tenant_level
            or identity.license_id != licence_id
        ):
            raise PermissionError("the licence is not this tenant's tenant-level mcp licence")
        return identity.license_id

    async def discover(
        self, conn: Connection, names: tuple[str, ...], tenant: UUID | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Latest published toolkit version per name, with its backend services.

        First-party toolkits are the platform tenant's, looked up by the product's names. With
        ``tenant``, every toolkit that tenant authored and published through the tool builder is
        added too (docs/TOOL_BUILDER.md decision 4), so a caller keeps ONE binding per product.
        """
        platform = self.settings.platform_tenant_id
        cur = await conn.execute(
            "SELECT t.name,v.toolkit_id,max(v.version) AS version FROM public.mcp_toolkits t "
            "JOIN public.mcp_toolkit_versions v USING(tenant_id,toolkit_id) "
            "WHERE t.tenant_id=%s AND t.name=ANY(%s) AND v.status='published' "
            "GROUP BY t.name,v.toolkit_id",
            (platform, list(names)),
        )
        found = {row["name"]: {**row, "tenant_id": platform} for row in await cur.fetchall()}
        own: list[dict[str, Any]] = []
        if tenant is not None and tenant != platform:
            cur = await conn.execute(
                "SELECT t.name,v.toolkit_id,max(v.version) AS version FROM public.mcp_toolkits t "
                "JOIN public.mcp_toolkit_versions v USING(tenant_id,toolkit_id) "
                "WHERE t.tenant_id=%s AND v.status='published' "
                "GROUP BY t.name,v.toolkit_id ORDER BY t.name",
                (tenant,),
            )
            own = [{**row, "tenant_id": tenant} for row in await cur.fetchall()]
        resolved, skipped = [], []
        candidates = [(name, found.get(name)) for name in names] + [(r["name"], r) for r in own]
        taken: dict[str, tuple[UUID, UUID]] = {}
        for name, row in candidates:
            if row is None:
                skipped.append({"toolkit": name, "reason": NOT_PUBLISHED})
                continue
            cur = await conn.execute(
                "SELECT public_name,owner_tenant_id,tool_id FROM public.mcp_toolkit_version_tools "
                "WHERE tenant_id=%s AND toolkit_id=%s AND version=%s",
                (row["tenant_id"], row["toolkit_id"], row["version"]),
            )
            names_in = {
                r["public_name"]: (r["owner_tenant_id"], r["tool_id"]) for r in await cur.fetchall()
            }
            if row["tenant_id"] != platform and any(
                n in taken and taken[n] != who for n, who in names_in.items()
            ):
                # A tenant toolkit never displaces a tool already selected (or breaks the
                # binding): it is reported and left out until the names differ.
                skipped.append({"toolkit": name, "reason": NAME_COLLISION})
                continue
            taken.update(names_in)
            cur = await conn.execute(
                "SELECT DISTINCT s.manifest->>'service_key' AS service,"
                "s.manifest->>'license_app_key' AS app "
                "FROM public.mcp_toolkit_versions tv "
                "CROSS JOIN LATERAL jsonb_array_elements(tv.tools) e "
                "JOIN public.mcp_tool_version_operations r ON r.tenant_id=(e->'tool'->>'tenant_id')::uuid "
                "AND r.tool_id=(e->'tool'->>'id')::uuid AND r.version=(e->'tool'->>'version')::integer "
                "JOIN public.mcp_operation_versions o ON (o.operation_id,o.version)=(r.operation_id,r.operation_version) "
                "JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version) "
                # A tenant source carries its own credential: no licence reference to establish.
                "WHERE tv.tenant_id=%s AND tv.toolkit_id=%s AND tv.version=%s "
                "AND s.manifest->>'auth_adapter' IS DISTINCT FROM 'source' ORDER BY 1",
                (row["tenant_id"], row["toolkit_id"], row["version"]),
            )
            services = {r["service"]: r["app"] for r in await cur.fetchall()}
            resolved.append(
                {
                    "name": name,
                    "tenant_id": row["tenant_id"],
                    "toolkit_id": row["toolkit_id"],
                    "version": row["version"],
                    "services": services,
                }
            )
        return resolved, skipped

    async def service_references(
        self,
        tenant: UUID,
        resolved: list[dict[str, Any]],
        skipped: list[dict[str, str]],
        bundle: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, CredentialReference]]:
        """Backend licence references for each toolkit; toolkits lacking one are skipped."""
        known: dict[str, UUID | None] = {}
        kept, references = [], {}
        for toolkit in resolved:
            ok = True
            for service, app in toolkit["services"].items():
                if app not in known:
                    known[app] = None
                    if self.minter is not None:
                        try:
                            known[app] = await self.minter.licence_id(tenant, app, bundle)
                        except MinterUnavailableError:
                            raise AuthorityUnavailableError(
                                "licence authority unavailable"
                            ) from None
                        except ExecutionDeniedError:
                            # 403 dependency_grant_scope, no licence: skip, fail closed.
                            known[app] = None
                licence = known[app]
                if licence is None or not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", service):
                    ok = False
                    continue
                references[service] = CredentialReference(
                    service=service, alias=service, license_id=licence, app_key=app, bundle=bundle
                )
            if ok:
                kept.append(toolkit)
            else:
                skipped.append({"toolkit": toolkit["name"], "reason": CREDENTIALS_UNAVAILABLE})
        needed = {s for t in kept for s in t["services"]}
        return kept, {k: v for k, v in references.items() if k in needed}

    async def ensure(
        self,
        tenant: UUID,
        product: str,
        azp: str,
        *,
        licence_id: UUID,
        licence_token: str,
        rotate: bool = False,
    ) -> dict[str, Any]:
        names = self.settings.binding_products.get(product)
        if names is None:
            raise CatalogNotFoundError("unknown product")
        if tenant == self.settings.platform_tenant_id:
            raise PermissionError("the platform tenant is not provisioned through a product")
        gateway = await self.gateway_licence(tenant, licence_id, licence_token)
        async with self.pool.connection() as conn:
            resolved, skipped = await self.discover(conn, tuple(names), tenant)
        resolved, services = await self.service_references(
            tenant, resolved, skipped, self.settings.product_bundles[product]
        )
        actor = actor_for(azp, gateway)
        try:
            async with self.pool.connection() as conn, conn.transaction():
                return await self.write(
                    conn, tenant, product, actor, gateway, resolved, services, skipped, rotate
                )
        except UniqueViolation:
            raise CatalogConflictError("ensure profile name is already in use") from None

    async def rebind(
        self, tenant: UUID, actor: Attribution, gateway: UUID, product: str | None = None
    ) -> list[dict[str, Any]]:
        """Re-provision a tenant's EXISTING product bindings with its current toolkits (platform
        toolkits of the product plus the tenant's own published ones). No token is issued or
        returned: a binding is only updated when it was provisioned for ``gateway`` (the caller's
        own tenant-level mcp licence); otherwise it is reported ``licence_changed`` and left to
        bindings/ensure."""
        if tenant == self.settings.platform_tenant_id:
            raise PermissionError("the platform tenant is not provisioned through a product")
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT product,gateway_license_id FROM public.mcp_binding_ensures "
                "WHERE tenant_id=%s AND binding_id IS NOT NULL AND (%s::text IS NULL OR product=%s) "
                "ORDER BY product",
                (tenant, product, product),
            )
            rows = await cur.fetchall()
        report: list[dict[str, Any]] = []
        for row in rows:
            name = row["product"]
            names = self.settings.binding_products.get(name)
            if names is None:
                report.append({"product": name, "status": "unknown_product"})
                continue
            if row["gateway_license_id"] != gateway:
                report.append({"product": name, "status": "licence_changed"})
                continue
            async with self.pool.connection() as conn:
                resolved, skipped = await self.discover(conn, tuple(names), tenant)
            resolved, services = await self.service_references(
                tenant, resolved, skipped, self.settings.product_bundles[name]
            )
            try:
                async with self.pool.connection() as conn, conn.transaction():
                    result = await self.write(
                        conn, tenant, name, actor, gateway, resolved, services, skipped, False
                    )
            except UniqueViolation:
                raise CatalogConflictError("ensure profile name is already in use") from None
            report.append(
                {
                    "product": name,
                    "status": result["status"],
                    "binding_revision": result["binding_revision"],
                    "toolkits": result["toolkits"],
                    "skipped": result["skipped"],
                }
            )
        return report

    async def write(
        self,
        conn: Connection,
        tenant: UUID,
        product: str,
        actor: Attribution,
        gateway: UUID,
        resolved: list[dict[str, Any]],
        services: dict[str, CredentialReference],
        skipped: list[dict[str, str]],
        rotate: bool,
    ) -> dict[str, Any]:
        await conn.execute(
            "INSERT INTO public.mcp_binding_ensures(ensure_id,tenant_id,product,created_by,updated_by) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,product) DO NOTHING",
            (uuid7(), tenant, product, actor.subject, actor.subject),
        )
        cur = await conn.execute(
            "SELECT * FROM public.mcp_binding_ensures WHERE tenant_id=%s AND product=%s FOR UPDATE",
            (tenant, product),
        )
        row = await cur.fetchone()
        if row is None:
            raise CatalogConflictError("binding ensure unavailable")
        binding = None
        if row["binding_id"] is not None:
            binding = await get_binding(conn, tenant, row["binding_id"], lock=True)
        valid = (
            binding is not None
            and not binding["revoked"]
            and binding["gateway_license_id"] == gateway
        )
        token: str | None = None
        status = "unchanged" if valid else "pending"
        profile_id = row["credential_profile_id"]
        toolkits = [
            {
                "name": t["name"],
                "tenant_id": str(t["tenant_id"]),
                "toolkit_id": str(t["toolkit_id"]),
                "version": t["version"],
            }
            for t in resolved
        ]
        if resolved:
            profile_id = await self.write_profile(
                conn, tenant, product, actor, gateway, services, profile_id
            )
            ttl = timedelta(days=self.settings.binding_ensure_ttl_days)
            now = datetime.now(UTC)
            spec = BindingSpec(
                credential_profile_id=profile_id,
                toolkits=tuple(
                    sorted(
                        (
                            ToolkitReference(
                                tenant_id=t["tenant_id"],
                                id=t["toolkit_id"],
                                version=t["version"],
                            )
                            for t in resolved
                        ),
                        key=lambda r: (str(r.tenant_id), str(r.id), r.version),
                    )
                ),
                context=BoundContext(tenant_id=tenant),
                # Ceiling only: each tool's own manifest limits are the binding constraint.
                limits=MAX_LIMITS,
                expires_at=now + ttl,
            )
            if binding is not None and not binding["revoked"] and not valid:
                # The tenant's mcp licence changed: the old binding can never authenticate again.
                await self.access.act_binding(
                    conn, tenant, actor, binding["binding_id"], binding["revision"], revoke=True
                )
            if not valid:
                created, token = await self.access.write_binding(conn, tenant, actor, spec)
                binding_id, status = created.binding_id, "created"
            else:
                binding_id = row["binding_id"]
                current = parse(BindingSpec, binding["spec"]) if binding else spec
                if (
                    current.toolkits != spec.toolkits
                    or current.credential_profile_id != spec.credential_profile_id
                    or current.limits != spec.limits
                    or not current.enabled
                    or current.expires_at - now < ttl / 2
                ):
                    await self.access.write_binding(
                        conn, tenant, actor, spec, binding_id, binding["revision"] if binding else 0
                    )
                    status = "updated"
        else:
            binding_id = row["binding_id"] if valid else None
        if rotate and binding_id is not None and token is None:
            current_row = await get_binding(conn, tenant, binding_id, lock=True)
            _, token = await self.access.act_binding(
                conn, tenant, actor, binding_id, current_row["revision"]
            )
            status = "rotated"
        issued_at = (
            datetime.now(UTC)
            if token is not None
            else (row["token_issued_at"] if binding_id == row["binding_id"] else None)
        )
        await conn.execute(
            "UPDATE public.mcp_binding_ensures SET revision=revision+1,gateway_license_id=%s,"
            "credential_profile_id=%s,binding_id=%s,toolkits=%s,skipped=%s,token_issued_at=%s,"
            "updated_by=%s,updated_at=clock_timestamp() WHERE ensure_id=%s",
            (
                gateway if binding_id is not None else row["gateway_license_id"],
                profile_id,
                binding_id,
                Jsonb(toolkits),
                Jsonb(skipped),
                issued_at,
                actor.subject,
                row["ensure_id"],
            ),
        )
        await audit(conn, tenant, actor, row["ensure_id"], row["revision"] + 1, "binding.ensured")
        final = await get_binding(conn, tenant, binding_id) if binding_id is not None else None
        result: dict[str, Any] = {
            "ensure_id": str(row["ensure_id"]),
            "tenant_id": str(tenant),
            "product": product,
            "status": status,
            "binding_id": str(binding_id) if binding_id is not None else None,
            "binding_revision": final["revision"] if final else None,
            "gateway_license_id": str(gateway),
            "expires_at": final["expires_at"].isoformat() if final else None,
            "toolkits": toolkits,
            "skipped": skipped,
            "token_already_issued": token is None and binding_id is not None,
        }
        if token is not None:
            result["token"] = token
        return result

    async def write_profile(
        self,
        conn: Connection,
        tenant: UUID,
        product: str,
        actor: Attribution,
        gateway: UUID,
        services: dict[str, CredentialReference],
        profile_id: UUID | None,
    ) -> UUID:
        app = self.settings.license_app_key
        if not app:
            raise AuthorityUnavailableError("license authentication is not configured")
        desired = CredentialProfile(
            profile_id=profile_id or uuid7(),
            tenant_id=tenant,
            name=profile_name(product),
            gateway=CredentialReference(
                service=GATEWAY_SERVICE, alias=GATEWAY_SERVICE, license_id=gateway, app_key=app
            ),
            services=tuple(services[k] for k in sorted(services)),
        )
        if profile_id is None:
            await self.access.write_profile(conn, tenant, actor, desired)
            return desired.profile_id
        row = await self.access.profile_row(conn, tenant, profile_id, lock=True)
        if parse(CredentialProfile, row["manifest"]) != desired:
            await self.access.write_profile(conn, tenant, actor, desired, row["revision"])
        return profile_id
