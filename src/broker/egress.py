"""Egress for tenant-registered sources: resolve, validate every address, pin the connection.

The connection is made to a validated IP literal while TLS (SNI and certificate checks) and the
``Host`` header stay on the hostname, so a DNS answer that changes between "check" and "use"
cannot redirect the request. ``PinnedBackend`` is the only place a tenant connection is opened;
``TenantTransport`` installs it as the httpcore pool's network backend (the pin relies on
``httpx.AsyncHTTPTransport._pool``, hence httpx/httpcore are pinned exactly and a test asserts the
backend is used).
"""

import ipaddress
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence

import anyio
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

from src.execution.models import ExecutionDeniedError
from src.sources.guard import is_ip_literal
from src.sources.models import INTERNAL_SUFFIXES

# A request line, header value or token can reach these loggers at DEBUG; never let it.
for _name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection", "hpack"):
    logging.getLogger(_name).setLevel(logging.WARNING)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192", "fd00:ec2::254")
)
#: Tunnelling / translation prefixes whose embedded IPv4 is validated in addition.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_TEREDO = ipaddress.ip_network("2001::/32")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")
_V4_COMPAT = ipaddress.ip_network("::/96")
#: Deprecated ranges the stdlib still reports as global: IPv6 site-local (RFC 3879) and the 6to4
#: relay anycast prefix (RFC 7526).
_DEPRECATED = (ipaddress.ip_network("fec0::/10"), ipaddress.ip_network("192.88.99.0/24"))
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+")


class EgressDeniedError(Exception):  # noqa: N818 -- a policy refusal, raised before any byte is sent
    """The destination (host, port, address, scheme or redirect) is refused. ``reason`` is the
    fixed trace kind (``ssrf_blocked`` or ``redirect``); the message never names the target."""

    def __init__(self, reason: str = "ssrf_blocked") -> None:
        super().__init__(reason)
        self.reason = reason


