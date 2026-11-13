"""Source auth adapters, OAuth2 token cache, and owner-scoped source resolution."""

import asyncio
import base64
import json
from urllib.parse import parse_qs
from uuid import uuid4, uuid7

import httpx
import pytest
from pydantic import SecretStr, TypeAdapter

from src.auth.licenses import LicenseIdentity
from src.broker.auth import AuthAdapters, AuthFailedError
from src.broker.egress import EgressDeniedError
from src.broker.policy import transport
from src.catalog.models import AuthAdapter, OperationManifest
from src.credentials.minter import MintedCredential
from src.execution.models import ExecutionDeniedError
from src.sources.models import SourceAuth, TenantSourceCreate
from src.sources.registry import ResolvedSource, SourceRegistry
from src.sources.repository import TenantSourceRepository
from tests.fixtures import ACTOR, TENANT_A, operation, register_source, settings
from tests.test_execution import claim, make_broker, setup_runtime

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
SECRET = "s3cr3t-value-abc"  # pragma: allowlist secret
AUTH = TypeAdapter(SourceAuth)


def resolved(auth: dict, token: str | None = SECRET, **overrides) -> ResolvedSource:
    values = {
        "source_key": "crm",
        "base_url": "https://crm.example.com/v1",
        "auth": AUTH.validate_python(auth),
        "idempotency_header": None,
        "ca_file": None,
        "max_request_bytes": None,
        "max_response_bytes": None,
        "token": SecretStr(token) if token is not None else None,
        "owner_tenant_id": uuid4(),
        "source_id": uuid7(),
        "revision": 1,
        "tenant_owned": True,
    }
    return ResolvedSource(**(values | overrides))


