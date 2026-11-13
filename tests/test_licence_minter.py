"""LicenceMinterCredentialResolver against a fake licences + Keycloak (httpx.MockTransport).

The bundle-token contract is licences main.py `internal_mint_tenant_bundle_token`: 201
{token, expires_at, tenant_id, bundle, app, license_id, token_id}; 403 with
detail.error == "dependency_grant_scope" when the app is outside the caller's grants.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid7

import httpx
import pytest

from src.credentials.minter import (
    DependencyGrantScopeError,
    LicenceMinterCredentialResolver,
    MinterUnavailableError,
)
from src.execution.models import ExecutionDeniedError
from tests.fixtures import settings

TENANT = UUID("10000000-0000-4000-8000-0000000000aa")  # licences SQL-minted: v4
LICENCE = uuid4()
TOKEN_URL = "https://auth.example/realms/portal/protocol/openid-connect/token"
LICENCES = "https://licenses.example"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).timestamp()

    def __call__(self) -> float:
        return self.now


class FakeAuthority:
    """Keycloak client-credentials endpoint + licences bundle-token endpoint."""

    def __init__(self) -> None:
        self.token_calls = 0
        self.mint_calls: list[httpx.Request] = []
        self.mint_status = 201
        self.mint_body: dict | None = None
        self.token_status = 200
        self.token_body: object = {"access_token": "kc-service-token", "expires_in": 300}
        self.licences: dict[str, UUID] = {"coding": LICENCE}
        self.granted = {"coding", "strata-rag"}
        self.fail_network = False
        self.expires = datetime.now(UTC) + timedelta(minutes=15)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail_network:
            raise httpx.ConnectError("synthetic outage")
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["grant_type"] == "client_credentials"
            assert form["client_id"] == "mcp-bundle-minter"
            assert form["client_secret"] == "s" * 40
            if isinstance(self.token_body, bytes):
                return httpx.Response(self.token_status, content=self.token_body)
            return httpx.Response(self.token_status, json=self.token_body)
        self.mint_calls.append(request)
        assert request.headers["authorization"] == "Bearer kc-service-token"
        assert request.url.path == f"/internal/tenants/{TENANT}/bundle-token"
        app = request.url.params["app"]
        bundle = request.url.params.get("bundle", "care-bus")
        assert request.url.params["ttl_minutes"] == "15"
        if self.mint_status != 201:
            return httpx.Response(self.mint_status, json=self.mint_body or {"detail": "x"})
        if app not in self.granted:
            return httpx.Response(
                403,
                json={
                    "detail": {
                        "error": "dependency_grant_scope",
                        "reason": "app_outside_dependency_grant",
                        "granted_apps": sorted(self.granted),
                    }
                },
            )
        body = {
            "token": f"minted-{app}-{len(self.mint_calls)}",
            "expires_at": self.expires.isoformat(),
            "tenant_id": str(TENANT),
            "bundle": bundle,
            "app": app,
            "license_id": str(self.licences.get(app, LICENCE)),
            "token_id": str(uuid4()),
        }
        return httpx.Response(201, json=self.mint_body or body)


def configuration(tmp_path, **updates):
    secret = tmp_path / "minter-secret"
    secret.write_text("s" * 40 + "\n")
    values = {
        "licence_minter_enabled": True,
        "licence_minter_url": LICENCES + "/",
        "licence_minter_token_url": TOKEN_URL,
        "licence_minter_secret_file": secret,
        **updates,
    }
    return settings(**values)


@pytest.fixture
def minter(tmp_path):
    authority, clock = FakeAuthority(), Clock()
    resolver = LicenceMinterCredentialResolver(
        configuration(tmp_path),
        httpx.AsyncClient(transport=httpx.MockTransport(authority)),
        clock,
    )
    yield resolver, authority, clock


def reference(app="coding", licence=LICENCE, bundle=None):
    return SimpleNamespace(
        app_key=app, license_id=licence, service="coding", alias="coding", bundle=bundle
    )


async def test_mint_caches_until_expiry_minus_skew(minter):
    resolver, authority, clock = minter
    authority.token_body = {"access_token": "kc-service-token", "expires_in": 3600}
    first = await resolver.mint(TENANT, "coding")
    assert first.license_id == LICENCE and first.token.get_secret_value() == "minted-coding-1"
    assert "minted-coding" not in repr(first)
    assert (await resolver.mint(TENANT, "coding")) is first
    assert len(authority.mint_calls) == 1 and authority.token_calls == 1
    # Expiry minus the 30 s skew forces a re-mint; the client token is still cached.
    clock.now = authority.expires.timestamp() - 29
    with pytest.raises(ExecutionDeniedError, match="already expired"):
        await resolver.mint(TENANT, "coding")  # the authority answered a stale expiry
    authority.expires += timedelta(minutes=15)
    second = await resolver.mint(TENANT, "coding")
    assert second.token.get_secret_value() == "minted-coding-3"
    assert authority.token_calls == 1
    # The client token is refreshed once its own lifetime (minus skew) passes.
    clock.now += 3600
    authority.expires = datetime.fromtimestamp(clock.now, UTC) + timedelta(minutes=15)
    await resolver.mint(TENANT, "strata-rag")
    assert authority.token_calls == 2
    await resolver.close()


async def test_dependency_grant_scope_fails_closed_and_is_not_cached(minter):
    resolver, authority, _ = minter
    for _ in range(2):
        with pytest.raises(DependencyGrantScopeError):
            await resolver.mint(TENANT, "laya")
    assert len(authority.mint_calls) == 2 and not resolver.cache
    with pytest.raises(ExecutionDeniedError):
        await resolver.aresolve(TENANT, uuid7(), reference("laya"))


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (403, ExecutionDeniedError),
        (404, ExecutionDeniedError),
        (409, ExecutionDeniedError),
        (500, MinterUnavailableError),
        (502, MinterUnavailableError),
    ],
)
async def test_refusals_and_outages_fail_closed(minter, status, error):
    resolver, authority, _ = minter
    authority.mint_status = status
    with pytest.raises(error):
        await resolver.mint(TENANT, "coding")
    assert not resolver.cache


async def test_401_drops_the_client_token(minter):
    resolver, authority, _ = minter
    authority.mint_status = 401
    with pytest.raises(MinterUnavailableError):
        await resolver.mint(TENANT, "coding")
    assert resolver.service_token is None
    authority.mint_status = 201
    await resolver.mint(TENANT, "coding")
    assert authority.token_calls == 2


async def test_network_failure_is_unavailable(minter):
    resolver, authority, _ = minter
    authority.fail_network = True
    with pytest.raises(MinterUnavailableError):
        await resolver.mint(TENANT, "coding")


@pytest.mark.parametrize(
    "patch",
    [
        {"tenant_id": str(uuid4())},
        {"app": "other"},
        {"license_id": "not-a-uuid"},
        {"token": "has space"},
        {"token": ""},
        {"expires_at": "2099-01-01"},
        {"expires_at": "2000-01-01T00:00:00+00:00"},
        {"token": None},
    ],
)
async def test_untrusted_mint_responses_are_rejected(minter, patch):
    resolver, authority, _ = minter
    authority.mint_body = {
        "token": "minted",
        "expires_at": authority.expires.isoformat(),
        "tenant_id": str(TENANT),
        "bundle": "care-bus",
        "app": "coding",
        "license_id": str(LICENCE),
        "token_id": str(uuid4()),
        **patch,
    }
    with pytest.raises(ExecutionDeniedError):
        await resolver.mint(TENANT, "coding")
    assert not resolver.cache


async def test_non_object_mint_response_is_rejected(minter):
    resolver, authority, _ = minter
    authority.mint_body = None
    authority.mint_status = 201

    def handler(request):
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "kc", "expires_in": 60})
        return httpx.Response(201, content=b"[1]")

    resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExecutionDeniedError):
        await resolver.mint(TENANT, "coding")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": "unauthorized_client"}),
        (200, {"access_token": "kc"}),
        (200, {"access_token": "kc", "expires_in": 0}),
        (200, {"access_token": "kc", "expires_in": True}),
        (200, {"access_token": "has space", "expires_in": 60}),
        (200, []),
        (200, b"not json"),
    ],
)
async def test_client_credentials_failures_are_unavailable(minter, status, body):
    resolver, authority, _ = minter
    authority.token_status, authority.token_body = status, body
    with pytest.raises(MinterUnavailableError):
        await resolver.mint(TENANT, "coding")
    assert resolver.service_token is None and not authority.mint_calls


async def test_oversized_responses_are_rejected(minter):
    resolver, _, _ = minter

    def handler(request):
        return httpx.Response(200, content=b'{"x":"' + b"a" * 70000 + b'"}')

    resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(MinterUnavailableError):
        await resolver.mint(TENANT, "coding")


async def test_broker_resolution_checks_licence_identity_and_sync_is_cache_only(minter):
    resolver, _, clock = minter
    with pytest.raises(ExecutionDeniedError):
        resolver.resolve(TENANT, uuid7(), reference())
    token = await resolver.aresolve(TENANT, uuid7(), reference())
    assert resolver.resolve(TENANT, uuid7(), reference()) == token
    with pytest.raises(ExecutionDeniedError):
        resolver.resolve(TENANT, uuid7(), reference(licence=uuid4()))
    with pytest.raises(ExecutionDeniedError):
        await resolver.aresolve(TENANT, uuid7(), reference(licence=uuid4()))
    assert await resolver.licence_id(TENANT, "coding") == LICENCE
    clock.now += 3600
    with pytest.raises(ExecutionDeniedError):
        resolver.resolve(TENANT, uuid7(), reference())


async def test_cache_is_bounded(minter, monkeypatch):
    resolver, authority, _ = minter
    monkeypatch.setattr("src.credentials.minter.CACHE_ENTRIES", 1)
    await resolver.mint(TENANT, "coding")
    await resolver.mint(TENANT, "strata-rag")
    assert list(resolver.cache) == [(TENANT, "strata-rag", None)]


def test_secret_file_is_required_and_bounded(tmp_path):
    config = configuration(tmp_path)
    resolver = LicenceMinterCredentialResolver(config)
    resolver.read()
    assert resolver.secret == "s" * 40
    config.licence_minter_secret_file.write_text("bad secret\n")
    with pytest.raises(ExecutionDeniedError):
        resolver.read()
    config.licence_minter_secret_file.unlink()
    with pytest.raises(ExecutionDeniedError):
        LicenceMinterCredentialResolver(config)
    with pytest.raises(ValueError, match="not configured"):
        LicenceMinterCredentialResolver(settings())


@pytest.mark.parametrize(
    "updates",
    [
        {"licence_minter_url": None},
        {"licence_minter_token_url": None},
        {"licence_minter_secret_file": None},
        {"licence_minter_url": "http://blocked.internal"},
        {"licence_minter_token_url": "https://user:pw@auth.example/token"},
        {"licence_minter_url": "https://licenses.example?x=1"},
    ],
)
def test_minter_configuration_fails_closed(tmp_path, updates):
    with pytest.raises(ValueError):
        configuration(tmp_path, **updates)


def test_internal_http_is_explicit(tmp_path):
    config = configuration(
        tmp_path,
        licence_minter_url="http://blocked.svc.cluster.local",
        license_allow_http=True,
    )
    assert config.licence_minter_enabled


def test_minting_is_off_by_default():
    assert settings().licence_minter_enabled is False


def test_worker_selects_the_minter_only_when_enabled(tmp_path):
    from src.credentials.resolver import FileCredentialResolver
    from src.execution.worker import credential_resolver

    bundle = tmp_path / "bundle.json"
    assert isinstance(
        credential_resolver(settings(credential_bundle_file=bundle)), FileCredentialResolver
    )
    assert isinstance(credential_resolver(configuration(tmp_path)), LicenceMinterCredentialResolver)


async def test_broker_awaits_a_minting_resolver():
    from src.broker.client import Broker
    from src.config import Settings

    tenant = uuid4()

    class Minting:
        def __init__(self):
            self.calls = []

        async def aresolve(self, tenant_id, profile_id, ref):
            self.calls.append((tenant_id, ref.app_key))
            from pydantic import SecretStr

            return SecretStr("minted")

        def resolve(self, *args):  # pragma: no cover - must not be used by identity()
            raise AssertionError("sync resolve used")

    class Licences:
        async def authenticate(self, token, app):
            assert token == "minted" and app == "coding"
            return SimpleNamespace(tenant_id=tenant, license_id=LICENCE)

    repository = SimpleNamespace(settings=Settings.model_construct(api_max_inflight=1))
    credentials = Minting()
    broker = Broker(
        SimpleNamespace(repository=repository),
        None,
        credentials,
        Licences(),
        None,
    )
    try:
        token = await broker.identity(
            {"tenant_id": tenant}, SimpleNamespace(profile_id=uuid7()), reference()
        )
        assert token.get_secret_value() == "minted" and credentials.calls == [(tenant, "coding")]
        with pytest.raises(ExecutionDeniedError):
            await broker.identity(
                {"tenant_id": tenant},
                SimpleNamespace(profile_id=uuid7()),
                reference(licence=uuid4()),
            )
    finally:
        await broker.close()


async def test_bundle_is_named_explicitly_and_checked(minter):
    resolver, authority, _ = minter
    token = await resolver.aresolve(TENANT, uuid7(), reference(bundle="agent"))
    assert authority.mint_calls[-1].url.params["bundle"] == "agent"
    # Cached per bundle: the same app in another bundle is a separate mint.
    assert resolver.resolve(TENANT, uuid7(), reference(bundle="agent")) == token
    with pytest.raises(ExecutionDeniedError):
        resolver.resolve(TENANT, uuid7(), reference(bundle="care-bus"))
    await resolver.mint(TENANT, "coding")
    assert "bundle" not in authority.mint_calls[-1].url.params
    assert len(authority.mint_calls) == 2
    # A token minted in another bundle than the one asked for is rejected.
    authority.mint_body = {
        "token": "minted",
        "expires_at": authority.expires.isoformat(),
        "tenant_id": str(TENANT),
        "bundle": "care-bus",
        "app": "coding",
        "license_id": str(LICENCE),
        "token_id": str(uuid4()),
    }
    with pytest.raises(ExecutionDeniedError):
        await resolver.mint(TENANT, "coding", "agent-2")


async def test_mcp_never_mints_its_own_application_licence(tmp_path):
    authority = FakeAuthority()
    resolver = LicenceMinterCredentialResolver(
        configuration(
            tmp_path, license_introspect_url="https://licenses.example/i", license_app_key="mcp"
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(authority)),
    )
    assert resolver.gateway_credentials is False
    with pytest.raises(ExecutionDeniedError):
        await resolver.mint(TENANT, "mcp", "agent")
    assert authority.token_calls == 0 and authority.mint_calls == []
    await resolver.close()


async def test_broker_skips_the_gateway_only_for_a_minting_resolver():
    from src.broker.client import Broker
    from src.config import Settings

    repository = SimpleNamespace(settings=Settings.model_construct(api_max_inflight=1))
    for credentials, expected in (
        (SimpleNamespace(gateway_credentials=False), False),
        (SimpleNamespace(), True),
    ):
        broker = Broker(
            SimpleNamespace(repository=repository),
            None,
            credentials,
            None,
            None,
        )
        try:
            assert broker.gateway_credentials is expected
        finally:
            await broker.close()
