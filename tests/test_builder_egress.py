"""Tenant egress: address policy, hostname policy, pinned connections, no redirects."""

import asyncio
import datetime
import ipaddress
import ssl
from contextlib import asynccontextmanager

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from src.broker import egress
from src.broker.egress import (
    EgressDeniedError,
    EgressPolicy,
    PinnedBackend,
    RateLimitedError,
    SourceLimiter,
    TenantTransport,
    address_allowed,
    check_hostname,
    tenant_client,
)

HOST = "api.tenant-test.example.com"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "169.254.169.254",
        "169.254.170.2",
        "100.64.0.1",
        "100.100.100.200",
        "192.0.0.192",
        "0.0.0.0",
        "224.0.0.1",
        "239.255.255.250",
        "240.0.0.1",
        "255.255.255.255",
        "198.18.0.1",
        "192.0.2.1",
        "::1",
        "::",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd00:ec2::254",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.1.2.3",
        "::ffff:169.254.169.254",
        "::127.0.0.1",
        "64:ff9b::7f00:1",
        "64:ff9b::a9fe:a9fe",
        "2002:7f00:1::1",
        "2002:a9fe:a9fe::1",
        "2002:0a00:0001::",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # teredo with a private embedded client
        "2001:db8::1",
        "100::1",
        "fec0::1",  # deprecated site-local
        "febf::1",
        "192.88.99.1",  # 6to4 relay anycast
        "192.88.99.255",
        "64:ff9b::c058:6301",  # NAT64 of 192.88.99.1
    ],
)
def test_non_public_addresses_are_refused(address):
    assert not address_allowed(egress.parse_address(address), ())


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "2a00:1450:4001::1"]
)
def test_global_unicast_addresses_pass(address):
    assert address_allowed(egress.parse_address(address), ())


def test_deny_cidrs_apply_to_both_families():
    deny = [ipaddress.ip_network("8.8.8.0/24"), ipaddress.ip_network("2606:4700::/32")]
    assert not address_allowed(egress.parse_address("8.8.8.8"), deny)
    assert not address_allowed(egress.parse_address("2606:4700:4700::1111"), deny)
    assert address_allowed(egress.parse_address("1.1.1.1"), deny)
    # An embedded address is checked against the deny list too.
    assert not address_allowed(egress.parse_address("::ffff:8.8.8.8"), deny)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "app.localhost",
        "127.0.0.1",
        "[::1]",
        "2130706433",
        "0x7f.1",
        "intranet",
        "printer.local",
        "db.internal",
        "api.svc",
        "x.cluster.local",
        "x.svc.cluster.local",
        "",
        "a..b.com",
        "-bad.example.com",
        "under_score.example.com",
        "u@h.example.com",
    ],
)
def test_internal_hostnames_are_refused(host):
    with pytest.raises(EgressDeniedError):
        check_hostname(host)


def test_public_hostnames_pass():
    assert check_hostname("API.Example.com.") == "api.example.com"


def resolver(*answers):
    """A resolver returning each answer in turn (the last repeats) and counting lookups."""
    calls = []

    async def resolve(host, port):
        calls.append((host, port))
        return answers[min(len(calls), len(answers)) - 1]

    resolve.calls = calls
    return resolve