def adapters(handler=None, ttl=3600, clock=None):
    calls = []

    def respond(request):
        calls.append(request)
        return handler(request) if handler else httpx.Response(200, json=token_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
    kwargs = {"clock": clock} if clock else {}
    return AuthAdapters(ttl, lambda: client, **kwargs), calls


def token_body(access="at-1", expires=3600, kind="Bearer", **more):
    return {"access_token": access, "token_type": kind, "expires_in": expires, **more}


OAUTH = {
    "kind": "oauth2_client_credentials",
    "token_url": "https://idp.example.com/oauth/token",
    "client_id": "client-1",
    "scope": "read write",
    "audience": "https://crm.example.com",
}


# --- simple kinds -----------------------------------------------------------------------------


async def test_none_sends_nothing():
    headers, query = {}, {}
    assert await adapters()[0].apply(resolved({"kind": "none"}, None), headers, query) == []
    assert headers == {} and query == {}


async def test_static_bearer_and_header():
    a = adapters()[0]
    headers = {}
    assert await a.apply(resolved({"kind": "static_bearer"}), headers, {}) == [SECRET]
    assert headers == {"Authorization": "Bearer " + SECRET}
    headers = {}
    await a.apply(resolved({"kind": "static_header", "header": "X-Api-Key"}), headers, {})
    assert headers == {"X-Api-Key": SECRET}


async def test_basic_encodes_user_and_password_with_spaces():
    headers = {}
    source = resolved({"kind": "basic", "username": "svc"}, token="pass word 1")
    secrets = await adapters()[0].apply(source, headers, {})
    blob = base64.b64encode(b"svc:pass word 1").decode()
    assert headers == {"Authorization": "Basic " + blob}
    assert "pass word 1" in secrets and blob in secrets


SPACED = "a b&c"  # pragma: allowlist secret
MAPPED = "mapped"  # pragma: allowlist secret


async def test_query_param_is_added_last_and_collisions_are_rejected():
    source = resolved({"kind": "query_param", "param": "api_key"}, token=SPACED)
    query = {"limit": "5"}
    secrets = await adapters()[0].apply(source, query=query, headers={})
    assert list(query) == ["limit", "api_key"] and query["api_key"] == SPACED
    assert "a%20b%26c" in secrets  # the encoded form is a leak target too
    with pytest.raises(AuthFailedError):
        await adapters()[0].apply(source, headers={}, query={"api_key": MAPPED})


async def test_mapped_header_may_not_collide_with_the_credential_header():
    source = resolved({"kind": "static_header", "header": "X-Api-Key"})
    with pytest.raises(AuthFailedError):
        await adapters()[0].apply(source, {"x-api-key": "mapped"}, {})
    with pytest.raises(AuthFailedError):
        await adapters()[0].apply(resolved({"kind": "static_bearer"}), {"authorization": "x"}, {})


async def test_secretful_kind_without_a_secret_is_refused():
    with pytest.raises(ExecutionDeniedError):
        await adapters()[0].apply(resolved({"kind": "static_bearer"}, None), {}, {})


def optional_operation(optional: bool) -> OperationManifest:
    mappings = (
        {"location": "query", "target": "q", "value": {"source": "input", "pointer": "/q"}},
        {
            "location": "query",
            "target": "limit",
            "value": {"source": "input", "pointer": "/limit"},
            "optional": optional,
        },
        {
            "location": "body",
            "target": "/note",
            "value": {"source": "input", "pointer": "/note"},
            "optional": optional,
        },
    )
    payload = {
        **operation(1).model_dump(mode="json"),
        "mappings": mappings,
        "query_fields": ["q", "limit"],
    }
    return OperationManifest.model_validate_json(json.dumps(payload))


def test_optional_inputs_are_omitted_when_absent_and_required_ones_deny():
    op = optional_operation(True)
    _, query, _, body = transport(op, {"q": "x"}, {})
    assert query == {"q": "x"} and body is None
    _, query, _, body = transport(op, {"q": "x", "limit": 3, "note": "n"}, {})
    assert query == {"q": "x", "limit": "3"} or query == {"q": "x", "limit": 3}
    assert body == {"note": "n"}
    with pytest.raises(ValueError):
        transport(optional_operation(False), {"q": "x"}, {})


# --- OAuth2 client credentials -----------------------------------------------------------------


async def test_oauth_basic_client_auth_request_shape_and_header():
    a, calls = adapters()
    headers = {}
    secrets = await a.apply(resolved(OAUTH), headers, {})
    assert headers["Authorization"] == "Bearer at-1" and "at-1" in secrets and SECRET in secrets
    request = calls[0]
    assert request.method == "POST" and str(request.url) == OAUTH["token_url"]
    form = parse_qs(request.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "scope": ["read write"],
        "audience": ["https://crm.example.com"],
    }
    expected = base64.b64encode(f"client-1:{SECRET}".encode()).decode()
    assert request.headers["Authorization"] == "Basic " + expected
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"


async def test_oauth_post_client_auth_puts_credentials_in_the_body():
    a, calls = adapters()
    await a.apply(resolved(OAUTH | {"client_auth": "post"}), {}, {})
    form = parse_qs(calls[0].content.decode())
    assert form["client_id"] == ["client-1"] and form["client_secret"] == [SECRET]
    assert "Authorization" not in calls[0].headers


async def test_oauth_token_is_cached_until_ttl_and_refreshed_after():
    now = [100.0]
    a, calls = adapters(
        lambda r: httpx.Response(200, json=token_body(f"at-{len(calls)}", 120)),
        clock=lambda: now[0],
    )
    source = resolved(OAUTH)
    for _ in range(3):
        headers = {}
        await a.apply(source, headers, {})
    assert len(calls) == 1 and headers["Authorization"] == "Bearer at-1"
    now[0] += 120 - 30 + 1  # past expires_in minus the 30 s skew
    headers = {}
    await a.apply(source, headers, {})
    assert len(calls) == 2 and headers["Authorization"] == "Bearer at-2"


async def test_oauth_lifetime_is_capped_by_settings_and_short_tokens_are_not_cached():
    now = [0.0]
    capped = resolved(OAUTH)
    a, calls = adapters(
        lambda r: httpx.Response(200, json=token_body("x", 86400)), ttl=60, clock=lambda: now[0]
    )
    await a.apply(capped, {}, {})
    now[0] = 29  # cap 60 s minus the 30 s skew
    await a.apply(capped, {}, {})
    assert len(calls) == 1
    now[0] = 31
    await a.apply(capped, {}, {})
    assert len(calls) == 2
    short, short_calls = adapters(lambda r: httpx.Response(200, json=token_body("y", 20)))
    source = resolved(OAUTH)
    await short.apply(source, {}, {})
    await short.apply(source, {}, {})
    assert len(short_calls) == 2  # a token that expires inside the skew is never reused


async def test_oauth_refresh_is_single_flight():
    release = asyncio.Event()
    calls = []

    async def slow(request):
        calls.append(request)
        await release.wait()
        return httpx.Response(200, json=token_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow), trust_env=False)
    a = AuthAdapters(3600, lambda: client)
    source = resolved(OAUTH)
    tasks = [asyncio.create_task(a.apply(source, {}, {})) for _ in range(8)]
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(*tasks)
    assert len(calls) == 1


