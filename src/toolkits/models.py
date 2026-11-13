"""Closed v1 toolkit, credential-reference and binding request contracts."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import UUID7, Field, JsonValue, model_validator

from src.catalog.models import (
    AppKey,
    AuthorityId,
    Contract,
    ExactReference,
    Name,
    OperationConstraints,
    PositiveVersion,
    ToolLimits,
)


class ToolReference(ExactReference):
    tenant_id: AuthorityId


class ToolkitReference(ExactReference):
    """A binding's exact toolkit pin. ``tenant_id`` is the toolkit's owner: the binding's tenant
    or the reserved platform tenant (first-party toolkits), never any other tenant. Omitted, it
    is the binding's tenant; stored specs always carry it explicitly."""

    tenant_id: AuthorityId | None = None


class ToolSelection(Contract):
    tool: ToolReference
    operation_constraints: Annotated[tuple[OperationConstraints, ...], Field(max_length=100)] = ()
    limits: ToolLimits = Field(default_factory=ToolLimits)


class Applicability(Contract):
    capability: Name | None = None
    project_phase: Name | None = None
    work_type: Name | None = None
    card_stage: Name | None = None
    responsibility: Name | None = None


class ToolkitManifest(Contract):
    schema_version: Literal[1] = 1
    toolkit_id: UUID7
    tenant_id: AuthorityId
    version: PositiveVersion
    name: Name
    description: Annotated[str, Field(min_length=1, max_length=4096)]
    tools: Annotated[tuple[ToolSelection, ...], Field(max_length=128)] = ()
    includes: Annotated[tuple[ExactReference, ...], Field(max_length=32)] = ()
    applicability: Applicability = Field(default_factory=Applicability)

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        tools = [(t.tool.tenant_id, t.tool.id, t.tool.version) for t in self.tools]
        includes = [(r.id, r.version) for r in self.includes]
        if len(tools) != len(set(tools)) or len(includes) != len(set(includes)):
            raise ValueError("duplicate exact reference")
        if any(r.id == self.toolkit_id for r in self.includes):
            raise ValueError("self inclusion is forbidden")
        if not tools and not includes:
            raise ValueError("a toolkit must select tools")
        return self


class EffectiveTool(ToolSelection):
    public_name: Name


class ToolkitDescriptor(Contract):
    manifest: ToolkitManifest
    revision: PositiveVersion
    status: Literal["draft", "published", "disabled", "retired"]
    tools: tuple[EffectiveTool, ...]


class CredentialReference(Contract):
    service: Name
    alias: Name
    license_id: AuthorityId
    app_key: AppKey
    #: licences bundle key the minter names explicitly (``bundle-token?bundle=``). bindings/ensure
    #: sets it from the binding's product (Settings.product_bundles); None lets licences choose.
    bundle: AppKey | None = None


class CredentialProfile(Contract):
    schema_version: Literal[1] = 1
    profile_id: UUID7
    tenant_id: AuthorityId
    name: Name
    gateway: CredentialReference
    services: Annotated[tuple[CredentialReference, ...], Field(max_length=128)] = ()

    @model_validator(mode="after")
    def unique_services(self) -> Self:
        keys = [s.service for s in self.services]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate service credential reference")
        return self


class BoundContext(Contract):
    tenant_id: AuthorityId
    project_id: UUID7 | None = None
    workspace_id: UUID7 | None = None
    project_phase: Name | None = None
    work_type: Name | None = None
    card_stage: Name | None = None
    responsibility: Name | None = None
    test_command: Annotated[str, Field(min_length=1, max_length=1024)] | None = None
    reviewed_commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40,64}$")] | None = None

    def public_values(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", exclude_none=True)


class BindingSpec(Contract):
    schema_version: Literal[1] = 1
    credential_profile_id: UUID7
    toolkits: Annotated[tuple[ToolkitReference, ...], Field(min_length=1, max_length=32)]
    tool_subset: Annotated[tuple[ToolReference, ...], Field(max_length=128)] | None = None
    context: BoundContext
    operation_constraints: Annotated[tuple[OperationConstraints, ...], Field(max_length=100)] = ()
    limits: ToolLimits = Field(default_factory=ToolLimits)
    expires_at: datetime
    enabled: bool = True

    @model_validator(mode="after")
    def unambiguous(self) -> Self:
        if self.expires_at.tzinfo is None:
            raise ValueError("binding expiry must be timezone aware")
        for refs in (self.toolkits, self.tool_subset or ()):
            keys = [r.model_dump_json() for r in refs]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate exact reference")
        return self


def owned_toolkits(spec: BindingSpec, tenant: UUID) -> BindingSpec:
    """The spec with every toolkit owner explicit (an omitted owner is the binding's tenant)."""
    toolkits = tuple(
        r if r.tenant_id is not None else r.model_copy(update={"tenant_id": tenant})
        for r in spec.toolkits
    )
    return BindingSpec.model_validate_json(
        spec.model_copy(update={"toolkits": toolkits}).model_dump_json()
    )


class BindingDescriptor(Contract):
    binding_id: UUID7
    tenant_id: AuthorityId
    gateway_license_id: AuthorityId
    revision: PositiveVersion
    revoked: bool
    spec: BindingSpec
    tools: tuple[EffectiveTool, ...]


class RevisionRequest(Contract):
    expected_revision: PositiveVersion


class BindingUpdate(RevisionRequest):
    spec: BindingSpec


class ProfileUpdate(RevisionRequest):
    profile: CredentialProfile


class ToolkitUpdate(RevisionRequest):
    manifest: ToolkitManifest
