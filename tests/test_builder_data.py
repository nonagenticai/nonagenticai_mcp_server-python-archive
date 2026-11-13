"""Tenant builder data layer on real PostgreSQL: migration 0010, definer functions, isolation."""

import asyncio
import base64
from uuid import UUID, uuid4, uuid7

import psycopg
import pytest
from alembic import command
from psycopg import sql
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from scripts.grant_runtime_role import grant, grant_execution, grant_platform_builder
from src.catalog.models import AuthAdapter, ExactReference, ServiceManifest
from src.config import PLATFORM_TENANT_ID, Settings
from src.sources.crypto import decrypt_token
from src.sources.models import TenantSourceCreate, TenantSourceUpdate, TokenUpdate
from src.sources.repository import SourceRepository, TenantSourceRepository, resolve_row
from src.storage.builder import (
    BuilderCatalogRepository,
    enforce_quota,
    usage,
)
from src.storage.catalog import (
    BuilderForbiddenError,
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogRepository,
    PlatformCatalogRepository,
    QuotaExceededError,
)
from src.storage.database import Database
from tests.conftest import migration_config
from tests.fixtures import ACTOR, FILES, operation, register_source, service, tool

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
HOSTS = ("*.example.com",)


def connect(url: str):
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1))


def sources(database, **overrides) -> TenantSourceRepository:
    options = {"max_sources": 3, "licence_hosts": HOSTS} | overrides
    return TenantSourceRepository(database.pool, SecretStr(KEY), **options)


def source_body(key="crm", **overrides) -> TenantSourceCreate:
    values = {
        "source_key": key,
        "base_url": f"https://{key}.example.com/v1",
        "auth": {"kind": "static_bearer"},
        "token": SecretStr("tok-" + key),
    } | overrides
    return TenantSourceCreate.model_validate(values)


def update_body(revision: int, **overrides) -> TenantSourceUpdate:
    values = {
        "base_url": "https://crm.example.com/v2",
        "auth": {"kind": "static_bearer"},
        "expected_revision": revision,
    } | overrides
    return TenantSourceUpdate.model_validate(values)


def tenant_service(source_key: str, number: int = 1) -> ServiceManifest:
    sid = uuid7()
    return ServiceManifest(
        service_id=sid,
        version=1,
        service_key=f"t{sid.hex[-12:]}_{source_key}"[:64],
        destination_alias=source_key,
        auth_adapter=AuthAdapter.SOURCE,
        credential_profile_key=source_key,
        max_request_bytes=4096,
        max_response_bytes=8192,
    )


def tenant_operation(svc: ServiceManifest, version: int = 1, op_id: UUID | None = None, **kw):
    return operation(
        operation_id=op_id or uuid7(),
        version=version,
        service=ExactReference(id=svc.service_id, version=svc.version),
        **kw,
    )


def tool_for(tenant: UUID, *refs: ExactReference, name="builder_tool"):
    return tool(
        tenant=tenant,
        tool_id=uuid7(),
        public_name=name,
        allowed_operations=tuple(refs),
    )


async def make_tenant_service(database, tenant: UUID, key="crm", ops=("list",)):
    """An own source, its draft service and draft operations, via the builder interfaces."""
    await sources(database).create(tenant, source_body(key), ACTOR)
    builder = BuilderCatalogRepository(database.pool)
    svc = tenant_service(key)
    await builder.create_service(tenant, svc, ACTOR)
    made = {}
    for op_key in ops:
        op = tenant_operation(svc)
        await builder.create_operation(tenant, op, ACTOR, op_key=op_key, max_operations=50)
        made[op_key] = op
    return svc, made


@pytest.fixture
def tenants() -> tuple[UUID, UUID]:
    # Licences mints UUIDv4 tenant ids: the builder must work with them.
    return uuid4(), uuid4()


# --- migration --------------------------------------------------------------------------------


