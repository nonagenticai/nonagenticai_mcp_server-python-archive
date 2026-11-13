"""Credential injection for registered sources (everything except the per-call ``licence`` mint,
which the broker performs with the tenant's profile).

One encrypted secret per source. Every secret and every encoded form of it that could reach an
upstream request is returned to the caller so it can be added to the broker's ``leaks()`` check.
The OAuth2 client-credentials token is cached in process, per ``(source_id, revision)``, with a
single-flight refresh, a bounded lifetime and eviction when an upstream answers 401. The token
endpoint is called through the same pinned egress client as the source itself.
"""

import asyncio
import base64
import json
import time
from collections.abc import Callable
from urllib.parse import quote, urlencode

import httpx

from src.execution.models import ExecutionDeniedError
from src.sources.models import (
    BasicAuth,
    OAuth2ClientCredentialsAuth,
    QueryParamAuth,
    StaticHeaderAuth,
)
from src.sources.registry import ResolvedSource

TOKEN_RESPONSE_LIMIT = 64 * 1024
TOKEN_TIMEOUT_SECONDS = 10.0
SKEW_SECONDS = 30
DEFAULT_LIFETIME_SECONDS = 300
CACHE_LIMIT = 256


class AuthFailedError(ExecutionDeniedError):
    """The credential could not be applied or obtained (trace reason ``auth_failed``)."""

    reason = "auth_failed"


def _form(value: str) -> str:
    return quote(value, safe="")


class AuthAdapters:
    def __init__(
        self,
        max_ttl: int,
        client: Callable[[], httpx.AsyncClient],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_ttl, self._client, self._clock = max_ttl, client, clock
        self._tokens: dict[tuple, tuple[str, float]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    async def apply(self, source: ResolvedSource, headers: dict, query: dict) -> list[str]:
        """Add the source credential to ``headers``/``query`` and return every secret string.

        Runs after the operation mapping, so a mapping that targets the credential's header or
        query parameter is rejected rather than overwritten. The query secret goes in last.
        """
        auth, token = source.auth, source.token
        kind = auth.kind
        if kind in {"none", "licence"}:
            return []
        if token is None:
            raise AuthFailedError
        secret = token.get_secret_value()
        secrets = [secret]
        if kind == "static_bearer":
            self._set(headers, "Authorization", "Bearer " + secret)
        elif isinstance(auth, StaticHeaderAuth):
            self._set(headers, auth.header, secret)
        elif isinstance(auth, BasicAuth):
            blob = base64.b64encode(f"{auth.username}:{secret}".encode()).decode()
            self._set(headers, "Authorization", "Basic " + blob)
            secrets.append(blob)
        elif isinstance(auth, OAuth2ClientCredentialsAuth):
            access = await self._access_token(source, secret)
            self._set(headers, "Authorization", "Bearer " + access)
            secrets.append(access)
        elif isinstance(auth, QueryParamAuth):
            if auth.param in query:
                raise AuthFailedError
            query[auth.param] = secret
            secrets.append(quote(secret, safe=""))
        else:
            raise AuthFailedError
        return secrets

    @staticmethod
    def _set(headers: dict, name: str, value: str) -> None:
        if any(existing.lower() == name.lower() for existing in headers):
            raise AuthFailedError
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise AuthFailedError
        headers[name] = value

    def evict(self, source: ResolvedSource) -> None:
        """An upstream rejected the token: drop it so the next call fetches a fresh one."""
        self._tokens.pop((source.source_id, source.revision), None)

    async def _access_token(self, source: ResolvedSource, client_secret: str) -> str:
        key = (source.source_id, source.revision)
        hit = self._tokens.get(key)
        if hit and self._clock() < hit[1]:
            return hit[0]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._tokens.get(key)  # single flight: a concurrent caller may have refreshed
            if hit and self._clock() < hit[1]:
                return hit[0]
            access, ttl = await self._fetch(source, client_secret)
            if len(self._tokens) >= CACHE_LIMIT:
                now = self._clock()
                self._tokens = {k: v for k, v in self._tokens.items() if v[1] > now}
                if len(self._tokens) >= CACHE_LIMIT:
                    self._tokens.clear()
            if ttl > 0:
                self._tokens[key] = (access, self._clock() + ttl)
            if len(self._locks) > 4 * CACHE_LIMIT:
                self._locks = {k: v for k, v in self._locks.items() if v.locked()}
            return access

    async def _fetch(self, source: ResolvedSource, client_secret: str) -> tuple[str, float]:
        auth = source.auth
        if not isinstance(auth, OAuth2ClientCredentialsAuth):
            raise AuthFailedError
        form = {"grant_type": "client_credentials"}
        headers = {"Accept": "application/json"}
        if auth.scope:
            form["scope"] = auth.scope
        if auth.audience:
            form["audience"] = auth.audience
        if auth.client_auth == "basic":
            blob = base64.b64encode(
                f"{_form(auth.client_id)}:{_form(client_secret)}".encode()
            ).decode()
            headers["Authorization"] = "Basic " + blob
        else:
            form["client_id"], form["client_secret"] = auth.client_id, client_secret
        try:
            async with asyncio.timeout(TOKEN_TIMEOUT_SECONDS):
                async with self._client().stream(
                    "POST",
                    auth.token_url,
                    content=urlencode(form).encode(),
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=TOKEN_TIMEOUT_SECONDS,
                ) as response:
                    if response.status_code != 200:
                        raise AuthFailedError
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > TOKEN_RESPONSE_LIMIT:
                            raise AuthFailedError
            payload = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            access, kind = payload["access_token"], payload["token_type"]
            lifetime = payload.get("expires_in", DEFAULT_LIFETIME_SECONDS)
            if (
                not isinstance(access, str)
                or not access.isascii()
                or not access.isprintable()
                or " " in access
                or not access
                or not isinstance(kind, str)
                or kind.lower() != "bearer"
                or isinstance(lifetime, bool)
                or not isinstance(lifetime, int | float)
            ):
                raise AuthFailedError
        except AuthFailedError:
            raise
        except ValueError, KeyError, TypeError, httpx.HTTPError, TimeoutError:
            raise AuthFailedError from None
        # EgressDeniedError (token endpoint on a refused address) is deliberately not caught
        # here: it propagates and is traced as ssrf_blocked.
        return access, min(lifetime, self.max_ttl) - SKEW_SECONDS
