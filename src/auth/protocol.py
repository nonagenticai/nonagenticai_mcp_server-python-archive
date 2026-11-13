"""Pure ASGI protection for mounted protocol and REST paths, including reconnects."""

import json
import logging
import re
import uuid

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.auth import keycloak
from src.auth.context import CallerAuthority, RequestAuthority, authority, caller_authority
from src.auth.licenses import AuthenticationError, AuthorityUnavailableError
from src.auth.principal import (
    CARE_BUS_LICENCE,
    KEYCLOAK_SERVICE,
    TENANT_AUTHOR,
    AuthorPrincipal,
    CallerPrincipal,
    Principal,
    ServicePrincipal,
)
from src.builtin.scope import (
    CAREBUS_LICENCE_APP,
    MCP_LICENCE_SCOPE,
    licence_app_key,
    licence_scope,
)
from src.storage.catalog import CatalogConflictError, CatalogNotFoundError


def header(scope: Scope, name: bytes, maximum: int = 4096) -> str | None:
    values = [v for k, v in scope.get("headers", ()) if k.lower() == name]
    if len(values) > 1:
        raise AuthenticationError("duplicate authentication header")
    if not values:
        return None
    value = values[0]
    if not 1 <= len(value) <= maximum or any(c < 33 or c > 126 for c in value):
        # The licence headers (X-Tenant-Id, or Authorization through the ingress) alone have a
        # single required space.
        if (
            name not in (b"x-tenant-id", b"authorization")
            or not value.startswith(b"Bearer ")
            or any(c < 33 or c > 126 for c in value[7:])
            or len(value) > maximum
        ):
            raise AuthenticationError("invalid authentication header")
    return value.decode("ascii")


logger = logging.getLogger(__name__)

#: Paths a non-binding (builtin) caller may use: the protocol endpoint only.
PROTOCOL_PATHS = frozenset({"/mcp", "/mcp/"})


class ForbiddenError(PermissionError):
    """Authenticated, but this credential kind may not use this path (403)."""


def bearer(scope: Scope, name: bytes, maximum: int = 4103) -> str | None:
    """Single printable-ASCII header value; a leading case-insensitive "Bearer " is removed."""
    values = [v for k, v in scope.get("headers", ()) if k.lower() == name]
    if len(values) > 1:
        raise AuthenticationError("duplicate authentication header")
    if not values:
        return None
    value = values[0]
    if not 1 <= len(value) <= maximum or any(c < 32 or c > 126 for c in value):
        raise AuthenticationError("invalid authentication header")
    text = value.decode("ascii").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text or None


def has_bearer(scope: Scope, name: bytes) -> bool:
    return any(
        k.lower() == name and v[:7].lower() == b"bearer " for k, v in scope.get("headers", ())
    )


def jwt_tenant(payload: dict) -> str | None:
    """The token's `tenant_id` claim when it is a uuid, else None (main #41). Never `sub`."""
    value = payload.get("tenant_id")
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


