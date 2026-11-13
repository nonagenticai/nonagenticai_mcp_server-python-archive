"""The builder_* builtin MCP toolkit: a tenant authors sources, operations, tools and toolkits.

Mounted under namespace ``builder`` (mounted name = ``builder_<tool>``). Offered only to a
TENANT_AUTHOR caller (tenant-level mcp licence, no binding; src.builtin.scope "licence:mcp").
The tenant always comes from the authenticated caller, never from an argument. Secrets are
write-only: ``builder_source_set_secret`` / ``token`` are never echoed, logged or audited.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Annotated, Any
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from src.auth.context import current_caller
from src.auth.principal import TENANT_AUTHOR, AuthorPrincipal
from src.builder import runtime
from src.builder.models import (
    BindRequest,
    ImportRequest,
    OperationPatch,
    SpecFetchRequest,
    ToolCreate,
    ToolkitPublishRequest,
    ToolkitRequest,
    ToolPublishRequest,
    ToolTestRequest,
    ToolUpdate,
)
from src.builder.service import BuilderError, BuilderInputError, parse
from src.sources.models import RevisionOnly, TenantSourceCreate, TenantSourceUpdate, TokenUpdate
from src.storage.catalog import (
    BuilderForbiddenError,
    CatalogConflictError,
    CatalogNotFoundError,
    QuotaExceededError,
)

logger = logging.getLogger(__name__)

SCRIPT_GUIDE = """\
SCRIPT GUIDE. A tool is ONE deterministic async Python script run in an isolated sandbox. Send
files=[{"path":"tool.py","content":"..."}]; the entrypoint is tool:main, i.e.
`async def main(inputs, ctx)` returning the result envelope. The host adds kit.py (helpers) and
ops.py (operation table): never send them, just `import kit`. Standard library only; the only way
out of the sandbox is kit.call.
Calling your operations: the script key is "<source_key>.<op_key>" (platform service operations:
"<service>.<op_key>"), exactly the entries of `ops`. Arguments are the operation's input_schema
fields (see builder_operation_list).
  status, body = await kit.call(ctx, "crm.get_customer", {"id": inputs["id"]})
    -> (200, body); or (status, body) for a 4xx the operation lists in expected_error_statuses.
       Any other failure raises RuntimeError("api_call_denied").
  body = await kit.call_ok(ctx, key, args, what="load customer")   # non-200 ends the tool with backend_error
  return await kit.guarded(run(inputs, ctx))                        # turns aborts/denied calls into envelopes
Result envelope (build it with the helpers; invariants are enforced):
  kit.completed(data, checks, next_steps)        -> status ok (check_failed if a check fails)
  kit.check(name, "pass"|"fail"|"degraded"|"error", summary)   # name: lowercase snake_case
  kit.precondition_failed(reason, [kit.step("other_tool", "why")])   # reason: snake_case
  kit.backend_error("message", retryable=False)
  kit.pending(token, next_steps) with kit.encode_token(tool, state) / kit.decode_token(token, tool)
  kit.step(tool, reason, arguments); kit.clip(text); kit.Budget(wall_seconds) and kit.poll(...)
    for bounded waiting; kit.describe(status, body) summarises an unexpected HTTP status.
