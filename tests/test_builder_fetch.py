"""Broker.fetch_document: spec fetch through the source credential and the egress policy."""

from types import SimpleNamespace
from uuid import uuid4, uuid7

import httpx
import pytest
from pydantic import SecretStr, TypeAdapter

from src.broker.client import Broker
from src.execution.models import ExecutionDeniedError
from src.sources.models import SourceAuth
from src.sources.registry import ResolvedSource
from tests.fixtures import settings

AUTH = TypeAdapter(SourceAuth)
SECRET = "spec-secret-value-123"  # pragma: allowlist secret
TENANT = uuid4()
SPEC = b'{"openapi": "3.1.0", "info": {"title": "t", "version": "1"}}'


class FakeMinter:
    def __init__(self):
        self.calls = []

    async def mint(self, tenant, app, bundle=None):
        self.calls.append((tenant, app, bundle))
        return SimpleNamespace(token=SecretStr("minted-licence-token"))


def source(auth, token=SECRET, tenant_owned=True, **extra):
    values = {
        "source_key": "crm",
        "base_url": "https://crm.example.com/v1",
        "auth": AUTH.validate_python(auth),
        "idempotency_header": None,
        "ca_file": None,
        "max_request_bytes": None,
        "max_response_bytes": None,
        "token": SecretStr(token) if token else None,
        "owner_tenant_id": TENANT,
        "source_id": uuid7(),
        "revision": 1,
        "tenant_owned": tenant_owned,
    }
    return ResolvedSource(**(values | extra))


def make(handler=None, *, resolver=None, minter=None, injected=None):
    execution = SimpleNamespace(repository=SimpleNamespace(settings=settings()))
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond)) if handler else None
    broker = Broker(
        execution,
        None,
        minter,
        None,
        None,
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)) if injected else None,
        tenant_http=http,
        resolver=resolver,
    )
    return broker, seen


async def test_tenant_source_fetches_same_origin_path_with_its_credential():
    broker, seen = make(lambda r: httpx.Response(200, content=SPEC))
    raw = await broker.fetch_document(source({"kind": "static_bearer"}), "/openapi.json", 1024, 5.0)
    assert raw == SPEC
    request = seen[0]
    assert str(request.url) == "https://crm.example.com/openapi.json"
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert request.method == "GET"


async def test_query_param_credential_is_sent_as_a_query_parameter():
    broker, seen = make(lambda r: httpx.Response(200, content=SPEC))
    await broker.fetch_document(
        source({"kind": "query_param", "param": "api_key"}), "/spec", 1024, 5.0
    )
    assert seen[0].url.params["api_key"] == SECRET


@pytest.mark.parametrize(
    ("scheme", "header"), [("bearer", "Authorization"), ("x_tenant_id_bearer", "X-Tenant-Id")]
)
async def test_licence_source_mints_for_the_owner_tenant(scheme, header):
    minter = FakeMinter()
    broker, seen = make(lambda r: httpx.Response(200, content=SPEC), minter=minter)
    auth = {"kind": "licence", "app_key": "crm", "scheme": scheme}
    await broker.fetch_document(source(auth, token=None), "/openapi.json", 1024, 5.0)
    assert minter.calls == [(TENANT, "crm", None)]
    assert seen[0].headers[header] == "Bearer minted-licence-token"


async def test_licence_source_without_a_minter_is_denied():
    broker, _ = make(lambda r: httpx.Response(200, content=SPEC))
    with pytest.raises(ExecutionDeniedError):
        await broker.fetch_document(
            source({"kind": "licence", "app_key": "crm"}, token=None), "/openapi.json", 1024, 5.0
        )


@pytest.mark.parametrize("path", ["openapi.json", "/a?x=1", "/a/../b", "/a b", "//", "/a#f", ""])
async def test_only_plain_absolute_paths_are_fetched(path):
    broker, seen = make(lambda r: httpx.Response(200, content=SPEC))
    if path == "//":
        # An absolute path made only of slashes still stays on the source origin.
        await broker.fetch_document(source({"kind": "none"}, token=None), path, 1024, 5.0)
        assert seen[0].url.host == "crm.example.com"
        return
    with pytest.raises(ValueError, match="path"):
        await broker.fetch_document(source({"kind": "none"}, token=None), path, 1024, 5.0)
    assert seen == []


async def test_oversized_and_non_2xx_answers_are_refused():
    broker, _ = make(lambda r: httpx.Response(200, content=b"x" * 2000))
    with pytest.raises(ValueError, match="larger"):
        await broker.fetch_document(source({"kind": "none"}, token=None), "/s", 1000, 5.0)
    broker, _ = make(lambda r: httpx.Response(404))
    with pytest.raises(ValueError, match="404"):
        await broker.fetch_document(source({"kind": "none"}, token=None), "/s", 1000, 5.0)


async def test_a_body_that_echoes_the_credential_is_refused():
    broker, _ = make(lambda r: httpx.Response(200, content=b'{"leak": "' + SECRET.encode() + b'"}'))
    with pytest.raises(ExecutionDeniedError):
        await broker.fetch_document(source({"kind": "static_bearer"}), "/s", 1000, 5.0)


async def test_transport_failures_are_oserror_not_httpx_errors():
    def boom(request):
        raise httpx.ConnectError("secret-looking detail")

    broker, _ = make(boom)
    with pytest.raises(OSError) as error:
        await broker.fetch_document(source({"kind": "none"}, token=None), "/s", 1000, 5.0)
    assert "secret-looking" not in str(error.value)


async def test_platform_source_uses_the_allow_list_client_and_never_follows_redirects():
    broker, seen = make(
        lambda r: httpx.Response(302, headers={"Location": "https://evil.example.com/"}),
        injected=True,
    )
    with pytest.raises(ValueError, match="302"):
        await broker.fetch_document(
            source({"kind": "none"}, token=None, tenant_owned=False), "/s", 1000, 5.0
        )
    assert len(seen) == 1


async def resolve_private(host, port):
    return ["10.0.0.5"]


@pytest.mark.parametrize(
    "base_url", ["https://crm.example.com/v1", "https://db.internal/v1", "https://localhost/v1"]
)
async def test_ssrf_policy_applies_to_spec_fetch(base_url):
    # No injected transport: the real pinned transport must refuse a private resolution and
    # internal names before any connection is made.
    broker, _ = make(resolver=resolve_private)
    with pytest.raises(ExecutionDeniedError):
        await broker.fetch_document(
            source({"kind": "none"}, token=None, base_url=base_url), "/openapi.json", 1000, 5.0
        )
    await broker.tenant_client().aclose()
