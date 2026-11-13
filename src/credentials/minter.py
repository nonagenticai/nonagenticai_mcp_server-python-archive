"""Per-tenant backend credentials minted by licences, beside the mounted-file resolver.

As the Keycloak client ``licence_minter_client_id`` (``mcp-bundle-minter``, realm role
``bundle-token-minter``) this resolver calls licences
``POST /internal/tenants/{tenant}/bundle-token?app=<backend>&ttl_minutes=<n>[&bundle=<key>]`` (201
``{token, expires_at, tenant_id, bundle, app, license_id, token_id}``). licences scopes the
call to the tenant's ``app_requires`` dependency grants of owner ``mcp``; any other app is 403
``dependency_grant_scope``. Every refusal, outage or malformed answer fails closed
(ExecutionDeniedError); only successful mints are cached, until the token's expiry minus skew.

The broker awaits ``aresolve`` during preflight (which also verifies each credential's licence
identity); its later synchronous ``resolve`` calls are served from that cache only. Tokens are
never logged and never leave this object except as SecretStr.

The bundle is named explicitly whenever the credential reference carries one (bindings/ensure
sets it from the binding's product), so a tenant holding several bundles that contain the same
app never gets 409 ``ambiguous_bundle``. The mcp application's own licence is never minted: the
gateway licence is the one the caller presented (``gateway_credentials = False``).
"""

import asyncio
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr

from src.auth.licenses import authority_uuid, expiry
from src.config import Settings
from src.execution.models import ExecutionDeniedError

MAX_RESPONSE_BYTES = 65536
CACHE_ENTRIES = 1024


class MinterUnavailableError(ExecutionDeniedError):
    """licences or Keycloak could not be reached or answered unexpectedly (retryable)."""


class DependencyGrantScopeError(ExecutionDeniedError):
    """licences 403 ``dependency_grant_scope``: the app is outside mcp's grants for the tenant."""


@dataclass(frozen=True, slots=True)
class MintedCredential:
    license_id: UUID
    valid_until: float
    token: SecretStr = field(repr=False)


def printable(value: object, maximum: int = 8192) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError("invalid credential material")
    return value