async def test_upgrade_from_0009_keeps_platform_rows_valid(empty_database):
    config = migration_config(empty_database)
    command.upgrade(config, "0009_tool_sources")
    db = Database(Settings(database_url=SecretStr(empty_database), pool_min_size=0))
    await db.open()
    platform = PlatformCatalogRepository(db.pool)
    catalog = CatalogRepository(db.pool)
    svc, op = service(1), operation(1)
    tenant_tool = tool_for(
        UUID("10000000-0000-7000-8000-000000000001"), ExactReference(id=op.operation_id, version=1)
    )
    try:
        async with db.pool.connection() as conn:  # 0009 shape: no owner column yet
            await conn.execute(
                "INSERT INTO mcp_sources(source_id,source_key,base_url,auth,created_by,updated_by) "
                "VALUES (%s,'destination_1','https://service1.example',"
                '\'{"kind":"licence","app_key":"app_1"}\',\'{}\',\'{}\')',
                (uuid7(),),
            )
        await platform.add_service(svc, ACTOR)
        await platform.add_operation(op, ACTOR)
        async with db.pool.connection() as conn:  # the 0009 code path publishes with plain UPDATEs
            await conn.execute("UPDATE mcp_service_versions SET status='published'")
            await conn.execute("UPDATE mcp_operation_versions SET status='published'")
        await catalog.create(tenant_tool.tenant_id, tenant_tool, FILES, ACTOR)
    finally:
        await db.close()

    command.upgrade(config, "head")

    db = Database(Settings(database_url=SecretStr(empty_database), pool_min_size=0))
    await db.open()
    try:
        async with db.pool.connection() as conn:
            cur = await conn.execute("SELECT owner_tenant_id FROM mcp_services")
            assert {r["owner_tenant_id"] for r in await cur.fetchall()} == {PLATFORM_TENANT_ID}
            cur = await conn.execute("SELECT owner_tenant_id,spec_path FROM mcp_sources")
            assert [(r["owner_tenant_id"], r["spec_path"]) for r in await cur.fetchall()] == [
                (PLATFORM_TENANT_ID, None)
            ]
        # The pre-existing tenant tool still publishes against the platform's licence service.
        await CatalogRepository(db.pool).transition(
            tenant_tool.tenant_id, tenant_tool.tool_id, 1, "published", ACTOR, expected_revision=1
        )
        # And a tenant may now reuse the platform's source key for its own source.
        view = await sources(db).create(
            uuid4(), source_body("destination_1", base_url="https://dest.example.com"), ACTOR
        )
        assert view.source_key == "destination_1"
        assert (
            await SourceRepository(db.pool, HOSTS, None).get("destination_1")
        ).owner_tenant_id == PLATFORM_TENANT_ID
    finally:
        await db.close()


def test_database_constraints_on_sources(database_url):
    tenant = uuid4()

    def insert(conn, key="s", **values):
        columns = {
            "source_id": uuid7(),
            "owner_tenant_id": tenant,
            "source_key": key,
            "base_url": "https://api.example.com",
            "auth": psycopg.types.json.Jsonb({"kind": "none"}),
            "created_by": psycopg.types.json.Jsonb({}),
            "updated_by": psycopg.types.json.Jsonb({}),
        } | values
        names = sql.SQL(",").join(map(sql.Identifier, columns))
        conn.execute(
            sql.SQL("INSERT INTO public.mcp_sources({}) VALUES ({})").format(
                names, sql.SQL(",").join([sql.Placeholder()] * len(columns))
            ),
            list(columns.values()),
        )

    cipher = {"token_ciphertext": b"x" * 40, "token_set_at": "2026-01-01"}
    with connect(database_url) as conn:
        insert(conn, "ok_none")
        insert(
            conn,
            "ok_basic",
            auth=psycopg.types.json.Jsonb({"kind": "basic", "username": "u"}),
            **cipher,
        )
        # per-owner uniqueness: the same key under another owner is fine, under the same one is not
        insert(conn, "ok_none", owner_tenant_id=uuid4())
        for bad in (
            {"source_key": "ok_none"},
            {"base_url": "http://api.example.com"},
            {"allow_internal_http": True},
            {"ca_file": "/etc/ca.pem"},
            {"spec_path": "/a/../b"},
            {"spec_path": "no-slash"},
            {
                "auth": psycopg.types.json.Jsonb({"kind": "none"}),
                **cipher,
            },  # none carries no secret
            {
                "auth": psycopg.types.json.Jsonb({"kind": "basic", "username": "u"})
            },  # basic needs one
            {"auth": psycopg.types.json.Jsonb({"kind": "weird"})},
        ):
            with pytest.raises((CheckViolation, UniqueViolation)):
                with conn.transaction():
                    insert(conn, bad.pop("source_key", "x1"), **bad)
        # The platform keeps internal http and a private CA.
        insert(
            conn,
            "plat",
            owner_tenant_id=PLATFORM_TENANT_ID,
            base_url="http://blocked.internal",
            allow_internal_http=True,
            ca_file="/etc/ca.pem",
        )


def test_owner_and_identity_are_frozen(database_url):
    with connect(database_url) as conn:
        source_id = uuid7()
        owner = uuid4()
        conn.execute(
            "INSERT INTO mcp_sources(source_id,owner_tenant_id,source_key,base_url,auth,created_by,updated_by) "
            "VALUES (%s,%s,'s','https://a.example.com','{\"kind\":\"none\"}','{}','{}')",
            (source_id, owner),
        )
        for statement in (
            "UPDATE mcp_sources SET revision=revision+1, owner_tenant_id=gen_random_uuid()",
            "UPDATE mcp_sources SET revision=revision+1, source_key='other'",
            "DELETE FROM mcp_sources",
        ):
            with pytest.raises(CheckViolation), conn.transaction():
                conn.execute(statement)  # type: ignore[arg-type]


# --- tenant sources -----------------------------------------------------------------------------


