"""Request-scoped authority bridge; never contains incoming bearer credentials.

Exception: CallerAuthority.strata_token is the care-bus tenant's strata-rag licence
(X-Strata-Token), forwarded only to strata_rag by faq_ask (src/builtin/carebus/client.py).
"""

from contextvars import ContextVar
from dataclasses import dataclass, field

from src.auth.principal import CallerPrincipal, Principal


@dataclass(frozen=True, slots=True)
class RequestAuthority:
    principal: Principal
    idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class CallerAuthority:
    principal: CallerPrincipal
    strata_token: str | None = field(default=None, repr=False)


authority: ContextVar[RequestAuthority | None] = ContextVar("mcp_request_authority", default=None)
# Set instead of `authority` for builtin-provider callers, so catalog code that calls
# current_authority() fails closed for them.
caller_authority: ContextVar[CallerAuthority | None] = ContextVar(
    "mcp_caller_authority", default=None
)


# The person or agent a tools/call says it acts for (_meta "ai.nonagentic/actor", validated), so
# an external run (fr) can act as that channel address. Never authority: fr resolves the address.
call_actor: ContextVar[dict | None] = ContextVar("mcp_call_actor", default=None)


def current_authority() -> RequestAuthority:
    value = authority.get()
    if value is None:
        raise PermissionError("explicit request authority required")
    return value


def current_caller() -> CallerAuthority | None:
    return caller_authority.get()
