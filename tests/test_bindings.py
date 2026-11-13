"""Real PostgreSQL: complete exact selection, atomic authority changes and isolation."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid7

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation
from psycopg.types.json import Jsonb

from src.auth.licenses import AuthenticationError, LicenseIdentity, digest_token
from src.auth.principal import Principal
from src.catalog.models import ExactReference, OperationConstraints
from src.catalog.service import CatalogService
from src.storage.bindings import BindingRepository
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError, CatalogRepository
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
from tests.fixtures import ACTOR, FILES, TENANT_A, TENANT_B, TOOL, settings, tool

LICENSE = uuid7()
PRINCIPAL = Principal(TENANT_A, LICENSE, "synthetic-operator", management=True)
OTHER = Principal(TENANT_B, uuid7(), "other-operator", management=True)


def identity(tenant=TENANT_A, license_id=LICENSE):
    return LicenseIdentity(tenant, license_id, "synthetic-subject", float("inf"))


def profile(tenant=TENANT_A, **updates):
    return CredentialProfile(
        profile_id=uuid7(),
        tenant_id=tenant,
        name="synthetic_profile",
        gateway=CredentialReference(
            service="mcp", alias="gateway_alias", license_id=LICENSE, app_key="mcp"
        ),
        **updates,
    )


def toolkit(tenant=TENANT_A, **updates):
    values: dict[str, Any] = dict(
        toolkit_id=uuid7(),
        tenant_id=tenant,
        version=1,
        name="synthetic_toolkit",
        description="Synthetic toolkit",
        tools=(ToolSelection(tool=ToolReference(tenant_id=tenant, id=TOOL, version=1)),),
    )
    return ToolkitManifest(**{**values, **updates})


@pytest.fixture
async def access(database):
    config = settings(
        license_introspect_url="https://licenses.example/introspect", license_app_key="mcp"
    )
    catalog = CatalogService(CatalogRepository(database.pool), config)
    await catalog.create_draft(TENANT_A, tool(), FILES, ACTOR)
    await catalog.transition(TENANT_A, TOOL, 1, "published", ACTOR, expected_revision=1)
    return AccessService(BindingRepository(database.pool), config)


async def selection(access):
    credential = profile()
    await access.profile_write(PRINCIPAL, credential)
    manifest = toolkit()
    await access.toolkit_write(PRINCIPAL, manifest)
    await access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "published", 1)
    spec = BindingSpec(
        credential_profile_id=credential.profile_id,
        toolkits=(ToolkitReference(id=manifest.toolkit_id, version=1),),
        context=BoundContext(tenant_id=TENANT_A),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return spec, manifest, credential


async def test_binding_issue_rotate_revoke_and_no_secrets(access):
    spec, manifest, credential = await selection(access)
    before = await CatalogRepository(access.repository.pool).generation()
    binding, token = await access.binding_write(PRINCIPAL, spec)
    assert binding.binding_id.version == 7 and token and len(token) == 43
    assert binding.gateway_license_id == LICENSE
    assert (
        await access.repository.authenticate(identity(), token)
    ).binding_id == binding.binding_id
    assert await access.effective(binding) == binding.tools
    assert binding.tools[0].public_name == "synthetic_tool"
    assert token not in binding.model_dump_json()
    assert await CatalogRepository(access.repository.pool).generation() == before + 1
    async with access.repository.pool.connection() as conn:
        row = await (await conn.execute("SELECT token_sha256 FROM mcp_bindings")).fetchone()
        assert row and row["token_sha256"] == digest_token(token)
        events = await (
            await conn.execute("SELECT * FROM mcp_access_events ORDER BY event_id")
        ).fetchall()
        assert events and all(e["event_id"].version == 7 for e in events)
        assert token not in repr(events)
    for wrong in [identity(TENANT_B), identity(license_id=uuid7())]:
        with pytest.raises(AuthenticationError):
            await access.repository.authenticate(wrong, token)
    with pytest.raises(AuthenticationError):
        await access.repository.authenticate(identity(), "guess")
    with pytest.raises(CatalogNotFoundError):
        await access.repository.inspect(TENANT_B, binding.binding_id)
    with pytest.raises(CatalogConflictError):
        await access.binding_action(PRINCIPAL, binding.binding_id, 99)
    rotated, next_token = await access.binding_action(PRINCIPAL, binding.binding_id, 1)
    assert rotated.revision == 2 and next_token != token
    with pytest.raises(AuthenticationError):
        await access.repository.authenticate(identity(), token)
    assert next_token and await access.repository.authenticate(identity(), next_token)
    revoked, no_token = await access.binding_action(PRINCIPAL, binding.binding_id, 2, revoke=True)
    assert revoked.revoked and no_token is None
    with pytest.raises(AuthenticationError):
        await access.repository.authenticate(identity(), next_token)
    for action in [
        access.binding_action(PRINCIPAL, binding.binding_id, 3),
        access.binding_write(PRINCIPAL, spec, binding.binding_id, 3),
    ]:
        with pytest.raises(CatalogConflictError):
            await action


async def test_binding_revision_concurrency_and_disabled_state(access):
    spec, _, _ = await selection(access)
    binding, token = await access.binding_write(PRINCIPAL, spec)
    results = await asyncio.gather(
        access.binding_write(
            PRINCIPAL, spec.model_copy(update={"enabled": False}), binding.binding_id, 1
        ),
        access.binding_action(PRINCIPAL, binding.binding_id, 1),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CatalogConflictError) for r in results) == 1
    current = await access.repository.inspect(TENANT_A, binding.binding_id)
    if current.spec.enabled:
        current, _ = await access.binding_write(
            PRINCIPAL,
            spec.model_copy(update={"enabled": False}),
            binding.binding_id,
            current.revision,
        )
    assert not current.spec.enabled
    with pytest.raises(AuthenticationError):
        await access.repository.authenticate(identity(), token)
    with pytest.raises(CatalogNotFoundError):
        await access.binding_action(OTHER, binding.binding_id, current.revision)
    with pytest.raises(ValueError, match="expected revision"):
        await access.binding_write(PRINCIPAL, spec, binding.binding_id)


async def test_binding_scope_subset_context_and_expiry_fail_closed(access):
    spec, _, _ = await selection(access)
    bad_specs = [
        spec.model_copy(update={"context": BoundContext(tenant_id=TENANT_B)}),
        spec.model_copy(update={"context": BoundContext(tenant_id=TENANT_A, project_id=uuid7())}),
        spec.model_copy(
            update={"context": BoundContext(tenant_id=TENANT_A, project_phase="build")}
        ),
        spec.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}),
        spec.model_copy(
            update={"tool_subset": (ToolReference(tenant_id=TENANT_A, id=uuid7(), version=1),)}
        ),
        spec.model_copy(update={"credential_profile_id": uuid7()}),
        spec.model_copy(
            update={
                "operation_constraints": (
                    OperationConstraints(operation=ExactReference(id=uuid7(), version=1), rules=()),
                )
            }
        ),
    ]
    for bad in bad_specs:
        with pytest.raises((ValueError, CatalogNotFoundError)):
            await access.binding_write(PRINCIPAL, bad)
    binding, token = await access.binding_write(
        PRINCIPAL, spec.model_copy(update={"tool_subset": ()})
    )
    assert binding.tools == ()
    async with access.repository.pool.connection() as conn:
        old = binding.spec.model_copy(
            update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        await conn.execute(
            "UPDATE mcp_bindings SET spec=%s,expires_at=%s,revision=revision+1 WHERE binding_id=%s",
            (Jsonb(old.model_dump(mode="json")), old.expires_at, binding.binding_id),
        )
    with pytest.raises(AuthenticationError):
        await access.repository.authenticate(identity(), token)


async def test_profile_update_is_revisioned_unverified_and_revalidates_bindings(access):
    spec, _, credential = await selection(access)
    binding, _ = await access.binding_write(PRINCIPAL, spec)
    result = await access.profile_write(PRINCIPAL, credential, 1)
    assert result["revision"] == 2 and result["credentials_verified"] is False
    with pytest.raises(CatalogConflictError):
        await access.profile_write(PRINCIPAL, credential, 1)
    with pytest.raises(CatalogConflictError):
        await access.profile_write(PRINCIPAL, credential)
    for principal, value in [(OTHER, credential), (PRINCIPAL, profile(tenant=TENANT_B))]:
        with pytest.raises(PermissionError):
            await access.profile_write(principal, value)
    wrong = credential.model_copy(
        update={"gateway": credential.gateway.model_copy(update={"app_key": "other"})}
    )
    with pytest.raises(ValueError):
        await access.profile_write(PRINCIPAL, wrong, 2)
    with pytest.raises(ValueError):
        await access.profile_write(PRINCIPAL, credential.model_copy(update={"name": "renamed"}), 2)
    changed = credential.model_copy(
        update={"gateway": credential.gateway.model_copy(update={"license_id": uuid7()})}
    )
    await access.profile_write(PRINCIPAL, changed, 2)
    with pytest.raises(CatalogConflictError):
        await access.effective(binding)
    with pytest.raises(ValueError, match="immutable"):
        await access.binding_write(PRINCIPAL, spec, binding.binding_id, 1)
    with pytest.raises(CatalogNotFoundError):
        await access.profile_write(OTHER, profile(tenant=TENANT_B), 1)


async def test_toolkit_versions_inclusions_immutability_and_live_disable(access):
    spec, manifest, _ = await selection(access)
    included = toolkit(
        name="included_toolkit",
        tools=(),
        includes=(ExactReference(id=manifest.toolkit_id, version=1),),
    )
    first = await access.toolkit_write(PRINCIPAL, included)
    assert first.tools[0].tool.id == TOOL
    first = await access.toolkit_transition(PRINCIPAL, included.toolkit_id, 1, "published", 1)
    bound_spec = spec.model_copy(
        update={"toolkits": (ToolkitReference(id=included.toolkit_id, version=1),)}
    )
    binding, _ = await access.binding_write(PRINCIPAL, bound_spec)
    appended = await access.toolkit_write(PRINCIPAL, manifest, 2)
    assert appended.manifest.version == 2 and appended.revision == 3
    assert (await access.effective(binding))[0].tool.version == 1
    with pytest.raises(CatalogConflictError):
        await access.toolkit_write(PRINCIPAL, manifest, 2)
    with pytest.raises(CatalogConflictError):
        await access.toolkit_write(PRINCIPAL, manifest)
    with pytest.raises(CatalogNotFoundError):
        await access.toolkit_write(OTHER, toolkit(tenant=TENANT_B), 1)
    with pytest.raises(PermissionError):
        await access.toolkit_write(OTHER, manifest)
    with pytest.raises(ValueError):
        await access.toolkit_write(PRINCIPAL, manifest.model_copy(update={"name": "renamed"}), 3)
    with pytest.raises(ValueError):
        await access.toolkit_write(PRINCIPAL, toolkit(version=2))
    with pytest.raises(CatalogConflictError):
        await access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "published", 3)
    with pytest.raises(CatalogNotFoundError):
        await access.toolkit_transition(OTHER, manifest.toolkit_id, 1, "disabled", 3)
    await access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "disabled", 3)
    with pytest.raises(CatalogConflictError):
        await access.effective(binding)
    async with access.repository.pool.connection() as conn:
        with pytest.raises(CheckViolation):
            async with conn.transaction():
                await conn.execute("UPDATE mcp_toolkit_versions SET tools='[]'")
        with pytest.raises(CheckViolation):
            async with conn.transaction():
                await conn.execute("DELETE FROM mcp_toolkit_version_tools")
        with pytest.raises(CheckViolation):
            async with conn.transaction():
                await conn.execute("DELETE FROM mcp_access_events")
        with pytest.raises(CheckViolation):
            async with conn.transaction():
                await conn.execute("UPDATE mcp_bindings SET revoked=true")
        with pytest.raises(ForeignKeyViolation):
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO mcp_binding_toolkits VALUES (%s,%s,%s,1,%s)",
                    (TENANT_B, binding.binding_id, manifest.toolkit_id, TENANT_B),
                )


async def test_unknown_draft_foreign_and_disabled_tool_rejected(access):
    for ref in [
        ToolReference(tenant_id=TENANT_B, id=TOOL, version=1),
        ToolReference(tenant_id=TENANT_A, id=uuid7(), version=1),
    ]:
        with pytest.raises(CatalogNotFoundError):
            await access.toolkit_write(PRINCIPAL, toolkit(tools=(ToolSelection(tool=ref),)))
    manifest = toolkit(tools=(), includes=(ExactReference(id=uuid7(), version=1),))
    with pytest.raises(CatalogNotFoundError):
        await access.toolkit_write(PRINCIPAL, manifest)
    draft = await access.toolkit_write(PRINCIPAL, toolkit())
    with pytest.raises(CatalogConflictError):
        await access.toolkit_write(
            PRINCIPAL,
            toolkit(
                name="includes_draft",
                tools=(),
                includes=(ExactReference(id=draft.manifest.toolkit_id, version=1),),
            ),
        )
    spec, _, _ = await selection_with_existing_draft(access, draft)
    binding, _ = await access.binding_write(PRINCIPAL, spec)
    catalog = CatalogService(CatalogRepository(access.repository.pool), access.settings)
    await catalog.transition(TENANT_A, TOOL, 1, "disabled", ACTOR, expected_revision=2)
    with pytest.raises(CatalogConflictError):
        await access.effective(binding)


async def selection_with_existing_draft(access, draft):
    credential = profile()
    await access.profile_write(PRINCIPAL, credential)
    await access.toolkit_transition(PRINCIPAL, draft.manifest.toolkit_id, 1, "published", 1)
    spec = BindingSpec(
        credential_profile_id=credential.profile_id,
        toolkits=(ToolkitReference(id=draft.manifest.toolkit_id, version=1),),
        context=BoundContext(tenant_id=TENANT_A),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return spec, draft.manifest, credential


async def test_sessions_shared_durable_and_cannot_switch_identity(access):
    spec, _, _ = await selection(access)
    binding, token = await access.binding_write(PRINCIPAL, spec)
    assert token
    session = "synthetic-session"
    await access.repository.pin_session(session, identity(), binding, token, 3600)
    replica = BindingRepository(access.repository.pool)
    await replica.check_session(session, identity(), binding, token)
    with pytest.raises(AuthenticationError):
        await replica.pin_session(session, identity(), binding, token, 3600)
    other, _ = await access.binding_write(PRINCIPAL, spec)
    for license_identity, bound, secret in [
        (identity(TENANT_B), binding, token),
        (identity(license_id=uuid7()), binding, token),
        (identity(), other, token),
        (identity(), binding, "bad"),
    ]:
        with pytest.raises(AuthenticationError):
            await replica.check_session(session, license_identity, bound, secret)
    await replica.delete_session(session, TENANT_B, binding.binding_id)
    await replica.check_session(session, identity(), binding, token)
    await replica.delete_session(session, TENANT_A, binding.binding_id)
    with pytest.raises(AuthenticationError):
        await replica.check_session(session, identity(), binding, token)
    short_identity = LicenseIdentity(
        TENANT_A, LICENSE, "subject", datetime.now(UTC).timestamp() - 1
    )
    await replica.pin_session(session, short_identity, binding, token, 3600)
    with pytest.raises(AuthenticationError):
        await replica.check_session(session, identity(), binding, token)
    await replica.pin_session("new-session", identity(), binding, token, 3600)
    await access.binding_action(PRINCIPAL, binding.binding_id, 1)
    with pytest.raises(AuthenticationError):
        await replica.check_session("new-session", identity(), binding, token)