async def test_tenant_source_lifecycle_and_isolation(database, tenants):
    a, b = tenants
    repo = sources(database)
    created = await repo.create(a, source_body("crm"), ACTOR)
    assert created.owner_tenant_id == a
    assert created.has_token and created.revision == 1
    # Same key for another tenant is independent; neither sees the other's.
    await repo.create(b, source_body("crm", base_url="https://other.example.com"), ACTOR)
    assert (await repo.get(a, "crm")).base_url == "https://crm.example.com/v1"
    assert (await repo.get(b, "crm")).base_url == "https://other.example.com"
    assert [s.source_key for s in await repo.page(a, 10, 0)] == ["crm"]
    with pytest.raises(CatalogNotFoundError):
        await repo.get(uuid4(), "crm")
    # Another tenant cannot update, rotate or disable it (it addresses its own row or none).
    with pytest.raises(CatalogNotFoundError):
        await repo.update(uuid4(), "crm", update_body(1), ACTOR)
    # Key reuse by the same tenant conflicts.
    with pytest.raises(CatalogConflictError):
        await repo.create(a, source_body("crm"), ACTOR)

    # The stored secret decrypts only for its own source id; the token is write-only.
    async with database.pool.connection() as conn:
        row = await resolve_row(conn, a, "crm")
    assert row is not None
    assert (
        decrypt_token(SecretStr(KEY), row["source_id"], row["token_ciphertext"]).get_secret_value()
        == "tok-crm"
    )
    assert "tok-crm" not in created.model_dump_json()

    updated = await repo.update(a, "crm", update_body(1), ACTOR)  # token kept
    assert (
        updated.revision == 2
        and updated.base_url == "https://crm.example.com/v2"
        and updated.has_token
    )
    with pytest.raises(CatalogConflictError):  # stale revision
        await repo.update(a, "crm", update_body(1), ACTOR)

    rotated = await repo.set_secret(
        a, "crm", TokenUpdate(token=SecretStr("new-tok"), expected_revision=2), ACTOR
    )
    assert rotated.revision == 3
    async with database.pool.connection() as conn:
        row = await resolve_row(conn, a, "crm")
    assert row is not None
    assert (
        decrypt_token(SecretStr(KEY), row["source_id"], row["token_ciphertext"]).get_secret_value()
        == "new-tok"
    )

    # A kind without a secret clears the stored one; switching back needs a new one.
    none = await repo.update(a, "crm", update_body(3, auth={"kind": "none"}), ACTOR)
    assert not none.has_token
    with pytest.raises(ValueError, match="secret"):
        await repo.update(a, "crm", update_body(4, auth={"kind": "static_bearer"}), ACTOR)
    with pytest.raises(ValueError, match="no stored secret"):
        await repo.set_secret(
            a, "crm", TokenUpdate(token=SecretStr("x"), expected_revision=4), ACTOR
        )

    disabled = await repo.set_status(a, "crm", 4, "disabled", ACTOR)
    assert disabled.status == "disabled" and disabled.revision == 5
    with pytest.raises(CatalogConflictError):
        await repo.set_status(a, "crm", 5, "disabled", ACTOR)
    assert (await repo.set_status(a, "crm", 5, "active", ACTOR)).status == "active"
    # Disabling keeps the key reserved.
    await repo.set_status(a, "crm", 6, "disabled", ACTOR)
    with pytest.raises(CatalogConflictError):
        await repo.create(a, source_body("crm"), ACTOR)

    async with database.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT event,owner_tenant_id FROM mcp_source_events WHERE source_key='crm' "
            "AND owner_tenant_id=%s ORDER BY event_id",
            (a,),
        )
        events = await cur.fetchall()
    assert [e["event"] for e in events] == [
        "created",
        "updated",
        "token_set",
        "updated",
        "disabled",
        "enabled",
        "disabled",
    ]


async def test_platform_repository_never_sees_tenant_sources(database, tenants):
    a, _ = tenants
    await sources(database).create(a, source_body("crm"), ACTOR)
    platform = SourceRepository(database.pool, HOSTS, None)
    assert await platform.page(10, 0) == []
    with pytest.raises(CatalogNotFoundError):
        await platform.get("crm")


async def test_licence_sources_only_on_operator_allowed_hosts(database, tenants):
    a, _ = tenants
    repo = sources(database)
    licence = {"kind": "licence", "app_key": "strata-rag"}
    with pytest.raises(ValueError, match="allowed host"):
        await repo.create(
            a, source_body("rag", base_url="https://evil.test", auth=licence, token=None), ACTOR
        )
    ok = await repo.create(a, source_body("rag", auth=licence, token=None), ACTOR)
    assert ok.auth.kind == "licence" and not ok.has_token
    with pytest.raises(ValueError, match="port"):
        await repo.create(a, source_body("odd", base_url="https://odd.example.com:8443"), ACTOR)


async def test_source_quota_is_exact_under_concurrency(database, tenants):
    a, b = tenants
    repo = sources(database, max_sources=3)
    results = await asyncio.gather(
        *(repo.create(a, source_body(f"s{i}"), ACTOR) for i in range(10)), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in results) == 3
    assert all(isinstance(r, QuotaExceededError) for r in results if isinstance(r, Exception))
    # Another tenant has its own budget; disabling frees a slot.
    await repo.create(b, source_body("s0"), ACTOR)
    active = [s for s in await repo.page(a, 10, 0)]
    await repo.set_status(a, active[0].source_key, active[0].revision, "disabled", ACTOR)
    await repo.create(a, source_body("again"), ACTOR)
    async with database.pool.connection() as conn:
        assert (await usage(conn, a))["sources"] == 3


