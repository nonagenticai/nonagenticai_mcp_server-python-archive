"""Tool-source contracts. A token is accepted write-only and is never part of any view."""

import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    UUID7,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from src.catalog.models import AppKey, AuthAdapter, AuthorityId, Contract, Name
from src.sources.crypto import valid_secret, valid_token
from src.sources.guard import checked_base_url, is_ip_literal

#: Headers a static source may not set: framing and routing belong to the HTTP client.
FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "te",
        "upgrade",
        "cookie",
        "proxy-authorization",
        "idempotency-key",
        "x-idempotency-key",
    }
)


class NoAuth(Contract):
    """A public API: nothing is sent and nothing is stored."""

    kind: Literal["none"]


class LicenceAuth(Contract):
    """The tenant's licence for ``app_key``, minted per call (conveyor-style)."""

    kind: Literal["licence"]
    app_key: AppKey
    scheme: Literal["bearer", "x_tenant_id_bearer"] = "bearer"
    #: The licences bundle the minter names explicitly; none lets licences pick the app's bundle.
    bundle: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")] = None

    @property
    def adapter(self) -> AuthAdapter:
        return AuthAdapter.LICENSE_BEARER if self.scheme == "bearer" else AuthAdapter.LICENSE_HEADER


class StaticBearerAuth(Contract):
    kind: Literal["static_bearer"]


class StaticHeaderAuth(Contract):
    kind: Literal["static_header"]
    header: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")]

    @field_validator("header")
    @classmethod
    def allowed_header(cls, value: str) -> str:
        if value.lower() in FORBIDDEN_HEADERS or value.lower() == "authorization":
            raise ValueError("header is reserved")
        return value


class QueryParamAuth(Contract):
    """The stored secret is sent as ``?param=<secret>``."""

    kind: Literal["query_param"]
    param: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")]


class BasicAuth(Contract):
    """HTTP Basic: ``username`` is stored in the clear, the password is the stored secret."""

    kind: Literal["basic"]
    username: Annotated[str, Field(pattern=r"^[\x20-\x39\x3b-\x7e]{1,128}$")]


class OAuth2ClientCredentialsAuth(Contract):
    """RFC 6749 client-credentials grant; the stored secret is the client secret."""

    kind: Literal["oauth2_client_credentials"]
    token_url: Annotated[str, Field(min_length=1, max_length=512)]
    client_id: Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,256}$")]
    scope: Annotated[
        str | None, Field(pattern=r"^[\x21-\x7e]+( [\x21-\x7e]+)*$", max_length=256)
    ] = None
    audience: Annotated[str | None, Field(pattern=r"^[\x21-\x7e]{1,256}$")] = None
    client_auth: Literal["basic", "post"] = "basic"

    @field_validator("token_url")
    @classmethod
    def https_token_url(cls, value: str) -> str:
        return checked_tenant_url(value, allow_path=True)


SourceAuth = Annotated[
    NoAuth
    | LicenceAuth
    | StaticBearerAuth
    | StaticHeaderAuth
    | QueryParamAuth
    | BasicAuth
    | OAuth2ClientCredentialsAuth,
    Field(discriminator="kind"),
]
#: Kinds that need no stored secret; every other kind has exactly one.
SECRETLESS_KINDS = frozenset({"licence", "none"})
IdempotencyHeader = Literal["Idempotency-Key", "X-Idempotency-Key"]
Limit = Annotated[int, Field(gt=0, le=10485760)]
SpecPath = Annotated[str, Field(pattern=r"^/[A-Za-z0-9._~/-]{0,255}$")]
#: Hostnames a tenant may never register (cluster-internal names); egress re-checks at call time.
INTERNAL_SUFFIXES = (".local", ".internal", ".svc", ".cluster.local", ".localhost", ".lan")


def needs_secret(auth: BaseModel) -> bool:
    return getattr(auth, "kind", None) not in SECRETLESS_KINDS


