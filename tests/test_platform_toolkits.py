"""Platform-owned toolkits in tenant bindings, and tenant isolation of toolkits (real PostgreSQL).

A tenant's binding pins exact published toolkit versions owned by the tenant or by the reserved
platform tenant, read-only. Another tenant's toolkit is never visible: not through a binding
reference, not through management routes, not through the database constraints.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid7

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation

from src.auth.principal import Principal
from src.catalog.models import ExactReference
from src.catalog.resolver import CatalogResolver
from src.catalog.service import CatalogService
from src.config import PLATFORM_TENANT_ID
from src.storage.bindings import BindingRepository
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError, CatalogRepository
from src.storage.schema import SchemaUnavailableError, verify_schema
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
from tests.fixtures import ACTOR, FILES, TENANT_A, TENANT_B, settings, tool

PLATFORM = PLATFORM_TENANT_ID
PLATFORM_TOOL = UUID("20000000-0000-7000-8000-0000000000f1")
TENANT_TOOL = UUID("20000000-0000-7000-8000-0000000000a1")
B_TOOL = UUID("20000000-0000-7000-8000-0000000000b1")
LICENCE_A, LICENCE_B = uuid7(), uuid7()
OPERATOR_A = Principal(TENANT_A, LICENCE_A, "operator-a", management=True)
OPERATOR_B = Principal(TENANT_B, LICENCE_B, "operator-b", management=True)
OPERATOR_P = Principal(PLATFORM, uuid7(), "platform-operator", management=True)


def kit(tenant, tool_id, name="kit", **updates) -> ToolkitManifest:
    values: dict[str, Any] = dict(
        toolkit_id=uuid7(),
        tenant_id=tenant,
        version=1,
        name=name,
        description=f"{name} toolkit",
        tools=(ToolSelection(tool=ToolReference(tenant_id=tenant, id=tool_id, version=1)),),
    )
    return ToolkitManifest(**{**values, **updates})


@pytest.fixture
async def world(database):
    """Published tools + toolkits for the platform, tenant A and tenant B."""
    config = settings(
        license_introspect_url="https://licenses.example/introspect", license_app_key="mcp"
    )
    catalog = CatalogService(CatalogRepository(database.pool), config)
    access = AccessService(BindingRepository(database.pool), config)
    kits = {}
    for operator, tool_id, public, operation in (
        (OPERATOR_P, PLATFORM_TOOL, "coding_provision_repo", "coding.provision_repo"),
        (OPERATOR_A, TENANT_TOOL, "own_tool", "own.read"),
        (OPERATOR_B, B_TOOL, "b_tool", "b.read"),
    ):
        tenant = operator.tenant_id
        manifest = tool(tenant, tool_id, public_name=public, operation=operation)
        await catalog.create_draft(tenant, manifest, FILES, ACTOR)
        await catalog.transition(tenant, tool_id, 1, "published", ACTOR, expected_revision=1)
        toolkit = kit(tenant, tool_id, "coding" if operator is OPERATOR_P else "own")
        await access.toolkit_write(operator, toolkit)
        await access.toolkit_transition(operator, toolkit.toolkit_id, 1, "published", 1)
        kits[tenant] = toolkit
    return access, kits


async def profile_for(access, operator, licence):
    credential = CredentialProfile(
        profile_id=uuid7(),
        tenant_id=operator.tenant_id,
        name="ensure_agent",
        gateway=CredentialReference(
            service="mcp", alias="gateway", license_id=licence, app_key="mcp"
        ),
    )
    await access.profile_write(operator, credential)
    return credential


def spec(profile, *refs) -> BindingSpec:
    return BindingSpec(
        credential_profile_id=profile.profile_id,
        toolkits=refs,
        context=BoundContext(tenant_id=profile.tenant_id),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def ref(manifest: ToolkitManifest, owner: UUID | None) -> ToolkitReference:
    return ToolkitReference(tenant_id=owner, id=manifest.toolkit_id, version=1)


async def test_binding_pins_platform_and_own_toolkits_read_only(world):
    access, kits = world
    credential = await profile_for(access, OPERATOR_A, LICENCE_A)
    binding, token = await access.binding_write(
        OPERATOR_A,
        spec(credential, ref(kits[PLATFORM], PLATFORM), ref(kits[TENANT_A], None)),
    )
    assert token
    # Stored specs carry every owner explicitly.
    assert {r.tenant_id for r in binding.spec.toolkits} == {PLATFORM, TENANT_A}
    assert {t.tool.tenant_id for t in binding.tools} == {PLATFORM, TENANT_A}
    assert await access.effective(binding) == binding.tools

    resolver = CatalogResolver(access)
    principal = Principal(TENANT_A, LICENCE_A, "agent", binding.binding_id, binding.revision)
    listed = {t.manifest.public_name: t.public() for t in await resolver.resolve(principal)}
    assert listed["coding_provision_repo"]["_meta"] == {"operation": "coding.provision_repo"}
    assert listed["own_tool"]["_meta"] == {"operation": "own.read"}

    async with access.repository.pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT owner_tenant_id FROM mcp_binding_toolkits WHERE binding_id=%s",
                (binding.binding_id,),
            )
        ).fetchall()
        assert {r["owner_tenant_id"] for r in rows} == {PLATFORM, TENANT_A}

    # Read-only: tenant A cannot draft, publish or disable the platform's toolkit.
    with pytest.raises(PermissionError):
        await access.toolkit_write(OPERATOR_A, kits[PLATFORM].model_copy(update={"version": 2}), 2)
    for status in ("published", "disabled"):
        with pytest.raises(CatalogNotFoundError):
            await access.toolkit_transition(OPERATOR_A, kits[PLATFORM].toolkit_id, 1, status, 2)

    # Disabling the platform version reaches the tenant binding on its next resolution.
    await access.toolkit_transition(OPERATOR_P, kits[PLATFORM].toolkit_id, 1, "disabled", 2)
    with pytest.raises(CatalogConflictError):
        await access.effective(binding)


async def test_tenant_a_never_sees_tenant_b_toolkits(world):
    access, kits = world
    credential = await profile_for(access, OPERATOR_A, LICENCE_A)
    b_kit = kits[TENANT_B]
    # Explicit foreign owner, implicit owner (B's id read as A's) and a forged platform owner.
    for reference in (ref(b_kit, TENANT_B), ref(b_kit, None), ref(b_kit, PLATFORM)):
        with pytest.raises(CatalogNotFoundError):
            await access.binding_write(OPERATOR_A, spec(credential, reference))
    # B's tool is not selectable in A's toolkit, nor is the platform's tool (no share).
    for owner, tool_id in ((TENANT_B, B_TOOL), (PLATFORM, PLATFORM_TOOL)):
        foreign = kit(TENANT_A, tool_id, "foreign").model_copy(
            update={
                "tools": (
                    ToolSelection(tool=ToolReference(tenant_id=owner, id=tool_id, version=1)),
                )
            }
        )
        with pytest.raises(CatalogNotFoundError):
            await access.toolkit_write(OPERATOR_A, foreign)
    # A tenant toolkit cannot include the platform's toolkit (inclusions are same-tenant).
    including = kit(
        TENANT_A,
        TENANT_TOOL,
        "including",
        includes=(ExactReference(id=kits[PLATFORM].toolkit_id, version=1),),
    )
    with pytest.raises(CatalogNotFoundError):
        await access.toolkit_write(OPERATOR_A, including)
    # Tenant B's management sees none of A's or the platform's toolkits.
    for tenant in (TENANT_A, PLATFORM):
        with pytest.raises(CatalogNotFoundError):
            await access.toolkit_transition(OPERATOR_B, kits[tenant].toolkit_id, 1, "disabled", 2)


async def test_database_rejects_a_third_tenant_owner(world):
    access, kits = world
    credential = await profile_for(access, OPERATOR_A, LICENCE_A)
    binding, _ = await access.binding_write(OPERATOR_A, spec(credential, ref(kits[TENANT_A], None)))
    async with access.repository.pool.connection() as conn:
        with pytest.raises(CheckViolation):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO mcp_binding_toolkits(tenant_id,binding_id,toolkit_id,version,"
                    "owner_tenant_id) VALUES (%s,%s,%s,1,%s)",
                    (TENANT_A, binding.binding_id, kits[TENANT_B].toolkit_id, TENANT_B),
                )
        # A same-tenant row must name an existing toolkit version of that owner.
        with pytest.raises(ForeignKeyViolation):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO mcp_binding_toolkits(tenant_id,binding_id,toolkit_id,version,"
                    "owner_tenant_id) VALUES (%s,%s,%s,1,%s)",
                    (TENANT_A, binding.binding_id, kits[TENANT_B].toolkit_id, TENANT_A),
                )


async def test_operations_must_be_unique_in_a_binding(world, database):
    access, kits = world
    config = access.settings
    catalog = CatalogService(CatalogRepository(database.pool), config)
    clash = UUID("20000000-0000-7000-8000-0000000000a2")
    manifest = tool(TENANT_A, clash, public_name="clashing_tool", operation="coding.provision_repo")
    await catalog.create_draft(TENANT_A, manifest, FILES, ACTOR)
    await catalog.transition(TENANT_A, clash, 1, "published", ACTOR, expected_revision=1)
    clashing = kit(TENANT_A, clash, "clashing")
    await access.toolkit_write(OPERATOR_A, clashing)
    await access.toolkit_transition(OPERATOR_A, clashing.toolkit_id, 1, "published", 1)
    credential = await profile_for(access, OPERATOR_A, LICENCE_A)
    with pytest.raises(CatalogConflictError):
        await access.binding_write(
            OPERATOR_A, spec(credential, ref(kits[PLATFORM], PLATFORM), ref(clashing, None))
        )


async def test_schema_check_requires_the_configured_platform_tenant(database):
    async with database.pool.connection() as conn:
        await verify_schema(conn, PLATFORM)
        with pytest.raises(SchemaUnavailableError):
            await verify_schema(conn, uuid7())
        constraints = {
            r["conname"]
            for r in await (
                await conn.execute("SELECT conname FROM pg_constraint WHERE conname LIKE 'mcp_%'")
            ).fetchall()
        }
        assert {"mcp_tool_versions_operation", "mcp_binding_toolkits_owner"} <= constraints


def test_settings_platform_tenant_and_product_bundles():
    from src.config import DEFAULT_PRODUCT_BUNDLES

    assert settings().platform_tenant_id == PLATFORM and PLATFORM.version == 7
    assert settings().product_bundles == DEFAULT_PRODUCT_BUNDLES
    with pytest.raises(ValueError):
        settings(platform_tenant_id=UUID("10000000-0000-4000-8000-000000000001"))
    with pytest.raises(ValueError, match="bundle"):
        settings(product_bundles={"agent": "agent"})  # care-bus missing
    with pytest.raises(ValueError, match="bundle"):
        settings(product_bundles={"agent": "Agent", "care-bus": "care-bus"})


@pytest.mark.parametrize(
    "operation", ["coding", "Coding.provision", "coding.", "a.b.c", "x" * 40 + ".y"]
)
def test_tool_operation_is_validated(operation):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        tool(operation=operation)