async def test_platform_tenant_cannot_use_the_builder(database):
    with pytest.raises(BuilderForbiddenError):
        await sources(database).create(PLATFORM_TENANT_ID, source_body("crm"), ACTOR)


async def test_cached_specs_keep_the_last_three(database, tenants):
    a, b = tenants
    repo = sources(database)
    await repo.create(a, source_body("crm"), ACTOR)
    for i in range(5):
        await repo.put_spec(a, "crm", f"{i:064x}", {"openapi": "3.1.0", "n": i}, 100 + i)
    async with database.pool.connection() as conn:
        cur = await conn.execute("SELECT sha256 FROM mcp_source_specs ORDER BY fetched_at")
        kept = [r["sha256"] for r in await cur.fetchall()]
    assert kept == [f"{i:064x}" for i in (2, 3, 4)]
    newest = await repo.get_spec(a, "crm")
    assert newest is not None and newest["document"]["n"] == 4
    named = await repo.get_spec(a, "crm", f"{2:064x}")
    assert named is not None and named["bytes"] == 102
    assert await repo.get_spec(a, "crm", f"{0:064x}") is None
    # Re-putting the same document refreshes it without duplicating.
    await repo.put_spec(a, "crm", f"{2:064x}", {"openapi": "3.1.0", "n": 2}, 102)
    newest = await repo.get_spec(a, "crm")
    assert newest is not None and newest["sha256"] == f"{2:064x}"
    # No other tenant can read or write them.
    assert await repo.get_spec(b, "crm") is None
    with pytest.raises(CatalogNotFoundError):
        await repo.put_spec(b, "crm", "f" * 64, {}, 1)


# --- tenant services and operations -------------------------------------------------------------


async def test_service_operation_lifecycle(database, tenants):
    a, b = tenants
    svc, made = await make_tenant_service(database, a, "crm", ops=("list", "get"))
    builder = BuilderCatalogRepository(database.pool)

    record = await builder.service_for_source(a, "crm")
    assert (
        record is not None and record.status == "draft" and record.manifest.license_app_key is None
    )
    assert await builder.service_for_source(b, "crm") is None
    keyed = await builder.operations(a, "crm")
    assert [o.op_key for o in keyed] == ["get", "list"]
    assert (await builder.operation_by_key(a, "crm", "list")) is not None
    assert await builder.operation_by_key(b, "crm", "list") is None
    assert await builder.operation_by_key(a, "crm", "missing") is None

    # One service per source.
    with pytest.raises(CatalogConflictError):
        await builder.create_service(a, tenant_service("crm"), ACTOR)
    # The source must exist, be active and be the tenant's own.
    with pytest.raises(CatalogNotFoundError):
        await builder.create_service(b, tenant_service("crm"), ACTOR)
    # Builder services use the source adapter only.
    legacy = service(9, destination_alias="crm")
    with pytest.raises(ValueError, match="source adapter"):
        await builder.create_service(a, legacy, ACTOR)

    # Operations: duplicate key, bad version, foreign service, missing key.
    dup = tenant_operation(svc)
    with pytest.raises(CatalogConflictError):
        await builder.create_operation(a, dup, ACTOR, op_key="list", max_operations=50)
    with pytest.raises(CatalogNotFoundError):
        await builder.create_operation(
            b, tenant_operation(svc), ACTOR, op_key="x", max_operations=50
        )
    with pytest.raises(ValueError, match="operation key"):
        await builder.create_operation(
            a, tenant_operation(svc), ACTOR, op_key=None, max_operations=50
        )
    with pytest.raises(CatalogConflictError, match="append"):
        await builder.create_operation(
            a, tenant_operation(svc, version=2), ACTOR, op_key="n", max_operations=50
        )

    # Version 2 of an existing operation keeps its key.
    first = made["list"]
    v2 = tenant_operation(svc, version=2, op_id=first.operation_id, path_template="/records2")
    await builder.create_operation(a, v2, ACTOR, op_key=None, max_operations=50)
    assert (await builder.operation_by_key(a, "crm", "list")).version == 2  # type: ignore[union-attr]
    assert len(await builder.operations(a, "crm")) == 2

    # Publish (services first), idempotently; it cannot touch another tenant's versions.
    refs = dict(
        services=[ExactReference(id=svc.service_id, version=1)],
        operations=[ExactReference(id=o.operation_id, version=1) for o in made.values()],
    )
    with pytest.raises(CatalogNotFoundError):
        await builder.publish(b, ACTOR, **refs)
    await builder.publish(a, ACTOR, **refs)
    await builder.publish(a, ACTOR, **refs)
    record = await builder.service_for_source(a, "crm")
    assert record is not None and record.status == "published"

    # Append service version 2; the key, source and adapter are fixed.
    s2 = svc.model_copy(update={"version": 2, "max_response_bytes": 4096})
    await builder.append_service(a, s2, ACTOR, expected_revision=1)
    with pytest.raises(CatalogConflictError):
        await builder.append_service(
            a, svc.model_copy(update={"version": 3}), ACTOR, expected_revision=1
        )
    record = await builder.service_for_source(a, "crm")
    assert record is not None and record.version == 2 and record.revision == 2

    await builder.set_status(
        a, "operation", ExactReference(id=first.operation_id, version=1), "disabled", ACTOR
    )


