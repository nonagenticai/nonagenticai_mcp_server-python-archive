"""Author test runs (mode='draft'): own tenant only, write confirmation, trace, rate limit."""

from uuid import uuid4, uuid7

import pytest

from src.catalog.models import ExactReference
from src.catalog.resolver import CatalogResolver
from src.catalog.service import CatalogService
from src.execution.models import ConfirmationRequiredError, ExecutionDeniedError
from src.execution.scheduler import Scheduler
from src.execution.service import ExecutionService
from src.storage.bindings import BindingRepository
from src.storage.catalog import CatalogNotFoundError, CatalogRepository, QuotaExceededError
from src.storage.executions import ExecutionRepository
from src.toolkits.service import AccessService
from tests.fixtures import ACTOR, FILES, IMAGE, settings, tool
from tests.test_builder_data import make_tenant_service, tool_for  # noqa: F401


async def build(database, tenant, *, effect="read_only", **overrides):
    config = settings(
        execution_enabled=True,
        execution_global_slots=2,
        runner_image_digest=IMAGE,
        license_introspect_url="https://licenses.example/introspect",
        license_app_key="mcp",
        **overrides,
    )
    svc, ops = await make_tenant_service(database, tenant, "crm", ("list",))
    refs = tuple(ExactReference(id=o.operation_id, version=1) for o in ops.values())
    manifest = tool(tenant=tenant, tool_id=uuid7(), allowed_operations=refs, effect=effect)
    catalog = CatalogService(CatalogRepository(database.pool), config)
    await catalog.create_draft(tenant, manifest, FILES, ACTOR)
    repository = ExecutionRepository(database.pool, config)
    resolver = CatalogResolver(AccessService(BindingRepository(database.pool), config))
    async with database.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO mcp_execution_slots(slot_id) SELECT generate_series(1,2) ON CONFLICT DO NOTHING"
        )
    return manifest, ExecutionService(repository, resolver), repository, ops, svc


async def test_own_draft_run_queues_without_binding(database):
    tenant, licence = uuid4(), uuid4()
    manifest, execution, repository, _, _ = await build(database, tenant)
    view = await execution.submit_draft(tenant, licence, manifest.tool_id, 1, {})
    row = await repository.get(tenant, view["run_id"])
    assert view["status"] == "queued" and view["trace"] == [] and view["mode"] == "draft"
    assert (
        row["mode"] == "draft"
        and row["binding_id"] is None
        and row["credential_profile_id"] is None
    )
    assert row["gateway_license_id"] == licence and row["owner_tenant_id"] == tenant
    assert row["profile_snapshot"]["gateway"]["license_id"] == str(licence)
    again = await execution.submit_draft(
        tenant, licence, manifest.tool_id, 1, {}, idempotency_key="k1"
    )
    same = await execution.submit_draft(
        tenant, licence, manifest.tool_id, 1, {}, idempotency_key="k1"
    )
    assert again["run_id"] == same["run_id"]
    with pytest.raises(CatalogNotFoundError):
        await execution.draft_run(uuid4(), view["run_id"])


async def test_other_tenants_tool_is_denied(database):
    tenant, other = uuid4(), uuid4()
    manifest, execution, _, _, _ = await build(database, tenant)
    with pytest.raises(CatalogNotFoundError):
        await execution.submit_draft(other, uuid4(), manifest.tool_id, 1, {})


async def test_writes_need_confirmation(database):
    tenant, licence = uuid4(), uuid4()
    manifest, execution, _, _, _ = await build(database, tenant, effect="writes")
    with pytest.raises(ConfirmationRequiredError):
        await execution.submit_draft(tenant, licence, manifest.tool_id, 1, {})
    view = await execution.submit_draft(
        tenant, licence, manifest.tool_id, 1, {}, confirm_writes=True
    )
    assert view["status"] == "queued"


async def test_invalid_inputs_and_disabled_execution(database):
    tenant = uuid4()
    manifest, execution, _, _, _ = await build(database, tenant)
    with pytest.raises(ValueError):
        await execution.submit_draft(tenant, uuid4(), manifest.tool_id, 1, {"x": 1})


async def test_test_runs_per_hour_limit(database):
    tenant, licence = uuid4(), uuid4()
    manifest, execution, _, _, _ = await build(
        database, tenant, builder_test_runs_per_hour=2, queue_per_tenant_max=10
    )
    for _ in range(2):
        await execution.submit_draft(tenant, licence, manifest.tool_id, 1, {})
    with pytest.raises(QuotaExceededError):
        await execution.submit_draft(tenant, licence, manifest.tool_id, 1, {})
    # Another tenant is unaffected by this tenant's count.
    other = uuid4()
    other_manifest, other_exec, _, _, _ = await build(database, other, builder_test_runs_per_hour=2)
    assert (await other_exec.submit_draft(other, uuid4(), other_manifest.tool_id, 1, {}))["run_id"]


async def test_claim_live_and_trace_rows(database):
    tenant, licence = uuid4(), uuid4()
    manifest, execution, repository, ops, _ = await build(database, tenant)
    view = await execution.submit_draft(tenant, licence, manifest.tool_id, 1, {})
    row = await Scheduler(repository).claim(uuid7())
    effective, profile = await execution.live(row)
    assert effective.tool.tenant_id == tenant and profile.gateway.license_id == licence
    operation = next(iter(ops.values()))
    sequence = await repository.journal_intent(
        row, operation, "a" * 64, None, row["gateway_license_id"], method="GET", path="/v1/x"
    )
    await repository.journal_outcome(
        row,
        sequence,
        "failed",
        http_status=404,
        duration_ms=12,
        reason="status",
        error_excerpt="no such customer",
    )
    shown = await execution.draft_run(tenant, view["run_id"])
    call = shown["trace"][0]
    assert (call["method"], call["path"], call["http_status"], call["reason"]) == (
        "GET",
        "/v1/x",
        404,
        "status",
    )
    assert call["duration_ms"] == 12 and call["status"] == "failed"
    assert shown["error_excerpt"] == "no such customer" == call["error_excerpt"]
    # Authority loss mid-run: a retired tool stops the run.
    async with database.pool.connection() as conn:
        await conn.execute(
            "UPDATE mcp_tool_versions SET status='disabled' WHERE tool_id=%s", (manifest.tool_id,)
        )
    with pytest.raises(ExecutionDeniedError):
        await execution.live(row)


async def test_bound_runs_are_not_draft_runs(database):
    tenant = uuid4()
    _, execution, repository, _, _ = await build(database, tenant)
    with pytest.raises(CatalogNotFoundError):
        await execution.draft_run(tenant, uuid7())
