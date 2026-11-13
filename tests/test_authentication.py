"""No live licenses: exercise expiry/cache behavior and untrusted authority responses."""

import json
from datetime import UTC, datetime
from uuid import uuid7

import httpx
import pytest

from src.auth.licenses import (
    AuthenticationError,
    AuthorityUnavailableError,
    LicenseClient,
    LicenseIdentity,
)
from src.auth.management import ManagementAuthorization, read_key
from tests.fixtures import TENANT_A, TENANT_B, settings

LICENSE = uuid7()
SUBJECT = uuid7()


def info(**updates):
    return {
        "active": True,
        "tenant_id": str(TENANT_A),
        "license_id": str(LICENSE),
        "subject_id": str(SUBJECT),
        "subject_kind": "tenant",
        "app": {"key": "mcp"},
        "expires_at": None,
        "token_expires_at": None,
        **updates,
    }


def configuration(**updates):
    return settings(
        license_introspect_url="https://licenses.example/license/introspect",
        license_app_key="mcp",
        **updates,
    )


def client_for(handler, **updates):
    return LicenseClient(
        configuration(**updates), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"active": False},
        {"active": 1},
        {"app": {"key": "wrong"}},
        {"app": None},
        {"tenant_id": None},
        {"tenant_id": "not-uuid"},
        {"license_id": None},
        {"tenant_id": "00000000-0000-0000-0000-000000000000"},
        {"license_id": 7},
        {"subject_kind": "robot"},
        {"subject_id": None},
        {"subject_id": ""},
        {"subject_id": "x" * 257},
        {"expires_at": "2020-01-01T00:00:00+00:00"},
        {"token_expires_at": "2020-01-01T00:00:00+00:00"},
        {"token_expires_at": "2099-01-01"},
        {"expires_at": 123},
    ],
)
async def test_license_rejects_invalid_authority(patch):
    client = client_for(lambda req: httpx.Response(200, json=info(**patch)))
    try:
        with pytest.raises(AuthenticationError):
            await client.authenticate("synthetic-license")
        assert not client.cache
    finally:
        await client.close()


@pytest.mark.parametrize(
    "patch",
    [
        # licences mints tenant/licence rows by SQL migration with gen_random_uuid(): v4.
        {"tenant_id": "10000000-0000-4000-8000-000000000001"},
        {"license_id": "10000000-0000-4000-8000-000000000001"},
        {"tenant_id": str(TENANT_B)},
    ],
)
async def test_licence_authority_identities_may_be_any_uuid_version(patch):
    client = client_for(lambda req: httpx.Response(200, json=info(**patch)))
    try:
        identity = await client.authenticate("synthetic-license")
        assert str(identity.tenant_id) == info(**patch)["tenant_id"]
        assert str(identity.license_id) == info(**patch)["license_id"]
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("kind", "tenant_level"), [("tenant", True), ("member", False), (None, False)]
)
async def test_identity_carries_subject_kind(kind, tenant_level):
    client = client_for(lambda req: httpx.Response(200, json=info(subject_kind=kind)))
    try:
        identity = await client.authenticate("synthetic-license")
        assert identity.subject_kind == kind and identity.tenant_level is tenant_level
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body", [b"null", b"[]", b"{", b"{}", b'{"active":true,"app":{"key":"mcp"}}']
)
async def test_malformed_response(body):
    client = client_for(lambda req: httpx.Response(200, content=body))
    with pytest.raises(AuthenticationError):
        await client.authenticate("synthetic-license")
    await client.close()


@pytest.mark.parametrize("token", ["", "x" * 4097, "one two", "line\nbreak", "nonascii-é"])
async def test_invalid_token_never_contacts_authority(token):
    def handler(req):
        pytest.fail("invalid token must not reach network")

    client = client_for(handler)
    with pytest.raises(AuthenticationError):
        await client.authenticate(token)
    await client.close()


