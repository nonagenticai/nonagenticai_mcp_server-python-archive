"""Operator keys are mounted files, independent of license and binding credentials."""

import hmac
from pathlib import Path

from src.auth.licenses import AuthenticationError, AuthorityUnavailableError, LicenseIdentity
from src.auth.principal import Principal
from src.config import Settings


def read_key(path: Path | None) -> bytes | None:
    if path is None:
        return None
    # Read once at startup, bounded even for incorrectly mounted files.
    with path.open("rb") as stream:
        value = stream.read(4097).strip()
    if not 32 <= len(value) <= 4096 or any(c < 33 or c > 126 for c in value):
        raise ValueError("operator key file must contain 32 to 4096 printable bytes")
    return value


class ManagementAuthorization:
    def __init__(self, settings: Settings) -> None:
        self.tenants = settings.admin_tenant_ids
        self.admin = read_key(settings.admin_key_file)
        self.platform = read_key(settings.platform_admin_key_file)
        if self.admin and self.platform and hmac.compare_digest(self.admin, self.platform):
            raise ValueError("platform and tenant operator keys must be distinct")

    def authorize(self, identity: LicenseIdentity, key: str, platform_key: str | None) -> Principal:
        if self.admin is None:
            raise AuthorityUnavailableError("management is not configured")
        if identity.tenant_id not in self.tenants or not hmac.compare_digest(
            self.admin, key.encode("utf-8")
        ):
            raise AuthenticationError("management denied")
        platform = False
        if platform_key is not None:
            if self.platform is None or not hmac.compare_digest(
                self.platform, platform_key.encode("utf-8")
            ):
                raise AuthenticationError("platform management denied")
            platform = True
        return Principal(
            identity.tenant_id,
            identity.license_id,
            identity.subject,
            management=True,
            platform=platform,
        )
