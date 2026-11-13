"""One live exact-selection resolver. No executable packages are imported or loaded."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from src.catalog.models import (
    OperationConstraints,
    OperationManifest,
    OwnershipRead,
    SourcePointer,
    ToolLimits,
    ToolManifest,
)
from src.catalog.validation import validate_instance
from src.config import Settings
from src.storage.bindings import Connection, get_toolkit, parse
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError
from src.toolkits.constraints import check_rules, pointer
from src.toolkits.models import (
    BindingSpec,
    BoundContext,
    EffectiveTool,
    ToolkitManifest,
    ToolReference,
    ToolSelection,
)


def reference_key(ref: ToolReference) -> tuple[UUID, UUID, int]:
    return ref.tenant_id, ref.id, ref.version


def narrow_limits(*limits: ToolLimits) -> ToolLimits:
    return ToolLimits(
        **{key: min(getattr(item, key) for item in limits) for key in ToolLimits.model_fields}
    )


def constrain(manifest: ToolManifest, rules: tuple[OperationConstraints, ...]) -> None:
    allowed = {(r.id, r.version) for r in manifest.allowed_operations}
    if any((r.operation.id, r.operation.version) not in allowed for r in rules):
        raise ValueError("constraint targets an undeclared operation")


def combine(tools: list[EffectiveTool]) -> tuple[EffectiveTool, ...]:
    result: dict[str, EffectiveTool] = {}
    for item in tools:
        prior = result.get(item.public_name)
        if prior:
            if prior.tool != item.tool:
                raise CatalogConflictError("public name collision in effective selection")
            # Restrictions are conjunctions, including repeated inclusions.
            item = item.model_copy(
                update={
                    "operation_constraints": prior.operation_constraints
                    + item.operation_constraints,
                    "limits": narrow_limits(prior.limits, item.limits),
                }
            )
        result[item.public_name] = EffectiveTool.model_validate_json(item.model_dump_json())
    if len(result) > 128:
        raise ValueError("effective selection exceeds 128 tools")
    return tuple(result[key] for key in sorted(result))


@dataclass
class SelectionResolver:
    settings: Settings

    async def ownership_reads(
        self, conn: Connection, entries: tuple[OperationConstraints, ...]
    ) -> None:
        for entry in entries:
            for rule in entry.rules:
                if isinstance(rule, OwnershipRead):
                    cur = await conn.execute(
                        "SELECT o.manifest,o.status,s.status AS service_status FROM public.mcp_operation_versions o JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version) WHERE o.operation_id=%s AND o.version=%s",
                        (rule.operation.id, rule.operation.version),
                    )
                    row = await cur.fetchone()
                    if (
                        row is None
                        or row["status"] != "published"
                        or row["service_status"] != "published"
                    ):
                        raise ValueError("ownership read is not approved")
                    operation = parse(OperationManifest, row["manifest"])
                    if operation.effect != "read_only" or any(
                        isinstance(r, OwnershipRead) for r in operation.scope_rules
                    ):
                        raise ValueError("ownership reads require nonrecursive read operations")

    async def tool(
        self,
        conn: Connection,
        tenant: UUID,
        ref: ToolReference,
        *,
        lock: bool = False,
        bound: bool = False,
    ) -> ToolManifest:
        """A selectable tool: the tenant's own, or shared with it.

        ``bound`` is for tools of an already resolved binding selection only: a platform-owned
        tool reached through a pinned platform toolkit is then read as the platform tenant, its
        owner. Toolkit membership was proven by the toolkit resolution that produced the
        selection, so this never widens what a tenant's own toolkit may contain.
        """
        if bound and ref.tenant_id == self.settings.platform_tenant_id:
            tenant = ref.tenant_id
        cur = await conn.execute(
            "SELECT v.manifest,v.status,t.retired FROM public.mcp_tool_versions v JOIN public.mcp_tools t USING(tenant_id,tool_id) "
            "WHERE v.tenant_id=%s AND v.tool_id=%s AND v.version=%s AND "
            "(v.tenant_id=%s OR EXISTS(SELECT 1 FROM public.mcp_tool_shares s WHERE "
            "s.owner_tenant_id=v.tenant_id AND s.tool_id=v.tool_id AND s.grantee_tenant_id=%s))",
            (*reference_key(ref), tenant, tenant),
        )
        row = await cur.fetchone()
        if row is None:
            raise CatalogNotFoundError("selected tool not found")
        if row["status"] != "published" or row["retired"]:
            raise CatalogConflictError("selected tool is unavailable")
        if lock:
            await conn.execute(
                "SELECT public.mcp_lock_access_tool(%s,%s,%s,%s)", (tenant, *reference_key(ref))
            )
        cur = await conn.execute(
            "SELECT o.status AS op,s.status AS service FROM public.mcp_tool_version_operations r "
            "JOIN public.mcp_operation_versions o ON (o.operation_id,o.version)=(r.operation_id,r.operation_version) "
            "JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version) "
            "WHERE r.tenant_id=%s AND r.tool_id=%s AND r.version=%s",
            reference_key(ref),
        )
        if any(r["op"] != "published" or r["service"] != "published" for r in await cur.fetchall()):
            raise CatalogConflictError("selected operation is unavailable")
        return parse(ToolManifest, row["manifest"])

    async def tools_bulk(
        self,
        conn: Connection,
        tenant: UUID,
        refs: Sequence[ToolReference],
        *,
        lock: bool = False,
        bound: bool = False,
    ) -> dict[ToolReference, ToolManifest]:
        """``tool`` for many references in two statements instead of two per tool.

        Same rules as ``tool``: the row must belong to ``ref.tenant_id`` and be visible to
        ``tenant`` (its own, or shared with it), published and not retired, with every
        operation and service published. ``bound`` reads platform-owned references as the
        platform tenant, their owner.
        """
        if not refs:
            return {}
        # ``tool`` reads the owner's row and admits the reader only when the row is that
        # reader's own or explicitly shared with it.
        readers = [
            ref.tenant_id if bound and ref.tenant_id == self.settings.platform_tenant_id else tenant
            for ref in refs
        ]
        cur = await conn.execute(
            "WITH want(owner,tool_id,version,reader) AS "
            "(SELECT * FROM unnest(%s::uuid[],%s::uuid[],%s::int[],%s::uuid[])) "
            "SELECT w.owner,w.tool_id,w.version,v.manifest,v.status,t.retired "
            "FROM want w JOIN public.mcp_tool_versions v "
            "ON (v.tenant_id,v.tool_id,v.version)=(w.owner,w.tool_id,w.version) "
            "JOIN public.mcp_tools t ON (t.tenant_id,t.tool_id)=(v.tenant_id,v.tool_id) "
            "WHERE w.owner=w.reader OR EXISTS(SELECT 1 FROM public.mcp_tool_shares s "
            "WHERE s.owner_tenant_id=w.owner AND s.tool_id=w.tool_id "
            "AND s.grantee_tenant_id=w.reader)",
            (
                [ref.tenant_id for ref in refs],
                [ref.id for ref in refs],
                [ref.version for ref in refs],
                readers,
            ),
        )
        found: dict[tuple[UUID, UUID, int], ToolManifest] = {}
        for row in await cur.fetchall():
            if row["status"] != "published" or row["retired"]:
                raise CatalogConflictError("selected tool is unavailable")
            found[(row["owner"], row["tool_id"], row["version"])] = parse(
                ToolManifest, row["manifest"]
            )
        manifests: dict[ToolReference, ToolManifest] = {}
        for ref in refs:
            manifest = found.get(reference_key(ref))
            if manifest is None:
                raise CatalogNotFoundError("selected tool not found")
            manifests[ref] = manifest
        if lock:
            # Advisory locks stay per reference and in reference order: they serialize
            # concurrent edits, so this is the write path, where a single extra round trip
            # per tool buys exactly the lock semantics ``tool`` has.
            for ref, reader in zip(refs, readers, strict=True):
                await conn.execute(
                    "SELECT public.mcp_lock_access_tool(%s,%s,%s,%s)",
                    (reader, *reference_key(ref)),
                )
        cur = await conn.execute(
            "SELECT r.tenant_id,r.tool_id,r.version,"
            "bool_and(o.status='published' AND s.status='published') AS ok "
            "FROM public.mcp_tool_version_operations r "
            "JOIN public.mcp_operation_versions o "
            "ON (o.operation_id,o.version)=(r.operation_id,r.operation_version) "
            "JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version) "
            "WHERE (r.tenant_id,r.tool_id,r.version) IN "
            "(SELECT * FROM unnest(%s::uuid[],%s::uuid[],%s::int[])) "
            "GROUP BY 1,2,3",
            (
                [ref.tenant_id for ref in refs],
                [ref.id for ref in refs],
                [ref.version for ref in refs],
            ),
        )
        if any(not row["ok"] for row in await cur.fetchall()):
            raise CatalogConflictError("selected operation is unavailable")
        return manifests

    async def selection(
        self,
        conn: Connection,
        tenant: UUID,
        entry: ToolSelection,
        *,
        lock: bool = False,
        manifest: ToolManifest | None = None,
    ) -> EffectiveTool:
        manifest = manifest or await self.tool(conn, tenant, entry.tool, lock=lock)
        constrain(manifest, entry.operation_constraints)
        await self.ownership_reads(conn, entry.operation_constraints)
        return EffectiveTool(
            tool=entry.tool,
            public_name=manifest.public_name,
            operation_constraints=manifest.operation_constraints + entry.operation_constraints,
            limits=narrow_limits(manifest.limits, entry.limits),
        )

    async def toolkit(
        self,
        conn: Connection,
        tenant: UUID,
        identity: UUID,
        version: int,
        *,
        seen: frozenset[tuple[UUID, int]] = frozenset(),
        lock: bool = False,
        context: BoundContext | None = None,
        budget: list[int] | None = None,
    ) -> tuple[EffectiveTool, ...]:
        key = identity, version
        budget = budget if budget is not None else [0]
        budget[0] += 1
        if key in seen or len(seen) >= 16 or budget[0] > 256:
            raise ValueError("toolkit cycle or depth limit")
        if lock:
            await conn.execute(
                "SELECT 1 FROM public.mcp_toolkit_versions WHERE tenant_id=%s AND toolkit_id=%s AND version=%s FOR SHARE",
                (tenant, identity, version),
            )
        descriptor = await get_toolkit(conn, tenant, identity, version)
        if descriptor.status != "published":
            raise CatalogConflictError("toolkit is unavailable")
        if context is not None:
            for name, value in descriptor.manifest.applicability.model_dump(
                exclude_none=True
            ).items():
                # Capability describes content; all context applicability fields restrict selection.
                if name != "capability" and getattr(context, name) != value:
                    raise ValueError("binding context does not satisfy toolkit applicability")
        # Recheck every inclusion live, so disabling an ancestor reaches all bindings.
        for ref in descriptor.manifest.includes:
            await self.toolkit(
                conn,
                tenant,
                ref.id,
                ref.version,
                seen=seen | {key},
                lock=lock,
                context=context,
                budget=budget,
            )
        await self.tools_bulk(conn, tenant, [entry.tool for entry in descriptor.tools], lock=lock)
        return descriptor.tools

    async def flatten(
        self, conn: Connection, manifest: ToolkitManifest
    ) -> tuple[EffectiveTool, ...]:
        loaded = await self.tools_bulk(
            conn, manifest.tenant_id, [t.tool for t in manifest.tools], lock=True
        )
        tools = [
            await self.selection(
                conn, manifest.tenant_id, t, lock=True, manifest=loaded.get(t.tool)
            )
            for t in manifest.tools
        ]
        budget = [0]
        for ref in manifest.includes:
            tools.extend(
                await self.toolkit(
                    conn, manifest.tenant_id, ref.id, ref.version, lock=True, budget=budget
                )
            )
        return combine(tools)

    async def binding(
        self, conn: Connection, tenant: UUID, spec: BindingSpec, *, lock: bool = False
    ) -> tuple[EffectiveTool, ...]:
        if spec.context.tenant_id != tenant:
            raise ValueError("bound tenant differs from authenticated tenant")
        await self.ownership_reads(conn, spec.operation_constraints)
        tools: list[EffectiveTool] = []
        budget = [0]
        for ref in spec.toolkits:
            # A binding reads its own tenant's toolkits and the platform's, nothing else.
            owner = ref.tenant_id or tenant
            if owner not in (tenant, self.settings.platform_tenant_id):
                raise CatalogNotFoundError("toolkit version not found")
            tools.extend(
                await self.toolkit(
                    conn,
                    owner,
                    ref.id,
                    ref.version,
                    lock=lock,
                    context=spec.context,
                    budget=budget,
                )
            )
        flattened = combine(tools)
        if spec.tool_subset is not None:
            selected = {reference_key(r) for r in spec.tool_subset}
            if not selected <= {reference_key(t.tool) for t in flattened}:
                raise ValueError("subset contains tools outside toolkit membership")
            flattened = tuple(t for t in flattened if reference_key(t.tool) in selected)
        permitted_ops: set[tuple[UUID, int]] = set()
        resolved = []
        context = spec.context.public_values()
        operations: set[str] = set()
        loaded = await self.tools_bulk(
            conn, tenant, [entry.tool for entry in flattened], lock=lock, bound=True
        )
        # One read for every operation behind the selection, not one read per operation.
        wanted: dict[tuple[UUID, int], None] = {}
        for entry in flattened:
            for allowed_op in loaded[entry.tool].allowed_operations:
                wanted.setdefault((allowed_op.id, allowed_op.version), None)
        operation_manifests: dict[tuple[UUID, int], OperationManifest] = {}
        if wanted:
            cur = await conn.execute(
                "SELECT operation_id,version,manifest FROM public.mcp_operation_versions "
                "WHERE (operation_id,version) IN (SELECT * FROM unnest(%s::uuid[],%s::int[]))",
                ([key[0] for key in wanted], [key[1] for key in wanted]),
            )
            for row in await cur.fetchall():
                operation_manifests[(row["operation_id"], row["version"])] = parse(
                    OperationManifest, row["manifest"]
                )
        for entry in flattened:
            manifest = loaded[entry.tool]
            # The agent runner maps canonical operations to tools one to one.
            if manifest.operation in operations:
                raise CatalogConflictError("operation collision in effective selection")
            operations.add(manifest.operation)

            from src.catalog.public import validate_public_input

            validate_public_input(manifest.input_schema)
            # Tool context may accept only its declared projection; tenant remains host authority.
            properties = manifest.context_schema.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError("context schema must declare properties")
            projected = {key: value for key, value in context.items() if key in properties}
            validate_instance(manifest.context_schema, projected, self.settings)
            allowed = {(r.id, r.version) for r in manifest.allowed_operations}
            permitted_ops |= allowed
            extra = tuple(
                r
                for r in spec.operation_constraints
                if (r.operation.id, r.operation.version) in allowed
            )
            for op in manifest.allowed_operations:
                operation = operation_manifests.get((op.id, op.version))
                assert operation is not None
                for mapping in operation.mappings:
                    if (
                        isinstance(mapping.value, SourcePointer)
                        and mapping.value.source == "context"
                    ):
                        pointer(context, mapping.value.pointer)
                rules = operation.scope_rules + tuple(
                    rule
                    for entry_rules in entry.operation_constraints + extra
                    if entry_rules.operation == op
                    for rule in entry_rules.rules
                )
                check_rules(rules, context)
            resolved.append(
                entry.model_copy(
                    update={
                        "operation_constraints": entry.operation_constraints + extra,
                        "limits": narrow_limits(entry.limits, spec.limits),
                    }
                )
            )
        if any(
            (r.operation.id, r.operation.version) not in permitted_ops
            for r in spec.operation_constraints
        ):
            raise ValueError("binding constraint targets an unselected operation")
        return tuple(resolved)
