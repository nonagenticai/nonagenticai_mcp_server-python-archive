from pathlib import Path
from typing import LiteralString
from uuid import uuid7

import psycopg
import pytest
from alembic import command
from alembic.util.exc import CommandError
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from scripts.grant_runtime_role import grant
from scripts.reset_development_database import reset
from src.catalog.service import CatalogService
from src.config import Settings
from src.storage.catalog import CatalogRepository
from src.storage.database import Database
from src.storage.schema import EXPECTED_REVISION, verify_schema
from src.toolkits.models import ToolkitReference
from tests.conftest import migration_config
from tests.fixtures import ACTOR, FILES, IMAGE, TENANT_A, TOOL, tool


def connect(url):
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1))


def test_baseline_is_empty_idempotent_and_independent(database_url):
    config = migration_config(database_url)
    with connect(database_url) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            EXPECTED_REVISION,
        )
        assert conn.execute("SELECT count(*) FROM mcp_tools").fetchone() == (0,)
        conn.execute("UPDATE mcp_catalog_state SET generation=7")
        tables = {
            row[0]
            for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        assert tables == {
            "alembic_version",
            "mcp_runs",
            "mcp_task_inputs",
            "mcp_run_activity",
            "mcp_run_api_calls",
            "mcp_run_events",
            "mcp_execution_slots",
            "mcp_tenant_execution_locks",
            "mcp_api_permits",
            "mcp_api_rate_windows",
            "mcp_catalog_state",
            "mcp_services",
            "mcp_service_versions",
            "mcp_operation_versions",
            "mcp_tools",
            "mcp_tool_versions",
            "mcp_tool_shares",
            "mcp_tool_version_files",
            "mcp_tool_version_operations",
            "mcp_toolkits",
            "mcp_toolkit_versions",
            "mcp_toolkit_version_tools",
            "mcp_toolkit_inclusions",
            "mcp_credential_profiles",
            "mcp_credential_refs",
            "mcp_bindings",
            "mcp_binding_tools",
            "mcp_binding_toolkits",
            "mcp_protocol_sessions",
            "mcp_access_events",
            "mcp_tool_call_audit",
            "mcp_binding_ensures",
            "mcp_sources",
            "mcp_source_events",
            "mcp_service_grants",
            "mcp_operation_keys",
            "mcp_source_specs",
        }
    command.upgrade(config, "head")
    with connect(database_url) as conn:
        assert conn.execute("SELECT generation FROM mcp_catalog_state").fetchone() == (7,)
    with pytest.raises(RuntimeError, match="fix forward"):
        command.downgrade(config, "base")


def test_old_revision_refused_then_controlled_reset(empty_database):
    target = make_url(empty_database).database
    assert target is not None
    with connect(empty_database) as conn:
        conn.execute("CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES ('0003_archive_stray_8382f0b3')")
        conn.execute("CREATE TABLE mcp_tools(name text)")
        conn.execute("INSERT INTO mcp_tools VALUES ('discarded')")
        conn.execute("CREATE TABLE users(id integer)")
        conn.execute("CREATE TABLE oauth_clients(id integer)")
        conn.execute("CREATE SCHEMA archive")
        conn.execute("CREATE TABLE archive.old_tools(name text)")
    with pytest.raises(CommandError, match="Can't locate revision"):
        command.upgrade(migration_config(empty_database), "head")
    for name, stopped in [("postgres", True), (target, False), ("wrong_database", True)]:
        with pytest.raises(ValueError):
            reset(empty_database, name, writers_stopped=stopped)
    with connect(empty_database) as conn:
        assert conn.execute("SELECT name FROM mcp_tools").fetchone() == ("discarded",)
    reset(empty_database, target, writers_stopped=True)
    command.upgrade(migration_config(empty_database), "head")
    with connect(empty_database) as conn:
        assert conn.execute("SELECT count(*) FROM mcp_tools").fetchone() == (0,)
        assert conn.execute(
            "SELECT to_regclass('public.users'),to_regclass('public.oauth_clients'),to_regnamespace('archive')"
        ).fetchone() == (None, None, None)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            EXPECTED_REVISION,
        )