def parse_address(text: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(text.split("%", 1)[0].strip("[]"))
    except ValueError:
        return None


def embedded_addresses(ip: IPAddress) -> list[IPAddress]:
    """IPv4 addresses an IPv6 address carries (mapped, NAT64, 6to4, Teredo, IPv4-compatible)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    found: list[IPAddress] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip in _NAT64 or ip in _V4_COMPAT:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip in _SIXTOFOUR and ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip in _TEREDO and ip.teredo is not None:
        server, client = ip.teredo
        found.extend((server, client))
    return found


def address_allowed(ip: IPAddress, deny: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]):
    """Whether a connection to ``ip`` is permitted: global unicast only, outside ``deny``."""
    if ip in METADATA_ADDRESSES:
        return False
    for embedded in embedded_addresses(ip):
        if not address_allowed(embedded, deny):
            return False
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
    ):
        return False
    if any(ip.version == net.version and ip in net for net in _DEPRECATED):
        return False
    # The tunnelling prefixes carry their own target: bare ::/96, 2001::/32 and 2002::/16 are
    # only reachable through the embedded address checked above, and never themselves global
    # unicast we should talk to directly.
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip in _V4_COMPAT or ip in _NAT64 or ip in _TEREDO or ip in _SIXTOFOUR
    ):
        return False
    return not any(ip.version == net.version and ip in net for net in deny)


def check_hostname(host: str) -> str:
    """A public DNS name or EgressDeniedError. Applied at registration and again on every call."""
    name = host.lower().rstrip(".")
    if (
        not name
        or len(name) > 253
        or is_ip_literal(name)
        or "." not in name
        or not _HOSTNAME.fullmatch(name)
        or name == "localhost"
        or name.endswith(INTERNAL_SUFFIXES)
    ):
        raise EgressDeniedError
    return name


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class EgressPolicy:
    def __init__(
        self,
        ports: Iterable[int] = (443,),
        deny_cidrs: Iterable[str] = (),
        resolver: Resolver | None = None,
    ) -> None:
        self.ports = frozenset(ports)
        self.deny = tuple(ipaddress.ip_network(c, strict=False) for c in deny_cidrs)
        self.resolver: Resolver = resolver or system_resolver

    @classmethod
    def from_settings(cls, settings, resolver: Resolver | None = None) -> EgressPolicy:
        return cls(settings.builder_allowed_ports, settings.builder_deny_cidrs, resolver)

    async def validated(self, host: str, port: int) -> list[IPAddress]:
        """Every resolved address, all of which pass the policy, or EgressDeniedError."""
        check_hostname(host)
        if port not in self.ports:
            raise EgressDeniedError
        try:
            answers = await self.resolver(host, port)
        except OSError:
            raise EgressDeniedError("transport") from None
        addresses: list[IPAddress] = []
        for answer in answers:
            ip = parse_address(answer)
            if ip is None or not address_allowed(ip, self.deny):
                raise EgressDeniedError  # one bad answer poisons the whole set
            if ip not in addresses:
                addresses.append(ip)
        if not addresses:
            raise EgressDeniedError("transport")
        return addresses


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens connections only to validated IP literals; TLS keeps the hostname."""

    def __init__(
        self, policy: EgressPolicy, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self.policy = policy
        self.inner = inner or AnyIOBackend()
        self.connections: list[tuple[str, str, int]] = []  # (hostname, pinned ip, port) for tests

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self.policy.validated(host, port)
        last: Exception | None = None
        for ip in addresses:
            try:
                stream = await self.inner.connect_tcp(
                    str(ip), port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as error:
                last = error
                continue
            self.connections.append((host, str(ip), port))
            return stream
        raise last or EgressDeniedError("transport")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise EgressDeniedError

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class TenantTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose connection pool can only use :class:`PinnedBackend`."""

    def __init__(
        self,
        policy: EgressPolicy,
        limits: httpx.Limits | None = None,
        ssl_context=None,
        backend: PinnedBackend | None = None,
    ) -> None:
        limits = limits or httpx.Limits(
            max_connections=64, max_keepalive_connections=8, keepalive_expiry=15
        )
        super().__init__(verify=True, trust_env=False, http1=True, http2=False, limits=limits)
        self.policy = policy
        self.backend = backend or PinnedBackend(policy)
        # httpx exposes no network_backend option: replace the pool it built (no connections yet).
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=self.backend,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme != "https" or url.userinfo:
            raise EgressDeniedError
        check_hostname(url.host)
        if (url.port or 443) not in self.policy.ports:
            raise EgressDeniedError
        response = await super().handle_async_request(request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise EgressDeniedError("redirect")
        return response


def tenant_client(
    policy: EgressPolicy, limits: httpx.Limits | None = None, transport=None
) -> httpx.AsyncClient:
    """The one client shape for tenant sources: no redirects, no environment, strict TLS."""
    return httpx.AsyncClient(
        transport=transport or TenantTransport(policy, limits),
        follow_redirects=False,
        trust_env=False,
        verify=True,
    )


class RateLimitedError(ExecutionDeniedError):
    """A tenant source is over its per-minute or concurrency budget (denied before any I/O)."""


class _Slot:
    def __init__(self, release: Callable[[], None]) -> None:
        self.release = release


class SourceLimiter:
    """In-process per-(tenant, service) call rate and concurrency for tenant sources.

    The database permit stays the cross-process global limit; this bounds one process, so a
    tenant tool cannot turn the broker into a request cannon against a single third party.
    """

    def __init__(
        self, calls_per_minute: int, concurrency: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.calls, self.concurrency, self._clock = calls_per_minute, concurrency, clock
        self._recent: dict[tuple, list[float]] = {}
        self._inflight: dict[tuple, int] = {}

    def acquire(self, tenant, service) -> _Slot:
        key, now = (tenant, service), self._clock()
        recent = [t for t in self._recent.get(key, ()) if now - t < 60]
        if len(recent) >= self.calls or self._inflight.get(key, 0) >= self.concurrency:
            self._recent[key] = recent
            raise RateLimitedError
        recent.append(now)
        self._recent[key] = recent
        self._inflight[key] = self._inflight.get(key, 0) + 1
        if len(self._recent) > 4096:
            self._recent = {k: v for k, v in self._recent.items() if v and now - v[-1] < 60}

        def release() -> None:
            left = self._inflight.get(key, 1) - 1
            if left > 0:
                self._inflight[key] = left
            else:
                self._inflight.pop(key, None)

        return _Slot(release)
