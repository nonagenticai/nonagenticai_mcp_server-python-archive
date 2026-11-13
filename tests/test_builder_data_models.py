"""Builder contracts that need no database: auth variants, tenant URL policy, manifests, settings."""

import base64
import json
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError

from src.catalog.models import (
    ArgumentMapping,
    AuthAdapter,
    OperationManifest,
    ServiceManifest,
)
from src.platform.seed import same
from src.sources.crypto import decrypt_token, encrypt_token
from src.sources.models import (
    SourceAuth,
    SourceView,
    TenantSourceCreate,
    TenantSourceUpdate,
    TokenUpdate,
    checked_tenant_url,
)
from tests.fixtures import operation, service, settings

AUTH = TypeAdapter(SourceAuth)
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()


@pytest.mark.parametrize(
    "auth",
    [
        {"kind": "none"},
        {"kind": "static_bearer"},
        {"kind": "static_header", "header": "X-Api-Key"},
        {"kind": "query_param", "param": "api_key"},
        {"kind": "basic", "username": "svc user"},
        {
            "kind": "oauth2_client_credentials",
            "token_url": "https://auth.example.com/oauth/token",
            "client_id": "abc",
            "scope": "read write",
            "audience": "https://api.example.com",
            "client_auth": "post",
        },
        {"kind": "licence", "app_key": "strata-rag", "scheme": "bearer", "bundle": "agent"},
    ],
)
def test_every_auth_kind_round_trips(auth):
    parsed = AUTH.validate_python(auth)
    assert parsed.model_dump(mode="json", exclude_none=True)["kind"] == auth["kind"]


@pytest.mark.parametrize(
    "auth",
    [
        {"kind": "static_header", "header": "Authorization"},
        {"kind": "static_header", "header": "Host"},
        {"kind": "query_param", "param": "a b"},
        {"kind": "basic", "username": "a:b"},
        {"kind": "basic"},
        {
            "kind": "oauth2_client_credentials",
            "token_url": "http://x.example.com/t",
            "client_id": "a",
        },
        {"kind": "oauth2_client_credentials", "token_url": "https://10.0.0.1/t", "client_id": "a"},
        {
            "kind": "oauth2_client_credentials",
            "token_url": "https://x.example.com/t?x=1",
            "client_id": "a",
        },
        {"kind": "licence", "app_key": "x", "bundle": "Bad Bundle"},
        {"kind": "unknown"},
        {"kind": "none", "extra": 1},
    ],
)
def test_auth_variants_reject_unsafe_shapes(auth):
    with pytest.raises(ValidationError):
        AUTH.validate_python(auth)


def tenant_body(**overrides):
    values = {
        "source_key": "crm",
        "base_url": "https://api.example.com/v1",
        "auth": {"kind": "static_bearer"},
        "token": SecretStr("tok-123"),
    }
    values.update(overrides)
    return values


def test_tenant_source_accepts_a_public_https_origin_and_hides_the_token():
    body = TenantSourceCreate.model_validate(tenant_body(spec_path="/openapi.json"))
    assert "tok-123" not in repr(body)
    assert "tok-123" not in str(body.model_dump())
    update = {k: v for k, v in tenant_body().items() if k != "source_key"}
    TenantSourceUpdate.model_validate(update | {"expected_revision": 2})


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com",
        "https://127.0.0.1",
        "https://[::1]/x",
        "https://localhost",
        "https://intranet",
        "https://svc.cluster.local",
        "https://x.internal",
        "https://x.svc",
        "https://printer.local",
        "https://user@api.example.com",
        "https://api.example.com?x=1",
        "https://api.example.com#f",
        "https://api.example.com/a/../b",
        "https://api.example.com:99999",
        "https://2130706433",
        "https://169.254.169.254/latest",
        "ftp://api.example.com",
    ],
)
def test_tenant_urls_must_be_public_https(url):
    with pytest.raises(ValueError):
        checked_tenant_url(url, allow_path=True)
    with pytest.raises(ValidationError):
        TenantSourceCreate.model_validate(tenant_body(base_url=url))


def test_tenant_source_cannot_carry_operator_only_fields():
    for field, value in (("allow_internal_http", True), ("ca_file", "/etc/ca.pem")):
        with pytest.raises(ValidationError):
            TenantSourceCreate.model_validate(tenant_body(**{field: value}))


def test_secret_rules_follow_the_kind():
    with pytest.raises(ValidationError, match="needs its secret"):
        TenantSourceCreate.model_validate(tenant_body(token=None))
    # none/licence store nothing, so a token is refused.
    with pytest.raises(ValidationError, match="no stored secret"):
        TenantSourceCreate.model_validate(tenant_body(auth={"kind": "none"}))
    TenantSourceCreate.model_validate(tenant_body(auth={"kind": "none"}, token=None))
    # A basic-auth password may contain spaces; a bearer token may not.
    TenantSourceCreate.model_validate(
        tenant_body(auth={"kind": "basic", "username": "u"}, token=SecretStr("pass word"))
    )
    with pytest.raises(ValidationError):
        TenantSourceCreate.model_validate(tenant_body(token=SecretStr("pass word")))
    with pytest.raises(ValidationError):
        TokenUpdate.model_validate({"token": SecretStr("a\nb"), "expected_revision": 1})