async def test_operation_quota_counts_keyed_operations(database, tenants):
    a, b = tenants
    svc, _ = await make_tenant_service(database, a, "crm", ops=("one", "two"))
    builder = BuilderCatalogRepository(database.pool)
    with pytest.raises(QuotaExceededError):
        await builder.create_operation(
            a, tenant_operation(svc), ACTOR, op_key="three", max_operations=2
        )
    # A new version of an existing operation is not a new operation.
    existing = (await builder.operations(a, "crm"))[0]
    await builder.create_operation(
        a,
        tenant_operation(svc, version=2, op_id=existing.operation_id),
        ACTOR,
        op_key=None,
        max_operations=2,
    )
    # The platform tenant never writes through the builder.
    with pytest.raises(BuilderForbiddenError):
        await builder.create_service(PLATFORM_TENANT_ID, tenant_service("crm"), ACTOR)


async def test_tool_and_toolkit_quota_helpers(database, tenants):
    a, _ = tenants
    async with database.pool.connection() as conn, conn.transaction():
        await enforce_quota(conn, a, "tools", 1)
    await CatalogRepository(database.pool).create(a, tool_for(a), FILES, ACTOR)
    with pytest.raises(QuotaExceededError):
        async with database.pool.connection() as conn, conn.transaction():
            await enforce_quota(conn, a, "tools", 1)
    async with database.pool.connection() as conn:
        assert (await usage(conn, a)) == {"sources": 0, "operations": 0, "tools": 1, "toolkits": 0}


# --- reference rule ----------------------------------------------------------------------------


async def test_tools_may_reference_only_own_or_shared_services(database, tenants):
    a, b = tenants
    catalog = CatalogRepository(database.pool)
    platform = PlatformCatalogRepository(database.pool)
    builder = BuilderCatalogRepository(database.pool)

    own_svc, own_ops = await make_tenant_service(database, a, "crm")
    b_svc, b_ops = await make_tenant_service(database, b, "billing")
    own = ExactReference(id=own_ops["list"].operation_id, version=1)
    foreign = ExactReference(id=b_ops["list"].operation_id, version=1)

    # Platform: one licence-kind service (shared by default), one static-token service.
    await register_source(database.pool, "destination_1")
    async with database.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO mcp_sources(source_id,source_key,base_url,auth,token_ciphertext,token_set_at,"
            "created_by,updated_by) VALUES (%s,'destination_2','https://s2.example','{\"kind\":\"static_bearer\"}',"
            "%s,clock_timestamp(),'{}','{}')",
            (uuid7(), b"x" * 40),
        )
    shared, static = service(1), service(2)
    for number, svc in ((1, shared), (2, static)):
        await platform.add_service(svc, ACTOR)
        await platform.add_operation(operation(number), ACTOR)
    shared_ref = ExactReference(id=operation(1).operation_id, version=1)
    static_ref = ExactReference(id=operation(2).operation_id, version=1)

    # Own draft operations: allowed. Another tenant's: denied (database trigger, not just code).
    await catalog.create(a, tool_for(a, own), FILES, ACTOR)
    with pytest.raises(CheckViolation, match="not available"):
        await catalog.create(a, tool_for(a, foreign, name="steals"), FILES, ACTOR)
    # Licence-kind platform service: allowed without a grant. Static one: denied.
    await catalog.create(a, tool_for(a, shared_ref, name="uses_shared"), FILES, ACTOR)
    with pytest.raises(CheckViolation, match="not available"):
        await catalog.create(a, tool_for(a, static_ref, name="uses_static"), FILES, ACTOR)

    assert await catalog.may_use_service(a, ExactReference(id=shared.service_id, version=1))
    assert not await catalog.may_use_service(a, ExactReference(id=static.service_id, version=1))
    assert not await catalog.may_use_service(a, ExactReference(id=b_svc.service_id, version=1))
    assert await catalog.may_use_service(b, ExactReference(id=b_svc.service_id, version=1))
    visible = await builder.visible_platform_services(a)
    assert [(v["service_key"], v["via"]) for v in visible] == []  # nothing published yet

    # Grant -> static allowed for that tenant only.
    generation = await catalog.generation()
    await platform.grant_service(static.service_id, a, ACTOR)
    await platform.grant_service(static.service_id, a, ACTOR)  # idempotent
    assert await catalog.generation() > generation
    granted = await catalog.create(a, tool_for(a, static_ref, name="uses_static"), FILES, ACTOR)
    with pytest.raises(CheckViolation, match="not available"):
        await catalog.create(b, tool_for(b, static_ref, name="b_static"), FILES, ACTOR)

    # Publication re-checks: revoke the grant, then the draft cannot be published.
    await builder.publish(a, ACTOR, services=[ExactReference(id=own_svc.service_id, version=1)])
    for kind, ref in (
        ("service", ExactReference(id=static.service_id, version=1)),
        ("operation", static_ref),
    ):
        await platform.transition(kind, ref, "published", ACTOR)
    await platform.revoke_service(static.service_id, a)
    with pytest.raises(CheckViolation, match="not available"):
        await catalog.transition(
            a, granted.tool_id, 1, "published", ACTOR, expected_revision=granted.revision
        )
    await platform.grant_service(static.service_id, a, ACTOR)
    done = await catalog.transition(
        a, granted.tool_id, 1, "published", ACTOR, expected_revision=granted.revision
    )
    assert done.status == "published"
    assert [(v["service_key"], v["via"]) for v in await builder.visible_platform_services(a)] == [
        ("service_2", "grant")
    ]
    assert await builder.visible_platform_services(b) == []


