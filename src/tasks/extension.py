"""``io.modelcontextprotocol/tasks`` server extension.

Task creation happens in ``CatalogMCP``'s own ``tools/call`` handler (the catalog protocol
bypasses FastMCP's call chain, so ``intercept_tool_call`` would never fire). This extension
registers the ``tasks/*`` methods and holds the creation helper. No outbound HTTP.
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import mcp_types
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.extensions import (
    MethodBinding,
    ServerExtension,
    read_client_extension_settings,
)
from mcp.server.context import ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import decode_header_value
from mcp.types import RequestParams

from src.auth.context import current_authority, current_caller
from src.config import Settings
from src.execution.models import TERMINAL
from src.storage.executions import ExecutionRepository
from src.storage.task_inputs import TaskInputRepository
from src.tasks.activity import fr_meta
from src.tasks.mapping import create_task_result, task_view
from src.tasks.models import TASKS_EXTENSION_ID

TASK_PROTOCOL_VERSION = "2026-07-28"
ACTOR_META_KEY = "ai.nonagentic/actor"
COMPLETE = {"resultType": "complete"}


class TaskParams(RequestParams):
    task_id: str


class TaskUpdateParams(TaskParams):
    input_responses: dict[str, Any]


def _not_found() -> MCPError:
    return MCPError(code=mcp_types.INVALID_PARAMS, message="task not found")


def declared(ctx: ServerRequestContext) -> bool:
    return read_client_extension_settings(ctx, TASKS_EXTENSION_ID) is not None


def parse_actor(params: object) -> dict | None:
    """The validated ``_meta["ai.nonagentic/actor"]`` of a request, or None if absent/invalid.

    ``{"channel": "mcp", "address": str}`` plus, optionally, ``job`` (a UUID string: the agent job
    acting) and ``on_behalf_of`` (str, the person it acts for). A wrongly typed optional field
    rejects the actor; unknown keys are dropped."""
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    actor = meta.get(ACTOR_META_KEY) if isinstance(meta, Mapping) else None
    if not isinstance(actor, Mapping) or actor.get("channel") != "mcp":
        return None
    address = actor.get("address")
    if not isinstance(address, str) or not 0 < len(address) <= 256:
        return None
    parsed: dict = {"channel": "mcp", "address": address}
    job = actor.get("job")
    if job is not None:
        if not isinstance(job, str):
            return None
        try:
            parsed["job"] = str(UUID(job))
        except ValueError:
            return None
    on_behalf_of = actor.get("on_behalf_of")
    if on_behalf_of is not None:
        if not isinstance(on_behalf_of, str) or not 0 < len(on_behalf_of) <= 256:
            return None
        parsed["on_behalf_of"] = on_behalf_of
    return parsed


def _valid_actor(ctx: ServerRequestContext) -> dict:
    actor = parse_actor(ctx.params)
    if actor is None:
        raise MCPError(code=mcp_types.INVALID_PARAMS, message="actor required")
    return actor


class TasksExtension(ServerExtension):
    identifier = TASKS_EXTENSION_ID

    def __init__(
        self, settings: Settings, runs: ExecutionRepository, inputs: TaskInputRepository
    ) -> None:
        self._config, self.runs, self.inputs = settings, runs, inputs

    def methods(self):
        versions = frozenset({TASK_PROTOCOL_VERSION})
        return (
            MethodBinding("tasks/get", TaskParams, self._get, versions),
            MethodBinding("tasks/update", TaskUpdateParams, self._update, versions),
            MethodBinding("tasks/cancel", TaskParams, self._cancel, versions),
        )

    def wants_task(self, ctx: ServerRequestContext) -> bool:
        return (
            self._config.tasks_enabled
            and ctx.protocol_version == TASK_PROTOCOL_VERSION
            and declared(ctx)
        )

    async def _fr_meta(self, tenant_id: UUID, row: Mapping) -> dict | None:
        """The fr activity block of this run (None for a non-fr runtime)."""
        return await fr_meta(self.runs.pool, tenant_id, row, self._config)

    async def task_result(self, ctx, principal, structured: Mapping | None) -> dict | None:
        """A task-creation result for a non-terminal run, else None (caller returns the envelope)."""
        if not self.wants_task(ctx) or not structured or not structured.get("run_id"):
            return None
        if structured.get("status") in TERMINAL:
            return None
        row = await self.runs.get(principal.tenant_id, UUID(structured["run_id"]))
        meta = await self._fr_meta(principal.tenant_id, row)
        return create_task_result(row, self._config, meta)

    async def _owned(self, ctx: ServerRequestContext, params: TaskParams) -> tuple[Any, dict]:
        if not declared(ctx):
            raise MCPError(
                code=mcp_types.MISSING_REQUIRED_CLIENT_CAPABILITY,
                message="tasks extension required",
                data={"requiredCapabilities": {"extensions": {TASKS_EXTENSION_ID: {}}}},
            )
        try:
            header = get_http_request().headers.get("mcp-name")
        except RuntimeError:
            header = None
        name = decode_header_value(header) if header is not None else None
        if name is not None and name != params.task_id:
            raise MCPError(code=mcp_types.HEADER_MISMATCH, message="Mcp-Name header mismatch")
        try:
            if current_caller() is not None:
                raise PermissionError("builtin caller")
            principal = current_authority().principal
            run_id = UUID(params.task_id)
            row = await self.runs.get(principal.tenant_id, run_id)
            if (
                principal.binding_id is None
                or row["binding_id"] != principal.binding_id
                or row["gateway_license_id"] != principal.license_id
                or row.get("payload_expired_at") is not None
            ):
                raise PermissionError("not owned")
        except Exception:
            raise _not_found() from None
        return principal, row

    async def _get(self, ctx, params):
        principal, row = await self._owned(ctx, params)
        open_inputs = await self.inputs.open_for(principal.tenant_id, row["run_id"])
        meta = await self._fr_meta(principal.tenant_id, row)
        return task_view(row, open_inputs, self._config, meta)

    async def _update(self, ctx, params):
        principal, row = await self._owned(ctx, params)
        actor = _valid_actor(ctx)
        await self.inputs.answer(principal.tenant_id, row["run_id"], params.input_responses, actor)
        return dict(COMPLETE)

    async def _cancel(self, ctx, params):
        principal, row = await self._owned(ctx, params)
        await self.runs.cancel(principal.tenant_id, row["run_id"])
        return dict(COMPLETE)
