"""Shared REST/MCP admission and live execution authority."""

import asyncio
import re
from datetime import UTC, datetime
from uuid import uuid7

from src.auth.context import call_actor
from src.auth.principal import Principal
from src.catalog.models import Attribution, AuthAdapter, ToolLimits
from src.catalog.resolver import ResolvedTool
from src.catalog.submission import SubmissionError
from src.catalog.validation import validate_instance
from src.execution.models import (
    TERMINAL,
    ConfirmationRequiredError,
    ExecutionDeniedError,
    draft_view,
    envelope,
)
from src.storage.bindings import parse
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError
from src.toolkits.models import (
    BoundContext,
    CredentialProfile,
    CredentialReference,
    EffectiveTool,
    ToolReference,
)
from src.toolkits.resolver import narrow_limits

POLL_RETRIES = 3
DRAFT_STATUSES = frozenset({"draft", "published"})


def resolved_snapshot(row):
    authority = row["authority"]
    return ResolvedTool(authority["manifest"], authority["effective"], authority["context"])


class ExecutionService:
    def __init__(self, repository, resolver, minter=None):
        self.repository, self.resolver = repository, resolver
        self.settings = repository.settings
        #: Licence minter, only needed for drafts that call licence-kind platform services.
        self.minter = minter

    async def submit(self, principal, tool, inputs, idempotency_key):
        if not self.settings.execution_enabled:
            raise SubmissionError()
        if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", idempotency_key
        ):
            raise ValueError("invalid idempotency key")
        validate_instance(tool.manifest.input_schema, inputs, self.settings)
        before = await self.resolver.repository.generation()
        current = next(
            (
                t
                for t in await self.resolver.resolve(principal)
                if t.effective.tool == tool.effective.tool
            ),
            None,
        )
        if current != tool:
            raise CatalogConflictError("authority changed")
        row = await self.repository.accept(
            principal, current, inputs, idempotency_key, before, call_actor.get()
        )
        # Durable acceptance precedes waiting; request cancellation never cancels a run.
        run_id = row["run_id"]
        if tool.manifest.runtime != "kata_python":
            # External runs are driven out of band and never finish inside the sync window.
            return envelope(row)
        row = await self._wait(row, lambda: self.repository.get(principal.tenant_id, run_id))
        return envelope(row)

    async def _wait(self, row, fetch):
        """Poll an ACCEPTED run until terminal or the sync wait ends. A transient read error must
        not deny a run that is already durable: after a few backed-off retries return the last
        known (non-terminal) row so the client polls the run instead."""
        loop = asyncio.get_running_loop()
        stop = loop.time() + self.settings.sync_wait_seconds
        failures = 0
        while row["status"] not in TERMINAL and loop.time() < stop:
            await asyncio.sleep(0.05 * 2**failures)
            try:
                row = await fetch()
                failures = 0
            except Exception:
                failures += 1
                if failures > POLL_RETRIES:
                    break
        return row

    async def inspect(self, principal, identity):
        row = await self.repository.get(principal.tenant_id, identity)
        if not principal.management:
            if (
                principal.binding_id != row["binding_id"]
                or principal.license_id != row["gateway_license_id"]
            ):
                raise PermissionError("run ownership required")
            await self.resolver.resolve(principal)
        return row

    async def submit_draft(
        self,
        author_tenant,
        license_id,
        tool_id,
        version,
        inputs,
        confirm_writes=False,
        idempotency_key=None,
    ):
        """Run the author's own draft or published tool version without a binding.

        The caller proved ``license_id`` as ``author_tenant``'s tenant-level mcp licence; it is
        the run's gateway. The profile is ephemeral (snapshotted on the run): the gateway plus,
        for licence-kind platform services, the minter's licence for that service. Tools of other
        tenants are not found. A tool that writes needs ``confirm_writes``."""
        if not self.settings.execution_enabled:
            raise SubmissionError()
        if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", idempotency_key
        ):
            raise ValueError("invalid idempotency key")
        catalog = self.resolver.repository
        before = await catalog.generation()
        descriptor = await catalog.get_version(author_tenant, tool_id, version)
        manifest = descriptor.manifest
        if descriptor.status not in DRAFT_STATUSES or await self._retired(author_tenant, tool_id):
            raise CatalogConflictError("tool version is not runnable")
        if (
            manifest.runtime == "kata_python"
            and manifest.runner_image_digest not in self.settings.approved_runner_images
        ):
            raise CatalogConflictError("tool runner image is not approved")
        services, writes = await self._draft_dependencies(author_tenant, manifest)
        if (manifest.effect == "writes" or writes) and not confirm_writes:
            raise ConfirmationRequiredError("this tool writes; confirm_writes is required")
        validate_instance(manifest.input_schema, inputs, self.settings)
        profile = CredentialProfile(
            profile_id=uuid7(),
            tenant_id=author_tenant,
            name="draft_run",
            gateway=CredentialReference(
                service="gateway",
                alias="gateway",
                license_id=license_id,
                app_key=self.settings.license_app_key or "mcp",
            ),
            services=await self._licence_references(author_tenant, services),
        )
        ceiling = ToolLimits(
            wall_seconds=min(300, self.settings.run_max_seconds),
            queue_seconds=min(600, self.settings.run_queue_timeout_seconds),
            api_calls=self.settings.max_api_calls_per_run,
        )
        resolved = ResolvedTool(
            manifest.model_dump_json(),
            EffectiveTool(
                tool=ToolReference(id=tool_id, version=version, tenant_id=author_tenant),
                limits=narrow_limits(manifest.limits, ceiling),
                public_name=manifest.public_name,
            ).model_dump_json(),
            BoundContext(tenant_id=author_tenant).model_dump_json(),
        )
        row = await self.repository.accept_draft(
            author_tenant,
            license_id,
            resolved,
            profile,
            inputs,
            idempotency_key,
            before,
            Attribution(subject=f"tenant:{author_tenant}", license_id=license_id),
        )
        run_id = row["run_id"]
        row = await self._wait(row, lambda: self.repository.get_draft(author_tenant, run_id))
        return draft_view(row, await self.repository.trace(author_tenant, row["run_id"]))

    async def draft_run(self, author_tenant, run_id):
        """The author's test run: envelope plus the upstream call trace."""
        row = await self.repository.get_draft(author_tenant, run_id)
        return draft_view(row, await self.repository.trace(author_tenant, run_id))

    async def _retired(self, tenant, tool_id):
        async with self.repository.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT retired FROM public.mcp_tools WHERE tenant_id=%s AND tool_id=%s",
                (tenant, tool_id),
            )
            row = await cur.fetchone()
        return row is None or row["retired"]

    async def _draft_dependencies(self, tenant, manifest):
        """Every operation and service the tool names must be usable by THIS tenant now: its own
        draft or published ones, or published platform services it may use. Returns the services
        and whether any operation writes."""
        catalog = self.resolver.repository
        services, writes = {}, False
        for reference in manifest.allowed_operations:
            operation, status = await catalog.operation(reference)
            service, service_status = await catalog.service(operation.service)
            owner = await catalog.service_owner(operation.service.id)
            if owner != tenant:
                allowed = (status, service_status) == ("published", "published")
            else:
                allowed = status in DRAFT_STATUSES and service_status in DRAFT_STATUSES
            if not allowed or not await catalog.may_use_service(tenant, operation.service):
                raise CatalogConflictError("tool depends on an unavailable operation or service")
            writes = writes or operation.effect == "writes"
            services[service.service_key] = service
        return services, writes

    async def _licence_references(self, tenant, services):
        """Minted licence references for platform services whose source is licence kind."""
        result = []
        for service in services.values():
            if service.auth_adapter is AuthAdapter.SOURCE:
                continue
            async with self.repository.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT auth->>'kind' AS kind FROM public.mcp_sources WHERE owner_tenant_id=%s AND source_key=%s",
                    (self.settings.platform_tenant_id, service.destination_alias),
                )
                source = await cur.fetchone()
            if source is None or source["kind"] != "licence":
                continue  # a stored token: there is no tenant licence to verify
            if self.minter is None or service.license_app_key is None:
                raise ExecutionDeniedError()
            licence = await self.minter.licence_id(tenant, service.license_app_key, None)
            result.append(
                CredentialReference(
                    service=service.service_key,
                    alias=service.service_key,
                    license_id=licence,
                    app_key=service.license_app_key,
                )
            )
        return tuple(result)

    async def live_draft(self, row):
        """Draft analogue of ``live``: fence, then the author's CURRENT authority. The tool
        version must be unchanged, still runnable and still owned by the run's tenant, and every
        dependency must still be usable by it. There is no binding or stored profile to check."""
        async with self.repository.pool.connection() as conn:
            await self.repository.owned(conn, row["run_id"], row["worker_id"], row["fence"])
        tenant, original = row["tenant_id"], resolved_snapshot(row)
        if row["owner_tenant_id"] != tenant:
            raise ExecutionDeniedError()
        try:
            descriptor = await self.resolver.repository.get_version(
                tenant, row["tool_id"], row["tool_version"]
            )
            if (
                descriptor.status not in DRAFT_STATUSES
                or await self._retired(tenant, row["tool_id"])
                or descriptor.manifest.model_dump_json() != original.manifest_json
            ):
                raise ExecutionDeniedError()
            await self._draft_dependencies(tenant, descriptor.manifest)
        except CatalogNotFoundError, CatalogConflictError:
            raise ExecutionDeniedError() from None
        # A later narrowing of the time allowance takes effect immediately too.
        if (
            row["started_at"]
            and (datetime.now(UTC) - row["started_at"]).total_seconds()
            > original.effective.limits.wall_seconds
        ):
            raise ExecutionDeniedError()
        return original.effective, parse(CredentialProfile, row["profile_snapshot"])

    async def live(self, row):
        """Return the intersection of snapshotted and current authority.

        Context/profile changes invalidate existing work; widening never replaces
        snapshotted restrictions. Tokens never enter these snapshots.
        """
        if row.get("mode") == "draft":
            return await self.live_draft(row)
        async with self.repository.pool.connection() as conn:
            await self.repository.owned(conn, row["run_id"], row["worker_id"], row["fence"])
        binding = await self.resolver.access.repository.inspect(row["tenant_id"], row["binding_id"])
        if (
            binding.spec.context.model_dump_json() != row["authority"]["context"]
            or binding.spec.credential_profile_id != row["credential_profile_id"]
        ):
            raise ExecutionDeniedError()
        principal = Principal(
            row["tenant_id"],
            row["gateway_license_id"],
            "execution-worker",
            row["binding_id"],
            binding.revision,
        )
        current = next(
            (
                t
                for t in await self.resolver.resolve(principal)
                if t.effective.tool == resolved_snapshot(row).effective.tool
            ),
            None,
        )
        original = resolved_snapshot(row)
        if current is None or current.manifest_json != original.manifest_json:
            raise ExecutionDeniedError()
        async with self.repository.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT manifest FROM public.mcp_credential_profiles WHERE tenant_id=%s AND profile_id=%s",
                (row["tenant_id"], row["credential_profile_id"]),
            )
            profile_row = await cur.fetchone()
        if not profile_row or profile_row["manifest"] != row["profile_snapshot"]:
            raise ExecutionDeniedError()
        effective = original.effective.model_copy(
            update={
                "operation_constraints": original.effective.operation_constraints
                + current.effective.operation_constraints,
                "limits": narrow_limits(original.effective.limits, current.effective.limits),
            }
        )
        # A later narrowing of the time allowance takes effect immediately too.
        if (
            row["started_at"]
            and (datetime.now(UTC) - row["started_at"]).total_seconds()
            > effective.limits.wall_seconds
        ):
            raise ExecutionDeniedError()
        return effective, parse(CredentialProfile, profile_row["manifest"])
