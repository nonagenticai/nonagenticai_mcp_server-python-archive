"""Deployment-owned upstream limits; counters and permits live in PostgreSQL."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import UUID7, Field

from src.catalog.models import AuthorityId, Contract


class ServiceLimit(Contract):
    service_id: UUID7
    tenant_id: AuthorityId | None = None
    calls_per_minute: Annotated[int, Field(gt=0, le=10000)]
    concurrency: Annotated[int, Field(gt=0, le=100)]


class RateLimits(Contract):
    schema_version: Literal[1] = 1
    limits: Annotated[tuple[ServiceLimit, ...], Field(max_length=1024)] = ()

    @classmethod
    def read(cls, path: Path | None):
        if path is None:
            return cls()
        with path.open("rb") as stream:
            payload = stream.read(262145)
        if len(payload) > 262144:
            raise ValueError("upstream limit file exceeds bounds")
        result = cls.model_validate_json(payload)
        identities = [(item.service_id, item.tenant_id) for item in result.limits]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate upstream limit")
        return result

    def ceiling(self, tenant, service, calls, concurrency):
        for item in self.limits:
            if item.service_id == service and item.tenant_id in {None, tenant}:
                calls = min(calls, item.calls_per_minute)
                concurrency = min(concurrency, item.concurrency)
        return calls, concurrency
