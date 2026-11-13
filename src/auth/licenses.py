"""Shared bounded introspection client. Cache contains hashes and identity, never tokens."""

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import RFC_4122, UUID

import httpx

from src.config import Settings


class AuthenticationError(ValueError):
    """Invalid or expired authority (safe fixed messages only)."""


class AuthorityUnavailableError(RuntimeError):
    """Authority cannot currently be verified; fail closed."""


#: Licence `subject_kind` of a tenant-level holder (`svc:<tenant>:<app>`), never a person.
TENANT_SUBJECT = "tenant"


@dataclass(frozen=True, slots=True)
class LicenseIdentity:
    """Introspected licence authority.

    tenant_id/license_id come from the licence authority and may be ANY valid UUID version:
    licences inserts rows by SQL migration with gen_random_uuid() (v4). Only identities mcp
    itself mints (bindings, toolkits, profiles, events, runs) are required to be UUIDv7.
    """

    tenant_id: UUID
    license_id: UUID
    subject: str
    valid_until: float
    subject_kind: str | None = None

    @property
    def tenant_level(self) -> bool:
        return self.subject_kind == TENANT_SUBJECT


def authority_uuid(value: object) -> UUID:
    """A licence-authority identity: any RFC 4122/9562 UUID version (v4 from SQL, v7, ...)."""
    if not isinstance(value, str):
        raise ValueError("identity must be a string")
    parsed = UUID(value)
    if parsed.variant != RFC_4122 or parsed.version is None:
        raise ValueError("identity must be an RFC 9562 UUID")
    return parsed


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expiry(value: object) -> float:
    # Explicit null denotes a perpetual license/token in the upstream contract.
    if value is None:
        return float("inf")
    if not isinstance(value, str):
        raise ValueError("invalid expiry")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("expiry must be timezone aware")
    return parsed.timestamp()


class LicenseClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = lambda: datetime.now(UTC).timestamp(),
    ) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.license_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        )
        self.clock = clock
        self.cache: OrderedDict[tuple[str, str], tuple[float, LicenseIdentity]] = OrderedDict()
        self.raw_cache: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()

    async def close(self) -> None:
        self.cache.clear()
        self.raw_cache.clear()
        await self.client.aclose()

    async def authenticate(self, token: str, app_key: str | None = None) -> LicenseIdentity:
        app = app_key or self.settings.license_app_key
        if not self.settings.license_introspect_url or not app:
            raise AuthorityUnavailableError("authentication is not configured")
        if not 1 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise AuthenticationError("invalid license")
        key = (digest_token(token), app)
        now = self.clock()
        cached = self.cache.get(key)
        if cached and now < cached[0]:
            self.cache.move_to_end(key)
            return cached[1]
        self.cache.pop(key, None)
        try:
            async with (
                asyncio.timeout(self.settings.license_timeout_seconds),
                self.client.stream(
                    "POST",
                    self.settings.license_introspect_url,
                    json={"token": token, "app": app},
                ) as response,
            ):
                if response.status_code != 200:
                    raise AuthorityUnavailableError("license authority unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.license_max_response_bytes:
                        raise AuthorityUnavailableError("license authority response rejected")
            info = json.loads(body)
            if (
                not isinstance(info, dict)
                or info.get("active") is not True
                or not isinstance(info.get("app"), dict)
                or info["app"].get("key") != app
            ):
                raise AuthenticationError("invalid license")
            tenant = authority_uuid(info["tenant_id"])
            license_id = authority_uuid(info["license_id"])
            subject = info.get("subject_id")
            if not isinstance(subject, str) or not 1 <= len(subject) <= 256:
                raise AuthenticationError("invalid license")
            # Missing fields are malformed, not perpetual. Skew shortens authority.
            until = min(expiry(info["expires_at"]), expiry(info["token_expires_at"]))
            until -= self.settings.license_expiry_skew_seconds
            if self.clock() >= until:
                raise AuthenticationError("expired license")
            kind = info.get("subject_kind")
            if kind is not None and kind not in {"tenant", "member"}:
                raise AuthenticationError("invalid license")
            identity = LicenseIdentity(tenant, license_id, subject, until, kind)
        except httpx.HTTPError, TimeoutError:
            raise AuthorityUnavailableError("license authority unavailable") from None
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            if isinstance(error, AuthenticationError):
                raise
            raise AuthenticationError("invalid license response") from None
        cache_until = min(now + self.settings.license_cache_ttl_seconds, until)
        if self.clock() < cache_until:
            self.cache[key] = (cache_until, identity)
            while len(self.cache) > self.settings.license_cache_max_entries:
                self.cache.popitem(last=False)
        return identity

    async def introspect(self, token: str) -> dict:
        """Raw introspection exactly as main's care-bus path sent it: {"token": ...}, no app.

        Used only for non-binding licence callers (care-bus tenant licences). The caller checks
        `active`, `app.key` and `subject_kind`; nothing here grants access. Same bounds, cache
        (hash-keyed, TTL-capped, active payloads only) and fail-closed errors as authenticate().
        """
        if not self.settings.license_introspect_url:
            raise AuthorityUnavailableError("authentication is not configured")
        if not 1 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise AuthenticationError("invalid license")
        key = (digest_token(token), "")
        now = self.clock()
        cached = self.raw_cache.get(key)
        if cached and now < cached[0]:
            self.raw_cache.move_to_end(key)
            return cached[1]
        self.raw_cache.pop(key, None)
        try:
            async with (
                asyncio.timeout(self.settings.license_timeout_seconds),
                self.client.stream(
                    "POST", self.settings.license_introspect_url, json={"token": token}
                ) as response,
            ):
                if response.status_code != 200:
                    raise AuthorityUnavailableError("license authority unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.license_max_response_bytes:
                        raise AuthorityUnavailableError("license authority response rejected")
            info = json.loads(body)
        except httpx.HTTPError, TimeoutError:
            raise AuthorityUnavailableError("license authority unavailable") from None
        except (TypeError, ValueError, RecursionError) as error:
            if isinstance(error, AuthenticationError):
                raise
            raise AuthenticationError("invalid license response") from None
        if not isinstance(info, dict) or info.get("active") is not True:
            raise AuthenticationError("invalid license")
        if self.settings.license_cache_ttl_seconds:
            self.raw_cache[key] = (now + self.settings.license_cache_ttl_seconds, info)
            while len(self.raw_cache) > self.settings.license_cache_max_entries:
                self.raw_cache.popitem(last=False)
        return info