async def test_publishing_a_tool_and_its_dependencies_is_one_transaction(database, tenants):
    a, _ = tenants
    svc, ops = await make_tenant_service(database, a, "crm")
    catalog = CatalogRepository(database.pool)
    builder = BuilderCatalogRepository(database.pool)
    ref = ExactReference(id=ops["list"].operation_id, version=1)
    created = await catalog.create(a, tool_for(a, ref), FILES, ACTOR)
    # Without publishing the dependencies the tool cannot publish; a failure leaves nothing behind.
    with pytest.raises(CatalogConflictError):
        await catalog.transition(
            a, created.tool_id, 1, "published", ACTOR, expected_revision=created.revision
        )
    async with database.pool.connection() as conn:
        try:
            async with conn.transaction():
                await builder.publish(
                    a,
                    ACTOR,
                    services=[ExactReference(id=svc.service_id, version=1)],
                    operations=[ref],
                    conn=conn,
                )
                await catalog.transition(
                    a,
                    created.tool_id,
                    1,
                    "published",
                    ACTOR,
                    expected_revision=created.revision + 1,
                    conn=conn,
                )
        except CatalogConflictError:
            pass  # wrong expected revision on purpose: everything rolls back
    record = await builder.service_for_source(a, "crm")
    assert record is not None and record.status == "draft"
    async with database.pool.connection() as conn, conn.transaction():
        await builder.publish(
            a,
            ACTOR,
            services=[ExactReference(id=svc.service_id, version=1)],
            operations=[ref],
            conn=conn,
        )
        done = await catalog.transition(
            a, created.tool_id, 1, "published", ACTOR, expected_revision=created.revision, conn=conn
        )
    assert done.status == "published"
    assert (await builder.service_for_source(a, "crm")).status == "published"  # type: ignore[union-attr]


# --- runs and trace -----------------------------------------------------------------------------


def run_values(tenant: UUID, tool_id: UUID, **overrides):
    values = {
        "run_id": uuid7(),
        "tenant_id": tenant,
        "gateway_license_id": uuid4(),
        "profile_snapshot": psycopg.types.json.Jsonb({}),
        "owner_tenant_id": tenant,
        "tool_id": tool_id,
        "tool_version": 1,
        "authority": psycopg.types.json.Jsonb({}),
        "inputs": psycopg.types.json.Jsonb({}),
        "input_hash": "a" * 64,
        "created_by": psycopg.types.json.Jsonb({}),
        "queue_deadline": "2030-01-01",
    } | overrides
    return values


def insert_run(conn, values):
    conn.execute(
        sql.SQL("INSERT INTO mcp_runs({}) VALUES ({})").format(
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join([sql.Placeholder()] * len(values)),
        ),
        list(values.values()),
    )


