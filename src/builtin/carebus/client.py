"""Shared HTTP client for the care-bus MCP tools.

Every error leaves as a ToolError whose message starts with "[retryable] " or "[permanent] ".
The care-bus tool-executor reads that prefix to decide whether a worker
may retry, so the prefix is a cross-repo contract, not cosmetics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from urllib.parse import quote

import httpx
from fastmcp.exceptions import ToolError

from src.auth.context import current_caller

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 2.5  # wall-clock per call; must stay below the worker's 3 s tool timeout
# strata_rag /api/ask includes vLLM generation; FAQ_TIMEOUT_S (default 18) stays below the executor's 20 s faq budget.

_ENV = {
    "demo_backends": ("DEMO_BACKENDS_URL", "DEMO_BACKENDS_TOKEN"),
    "strata_rag": ("STRATA_RAG_URL", "STRATA_RAG_TOKEN"),
    "carebus_api": ("CAREBUS_API_URL", "CAREBUS_API_TOKEN"),
}
_clients: dict[str, ServiceClient | None] = {name: None for name in _ENV}

# Path ids come from the LLM / the customer. httpx normalises dot segments, so an unchecked
# "../admin/reset?" would reach another route with the same bearer token; percent-encoding alone
# leaves ".." intact. Allow-list first, then encode.
_PATH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def path_id(field: str, value: object) -> str:
    """Validate one caller-supplied id and return it as a single encoded URL path segment."""
    if not isinstance(value, str) or value in (".", "..") or not _PATH_ID.fullmatch(value):
        shown = repr(value)
        if len(shown) > 40:
            shown = shown[:40] + "..."
        raise ToolError(
            f"[permanent] invalid {field}: {shown} "
            "(expected 1-128 chars of letters, digits, '_', '.', ':', '-')"
        )
    return quote(value, safe="")


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    except ValueError:
        pass
    return resp.text[:200] or resp.reason_phrase


class ServiceClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        tenant_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport = transport
        self.timeout = timeout
        self.tenant_id = tenant_id

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if self.tenant_id:
            headers["X-Tenant-Id"] = self.tenant_id
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                headers=headers,
            ) as client:
                # httpx's scalar timeout applies per phase (connect/read/write/pool); the
                # executor's budget is wall-clock, so bound the whole call as well.
                async with asyncio.timeout(self.timeout):
                    resp = await client.request(method, path, **kwargs)
        except (httpx.TimeoutException, TimeoutError) as e:
            ms = int((time.monotonic() - started) * 1000)
            raise ToolError(f"[retryable] timeout after {ms} ms calling {method} {path}") from e
        except httpx.TransportError as e:
            # The internal base URL is logged, never returned to the caller.
            logger.warning(
                "care-bus backend %s unreachable for %s %s: %r", self.base_url, method, path, e
            )
            raise ToolError(
                f"[retryable] cannot reach backend for {method} {path}: {e.__class__.__name__}"
            ) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ToolError(
                f"[retryable] {resp.status_code} from {method} {path}: {_message(resp)}"
            )
        if resp.status_code >= 400:
            raise ToolError(
                f"[permanent] {resp.status_code} from {method} {path}: {_message(resp)}"
            )
        return resp.json()

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict) -> dict:
        return await self._request("POST", path, json=body)


def _get(service: str) -> ServiceClient:
    client = _clients[service]
    if client is None:
        url_var, token_var = _ENV[service]
        url = os.environ.get(url_var)
        if not url:
            raise ToolError(f"[permanent] {url_var} is not set on the MCP server")
        timeout = (
            float(os.environ.get("FAQ_TIMEOUT_S", "18"))
            if service == "strata_rag"
            else DEFAULT_TIMEOUT_S
        )
        client = ServiceClient(url, os.environ.get(token_var), timeout=timeout)
        _clients[service] = client
    return client


def demo_backends() -> ServiceClient:
    return _get("demo_backends")


def strata_rag() -> ServiceClient:
    """strata_rag for THIS caller's tenant.

    A licence caller (a tenant's care-bus licence) searches its own tenant: the request's
    X-Strata-Token (that tenant's strata-rag licence token, kept in the CallerAuthority) replaces the
    pinned STRATA_RAG_TOKEN, and X-Tenant-Id names the licence's tenant so strata_rag answers 403
    tenant_mismatch if the two disagree. No forwarded token -> refused, never the pinned tenant.
    A Keycloak caller (the transition path) still uses STRATA_RAG_TOKEN unchanged.
    """
    base = _get("strata_rag")
    caller = current_caller()
    if caller is None or caller.principal.auth_method != "license":
        return base
    token = caller.strata_token
    if not token:
        raise ToolError(
            "[permanent] X-Strata-Token (the tenant's strata-rag licence token) is required "
            "for a licence caller"
        )
    return ServiceClient(
        base.base_url,
        token,
        transport=base.transport,
        timeout=base.timeout,
        tenant_id=caller.principal.tenant_id,
    )


def carebus_api() -> ServiceClient:
    return _get("carebus_api")


def set_client(service: str, client: ServiceClient | None) -> None:
    """Test hook: inject a client (or None to fall back to env on next use)."""
    if service not in _clients:
        raise KeyError(service)
    _clients[service] = client