def test_reset_refuses_unrelated_schema(empty_database):
    with connect(empty_database) as conn:
        conn.execute("CREATE SCHEMA another_application")
    with pytest.raises(ValueError, match="unexpected schema"):
        reset(empty_database, str(make_url(empty_database).database), writers_stopped=True)
    with connect(empty_database) as conn:
        assert (
            conn.execute("SELECT to_regnamespace('another_application')").fetchone()[0] is not None
        )


async def test_runtime_role_has_no_ddl_and_keeps_lifecycle_guards(empty_database, postgres_server):
    runtime = "mcp_runtime_" + uuid7().hex
    owner = "mcp_owner_" + uuid7().hex
    parsed = make_url(empty_database)
    password = "synthetic-test-only"  # pragma: allowlist secret
    with connect(empty_database) as conn:
        admin = conn.execute("SELECT current_user").fetchone()[0]
        for role in (runtime, owner):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        conn.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(str(parsed.database)), sql.Identifier(owner)
            )
        )
    owner_url = parsed.set(username=owner, password=password).render_as_string(hide_password=False)
    try:
        # The migration role owns only this dedicated database, is not superuser,
        # and successfully applies the self-contained baseline without application imports.
        command.upgrade(migration_config(owner_url), "head")
        grant(owner_url, runtime)
        runtime_url = parsed.set(username=runtime, password=password).render_as_string(
            hide_password=False
        )
        configuration = Settings(
            database_url=SecretStr(runtime_url),
            pool_min_size=0,
            approved_runner_images=(IMAGE,),
            license_introspect_url="https://licenses.example/introspect",
            license_app_key="mcp",
        )
        db = Database(configuration)
        await db.open()
        try:
            async with db.pool.connection() as conn:
                await verify_schema(conn)
                privileges = await conn.execute(
                    "SELECT has_schema_privilege(current_user,'public','CREATE') AS ddl"
                )
                row = await privileges.fetchone()
                assert row is not None and row["ddl"] is False
            with pytest.raises(InsufficientPrivilege):
                async with db.pool.connection() as conn:
                    await conn.execute("CREATE TABLE public.forbidden(id int)")
            catalog = CatalogService(CatalogRepository(db.pool), configuration)
            await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
            await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1)
            assert await catalog.repository.generation() == 1
            # Platform contracts are authored with the migration/operator role only.
            from datetime import UTC, datetime, timedelta

            from src.catalog.models import ExactReference
            from src.catalog.service import PlatformCatalogService
            from src.storage.bindings import BindingRepository
            from src.storage.catalog import PlatformCatalogRepository
            from src.toolkits.models import (
                BindingSpec,
                BoundContext,
                CredentialReference,
                ToolReference,
                ToolSelection,
            )
            from src.toolkits.service import AccessService
            from tests.fixtures import operation, register_source, service
            from tests.test_bindings import PRINCIPAL, identity, profile, toolkit

            owner_db = Database(Settings(database_url=SecretStr(owner_url), pool_min_size=0))
            await owner_db.open()
            try:
                platform = PlatformCatalogService(PlatformCatalogRepository(owner_db.pool), catalog)
                await platform.add_service(service(), ACTOR)
                await register_source(
                    owner_db.pool, service().destination_alias, app_key=service().license_app_key
                )
                await platform.repository.transition(
                    "service",
                    ExactReference(id=service().service_id, version=1),
                    "published",
                    ACTOR,
                )
                await platform.add_operation(operation(), ACTOR)
                await platform.repository.transition(
                    "operation",
                    ExactReference(id=operation().operation_id, version=1),
                    "published",
                    ACTOR,
                )
            finally:
                await owner_db.close()
            await catalog.append_draft(
                TENANT_A,
                TOOL,
                tool(allowed_operations=(ExactReference(id=operation().operation_id, version=1),)),
                FILES,
                ACTOR,
                expected_revision=2,
            )
            await catalog.transition(TENANT_A, TOOL, 2, "published", ACTOR, expected_revision=3)
            access = AccessService(BindingRepository(db.pool), configuration)
            credential = profile(
                services=(
                    CredentialReference(
                        service="service_1",
                        alias="upstream_alias",
                        license_id=uuid7(),
                        app_key="app_1",
                    ),
                )
            )
            await access.profile_write(PRINCIPAL, credential)
            manifest = toolkit(
                tools=(ToolSelection(tool=ToolReference(tenant_id=TENANT_A, id=TOOL, version=2)),)
            )
            await access.toolkit_write(PRINCIPAL, manifest)
            await access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "published", 1)
            binding, token = await access.binding_write(
                PRINCIPAL,
                BindingSpec(
                    credential_profile_id=credential.profile_id,
                    toolkits=(ToolkitReference(id=manifest.toolkit_id, version=1),),
                    context=BoundContext(tenant_id=TENANT_A),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
            )
            assert token and await access.repository.authenticate(identity(), token)
            assert await access.effective(binding) == binding.tools
            await access.repository.pin_session("role-session", identity(), binding, token, 60)
            await access.binding_action(PRINCIPAL, binding.binding_id, 1, revoke=True)
            # bindings/ensure (0007/0008) under the runtime role: the platform tenant publishes
            # the first-party toolkit; the tenant's binding pins it (profile, binding, rotate).
            from src.auth.licenses import LicenseIdentity
            from src.auth.principal import Principal
            from src.config import PLATFORM_TENANT_ID
            from src.storage.bindings_ensure import BindingEnsureService
            from tests.test_binding_ensure import FakeLicences, FakeMinter

            platform_tool = tool(
                PLATFORM_TENANT_ID,
                allowed_operations=(ExactReference(id=operation().operation_id, version=1),),
            )
            await catalog.create_draft(PLATFORM_TENANT_ID, platform_tool, FILES, ACTOR)
            await catalog.transition(
                PLATFORM_TENANT_ID, TOOL, 1, "published", ACTOR, expected_revision=1
            )
            platform_operator = Principal(PLATFORM_TENANT_ID, uuid7(), "platform", management=True)
            platform_kit = toolkit(PLATFORM_TENANT_ID)
            await access.toolkit_write(platform_operator, platform_kit)
            await access.toolkit_transition(
                platform_operator, platform_kit.toolkit_id, 1, "published", 1
            )
            ensure_access = AccessService(
                BindingRepository(db.pool),
                configuration.model_copy(
                    update={"binding_products": {"agent": ("synthetic_toolkit",)}}
                ),
            )
            gateway = uuid7()
            ensurer = BindingEnsureService(
                ensure_access,
                FakeLicences(LicenseIdentity(TENANT_A, gateway, "svc", float("inf"), "tenant")),
                FakeMinter({"app_1": uuid7()}),
            )
            held = {"licence_id": gateway, "licence_token": "held"}
            first = await ensurer.ensure(TENANT_A, "agent", "svc", **held)
            assert first["status"] == "created" and first["token"]
            assert first["toolkits"][0]["tenant_id"] == str(PLATFORM_TENANT_ID)
            again = await ensurer.ensure(TENANT_A, "agent", "svc", **held)
            assert again["token_already_issued"] and "token" not in again
            rotated = await ensurer.ensure(TENANT_A, "agent", "svc", rotate=True, **held)
            assert rotated["status"] == "rotated" and rotated["token"] != first["token"]
            async with db.pool.connection() as conn:
                await verify_schema(conn, PLATFORM_TENANT_ID)
            with pytest.raises(InsufficientPrivilege):
                async with db.pool.connection() as conn:
                    await conn.execute("DELETE FROM mcp_binding_ensures")
            statements: list[LiteralString] = [
                "UPDATE mcp_tool_versions SET package_sha256=repeat('b',64)",
                "DELETE FROM mcp_tool_versions",
                "UPDATE mcp_catalog_state SET generation=0",
                "INSERT INTO mcp_services(service_id,service_key,created_by) VALUES (gen_random_uuid(),'bad','{}')",
            ]
            for statement in statements:
                with pytest.raises(InsufficientPrivilege):
                    async with db.pool.connection() as conn:
                        await conn.execute(statement)
        finally:
            await db.close()
    finally:
        with connect(empty_database) as conn:
            for role in (runtime, owner):
                conn.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(str(parsed.database)), sql.Identifier(admin)
                )
            )
            for role in (runtime, owner):
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_schema_checker_unmigrated_unknown_and_state_missing(database):
    async with database.pool.connection() as conn:
        await verify_schema(conn)
        await conn.execute("UPDATE alembic_version SET version_num='unknown'")
    with pytest.raises(RuntimeError, match="revision"):
        async with database.pool.connection() as conn:
            await verify_schema(conn)
    async with database.pool.connection() as conn:
        await conn.execute("UPDATE alembic_version SET version_num=%s", (EXPECTED_REVISION,))
        await conn.execute("DELETE FROM mcp_catalog_state")
    with pytest.raises(RuntimeError, match="state"):
        async with database.pool.connection() as conn:
            await verify_schema(conn)