async def authenticate_caller(state, scope: Scope, path: str) -> CallerAuthority:
    """Non-binding callers of the builtin provider (main #41/#43/#46 semantics, fail closed).

    (a) `X-Tenant-Id: Bearer <licence>` without X-MCP-Binding: introspected; only app
        `care-bus` authenticates (scope per src.builtin.scope.licence_scope). Checked first; an
        invalid licence is a 401 and does NOT fall through to (b).
    (b) `Authorization: Bearer <jwt>`: a Keycloak access token (src.auth.keycloak), only when
        all four KEYCLOAK_* are set; scoped by azp. `Authorization` is never a licence candidate.
    Both are valid only on /mcp itself (403 elsewhere).
    """
    if has_bearer(scope, b"x-tenant-id"):
        token = bearer(scope, b"x-tenant-id")
        if not token:
            raise AuthenticationError("license required")
        if not hasattr(state, "licenses"):
            raise AuthorityUnavailableError("authentication unavailable")
        info = await state.licenses.introspect(token)
        if licence_app_key(info) != CAREBUS_LICENCE_APP:
            settings = getattr(state, "settings", None)
            app = getattr(settings, "license_app_key", None)
            if (
                settings is not None
                and settings.builder_enabled
                and app
                and (licence_app_key(info) == app)
            ):
                # Tool builder: the tenant's own tenant-level mcp licence, no binding, scoped to
                # the builder_* toolkit only (docs/TOOL_BUILDER.md).
                return await builder_caller(state, token, path)
            # An mcp licence needs X-MCP-Binding; other apps have no builtin scope.
            raise AuthenticationError("binding required")
        tenant = info.get("tenant_id") or info.get("subject_id")
        subject = info.get("subject_id") or tenant
        if not isinstance(tenant, str) or not tenant or len(tenant) > 256:
            raise AuthenticationError("license missing tenant")
        if path not in PROTOCOL_PATHS:
            raise ForbiddenError("licence callers without a binding may only call /mcp")
        strata = None
        try:
            # The caller's strata-rag licence for the SAME tenant (faq_ask, main #46).
            strata = bearer(scope, b"x-strata-token")
        except AuthenticationError:
            strata = None
        return CallerAuthority(
            CallerPrincipal(CARE_BUS_LICENCE, str(subject)[:256], licence_scope(info), tenant),
            strata,
        )
    if has_bearer(scope, b"authorization"):
        config = keycloak.get_keycloak_config()
        if config is None:
            raise AuthenticationError("keycloak disabled")
        token = bearer(scope, b"authorization", 8192)
        if not token:
            raise AuthenticationError("token required")
        try:
            payload = await keycloak.validate_keycloak_token(token, config)
        except Exception as error:
            logger.warning("Keycloak token rejected: %s", type(error).__name__)
            raise AuthenticationError("invalid keycloak token") from None
        azp = str(payload["azp"])
        if path not in PROTOCOL_PATHS:
            raise ForbiddenError("Keycloak service tokens may only call /mcp")
        return CallerAuthority(
            CallerPrincipal(KEYCLOAK_SERVICE, azp[:256], azp, jwt_tenant(payload))
        )
    raise AuthenticationError("credential required")


async def builder_caller(state, token: str, path: str) -> CallerAuthority:
    """A tenant author on /mcp: strict licence authentication, tenant-level only."""
    identity = await state.licenses.authenticate(token)
    if not identity.tenant_level:
        # A member's per-login licence never authors (it is not the tenant's `svc:` licence).
        raise AuthenticationError("tenant licence required")
    if identity.tenant_id == state.settings.platform_tenant_id:
        raise ForbiddenError("the platform tenant does not author through the builder")
    if path not in PROTOCOL_PATHS:
        raise ForbiddenError("licence callers without a binding may only call /mcp")
    return CallerAuthority(
        CallerPrincipal(
            TENANT_AUTHOR,
            identity.subject[:256],
            MCP_LICENCE_SCOPE,
            str(identity.tenant_id),
            str(identity.license_id),
        )
    )


#: Service-to-service routes. Only a Keycloak service JWT authenticates here.
INTERNAL_PREFIX = "/mcp/internal/"
#: Tenant tool builder REST (tenant-level mcp licence) and its product-platform twin.
BUILDER_PREFIX = "/mcp/builder"
PRODUCT_BUILDER = re.compile(r"^/mcp/internal/tenants/([^/]+)/builder(?:/|$)")
AUTHOR_FORBIDDEN_HEADERS = frozenset(
    {b"x-mcp-admin-key", b"x-mcp-platform-admin-key", b"x-mcp-binding"}
)


def is_builder_path(path: str) -> bool:
    return path == BUILDER_PREFIX or path.startswith(BUILDER_PREFIX + "/")


async def authenticate_author(state, scope: Scope) -> AuthorPrincipal:
    """`/mcp/builder/*`: `X-Tenant-Id: Bearer <tenant-level mcp licence>` and nothing else.

    Any operator or binding header makes the request ambiguous and is refused (401). A member
    licence is refused (401); the platform tenant is refused (403).
    """
    names = {k.lower() for k, _ in scope.get("headers", ())}
    if names & AUTHOR_FORBIDDEN_HEADERS:
        raise AuthenticationError("builder routes accept a tenant licence only")
    gateway = header(scope, b"x-tenant-id", 4103)
    if gateway is None or not gateway.startswith("Bearer ") or not gateway[7:]:
        raise AuthenticationError("license required")
    if not hasattr(state, "licenses"):
        raise AuthorityUnavailableError("authentication unavailable")
    identity = await state.licenses.authenticate(gateway[7:])
    if not identity.tenant_level:
        raise AuthenticationError("tenant licence required")
    if identity.tenant_id == state.settings.platform_tenant_id:
        raise ForbiddenError("the platform tenant does not author through the builder")
    return AuthorPrincipal(identity.tenant_id, identity.license_id, identity.subject[:256])