async def test_oauth_401_eviction_and_revision_keying():
    a, calls = adapters(lambda r: httpx.Response(200, json=token_body(f"at-{len(calls)}")))
    source = resolved(OAUTH)
    await a.apply(source, {}, {})
    a.evict(source)
    headers = {}
    await a.apply(source, headers, {})
    assert len(calls) == 2 and headers["Authorization"] == "Bearer at-2"
    # A new revision of the same source (rotated secret) never reuses the cached token.
    from dataclasses import replace

    await a.apply(replace(source, revision=2), {}, {})
    assert len(calls) == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "invalid_client"}),
        httpx.Response(200, json=token_body(kind="mac")),
        httpx.Response(200, json={"token_type": "bearer"}),
        httpx.Response(200, json=token_body("bad token")),
        httpx.Response(200, json=token_body("bad\r\nInjected: 1")),
        httpx.Response(200, json=token_body(5)),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b"x" * 70000),
    ],
)
async def test_oauth_bad_token_responses_fail_closed(response):
    a, _ = adapters(lambda r: response)
    with pytest.raises(AuthFailedError):
        await a.apply(resolved(OAUTH), {}, {})


async def test_oauth_token_endpoint_goes_through_the_egress_client():
    def refused(request):
        raise EgressDeniedError

    a, _ = adapters(refused)
    with pytest.raises(EgressDeniedError):  # traced as ssrf_blocked by the broker
        await a.apply(resolved(OAUTH), {}, {})


# --- owner-scoped resolution -------------------------------------------------------------------


def registry(database, **overrides):
    config = settings(
        source_secret_key=SecretStr(KEY),
        source_cache_ttl_seconds=0,
        source_allowed_hosts=("*.example",),
        **overrides,
    )
    return SourceRegistry(database.pool, config), config


def tenant_repo(database):
    return TenantSourceRepository(
        database.pool, SecretStr(KEY), max_sources=5, licence_hosts=("*.example.com",)
    )


async def test_same_key_resolves_by_owner_never_by_key_alone(database):
    await register_source(database.pool, "shared_1", base_url="https://platform.example")
    body = TenantSourceCreate.model_validate(
        {
            "source_key": "shared_1",
            "base_url": "https://tenant.example.com/v1",
            "auth": {"kind": "static_bearer"},
            "token": SECRET,
        }
    )
    await tenant_repo(database).create(TENANT_A, body, ACTOR)
    sources, config = registry(database)

    platform = await sources.get(config.platform_tenant_id, "shared_1")
    assert platform.base_url == "https://platform.example" and not platform.tenant_owned
    assert platform.auth.kind == "licence" and platform.token is None

    tenant = await sources.get(TENANT_A, "shared_1")
    assert tenant.base_url == "https://tenant.example.com/v1" and tenant.tenant_owned
    assert tenant.token is not None and tenant.token.get_secret_value() == SECRET

    with pytest.raises(ExecutionDeniedError):  # another tenant has no such source
        await sources.get(uuid4(), "shared_1")
    # And a key only the tenant owns is invisible to platform services.
    await tenant_repo(database).create(
        TENANT_A, body.model_copy(update={"source_key": "only_mine"}), ACTOR
    )
    with pytest.raises(ExecutionDeniedError):
        await sources.get(config.platform_tenant_id, "only_mine")
    assert (await sources.get(TENANT_A, "only_mine")).tenant_owned


