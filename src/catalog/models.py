"""Schema version, artifact version and database revision are separate identities."""

import math
import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import RFC_4122, UUID

from pydantic import (
    UUID7,
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from src.catalog.package import canonical_path

PositiveVersion = Annotated[int, Field(gt=0, le=2147483647)]


def _authority_identity(value: UUID) -> UUID:
    if value.variant != RFC_4122 or value.version is None:
        raise ValueError("identity must be an RFC 9562 UUID")
    return value


#: Tenant and licence identities come from the licence authority, which mints them by SQL
#: migration as UUIDv4. Any RFC 9562 version is accepted; identities mcp mints stay UUID7.
AuthorityId = Annotated[UUID, AfterValidator(_authority_identity)]
#: Catalog names, MCP tool names included (inside LLM providers' ^[a-zA-Z0-9_-]{1,64}$).
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
#: The agent platform's canonical lowercase ``object.verb`` operation a tool implements. MCP
#: tools/list carries it as ``_meta.operation`` (tool names cannot contain dots). Migration 0008
#: enforces the same pattern on every stored tool version.
OPERATION_PATTERN = r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$"
Name = Annotated[str, Field(pattern=NAME_PATTERN)]
Operation = Annotated[str, Field(pattern=OPERATION_PATTERN)]
#: A licences application key (`strata-rag`, `care-bus`, `mcp`): hyphens are part of the key.
AppKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ImageDigest = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}$")]
Pointer = Annotated[str, Field(pattern=r"^(?:/(?:[^~/]|~[01])*)*$", max_length=512)]


class Contract(BaseModel):
    # hide_input_in_errors: a validation error never echoes the offending value (it may be a secret).
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def strict_schema_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class ExactReference(Contract):
    id: UUID7
    version: PositiveVersion


class ContextEquality(Contract):
    kind: Literal["context_equal"]
    argument: Pointer
    context: Pointer


class SetMembership(Contract):
    kind: Literal["set_membership"]
    argument: Pointer
    values: Annotated[tuple[JsonValue, ...], Field(min_length=1, max_length=64)]


class NumericBounds(Contract):
    kind: Literal["numeric_bounds"]
    argument: Pointer
    minimum: float
    maximum: float

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (
            not math.isfinite(self.minimum)
            or not math.isfinite(self.maximum)
            or self.minimum > self.maximum
        ):
            raise ValueError("invalid numeric bounds")
        return self


class RelativePath(Contract):
    kind: Literal["relative_path"]
    argument: Pointer
    root: str

    @field_validator("root")
    @classmethod
    def safe_root(cls, value: str) -> str:
        return canonical_path(value)


class OwnershipRead(Contract):
    kind: Literal["ownership_read"]
    argument: Pointer
    operation: ExactReference
    result: Pointer
    context: Pointer


Constraint = Annotated[
    ContextEquality | SetMembership | NumericBounds | RelativePath | OwnershipRead,
    Field(discriminator="kind"),
]


class SourcePointer(Contract):
    source: Literal["input", "context"]
    pointer: Pointer


class LiteralValue(Contract):
    source: Literal["literal"]
    value: JsonValue


class ArgumentMapping(Contract):
    location: Literal["path", "query", "header", "body"]
    target: Annotated[str, Field(max_length=512)]
    value: Annotated[SourcePointer | LiteralValue, Field(discriminator="source")]
    #: An absent optional input is omitted from the request instead of denying the call.
    #: Only for tool inputs, and never for a path parameter (the path would be incomplete).
    optional: bool = False

    @model_validator(mode="after")
    def optional_only_for_inputs(self) -> Self:
        if self.optional and (self.location == "path" or self.value.source != "input"):
            raise ValueError("only non-path input mappings can be optional")
        return self


class AuthAdapter(StrEnum):
    LICENSE_BEARER = "license_bearer"
    LICENSE_HEADER = "license_header"
    #: The credential is whatever the destination source stores (tenant-registered sources).
    SOURCE = "source"


class ServiceManifest(Contract):
    schema_version: Literal[1] = 1
    service_id: UUID7
    version: PositiveVersion
    service_key: Name
    destination_alias: Name
    #: Required for the licence adapters; optional (normally absent) when ``auth_adapter`` is
    #: ``source`` because the source row then owns the credential.
    license_app_key: AppKey | None = None
    auth_adapter: AuthAdapter
    credential_profile_key: Name
    max_request_bytes: Annotated[int, Field(gt=0, le=10485760)]
    max_response_bytes: Annotated[int, Field(gt=0, le=10485760)]

    @model_validator(mode="after")
    def app_key_for_licence_adapters(self) -> Self:
        if self.license_app_key is None and self.auth_adapter is not AuthAdapter.SOURCE:
            raise ValueError("licence adapters need license_app_key")
        return self


