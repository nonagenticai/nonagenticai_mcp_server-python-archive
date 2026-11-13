"""Broker-side source resolution: the registry is read at call time, never from a file.

A source is identified by ``(owner, key)``: the owner is the calling service's
``owner_tenant_id``, so a tenant source that reuses a platform key is never used for a platform
service and vice versa. A short TTL cache bounds database reads; a disabled or removed source is
denied within the TTL. Platform sources are enforced against the operator allow-list again here;
tenant sources are public https origins re-checked by the egress policy on every connection (a
tenant ``licence`` source additionally needs the operator allow-list). Stored secrets are decrypted
only in this process, per call.
"""

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr, TypeAdapter

from src.broker.egress import EgressDeniedError, check_hostname
from src.execution.models import ExecutionDeniedError
from src.sources.crypto import decrypt_token
from src.sources.guard import enforce, host_allowed
from src.sources.models import SourceAuth, checked_tenant_url, needs_secret
from src.sources.repository import resolve_row

AUTH = TypeAdapter(SourceAuth)


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    source_key: str
    base_url: str
    auth: SourceAuth
    idempotency_header: str | None
    ca_file: str | None
    max_request_bytes: int | None
    max_response_bytes: int | None
    token: SecretStr | None = field(default=None, repr=False)
    owner_tenant_id: UUID | None = None
    source_id: UUID | None = None
    revision: int = 1
    #: True for tenant-owned sources: always egressed through the pinned tenant transport.
    tenant_owned: bool = False


class SourceRegistry:
    def __init__(self, pool, settings) -> None:
        self.pool, self.settings = pool, settings
        self.cache: dict[tuple[UUID, str], tuple[float, dict | None]] = {}

    async def row(self, owner: UUID, key: str) -> dict | None:
        now = time.monotonic()
        hit = self.cache.get((owner, key))
        if hit and now < hit[0]:
            return hit[1]
        async with self.pool.connection() as conn:
            row = await resolve_row(conn, owner, key)
        if len(self.cache) > 1024:
            self.cache.clear()
        self.cache[owner, key] = (now + self.settings.source_cache_ttl_seconds, row)
        return row

    async def get(self, owner: UUID, key: str) -> ResolvedSource:
        """The active source ``(owner, key)`` or ExecutionDeniedError (missing, disabled, host not
        allowed, credential undecryptable). Never includes row content in the error."""
        row = await self.row(owner, key)
        if row is None or row["status"] != "active" or row["owner_tenant_id"] != owner:
            raise ExecutionDeniedError()
        tenant_owned = owner != self.settings.platform_tenant_id
        try:
            auth = AUTH.validate_python(row["auth"])
            if tenant_owned:
                if row["allow_internal_http"] or row["ca_file"]:
                    raise ValueError("tenant source is public https only")
                checked_tenant_url(row["base_url"], allow_path=True)
                url = urlsplit(row["base_url"])
                check_hostname(url.hostname or "")
                if (url.port or 443) not in self.settings.builder_allowed_ports:
                    raise ValueError("port not allowed")
                if auth.kind == "licence" and not host_allowed(
                    row["base_url"], self.settings.source_allowed_hosts
                ):
                    raise ValueError("licence sources need an operator-approved host")
            else:
                enforce(
                    row["base_url"], row["allow_internal_http"], self.settings.source_allowed_hosts
                )
        except ValueError, EgressDeniedError:
            raise ExecutionDeniedError() from None
        token = None
        if needs_secret(auth):
            if row["token_ciphertext"] is None or self.settings.source_secret_key is None:
                raise ExecutionDeniedError()
            try:
                token = decrypt_token(
                    self.settings.source_secret_key, row["source_id"], row["token_ciphertext"]
                )
            except Exception:
                raise ExecutionDeniedError() from None
        return ResolvedSource(
            key,
            row["base_url"],
            auth,
            row["idempotency_header"],
            row["ca_file"],
            row["max_request_bytes"],
            row["max_response_bytes"],
            token,
            owner,
            row["source_id"],
            row["revision"],
            tenant_owned,
        )