Write tools: set effect="writes" if ANY operation the tool uses writes; test them with
confirm_writes=true only against data you accept changing.
"""

builder_mcp = FastMCP(
    name="builder",
    instructions=(
        "Author tools over any HTTPS API. Flow: source_create (+ source_set_secret if needed) -> "
        "spec_fetch -> operations_import -> tool_create -> tool_test (read the trace with run_get) "
        "-> tool_publish -> toolkit_create -> toolkit_publish -> toolkit_bind."
    ),
    mask_error_details=False,
    strict_input_validation=True,
)

Key = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="lowercase snake_case key, starts with a letter, max 64 chars",
    ),
]
Revision = Annotated[int, Field(ge=1, description="the revision you last read (optimistic lock)")]
ToolId = Annotated[str, Field(description="tool id (uuid) returned by tool_create / tool_list")]
Obj = dict[str, Any]


def author() -> AuthorPrincipal:
    caller = current_caller()
    principal = caller.principal if caller else None
    if (
        principal is None
        or principal.kind != TENANT_AUTHOR
        or not principal.tenant_id
        or not principal.license_id
    ):
        raise ToolError("[permanent] not permitted")
    return AuthorPrincipal(UUID(principal.tenant_id), UUID(principal.license_id), principal.subject)


def uuid_arg(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ToolError(f"{what} must be a uuid") from None


def guard[**P](func: Callable[P, Awaitable[Any]]) -> Callable[P, Awaitable[Any]]:
    """Domain errors become ToolErrors with a message the model can act on."""

    @wraps(func)
    async def run(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return await func(*args, **kwargs)
        except ToolError:
            raise
        except QuotaExceededError as error:
            raise ToolError(f"quota exceeded: {error}") from None
        except CatalogNotFoundError as error:
            raise ToolError(f"not found: {error}") from None
        except CatalogConflictError as error:
            raise ToolError(f"conflict: {error}") from None
        except (BuilderError, BuilderInputError) as error:
            raise ToolError(str(error)) from None
        except BuilderForbiddenError, PermissionError:
            raise ToolError("[permanent] not permitted") from None
        except ValueError as error:
            raise ToolError(f"invalid: {str(error)[:500]}") from None
        except Exception:
            logger.exception("builder tool failed")
            raise ToolError("internal error") from None

    return run


def given(**fields: Any) -> Obj:
    return {k: v for k, v in fields.items() if v is not None}


# --- sources ----------------------------------------------------------------------------------


@builder_mcp.tool()
@guard
async def source_create(
    source_key: Key,
    base_url: Annotated[
        str, Field(description="public https URL of the API (no IP, no internal host)")
    ],
    auth: Annotated[
        Obj,
        Field(
            description=(
                'Auth config by "kind": {"kind":"none"}; {"kind":"static_bearer"}; '
                '{"kind":"static_header","header":"X-Api-Key"}; {"kind":"query_param","param":"key"}; '
                '{"kind":"basic","username":"u"}; {"kind":"oauth2_client_credentials","token_url":'
                '"https://...","client_id":"..","scope":"..","client_auth":"basic"|"post"}. '
                "The secret itself goes in `token` (or builder_source_set_secret)."
            )
        ),
    ],
    token: Annotated[
        str | None,
        Field(description="the secret for kinds that need one (write-only, never returned)"),
    ] = None,
    spec_path: Annotated[
        str | None, Field(description='path of the OpenAPI JSON document, e.g. "/openapi.json"')
    ] = None,
    idempotency_header: str | None = None,
    max_request_bytes: Annotated[int | None, Field(ge=1)] = None,
    max_response_bytes: Annotated[int | None, Field(ge=1)] = None,
) -> Obj:
    """Register an HTTPS API as a source of yours. Returns the source view (never the secret)."""
    body = parse(
        TenantSourceCreate,
        given(
            source_key=source_key,
            base_url=base_url,
            auth=auth,
            token=token,
            spec_path=spec_path,
            idempotency_header=idempotency_header,
            max_request_bytes=max_request_bytes,
            max_response_bytes=max_response_bytes,
        ),
    )
    return await runtime.get().source_create(author(), body)


@builder_mcp.tool()
@guard
async def source_update(
    source_key: Key,
    expected_revision: Revision,
    base_url: str,
    auth: Obj,
    token: Annotated[
        str | None, Field(description="omit to keep the stored secret (write-only)")
    ] = None,
    spec_path: str | None = None,
    idempotency_header: str | None = None,
    max_request_bytes: Annotated[int | None, Field(ge=1)] = None,
    max_response_bytes: Annotated[int | None, Field(ge=1)] = None,
) -> Obj:
    """Replace a source's configuration (same fields as source_create). Omitted token keeps it."""
    body = parse(
        TenantSourceUpdate,
        given(
            base_url=base_url,
            auth=auth,
            token=token,
            spec_path=spec_path,
            idempotency_header=idempotency_header,
            max_request_bytes=max_request_bytes,
            max_response_bytes=max_response_bytes,
            expected_revision=expected_revision,
        ),
    )
    return await runtime.get().source_update(author(), source_key, body)


