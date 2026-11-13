"""Read-only atomic bundle snapshots. Database aliases are never filesystem paths."""

import os
import stat
from pathlib import Path
from typing import Annotated, Protocol

from pydantic import UUID7, Field, SecretStr, model_validator

from src.catalog.models import AppKey, AuthorityId, Contract, Name
from src.execution.models import ExecutionDeniedError


class BundleEntry(Contract):
    tenant_id: AuthorityId
    profile_id: UUID7
    service: Name
    alias: Name
    license_id: AuthorityId
    app_key: AppKey
    token: SecretStr
    oauth_bearer: SecretStr | None = None


class CredentialBundle(Contract):
    schema_version: int = Field(ge=1, le=1)
    entries: Annotated[tuple[BundleEntry, ...], Field(max_length=1024)]

    @model_validator(mode="after")
    def unique_entries(self):
        keys = [(e.tenant_id, e.profile_id, e.service, e.alias) for e in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate credential identity")
        if any(
            not 1 <= len(e.token.get_secret_value()) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in e.token.get_secret_value())
            for e in self.entries
        ):
            raise ValueError("invalid credential material")
        if any(
            e.oauth_bearer is not None
            and (
                not 1 <= len(e.oauth_bearer.get_secret_value()) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in e.oauth_bearer.get_secret_value())
            )
            for e in self.entries
        ):
            raise ValueError("invalid upstream credential material")
        return self


class CredentialResolver(Protocol):
    def resolve(self, tenant, profile, reference) -> SecretStr: ...


class FileCredentialResolver:
    def __init__(self, path: Path):
        self.path = path

    def read(self):
        # Follow projected-secret symlinks deliberately. Validate the opened inode,
        # so atomic deployment rotation yields one complete snapshot per read.
        try:
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid not in {0, os.geteuid()}
                    or info.st_mode & 0o022
                    or info.st_size > 1048576
                ):
                    raise ExecutionDeniedError()
                payload = stream.read(1048577)
            return CredentialBundle.model_validate_json(payload)
        except OSError, ValueError:
            raise ExecutionDeniedError("credential bundle unavailable") from None

    def resolve(self, tenant, profile, reference):
        return self.entry(tenant, profile, reference).token

    def entry(self, tenant, profile, reference):
        for entry in self.read().entries:
            if (entry.tenant_id, entry.profile_id, entry.service, entry.alias) == (
                tenant,
                profile,
                reference.service,
                reference.alias,
            ):
                if entry.license_id != reference.license_id or entry.app_key != reference.app_key:
                    raise ExecutionDeniedError()
                return entry
        raise ExecutionDeniedError("credential reference unavailable")
