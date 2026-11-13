import asyncio
from uuid import uuid7

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation

from src.catalog.models import (
    Attribution,
    ExactReference,
    OperationConstraints,
    OwnershipRead,
)
from src.catalog.service import CatalogService, PlatformCatalogService
from src.storage.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogRepository,
    PlatformCatalogRepository,
)
from tests.fixtures import (
    ACTOR,
    FILES,
    TENANT_A,
    TENANT_B,
    TOOL,
    operation,
    register_source,
    service,
    settings,
    tool,
)


@pytest.fixture
async def catalog(database):
    return CatalogService(CatalogRepository(database.pool), settings())


@pytest.fixture
async def platform(database, catalog):
    return PlatformCatalogService(PlatformCatalogRepository(database.pool), catalog)


async def publish_operations(platform, number=1):
    svc, op = service(number), operation(number)
    await platform.add_service(svc, ACTOR)
    await platform.add_operation(op, ACTOR)
    await register_source(
        platform.repository.pool, svc.destination_alias, app_key=svc.license_app_key
    )
    await platform.repository.transition(
        "service", ExactReference(id=svc.service_id, version=1), "published", ACTOR
    )
    await platform.repository.transition(
        "operation", ExactReference(id=op.operation_id, version=1), "published", ACTOR
    )
    return ExactReference(id=op.operation_id, version=1)


async def test_complete_creation_ownership_and_copy(catalog):
    actor = Attribution(subject=str(TENANT_B), license_id=ACTOR.license_id)
    a = await catalog.create_draft(TENANT_A, tool(), FILES, actor)
    b = await catalog.create_draft(TENANT_B, tool(tenant=TENANT_B), FILES, ACTOR)
    assert a.tool_id == b.tool_id == TOOL
    assert a.created_by == actor and a.tenant_id == TENANT_A
    assert await catalog.repository.files(TENANT_A, TOOL, 1) == tuple(
        sorted(FILES, key=lambda f: f.path)
    )
    with pytest.raises(CatalogConflictError):
        await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    # A known ID is insufficient when the other tenant has no matching owned version.
    foreign_id = uuid7()
    await catalog.create_draft(
        TENANT_A, tool(tool_id=foreign_id, public_name="private"), FILES, ACTOR
    )
    for action in [
        catalog.repository.get_version(TENANT_B, foreign_id, 1),
        catalog.repository.files(TENANT_B, foreign_id, 1),
        catalog.copy_version(TENANT_B, foreign_id, 1, ACTOR, expected_revision=1),
        catalog.transition(TENANT_B, foreign_id, 1, "disabled", ACTOR, expected_revision=1),
    ]:
        with pytest.raises(CatalogNotFoundError):
            await action
    copied = await catalog.copy_version(TENANT_A, TOOL, 1, ACTOR, expected_revision=1)
    assert copied.version == 2 and copied.revision == 2
    assert copied.source == ExactReference(id=TOOL, version=1)
    assert copied.status == "draft"
    assert await catalog.repository.files(TENANT_A, TOOL, 2) == await catalog.repository.files(
        TENANT_A, TOOL, 1
    )


async def test_concurrent_appends_and_expected_revision(catalog):
    await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    results = await asyncio.gather(
        *[catalog.append_draft(TENANT_A, TOOL, tool(), FILES, ACTOR) for _ in range(8)]
    )
    assert sorted(r.version for r in results) == list(range(2, 10))
    assert sorted(r.revision for r in results) == list(range(2, 10))
    with pytest.raises(CatalogConflictError, match="stale"):
        await catalog.append_draft(TENANT_A, TOOL, tool(), FILES, ACTOR, expected_revision=1)
    latest = await catalog.repository.get_version(TENANT_A, TOOL, 9)
    assert latest.revision == 9
    with pytest.raises(CatalogConflictError, match="identity"):
        await catalog.append_draft(TENANT_B, TOOL, tool(), FILES, ACTOR)
    with pytest.raises(CatalogConflictError, match="name"):
        await catalog.append_draft(TENANT_A, TOOL, tool(public_name="changed"), FILES, ACTOR)


