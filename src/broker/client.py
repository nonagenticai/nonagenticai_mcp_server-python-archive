"""Fenced, journalled, bounded HTTP broker with per-application credentials."""

import asyncio
import inspect
import json
import logging
import re
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from src.broker.auth import AuthAdapters, AuthFailedError
from src.broker.egress import EgressDeniedError, EgressPolicy, SourceLimiter, tenant_client
from src.broker.models import UpstreamStatusError
from src.broker.policy import enforce, project, transport
from src.catalog.models import AuthAdapter, OwnershipRead
from src.catalog.validation import validate_instance
from src.execution.models import ExecutionDeniedError, OutcomeUnknownError
from src.execution.service import resolved_snapshot
from src.storage.executions import encoded, value_hash
from src.toolkits.models import CredentialReference

logger = logging.getLogger(__name__)


def denial_site(error: BaseException) -> str:
    """Innermost src/ frame that raised, as ``path:line`` (no message, no locals)."""
    site, cause, seen = "unknown", error, set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        tb = cause.__traceback__
        while tb is not None:
            name = tb.tb_frame.f_code.co_filename
            if "/src/" in name:
                site = f"{name[name.rindex('/src/') + 1 :]}:{tb.tb_lineno}"
            tb = tb.tb_next
        # ``raise X from None`` hides the chain (__suppress_context__) but keeps __context__.
        cause = cause.__cause__ or cause.__context__
    return site


EXCERPT_CHARS = 2048