@builder_mcp.tool()
@guard
async def source_set_secret(
    source_key: Key,
    token: Annotated[
        str,
        # No length constraint here on purpose: a failed constraint would make the MCP layer echo
        # (and log) the rejected value. TokenUpdate enforces 1..4096 without echoing it.
        Field(description="the new secret (1-4096 printable characters)"),
    ],
    expected_revision: Revision,
) -> Obj:
    """Set or rotate a source's secret. Write-only: the response shows only has_token."""
    body = parse(TokenUpdate, {"token": token, "expected_revision": expected_revision})
    return await runtime.get().source_set_secret(author(), source_key, body)


@builder_mcp.tool()
@guard
async def source_list(
    limit: Annotated[int, Field(ge=1, le=100)] = 50, offset: Annotated[int, Field(ge=0)] = 0
) -> Obj:
    """List your sources (no secrets) with their revisions."""
    return await runtime.get().source_list(author(), limit, offset)


@builder_mcp.tool()
@guard
async def source_disable(source_key: Key, expected_revision: Revision) -> Obj:
    """Disable a source: its operations and tools stop working; the key stays reserved."""
    body = parse(RevisionOnly, {"expected_revision": expected_revision})
    return await runtime.get().source_set_status(author(), source_key, body, "disabled")


# --- spec and operations ----------------------------------------------------------------------


@builder_mcp.tool()
@guard
async def spec_fetch(
    source_key: Key,
    path: Annotated[
        str | None,
        Field(description="document path on the same origin; default: the source spec_path"),
    ] = None,
) -> Obj:
    """Fetch the source's OpenAPI 3.0/3.1 document (JSON only) through its credential. Returns its
    sha256 and every operation with `importable` and a `reason` when not."""
    body = parse(SpecFetchRequest, given(path=path))
    return await runtime.get().spec_fetch(author(), source_key, body)


@builder_mcp.tool()
@guard
async def spec_operations(
    source_key: Key,
    spec_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
) -> Obj:
    """List the operations of a previously fetched spec (by sha256)."""
    return await runtime.get().spec_operations(author(), source_key, spec_sha256)


@builder_mcp.tool()
@guard
async def operations_import(
    source_key: Key,
    spec_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
    select: Annotated[
        list[str | Obj],
        Field(
            min_length=1,
            max_length=128,
            description='operationIds, or {"method":"GET","path":"/customers/{id}"}',
        ),
    ],
    overrides: Annotated[
        Obj | None,
        Field(
            description=(
                'Per operation, keyed by operationId or "GET /path": {"op_key":..,"effect":'
                '"read_only"|"writes","expected_error_statuses":[404],"response_projection":'
                '["/data"],"timeout_seconds":20}. Handle 4xx in your script by listing them in '
                "expected_error_statuses."
            )
        ),
    ] = None,
) -> Obj:
    """Create draft operations (each addressed by an `op_key`) from the cached spec. Re-importing a
    key adds a new version."""
    body = parse(ImportRequest, given(spec_sha256=spec_sha256, select=select, overrides=overrides))
    return await runtime.get().operations_import(author(), source_key, body)


@builder_mcp.tool()
@guard
async def operation_list(source_key: Key) -> Obj:
    """List a source's operations: op_key, version, status, method/path, effect and the
    input/output schemas the script calls them with."""
    return await runtime.get().operation_list(author(), source_key)