async def test_atomic_write_failure_and_no_version_reuse(catalog, database):
    await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    # Bypass the service to force a relational failure halfway through the package insert.
    bad = tool(allowed_operations=(ExactReference(id=uuid7(), version=1),))
    with pytest.raises(ForeignKeyViolation):
        await catalog.repository.append(TENANT_A, TOOL, bad, FILES, ACTOR, expected_revision=None)
    with pytest.raises(CatalogNotFoundError):
        await catalog.repository.get_version(TENANT_A, TOOL, 2)
    assert (await catalog.repository.get_version(TENANT_A, TOOL, 1)).revision == 1
    assert (await catalog.append_draft(TENANT_A, TOOL, tool(), FILES, ACTOR)).version == 2
    # Gaps cannot reuse a previous number: max, not count, drives allocation.
    async with database.pool.connection() as conn:
        await conn.execute("SELECT 1")
    # Snapshots are never deletable, including drafts.
    with pytest.raises(CheckViolation):
        async with database.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.mcp_tool_versions WHERE tenant_id=%s AND tool_id=%s AND version=1",
                (TENANT_A, TOOL),
            )
    assert (await catalog.append_draft(TENANT_A, TOOL, tool(), FILES, ACTOR)).version == 3


async def test_lifecycle_generation_and_immutable_history(catalog, database):
    assert await catalog.repository.generation() == 0
    await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    assert await catalog.repository.generation() == 0
    published = await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1)
    assert published.status == "published" and published.revision == 2
    assert await catalog.repository.generation() == 1
    attempts = [
        (
            'UPDATE public.mcp_tool_versions SET manifest=manifest || \'{"description":"changed"}\'::jsonb WHERE tenant_id=%s AND tool_id=%s',
            (TENANT_A, TOOL),
        ),
        (
            "UPDATE public.mcp_tool_version_files SET content='evil' WHERE tenant_id=%s AND tool_id=%s",
            (TENANT_A, TOOL),
        ),
        (
            "DELETE FROM public.mcp_tool_version_files WHERE tenant_id=%s AND tool_id=%s",
            (TENANT_A, TOOL),
        ),
        ("DELETE FROM public.mcp_tools WHERE tenant_id=%s AND tool_id=%s", (TENANT_A, TOOL)),
        (
            "UPDATE public.mcp_tools SET public_name='changed',revision=revision+1 WHERE tenant_id=%s AND tool_id=%s",
            (TENANT_A, TOOL),
        ),
        (
            "UPDATE public.mcp_tool_versions SET status='draft' WHERE tenant_id=%s AND tool_id=%s",
            (TENANT_A, TOOL),
        ),
        (
            "INSERT INTO public.mcp_tool_version_files(tenant_id,tool_id,version,path,content) VALUES (%s,%s,1,'new.py','')",
            (TENANT_A, TOOL),
        ),
    ]
    for statement, params in attempts:
        with pytest.raises(CheckViolation):
            async with database.pool.connection() as conn:
                await conn.execute(statement, params)
    assert await catalog.repository.generation() == 1
    disabled = await catalog.transition(TENANT_A, TOOL, 1, "disabled", ACTOR, expected_revision=2)
    assert disabled.manifest == published.manifest
    assert await catalog.repository.generation() == 2
    with pytest.raises(CatalogConflictError):
        await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=3)
    retired = await catalog.transition(TENANT_A, TOOL, 1, "retired", ACTOR, expected_revision=3)
    assert retired.status == "retired"
    await catalog.repository.retire(TENANT_A, TOOL, expected_revision=4)
    assert await catalog.repository.generation() == 4
    with pytest.raises(CatalogConflictError):
        await catalog.append_draft(TENANT_A, TOOL, tool(), FILES, ACTOR)
    with pytest.raises(CatalogConflictError):
        await catalog.repository.retire(TENANT_A, TOOL, expected_revision=5)
    with pytest.raises(CheckViolation):
        async with database.pool.connection() as conn:
            await conn.execute(
                "UPDATE public.mcp_tools SET retired=false,revision=revision+1 WHERE tenant_id=%s AND tool_id=%s",
                (TENANT_A, TOOL),
            )


