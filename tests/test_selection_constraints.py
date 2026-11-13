"""Policy intersections are preserved, and impossible binding authority is rejected."""

from uuid import uuid7

import pytest
from psycopg.errors import CheckViolation

from src.catalog.models import (
    ContextEquality,
    ExactReference,
    NumericBounds,
    OperationConstraints,
    OwnershipRead,
    RelativePath,
    SetMembership,
    ToolLimits,
)
from src.catalog.service import CatalogService, PlatformCatalogService
from src.storage.catalog import CatalogConflictError, CatalogRepository, PlatformCatalogRepository
from src.toolkits.constraints import check_rules, pointer
from src.toolkits.models import (
    Applicability,
    BoundContext,
    CredentialReference,
    ToolkitReference,
    ToolReference,
    ToolSelection,
)
from src.toolkits.resolver import combine
from tests.fixtures import (
    ACTOR,
    FILES,
    TENANT_A,
    TENANT_B,
    TOOL,
    operation,
    register_source,
    service,
    tool,
)
from tests.test_bindings import PRINCIPAL, profile, selection, toolkit


@pytest.mark.parametrize(
    "rules",
    [
        (
            NumericBounds(kind="numeric_bounds", argument="/n", minimum=1, maximum=2),
            NumericBounds(kind="numeric_bounds", argument="/n", minimum=3, maximum=4),
        ),
        (
            SetMembership(kind="set_membership", argument="/x", values=("a",)),
            SetMembership(kind="set_membership", argument="/x", values=("b",)),
        ),
        (
            ContextEquality(kind="context_equal", argument="/x", context="/project"),
            ContextEquality(kind="context_equal", argument="/x", context="/workspace"),
        ),
        (
            RelativePath(kind="relative_path", argument="/path", root="src"),
            RelativePath(kind="relative_path", argument="/path", root="docs"),
        ),
        (
            SetMembership(kind="set_membership", argument="/path", values=("../escape",)),
            RelativePath(kind="relative_path", argument="/path", root="src"),
        ),
        (
            SetMembership(kind="set_membership", argument="/n", values=(True,)),
            NumericBounds(kind="numeric_bounds", argument="/n", minimum=0, maximum=1),
        ),
        (
            SetMembership(kind="set_membership", argument="/path", values=("docs/a",)),
            RelativePath(kind="relative_path", argument="/path", root="src"),
        ),
        (
            SetMembership(kind="set_membership", argument="/path", values=(123,)),
            RelativePath(kind="relative_path", argument="/path", root="src"),
        ),
    ],
)
def test_empty_constraint_intersections(rules):
    with pytest.raises(ValueError):
        check_rules(rules, {"project": "a", "workspace": "b"})


def test_valid_intersections_context_pointer_and_missing_context():
    check_rules(
        (
            NumericBounds(kind="numeric_bounds", argument="/n", minimum=1, maximum=10),
            SetMembership(kind="set_membership", argument="/n", values=(5, 6)),
        ),
        {},
    )
    check_rules(
        (
            RelativePath(kind="relative_path", argument="/p", root="src"),
            RelativePath(kind="relative_path", argument="/p", root="src/lib"),
            SetMembership(kind="set_membership", argument="/p", values=("src/lib/a",)),
        ),
        {},
    )
    assert pointer({"a/b": [{"~": "value"}]}, "/a~1b/0/~0") == "value"
    with pytest.raises(ValueError):
        pointer({}, "/missing")
    with pytest.raises(ValueError):
        check_rules(
            (
                OwnershipRead(
                    kind="ownership_read",
                    argument="/x",
                    operation=ExactReference(id=uuid7(), version=1),
                    result="/id",
                    context="/missing",
                ),
            ),
            {},
        )