async def test_draft_runs_have_no_binding_and_trace_columns(database, database_url, tenants):
    a, b = tenants
    created = await CatalogRepository(database.pool).create(a, tool_for(a), FILES, ACTOR)
    platform = PlatformCatalogRepository(database.pool)
    await register_source(database.pool, "destination_1")
    await platform.add_service(service(1), ACTOR)
    await platform.add_operation(operation(1), ACTOR)
    with connect(database_url) as conn:
        draft = run_values(
            a,
            created.tool_id,
            mode="draft",
            binding_id=None,
            binding_revision=None,
            credential_profile_id=None,
        )
        insert_run(conn, draft)  # UUIDv4 tenant and gateway licence ids are accepted
        assert conn.execute("SELECT mode FROM mcp_runs").fetchone() == ("draft",)
        bad = (
            # bound without its binding
            run_values(a, created.tool_id),
            # draft that names a binding
            run_values(
                a,
                created.tool_id,
                mode="draft",
                binding_id=uuid7(),
                binding_revision=1,
                credential_profile_id=uuid7(),
            ),
            # draft for a tool the tenant does not own
            run_values(
                b,
                created.tool_id,
                mode="draft",
                owner_tenant_id=a,
                binding_id=None,
                binding_revision=None,
                credential_profile_id=None,
            ),
            run_values(
                a,
                created.tool_id,
                mode="other",
                binding_id=None,
                binding_revision=None,
                credential_profile_id=None,
            ),
        )
        for values in bad:
            with pytest.raises((CheckViolation, psycopg.errors.IntegrityError)), conn.transaction():
                insert_run(conn, values)
        # Draft idempotency keys are unique per tenant/tool.
        keyed = run_values(
            a,
            created.tool_id,
            mode="draft",
            idempotency_key="k1",
            binding_id=None,
            binding_revision=None,
            credential_profile_id=None,
        )
        insert_run(conn, keyed)
        with pytest.raises(UniqueViolation), conn.transaction():
            insert_run(conn, keyed | {"run_id": uuid7()})
        # Mode is part of the immutable run snapshot.
        with pytest.raises(Exception, match="immutable"), conn.transaction():
            conn.execute("UPDATE mcp_runs SET mode='bound' WHERE run_id=%s", (draft["run_id"],))

        # Journal trace: the request shape at intent, the outcome at finalisation, nothing else.
        op = operation(1)
        conn.execute(
            "INSERT INTO mcp_run_api_calls(tenant_id,run_id,sequence,operation_id,operation_version,"
            "request_hash,license_id,effect,method,path) VALUES (%s,%s,1,%s,1,%s,%s,'read_only','GET','/records/1')",
            (a, draft["run_id"], op.operation_id, "b" * 64, uuid4()),
        )
        conn.execute(
            "UPDATE mcp_run_api_calls SET status='failed',finished_at=clock_timestamp(),"
            "http_status=502,duration_ms=12,reason='status' WHERE run_id=%s",
            (draft["run_id"],),
        )
        assert conn.execute(
            "SELECT method,path,http_status,duration_ms,reason FROM mcp_run_api_calls"
        ).fetchone() == (
            "GET",
            "/records/1",
            502,
            12,
            "status",
        )
        for statement in (
            "UPDATE mcp_run_api_calls SET path='/other'",
            "UPDATE mcp_run_api_calls SET reason='bogus'",
        ):
            with pytest.raises(Exception), conn.transaction():
                conn.execute(statement)  # type: ignore[arg-type]
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO mcp_run_api_calls(tenant_id,run_id,sequence,operation_id,operation_version,"
                "request_hash,license_id,effect,reason) VALUES (%s,%s,2,%s,1,%s,%s,'read_only','nope')",
                (a, draft["run_id"], op.operation_id, "b" * 64, uuid4()),
            )


def test_audit_accepts_tenant_author(database_url):
    with connect(database_url) as conn:
        insert = (
            "INSERT INTO mcp_tool_call_audit(tool,status,principal_kind,auth_method,principal,latency_ms) "
            "VALUES ('builder_x','success',%s,'license','p',1)"
        )
        conn.execute(insert, ("tenant_author",))  # type: ignore[arg-type]
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(insert, ("someone_else",))  # type: ignore[arg-type]


def test_grants_and_operation_keys_are_guarded(database_url):
    with connect(database_url) as conn:
        svc = uuid7()
        tenant_svc = uuid7()
        conn.execute(
            "INSERT INTO mcp_services(service_id,service_key,created_by) VALUES (%s,'p',%s)",
            (svc, "{}"),
        )
        conn.execute(
            "INSERT INTO mcp_services(service_id,service_key,owner_tenant_id,created_by) VALUES (%s,'t',%s,%s)",
            (tenant_svc, uuid4(), "{}"),
        )
        conn.execute(
            "INSERT INTO mcp_service_grants(service_id,grantee_tenant_id,granted_by) VALUES (%s,%s,'{}')",
            (svc, uuid4()),
        )
        for service_id, grantee in ((tenant_svc, uuid4()), (svc, PLATFORM_TENANT_ID)):
            with pytest.raises(CheckViolation), conn.transaction():
                conn.execute(
                    "INSERT INTO mcp_service_grants(service_id,grantee_tenant_id,granted_by) VALUES (%s,%s,'{}')",
                    (service_id, grantee),
                )
        conn.execute(
            "INSERT INTO mcp_operation_keys(service_id,op_key,operation_id) VALUES (%s,'list',%s)",
            (svc, uuid7()),
        )
        for statement in (
            "UPDATE mcp_operation_keys SET op_key='x'",
            "DELETE FROM mcp_operation_keys",
        ):
            with pytest.raises(CheckViolation), conn.transaction():
                conn.execute(statement)  # type: ignore[arg-type]
        with pytest.raises(CheckViolation), conn.transaction():
            conn.execute(
                "INSERT INTO mcp_operation_keys(service_id,op_key,operation_id) VALUES (%s,'Bad Key',%s)",
                (svc, uuid7()),
            )


# --- roles --------------------------------------------------------------------------------------


def role_url(url: str, role: str, password: str) -> str:
    return make_url(url).set(username=role, password=password).render_as_string(hide_password=False)