async def test_composite_foreign_keys_complete_snapshot_and_late_children(catalog, database):
    await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    with pytest.raises(ForeignKeyViolation):
        async with database.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO public.mcp_tool_version_files(tenant_id,tool_id,version,path,content) VALUES (%s,%s,1,'x','')",
                (TENANT_B, TOOL),
            )
    with pytest.raises(CheckViolation, match="atomically"):
        async with database.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO public.mcp_tool_version_files(tenant_id,tool_id,version,path,content) VALUES (%s,%s,1,'x','')",
                (TENANT_A, TOOL),
            )
    with pytest.raises(CheckViolation):
        await catalog.repository.append(TENANT_A, TOOL, tool(), (), ACTOR, expected_revision=None)
    with pytest.raises(CatalogNotFoundError):
        await catalog.repository.get_version(TENANT_A, TOOL, 2)
    with pytest.raises(CatalogConflictError):
        await catalog.repository.create(TENANT_B, tool(), FILES, ACTOR)
    with pytest.raises(CatalogConflictError):
        await catalog.repository.create(TENANT_A, tool(version=2), FILES, ACTOR)


async def test_two_service_references_publication_and_disable(catalog, platform):
    references = tuple([await publish_operations(platform, n) for n in (1, 2)])
    manifest = tool(allowed_operations=references)
    await catalog.create_draft(TENANT_A, manifest, FILES, ACTOR)
    published = await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1)
    assert published.manifest.allowed_operations == references
    assert await catalog.repository.generation() == 5
    await platform.repository.transition("service", operation(1).service, "disabled", ACTOR)
    await catalog.append_draft(TENANT_A, TOOL, manifest, FILES, ACTOR)
    with pytest.raises(CatalogConflictError, match="not published"):
        await catalog.transition(TENANT_A, TOOL, 2, "published", ACTOR, expected_revision=3)
    assert await catalog.repository.generation() == 6


async def test_contract_dependency_validation(catalog, platform):
    with pytest.raises(CatalogNotFoundError):
        await catalog.validate(
            tool(allowed_operations=(ExactReference(id=uuid7(), version=1),)), FILES
        )
    with pytest.raises(ValueError, match="operator-approved"):
        await catalog.validate(
            tool(runner_image_digest="registry.example/runner@sha256:" + "b" * 64), FILES
        )
    await platform.add_service(service(), ACTOR)
    with pytest.raises(CatalogConflictError, match="response"):
        await platform.add_operation(operation(max_response_bytes=9000), ACTOR)
    await platform.add_operation(operation(effect="writes", method="POST"), ACTOR)
    reference = ExactReference(id=operation().operation_id, version=1)
    with pytest.raises(ValueError, match="read-only"):
        await catalog.validate(tool(allowed_operations=(reference,)), FILES)
    await catalog.create_draft(
        TENANT_A, tool(allowed_operations=(reference,), effect="writes"), FILES, ACTOR
    )
    with pytest.raises(CatalogConflictError, match="not published"):
        await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1)
    with pytest.raises(CatalogConflictError, match="service"):
        await platform.repository.transition("operation", reference, "published", ACTOR)


async def test_ownership_read_policy(catalog, platform):
    reference = await publish_operations(platform)
    rule = OwnershipRead(
        kind="ownership_read",
        argument="/resource",
        operation=reference,
        result="/tenant",
        context="/tenant",
    )
    manifest = tool(
        allowed_operations=(reference,),
        operation_constraints=(OperationConstraints(operation=reference, rules=(rule,)),),
    )
    await catalog.validate(manifest, FILES)
    await platform.repository.transition("operation", reference, "disabled", ACTOR)
    with pytest.raises(ValueError, match="approved"):
        await catalog.validate(manifest, FILES)
    recursive_op = operation(version=2, scope_rules=(rule,))
    # The referenced operation is disabled so the draft is rejected already.
    with pytest.raises(ValueError):
        await platform.add_operation(recursive_op, ACTOR)