class OperationManifest(Contract):
    schema_version: Literal[1] = 1
    operation_id: UUID7
    version: PositiveVersion
    service: ExactReference
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path_template: Annotated[str, Field(min_length=1, max_length=1024)]
    query_fields: tuple[Name, ...] = ()
    header_fields: tuple[Name, ...] = ()
    mappings: tuple[ArgumentMapping, ...] = ()
    scope_rules: tuple[Constraint, ...] = ()
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    response_projection: Annotated[tuple[Pointer, ...], Field(min_length=1)]
    redact_fields: tuple[Pointer, ...] = ()
    effect: Literal["read_only", "writes"]
    idempotency: Literal["none", "downstream_key"] = "none"
    max_retries: Annotated[int, Field(ge=0, le=3)] = 0
    timeout_seconds: Annotated[int, Field(gt=0, le=300)]
    #: 4xx statuses the tool handles itself (e.g. 404, 409). The runner receives them as
    #: ``ApiStatusError``; any other non-2xx stays a denial. Never 401/403/407.
    expected_error_statuses: tuple[int, ...] = ()
    #: A successful response may legitimately carry no body (204, or an empty 2xx): the broker
    #: returns ``{}`` instead of denying an unparseable body.
    empty_response: bool = False
    pagination_limit: Annotated[int, Field(gt=0, le=1000)]
    max_response_bytes: Annotated[int, Field(gt=0, le=10485760)]

    @model_validator(mode="after")
    def fixed_transport(self) -> Self:
        path = self.path_template
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\\?#%\r\n"):
            raise ValueError("operation needs a fixed relative path template")
        if any(p in {".", "..", ""} for p in path[1:].split("/")):
            raise ValueError("noncanonical operation path")
        tokens = re.findall(r"\{([a-z][a-z0-9_]*)\}", path)
        stripped = re.sub(r"\{[a-z][a-z0-9_]*\}", "", path)
        if "{" in stripped or "}" in stripped or any(ord(c) < 32 for c in path):
            raise ValueError("invalid path template")
        mapped: set[tuple[str, str]] = set()
        for mapping in self.mappings:
            if mapping.location == "body":
                if not re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", mapping.target):
                    raise ValueError("body target must be a JSON Pointer")
            elif not mapping.target:
                raise ValueError("transport field target is required")
            key = mapping.location, mapping.target
            if key in mapped:
                raise ValueError("duplicate mapping target")
            mapped.add(key)
            if mapping.location == "query" and mapping.target not in self.query_fields:
                raise ValueError("undeclared query field")
            if mapping.location == "header" and mapping.target not in self.header_fields:
                raise ValueError("undeclared header field")
        if set(tokens) != {target for location, target in mapped if location == "path"}:
            raise ValueError("path mappings must cover exactly the template variables")
        body_targets = [target for location, target in mapped if location == "body"]
        if any(
            a != b and (a == "" or b.startswith(a + "/"))
            for a in body_targets
            for b in body_targets
        ):
            raise ValueError("overlapping body mapping targets")
        forbidden = {"authorization", "cookie", "host", "proxy_authorization", "x_tenant_id"}
        forbidden |= {
            "connection",
            "content_length",
            "transfer_encoding",
            "idempotency_key",
            "x_idempotency_key",
        }
        if forbidden.intersection(self.header_fields):
            raise ValueError("transport credentials cannot be mapped")
        if self.effect == "writes" and self.max_retries and self.idempotency != "downstream_key":
            raise ValueError("mutation retries require downstream idempotency")
        if self.method in {"PUT", "PATCH", "DELETE"} and self.effect != "writes":
            raise ValueError("mutating method must declare writes")
        statuses = self.expected_error_statuses
        if (
            len(statuses) > 16
            or len(set(statuses)) != len(statuses)
            or any(
                type(s) is not int or not 400 <= s <= 499 or s in {401, 403, 407} for s in statuses
            )
        ):
            raise ValueError("expected error statuses must be unique 4xx other than 401/403/407")
        return self


