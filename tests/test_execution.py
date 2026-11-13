"""Disposable PostgreSQL and test-only backends. These are not Kata acceptance evidence."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid7

import httpx
import pytest
from psycopg.errors import RaiseException

from runner.protocol import encode
from src.auth.licenses import LicenseIdentity
from src.auth.principal import Principal
from src.broker.client import Broker
from src.catalog.models import ExactReference, ToolLimits
from src.catalog.resolver import CatalogResolver
from src.catalog.service import CatalogService, PlatformCatalogService
from src.catalog.submission import SubmissionError
from src.credentials.resolver import FileCredentialResolver
from src.execution.backend import Pod
from src.execution.controller import Controller
from src.execution.kata import pod_manifest, pod_name
from src.execution.models import ExecutionDeniedError, OutcomeUnknownError, QueueFullError
from src.execution.recovery import Recovery
from src.execution.scheduler import Scheduler
from src.execution.service import ExecutionService
from src.sources.registry import SourceRegistry
from src.storage.bindings import BindingRepository
from src.storage.bindings_ensure import MAX_LIMITS
from src.storage.catalog import (
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogRepository,
    PlatformCatalogRepository,
)
from src.storage.executions import ExecutionRepository
from src.toolkits.models import (
    BindingSpec,
    BoundContext,
    CredentialProfile,
    CredentialReference,
    ToolkitReference,
    ToolReference,
    ToolSelection,
)
from src.toolkits.service import AccessService
from tests.fixtures import (
    ACTOR,
    FILES,
    IMAGE,
    TENANT_A,
    TENANT_B,
    TOOL,
    operation,
    register_source,
    service,
    settings,
    tool,
)
from tests.test_bindings import LICENSE, PRINCIPAL, profile, toolkit


async def setup_runtime(
    database,
    *,
    writes=False,
    tool_limits: ToolLimits | None = None,
    binding_limits: ToolLimits | None = None,
    **overrides,
):
    config = settings(
        execution_enabled=True,
        execution_global_slots=2,
        license_introspect_url="https://licenses.example/introspect",
        license_app_key="mcp",
        source_allowed_hosts=("*.example",),
        kata_runtime_class="kata-qemu",
        runner_image_digest=IMAGE,
        **overrides,
    )
    catalog = CatalogService(CatalogRepository(database.pool), config)
    platform = PlatformCatalogService(PlatformCatalogRepository(database.pool), catalog)
    refs = []
    credentials = []
    for number in (1, 2):
        contract = service(number)
        await platform.add_service(contract, ACTOR)
        await register_source(
            database.pool,
            contract.destination_alias,
            app_key=contract.license_app_key,
            base_url=f"https://service{number}.example",
        )
        await platform.repository.transition(
            "service", ExactReference(id=contract.service_id, version=1), "published", ACTOR
        )
        op = operation(
            number,
            response_projection=("",),
            method="POST" if writes else "GET",
            effect="writes" if writes else "read_only",
        )
        await platform.add_operation(op, ACTOR)
        ref = ExactReference(id=op.operation_id, version=1)
        await platform.repository.transition("operation", ref, "published", ACTOR)
        refs.append(ref)
        credentials.append(
            CredentialReference(
                service=contract.service_key,
                alias=f"alias_{number}",
                license_id=uuid7(),
                app_key=contract.license_app_key,
            )
        )
    limits = ToolLimits() if tool_limits is None else tool_limits
    manifest = tool(
        allowed_operations=tuple(refs),
        effect="writes" if writes else "read_only",
        limits=limits,
    )
    await catalog.create_draft(TENANT_A, manifest, FILES, ACTOR)
    await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1)
    access = AccessService(BindingRepository(database.pool), config)
    credential = profile(services=tuple(credentials))
    await access.profile_write(PRINCIPAL, credential)
    # The selection carries the tool's own limits, as the platform seeder and the tenant builder
    # write them; only the binding's ceiling narrows them.
    kit = toolkit(
        tools=(
            ToolSelection(
                tool=ToolReference(tenant_id=TENANT_A, id=TOOL, version=1), limits=limits
            ),
        )
    )
    await access.toolkit_write(PRINCIPAL, kit)
    await access.toolkit_transition(PRINCIPAL, kit.toolkit_id, 1, "published", 1)
    spec = BindingSpec(
        credential_profile_id=credential.profile_id,
        toolkits=(ToolkitReference(id=kit.toolkit_id, version=1),),
        context=BoundContext(tenant_id=TENANT_A),
        limits=ToolLimits() if binding_limits is None else binding_limits,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    binding, token = await access.binding_write(PRINCIPAL, spec)
    principal = Principal(TENANT_A, LICENSE, "test-caller", binding.binding_id, binding.revision)
    resolver = CatalogResolver(access)
    resolved = (await resolver.resolve(principal))[0]
    repository = ExecutionRepository(database.pool, config)
    execution = ExecutionService(repository, resolver)
    async with database.pool.connection() as conn:
        await conn.execute("INSERT INTO mcp_execution_slots(slot_id) SELECT generate_series(1,2)")
    return SimpleNamespace(
        config=config,
        catalog=catalog,
        platform=platform,
        access=access,
        credential=credential,
        binding=binding,
        token=token,
        principal=principal,
        resolved=resolved,
        repository=repository,
        execution=execution,
        scheduler=Scheduler(repository),
        refs=refs,
        database=database,
    )


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database)


async def enqueue(runtime, key=None):
    result = await runtime.execution.submit(runtime.principal, runtime.resolved, {}, key)
    return await runtime.repository.get(TENANT_A, result["run_id"])


async def claim(runtime):
    await enqueue(runtime)
    return await runtime.scheduler.claim(uuid7())


async def expire(runtime, row):
    async with runtime.database.pool.connection() as conn:
        await conn.execute(
            "UPDATE mcp_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (row["run_id"],),
        )


async def test_durable_idempotency_and_strict_admission(runtime):
    results = await asyncio.gather(
        *(
            runtime.execution.submit(runtime.principal, runtime.resolved, {}, "same-key")
            for _ in range(8)
        )
    )
    assert len({r["run_id"] for r in results}) == 1
    row = await runtime.repository.get(TENANT_A, results[0]["run_id"])
    assert row["binding_revision"] == 1 and row["inputs"] == {} and runtime.token not in repr(row)
    # Conflicting normalized input is rejected by the durable boundary too.
    with pytest.raises(CatalogConflictError):
        await runtime.repository.accept(
            runtime.principal,
            runtime.resolved,
            {"different": 1},
            "same-key",
            await runtime.catalog.repository.generation(),
        )
    with pytest.raises(ValueError):
        await runtime.execution.submit(
            runtime.principal, runtime.resolved, {"tenant_id": "override"}, None
        )
    with pytest.raises(ValueError):
        await runtime.execution.submit(runtime.principal, runtime.resolved, {}, "bad key")
    with pytest.raises(CatalogNotFoundError):
        await runtime.repository.get(TENANT_B, row["run_id"])
    with pytest.raises(PermissionError):
        await runtime.execution.inspect(
            Principal(TENANT_A, LICENSE, "other", uuid7(), 1), row["run_id"]
        )
    assert (await runtime.execution.inspect(PRINCIPAL, row["run_id"]))["run_id"] == row["run_id"]
    events = await runtime.repository.events(TENANT_A, row["run_id"], 0, 10)
    assert [e["kind"] for e in events] == ["queued"]
    assert await runtime.repository.events(TENANT_B, row["run_id"], 0, 10) == []


async def test_queue_overload_and_unavailable(runtime):
    runtime.repository.settings = runtime.config.model_copy(update={"queue_per_tenant_max": 1})
    await enqueue(runtime)
    with pytest.raises(QueueFullError):
        await enqueue(runtime)
    runtime.execution.settings = runtime.config.model_copy(update={"execution_enabled": False})
    with pytest.raises(SubmissionError):
        await enqueue(runtime)


async def test_atomic_generation_and_binding_acceptance(runtime):
    with pytest.raises(CatalogConflictError):
        await runtime.repository.accept(runtime.principal, runtime.resolved, {}, None, -1)
    await runtime.access.binding_action(PRINCIPAL, runtime.binding.binding_id, 1, revoke=True)
    with pytest.raises(ExecutionDeniedError):
        await runtime.repository.accept(
            runtime.principal,
            runtime.resolved,
            {},
            None,
            await runtime.catalog.repository.generation(),
        )


async def test_global_and_tenant_capacity_across_workers(runtime):
    await runtime.scheduler.verify_capacity()
    await asyncio.gather(*(enqueue(runtime) for _ in range(6)))
    claims = await asyncio.gather(*(runtime.scheduler.claim(uuid7()) for _ in range(5)))
    assert len([c for c in claims if c]) == 1  # per-tenant ceiling, despite two global slots
    row = next(c for c in claims if c)
    async with runtime.database.pool.connection() as conn:
        assert (
            len(
                await (
                    await conn.execute("SELECT * FROM mcp_execution_slots WHERE run_id IS NOT NULL")
                ).fetchall()
            )
            == 1
        )
    await runtime.repository.finish(row, "failed", code="test_failure", absent=False)
    assert await runtime.scheduler.claim(uuid7()) is None
    # Quarantine survives process restart and still consumes tenant/global capacity.
    await expire(runtime, row)
    backend = FakeBackend()
    await Recovery(runtime.repository, backend).reconcile(uuid7())
    assert await runtime.scheduler.claim(uuid7()) is not None


async def test_two_tenants_share_global_slots_and_receive_fair_claims(runtime):
    await runtime.catalog.create_draft(TENANT_B, tool(tenant=TENANT_B), FILES, ACTOR)
    await runtime.catalog.transition(TENANT_B, TOOL, 1, "published", ACTOR, expected_revision=1)
    principal = Principal(TENANT_B, uuid7(), "other-operator", management=True)
    credential = CredentialProfile(
        profile_id=uuid7(),
        tenant_id=TENANT_B,
        name="other_profile",
        gateway=CredentialReference(
            service="mcp", alias="other_gateway", license_id=principal.license_id, app_key="mcp"
        ),
    )
    await runtime.access.profile_write(principal, credential)
    kit = toolkit(
        TENANT_B, tools=(ToolSelection(tool=ToolReference(tenant_id=TENANT_B, id=TOOL, version=1)),)
    )
    await runtime.access.toolkit_write(principal, kit)
    await runtime.access.toolkit_transition(principal, kit.toolkit_id, 1, "published", 1)
    binding, _ = await runtime.access.binding_write(
        principal,
        BindingSpec(
            credential_profile_id=credential.profile_id,
            toolkits=(ToolkitReference(id=kit.toolkit_id, version=1),),
            context=BoundContext(tenant_id=TENANT_B),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    caller = Principal(TENANT_B, principal.license_id, "other-caller", binding.binding_id, 1)
    resolved = (await runtime.execution.resolver.resolve(caller))[0]
    for _ in range(2):
        await enqueue(runtime)
        await runtime.execution.submit(caller, resolved, {}, None)
    rows = await asyncio.gather(*(runtime.scheduler.claim(uuid7()) for _ in range(4)))
    claimed = [r for r in rows if r]
    assert {r["tenant_id"] for r in claimed} == {TENANT_A, TENANT_B} and len(claimed) == 2
    for row in claimed:
        await runtime.repository.finish(row, "failed", code="fixture_done", absent=True)
    again = await asyncio.gather(runtime.scheduler.claim(uuid7()), runtime.scheduler.claim(uuid7()))
    assert {r["tenant_id"] for r in again} == {TENANT_A, TENANT_B}


async def test_cancel_queue_deadline_and_immutable_snapshots(runtime):
    first = await enqueue(runtime)
    await runtime.repository.cancel(TENANT_A, first["run_id"])
    assert await runtime.scheduler.claim(uuid7()) is None
    assert (await runtime.repository.get(TENANT_A, first["run_id"]))["status"] == "cancelled"
    with pytest.raises(RaiseException):
        async with runtime.database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_runs SET inputs='{}'::jsonb || '{\"x\":1}'::jsonb WHERE run_id=%s",
                (first["run_id"],),
            )
    with pytest.raises(RaiseException):
        async with runtime.database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_runs SET status='queued' WHERE run_id=%s", (first["run_id"],)
            )
    row = await enqueue(runtime)
    # Snapshot deadlines are immutable: exercise an already expired acceptance via a short wait.
    with pytest.raises(RaiseException):
        async with runtime.database.pool.connection() as conn:
            await conn.execute("DELETE FROM mcp_runs WHERE run_id=%s", (row["run_id"],))


async def test_queue_deadline_is_the_narrowed_tool_limits(database):
    """The accepted run's queue deadline is the tool's own queue budget, end to end.

    bindings/ensure writes a binding whose ceiling is MAX_LIMITS; a tool declaring 240 s gets the
    full 240 s, and the same shape with a tool declaring 60 s stays 60 s (the deadline follows
    the narrowed manifest, not a constant).
    """
    runtime = await setup_runtime(
        database, tool_limits=ToolLimits(queue_seconds=240), binding_limits=MAX_LIMITS
    )
    row = await enqueue(runtime)
    assert round((row["queue_deadline"] - row["created_at"]).total_seconds()) == 240

    narrow_id = uuid7()
    narrow = tool(
        TENANT_A,
        narrow_id,
        public_name="synthetic_narrow",
        limits=ToolLimits(queue_seconds=60),
    )
    await runtime.catalog.create_draft(TENANT_A, narrow, FILES, ACTOR)
    await runtime.catalog.transition(
        TENANT_A, narrow_id, 1, "published", ACTOR, expected_revision=1
    )
    kit = toolkit(
        name="synthetic_narrow_toolkit",
        tools=(
            ToolSelection(
                tool=ToolReference(tenant_id=TENANT_A, id=narrow_id, version=1),
                limits=narrow.limits,
            ),
        ),
    )
    await runtime.access.toolkit_write(PRINCIPAL, kit)
    await runtime.access.toolkit_transition(PRINCIPAL, kit.toolkit_id, 1, "published", 1)
    binding, _ = await runtime.access.binding_write(
        PRINCIPAL,
        BindingSpec(
            credential_profile_id=runtime.credential.profile_id,
            toolkits=(ToolkitReference(id=kit.toolkit_id, version=1),),
            context=BoundContext(tenant_id=TENANT_A),
            limits=MAX_LIMITS,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    caller = Principal(TENANT_A, LICENSE, "narrow-caller", binding.binding_id, binding.revision)
    resolved = (await runtime.execution.resolver.resolve(caller))[0]
    result = await runtime.execution.submit(caller, resolved, {}, None)
    narrow_row = await runtime.repository.get(TENANT_A, result["run_id"])
    assert round((narrow_row["queue_deadline"] - narrow_row["created_at"]).total_seconds()) == 60


async def test_live_disable_rotation_profile_and_fencing(runtime):
    row = await claim(runtime)
    await runtime.repository.heartbeat(row["run_id"], row["worker_id"], row["fence"])
    effective, _ = await runtime.execution.live(row)
    assert effective.tool == runtime.resolved.effective.tool
    rotated, _ = await runtime.access.binding_action(PRINCIPAL, runtime.binding.binding_id, 1)
    assert (await runtime.execution.live(row))[0].tool == effective.tool
    await runtime.catalog.transition(TENANT_A, TOOL, 1, "disabled", ACTOR, expected_revision=2)
    with pytest.raises((ValueError, PermissionError)):
        await runtime.execution.live(row)
    await expire(runtime, row)
    with pytest.raises(ExecutionDeniedError):
        await runtime.repository.heartbeat(row["run_id"], row["worker_id"], row["fence"])


class FakeChannel:
    def __init__(self, backend, frames):
        self.backend, self.frames = backend, list(frames)
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def receive(self):
        if self.frames:
            return self.frames.pop(0)
        self.backend.finished = True
        return None

    async def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, frames=None, confirm=True, exit_code=0):
        self.pods = {}
        self.finished = False
        self.confirm = confirm
        self.exit_code = exit_code
        frames = (
            frames
            if frames is not None
            else [
                (
                    encode({"type": "ready", "protocol": 1}, 65536)
                    + encode({"type": "result", "result": {}}, 65536),
                    b"",
                )
            ]
        )
        self.channel = FakeChannel(self, frames)

    async def ready(self):
        pass

    async def start(self, row, limits):
        pod = Pod(pod_name(row), str(uuid7()), str(row["run_id"]), row["fence"], "Running")
        self.pods[pod.name] = pod
        return pod

    async def inspect(self, name):
        pod = self.pods.get(name)
        if pod and self.finished:
            return Pod(
                pod.name,
                pod.uid,
                pod.run_id,
                pod.fence,
                "Succeeded" if self.exit_code == 0 else "Failed",
                self.exit_code,
            )
        return pod

    async def attach(self, pod):
        return self.channel

    async def stop(self, pod):
        if self.confirm:
            self.pods.pop(pod.name, None)
        return self.confirm

    async def list(self):
        return list(self.pods.values())


async def make_broker(runtime, tmp_path, handler=None):
    refs = (runtime.credential.gateway,) + runtime.credential.services
    tokens = {ref.service: "synthetic_" + ref.service for ref in refs}
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "tenant_id": str(TENANT_A),
                        "profile_id": str(runtime.credential.profile_id),
                        **ref.model_dump(mode="json", exclude={"bundle"}),
                        "token": tokens[ref.service],
                    }
                    for ref in refs
                ],
            }
        )
    )
    path.chmod(0o600)

    class Licenses:
        async def authenticate(self, token, app):
            ref = next(r for r in refs if tokens[r.service] == token)
            assert app == ref.app_key
            return LicenseIdentity(TENANT_A, ref.license_id, "test", float("inf"))

    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={}) if handler is None else handler(request)

    broker = Broker(
        runtime.execution,
        runtime.catalog.repository,
        FileCredentialResolver(path),
        Licenses(),
        SourceRegistry(runtime.database.pool, runtime.config),
        httpx.AsyncClient(transport=httpx.MockTransport(upstream), trust_env=False),
    )
    return broker, seen, tokens, path


async def test_two_application_broker_and_budget(runtime, tmp_path):
    row = await claim(runtime)
    broker, seen, tokens, path = await make_broker(runtime, tmp_path)
    try:
        await broker.preflight(row)
        for ref in runtime.refs:
            assert await broker.call(row, str(ref.id), {}) == {}
        assert [r.headers["Authorization"] for r in seen] == [
            "Bearer synthetic_service_1",
            "Bearer synthetic_service_2",
        ]
        assert [r.url.host for r in seen] == ["service1.example", "service2.example"]
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(uuid7()), {})
        async with runtime.database.pool.connection() as conn:
            journal = await (
                await conn.execute("SELECT * FROM mcp_run_api_calls ORDER BY sequence")
            ).fetchall()
        assert len(journal) == 2 and all(r["status"] == "succeeded" for r in journal)
        assert not any(token in repr(journal) for token in tokens.values())
        runtime.repository.settings = runtime.config.model_copy(update={"max_api_calls_per_run": 2})
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
        assert len(seen) == 2
        path.unlink()
        with pytest.raises(ExecutionDeniedError):
            await broker.preflight(row)
    finally:
        await broker.close()


@pytest.mark.parametrize("failure", ["redirect", "large", "bad_json", "http_error"])
async def test_broker_rejects_unsafe_responses(runtime, tmp_path, failure):
    row = await claim(runtime)

    def upstream(request):
        return {
            "redirect": httpx.Response(302, headers={"Location": "https://evil.example"}),
            "large": httpx.Response(200, content=b"x" * 10000),
            "bad_json": httpx.Response(200, content=b"NaN"),
            "http_error": httpx.Response(503),
        }[failure]

    broker, seen, _, _ = await make_broker(runtime, tmp_path, upstream)
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
        assert len(seen) == 1
    finally:
        await broker.close()


async def test_upstream_permits_are_shared_and_expire(runtime):
    row = await claim(runtime)
    runtime.repository.settings = runtime.config.model_copy(update={"api_max_inflight": 1})
    service_id = service().service_id
    permit = await runtime.repository.permit(row, service_id)
    with pytest.raises(ExecutionDeniedError):
        await ExecutionRepository(runtime.database.pool, runtime.repository.settings).permit(
            row, service_id
        )
    await runtime.repository.release_permit(row, permit)
    await runtime.repository.permit(row, service_id)


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "malformed",
        "no_ready",
        "duplicate",
        "after_final",
        "stderr",
        "failed_exit",
        "cleanup",
    ],
)
async def test_controller_protocol_exit_and_cleanup(runtime, tmp_path, scenario):
    row = await claim(runtime)
    ready = encode({"type": "ready", "protocol": 1}, 65536)
    result = encode({"type": "result", "result": {}}, 65536)
    api = encode(
        {
            "type": "api_call",
            "sequence": 1,
            "operation_id": str(runtime.refs[0].id),
            "arguments": {},
        },
        65536,
    )
    frames = {
        "success": ready + result,
        "malformed": b"oops\n",
        "no_ready": result,
        "duplicate": ready + api + api,
        "after_final": ready + result + api,
        "stderr": ready + result,
        "failed_exit": ready + result,
        "cleanup": ready + result,
    }[scenario]
    backend = FakeBackend(
        [(frames, b"x" * 9000 if scenario == "stderr" else b"")],
        confirm=scenario != "cleanup",
        exit_code=1 if scenario == "failed_exit" else 0,
    )
    broker, seen, tokens, _ = await make_broker(runtime, tmp_path)
    try:
        await Controller(
            runtime.repository, runtime.execution, broker, backend, runtime.catalog.repository
        ).execute(row)
        finished = await runtime.repository.get(TENANT_A, row["run_id"])
        assert finished["status"] == (
            "succeeded" if scenario in {"success", "cleanup"} else "interrupted"
        )
        assert backend.channel.closed
        assert not any(token in repr(backend.channel.sent) for token in tokens.values())
        assert len(seen) == (1 if scenario == "duplicate" else 0)
        async with runtime.database.pool.connection() as conn:
            slot = await (
                await conn.execute("SELECT * FROM mcp_execution_slots WHERE slot_id=1")
            ).fetchone()
        assert bool(slot["run_id"]) == (scenario == "cleanup")
        assert slot["quarantined"] == (scenario == "cleanup")
    finally:
        await broker.close()


async def test_recovery_fences_and_never_replays_mutations(runtime):
    row = await claim(runtime)
    backend = FakeBackend(confirm=False)
    pod = await backend.start(row, runtime.resolved.effective.limits)
    await runtime.repository.pod(row, pod.name, pod.uid)
    await runtime.repository.journal_intent(row, operation(), "a" * 64, None, uuid7())
    await expire(runtime, row)
    await Recovery(runtime.repository, backend).reconcile(uuid7())
    after = await runtime.repository.get(TENANT_A, row["run_id"])
    assert after["status"] == "interrupted" and after["fence"] > row["fence"]
    with pytest.raises(ExecutionDeniedError):
        await runtime.repository.journal_intent(row, operation(), "b" * 64, None, uuid7())
    backend.confirm = True
    await expire(runtime, after)
    await Recovery(runtime.repository, backend).reconcile(uuid7())
    assert not backend.pods
    async with runtime.database.pool.connection() as conn:
        assert (
            await (
                await conn.execute(
                    "SELECT count(*) AS n FROM mcp_execution_slots WHERE run_id IS NOT NULL"
                )
            ).fetchone()
        )["n"] == 0
        assert (await (await conn.execute("SELECT status FROM mcp_run_api_calls")).fetchone())[
            "status"
        ] == "outcome_unknown"


async def test_mutation_timeout_is_journalled_unknown_and_never_retried(database, tmp_path):
    runtime = await setup_runtime(database, writes=True)
    row = await claim(runtime)

    def upstream(request):
        raise httpx.ReadTimeout("synthetic ambiguous mutation")

    broker, seen, _, _ = await make_broker(runtime, tmp_path, upstream)
    try:
        with pytest.raises(OutcomeUnknownError):
            await broker.call(row, str(runtime.refs[0].id), {})
        assert len(seen) == 1
        async with runtime.database.pool.connection() as conn:
            journal = await (
                await conn.execute("SELECT status,effect FROM mcp_run_api_calls")
            ).fetchone()
        assert journal == {"status": "outcome_unknown", "effect": "writes"}
    finally:
        await broker.close()


async def test_recovery_requeues_only_before_pod_intent(runtime):
    row = await claim(runtime)
    await expire(runtime, row)
    backend = FakeBackend()
    await Recovery(runtime.repository, backend).reconcile(uuid7())
    current = await runtime.repository.get(TENANT_A, row["run_id"])
    assert current["status"] == "queued" and current["fence"] > row["fence"]
    assert not backend.pods
    assert (await runtime.scheduler.claim(uuid7()))["run_id"] == row["run_id"]


@pytest.mark.parametrize("action", ["cancel", "timeout"])
async def test_controller_cancels_hung_runner_and_enforces_deadline(
    runtime, tmp_path, action, monkeypatch
):
    runtime.repository.settings = runtime.config.model_copy(update={"heartbeat_seconds": 1})
    if action == "timeout":
        # The wall budget starts at runner ready (repository.running), separately
        # from the start-up budget the scheduler adds at claim time.
        runtime.scheduler.settings = runtime.config.model_copy(update={"run_max_seconds": 1})
        runtime.repository.settings = runtime.config.model_copy(
            update={"heartbeat_seconds": 1, "run_max_seconds": 1}
        )
    row = await claim(runtime)
    backend = FakeBackend()
    original = backend.channel.receive
    first = True

    async def blocked():
        nonlocal first
        if first:
            first = False
            return (encode({"type": "ready", "protocol": 1}, 65536), b"")
        await asyncio.sleep(30)
        return await original()

    monkeypatch.setattr(backend.channel, "receive", blocked)
    broker, _, _, _ = await make_broker(runtime, tmp_path)
    try:
        task = asyncio.create_task(
            Controller(
                runtime.repository, runtime.execution, broker, backend, runtime.catalog.repository
            ).execute(row)
        )
        if action == "cancel":
            while not backend.channel.sent:
                await asyncio.sleep(0)
            await runtime.repository.cancel(TENANT_A, row["run_id"])
        await asyncio.wait_for(task, 5)
        assert (await runtime.repository.get(TENANT_A, row["run_id"]))["status"] == (
            "cancelled" if action == "cancel" else "timed_out"
        )
        assert not backend.pods
    finally:
        await broker.close()


async def test_controller_worker_shutdown_cleans_up_then_propagates_cancel(
    runtime, tmp_path, monkeypatch
):
    row = await claim(runtime)
    backend = FakeBackend()
    original = backend.channel.receive
    first = True

    async def blocked():
        nonlocal first
        if first:
            first = False
            return (encode({"type": "ready", "protocol": 1}, 65536), b"")
        await asyncio.sleep(30)
        return await original()

    monkeypatch.setattr(backend.channel, "receive", blocked)
    broker, _, _, _ = await make_broker(runtime, tmp_path)
    try:
        task = asyncio.create_task(
            Controller(
                runtime.repository, runtime.execution, broker, backend, runtime.catalog.repository
            ).execute(row)
        )
        while not backend.channel.sent:
            await asyncio.sleep(0)
        task.cancel()  # what worker.py does to in-flight runs on shutdown
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert task.cancelled()
        finished = await runtime.repository.get(TENANT_A, row["run_id"])
        assert finished["status"] == "interrupted"
        assert finished["error"]["code"] == "worker_stopped"
        assert not backend.pods
        assert backend.channel.closed
    finally:
        await broker.close()


def test_pod_security_profile():
    from src.catalog.resolver import ResolvedTool
    from src.toolkits.models import EffectiveTool, ToolReference

    config = settings(kata_runtime_class="kata-qemu", runner_image_digest=IMAGE)
    resolved = ResolvedTool(
        tool().model_dump_json(),
        EffectiveTool(
            tool=ToolReference(tenant_id=TENANT_A, id=TOOL, version=1), public_name="synthetic_tool"
        ).model_dump_json(),
        BoundContext(tenant_id=TENANT_A).model_dump_json(),
    )
    row = {
        "run_id": uuid7(),
        "fence": 1,
        "authority": {
            "manifest": resolved.manifest_json,
            "effective": resolved.effective_json,
            "context": resolved.context_json,
        },
    }
    pod = pod_manifest(row, config, ToolLimits())
    spec = pod["spec"]
    assert spec["runtimeClassName"] == "kata-qemu" and spec["automountServiceAccountToken"] is False
    assert spec["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert "env" not in spec["containers"][0]
    assert "hostPath" not in repr(spec) and "secret" not in repr(spec)
    with pytest.raises(ExecutionDeniedError):
        pod_manifest(row, config.model_copy(update={"kata_runtime_class": None}), ToolLimits())