def test_runtime_role_writes_only_through_definer_functions(database_url):
    password = "synthetic-test-only"  # pragma: allowlist secret
    runtime, worker, platform = (f"mcp_{n}_" + uuid7().hex for n in ("rt", "wk", "pf"))
    tenant = uuid4()
    with connect(database_url) as admin:
        admin.autocommit = True
        for role in (runtime, worker, platform):
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        grant(database_url, runtime)
        admin.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(worker)))
        grant_execution(admin, worker, worker=True)
        admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(platform))
        )
        admin.execute(
            sql.SQL("GRANT SELECT ON public.mcp_services TO {}").format(sql.Identifier(platform))
        )
        grant_platform_builder(admin, platform)

    create = (
        "SELECT source_id FROM mcp_builder_source_create(%s,%s,'crm','https://crm.example.com','{\"kind\":\"none\"}',"
        "NULL,NULL,NULL,NULL,NULL,'{}',5)"
    )
    with connect(role_url(database_url, runtime, password)) as conn:
        conn.autocommit = True
        assert conn.execute(create, (tenant, uuid7())).fetchone() is not None  # type: ignore[arg-type]
        assert conn.execute(
            "SELECT count(*) FROM mcp_sources WHERE owner_tenant_id=%s", (tenant,)
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM mcp_service_grants").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM mcp_operation_keys").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM mcp_source_specs").fetchone() == (0,)
        for statement in (
            "INSERT INTO mcp_sources(source_id,source_key,base_url,auth,created_by,updated_by) "
            "VALUES (gen_random_uuid(),'x','https://x.example.com','{\"kind\":\"none\"}','{}','{}')",
            "UPDATE mcp_sources SET status='disabled'",
            "INSERT INTO mcp_service_grants(service_id,grantee_tenant_id,granted_by) VALUES (gen_random_uuid(),gen_random_uuid(),'{}')",
            "INSERT INTO mcp_operation_keys(service_id,op_key,operation_id) VALUES (gen_random_uuid(),'k',gen_random_uuid())",
            "DELETE FROM mcp_source_specs",
            "INSERT INTO mcp_source_specs(owner_tenant_id,source_id,sha256,bytes,document) VALUES (gen_random_uuid(),gen_random_uuid(),'a',1,'{}')",
        ):
            with pytest.raises(InsufficientPrivilege):
                conn.execute(statement)  # type: ignore[arg-type]
        with pytest.raises(psycopg.Error) as err:
            conn.execute(create, (PLATFORM_TENANT_ID, uuid7()))  # type: ignore[arg-type]
        assert err.value.sqlstate == "MB004"
        assert conn.execute(
            "SELECT public.mcp_tenant_may_use_service(%s,gen_random_uuid(),1)", (tenant,)
        ).fetchone() == (False,)

    with connect(role_url(database_url, worker, password)) as conn:
        conn.autocommit = True
        for table in ("mcp_sources", "mcp_services", "mcp_service_grants", "mcp_operation_keys"):
            conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 0").format(sql.Identifier(table)))
        for table in ("mcp_service_grants", "mcp_operation_keys", "mcp_sources"):
            assert conn.execute(
                "SELECT has_table_privilege(current_user,%s,'INSERT')", (f"public.{table}",)
            ).fetchone() == (False,)
        assert conn.execute(
            "SELECT has_table_privilege(current_user,'public.mcp_run_api_calls','INSERT,UPDATE')"
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT has_function_privilege(current_user,'public.mcp_tenant_may_use_service(uuid,uuid,integer)','EXECUTE')"
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT has_function_privilege(current_user,"
            "'public.mcp_builder_set_status(uuid,text,uuid,integer,text,jsonb)','EXECUTE')"
        ).fetchone() == (False,)

    with connect(role_url(database_url, platform, password)) as conn:
        conn.autocommit = True
        conn.execute(
            "INSERT INTO mcp_service_grants(service_id,grantee_tenant_id,granted_by) SELECT service_id,gen_random_uuid(),'{}' FROM mcp_services WHERE false"
        )
        assert conn.execute(
            "SELECT has_table_privilege(current_user,'public.mcp_operation_keys','INSERT')"
        ).fetchone() == (True,)


async def test_spec_storage_is_capped_per_tenant_across_sources(database, tenants):
    a, b = tenants
    repo = sources(database, spec_max_total_bytes=1000)
    for key in ("crm", "erp"):
        await repo.create(a, source_body(key), ACTOR)
    await repo.create(b, source_body("crm"), ACTOR)
    await repo.put_spec(a, "crm", "a" * 64, {"n": 1}, 600)
    with pytest.raises(QuotaExceededError):  # 600 + 500 > 1000, though each source is small
        await repo.put_spec(a, "erp", "b" * 64, {"n": 2}, 500)
    assert await repo.get_spec(a, "erp") is None  # the rejected insert rolled back
    await repo.put_spec(a, "erp", "b" * 64, {"n": 2}, 400)  # exactly at the cap
    # Another tenant has its own budget; rotation keeps working because the last-three window
    # prunes older bytes before the cap is counted.
    for i in range(5):
        await repo.put_spec(b, "crm", f"{i:064x}", {"n": i}, 330)
    with pytest.raises(QuotaExceededError):
        await repo.put_spec(b, "crm", "d" * 64, {"n": 4}, 1001)
