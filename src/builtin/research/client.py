"""HTTP clients for the in-cluster research backends: discovery-engine and scientific-processor.

Same error contract as the legal client (it is reused): every failure leaves as a ToolError whose
message starts with "[retryable] " (timeout, unreachable, 429, 5xx) or "[permanent] " (other 4xx,
invalid argument).

Auth: both services accept a licence token in `X-Tenant-Id: Bearer <token>`, or a trusted-network
(pod CIDR) peer with no X-Tenant-Id, which they serve as their DEFAULT_TENANT_ID
(ALLOW_TRUSTED_NETWORK_DEFAULT_TENANT, default on). DISCOVERY_ENGINE_TOKEN /
SCIENTIFIC_PROCESSOR_TOKEN are sent when set; otherwise no identity header is sent. Identities
are never forwarded from the MCP caller.

Test hook: set_client().
"""

from __future__ import annotations

import os

from ..legal.client import PublicClient

# service -> (URL env var, default in-cluster URL, token env var, timeout env var, default timeout)
# discovery-engine fans a search out to several public sources within its own ~75 s budget.
_ENV: dict[str, tuple[str, str, str, str, float]] = {
    "discovery_engine": (
        "DISCOVERY_ENGINE_URL",
        "http://discovery-engine.discovery-engine:8807",
        "DISCOVERY_ENGINE_TOKEN",
        "DISCOVERY_ENGINE_TIMEOUT_S",
        90.0,
    ),
    "scientific_processor": (
        "SCIENTIFIC_PROCESSOR_URL",
        "http://scientific-processor.scientific-processor:8808",
        "SCIENTIFIC_PROCESSOR_TOKEN",
        "SCIENTIFIC_PROCESSOR_TIMEOUT_S",
        30.0,
    ),
}
_clients: dict[str, PublicClient | None] = {name: None for name in _ENV}


def _timeout(env_var: str, default: float) -> float:
    try:
        return float(os.environ.get(env_var, default))
    except ValueError:
        return default


def get_client(service: str) -> PublicClient:
    client = _clients[service]
    if client is None:
        url_env, default_url, _token_env, timeout_env, default_timeout = _ENV[service]
        client = PublicClient(
            os.environ.get(url_env) or default_url,
            timeout=_timeout(timeout_env, default_timeout),
        )
        _clients[service] = client
    return client


def auth_headers(service: str) -> dict[str, str] | None:
    token = os.environ.get(_ENV[service][2])
    return {"X-Tenant-Id": f"Bearer {token}"} if token else None


def set_client(service: str, client: PublicClient | None) -> None:
    """Test hook: inject a client (or None to rebuild from env on next use)."""
    if service not in _clients:
        raise KeyError(service)
    _clients[service] = client