class Broker:
    def __init__(
        self,
        execution,
        catalog,
        credentials,
        licenses,
        sources,
        client=None,
        *,
        tenant_http=None,
        resolver=None,
    ):
        self.execution, self.repository = execution, execution.repository
        self.catalog, self.credentials, self.licenses = catalog, credentials, licenses
        #: Runtime source registry (``src.sources.registry.SourceRegistry``).
        self.sources = sources
        self.settings = self.repository.settings
        self._injected = client
        self._limits = httpx.Limits(
            max_connections=self.settings.api_max_inflight, max_keepalive_connections=8
        )
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self.client = client or self.client_for(None)
        # Tenant-registered sources never use ``client``/``client_for``: they always go through
        # the pinned egress transport (``tenant_http`` is a test seam for a mock transport).
        self.egress = EgressPolicy.from_settings(self.settings, resolver)
        self._tenant_http = tenant_http
        self.auth = AuthAdapters(self.settings.builder_oauth_token_max_ttl, self.tenant_client)
        # In-process per-(tenant, service) limiter for tenant sources. The database permit
        # (storage.executions.permit) remains the cross-process global limit.
        self.limiter = SourceLimiter(
            self.settings.builder_source_calls_per_minute, self.settings.builder_source_concurrency
        )
        self._kwargs: dict[str, frozenset[str]] = {}

    def tenant_client(self) -> httpx.AsyncClient:
        if self._tenant_http is None:
            self._tenant_http = tenant_client(self.egress, self._limits)
        return self._tenant_http

    async def _journal(self, name, *args, **trace):
        """Call a repository journal method with the trace keywords it accepts (method, path,
        http_status, duration_ms, reason); the storage layer owns the signature."""
        function = getattr(self.repository, name)
        if name not in self._kwargs:
            parameters = inspect.signature(function).parameters
            self._kwargs[name] = frozenset(parameters) | (
                frozenset(trace)
                if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
                else frozenset()
            )
        accepted = {k: v for k, v in trace.items() if k in self._kwargs[name] and v is not None}
        return await function(*args, **accepted)

    async def destination(self, operation, service):
        """The source named by ``service.destination_alias``, resolved for the service's owner
        (never by key alone), and the owner-scope rule for tenant tools."""
        owner = await self.catalog.service_owner(operation.service.id)
        return owner, await self.sources.get(owner, service.destination_alias)

    @staticmethod
    def licence_credential(profile, service, destination):
        """The profile credential a licence-kind source needs, or None when it does not match.
        ``source``-adapter services carry no profile credential (see ``source_licence``)."""
        app_key = service.license_app_key
        credential = next((c for c in profile.services if c.service == service.service_key), None)
        if (
            service.auth_adapter == AuthAdapter.SOURCE
            or credential is None
            or app_key is None
            or credential.app_key != app_key
            or destination.auth.app_key != app_key
            or destination.auth.adapter != service.auth_adapter
        ):
            return None
        return credential

    async def source_licence(self, row, service, destination):
        """Mint the RUN's tenant licence for a licence-kind ``source``-adapter service straight
        from the source row (app_key, bundle); bound and draft runs alike, since neither profile
        carries one. The registry already enforced the operator host allow-list. Fails closed when
        the resolver cannot mint or the tenant lacks the grant. Returns (reference, token)."""
        mint = getattr(self.credentials, "mint", None)
        app_key = destination.auth.app_key
        if mint is None or app_key is None or app_key == self.settings.license_app_key:
            raise ExecutionDeniedError()
        minted = await mint(row["tenant_id"], app_key, destination.auth.bundle)
        identity = await self.licenses.authenticate(minted.token.get_secret_value(), app_key)
        if identity.tenant_id != row["tenant_id"] or identity.license_id != minted.license_id:
            raise ExecutionDeniedError()
        reference = CredentialReference(
            service=service.service_key,
            alias=service.service_key,
            license_id=minted.license_id,
            app_key=app_key,
            bundle=destination.auth.bundle,
        )
        return reference, minted.token

    def client_for(self, ca_file: str | None):
        """One pooled client per CA bundle; destinations without ``ca_file`` share the default."""
        if self._injected is not None:
            return self._injected
        if ca_file not in self._clients:
            self._clients[ca_file] = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                verify=ssl.create_default_context(cafile=ca_file) if ca_file else True,
                limits=self._limits,
            )
        return self._clients[ca_file]

    async def close(self):
        for client in {id(c): c for c in (self.client, *self._clients.values())}.values():
            await client.aclose()

    @property
    def gateway_credentials(self) -> bool:
        """Whether the resolver holds the gateway (mcp) licence. A minting resolver does not:
        the gateway licence is the caller's, authenticated on the request that created the run
        (row ``gateway_license_id``), and mcp never mints its own application licence."""
        return getattr(self.credentials, "gateway_credentials", True)

    async def identity(self, row, profile, reference):
        # A minting resolver (src.credentials.minter) is async; the file resolver is not.
        aresolve = getattr(self.credentials, "aresolve", None)
        token = (
            await aresolve(row["tenant_id"], profile.profile_id, reference)
            if aresolve
            else self.credentials.resolve(row["tenant_id"], profile.profile_id, reference)
        )
        identity = await self.licenses.authenticate(token.get_secret_value(), reference.app_key)
        if identity.tenant_id != row["tenant_id"] or identity.license_id != reference.license_id:
            raise ExecutionDeniedError()
        return token

    async def usable(self, tenant_id, owner, service_reference) -> bool:
        """A service is callable by its owner, or by a tenant it is shared with (platform
        services: licence kind or granted). A tenant never reaches another tenant's service."""
        if owner not in {self.settings.platform_tenant_id, tenant_id}:
            return False
        return await self.catalog.may_use_service(tenant_id, service_reference)

    @staticmethod
    async def error_body(response, operation, limit, frame_limit, leaks):
        """Projected, redacted JSON of an expected-status response, or None when it is oversized,
        not JSON, not projectable, or carries credential material. Never an error itself."""
        try:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    return None
            raw = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            body = project(operation, raw)
            public = encoded(body)
            return None if len(public.encode()) > frame_limit or leaks(public) else body
        except Exception:
            return None

    async def fetch_document(self, source, path: str, max_bytes: int, timeout: float) -> bytes:
        """GET one document (an OpenAPI spec) from ``source``'s origin, as the source's own
        credential. Same-origin absolute path only, no redirects, no query, bounded size and time.

        Tenant sources go through the pinned egress transport (SSRF policy applies); platform
        sources through the allow-list client. A licence-kind source is minted for the source's
        owner (the requesting tenant). Returns the raw body; raises ``ExecutionDeniedError`` when
        the policy or the credential refuses, ``ValueError`` for a non-2xx or oversized answer.
        Secrets are never logged and a body that echoes one is refused.
        """
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9._~/-]{0,255}", path):
            raise ValueError("invalid document path")
        if ".." in path.split("/"):
            raise ValueError("invalid document path")
        parts = urlsplit(source.base_url)
        url = f"{parts.scheme}://{parts.netloc}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        query: dict[str, str] = {}
        secrets: list[str] = []
        try:
            if source.auth.kind == "licence":
                mint = getattr(self.credentials, "mint", None)
                if mint is None or source.owner_tenant_id is None:
                    raise ExecutionDeniedError()
                minted = await mint(source.owner_tenant_id, source.auth.app_key, source.auth.bundle)
                token = minted.token.get_secret_value()
                secrets.append(token)
                name = "Authorization" if source.auth.scheme == "bearer" else "X-Tenant-Id"
                headers[name] = "Bearer " + token
            else:
                secrets += await self.auth.apply(source, headers, query)
            http = self.tenant_client() if source.tenant_owned else self.client_for(source.ca_file)
            data = bytearray()
            async with asyncio.timeout(timeout):
                async with http.stream(
                    "GET", url, params=query, headers=headers, timeout=timeout
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise ValueError(f"document answered HTTP {response.status_code}")
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise ValueError("document is larger than the allowed size")
        except EgressDeniedError as error:
            raise ExecutionDeniedError() from error
        except httpx.TransportError as error:
            raise OSError(type(error).__name__) from None
        raw = bytes(data)
        text = raw.decode("utf-8", "replace")
        if any(secret and (secret in text or encoded(secret)[1:-1] in text) for secret in secrets):
            raise ExecutionDeniedError()
        return raw

    @staticmethod
    async def excerpt(response, leaks) -> str | None:
        """The first 2 KB of a failed upstream answer, for an author's test run. Withheld when
        it carries credential material; never raises."""
        try:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) >= EXCERPT_CHARS * 4:
                    break
            text = data.decode("utf-8", "replace").replace("\x00", "")[:EXCERPT_CHARS]
            return "[withheld: credential material]" if leaks(text) else text
        except Exception:
            return None

    async def preflight(self, row):
        _, profile = await self.execution.live(row)
        if (
            profile.gateway.app_key != self.settings.license_app_key
            or profile.gateway.license_id != row["gateway_license_id"]
        ):
            raise ExecutionDeniedError()
        if self.gateway_credentials:
            await self.identity(row, profile, profile.gateway)
        for credential in profile.services:
            await self.identity(row, profile, credential)
        # Verify every pinned service credential before allocating a pod.
        for reference in resolved_snapshot(row).manifest.allowed_operations:
            operation, status = await self.catalog.operation(reference)
            service, service_status = await self.catalog.service(operation.service)
            if status != "published" or service_status != "published":
                raise ExecutionDeniedError()
            owner, source = await self.destination(operation, service)
            if not await self.usable(row["tenant_id"], owner, operation.service):
                raise ExecutionDeniedError()
            if source.auth.kind != "licence":
                continue  # a stored token: no tenant licence to verify
            if service.auth_adapter == AuthAdapter.SOURCE:
                await self.source_licence(row, service, source)
                continue
            credential = self.licence_credential(profile, service, source)
            if credential is None:
                raise ExecutionDeniedError()
            await self.identity(row, profile, credential)

    async def call(
        self, row, operation_id, arguments, idempotency_key=None, *, ownership_reference=None
    ):
        try:
            return await self._call(
                row,
                operation_id,
                arguments,
                idempotency_key,
                ownership_reference=ownership_reference,
            )
        except UpstreamStatusError:
            # A declared, expected upstream status is a result for the tool, not a denial.
            raise
        except Exception as error:
            # Operators need to know WHICH check denied: only fixed, secret-free facts are logged
            # (identities, the exception type and the raising file:line, never its message).
            logger.warning(
                "broker denied run=%s operation=%s cause=%s at=%s",
                row["run_id"],
                operation_id,
                type(error).__name__,
                denial_site(error),
            )
            # Only fixed diagnostic kinds enter the bounded durable event stream.
            # A stale controller cannot append events after losing its fence.
            try:
                async with self.repository.pool.connection() as conn, conn.transaction():
                    await self.repository.owned(
                        conn, row["run_id"], row["worker_id"], row["fence"], allow_cancel=True
                    )
                    await self.repository.event(conn, row, "broker_denied")
            except Exception:
                pass
            raise

    async def _call(
        self, row, operation_id, arguments, idempotency_key=None, *, ownership_reference=None
    ):
        effective, profile = await self.execution.live(row)
        if self.gateway_credentials:
            # A minting resolver never holds the gateway licence: the caller's request already
            # authenticated it, and execution.live above re-checks binding and fence.
            await self.identity(row, profile, profile.gateway)
        manifest = resolved_snapshot(row).manifest
        reference = next(
            (r for r in manifest.allowed_operations if str(r.id) == operation_id), None
        )
        if ownership_reference is not None:
            # Only host policy evaluation can enter this branch, never a frame flag.
            reference = ownership_reference
        if reference is None:
            raise ExecutionDeniedError()
        operation, status = await self.catalog.operation(reference)
        service, service_status = await self.catalog.service(operation.service)
        if status != "published" or service_status != "published":
            raise ExecutionDeniedError()
        if ownership_reference and (
            operation.effect != "read_only"
            or any(isinstance(r, OwnershipRead) for r in operation.scope_rules)
        ):
            raise ExecutionDeniedError()
        if manifest.effect == "read_only" and operation.effect != "read_only":
            raise ExecutionDeniedError()
        validate_instance(operation.input_schema, arguments, self.settings)
        context = resolved_snapshot(row).context.public_values()

        async def ownership(rule, args):
            return await self.call(
                row, str(rule.operation.id), args, ownership_reference=rule.operation
            )

        await enforce(operation.scope_rules, arguments, context, ownership)
        if ownership_reference is None:
            for constraints in effective.operation_constraints:
                if constraints.operation == reference:
                    await enforce(constraints.rules, arguments, context, ownership)
        # URL and credential come from the registered source named by the service, resolved now
        # for the service's owner (a tenant source never serves a platform service or vice versa).
        owner, destination = await self.destination(operation, service)
        if not await self.usable(row["tenant_id"], owner, operation.service):
            raise ExecutionDeniedError()
        path, query, headers, body = transport(operation, arguments, context)
        entry_method = getattr(self.credentials, "entry", None)
        credential = None
        secrets: list[str] = []
        if destination.auth.kind == "licence":
            if service.auth_adapter == AuthAdapter.SOURCE:
                credential, token = await self.source_licence(row, service, destination)
            else:
                credential = self.licence_credential(profile, service, destination)
                if credential is None:
                    raise ExecutionDeniedError()
                token = await self.identity(row, profile, credential)
            secrets.append(token.get_secret_value())
            headers["Authorization" if destination.auth.scheme == "bearer" else "X-Tenant-Id"] = (
                "Bearer " + token.get_secret_value()
            )
            # Optional operator-provisioned upstream OAuth is independent of the
            # application's license. Only the license-header scheme can coexist in
            # this form; neither value is accepted from runner arguments or headers.
            if entry_method and service.auth_adapter != AuthAdapter.SOURCE:
                material = entry_method(row["tenant_id"], profile.profile_id, credential)
                if material.oauth_bearer:
                    if destination.auth.scheme != "x_tenant_id_bearer":
                        raise ExecutionDeniedError()
                    headers["Authorization"] = "Bearer " + material.oauth_bearer.get_secret_value()
        if idempotency_key:
            if (
                not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key)
                or operation.idempotency != "downstream_key"
                or not destination.idempotency_header
            ):
                raise ExecutionDeniedError()
            headers[destination.idempotency_header] = idempotency_key
        if operation.effect == "writes" and operation.max_retries and not idempotency_key:
            raise ExecutionDeniedError()
        trace: dict = {"method": operation.method, "path": path[:1024]}
        started = time.monotonic()

        def finish(**more):
            return {**trace, **more, "duration_ms": int((time.monotonic() - started) * 1000)}

        http = self.tenant_client() if destination.tenant_owned else None
        try:
            # Stored credentials last: the query secret is added after every mapped parameter and
            # a collision with a mapped name or header is rejected, never overwritten.
            secrets += await self.auth.apply(destination, headers, query)
        except EgressDeniedError as error:
            raise ExecutionDeniedError() from error  # the OAuth token endpoint was refused
        payload = encoded(body).encode() if body is not None else None
        request_bytes = (
            len(path.encode())
            + len(encoded(query).encode())
            + len(encoded(headers).encode())
            + len(payload or b"")
        )
        if request_bytes > min(
            service.max_request_bytes, destination.max_request_bytes or service.max_request_bytes
        ):
            raise ExecutionDeniedError()
        if payload is not None:
            headers["Content-Type"] = "application/json"

        def leaks(public: str) -> bool:
            """Whether mounted auth material appears in text bound for the runner."""
            for secret in secrets + [
                self.credentials.resolve(
                    row["tenant_id"], profile.profile_id, ref
                ).get_secret_value()
                for ref in ((profile.gateway,) if self.gateway_credentials else ())
                + profile.services
            ]:
                if secret in public or encoded(secret)[1:-1] in public:
                    return True
            if entry_method:
                for ref in (
                    (profile.gateway,) if self.gateway_credentials else ()
                ) + profile.services:
                    material = entry_method(row["tenant_id"], profile.profile_id, ref)
                    if material.oauth_bearer and material.oauth_bearer.get_secret_value() in public:
                        return True
            return False

        limit = min(
            operation.max_response_bytes,
            service.max_response_bytes,
            destination.max_response_bytes or service.max_response_bytes,
            self.settings.max_result_bytes,
            self.settings.api_max_response_bytes,
        )
        slot = (
            self.limiter.acquire(row["tenant_id"], operation.service.id)
            if destination.tenant_owned
            else None
        )
        try:
            permit = await self.repository.permit(row, service.service_id)
        except BaseException:
            if slot is not None:
                slot.release()
            raise
        sequence = None
        sent = False
        reason: str | None = None
        # Error excerpts are for the owning author, and only about the author's own services: an
        # upstream error body of a platform backend is never recorded.
        draft = row.get("mode") == "draft" and destination.tenant_owned
        try:
            # Commit intent before network I/O. Host request hash excludes credentials.
            sequence = await self._journal(
                "journal_intent",
                row,
                operation,
                value_hash(
                    {
                        "operation": str(reference.id),
                        "version": reference.version,
                        "arguments": arguments,
                    }
                ),
                idempotency_key,
                credential.license_id if credential else row["gateway_license_id"],
                effective.limits.api_calls,
                method=trace["method"],
                path=trace["path"],
            )
            async with asyncio.timeout(
                min(
                    operation.timeout_seconds,
                    self.settings.api_timeout_seconds,
                    max(
                        0.001,
                        (row["deadline"] - datetime.now(UTC)).total_seconds(),
                    ),
                )
            ):
                for attempt in range(operation.max_retries + 1):
                    # Fencing, revocation and budget rechecked even for retries. Budget is
                    # charged per actual HTTP attempt, including host ownership reads.
                    await self.execution.live(row)
                    if attempt:
                        await self.repository.release_permit(row, permit)
                        permit = await self.repository.permit(row, service.service_id)
                        sequence = await self._journal(
                            "journal_intent",
                            row,
                            operation,
                            value_hash(arguments),
                            idempotency_key,
                            credential.license_id if credential else row["gateway_license_id"],
                            effective.limits.api_calls,
                            method=trace["method"],
                            path=trace["path"],
                        )
                    sent, reason = True, None
                    try:
                        async with (http or self.client_for(destination.ca_file)).stream(
                            operation.method,
                            destination.base_url.rstrip("/") + path,
                            params=query,
                            headers=headers,
                            content=payload,
                            timeout=min(
                                operation.timeout_seconds, self.settings.api_timeout_seconds
                            ),
                        ) as response:
                            trace["http_status"] = response.status_code
                            if (
                                response.status_code == 401
                                and destination.auth.kind == "oauth2_client_credentials"
                            ):
                                self.auth.evict(destination)
                            if response.status_code in operation.expected_error_statuses:
                                body = await self.error_body(
                                    response,
                                    operation,
                                    limit,
                                    effective.limits.frame_bytes // 2,
                                    leaks,
                                )
                                if draft and body is not None:
                                    trace["error_excerpt"] = encoded(body)[:EXCERPT_CHARS]
                                await self._journal(
                                    "journal_outcome",
                                    row,
                                    sequence,
                                    "failed",
                                    **finish(reason="status"),
                                )
                                sequence = None
                                raise UpstreamStatusError(response.status_code, body)
                            if not 200 <= response.status_code < 300:
                                if draft:
                                    trace["error_excerpt"] = await self.excerpt(response, leaks)
                                reason = "auth_failed" if response.status_code == 401 else "status"
                                raise ExecutionDeniedError()
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > limit:
                                    reason = "too_large"
                                    raise ExecutionDeniedError()
                            reason = "schema_mismatch"
                            if operation.empty_response:
                                result = {}  # 204 / no body: the operation declares no payload
                            else:
                                raw = json.loads(
                                    data,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                                )
                                result = project(operation, raw)
                            # A downstream echo must not transport mounted auth material
                            # into a runner even if an operator declared a broad schema.
                            if leaks(encoded(result)):
                                raise ExecutionDeniedError()
                            validate_instance(operation.output_schema, result, self.settings)
                            if len(encoded(result).encode()) > effective.limits.frame_bytes // 2:
                                reason = "too_large"
                                raise ExecutionDeniedError()
                            reason = None
                        await self._journal(
                            "journal_outcome", row, sequence, "succeeded", **finish()
                        )
                        return result
                    except EgressDeniedError as error:
                        # Refused before any byte was sent (a redirect was received and refused).
                        reason = error.reason
                        sent = error.reason == "redirect"
                        raise
                    except httpx.TransportError as error:
                        reason = (
                            "timeout" if isinstance(error, httpx.TimeoutException) else "transport"
                        )
                        await self._journal(
                            "journal_outcome",
                            row,
                            sequence,
                            "outcome_unknown" if operation.effect == "writes" else "failed",
                            **finish(reason=reason),
                        )
                        if attempt >= operation.max_retries:
                            raise
                        await asyncio.sleep(0.05 * (attempt + 1))
        except UpstreamStatusError:
            raise
        except BaseException as error:
            if reason is None:
                reason = (
                    "timeout"
                    if isinstance(error, TimeoutError)
                    else "auth_failed"
                    if isinstance(error, AuthFailedError)
                    else "ssrf_blocked"
                    if isinstance(error, EgressDeniedError)
                    else "transport"
                    if isinstance(error, httpx.TransportError)
                    else None
                )
            ambiguous = sent and operation.effect == "writes"
            if sequence is not None:
                await self._journal(
                    "journal_outcome",
                    row,
                    sequence,
                    "outcome_unknown" if ambiguous else "failed",
                    **finish(reason=reason),
                )
            if ambiguous:
                raise OutcomeUnknownError() from None
            raise ExecutionDeniedError() from None
        finally:
            await self.repository.release_permit(row, permit)
            if slot is not None:
                slot.release()
