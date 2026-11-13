"""Read-only checks of the exact application schema contract."""

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

EXPECTED_REVISION = "0013_run_activity"


class SchemaUnavailableError(RuntimeError):
    """Missing, old, or unknown schema; never repaired at startup."""


async def verify_schema(
    connection: AsyncConnection[dict[str, Any]],
    platform_tenant: UUID | None = None,
    *,
    require_audit: bool = True,
) -> None:
    """``require_audit`` is the API's check: only the API writes builtin tool-call audit rows,
    so the executor worker (least privilege, no audit grant) verifies with it off."""
    result = await connection.execute("SELECT version_num FROM public.alembic_version")
    rows = await result.fetchall()
    if rows != [{"version_num": EXPECTED_REVISION}]:
        raise SchemaUnavailableError("unexpected database revision")
    result = await connection.execute(
        "SELECT generation FROM public.mcp_catalog_state WHERE singleton = true"
    )
    if await result.fetchone() is None:
        raise SchemaUnavailableError("catalog state missing")
    await connection.execute("SELECT artifact_version FROM public.mcp_access_events LIMIT 0")
    await connection.execute(
        "SELECT 1 FROM public.mcp_runs,public.mcp_run_events,public.mcp_task_inputs,"
        "public.mcp_run_activity LIMIT 0"
    )
    # Builtin per-call audit (0006): the runtime role may only INSERT, so check the grant.
    result = await connection.execute(
        "SELECT has_table_privilege(current_user,'public.mcp_tool_call_audit','INSERT') AS ok"
    )
    audit = await result.fetchone()
    if require_audit and (audit is None or audit["ok"] is not True):
        raise SchemaUnavailableError("tool call audit unavailable")
    # Platform toolkits (0008): the database's platform tenant must be the configured one, or
    # every platform toolkit pin would be rejected (or, worse, a different tenant trusted).
    result = await connection.execute(
        "SELECT public.mcp_platform_tenant() AS tenant, "
        "has_function_privilege(current_user,'public.mcp_platform_tenant()','EXECUTE') AS ok"
    )
    platform = await result.fetchone()
    if (
        platform is None
        or platform["ok"] is not True
        or (platform_tenant is not None and platform["tenant"] != platform_tenant)
    ):
        raise SchemaUnavailableError("platform tenant mismatch")
    # Resolve every required relation without reading package contents or mutating data.
    await connection.execute(
        "SELECT 1 FROM public.mcp_services, public.mcp_service_versions, "
        "public.mcp_operation_versions, public.mcp_tools, public.mcp_tool_versions, "
        "public.mcp_tool_version_files, public.mcp_tool_version_operations, "
        "public.mcp_toolkits, public.mcp_toolkit_versions, public.mcp_toolkit_version_tools, "
        "public.mcp_toolkit_inclusions, public.mcp_credential_profiles, public.mcp_credential_refs, "
        "public.mcp_bindings, public.mcp_binding_tools, public.mcp_binding_toolkits, "
        "public.mcp_protocol_sessions, public.mcp_access_events, public.mcp_binding_ensures, "
        "public.mcp_sources, public.mcp_service_grants, public.mcp_operation_keys, "
        "public.mcp_source_specs LIMIT 0"
    )
    # Tenant builder (0010): the reference rule is evaluated by both the API and the worker role.
    result = await connection.execute(
        "SELECT has_function_privilege(current_user,"
        "'public.mcp_tenant_may_use_service(uuid,uuid,integer)','EXECUTE') AS ok"
    )
    builder = await result.fetchone()
    if builder is None or builder["ok"] is not True:
        raise SchemaUnavailableError("tenant builder reference rule unavailable")
