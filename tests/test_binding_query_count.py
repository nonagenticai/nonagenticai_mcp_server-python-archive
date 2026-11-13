"""A binding resolves in a bounded number of statements, whatever its tool count.

The API and its PostgreSQL primary sit on different nodes with a ~13 ms round trip
between them, so every avoidable statement is a visible slice of ``tools/list``. The
binding walk used to issue four statements per tool plus one per tool operation, which
made a 11-tool binding cost 233 round trips (~4 s). Resolution must stay flat: real
authority is still re-read on every request, it is just read once and in bulk.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid7

import psycopg
import pytest

from src.auth.principal import Principal
from src.catalog.service import CatalogService
from src.storage.bindings import BindingRepository
from src.storage.catalog import CatalogRepository
from src.toolkits.models import (
    BindingSpec,
    BoundContext,
    CredentialProfile,
    CredentialReference,
    ToolkitManifest,
    ToolkitReference,
    ToolReference,
    ToolSelection,
)
from src.toolkits.service import AccessService
from tests.fixtures import ACTOR, FILES, TENANT_A, settings, tool

LICENSE = uuid7()
PRINCIPAL = Principal(TENANT_A, LICENSE, "synthetic-operator", management=True)


async def publish_tools(catalog: CatalogService, group: int, count: int) -> list[Any]:
    published = []
    for index in range(count):
        manifest = tool(tool_id=uuid7(), public_name=f"synthetic_{group}_{index}")
        await catalog.create_draft(TENANT_A, manifest, FILES, ACTOR)
        await catalog.transition(
            TENANT_A, manifest.tool_id, 1, "published", ACTOR, expected_revision=1
        )
        published.append(manifest)
    return published


async def bound(access: AccessService, catalog: CatalogService, count: int):
    tools = await publish_tools(catalog, count, count)
    credential = CredentialProfile(
        profile_id=uuid7(),
        tenant_id=TENANT_A,
        name=f"synthetic_profile_{count}",
        gateway=CredentialReference(
            service="mcp", alias="gateway_alias", license_id=LICENSE, app_key="mcp"
        ),
    )
    await access.profile_write(PRINCIPAL, credential)
    manifest = ToolkitManifest(
        toolkit_id=uuid7(),
        tenant_id=TENANT_A,
        version=1,
        name=f"synthetic_toolkit_{count}",
        description="Synthetic toolkit",
        tools=tuple(
            ToolSelection(tool=ToolReference(tenant_id=TENANT_A, id=t.tool_id, version=1))
            for t in tools
        ),
    )
    await access.toolkit_write(PRINCIPAL, manifest)
    await access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "published", 1)
    spec = BindingSpec(
        credential_profile_id=credential.profile_id,
        toolkits=(ToolkitReference(id=manifest.toolkit_id, version=1),),
        context=BoundContext(tenant_id=TENANT_A),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    binding, _ = await access.binding_write(PRINCIPAL, spec)
    return binding


async def counted(call):
    """Run ``call`` and report every statement issued while it ran."""
    original = psycopg.AsyncConnection.execute
    statements: list[str] = []

    async def wrapper(self, query, params=None, **kw):
        statements.append(" ".join(str(query).split()))
        return await original(self, query, params, **kw)

    psycopg.AsyncConnection.execute = wrapper
    try:
        result = await call()
    finally:
        psycopg.AsyncConnection.execute = original
    return result, statements


@pytest.fixture
async def access(database):
    config = settings(
        license_introspect_url="https://licenses.example/introspect", license_app_key="mcp"
    )
    catalog = CatalogService(CatalogRepository(database.pool), config)
    return AccessService(BindingRepository(database.pool), config), catalog


async def test_effective_selection_is_flat_in_tool_count(access):
    service, catalog = access
    small = await bound(service, catalog, 1)
    large = await bound(service, catalog, 8)

    small_tools, small_statements = await counted(lambda: service.effective(small))
    large_tools, large_statements = await counted(lambda: service.effective(large))

    assert len(small_tools) == 1 and len(large_tools) == 8
    assert large_tools == large.tools
    assert len(large_statements) <= 12, "\n".join(large_statements)
    # Eight times the tools must not mean more statements.
    assert len(large_statements) <= len(small_statements) + 2, (
        f"1 tool={len(small_statements)} statements, 8 tools={len(large_statements)}"
    )