def licence_from_body(body: bytes) -> tuple[bytes, str | None, str | None]:
    """Split `licence_id` / `licence_token` out of a JSON object body (product callers pass the
    tenant's licence in the body like bindings/ensure). Returns the body without them."""
    if not body.strip():
        return body, None, None
    try:
        document = json.loads(body)
    except ValueError, RecursionError:
        return body, None, None
    if not isinstance(document, dict):
        return body, None, None
    licence_id = document.pop("licence_id", None)
    licence_token = document.pop("licence_token", None)
    if licence_id is None and licence_token is None:
        return body, None, None
    return (
        json.dumps(document).encode(),
        licence_id if isinstance(licence_id, str) else "",
        licence_token if isinstance(licence_token, str) else "",
    )


async def authenticate_product_service(state, scope: Scope) -> ServicePrincipal:
    """Step 1 of the product builder routes, run BEFORE the request body is read: the headers
    carry no operator/binding credentials and the service JWT (azp on the allow-list) is valid."""
    names = {k.lower() for k, _ in scope.get("headers", ())}
    if names & AUTHOR_FORBIDDEN_HEADERS:
        raise AuthenticationError("internal routes accept a service token only")
    return await authenticate_service(state, scope, licence_headers=True)


async def authenticate_product_author(
    state, scope: Scope, tenant: uuid.UUID, body: bytes, service: ServicePrincipal
) -> tuple[AuthorPrincipal, bytes]:
    """The tenant licence half of the product routes, after ``authenticate_product_service``
    and the (bounded) body read: THAT tenant's tenant-level mcp licence.

    The licence comes as `X-Tenant-Id: Bearer <token>` with `X-MCP-Licence-Id: <uuid>`, or as
    `licence_id` / `licence_token` in the JSON body. It is introspected and must be the path
    tenant's tenant-level licence with exactly that id (403 otherwise).
    """
    gateway = header(scope, b"x-tenant-id", 4103)
    licence_id = header(scope, b"x-mcp-licence-id", 64)
    token = gateway[7:] if gateway and gateway.startswith("Bearer ") else None
    if gateway is not None and not token:
        raise AuthenticationError("license required")
    if token is None:
        body, licence_id, token = licence_from_body(body)
    if not token or not licence_id:
        raise AuthenticationError("the tenant's mcp licence is required")
    app = state.settings.license_app_key
    if not hasattr(state, "licenses") or not app:
        raise AuthorityUnavailableError("authentication unavailable")
    identity = await state.licenses.authenticate(token, app)
    try:
        matches = uuid.UUID(licence_id) == identity.license_id
    except ValueError:
        matches = False
    if identity.tenant_id != tenant or not identity.tenant_level or not matches:
        raise ForbiddenError("the licence is not this tenant's tenant-level mcp licence")
    if tenant == state.settings.platform_tenant_id:
        raise ForbiddenError("the platform tenant does not author through the builder")
    return AuthorPrincipal(
        tenant, identity.license_id, f"service:{service.azp}"[:256], service.azp
    ), body


async def authenticate_service(
    state, scope: Scope, *, licence_headers: bool = False
) -> ServicePrincipal:
    """`Authorization: Bearer <Keycloak service JWT>` whose azp is in binding_ensure_clients,
    validated ONLY against the MCP_INTERNAL_KEYCLOAK_* config (never KEYCLOAK_*).

    No licence, binding or operator key is interpreted on these paths: presenting one is
    ambiguous and refused (401). Keycloak or the allow-list unconfigured is 503.
    """
    names = {k.lower() for k, _ in scope.get("headers", ())}
    refused = {b"x-tenant-id", b"x-mcp-binding", b"x-mcp-admin-key"}
    if licence_headers:
        # Product builder routes carry the tenant's licence beside the service token.
        refused -= {b"x-tenant-id"}
    if names & refused:
        raise AuthenticationError("internal routes accept a service token only")
    settings = getattr(state, "settings", None)
    allowed = getattr(settings, "binding_ensure_clients", frozenset())
    config = keycloak.internal_config(settings)
    if config is None or not allowed:
        raise AuthorityUnavailableError("internal routes are not configured")
    token = bearer(scope, b"authorization", 8192)
    if not token:
        raise AuthenticationError("token required")
    try:
        payload = await keycloak.validate_keycloak_token(token, config)
    except Exception as error:
        logger.warning("Keycloak token rejected: %s", type(error).__name__)
        raise AuthenticationError("invalid keycloak token") from None
    azp = str(payload["azp"])
    if azp not in allowed:
        raise ForbiddenError("client may not call internal routes")
    return ServicePrincipal(azp[:256])


