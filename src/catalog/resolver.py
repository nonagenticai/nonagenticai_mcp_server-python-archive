"""One live public projection, with no cross-request authorization cache."""

from dataclasses import dataclass
from datetime import UTC, datetime

from src.auth.principal import Principal
from src.catalog.models import ToolManifest
from src.catalog.public import RESERVED_NAMES, output_envelope
from src.catalog.validation import validate_schema
from src.storage.catalog import CatalogConflictError, CatalogRepository
from src.toolkits.models import BoundContext, EffectiveTool
from src.toolkits.service import AccessService


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    # Immutable JSON snapshots avoid nested mutable dictionaries in frozen models.
    manifest_json: str
    effective_json: str
    context_json: str

    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest.model_validate_json(self.manifest_json)

    @property
    def effective(self) -> EffectiveTool:
        return EffectiveTool.model_validate_json(self.effective_json)

    @property
    def context(self) -> BoundContext:
        return BoundContext.model_validate_json(self.context_json)

    def public(self) -> dict:
        manifest = self.manifest
        return {
            "name": manifest.public_name,
            "description": manifest.description,
            "inputSchema": manifest.input_schema,
            "outputSchema": output_envelope(manifest.result_schema),
            "annotations": {
                "readOnlyHint": manifest.effect == "read_only",
                "destructiveHint": manifest.effect == "writes",
                "idempotentHint": False,
                "openWorldHint": bool(manifest.allowed_operations),
            },
            # The agent platform's canonical object.verb (tool names cannot carry dots).
            "_meta": {"operation": manifest.operation},
        }


class CatalogResolver:
    def __init__(self, access: AccessService) -> None:
        self.access = access
        self.repository = CatalogRepository(access.repository.pool)

    async def resolve(self, principal: Principal) -> tuple[ResolvedTool, ...]:
        if (
            principal.management
            or principal.binding_id is None
            or principal.binding_revision is None
        ):
            raise PermissionError("explicit binding authority required")
        # Generation checks surround all reads, including the durable binding. No
        # transaction or connection stays open across transport/dispatch waits.
        for _ in range(2):
            before = await self.repository.generation()
            binding = await self.access.repository.inspect(
                principal.tenant_id, principal.binding_id
            )
            if (
                binding.gateway_license_id != principal.license_id
                or binding.revision != principal.binding_revision
                or binding.revoked
                or not binding.spec.enabled
                or binding.spec.expires_at <= datetime.now(UTC)
            ):
                raise PermissionError("binding authority changed")
            effective = await self.access.effective(binding)
            result = []
            async with self.access.repository.pool.connection() as conn:
                manifests = await self.access.resolver.tools_bulk(
                    conn, principal.tenant_id, [entry.tool for entry in effective], bound=True
                )
                for entry in effective:
                    manifest = manifests[entry.tool]
                    if (
                        manifest.runtime == "kata_python"
                        and manifest.runner_image_digest
                        not in self.access.settings.approved_runner_images
                    ) or (manifest.public_name in RESERVED_NAMES):
                        raise PermissionError("tool is unavailable")
                    validate_schema(output_envelope(manifest.result_schema), self.access.settings)
                    result.append(
                        ResolvedTool(
                            manifest.model_dump_json(),
                            entry.model_dump_json(),
                            binding.spec.context.model_dump_json(),
                        )
                    )
            if before == await self.repository.generation():
                return tuple(sorted(result, key=lambda t: t.manifest.public_name))
        raise CatalogConflictError("catalog changed during resolution; retry")

    async def preview(self, operator: Principal, identity) -> tuple[ResolvedTool, ...]:
        if not operator.management:
            raise PermissionError("management authorization required")
        binding = await self.access.repository.inspect(operator.tenant_id, identity)
        return await self.resolve(
            Principal(
                operator.tenant_id,
                binding.gateway_license_id,
                operator.subject,
                identity,
                binding.revision,
            )
        )
