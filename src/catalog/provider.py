"""FastMCP 4 adapter: trusted wrappers, exact schemas, live authority."""

import json
from uuid import UUID

from fastmcp.server.providers import Provider
from fastmcp.tools import Tool, ToolResult
from mcp.types import ToolAnnotations
from pydantic import PrivateAttr

from src.auth.context import current_authority
from src.catalog.resolver import CatalogResolver, ResolvedTool
from src.catalog.submission import Submission, SubmissionError
from src.catalog.validation import InputValidationError, validate_instance
from src.execution.models import QueueFullError


class CatalogTool(Tool):
    _resolver: CatalogResolver = PrivateAttr()
    _submission: Submission = PrivateAttr()
    _resolved: ResolvedTool = PrivateAttr()

    def __init__(self, resolved: ResolvedTool, resolver: CatalogResolver, submission: Submission):
        public = resolved.public()
        super().__init__(
            name=public["name"],
            description=public["description"],
            parameters=public["inputSchema"],
            output_schema=public["outputSchema"],
            annotations=ToolAnnotations(**public["annotations"]),
            meta=dict(public["_meta"]),
            version=str(resolved.manifest.version),
        )
        self._resolver, self._submission, self._resolved = resolver, submission, resolved

    async def run(self, arguments: dict) -> ToolResult:
        try:
            request = current_authority()
            settings = self._resolver.access.settings
            try:
                validate_instance(self._resolved.manifest.input_schema, arguments, settings)
            except InputValidationError as invalid:
                # The caller can fix its own call: paths and keywords only, never values.
                return ToolResult(
                    content=json.dumps(
                        {"error": "invalid_arguments", "problems": invalid.problems},
                        ensure_ascii=False,
                    ),
                    is_error=True,
                )
            # Do this after input validation, immediately before submission. A
            # retained wrapper never retains its earlier caller's authority.
            tools = await self._resolver.resolve(request.principal)
            current = next((t for t in tools if t.manifest.public_name == self.name), None)
            if current is None or current != self._resolved:
                raise PermissionError("tool authority changed")
            envelope = await self._submission.submit(
                request.principal, current, arguments, request.idempotency_key
            )
            validate_instance(current.public()["outputSchema"], envelope, settings)
            if UUID(envelope["run_id"]).version != 7:
                raise ValueError("run identity must be UUIDv7")
            if envelope["status"] == "succeeded":
                validate_instance(current.manifest.result_schema, envelope["result"], settings)
            encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > min(
                current.effective.limits.output_bytes, settings.protocol_output_bytes // 2
            ):
                raise ValueError("result too large")
            return ToolResult(
                content=encoded,
                structured_content=envelope,
                is_error=envelope["status"] in {"failed", "cancelled", "timed_out", "interrupted"},
            )
        except QueueFullError:
            return ToolResult(content="execution_queue_full", is_error=True)
        except SubmissionError:
            return ToolResult(content="execution_unavailable", is_error=True)
        except Exception:
            # Never reflect inputs, schema diagnostics, SQL or host exception text.
            return ToolResult(content="tool_request_denied", is_error=True)


class CatalogProvider(Provider):
    def __init__(self, resolver: CatalogResolver, submission: Submission):
        super().__init__()
        self.resolver, self.submission = resolver, submission

    async def _list_tools(self):
        tools = await self.resolver.resolve(current_authority().principal)
        return [CatalogTool(t, self.resolver, self.submission) for t in tools]

    async def _get_tool(self, name, version=None):
        # Only binding-pinned versions exist in this provider. No latest fallback.
        for tool in await self._list_tools():
            if tool.name == name and (version is None or version.matches(tool.version)):
                return tool
        return None

    async def _get_tool_by_hash(self, tool_hash, tool_name):
        return None

    async def get_tasks(self):
        # Catalog runs are durable mcp_runs rows, never Docket background tasks. The default
        # would list tools at startup, outside any request authority.
        return []