def test_basic_passwords_with_spaces_survive_encryption():
    source = uuid4()
    blob = encrypt_token(SecretStr(KEY), source, SecretStr("pass word"))
    assert decrypt_token(SecretStr(KEY), source, blob).get_secret_value() == "pass word"


def test_source_view_exposes_owner_and_no_secret():
    view = SourceView.model_validate_json(
        json.dumps(
            {
                "source_id": "01900000-0000-7000-8000-000000000001",
                "owner_tenant_id": str(uuid4()),
                "source_key": "crm",
                "revision": 1,
                "status": "active",
                "base_url": "https://api.example.com",
                "allow_internal_http": False,
                "auth": {"kind": "basic", "username": "u"},
                "has_token": True,
                "token_set_at": None,
                "spec_path": None,
                "idempotency_header": None,
                "ca_file": None,
                "max_request_bytes": None,
                "max_response_bytes": None,
                "created_by": {},
                "updated_by": {},
                "created_at": "x",
                "updated_at": "x",
            }
        )
    )
    assert "token_ciphertext" not in view.model_dump()
    assert isinstance(view.owner_tenant_id, UUID)


# --- catalog contracts ------------------------------------------------------------------------


def mapping(**values):
    base = {
        "location": "query",
        "target": "q",
        "value": {"source": "input", "pointer": "/q"},
    }
    return ArgumentMapping.model_validate(base | values)


def test_optional_mapping_rules():
    assert mapping().optional is False
    assert mapping(optional=True).optional is True
    with pytest.raises(ValidationError, match="optional"):
        mapping(location="path", target="id", optional=True)
    with pytest.raises(ValidationError, match="optional"):
        mapping(value={"source": "context", "pointer": "/t"}, optional=True)
    with pytest.raises(ValidationError, match="optional"):
        mapping(value={"source": "literal", "value": 1}, optional=True)


def test_service_app_key_is_optional_only_for_the_source_adapter():
    with pytest.raises(ValidationError, match="app_key"):
        service(license_app_key=None)
    tenant = service(license_app_key=None, auth_adapter=AuthAdapter.SOURCE)
    assert tenant.license_app_key is None
    assert tenant.auth_adapter.value == "source"
    assert service(auth_adapter=AuthAdapter.SOURCE).license_app_key == "app_1"


def test_operation_empty_response_defaults_false():
    assert operation().empty_response is False
    assert operation(empty_response=True).empty_response is True


def test_legacy_manifests_without_the_new_fields_compare_equal():
    """The seed treats stored (pre-0010) manifests as unchanged: defaults reproduce them."""
    current = operation(mappings=(mapping(),), query_fields=("q",))
    stored = json.loads(current.model_dump_json())
    assert stored.pop("empty_response") is False
    for m in stored["mappings"]:
        assert m.pop("optional") is False
    parsed = OperationManifest.model_validate_json(json.dumps(stored))
    assert same(parsed, current)
    # A changed default is a real change: the seed would append a version.
    assert not same(parsed, operation(mappings=(mapping(optional=True),), query_fields=("q",)))
    assert not same(
        parsed, operation(mappings=(mapping(),), query_fields=("q",), empty_response=True)
    )
    legacy_service = ServiceManifest.model_validate_json(service().model_dump_json())
    assert same(legacy_service, service())


# --- settings ---------------------------------------------------------------------------------


def test_builder_settings_defaults_and_validation():
    s = settings()
    assert s.builder_enabled is False
    assert (s.builder_max_sources, s.builder_max_operations) == (20, 300)
    assert (s.builder_max_tools, s.builder_max_toolkits) == (50, 10)
    assert s.builder_spec_max_bytes == 5 * 1024 * 1024
    assert (s.builder_spec_fetches_per_hour, s.builder_test_runs_per_hour) == (30, 60)
    assert (s.builder_source_calls_per_minute, s.builder_source_concurrency) == (60, 2)
    assert s.builder_allowed_ports == (443,)
    assert s.builder_deny_cidrs == ()
    assert s.builder_oauth_token_max_ttl == 3600


def test_builder_settings_parse_environment_lists(monkeypatch):
    monkeypatch.setenv("MCP_BUILDER_ALLOWED_PORTS", "443, 8443")
    monkeypatch.setenv("MCP_BUILDER_DENY_CIDRS", "10.0.0.0/8,fd00::/8")
    monkeypatch.setenv("MCP_BUILDER_MAX_SOURCES", "5")
    s = settings()
    assert s.builder_allowed_ports == (443, 8443)
    assert s.builder_deny_cidrs == ("10.0.0.0/8", "fd00::/8")
    assert s.builder_max_sources == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"builder_allowed_ports": "0"},
        {"builder_allowed_ports": "70000"},
        {"builder_deny_cidrs": "not-a-cidr"},
        {"builder_max_sources": 0},
        {"builder_enabled": True},
    ],
)
def test_builder_settings_reject_invalid_values(overrides):
    with pytest.raises(ValueError):
        settings(**overrides)