@builder_mcp.tool()
@guard
async def operation_update(
    source_key: Key,
    op_key: Key,
    expected_version: Annotated[int, Field(ge=1, description="the version you last read")],
    effect: Annotated[str | None, Field(pattern="^(read_only|writes)$")] = None,
    expected_error_statuses: list[int] | None = None,
    response_projection: list[str] | None = None,
    redact_fields: list[str] | None = None,
    timeout_seconds: Annotated[int | None, Field(ge=1, le=300)] = None,
    max_retries: Annotated[int | None, Field(ge=0, le=3)] = None,
    idempotency: Annotated[str | None, Field(pattern="^(none|downstream_key)$")] = None,
    empty_response: bool | None = None,
) -> Obj:
    """Create a new draft version of an operation with the given changes. Tools pin exact
    versions: call builder_tool_update afterwards so a tool picks up the new one."""
    body = parse(
        OperationPatch,
        given(
            expected_version=expected_version,
            effect=effect,
            expected_error_statuses=expected_error_statuses,
            response_projection=response_projection,
            redact_fields=redact_fields,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            idempotency=idempotency,
            empty_response=empty_response,
        ),
    )
    return await runtime.get().operation_update(author(), source_key, op_key, body)


# --- tools ------------------------------------------------------------------------------------


