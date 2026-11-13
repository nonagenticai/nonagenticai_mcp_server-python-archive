"""Strict request bodies of the tenant builder (REST and the builder_* MCP tools share them).

All are ``Contract``s (strict, closed, frozen) and are parsed from JSON text so strict mode sees
the same representation on both surfaces. Secrets exist only in ``src.sources.models`` bodies.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import UUID7, Field, JsonValue, model_validator

from src.catalog.models import (
    Contract,
    Digest,
    Name,
    Operation,
    Pointer,
    PositiveVersion,
    ToolLimits,
)
from src.catalog.package import PackageFile
from src.sources.models import SpecPath

#: Reserved by the host: written into every tool package, never by the author.
HOST_FILES = frozenset({"kit.py", "ops.py"})


class SpecFetchRequest(Contract):
    """Fetch the source's OpenAPI document (JSON). ``path`` defaults to the source's spec_path."""

    path: SpecPath | None = None


class OperationSelector(Contract):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: Annotated[str, Field(min_length=1, max_length=1024)]


class OperationOverride(Contract):
    """Per-operation choices; every field is optional and replaces the importer's default."""

    op_key: Name | None = None
    effect: Literal["read_only", "writes"] | None = None
    expected_error_statuses: Annotated[tuple[int, ...], Field(max_length=16)] | None = None
    response_projection: Annotated[tuple[Pointer, ...], Field(min_length=1)] | None = None
    timeout_seconds: Annotated[int, Field(gt=0, le=300)] | None = None


class ImportRequest(Contract):
    """Import operations from a cached spec. ``select`` entries are an operationId or a
    ``{method, path}``; ``overrides`` is keyed by that operationId or by ``"METHOD /path"``."""

    spec_sha256: Digest
    select: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)] | OperationSelector, ...],
        Field(min_length=1, max_length=128),
    ]
    overrides: Annotated[dict[str, OperationOverride], Field(max_length=128)] = Field(
        default_factory=dict
    )


class OperationPatch(Contract):
    """A new draft version of an imported operation (omitted fields keep their value)."""

    expected_version: PositiveVersion
    effect: Literal["read_only", "writes"] | None = None
    expected_error_statuses: Annotated[tuple[int, ...], Field(max_length=16)] | None = None
    response_projection: Annotated[tuple[Pointer, ...], Field(min_length=1)] | None = None
    redact_fields: tuple[Pointer, ...] | None = None
    timeout_seconds: Annotated[int, Field(gt=0, le=300)] | None = None
    max_retries: Annotated[int, Field(ge=0, le=3)] | None = None
    idempotency: Literal["none", "downstream_key"] | None = None
    empty_response: bool | None = None


class ToolOperation(Contract):
    """An operation the script may call: one of your sources (``source_key``) or a platform
    service shared with you (``service``), plus its ``op_key``. In the script the operation is
    ``"<source_key or service>.<op_key>"``."""

    op_key: Name
    source_key: Name | None = None
    service: Name | None = None

    @model_validator(mode="after")
    def one_origin(self) -> Self:
        if (self.source_key is None) == (self.service is None):
            raise ValueError("name exactly one of source_key or service")
        return self

    @property
    def script_key(self) -> str:
        return f"{self.source_key or self.service}.{self.op_key}"


class ToolCreate(Contract):
    public_name: Name
    operation: Operation
    description: Annotated[str, Field(min_length=1, max_length=4096)]
    files: Annotated[tuple[PackageFile, ...], Field(min_length=1, max_length=32)]
    input_schema: dict[str, JsonValue]
    result_schema: dict[str, JsonValue] | None = None
    effect: Literal["read_only", "writes"]
    ops: Annotated[tuple[ToolOperation, ...], Field(max_length=32)] = ()
    limits: ToolLimits | None = None
    entrypoint: Annotated[str, Field(max_length=256)] = "tool:main"

    @model_validator(mode="after")
    def no_host_files(self) -> Self:
        if {f.path for f in self.files} & HOST_FILES:
            raise ValueError("kit.py and ops.py are written by the host; do not send them")
        keys = [o.script_key for o in self.ops]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate operation in ops")
        return self


class ToolUpdate(Contract):
    """A new draft version of a tool. Omitted fields keep the latest version's value; ``files``
    replaces ALL script files; ``ops`` replaces the operation list (versions are re-pinned to
    the newest of each operation either way)."""

    expected_revision: PositiveVersion
    description: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    files: Annotated[tuple[PackageFile, ...], Field(min_length=1, max_length=32)] | None = None
    input_schema: dict[str, JsonValue] | None = None
    result_schema: dict[str, JsonValue] | None = None
    effect: Literal["read_only", "writes"] | None = None
    ops: Annotated[tuple[ToolOperation, ...], Field(max_length=32)] | None = None
    limits: ToolLimits | None = None

    @model_validator(mode="after")
    def no_host_files(self) -> Self:
        if self.files and {f.path for f in self.files} & HOST_FILES:
            raise ValueError("kit.py and ops.py are written by the host; do not send them")
        return self


class ToolTestRequest(Contract):
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: Required for a tool whose effect is ``writes``: you confirm real writes to your source.
    confirm_writes: bool = False


class ToolPublishRequest(Contract):
    expected_revision: PositiveVersion


class ToolkitTool(Contract):
    tool_id: UUID7
    #: Omitted: the newest published version of the tool.
    version: PositiveVersion | None = None


class ToolkitRequest(Contract):
    """Create a toolkit (draft v1) or, with ``toolkit_id`` + ``expected_revision``, its next draft
    version. Only your own tools can be selected."""

    name: Name
    description: Annotated[str, Field(min_length=1, max_length=4096)]
    tools: Annotated[tuple[ToolkitTool, ...], Field(min_length=1, max_length=128)]
    toolkit_id: UUID7 | None = None
    expected_revision: PositiveVersion | None = None

    @model_validator(mode="after")
    def append_needs_revision(self) -> Self:
        if (self.toolkit_id is None) != (self.expected_revision is None):
            raise ValueError("toolkit_id and expected_revision go together")
        return self


class ToolkitPublishRequest(Contract):
    expected_revision: PositiveVersion


class BindRequest(Contract):
    """Re-bind the tenant's product bindings so they include its published toolkits.
    ``product`` limits it to one product; omitted: every product with a binding."""

    product: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")] | None = None


__all__ = [
    "BindRequest",
    "ImportRequest",
    "OperationOverride",
    "OperationPatch",
    "OperationSelector",
    "SpecFetchRequest",
    "ToolCreate",
    "ToolOperation",
    "ToolPublishRequest",
    "ToolTestRequest",
    "ToolUpdate",
    "ToolkitPublishRequest",
    "ToolkitRequest",
    "ToolkitTool",
]
