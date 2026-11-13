"""Immutable identity only; incoming bearer credentials never enter request state."""

from dataclasses import dataclass
from uuid import UUID

from src.catalog.models import Attribution


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: UUID
    license_id: UUID
    subject: str
    binding_id: UUID | None = None
    binding_revision: int | None = None
    management: bool = False
    platform: bool = False

    @property
    def actor(self) -> Attribution:
        return Attribution(subject=self.subject, license_id=self.license_id)


#: Caller kinds that never carry a binding; they reach only the builtin provider.
CARE_BUS_LICENCE = "carebus_licence"
KEYCLOAK_SERVICE = "keycloak_service"
#: A tenant authoring its own tools through the builder_* MCP toolkit (tenant-level mcp licence,
#: no binding). Audited as principal_kind ``tenant_author`` (migration 0010).
TENANT_AUTHOR = "tenant_author"


@dataclass(frozen=True, slots=True)
class CallerPrincipal:
    """A non-binding caller of the builtin provider (docs/AUTHENTICATION_BINDINGS.md).

    `kind` is CARE_BUS_LICENCE (app care-bus licence, X-Tenant-Id) or KEYCLOAK_SERVICE (Keycloak
    JWT, scoped by azp). `scope` is the key into src.builtin.scope.SCOPES; "" grants nothing.
    `subject` is the licence subject_id or the JWT azp. No credential is kept here.
    """

    kind: str
    subject: str
    scope: str
    tenant_id: str | None = None
    #: The authenticating licence's id (tenant authors only: attribution and draft-run gateway).
    license_id: str | None = None

    @property
    def auth_method(self) -> str:
        return "license" if self.kind in {CARE_BUS_LICENCE, TENANT_AUTHOR} else "keycloak"


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    """A Keycloak service account on /mcp/internal/* (azp in MCP_BINDING_ENSURE_CLIENTS).

    It carries no licence, binding or tenant: the route's path names the tenant, and the
    route itself decides what the service may do there. No credential is kept here.
    """

    azp: str


@dataclass(frozen=True, slots=True)
class AuthorPrincipal:
    """A tenant author of the tool builder (docs/TOOL_BUILDER.md): the tenant's tenant-level mcp
    licence, directly (``/mcp/builder/*``) or presented by a product platform acting for the
    tenant (``/mcp/internal/tenants/{t}/builder/*``, ``via`` = the service JWT's azp).

    It is NOT a ``Principal``: ``get_operator`` can never accept it, so an author can never reach
    the operator (management) routes, and it carries no binding. No credential is kept here.
    """

    tenant_id: UUID
    license_id: UUID
    subject: str
    via: str | None = None

    @property
    def actor(self) -> Attribution:
        return Attribution(subject=self.subject[:256], license_id=self.license_id)