async def test_tenant_licence_source_needs_an_operator_allowed_host(database):
    body = TenantSourceCreate.model_validate(
        {
            "source_key": "rag",
            "base_url": "https://rag.example.com/v1",
            "auth": {"kind": "licence", "app_key": "strata-rag"},
        }
    )
    await tenant_repo(database).create(TENANT_A, body, ACTOR)
    sources, _ = registry(database)  # operator allow-list is *.example only
    with pytest.raises(ExecutionDeniedError):
        await sources.get(TENANT_A, "rag")
    allowed, _ = registry(
        database,
    )
    allowed.settings = allowed.settings.model_copy(
        update={"source_allowed_hosts": ("rag.example.com",)}
    )
    assert (await allowed.get(TENANT_A, "rag")).auth.kind == "licence"


async def test_tenant_port_outside_the_allow_list_is_denied_at_call_time(database):
    body = TenantSourceCreate.model_validate(
        {
            "source_key": "crm",
            "base_url": "https://crm.example.com/v1",
            "auth": {"kind": "none"},
        }
    )
    await tenant_repo(database).create(TENANT_A, body, ACTOR)
    sources, _ = registry(database)
    assert (await sources.get(TENANT_A, "crm")).auth.kind == "none"
    sources.settings = sources.settings.model_copy(update={"builder_allowed_ports": (8443,)})
    with pytest.raises(ExecutionDeniedError):
        await sources.get(TENANT_A, "crm")