def test_offline_migration_needs_no_app_imports(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(migration_config("postgresql://unused/unused"), "head", sql=True)
    assert "CREATE TABLE public.mcp_tools" in capsys.readouterr().out
    config = migration_config("postgresql://unused/unused")
    del config.attributes["database_url"]
    with pytest.raises(RuntimeError, match="required"):
        command.upgrade(config, "head", sql=True)


async def test_populated_phase2_upgrade_preserves_authority(empty_database, monkeypatch):
    """An ordinary forward upgrade, never a reset of an existing phase-2 catalog."""
    from src.storage.bindings import BindingRepository
    from src.toolkits.service import AccessService
    from tests.fixtures import settings
    from tests.test_bindings import PRINCIPAL, identity, selection

    config = migration_config(empty_database)
    command.upgrade(config, "0002_toolkits_bindings")
    configuration = settings(
        database_url=empty_database,
        license_introspect_url="https://licenses.example/introspect",
        license_app_key="mcp",
    )
    db = Database(configuration)
    await db.open()
    try:
        catalog = CatalogService(CatalogRepository(db.pool), configuration)
        access = AccessService(BindingRepository(db.pool), configuration)

        async def legacy_audit(
            conn, tenant, actor, entity, revision, action, artifact_version=None
        ):
            if action.startswith("tool."):
                return  # Phase 2 did not audit tools.
            await conn.execute(
                "INSERT INTO mcp_access_events(event_id,tenant_id,subject,license_id,entity_id,revision,action) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uuid7(), tenant, actor.subject, actor.license_id, entity, revision, action),
            )

        async def legacy_members(conn, binding):
            # Phase-2 mcp_binding_toolkits had no owner column (0008 adds it).
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO mcp_binding_toolkits VALUES (%s,%s,%s,%s)",
                    [
                        (binding.tenant_id, binding.binding_id, r.id, r.version)
                        for r in binding.spec.toolkits
                    ],
                )
                await cur.executemany(
                    "INSERT INTO mcp_binding_tools VALUES (%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            binding.tenant_id,
                            binding.binding_id,
                            t.tool.tenant_id,
                            t.tool.id,
                            t.tool.version,
                            t.public_name,
                        )
                        for t in binding.tools
                    ],
                )

        with monkeypatch.context() as patch:
            patch.setattr("src.toolkits.service.binding_members", legacy_members)
            patch.setattr("src.storage.bindings.audit", legacy_audit)
            patch.setattr("src.toolkits.service.audit", legacy_audit)
            await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
            await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1)
            spec, _, _ = await selection(access)
            binding, token = await access.binding_write(PRINCIPAL, spec)
            assert token is not None
            await access.repository.pin_session("phase2-session", identity(), binding, token, 60)
        async with db.pool.connection() as conn:
            before = await (
                await conn.execute("SELECT token_sha256,spec,tools FROM mcp_bindings")
            ).fetchall()
        generation = await catalog.repository.generation()
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert await catalog.repository.generation() == generation
        assert await access.repository.authenticate(identity(), token) == binding
        assert await access.effective(binding) == binding.tools
        await access.repository.check_session("phase2-session", identity(), binding, token)
        async with db.pool.connection() as conn:
            assert (
                await (
                    await conn.execute("SELECT token_sha256,spec,tools FROM mcp_bindings")
                ).fetchall()
                == before
            )
            assert all(
                r["artifact_version"] is None
                for r in await (
                    await conn.execute("SELECT artifact_version FROM mcp_access_events")
                ).fetchall()
            )
        await catalog.append_draft(
            TENANT_A, TOOL, tool(version=2), FILES, ACTOR, expected_revision=2, exact_version=True
        )
        async with db.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT artifact_version FROM mcp_access_events WHERE action='tool.version_created'"
                )
            ).fetchone()
            assert row is not None and row["artifact_version"] == 2
        from psycopg.errors import CheckViolation

        with pytest.raises(CheckViolation):
            async with db.pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO mcp_access_events(event_id,tenant_id,subject,license_id,entity_id,revision,action) VALUES (%s,%s,'subject',%s,%s,1,'tool.created')",
                    (uuid7(), TENANT_A, PRINCIPAL.license_id, TOOL),
                )
        with pytest.raises(CheckViolation):
            async with db.pool.connection() as conn:
                await conn.execute(
                    "UPDATE mcp_access_events SET artifact_version=3 WHERE action='tool.version_created'"
                )
    finally:
        await db.close()


