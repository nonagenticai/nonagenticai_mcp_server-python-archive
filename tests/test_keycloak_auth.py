"""Builtin-caller authentication: care-bus licence OR Keycloak JWT, and the closed /mcp bypass.

Ported from main #41 (tests/test_keycloak_auth.py) onto the pure-ASGI AuthenticationMiddleware
(src/auth/protocol.py). The JWKS fetch is monkeypatched (PyJWKClient.fetch_data) with a JWK set
built from an RSA key generated here, so kid selection, RS256 verification and every claim check
run for real. Error bodies use this service's {"error": "unauthorized"} shape, not main's codes.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from src.auth import keycloak
from src.auth.context import current_caller
from src.auth.licenses import AuthenticationError
from src.auth.protocol import AuthenticationMiddleware

KC_URL = "https://auth.example.test"
REALM = "portal"
ISSUER = f"{KC_URL}/realms/{REALM}"
AUDIENCE = "nonagenticai-mcp"
CLIENT = "carebus-tool-executor"
KID = "test-kid-1"
TENANT = "0195f48b-d000-7000-8000-000000000001"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def _token(key: Any = _KEY, kid: str = KID, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [AUDIENCE, "account"],
        "azp": CLIENT,
        "sub": "service-account-sub",
        "iat": now,
        "exp": now + 300,
    }
    for name, value in overrides.items():
        if value is None:
            claims.pop(name, None)
        else:
            claims[name] = value
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def kc_env(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_URL", KC_URL)
    monkeypatch.setenv("KEYCLOAK_REALM", REALM)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENTS", f"{CLIENT}, other-client")
    fetches: list[str] = []

    def fake_fetch(self):  # PyJWKClient.fetch_data
        fetches.append(self.uri)
        return _jwks()

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fake_fetch)
    keycloak.get_keycloak_config.cache_clear()
    keycloak._jwks_client.cache_clear()
    yield fetches
    keycloak.get_keycloak_config.cache_clear()
    keycloak._jwks_client.cache_clear()


@pytest.fixture
def no_kc_env(monkeypatch):
    for name in keycloak.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    keycloak.get_keycloak_config.cache_clear()
    yield
    keycloak.get_keycloak_config.cache_clear()


CAREBUS_TENANT_LICENCE = {
    "active": True,
    "tenant_id": TENANT,
    "subject_id": "lic-subject",
    "subject_kind": "tenant",
    "app": {"key": "care-bus"},
}


class FakeLicenses:
    def __init__(self, answer):
        self.introspect = AsyncMock(side_effect=answer)


def _licences(answers: dict[str, dict] | None = None, default: dict | None = None):
    def answer(token: str) -> dict:
        info = (answers or {}).get(token, default)
        if not info or info.get("active") is not True:
            raise AuthenticationError("invalid license")
        return info

    return FakeLicenses(answer)


@pytest.fixture
def introspect():
    return _licences(default=CAREBUS_TENANT_LICENCE)


async def _echo(scope, receive, send):
    caller = current_caller()
    body = (
        {
            "auth_method": caller.principal.auth_method,
            "principal": caller.principal.subject,
            "tenant": caller.principal.tenant_id,
        }
        if caller
        else {"caller": None}
    )
    await JSONResponse(body)(scope, receive, send)


def _client(licences: Any = None) -> TestClient:
    state = SimpleNamespace(licenses=licences or _licences())
    return TestClient(AuthenticationMiddleware(_echo, state))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- the closed bypass -------------------------------------------------------------------------


def test_unauthenticated_post_mcp_is_401(kc_env):
    r = _client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_unauthenticated_post_mcp_on_the_real_app_is_401():
    from src.asgi import app

    r = TestClient(app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_trusted_network_ip_without_token_is_401(kc_env):
    # TestClient's client host is not an IP; pin a loopback one, the old trusted default.
    r = TestClient(_client().app, client=("127.0.0.1", 5555)).post("/mcp")
    assert r.status_code == 401
    r = TestClient(_client().app, client=("10.42.0.12", 5555)).post(
        "/mcp", headers={"X-Tenant-Id": TENANT}
    )
    assert r.status_code == 401


# --- (a) licence -------------------------------------------------------------------------------


def test_license_token_passes(introspect, no_kc_env):
    r = _client(introspect).post("/mcp", headers={"X-Tenant-Id": "Bearer lic-123"})
    assert r.status_code == 200
    assert r.json() == {"auth_method": "license", "principal": "lic-subject", "tenant": TENANT}
    introspect.introspect.assert_awaited_once_with("lic-123")


def test_invalid_license_does_not_fall_through_to_jwt(kc_env):
    r = _client(_licences(default={"active": False})).post(
        "/mcp", headers={"X-Tenant-Id": "Bearer bad", **_bearer(_token())}
    )
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_authorization_is_never_introspected_as_a_license(introspect, no_kc_env):
    r = _client(introspect).post("/mcp", headers=_bearer("lic-looking-token"))
    assert r.status_code == 401
    introspect.introspect.assert_not_awaited()


# --- (b) Keycloak JWT --------------------------------------------------------------------------


def test_valid_jwt_passes_without_tenant(kc_env, introspect):
    r = _client(introspect).post("/mcp", headers=_bearer(_token()))
    assert r.status_code == 200, r.text
    # no tenant_id claim: authenticated, but no tenant is invented (sub is not a tenant)
    assert r.json() == {"auth_method": "keycloak", "principal": CLIENT, "tenant": None}
    introspect.introspect.assert_not_awaited()
    assert kc_env == [f"{ISSUER}/protocol/openid-connect/certs"]


def test_jwks_is_cached_across_requests(kc_env):
    client = _client()
    for _ in range(3):
        assert client.post("/mcp", headers=_bearer(_token())).status_code == 200
    assert len(kc_env) == 1


def test_valid_jwt_with_uuid_tenant_claim_sets_tenant(kc_env):
    r = _client().post("/mcp", headers=_bearer(_token(tenant_id=TENANT)))
    assert r.status_code == 200
    assert r.json()["tenant"] == TENANT


def test_non_uuid_tenant_claim_is_ignored(kc_env):
    r = _client().post("/mcp", headers=_bearer(_token(tenant_id="n8n-dev")))
    assert r.status_code == 200
    assert r.json()["tenant"] is None


def test_bare_tenant_header_does_not_bind_for_jwt_caller(kc_env):
    r = _client().post("/mcp", headers={**_bearer(_token()), "X-Tenant-Id": str(uuid.uuid4())})
    assert r.status_code == 200
    assert r.json()["tenant"] is None


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _token(azp="portal-frontend"), id="wrong-azp"),
        pytest.param(lambda: _token(aud="account"), id="wrong-aud"),
        pytest.param(lambda: _token(aud=None), id="missing-aud"),
        pytest.param(lambda: _token(iss=f"{KC_URL}/realms/other"), id="wrong-iss"),
        pytest.param(lambda: _token(exp=int(time.time()) - 60), id="expired"),
        pytest.param(lambda: _token(exp=None), id="missing-exp"),
        pytest.param(lambda: _token(azp=None), id="missing-azp"),
        pytest.param(lambda: _token(key=_OTHER_KEY), id="bad-signature"),
        pytest.param(lambda: _token(kid="unknown-kid"), id="unknown-kid"),
        pytest.param(
            lambda: jwt.encode(
                {"iss": ISSUER, "aud": AUDIENCE, "azp": CLIENT, "exp": int(time.time()) + 60},
                "x" * 32,
                algorithm="HS256",
                headers={"kid": KID},
            ),
            id="hs256",
        ),
        pytest.param(lambda: "not-a-jwt", id="garbage"),
    ],
)
def test_bad_jwt_is_401(kc_env, token):
    r = _client().post("/mcp", headers=_bearer(token()))
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_iat_in_the_future_is_tolerated(kc_env):
    # Keycloak/pod clock skew must not 401 every caller (see keycloak.py verify_iat).
    r = _client().post("/mcp", headers=_bearer(_token(iat=int(time.time()) + 30)))
    assert r.status_code == 200


# --- configuration -----------------------------------------------------------------------------


def test_jwt_rejected_when_keycloak_not_configured(no_kc_env):
    assert keycloak.get_keycloak_config() is None
    r = _client().post("/mcp", headers=_bearer(_token()))
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("missing", keycloak.REQUIRED_ENV)
def test_partial_config_disables_jwt_path(kc_env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    keycloak.get_keycloak_config.cache_clear()
    assert keycloak.get_keycloak_config() is None
    assert _client().post("/mcp", headers=_bearer(_token())).status_code == 401


def test_empty_allowed_clients_list_disables_jwt_path(kc_env, monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ALLOWED_CLIENTS", " , ")
    keycloak.get_keycloak_config.cache_clear()
    assert keycloak.get_keycloak_config() is None


def test_config_issuer_and_certs_url(kc_env):
    cfg = keycloak.get_keycloak_config()
    assert cfg is not None
    assert cfg.issuer == ISSUER
    assert cfg.certs_url == f"{ISSUER}/protocol/openid-connect/certs"
    assert cfg.allowed_clients == frozenset({CLIENT, "other-client"})


# --- /api/health (care-bus-api probes {MCP_URL minus /mcp}/api/health) --------------------------


def test_api_health_is_public_on_the_real_app():
    from src.asgi import app

    client = TestClient(app)
    for headers in ({}, {"Authorization": "Bearer whatever"}):
        r = client.get("/api/health", headers=headers)
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"


def test_api_prefix_is_not_a_route_beyond_health():
    from src.asgi import app

    # Only /mcp... is protected here; anything else outside the explicit routes is a 404.
    assert TestClient(app).get("/api/healthz-not-a-route").status_code == 404


def test_jwks_fetch_sends_a_non_default_user_agent():
    """Cloudflare 403s urllib's default Python-urllib User-Agent on the realm certs URL."""
    keycloak._jwks_client.cache_clear()
    client = keycloak._jwks_client("https://kc.example/realms/r/protocol/openid-connect/certs")
    ua = client.headers.get("User-Agent", "")
    assert ua and not ua.startswith("Python-urllib")
    keycloak._jwks_client.cache_clear()