class ToolLimits(Contract):
    wall_seconds: Annotated[int, Field(gt=0, le=300)] = 60
    queue_seconds: Annotated[int, Field(gt=0, le=600)] = 60
    memory_mi: Annotated[int, Field(gt=0, le=1024)] = 256
    cpu_millicores: Annotated[int, Field(gt=0, le=1000)] = 250
    api_calls: Annotated[int, Field(ge=0, le=100)] = 10
    frame_bytes: Annotated[int, Field(gt=0, le=1048576)] = 65536
    output_bytes: Annotated[int, Field(gt=0, le=1048576)] = 262144

    @model_validator(mode="after")
    def frame_fits(self) -> Self:
        if self.frame_bytes > self.output_bytes:
            raise ValueError("frame limit exceeds aggregate output allowance")
        return self


class OperationConstraints(Contract):
    operation: ExactReference
    rules: Annotated[tuple[Constraint, ...], Field(max_length=64)]


class N8nWorkflowTarget(Contract):
    kind: Literal["n8n_workflow"]
    webhook_path: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9/_-]{0,127}$")]
    result_node: Annotated[str, Field(pattern=r"^[A-Za-z0-9 _.-]{1,128}$")] = "MCP Result"


class FrProjectTarget(Contract):
    kind: Literal["fr_project"]
    #: The default fr project type; a call may name another (input ``project_type``).
    project_type: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,128}$")]


class FrCommandTarget(Contract):
    """One fr channel command whose answer is the tool's result.

    ``projects.get`` is a pure read; ``projects.close`` asks fr to close the project and answers
    with the closed project. Neither takes free-form text: the catalog fixes the command."""

    kind: Literal["fr_command"]
    #: Fixed by the catalog: a tool target never carries free-form channel text.
    command: Literal["projects.get", "projects.close"]


ExternalTarget = Annotated[
    N8nWorkflowTarget | FrProjectTarget | FrCommandTarget, Field(discriminator="kind")
]


class ToolManifest(Contract):
    schema_version: Literal[1] = 1
    tool_id: UUID7
    tenant_id: AuthorityId
    version: PositiveVersion
    public_name: Name
    operation: Operation
    description: Annotated[str, Field(min_length=1, max_length=4096)]
    runtime: Literal["kata_python", "n8n_workflow", "fr_project", "fr_command"] = "kata_python"
    target: ExternalTarget | None = None
    entrypoint: Annotated[str, Field(max_length=256)] | None = None
    input_schema: dict[str, JsonValue]
    result_schema: dict[str, JsonValue]
    context_schema: dict[str, JsonValue]
    package_sha256: Digest
    runner_image_digest: ImageDigest | None = None
    allowed_operations: tuple[ExactReference, ...] = ()
    operation_constraints: tuple[OperationConstraints, ...] = ()
    limits: ToolLimits = Field(default_factory=ToolLimits)
    effect: Literal["read_only", "writes"]

    @model_validator(mode="after")
    def runtime_shape(self) -> Self:
        if self.runtime == "kata_python":
            if self.target is not None:
                raise ValueError("kata_python tools take no external target")
            if self.entrypoint is None or self.runner_image_digest is None:
                raise ValueError("kata_python tools require entrypoint and runner_image_digest")
        else:
            if self.target is None or self.target.kind != self.runtime:
                raise ValueError("external tools require a target whose kind matches the runtime")
            if self.entrypoint is not None or self.runner_image_digest is not None:
                raise ValueError("external tools take no entrypoint or runner image")
        return self

    @model_validator(mode="after")
    def exact_permissions(self) -> Self:
        allowed = {(r.id, r.version) for r in self.allowed_operations}
        constrained = [(r.operation.id, r.operation.version) for r in self.operation_constraints]
        if len(allowed) != len(self.allowed_operations) or len(set(constrained)) != len(
            constrained
        ):
            raise ValueError("duplicate operation reference")
        if len({r.id for r in self.allowed_operations}) != len(self.allowed_operations):
            raise ValueError("protocol v1 selects one exact version per operation identity")
        if not set(constrained) <= allowed:
            raise ValueError("constraints reference an undeclared operation")
        return self


class Attribution(Contract):
    subject: Annotated[str, Field(min_length=1, max_length=256)]
    license_id: AuthorityId | None = None


class VersionDescriptor(Contract):
    tenant_id: AuthorityId
    tool_id: UUID7
    version: PositiveVersion
    revision: PositiveVersion
    status: Literal["draft", "published", "disabled", "retired"]
    manifest: ToolManifest
    created_by: Attribution
    source: ExactReference | None = None