async def test_broker_never_uses_a_tenant_source_for_a_platform_service(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    # TENANT_A registers a source under the platform service's destination key.
    clash = TenantSourceCreate.model_validate(
        {
            "source_key": "destination_1",
            "base_url": "https://attacker.example.com/v1",
            "auth": {"kind": "static_bearer"},
            "token": SECRET,
        }
    )
    await tenant_repo(database).create(TENANT_A, clash, ACTOR)
    broker, seen, _, _ = await make_broker(runtime, tmp_path)
    broker.sources = SourceRegistry(
        database.pool,
        runtime.config.model_copy(
            update={"source_secret_key": SecretStr(KEY), "source_cache_ttl_seconds": 0}
        ),
    )
    try:
        await broker.preflight(row)
        assert await broker.call(row, str(runtime.refs[0].id), {}) == {}
        assert seen[0].url.host == "service1.example"  # the platform source, not the tenant's
        assert SECRET not in seen[0].headers.get("Authorization", "")
    finally:
        await broker.close()


# --- broker: tenant-owned path ------------------------------------------------------------------


async def tenant_broker(runtime, tmp_path, tenant_handler, **limits):
    """A broker whose first service resolves to a tenant-owned query_param source."""
    broker, platform_seen, _, _ = await make_broker(runtime, tmp_path)
    tenant_seen = []

    def respond(request):
        tenant_seen.append(request)
        return tenant_handler(request)

    broker.settings = broker.settings.model_copy(update=limits)
    broker.limiter = type(broker.limiter)(
        broker.settings.builder_source_calls_per_minute, broker.settings.builder_source_concurrency
    )
    broker._tenant_http = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
    source = resolved({"kind": "query_param", "param": "api_key"}, owner_tenant_id=TENANT_A)
    platform_destination = broker.destination

    async def destination(op, service):
        if op.service.id == (await broker.catalog.operation(runtime.refs[0]))[0].service.id:
            return TENANT_A, source
        return await platform_destination(op, service)

    broker.destination = destination
    return broker, tenant_seen, platform_seen


async def test_tenant_source_goes_through_the_tenant_client_with_the_query_secret(
    database, tmp_path
):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, tenant_seen, platform_seen = await tenant_broker(
        runtime, tmp_path, lambda r: httpx.Response(200, json={})
    )
    try:
        assert await broker.call(row, str(runtime.refs[0].id), {}) == {}
        assert platform_seen == [] and len(tenant_seen) == 1
        assert str(tenant_seen[0].url) == f"https://crm.example.com/v1/records?api_key={SECRET}"
    finally:
        await broker.close()


async def test_tenant_response_echoing_the_secret_is_denied(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, _, _ = await tenant_broker(
        runtime, tmp_path, lambda r: httpx.Response(200, json={"records": [SECRET]})
    )
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
    finally:
        await broker.close()


async def test_tenant_source_is_rate_limited_per_tenant_and_service(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, tenant_seen, _ = await tenant_broker(
        runtime,
        tmp_path,
        lambda r: httpx.Response(200, json={}),
        builder_source_calls_per_minute=1,
    )
    try:
        assert await broker.call(row, str(runtime.refs[0].id), {}) == {}
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
        assert len(tenant_seen) == 1
        # The platform service is not subject to the tenant limiter.
        assert await broker.call(row, str(runtime.refs[1].id), {}) == {}
    finally:
        await broker.close()


async def test_empty_response_operation_returns_an_empty_object(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, _, _ = await tenant_broker(runtime, tmp_path, lambda r: httpx.Response(204))
    original = broker.catalog.operation

    async def patched(reference):
        op, status = await original(reference)
        return op.model_copy(update={"empty_response": True}), status

    catalog = broker.catalog
    broker.catalog = type(
        "C",
        (),
        {
            "operation": staticmethod(patched),
            "service": catalog.service,
            "service_owner": catalog.service_owner,
            "may_use_service": catalog.may_use_service,
        },
    )()
    try:
        assert await broker.call(row, str(runtime.refs[0].id), {}) == {}
    finally:
        await broker.close()


# --- draft runs: redacted error excerpt ---------------------------------------------------------


async def excerpts(database, row):
    async with database.pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT error_excerpt FROM mcp_run_api_calls WHERE run_id=%s ORDER BY sequence",
            (row["run_id"],),
        )
        return [r["error_excerpt"] for r in await cursor.fetchall()]


async def test_draft_run_keeps_a_bounded_error_excerpt_and_bound_runs_do_not(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    body = ("upstream says no " * 400)[:5000]
    broker, _, _ = await tenant_broker(
        runtime, tmp_path, lambda r: httpx.Response(500, content=body.encode())
    )
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call({**row, "mode": "draft"}, str(runtime.refs[0].id), {})
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
    finally:
        await broker.close()
    draft, bound = await excerpts(database, row)
    assert draft == body[:2048] and len(draft) == 2048
    assert bound is None


async def test_draft_error_excerpt_withholds_credential_material(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, _, _ = await tenant_broker(
        runtime, tmp_path, lambda r: httpx.Response(401, text=f"bad key {SECRET}")
    )
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call({**row, "mode": "draft"}, str(runtime.refs[0].id), {})
    finally:
        await broker.close()
    (stored,) = await excerpts(database, row)
    assert SECRET not in stored and "withheld" in stored


# --- licence-kind sources minted for the run's tenant (source-adapter services) ----------------------


class Minting:
    """A minting credentials stub: the run's tenant gets a licence unless it is in ``deny``."""

    gateway_credentials = False

    def __init__(self, base, deny=()):
        self.base, self.deny, self.minted, self.license = base, set(deny), [], uuid7()

    def resolve(self, *args):
        return self.base.resolve(*args)

    async def mint(self, tenant, app, bundle=None):
        self.minted.append((tenant, app, bundle))
        if tenant in self.deny:
            raise ExecutionDeniedError("dependency grant")
        return MintedCredential(self.license, float("inf"), SecretStr("minted-licence-token"))


async def licence_source_broker(runtime, tmp_path, *, deny=(), draft_owner=False):
    broker, platform_seen, _, _ = await make_broker(runtime, tmp_path)
    tenant_seen = []

    def respond(request):
        tenant_seen.append(request)
        return httpx.Response(200, json={})

    minting = Minting(broker.credentials, deny)
    broker.credentials = minting

    others = broker.licenses

    class Licenses:
        async def authenticate(self, token, app):
            if token != "minted-licence-token":
                return await others.authenticate(token, app)
            assert app == "app_1"
            return LicenseIdentity(TENANT_A, minting.license, "test", float("inf"))

    broker.licenses = Licenses()
    broker._tenant_http = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
    source = resolved(
        {"kind": "licence", "app_key": "app_1", "bundle": "core"},
        token=None,
        owner_tenant_id=TENANT_A,
    )
    first = (await broker.catalog.operation(runtime.refs[0]))[0].service.id
    platform_destination, catalog = broker.destination, broker.catalog

    async def destination(op, service):
        if op.service.id == first:
            return TENANT_A, source
        return await platform_destination(op, service)

    async def service(reference):
        found, status = await catalog.service(reference)
        if reference.id == first:
            found = found.model_copy(update={"auth_adapter": AuthAdapter.SOURCE})
        return found, status

    broker.destination = destination
    broker.catalog = type(
        "C",
        (),
        {
            "operation": staticmethod(catalog.operation),
            "service": staticmethod(service),
            "service_owner": catalog.service_owner,
            "may_use_service": catalog.may_use_service,
        },
    )()
    return broker, tenant_seen, minting


@pytest.mark.parametrize("mode", ["bound", "draft"])
async def test_licence_source_is_minted_for_the_runs_tenant_from_the_source_row(
    database, tmp_path, mode
):
    runtime = await setup_runtime(database)
    row = {**await claim(runtime), "mode": mode}
    broker, tenant_seen, minting = await licence_source_broker(runtime, tmp_path)
    try:
        assert await broker.call(row, str(runtime.refs[0].id), {}) == {}
    finally:
        await broker.close()
    assert minting.minted == [(TENANT_A, "app_1", "core")]
    assert tenant_seen[0].headers["Authorization"] == "Bearer minted-licence-token"


@pytest.mark.parametrize("mode", ["bound", "draft"])
async def test_licence_source_fails_closed_without_the_tenant_grant(database, tmp_path, mode):
    runtime = await setup_runtime(database)
    row = {**await claim(runtime), "mode": mode}
    broker, tenant_seen, _ = await licence_source_broker(runtime, tmp_path, deny=[TENANT_A])
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
    finally:
        await broker.close()
    assert tenant_seen == []


async def test_licence_source_preflight_mints_and_a_non_minting_resolver_is_denied(
    database, tmp_path
):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, _, minting = await licence_source_broker(runtime, tmp_path)
    try:
        await broker.preflight(row)
        assert minting.minted == [(TENANT_A, "app_1", "core")]
        broker.credentials = minting.base  # the file resolver cannot mint
        with pytest.raises(ExecutionDeniedError):
            await broker.preflight(row)
    finally:
        await broker.close()


async def test_draft_error_excerpt_is_never_recorded_for_a_platform_backend(database, tmp_path):
    runtime = await setup_runtime(database)
    row = {**await claim(runtime), "mode": "draft"}
    broker, _, _, _ = await make_broker(
        runtime, tmp_path, lambda r: httpx.Response(500, text="platform backend internals")
    )
    try:
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[1].id), {})
    finally:
        await broker.close()
    assert set(await excerpts(database, row)) == {None}


class MinterLike:
    """gateway_credentials False and aresolve refusing the gateway app, like the real minter."""

    gateway_credentials = False

    def __init__(self, base, gateway_app, fail_services=False):
        self.base, self.gateway_app, self.fail_services = base, gateway_app, fail_services

    def resolve(self, *args):
        return self.base.resolve(*args)

    async def aresolve(self, tenant, profile, reference):
        if reference.app_key == self.gateway_app or self.fail_services:
            raise ExecutionDeniedError()
        return self.base.resolve(tenant, profile, reference)


async def test_gateway_licence_is_never_reminted_per_api_call(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, seen, _, _ = await make_broker(runtime, tmp_path)
    gateway_app = runtime.credential.gateway.app_key
    base = broker.credentials
    try:
        broker.credentials = MinterLike(base, gateway_app)
        await broker.preflight(row)
        assert await broker.call(row, str(runtime.refs[0].id), {}) == {}
        assert seen
        # the service credential mint failing still denies
        broker.credentials = MinterLike(base, gateway_app, fail_services=True)
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
    finally:
        await broker.close()
