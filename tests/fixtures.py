"""Synthetic packages and two independent application contracts; never customer data."""

from typing import Any
from uuid import UUID

from src.catalog.models import (
    Attribution,
    AuthAdapter,
    ExactReference,
    N8nWorkflowTarget,
    OperationManifest,
    ServiceManifest,
    ToolManifest,
)
from src.catalog.package import PackageFile, package_digest
from src.config import Settings

TENANT_A = UUID("10000000-0000-7000-8000-000000000001")
TENANT_B = UUID("10000000-0000-7000-8000-000000000002")
TOOL = UUID("20000000-0000-7000-8000-000000000001")
ACTOR = Attribution(
    subject="synthetic-subject", license_id=UUID("10000000-0000-7000-8000-000000000003")
)
IMAGE = "registry.example/runner@sha256:" + "a" * 64
SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
FILES = (
    PackageFile(path="package/main.py", content="async def run(inputs, ctx):\n    return {}\n"),
    PackageFile(path="package/__init__.py", content=""),
    PackageFile(path="data/message.txt", content="café ☕\n"),
)
SINGLE = (PackageFile(path="main.py", content="async def run(inputs, ctx):\n    return {}\n"),)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://unused/unused",
        "approved_runner_images": (IMAGE,),
        **overrides,
    }
    return Settings(**values)


def tool(tenant=TENANT_A, tool_id=TOOL, files=FILES, **updates) -> ToolManifest:
    values: dict[str, Any] = dict(
        tool_id=tool_id,
        tenant_id=tenant,
        version=1,
        public_name="synthetic_tool",
        description="Synthetic fixture",
        entrypoint="package.main:run",
        input_schema=SCHEMA,
        result_schema=SCHEMA,
        context_schema=SCHEMA,
        package_sha256=package_digest(files),
        runner_image_digest=IMAGE,
        effect="read_only",
    )
    values.update(updates)
    values.setdefault("operation", "synthetic." + values["public_name"][:32])
    return ToolManifest(**values)


def external_tool(**updates) -> ToolManifest:
    """An n8n_workflow tool: no package entrypoint and no runner image."""
    values: dict[str, Any] = dict(
        runtime="n8n_workflow",
        target=N8nWorkflowTarget(kind="n8n_workflow", webhook_path="mcp/demo"),
        entrypoint=None,
        runner_image_digest=None,
    )
    values.update(updates)
    return tool(**values)


def service(number=1, **updates) -> ServiceManifest:
    values: dict[str, Any] = dict(
        service_id=UUID(f"30000000-0000-7000-8000-{number:012d}"),
        version=1,
        service_key=f"service_{number}",
        destination_alias=f"destination_{number}",
        license_app_key=f"app_{number}",
        auth_adapter=AuthAdapter.LICENSE_BEARER,
        credential_profile_key=f"credential_{number}",
        max_request_bytes=4096,
        max_response_bytes=8192,
    )
    values.update(updates)
    return ServiceManifest(**values)


def operation(number=1, **updates) -> OperationManifest:
    values: dict[str, Any] = dict(
        operation_id=UUID(f"40000000-0000-7000-8000-{number:012d}"),
        version=1,
        service=ExactReference(id=service(number).service_id, version=1),
        method="GET",
        path_template="/records",
        input_schema=SCHEMA,
        output_schema=SCHEMA,
        response_projection=("/records",),
        effect="read_only",
        timeout_seconds=10,
        pagination_limit=50,
        max_response_bytes=8192,
    )
    values.update(updates)
    return OperationManifest(**values)


async def register_source(
    pool,
    key: str,
    *,
    app_key: str | None = None,
    base_url: str | None = None,
    scheme: str = "bearer",
    status: str = "active",
) -> None:
    """A licence-kind tool source row (what ``POST /mcp/sources`` creates), by direct SQL."""
    from uuid import uuid7

    from psycopg.types.json import Jsonb

    number = key.rsplit("_", 1)[-1]
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO public.mcp_sources(source_id,source_key,status,base_url,auth,created_by,"
            "updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (owner_tenant_id, source_key) DO NOTHING",
            (
                uuid7(),
                key,
                status,
                base_url or f"https://service{number}.example",
                Jsonb({"kind": "licence", "app_key": app_key or f"app_{number}", "scheme": scheme}),
                Jsonb(ACTOR.model_dump(mode="json")),
                Jsonb(ACTOR.model_dump(mode="json")),
            ),
        )


async def register_sources(pool, numbers=(1, 2)) -> None:
    for number in numbers:
        await register_source(pool, f"destination_{number}")
