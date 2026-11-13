"""Keycloak access-token validation (PyJWT + JWKS) for the ``Authorization: Bearer`` path.

Ported from enterprise_mcp_server ``src/keycloak_auth/{config,validator}.py`` (post-#172, PyJWT),
with three deliberate differences:

* The audience is REQUIRED, not opt-in. Keycloak puts a custom ``aud`` only through an audience
  mapper on the calling client, and that mapper exists for ``carebus-tool-executor`` (gitops
  realm-patch ``ensure_audience_mapper``), so there is no reason to accept a token without it.
* Keys come from the realm JWKS (``.../protocol/openid-connect/certs``) through
  ``jwt.PyJWKClient`` -- selected by ``kid``, cached -- instead of python-keycloak's single realm
  public key, so a Keycloak key rotation does not need a restart.
* No hardcoded defaults (``http://localhost:8080``, realm ``master``). The path is enabled only when
  ``KEYCLOAK_URL``, ``KEYCLOAK_REALM``, ``KEYCLOAK_AUDIENCE`` and ``KEYCLOAK_ALLOWED_CLIENTS`` are
  ALL set. Otherwise it is off and only licence tokens authenticate; a partial configuration is
  logged as an error and is also off (fail closed, never "accept any realm token").

A token passes only if: RS256 signature verifies against the realm JWKS; ``exp``, ``iss``, ``azp``
and ``aud`` are present; ``exp`` is in the future; ``iss`` equals ``{KEYCLOAK_URL}/realms/{REALM}``;
``aud`` contains one of ``KEYCLOAK_AUDIENCE``; and ``azp`` (the client the token was issued TO) is
in ``KEYCLOAK_ALLOWED_CLIENTS``. Without the azp gate every valid token of the realm -- any portal
user's browser token -- would be accepted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient
from jwt.types import Options

logger = logging.getLogger(__name__)

REQUIRED_ENV: tuple[str, ...] = (
    "KEYCLOAK_URL",
    "KEYCLOAK_REALM",
    "KEYCLOAK_AUDIENCE",
    "KEYCLOAK_ALLOWED_CLIENTS",
)

# PyJWKClient caches the JWK set for this long; an unknown kid forces one refetch.
JWKS_CACHE_LIFESPAN_S = 300
JWKS_FETCH_TIMEOUT_S = 5
# Sent on the JWKS fetch. Cloudflare in front of auth.nonagentic.ai answers urllib's default
# "Python-urllib/x.y" User-Agent with 403 (measured live 2026-10-22: every care-bus token was
# rejected as "Fail to fetch data from the url ... 403"), while any other agent gets 200.
JWKS_USER_AGENT = "nonagenticai-mcp-server (jwks)"


def _split(raw: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in raw.split(",") if v.strip())


@dataclass(frozen=True)
class KeycloakConfig:
    url: str
    realm: str
    audiences: tuple[str, ...]
    allowed_clients: frozenset[str]

    @property
    def issuer(self) -> str:
        return f"{self.url.rstrip('/')}/realms/{self.realm}"

    @property
    def certs_url(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/certs"


@lru_cache(maxsize=1)
def get_keycloak_config() -> KeycloakConfig | None:
    """The Keycloak config, or None when the JWT path is disabled. Cached; tests cache_clear()."""
    values = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV}
    missing = [name for name, value in values.items() if not value]
    if len(missing) == len(REQUIRED_ENV):
        logger.info("Keycloak JWT auth disabled (no KEYCLOAK_* set): licence tokens only")
        return None
    audiences = _split(values["KEYCLOAK_AUDIENCE"])
    clients = frozenset(_split(values["KEYCLOAK_ALLOWED_CLIENTS"]))
    if not audiences:
        missing.append("KEYCLOAK_AUDIENCE (empty list)")
    if not clients:
        missing.append("KEYCLOAK_ALLOWED_CLIENTS (empty list)")
    if missing:
        logger.error(
            "Keycloak JWT auth DISABLED: partially configured, missing %s; "
            "every Authorization: Bearer request will 401",
            ", ".join(sorted(set(missing))),
        )
        return None
    return KeycloakConfig(
        url=values["KEYCLOAK_URL"],
        realm=values["KEYCLOAK_REALM"],
        audiences=audiences,
        allowed_clients=clients,
    )


def internal_config(settings: Any) -> KeycloakConfig | None:
    """The verifier config for /mcp/internal/*, from MCP_INTERNAL_KEYCLOAK_* settings, or None."""
    if not settings or not settings.internal_keycloak_url or not settings.internal_keycloak_realm:
        return None
    if not settings.internal_audiences or not settings.internal_clients:
        return None
    return KeycloakConfig(
        url=settings.internal_keycloak_url,
        realm=settings.internal_keycloak_realm,
        audiences=settings.internal_audiences,
        allowed_clients=settings.internal_clients,
    )


@lru_cache(maxsize=4)
def _jwks_client(certs_url: str) -> PyJWKClient:
    return PyJWKClient(
        certs_url,
        cache_keys=True,
        lifespan=JWKS_CACHE_LIFESPAN_S,
        timeout=JWKS_FETCH_TIMEOUT_S,
        headers={"User-Agent": JWKS_USER_AGENT},
    )


async def _signing_key(token: str, config: KeycloakConfig) -> Any:
    """The JWKS key matching the token's kid. PyJWKClient fetches with urllib (blocking): thread."""
    client = _jwks_client(config.certs_url)
    signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
    return signing_key.key


async def validate_keycloak_token(token: str, config: KeycloakConfig) -> dict[str, Any]:
    """Validated claims of a Keycloak access token. Raises jwt.PyJWTError on any failure."""
    key = await _signing_key(token, config)
    options: Options = {
        "verify_signature": True,
        "verify_aud": True,
        "verify_exp": True,
        "verify_iss": True,
        "require": ["exp", "iss", "azp", "aud"],
        # NOT cosmetic (carried over from enterprise): PyJWT verifies `iat` by default and rejects
        # a token whose iat is in the future, so one second of clock skew between Keycloak and
        # this pod would 401 every caller. exp still bounds the token's life.
        "verify_iat": False,
    }
    payload: dict[str, Any] = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        options=options,
        audience=list(config.audiences),
        issuer=config.issuer,
    )
    azp = payload.get("azp")
    if azp not in config.allowed_clients:
        raise InvalidTokenError(
            f"token was issued to client {azp!r}, which may not call this service"
        )
    return payload