class FakeInner(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.connected = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        raise httpcore.ConnectError("fake")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError

    async def sleep(self, seconds):
        pass


async def test_mixed_public_and_private_answers_are_refused_entirely():
    inner = FakeInner()
    backend = PinnedBackend(EgressPolicy(resolver=resolver(["8.8.8.8", "10.0.0.5"])), inner)
    with pytest.raises(EgressDeniedError):
        await backend.connect_tcp(HOST, 443)
    assert inner.connected == []  # not even the public address was tried


async def test_connection_uses_the_validated_address_and_resolves_once():
    resolve = resolver(["8.8.8.8"], ["127.0.0.1"])  # the second lookup would be an attack
    inner = FakeInner()
    backend = PinnedBackend(EgressPolicy(resolver=resolve), inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp(HOST, 443)
    assert inner.connected == [("8.8.8.8", 443)]
    assert len(resolve.calls) == 1


async def test_every_validated_address_is_tried_in_order():
    inner = FakeInner()
    backend = PinnedBackend(EgressPolicy(resolver=resolver(["8.8.8.8", "1.1.1.1"])), inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp(HOST, 443)
    assert inner.connected == [("8.8.8.8", 443), ("1.1.1.1", 443)]


@pytest.mark.parametrize("port", [80, 8443, 22])
async def test_only_allow_listed_ports(port):
    inner = FakeInner()
    backend = PinnedBackend(EgressPolicy(resolver=resolver(["8.8.8.8"])), inner)
    with pytest.raises(EgressDeniedError):
        await backend.connect_tcp(HOST, port)
    assert inner.connected == []


async def test_unresolvable_and_empty_answers_are_refused():
    async def fail(host, port):
        raise OSError("nxdomain")

    for resolve in (fail, resolver([])):
        with pytest.raises(EgressDeniedError):
            await PinnedBackend(EgressPolicy(resolver=resolve), FakeInner()).connect_tcp(HOST, 443)


async def test_the_transport_really_uses_the_pinned_backend():
    inner = FakeInner()
    policy = EgressPolicy(resolver=resolver(["8.8.8.8"]))
    transport = TenantTransport(policy, backend=PinnedBackend(policy, inner))
    assert transport._pool._network_backend is transport.backend  # the pin point we rely on
    assert isinstance(transport.backend, PinnedBackend)
    async with tenant_client(policy, transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"https://{HOST}/x")
    assert inner.connected == [("8.8.8.8", 443)]  # the request went through our backend only


async def test_transport_refuses_plain_http_userinfo_and_internal_hosts():
    policy = EgressPolicy(resolver=resolver(["8.8.8.8"]))
    async with tenant_client(policy) as client:
        for url in (
            f"http://{HOST}/x",
            "https://u:p@api.example.com/x",
            "https://localhost/x",
            "https://127.0.0.1/x",
            "https://db.internal/x",
            f"https://{HOST}:8443/x",
        ):
            with pytest.raises(EgressDeniedError):
                await client.get(url)


def test_client_options_are_locked_down():
    client = tenant_client(EgressPolicy())
    assert client.follow_redirects is False
    assert client._trust_env is False
    assert isinstance(client._transport, TenantTransport)


# --- a real TLS server: SNI and Host stay on the hostname while the socket is pinned ----------


def self_signed(tmp_path, hostname):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


@asynccontextmanager
async def tls_server(tmp_path, response: bytes, hostname=HOST):
    cert_file, key_file = self_signed(tmp_path, hostname)
    seen = {"sni": [], "requests": []}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    context.sni_callback = lambda sock, name, ctx: seen["sni"].append(name)

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        seen["requests"].append(head.decode())
        writer.write(response)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    client_context = ssl.create_default_context(cafile=str(cert_file))
    try:
        yield port, seen, client_context
    finally:
        server.close()
        await server.wait_closed()


def loopback_ok(monkeypatch):
    # The server is on loopback, which the policy (correctly) refuses; this test is about the pin.
    monkeypatch.setattr(egress, "address_allowed", lambda ip, deny: True)


async def test_sni_and_host_use_the_hostname_while_the_socket_is_pinned(tmp_path, monkeypatch):
    loopback_ok(monkeypatch)
    ok = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
    async with tls_server(tmp_path, ok) as (port, seen, context):
        resolve = resolver(["127.0.0.1"])
        policy = EgressPolicy(ports=(port,), resolver=resolve)
        transport = TenantTransport(policy, ssl_context=context)
        async with tenant_client(policy, transport=transport) as client:
            response = await client.get(f"https://{HOST}:{port}/records?x=1")
        assert response.status_code == 200 and response.json() == {}
        assert seen["sni"] == [HOST]  # SNI and certificate verification on the hostname
        assert f"Host: {HOST}:{port}" in seen["requests"][0]
        assert seen["requests"][0].startswith("GET /records?x=1 ")
        assert transport.backend.connections == [(HOST, "127.0.0.1", port)]
        assert resolve.calls == [(HOST, port)]


async def test_certificate_for_another_name_is_rejected(tmp_path, monkeypatch):
    loopback_ok(monkeypatch)
    async with tls_server(tmp_path, b"", hostname="other.example.com") as (port, _, context):
        policy = EgressPolicy(ports=(port,), resolver=resolver(["127.0.0.1"]))
        transport = TenantTransport(policy, ssl_context=context)
        async with tenant_client(policy, transport=transport) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://{HOST}:{port}/")


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_are_refused(tmp_path, monkeypatch, status):
    loopback_ok(monkeypatch)
    redirect = (
        f"HTTP/1.1 {status} Moved\r\nLocation: https://169.254.169.254/\r\n"
        "Content-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode()
    async with tls_server(tmp_path, redirect) as (port, seen, context):
        policy = EgressPolicy(ports=(port,), resolver=resolver(["127.0.0.1"]))
        async with tenant_client(
            policy, transport=TenantTransport(policy, ssl_context=context)
        ) as client:
            with pytest.raises(EgressDeniedError) as denied:
                await client.get(f"https://{HOST}:{port}/")
        assert denied.value.reason == "redirect"
        assert len(seen["requests"]) == 1  # the Location was never followed


# --- limiter --------------------------------------------------------------------------------


def test_limiter_caps_rate_and_concurrency_per_tenant_and_service():
    now = [0.0]
    limiter = SourceLimiter(3, 2, clock=lambda: now[0])
    first = limiter.acquire("t", "s")
    second = limiter.acquire("t", "s")
    with pytest.raises(RateLimitedError):
        limiter.acquire("t", "s")  # concurrency 2
    limiter.acquire("t", "other")  # other service independent
    limiter.acquire("u", "s")  # other tenant independent
    first.release()
    third = limiter.acquire("t", "s")
    second.release(), third.release()
    with pytest.raises(RateLimitedError):
        limiter.acquire("t", "s")  # 3 calls in the minute
    now[0] = 61.0
    limiter.acquire("t", "s").release()