def read_secret(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            value = stream.read(4097).strip()
        return printable(value.decode("ascii"), 4096)
    except OSError, UnicodeDecodeError, ValueError:
        raise ExecutionDeniedError("minter client secret unavailable") from None


class LicenceMinterCredentialResolver:
    #: The gateway (mcp) licence is the caller's, authenticated on its request; never minted.
    gateway_credentials = False

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = lambda: datetime.now(UTC).timestamp(),
    ) -> None:
        if (
            not settings.licence_minter_enabled
            or not settings.licence_minter_url
            or not settings.licence_minter_token_url
            or not settings.licence_minter_secret_file
        ):
            raise ValueError("licence minting is not configured")
        self.settings = settings
        self.base_url = settings.licence_minter_url.rstrip("/")
        self.token_url = settings.licence_minter_token_url
        self.secret_file = settings.licence_minter_secret_file
        self.secret = read_secret(self.secret_file)
        self.client = client or httpx.AsyncClient(
            timeout=settings.license_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=4),
        )
        self.clock = clock
        self.cache: OrderedDict[tuple[UUID, str, str | None], MintedCredential] = OrderedDict()
        self.service_token: tuple[float, str] | None = None
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        self.cache.clear()
        self.service_token = None
        await self.client.aclose()

    def read(self) -> None:
        """Readiness: the mounted client secret is still present and well formed."""
        self.secret = read_secret(self.secret_file)

    async def bounded(self, response: httpx.Response) -> Any:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise MinterUnavailableError("licence authority response rejected")
        try:
            return json.loads(body)
        except ValueError, RecursionError:
            return None

    async def access_token(self) -> str:
        """Client-credentials token for licence_minter_client_id, cached until exp minus skew."""
        now = self.clock()
        if self.service_token and now < self.service_token[0]:
            return self.service_token[1]
        self.service_token = None
        try:
            async with (
                asyncio.timeout(self.settings.license_timeout_seconds),
                self.client.stream(
                    "POST",
                    self.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.licence_minter_client_id,
                        "client_secret": self.secret,
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise MinterUnavailableError("minter client authentication failed")
                payload = await self.bounded(response)
        except httpx.HTTPError, TimeoutError:
            raise MinterUnavailableError("minter client authentication unavailable") from None
        try:
            if not isinstance(payload, dict):
                raise ValueError("invalid token response")
            token = printable(payload["access_token"])
            lifetime = payload["expires_in"]
            if not isinstance(lifetime, int) or isinstance(lifetime, bool) or lifetime <= 0:
                raise ValueError("invalid lifetime")
        except KeyError, ValueError:
            raise MinterUnavailableError("minter client token rejected") from None
        until = now + lifetime - self.settings.licence_minter_skew_seconds
        if until > self.clock():
            self.service_token = (until, token)
        return token

    async def mint(self, tenant: UUID, app: str, bundle: str | None = None) -> MintedCredential:
        """A short-lived token on the tenant's licence for ``app`` (in ``bundle``). Fails closed."""
        if app == self.settings.license_app_key:
            raise ExecutionDeniedError("mcp never mints its own application licence")
        key = (tenant, app, bundle)
        cached = self.cache.get(key)
        if cached and self.clock() < cached.valid_until:
            self.cache.move_to_end(key)
            return cached
        self.cache.pop(key, None)
        async with self.lock:
            bearer = await self.access_token()
        url = f"{self.base_url}/internal/tenants/{tenant}/bundle-token"
        params = {"app": app, "ttl_minutes": str(self.settings.licence_minter_ttl_minutes)}
        if bundle is not None:
            params["bundle"] = bundle
        try:
            async with (
                asyncio.timeout(self.settings.license_timeout_seconds),
                self.client.stream(
                    "POST", url, params=params, headers={"Authorization": f"Bearer {bearer}"}
                ) as response,
            ):
                status = response.status_code
                payload = await self.bounded(response)
        except httpx.HTTPError, TimeoutError:
            raise MinterUnavailableError("licence authority unavailable") from None
        if status == 401:
            # The cached client token was rejected (rotated realm key, revoked client).
            self.service_token = None
            raise MinterUnavailableError("minter client token rejected")
        if status == 403:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict) and detail.get("error") == "dependency_grant_scope":
                raise DependencyGrantScopeError("app outside the tenant's dependency grants")
            raise ExecutionDeniedError("licence minting refused")
        if 400 <= status < 500:
            raise ExecutionDeniedError("licence minting refused")
        if status != 201:
            raise MinterUnavailableError("licence authority unavailable")
        try:
            if not isinstance(payload, dict):
                raise ValueError("invalid mint response")
            if authority_uuid(payload["tenant_id"]) != tenant or payload["app"] != app:
                raise ValueError("minted for another tenant or app")
            if bundle is not None and payload.get("bundle") != bundle:
                raise ValueError("minted in another bundle")
            license_id = authority_uuid(payload["license_id"])
            token = printable(payload["token"], 4096)
            until = expiry(payload["expires_at"]) - self.settings.licence_minter_skew_seconds
        except KeyError, TypeError, ValueError:
            raise ExecutionDeniedError("licence authority response rejected") from None
        minted = MintedCredential(license_id, until, SecretStr(token))
        if self.clock() >= until:
            raise ExecutionDeniedError("minted credential already expired")
        self.cache[key] = minted
        while len(self.cache) > CACHE_ENTRIES:
            self.cache.popitem(last=False)
        return minted

    async def licence_id(self, tenant: UUID, app: str, bundle: str | None = None) -> UUID:
        return (await self.mint(tenant, app, bundle)).license_id

    async def aresolve(self, tenant, profile, reference) -> SecretStr:
        minted = await self.mint(tenant, reference.app_key, reference.bundle)
        if minted.license_id != reference.license_id:
            raise ExecutionDeniedError()
        return minted.token

    def resolve(self, tenant, profile, reference) -> SecretStr:
        """Synchronous broker path: only credentials minted by a preceding aresolve."""
        cached = self.cache.get((tenant, reference.app_key, reference.bundle))
        if (
            cached is None
            or self.clock() >= cached.valid_until
            or cached.license_id != reference.license_id
        ):
            raise ExecutionDeniedError("credential reference unavailable")
        return cached.token