@builder_mcp.tool(
    description="Create a draft tool (a script over your operations).\n\n" + SCRIPT_GUIDE
)
@guard
async def tool_create(
    public_name: Annotated[
        Key, Field(description="the MCP tool name callers will see, e.g. crm_find_customer")
    ],
    operation: Annotated[
        str,
        Field(pattern=r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$", description='"object.verb"'),
    ],
    description: Annotated[str, Field(min_length=1, max_length=4096)],
    files: Annotated[
        list[Obj],
        Field(min_length=1, max_length=32, description='[{"path":"tool.py","content":".."}]'),
    ],
    input_schema: Annotated[
        Obj,
        Field(
            description='JSON Schema: {"type":"object","additionalProperties":false,"properties":{..}}'
        ),
    ],
    effect: Annotated[str, Field(pattern="^(read_only|writes)$")],
    ops: Annotated[
        list[Obj] | None,
        Field(
            description='[{"source_key":"crm","op_key":"get_customer"}] or [{"service":"..","op_key":".."}]'
        ),
    ] = None,
    result_schema: Annotated[
        Obj | None, Field(description="omit: the standard result envelope")
    ] = None,
    limits: Obj | None = None,
) -> Obj:
    body = parse(
        ToolCreate,
        given(
            public_name=public_name,
            operation=operation,
            description=description,
            files=files,
            input_schema=input_schema,
            effect=effect,
            ops=ops or [],
            result_schema=result_schema,
            limits=limits,
        ),
    )
    return await runtime.get().tool_create(author(), body)


@builder_mcp.tool(
    description="Create a new draft version of a tool; omitted fields keep their value, `files` "
    "replaces all script files, `ops` replaces the operation list and operations are re-pinned to "
    "their newest versions.\n\n" + SCRIPT_GUIDE
)
@guard
async def tool_update(
    tool_id: ToolId,
    expected_revision: Revision,
    description: str | None = None,
    files: list[Obj] | None = None,
    input_schema: Obj | None = None,
    result_schema: Obj | None = None,
    effect: Annotated[str | None, Field(pattern="^(read_only|writes)$")] = None,
    ops: list[Obj] | None = None,
    limits: Obj | None = None,
) -> Obj:
    body = parse(
        ToolUpdate,
        given(
            expected_revision=expected_revision,
            description=description,
            files=files,
            input_schema=input_schema,
            result_schema=result_schema,
            effect=effect,
            ops=ops,
            limits=limits,
        ),
    )
    return await runtime.get().tool_update(author(), uuid_arg(tool_id, "tool_id"), body)


@builder_mcp.tool()
@guard
async def tool_get(tool_id: ToolId, version: Annotated[int | None, Field(ge=1)] = None) -> Obj:
    """Read a tool version (default: latest): manifest, status, revision, your script files and ops."""
    return await runtime.get().tool_get(author(), uuid_arg(tool_id, "tool_id"), version)


@builder_mcp.tool()
@guard
async def tool_list(
    limit: Annotated[int, Field(ge=1, le=100)] = 50, offset: Annotated[int, Field(ge=0)] = 0
) -> Obj:
    """List your tools' versions with status and revision."""
    return await runtime.get().tool_list(author(), limit, offset)


@builder_mcp.tool()
@guard
async def tool_test(
    tool_id: ToolId,
    version: Annotated[int, Field(ge=1)],
    inputs: Obj | None = None,
    confirm_writes: Annotated[
        bool, Field(description="required for effect=writes tools: you accept real writes")
    ] = False,
) -> Obj:
    """Run a draft or published tool version once (no binding needed). Returns the run envelope;
    read the call trace and error excerpts with builder_run_get."""
    body = parse(ToolTestRequest, {"inputs": inputs or {}, "confirm_writes": confirm_writes})
    return await runtime.get().tool_test(author(), uuid_arg(tool_id, "tool_id"), version, body)


@builder_mcp.tool()
@guard
async def run_get(run_id: Annotated[str, Field(description="run id from tool_test")]) -> Obj:
    """The run envelope plus the trace of upstream calls (method, path, status, duration, reason)."""
    return await runtime.get().run_get(author(), uuid_arg(run_id, "run_id"))


@builder_mcp.tool()
@guard
async def tool_publish(
    tool_id: ToolId, version: Annotated[int, Field(ge=1)], expected_revision: Revision
) -> Obj:
    """Publish a tool version together with the draft services/operations it pins (one atomic step)."""
    body = parse(ToolPublishRequest, {"expected_revision": expected_revision})
    return await runtime.get().tool_publish(author(), uuid_arg(tool_id, "tool_id"), version, body)


# --- toolkits ---------------------------------------------------------------------------------


@builder_mcp.tool()
@guard
async def toolkit_create(
    name: Key,
    description: Annotated[str, Field(min_length=1, max_length=4096)],
    tools: Annotated[
        list[Obj],
        Field(
            min_length=1,
            max_length=128,
            description='[{"tool_id":"..","version":1}] (version omitted: newest published)',
        ),
    ],
    toolkit_id: Annotated[
        str | None, Field(description="set with expected_revision for a new version")
    ] = None,
    expected_revision: Annotated[int | None, Field(ge=1)] = None,
) -> Obj:
    """Create a draft toolkit of your published tools (or its next version)."""
    body = parse(
        ToolkitRequest,
        given(
            name=name,
            description=description,
            tools=tools,
            toolkit_id=toolkit_id,
            expected_revision=expected_revision,
        ),
    )
    return await runtime.get().toolkit_create(author(), body)


@builder_mcp.tool()
@guard
async def toolkit_publish(
    toolkit_id: str, version: Annotated[int, Field(ge=1)], expected_revision: Revision
) -> Obj:
    """Publish a toolkit version and re-bind your product bindings so they include it."""
    body = parse(ToolkitPublishRequest, {"expected_revision": expected_revision})
    return await runtime.get().toolkit_publish(
        author(), uuid_arg(toolkit_id, "toolkit_id"), version, body
    )


@builder_mcp.tool()
@guard
async def toolkit_bind(
    product: Annotated[str | None, Field(description="limit to one product; default all")] = None,
) -> Obj:
    """Re-bind your existing product bindings so they include your published toolkits."""
    body = parse(BindRequest, given(product=product))
    return await runtime.get().rebind(author(), body)


BUILDER_SERVERS: list[tuple[FastMCP, str]] = [(builder_mcp, "builder")]


def mount_builder(mcp: FastMCP) -> None:
    for server, namespace in BUILDER_SERVERS:
        mcp.mount(server, namespace=namespace)


__all__ = ["BUILDER_SERVERS", "SCRIPT_GUIDE", "builder_mcp", "json", "mount_builder"]
