"""Destination policy shared by registration and call time (SSRF guard).

A source's ``base_url`` is an operator-approved origin with an optional fixed path prefix
(``https://api.nonagentic.ai/coding``). The origin must additionally be named by
``MCP_SOURCE_ALLOWED_HOSTS``: exact hosts, ``*.suffix`` patterns, or ``host:port`` when the port is
not the scheme default. An empty allow-list allows nothing. IP literals and userinfo are always
rejected, so DNS names are the only addressable upstreams.
"""

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

PATH_PATTERN = r"(?:/[a-z0-9][a-z0-9_-]*)*/?"
DEFAULT_PORTS = {"https": 443, "http": 80}


def checked_base_url(base_url: str, allow_internal_http: bool = False) -> str:
    """Scheme/userinfo/path policy (no allow-list); raises ValueError."""
    try:
        url = urlsplit(base_url)
        port = url.port
    except ValueError:
        raise ValueError("source requires an explicitly approved origin") from None
    if (
        url.scheme not in ({"https", "http"} if allow_internal_http else {"https"})
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or "@" in url.netloc
        or url.query
        or url.fragment
        or not re.fullmatch(PATH_PATTERN, url.path)
        or (port is not None and not 1 <= port <= 65535)
        or is_ip_literal(url.hostname)
        or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", url.hostname)
    ):
        raise ValueError("source requires an explicitly approved origin")
    return base_url


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    # Numeric last labels (``127.1``, ``2130706433``, ``0x7f.1``) are resolved as addresses by
    # some stacks; no legitimate DNS name ends in a number.
    last = host.rsplit(".", 1)[-1]
    return last.isdigit() or last.startswith("0x")


def host_allowed(base_url: str, allowed: Iterable[str]) -> bool:
    url = urlsplit(base_url)
    host = (url.hostname or "").lower()
    port = url.port
    non_default = port is not None and port != DEFAULT_PORTS.get(url.scheme)
    for entry in allowed:
        entry = entry.strip().lower()
        if not entry:
            continue
        pattern, _, entry_port = entry.partition(":")
        if non_default:
            # A non-default port is allowed only by an explicit ``host:port`` entry.
            if entry_port != str(port):
                continue
        elif entry_port:
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and len(host) > len(suffix):
                return True
        elif host == pattern:
            return True
    return False


def enforce(base_url: str, allow_internal_http: bool, allowed: Iterable[str]) -> str:
    checked_base_url(base_url, allow_internal_http)
    if not host_allowed(base_url, allowed):
        raise ValueError("source host is not in the allowed host list")
    return base_url