async def test_positive_cache_bounds_ttl_app_expiries_and_outage():
    now = [1000.0]
    requests = []
    fail = [False]

    def handler(req):
        payload = json.loads(req.content)
        requests.append(payload)
        assert "authorization" not in req.headers
        if fail[0]:
            raise httpx.ConnectError("synthetic secret must not escape")
        return httpx.Response(
            200,
            json=info(
                app={"key": payload["app"]},
                expires_at=datetime.fromtimestamp(1015, UTC).isoformat(),
                token_expires_at=datetime.fromtimestamp(1010, UTC).isoformat(),
            ),
        )

    client = client_for(handler, license_cache_ttl_seconds=30, license_cache_max_entries=2)
    client.clock = lambda: now[0]
    first = await client.authenticate("token-one")
    assert first.tenant_id == TENANT_A and first.valid_until == 1008
    assert await client.authenticate("token-one") == first and len(requests) == 1
    await client.authenticate("token-one", "other_app")
    await client.authenticate("token-two")
    assert len(client.cache) == 2
    assert "token-one" not in repr(client.cache)
    fail[0] = True
    assert await client.authenticate("token-two") == first
    now[0] = 1008
    with pytest.raises(AuthorityUnavailableError, match="unavailable"):
        await client.authenticate("token-two")
    fail[0] = False
    with pytest.raises(AuthenticationError, match="expired"):
        await client.authenticate("token-two")
    await client.close()
    assert not client.cache


async def test_ttl_zero_no_cache_and_missing_configuration():
    client = client_for(lambda req: httpx.Response(200, json=info()), license_cache_ttl_seconds=0)
    await client.authenticate("one")
    assert not client.cache
    await client.close()
    client = LicenseClient(settings())
    with pytest.raises(AuthorityUnavailableError):
        await client.authenticate("one")
    await client.close()


@pytest.mark.parametrize("status", [302, 400, 401, 500])
async def test_no_redirects_or_authority_http_errors(status):
    client = client_for(
        lambda req: httpx.Response(status, headers={"Location": "https://evil.example"})
    )
    with pytest.raises(AuthorityUnavailableError):
        await client.authenticate("one")
    await client.close()


async def test_bounded_authority_body():
    client = client_for(
        lambda req: httpx.Response(200, content=b"x" * 65), license_max_response_bytes=64
    )
    with pytest.raises(AuthorityUnavailableError):
        await client.authenticate("one")
    await client.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"license_app_key": "mcp"},
        {"license_introspect_url": "https://example.test"},
        {"license_introspect_url": "http://example.test", "license_app_key": "mcp"},
        {
            "license_introspect_url": "https://user:pass@example.test",  # pragma: allowlist secret (synthetic rejected URL)
            "license_app_key": "mcp",
        },
        {"license_introspect_url": "https://example.test?token=bad", "license_app_key": "mcp"},
        {"admin_key_file": "/missing"},
        {"platform_admin_key_file": "/missing"},
    ],
)
def test_configuration_fails_closed(updates):
    with pytest.raises(ValueError):
        settings(**updates)


def test_internal_http_requires_explicit_operator_setting():
    assert settings(
        license_introspect_url="http://licenses/license/introspect",
        license_app_key="mcp",
        license_allow_http=True,
    )


def test_management_secrets_allowlist_and_platform_key(tmp_path):
    admin = tmp_path / "admin"
    platform = tmp_path / "platform"
    admin.write_text("a" * 32 + "\n")
    platform.write_text("p" * 32)
    auth = ManagementAuthorization(
        configuration(
            admin_key_file=admin, platform_admin_key_file=platform, admin_tenant_ids={TENANT_A}
        )
    )
    identity = LicenseIdentity(TENANT_A, LICENSE, str(SUBJECT), float("inf"))
    principal = auth.authorize(identity, "a" * 32, None)
    assert principal.management and not principal.platform
    assert principal.actor.subject != str(TENANT_A)
    assert auth.authorize(identity, "a" * 32, "p" * 32).platform
    for key, platform_key in [("bad", None), ("a" * 32, "bad")]:
        with pytest.raises(AuthenticationError):
            auth.authorize(identity, key, platform_key)
    with pytest.raises(AuthenticationError):
        auth.authorize(
            LicenseIdentity(TENANT_B, LICENSE, str(SUBJECT), float("inf")), "a" * 32, None
        )
    with pytest.raises(AuthenticationError):
        ManagementAuthorization(
            configuration(admin_key_file=admin, admin_tenant_ids={TENANT_A})
        ).authorize(identity, "a" * 32, "p" * 32)
    with pytest.raises(AuthorityUnavailableError):
        ManagementAuthorization(settings()).authorize(identity, "a" * 32, None)
    platform.write_text("a" * 32)
    with pytest.raises(ValueError, match="distinct"):
        ManagementAuthorization(
            configuration(
                admin_key_file=admin, platform_admin_key_file=platform, admin_tenant_ids={TENANT_A}
            )
        )
    for invalid in ["", "short", "x" * 4097, "x" * 32 + "\ninside"]:
        admin.write_text(invalid)
        with pytest.raises(ValueError):
            read_key(admin)
    assert read_key(None) is None
