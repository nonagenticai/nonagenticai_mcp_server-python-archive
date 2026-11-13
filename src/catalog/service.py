"""Strict complete-package validation and tenant lifecycle."""

from uuid import UUID

from src.catalog.models import (
    Attribution,
    ExactReference,
    OperationManifest,
    OwnershipRead,
    ServiceManifest,
    ToolManifest,
    VersionDescriptor,
)
from src.catalog.package import PackageFile, validate_package
from src.catalog.validation import validate_schema
from src.config import Settings
from src.storage.catalog import CatalogConflictError, CatalogRepository, PlatformCatalogRepository


class CatalogService:
    def __init__(self, repository: CatalogRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    async def validate(self, manifest: ToolManifest, files: tuple[PackageFile, ...]) -> None:
        # Revalidate even model_copy/model_construct input supplied by internal Python callers.
        manifest = ToolManifest.model_validate_json(manifest.model_dump_json())
        from src.catalog.public import RESERVED_NAMES, output_envelope, validate_public_input

        for schema in (manifest.input_schema, manifest.result_schema, manifest.context_schema):
            validate_schema(schema, self.settings)
        if manifest.public_name in RESERVED_NAMES:
            raise ValueError("reserved platform tool name")
        validate_public_input(manifest.input_schema)
        validate_schema(output_envelope(manifest.result_schema), self.settings)
        if manifest.runtime == "kata_python":
            assert manifest.entrypoint is not None
            validate_package(files, manifest.entrypoint, manifest.package_sha256, self.settings)
            if manifest.runner_image_digest not in self.settings.approved_runner_images:
                raise ValueError("runner image is not operator-approved")
        for reference in manifest.allowed_operations:
            operation, _ = await self.repository.operation(reference)
            await self.repository.service(operation.service)
            if manifest.effect == "read_only" and operation.effect != "read_only":
                raise ValueError("read-only tool cannot grant mutations")
        for entry in manifest.operation_constraints:
            await self._ownership_reads(entry.rules)

    async def _ownership_reads(self, rules: tuple) -> None:
        for rule in rules:
            if isinstance(rule, OwnershipRead):
                operation, state = await self.repository.operation(rule.operation)
                _, service_state = await self.repository.service(operation.service)
                if (
                    operation.effect != "read_only"
                    or state != "published"
                    or service_state != "published"
                ):
                    raise ValueError("ownership checks require an approved published read")
                if any(isinstance(r, OwnershipRead) for r in operation.scope_rules):
                    raise ValueError("recursive ownership checks are unsupported")

    async def create_draft(
        self,
        tenant_id: UUID,
        manifest: ToolManifest,
        files: tuple[PackageFile, ...],
        actor: Attribution,
    ) -> VersionDescriptor:
        await self.validate(manifest, files)
        return await self.repository.create(tenant_id, manifest, files, actor)

    async def append_draft(
        self,
        tenant_id: UUID,
        tool_id: UUID,
        content: ToolManifest,
        files: tuple[PackageFile, ...],
        actor: Attribution,
        *,
        expected_revision: int | None = None,
        exact_version: bool = False,
    ) -> VersionDescriptor:
        await self.validate(content, files)
        return await self.repository.append(
            tenant_id,
            tool_id,
            content,
            files,
            actor,
            expected_revision=expected_revision,
            exact_version=exact_version,
        )

    async def copy_version(
        self,
        tenant_id: UUID,
        tool_id: UUID,
        version: int,
        actor: Attribution,
        *,
        expected_revision: int,
    ) -> VersionDescriptor:
        original = await self.repository.get_version(tenant_id, tool_id, version)
        files = await self.repository.files(tenant_id, tool_id, version)
        await self.validate(original.manifest, files)
        return await self.repository.append(
            tenant_id,
            tool_id,
            original.manifest,
            files,
            actor,
            expected_revision=expected_revision,
            source=ExactReference(id=tool_id, version=version),
        )

    async def transition(
        self,
        tenant_id: UUID,
        tool_id: UUID,
        version: int,
        status: str,
        actor: Attribution,
        *,
        expected_revision: int,
    ) -> VersionDescriptor:
        if status == "published":
            original = await self.repository.get_version(tenant_id, tool_id, version)
            await self.validate(
                original.manifest, await self.repository.files(tenant_id, tool_id, version)
            )
        return await self.repository.transition(
            tenant_id, tool_id, version, status, actor, expected_revision=expected_revision
        )


class PlatformCatalogService:
    """Operator-only platform contract authoring; deliberately absent from ASGI state."""

    def __init__(self, repository: PlatformCatalogRepository, catalog: CatalogService) -> None:
        self.repository = repository
        self.catalog = catalog

    async def add_service(
        self, manifest: ServiceManifest, actor: Attribution, *, expected_revision: int | None = None
    ) -> None:
        manifest = ServiceManifest.model_validate_json(manifest.model_dump_json())
        await self.repository.add_service(manifest, actor, expected_revision=expected_revision)

    async def add_operation(self, manifest: OperationManifest, actor: Attribution) -> None:
        manifest = OperationManifest.model_validate_json(manifest.model_dump_json())
        validate_schema(manifest.input_schema, self.catalog.settings)
        validate_schema(manifest.output_schema, self.catalog.settings)
        service, _ = await self.catalog.repository.service(manifest.service)
        if manifest.max_response_bytes > service.max_response_bytes:
            raise CatalogConflictError("operation exceeds service response limit")
        await self.catalog._ownership_reads(manifest.scope_rules)
        await self.repository.add_operation(manifest, actor)