async def test_worker_schema_check_does_not_require_the_api_audit_grant(database):
    """mcp_executor has no INSERT on mcp_tool_call_audit (only the API writes builtin audit),
    so the worker verifies with require_audit=False while the API check still demands it."""
    async with database.pool.connection() as conn, conn.transaction():
        await conn.execute(
            "CREATE ROLE schema_check_worker NOLOGIN; "
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO schema_check_worker; "
            "GRANT EXECUTE ON FUNCTION public.mcp_platform_tenant() TO schema_check_worker; "
            # 0010: the worker evaluates the tenant reference rule too (grant_runtime_role.py).
            "GRANT EXECUTE ON FUNCTION public.mcp_tenant_may_use_service(uuid,uuid,integer) "
            "TO schema_check_worker; "
            "SET LOCAL ROLE schema_check_worker"
        )
        await verify_schema(conn, require_audit=False)
        with pytest.raises(RuntimeError, match="audit"):
            await verify_schema(conn)
        await conn.execute("RESET ROLE")
        await conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM schema_check_worker")
        await conn.execute(
            "REVOKE EXECUTE ON FUNCTION public.mcp_platform_tenant() FROM schema_check_worker; "
            "REVOKE EXECUTE ON FUNCTION public.mcp_tenant_may_use_service(uuid,uuid,integer) "
            "FROM schema_check_worker; "
            "DROP ROLE schema_check_worker"
        )