def checked_tenant_url(value: str, *, allow_path: bool) -> str:
    """Syntax of a tenant-registered HTTPS URL; raises ValueError.

    Public DNS names only: no IP literal, userinfo, query, fragment or single-label/internal
    host. This is the registration-time half; the egress layer re-checks (and pins the
    resolved address) on every call.
    """
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("invalid URL") from None
    host = url.hostname or ""
    if (
        url.scheme != "https"
        or url.username is not None
        or url.password is not None
        or "@" in url.netloc
        or url.query
        or url.fragment
        or (port is not None and not 1 <= port <= 65535)
        or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", host)
        or is_ip_literal(host)
        or "." not in host
        or ".." in host
        or host == "localhost"
        or host.endswith(INTERNAL_SUFFIXES)
        or not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*/?" if allow_path else r"/?", url.path)
        or any(p in {".", ".."} for p in url.path.split("/"))
        or len(value) > 512
    ):
        raise ValueError("a public https URL is required")
    return value


def checked_secret(kind: str, value: str) -> str:
    """The stored secret's charset for this kind (a basic password may contain spaces)."""
    return valid_secret(value) if kind == "basic" else valid_token(value)


class SourceCore(Contract):
    """Fields every source writer shares; the token is the one write-only secret."""

    model_config = ConfigDict(hide_input_in_errors=True)

    auth: SourceAuth
    idempotency_header: IdempotencyHeader | None = None
    max_request_bytes: Limit | None = None
    max_response_bytes: Limit | None = None
    #: Where the OpenAPI document lives, relative to ``base_url`` (same origin only).
    spec_path: SpecPath | None = None
    token: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def token_matches_kind(self) -> Self:
        if self.token is not None:
            checked_secret(self.auth.kind, self.token.get_secret_value())
            if not needs_secret(self.auth):
                raise ValueError("this source kind carries no stored secret")
        return self


class SourceBody(SourceCore):
    """Operator (platform) source: any approved origin, optional private CA."""

    base_url: Annotated[str, Field(min_length=1, max_length=512)]
    allow_internal_http: bool = False
    #: PEM CA bundle path on the broker host (self-signed internal CA); never disables TLS checks.
    ca_file: Annotated[str | None, Field(max_length=512)] = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        checked_base_url(self.base_url, self.allow_internal_http)
        if self.ca_file is not None and not re.fullmatch(r"/[A-Za-z0-9_./-]{1,511}", self.ca_file):
            raise ValueError("ca_file must be an absolute path")
        return self


class SourceCreate(SourceBody):
    source_key: Name

    @model_validator(mode="after")
    def static_needs_token(self) -> Self:
        if needs_secret(self.auth) and self.token is None:
            raise ValueError("a source of this kind needs its secret")
        return self


class SourceUpdate(SourceBody):
    expected_revision: Annotated[int, Field(gt=0)]


class TenantSourceBody(SourceCore):
    """A tenant's own source: public https only, no private CA, no plain http."""

    base_url: Annotated[str, Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def public_https(self) -> Self:
        checked_tenant_url(self.base_url, allow_path=True)
        return self


class TenantSourceCreate(TenantSourceBody):
    source_key: Name

    @model_validator(mode="after")
    def needs_its_secret(self) -> Self:
        if needs_secret(self.auth) and self.token is None:
            raise ValueError("a source of this kind needs its secret")
        return self


class TenantSourceUpdate(TenantSourceBody):
    expected_revision: Annotated[int, Field(gt=0)]


class TokenUpdate(Contract):
    model_config = ConfigDict(hide_input_in_errors=True)

    token: SecretStr = Field(repr=False)
    expected_revision: Annotated[int, Field(gt=0)]

    @field_validator("token")
    @classmethod
    def printable(cls, value: SecretStr) -> SecretStr:
        valid_secret(value.get_secret_value())
        return value


class RevisionOnly(Contract):
    expected_revision: Annotated[int, Field(gt=0)]


class SourceView(Contract):
    """Everything an operator may read back: no token, no ciphertext."""

    source_id: UUID7
    owner_tenant_id: AuthorityId
    source_key: Name
    revision: int
    status: Literal["active", "disabled"]
    base_url: str
    allow_internal_http: bool
    auth: SourceAuth
    has_token: bool
    token_set_at: str | None
    spec_path: str | None
    idempotency_header: IdempotencyHeader | None
    ca_file: str | None
    max_request_bytes: int | None
    max_response_bytes: int | None
    created_by: dict
    updated_by: dict
    created_at: str
    updated_at: str