def replay(body: bytes) -> Receive:
    """A receive callable that yields `body` once, then the real disconnect."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


class _BuiltinCaller(Exception):  # noqa: N818 -- control flow out of the auth try block
    def __init__(self, caller: CallerAuthority) -> None:
        self.caller = caller


def is_management(path: str) -> bool:
    # Explicit protocol/run aliases use binding auth. Unknown /mcp REST routes
    # require management; future routers cannot accidentally be anonymous.
    return not (
        path in {"/mcp", "/mcp/", "/mcp/sse", "/mcp/messages", "/mcp/available-tools"}
        or path.startswith("/mcp/runs")
        or path.startswith("/mcp/sse/")
        or path.startswith("/mcp/messages/")
    )


class AuthenticationMiddleware:
    def __init__(self, app: ASGIApp, state) -> None:
        self.app, self.state = app, state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root) :]
        protected = path == "/mcp" or path.startswith("/mcp/")
        if scope["type"] not in {"http", "websocket"} or not protected:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        product = PRODUCT_BUILDER.match(path)
        if product or is_builder_path(path):
            if not getattr(getattr(self.state, "settings", None), "builder_enabled", False):
                # Disabled builder: one consistent answer, before any credential is read.
                await JSONResponse(
                    {"error": "not_found"}, status_code=404, headers={"Cache-Control": "no-store"}
                )(scope, receive, send)
                return
            await self.call_author(scope, receive, send, product.group(1) if product else None)
            return
        if path.startswith(INTERNAL_PREFIX):
            await self.call_internal(scope, receive, send)
            return
        try:
            management = is_management(path) or (
                path.startswith("/mcp/runs/")
                and any(k.lower() == b"x-mcp-admin-key" for k, _ in scope.get("headers", ()))
            )
            headers = {k.lower() for k, _ in scope.get("headers", ())}
            if management and not has_bearer(scope, b"x-tenant-id"):
                if has_bearer(scope, b"authorization"):
                    # Validate first (an invalid JWT is 401), then refuse the path (main #41).
                    await authenticate_caller(self.state, scope, path)
            elif not management and b"x-mcp-binding" not in headers:
                caller = await authenticate_caller(self.state, scope, path)
                raise _BuiltinCaller(caller)
            gateway = header(scope, b"x-tenant-id", 4103)
            if not management and (gateway is None or not gateway.startswith("Bearer ")):
                # Through the ingress the edge Lua strips the client's X-Tenant-Id and injects a
                # bare tenant id, so an off-cluster binding caller sends its tenant licence as
                # Authorization: Bearer instead. It is authenticated below exactly like the
                # header form (never trusted from the edge); management keeps X-Tenant-Id only.
                gateway = header(scope, b"authorization", 4103)
            if gateway is None or not gateway.startswith("Bearer ") or not gateway[7:]:
                raise AuthenticationError("license required")
            admin_key = header(scope, b"x-mcp-admin-key") if management else None
            binding_token = header(scope, b"x-mcp-binding") if not management else None
            if (management and admin_key is None) or (not management and binding_token is None):
                raise AuthenticationError("credential required")
            if not hasattr(self.state, "licenses"):
                raise AuthorityUnavailableError("authentication unavailable")
            identity = await self.state.licenses.authenticate(gateway[7:])
            binding = None
            session = None
            if management:
                principal = self.state.management.authorize(
                    identity, admin_key, header(scope, b"x-mcp-platform-admin-key")
                )
            else:
                if not identity.tenant_level:
                    # A binding's gateway licence is the tenant's `svc:<tenant>:mcp` licence,
                    # never a member's per-login licence (licences subject_kind).
                    raise AuthenticationError("tenant licence required")
                binding = await self.state.bindings.authenticate(identity, binding_token)
                await self.state.access.effective(binding)
                principal = Principal(
                    identity.tenant_id,
                    identity.license_id,
                    identity.subject,
                    binding.binding_id,
                    binding.revision,
                )
                session = header(scope, b"mcp-session-id", 256)
                if session:
                    await self.state.bindings.check_session(
                        session, identity, binding, binding_token
                    )
            idempotency_key = header(scope, b"x-mcp-idempotency-key", 128)
            if idempotency_key is not None and not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", idempotency_key
            ):
                raise AuthenticationError("invalid idempotency key")
            scope.setdefault("state", {})["principal"] = principal
            # Buffer bounded management JSON before framework/Pydantic processing.
            if principal.management or path.startswith("/mcp/runs"):
                buffered = await self.buffer(scope, receive, send)
                if buffered is None:
                    return
                receive = buffered
        except _BuiltinCaller as found:
            await self.call_builtin(found.caller, scope, receive, send)
            return
        except AuthenticationError:
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        except ForbiddenError:
            await JSONResponse(
                {"error": "forbidden"}, status_code=403, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
            return
        except AuthorityUnavailableError, CatalogConflictError, CatalogNotFoundError, ValueError:
            await JSONResponse(
                {"error": "authority_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        except Exception:
            # Durable binding/session reads failing never authorize a request.
            await JSONResponse(
                {"error": "authority_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return

        rejected = False

        async def authenticated_send(message: Message) -> None:
            nonlocal rejected
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
                if binding is not None and 200 <= message["status"] < 300:
                    try:
                        new_session = header({"headers": response_headers}, b"mcp-session-id", 256)
                        if new_session and new_session != session:
                            if session:
                                raise AuthenticationError("session switching denied")
                            await self.state.bindings.pin_session(
                                new_session,
                                identity,
                                binding,
                                binding_token,
                                self.state.settings.session_ttl_seconds,
                            )
                        if session and scope.get("method") == "DELETE":
                            await self.state.bindings.delete_session(
                                session, identity.tenant_id, binding.binding_id
                            )
                    except Exception:
                        rejected = True
                        await JSONResponse({"error": "session_denied"}, status_code=503)(
                            scope, receive, send
                        )
                        return
            await send(message)

        context_token = authority.set(RequestAuthority(principal, idempotency_key))
        try:
            await self.app(scope, receive, authenticated_send)
        finally:
            authority.reset(context_token)

    async def buffer(self, scope: Scope, receive: Receive, send: Send) -> Receive | None:
        """Read the bounded body; None when the client left or it was too large (413 sent)."""
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            body.extend(message.get("body", b""))
            if len(body) > self.state.settings.max_management_bytes:
                await JSONResponse({"error": "request_too_large"}, status_code=413)(
                    scope, receive, send
                )
                return None
            if not message.get("more_body", False):
                break
        received = False
        original_receive = receive

        async def buffered() -> Message:
            nonlocal received
            if received:
                return await original_receive()
            received = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        return buffered

    async def call_author(
        self, scope: Scope, receive: Receive, send: Send, product_tenant: str | None
    ) -> None:
        """Builder REST: tenant licence (/mcp/builder) or service JWT + tenant licence (product)."""
        try:
            if product_tenant is None:
                author = await authenticate_author(self.state, scope)
                buffered = await self.buffer(scope, receive, send)
                if buffered is None:
                    return
            else:
                try:
                    tenant = uuid.UUID(product_tenant)
                except ValueError:
                    raise ForbiddenError("invalid tenant") from None
                # Authenticate the service JWT first: an unauthenticated caller's body is never read.
                service = await authenticate_product_service(self.state, scope)
                body = await self.read_body(scope, receive, send)
                if body is None:
                    return
                author, body = await authenticate_product_author(
                    self.state, scope, tenant, body, service
                )
                buffered = replay(body)
        except AuthenticationError:
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        except ForbiddenError, PermissionError:
            await JSONResponse(
                {"error": "forbidden"}, status_code=403, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
            return
        except Exception:
            await JSONResponse(
                {"error": "authority_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        scope.setdefault("state", {})["author"] = author

        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, buffered, no_store_send)

    async def read_body(self, scope: Scope, receive: Receive, send: Send) -> bytes | None:
        """The bounded request body, or None when the client left or it was too large (413)."""
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            body.extend(message.get("body", b""))
            if len(body) > self.state.settings.max_management_bytes:
                await JSONResponse({"error": "request_too_large"}, status_code=413)(
                    scope, receive, send
                )
                return None
            if not message.get("more_body", False):
                return bytes(body)

    async def call_internal(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Service-JWT routes: no licence, binding, session or request authority context."""
        try:
            principal = await authenticate_service(self.state, scope)
        except AuthenticationError:
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        except ForbiddenError:
            await JSONResponse(
                {"error": "forbidden"}, status_code=403, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
            return
        except Exception:
            await JSONResponse(
                {"error": "authority_unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        buffered = await self.buffer(scope, receive, send)
        if buffered is None:
            return

        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, buffered, no_store_send)

    async def call_builtin(
        self, caller: CallerAuthority, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Builtin callers: no binding, no session pinning, no catalog authority."""

        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"mcp-session-id"
                ]
                response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
            await send(message)

        context_token = caller_authority.set(caller)
        try:
            await self.app(scope, receive, no_store_send)
        finally:
            caller_authority.reset(context_token)