async def test_0010_checks_are_added_not_valid_then_validated(database):
    """The big-table CHECKs go in NOT VALID and are validated in a later statement, so no scan
    runs under ACCESS EXCLUSIVE; after the migration none is left unvalidated."""
    async with database.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT conrelid::regclass::text AS tbl, conname, convalidated FROM pg_constraint "
            "WHERE contype='c' AND conname = ANY(%s)",
            (
                [
                    "mcp_tool_call_audit_principal_kind_check",
                    "mcp_runs_mode_check",
                    "mcp_runs_mode_binding",
                    "mcp_run_api_calls_method_check",
                    "mcp_run_api_calls_reason_check",
                    "mcp_run_api_calls_error_excerpt_check",
                ],
            ),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 6 and all(r["convalidated"] for r in rows)
        cursor = await conn.execute(
            "SELECT conname FROM pg_constraint WHERE NOT convalidated AND connamespace='public'::regnamespace"
        )
        assert await cursor.fetchall() == []
    text = (Path(__file__).parent.parent / "migrations/versions/0010_tenant_builder.py").read_text()
    assert "SET LOCAL lock_timeout = '5s'" in text
    assert "DROP CONSTRAINT mcp_tool_call_audit_principal_kind_check" in text
    assert text.count("NOT VALID") >= 9 and "VALIDATE CONSTRAINT" in text