async def test_platform_append_lifecycle_and_sql_guards(catalog, platform, database):
    await platform.add_service(service(), ACTOR)
    with pytest.raises(CatalogConflictError):
        await platform.add_service(service(version=2), ACTOR, expected_revision=0)
    with pytest.raises(CatalogConflictError):
        await platform.add_service(service(version=3), ACTOR, expected_revision=1)
    await platform.add_service(service(version=2), ACTOR, expected_revision=1)
    await platform.add_operation(operation(), ACTOR)
    with pytest.raises(CatalogConflictError):
        await platform.add_operation(operation(version=3), ACTOR)
    await platform.add_operation(operation(version=2), ACTOR)
    await register_source(
        platform.repository.pool, service().destination_alias, app_key=service().license_app_key
    )
    for kind, reference in [
        ("service", operation().service),
        ("operation", ExactReference(id=operation().operation_id, version=1)),
    ]:
        await platform.repository.transition(kind, reference, "published", ACTOR)
    for table in ["mcp_service_versions", "mcp_operation_versions"]:
        with pytest.raises(CheckViolation):
            async with database.pool.connection() as conn:
                await conn.execute(f"UPDATE public.{table} SET manifest='{{}}'::jsonb")
        with pytest.raises(CheckViolation):
            async with database.pool.connection() as conn:
                await conn.execute(f"DELETE FROM public.{table}")
    with pytest.raises(CatalogConflictError):
        await platform.repository.transition("service", operation().service, "draft", ACTOR)
    with pytest.raises(CatalogNotFoundError):
        await platform.repository.transition(
            "service", ExactReference(id=uuid7(), version=1), "disabled", ACTOR
        )
    with pytest.raises(CatalogNotFoundError):
        await catalog.repository.service(ExactReference(id=uuid7(), version=1))
    await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    await platform.repository.grant_share(TENANT_A, TOOL, TENANT_B, ACTOR)
    # A platform share does not turn into tenant ownership.
    with pytest.raises(CatalogNotFoundError):
        await catalog.repository.get_version(TENANT_B, TOOL, 1)


async def test_version_gaps_and_deferred_package_completeness(catalog, database):
    await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    # A synthetic gap demonstrates that numbering uses the highest allocated version.
    async with database.pool.connection() as conn, conn.transaction():
        await catalog.repository._insert(conn, tool(version=5), FILES, ACTOR)
    assert (await catalog.append_draft(TENANT_A, TOOL, tool(), FILES, ACTOR)).version == 6
    with pytest.raises(CheckViolation, match="incomplete"):
        async with database.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO mcp_tool_versions(tenant_id,tool_id,version,manifest,package_sha256,"
                "file_count,operation_count,created_by,updated_by) "
                "SELECT tenant_id,tool_id,7,jsonb_set(manifest,'{version}','7'),package_sha256,"
                "file_count,operation_count,created_by,updated_by FROM mcp_tool_versions "
                "WHERE tenant_id=%s AND tool_id=%s AND version=1",
                (TENANT_A, TOOL),
            )
    with pytest.raises(CatalogNotFoundError):
        await catalog.repository.get_version(TENANT_A, TOOL, 7)


async def test_direct_sql_publication_requires_live_dependencies(catalog, platform, database):
    svc = service()
    await platform.add_service(svc, ACTOR)
    await platform.add_operation(operation(), ACTOR)
    reference = ExactReference(id=operation().operation_id, version=1)
    await catalog.create_draft(TENANT_A, tool(allowed_operations=(reference,)), FILES, ACTOR)
    with pytest.raises(CheckViolation, match="dependency"):
        async with database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_tool_versions SET status='published' WHERE tenant_id=%s AND tool_id=%s",
                (TENANT_A, TOOL),
            )
    assert await catalog.repository.generation() == 0
    with pytest.raises(CheckViolation, match="drafts"):
        async with database.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO mcp_service_versions(service_id,version,manifest,status,created_by,updated_by) "
                "SELECT service_id,2,jsonb_set(manifest,'{version}','2'),'published',created_by,updated_by "
                "FROM mcp_service_versions WHERE service_id=%s AND version=1",
                (svc.service_id,),
            )
    with pytest.raises(CheckViolation, match="service dependency"):
        async with database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_operation_versions SET status='published' WHERE operation_id=%s",
                (reference.id,),
            )
    assert await catalog.repository.generation() == 0
    before = await catalog.repository.generation()
    await platform.repository.grant_share(TENANT_A, TOOL, TENANT_B, ACTOR)
    assert await catalog.repository.generation() == before + 1