# --- path restriction: builtin callers may only use /mcp --------------------------------------


@pytest.mark.parametrize("path", ["/mcp/runs", "/mcp/sse", "/mcp/tools", "/mcp/bindings"])
def test_valid_jwt_is_403_off_the_protocol_endpoint(kc_env, path):
    assert _client().get(path, headers=_bearer(_token())).status_code == 403


def test_invalid_jwt_is_401_off_the_protocol_endpoint(kc_env):
    assert _client().get("/mcp/tools", headers=_bearer("garbage")).status_code == 401


def test_carebus_licence_is_403_off_the_protocol_endpoint(introspect, no_kc_env):
    r = _client(introspect).post("/mcp/runs", headers={"X-Tenant-Id": "Bearer lic-123"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "info",
    [
        {**CAREBUS_TENANT_LICENCE, "app": {"key": "mcp"}},
        {**CAREBUS_TENANT_LICENCE, "app": {"key": "w2v"}},
        {k: v for k, v in CAREBUS_TENANT_LICENCE.items() if k != "app"},
        {**CAREBUS_TENANT_LICENCE, "tenant_id": None, "subject_id": None},
    ],
)
def test_non_carebus_or_tenantless_licence_without_binding_is_401(no_kc_env, info):
    r = _client(_licences(default=info)).post("/mcp", headers={"X-Tenant-Id": "Bearer x"})
    assert r.status_code == 401


def test_session_id_is_never_pinned_for_builtin_callers(kc_env):
    async def with_session(scope, receive, send):
        await JSONResponse({}, headers={"mcp-session-id": "s-1"})(scope, receive, send)

    client = TestClient(AuthenticationMiddleware(with_session, SimpleNamespace()))
    r = client.post("/mcp", headers=_bearer(_token()))
    assert r.status_code == 200
    assert "mcp-session-id" not in r.headers
    assert r.headers["cache-control"] == "no-store"
