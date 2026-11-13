"""Live projection invalidation independent of provider/process caches."""

from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest

from src.auth.context import RequestAuthority, authority
from src.auth.principal import Principal
from src.catalog.provider import CatalogProvider
from src.catalog.resolver import CatalogResolver
from src.catalog.submission import UnavailableSubmission
from src.storage.catalog import CatalogConflictError
from tests.fixtures import TENANT_A, TOOL, settings
from tests.test_bindings import LICENSE, PRINCIPAL, selection
from tests.test_bindings import access as access


@pytest.mark.parametrize(
    "change", ["image", "tool", "retirement", "toolkit", "profile", "revision", "revoked", "expiry"]
)
async def test_projection_and_retained_wrapper_recheck_changes(access, change, monkeypatch):
    spec, manifest, credential = await selection(access)
    binding, token = await access.binding_write(PRINCIPAL, spec)
    resolver = CatalogResolver(access)
    replica = CatalogResolver(access)
    provider = CatalogProvider(replica, UnavailableSubmission())
    principal = Principal(TENANT_A, LICENSE, "caller", binding.binding_id, 1)
    resolved = await resolver.resolve(principal)
    assert len(resolved) == 1
    context = authority.set(RequestAuthority(principal, None))
    try:
        retained = await provider.get_tool("synthetic_tool")
        assert retained is not None
        if change == "image":
            access.settings = settings(
                approved_runner_images=(),
                license_introspect_url="https://licenses.example/introspect",
                license_app_key="mcp",
            )
        elif change in {"tool", "retirement"}:
            from src.catalog.service import CatalogService
            from src.storage.catalog import CatalogRepository

            await CatalogService(
                CatalogRepository(access.repository.pool), access.settings
            ).transition(
                TENANT_A,
                TOOL,
                1,
                "disabled" if change == "tool" else "retired",
                PRINCIPAL.actor,
                expected_revision=2,
            )
        elif change == "toolkit":
            await access.toolkit_transition(PRINCIPAL, manifest.toolkit_id, 1, "disabled", 2)
        elif change == "profile":
            await access.profile_write(
                PRINCIPAL,
                credential.model_copy(
                    update={
                        "gateway": credential.gateway.model_copy(update={"license_id": uuid7()})
                    }
                ),
                1,
            )
        elif change == "revision":
            await access.binding_action(PRINCIPAL, binding.binding_id, 1)
        elif change == "revoked":
            await access.binding_action(PRINCIPAL, binding.binding_id, 1, revoke=True)
        elif change == "expiry":

            class FutureDate(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(UTC) + timedelta(days=1)

            monkeypatch.setattr("src.catalog.resolver.datetime", FutureDate)
        with pytest.raises((PermissionError, CatalogConflictError, ValueError)):
            await replica.resolve(principal)
        assert (await retained.run({})).is_error
    finally:
        authority.reset(context)
    for denied in [PRINCIPAL, Principal(TENANT_A, LICENSE, "no-binding")]:
        with pytest.raises(PermissionError):
            await resolver.resolve(denied)


async def test_generation_retry_is_bounded(access, monkeypatch):
    spec, _, _ = await selection(access)
    binding, _ = await access.binding_write(PRINCIPAL, spec)
    resolver = CatalogResolver(access)
    principal = Principal(TENANT_A, LICENSE, "caller", binding.binding_id, 1)
    values = iter([1, 2, 2, 2])

    async def generation():
        return next(values)

    monkeypatch.setattr(resolver.repository, "generation", generation)
    assert len(await resolver.resolve(principal)) == 1
    values = iter([1, 2, 3, 4])
    with pytest.raises(CatalogConflictError):
        await resolver.resolve(principal)