async def published_operation(access):
    catalog = CatalogService(CatalogRepository(access.repository.pool), access.settings)
    platform = PlatformCatalogService(PlatformCatalogRepository(access.repository.pool), catalog)
    await platform.add_service(service(), ACTOR)
    await register_source(
        access.repository.pool, service().destination_alias, app_key=service().license_app_key
    )
    await platform.repository.transition(
        "service", ExactReference(id=service().service_id, version=1), "published", ACTOR
    )
    op = operation()
    await platform.add_operation(op, ACTOR)
    await platform.repository.transition(
        "operation", ExactReference(id=op.operation_id, version=1), "published", ACTOR
    )
    reference = ExactReference(id=op.operation_id, version=1)
    await catalog.append_draft(
        TENANT_A, TOOL, tool(allowed_operations=(reference,)), FILES, ACTOR, expected_revision=2
    )
    await catalog.transition(TENANT_A, TOOL, 2, "published", ACTOR, expected_revision=3)
    return reference, catalog, platform


async def test_overlapping_toolkits_intersect_limits_and_constraints(access):
    reference, _, _ = await published_operation(access)
    entry = ToolSelection(
        tool=ToolReference(tenant_id=TENANT_A, id=TOOL, version=2),
        operation_constraints=(
            OperationConstraints(
                operation=reference,
                rules=(NumericBounds(kind="numeric_bounds", argument="/n", minimum=0, maximum=5),),
            ),
        ),
        limits=ToolLimits(wall_seconds=20, api_calls=4),
    )
    a = await access.toolkit_write(PRINCIPAL, toolkit(tools=(entry,)))
    await access.toolkit_transition(PRINCIPAL, a.manifest.toolkit_id, 1, "published", 1)
    b = await access.toolkit_write(
        PRINCIPAL,
        toolkit(
            name="second_toolkit",
            tools=(entry.model_copy(update={"limits": ToolLimits(wall_seconds=10, api_calls=2)}),),
        ),
    )
    await access.toolkit_transition(PRINCIPAL, b.manifest.toolkit_id, 1, "published", 1)
    credential = profile(
        services=(
            CredentialReference(
                service="service_1", alias="service_alias", license_id=uuid7(), app_key="app_1"
            ),
        )
    )
    await access.profile_write(PRINCIPAL, credential)
    from datetime import UTC, datetime, timedelta

    from src.toolkits.models import BindingSpec

    spec = BindingSpec(
        credential_profile_id=credential.profile_id,
        toolkits=(
            ToolkitReference(id=a.manifest.toolkit_id, version=1),
            ToolkitReference(id=b.manifest.toolkit_id, version=1),
        ),
        context=BoundContext(tenant_id=TENANT_A),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        limits=ToolLimits(wall_seconds=15, api_calls=3),
    )
    binding, _ = await access.binding_write(PRINCIPAL, spec)
    assert (
        len(binding.tools) == 1
        and binding.tools[0].limits.wall_seconds == 10
        and binding.tools[0].limits.api_calls == 2
    )
    assert len(binding.tools[0].operation_constraints) == 2
    bad = spec.model_copy(
        update={
            "operation_constraints": (
                OperationConstraints(
                    operation=reference,
                    rules=(
                        NumericBounds(kind="numeric_bounds", argument="/n", minimum=6, maximum=8),
                    ),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="incompatible"):
        await access.binding_write(PRINCIPAL, bad)
    async with access.repository.pool.connection() as conn:
        # Equivalent per-replica selection, with exact public-name collision rejection.
        entry = binding.tools[0]
        assert combine([entry, entry])[0].limits == entry.limits
        with pytest.raises(CatalogConflictError):
            combine(
                [
                    entry,
                    entry.model_copy(
                        update={"tool": ToolReference(tenant_id=TENANT_A, id=uuid7(), version=1)}
                    ),
                ]
            )
        with pytest.raises(ValueError):
            await access.resolver.toolkit(
                conn,
                TENANT_A,
                a.manifest.toolkit_id,
                1,
                seen=frozenset({(a.manifest.toolkit_id, 1)}),
            )
        with pytest.raises(ValueError):
            await access.resolver.toolkit(conn, TENANT_A, a.manifest.toolkit_id, 1, budget=[256])
        with pytest.raises(CheckViolation):
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM mcp_binding_tools WHERE binding_id=%s", (binding.binding_id,)
                )
                await conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


async def test_shared_tools_require_explicit_grant_and_live_recheck(access):
    catalog = CatalogService(CatalogRepository(access.repository.pool), access.settings)
    foreign = tool(tenant=TENANT_B, tool_id=uuid7(), public_name="shared_tool")
    await catalog.create_draft(TENANT_B, foreign, FILES, ACTOR)
    await catalog.transition(TENANT_B, foreign.tool_id, 1, "published", ACTOR, expected_revision=1)
    ref = ToolReference(tenant_id=TENANT_B, id=foreign.tool_id, version=1)
    platform = PlatformCatalogRepository(access.repository.pool)
    await platform.grant_share(TENANT_B, foreign.tool_id, TENANT_A, ACTOR)
    draft = await access.toolkit_write(PRINCIPAL, toolkit(tools=(ToolSelection(tool=ref),)))
    await access.toolkit_transition(PRINCIPAL, draft.manifest.toolkit_id, 1, "published", 1)
    async with access.repository.pool.connection() as conn:
        await conn.execute("DELETE FROM mcp_tool_shares")
    with pytest.raises(LookupError):
        async with access.repository.pool.connection() as conn:
            await access.resolver.toolkit(conn, TENANT_A, draft.manifest.toolkit_id, 1)


async def test_applicability_and_bound_field_schemas_fail_closed(access):
    spec, manifest, _ = await selection(access)
    with pytest.raises(PermissionError):
        await access.toolkit_write(
            PRINCIPAL.__class__(TENANT_A, PRINCIPAL.license_id, "agent"), manifest
        )
    phase = toolkit(
        name="phase_toolkit",
        applicability=Applicability(project_phase="build", card_stage="review"),
    )
    await access.toolkit_write(PRINCIPAL, phase)
    await access.toolkit_transition(PRINCIPAL, phase.toolkit_id, 1, "published", 1)
    bad = spec.model_copy(update={"toolkits": (ExactReference(id=phase.toolkit_id, version=1),)})
    with pytest.raises(ValueError, match="applicability"):
        await access.binding_write(PRINCIPAL, bad)
    catalog = CatalogService(CatalogRepository(access.repository.pool), access.settings)
    schema = {
        "type": "object",
        "properties": {"tenant_id": {"type": "string"}},
        "additionalProperties": False,
    }
    # Authoring now rejects this too. Use the internal repository to verify
    # binding-time defense against pre-existing invalid public schemas.
    with pytest.raises(ValueError, match="public input"):
        await catalog.append_draft(
            TENANT_A, TOOL, tool(input_schema=schema), FILES, ACTOR, expected_revision=2
        )
    await catalog.repository.append(
        TENANT_A, TOOL, tool(input_schema=schema), FILES, ACTOR, expected_revision=2
    )
    await catalog.repository.transition(TENANT_A, TOOL, 2, "published", ACTOR, expected_revision=3)
    shadow = toolkit(
        name="shadow_toolkit",
        tools=(ToolSelection(tool=ToolReference(tenant_id=TENANT_A, id=TOOL, version=2)),),
    )
    await access.toolkit_write(PRINCIPAL, shadow)
    await access.toolkit_transition(PRINCIPAL, shadow.toolkit_id, 1, "published", 1)
    bad = spec.model_copy(update={"toolkits": (ExactReference(id=shadow.toolkit_id, version=1),)})
    with pytest.raises(ValueError, match="public input"):
        await access.binding_write(PRINCIPAL, bad)


# Expose the shared real-database fixture to pytest.
from tests.test_bindings import access as access
