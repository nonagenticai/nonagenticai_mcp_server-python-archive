"""web_scrape: fetch one public web page through the internal webscraper service.

Contract (webscraper_service src/routers/scrape.py): POST {WEBSCRAPER_URL}/api/scrape/url with
{"url", "scraper_type", "timeout"} -> ScrapeResponse {url, final_url, title, text_content,
markdown_content, status, error, ...}. Auth there is a licence token in `X-Tenant-Id: Bearer` or
a trusted-network (RFC1918) peer; WEBSCRAPER_TOKEN is sent when set.

The URL comes from an LLM, so it is checked here before the request leaves (the webscraper has its
own guard as well): http/https only, no credentials in the URL, and the host -- literal or every
address it resolves to -- must be a public address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..legal.client import PublicClient
from ..legal.html_text import truncate

web_mcp = FastMCP(name="web")

DEFAULT_TIMEOUT_S = 45.0  # the webscraper's own per-page timeout (sent below) is 30 s
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".cluster.local", ".svc", ".lan", ".home")
_client: PublicClient | None = None


def set_client(client: PublicClient | None) -> None:
    """Test hook: inject a client (or None to rebuild from env on next use)."""
    global _client
    _client = client


def _webscraper() -> PublicClient:
    global _client
    if _client is None:
        url = os.environ.get("WEBSCRAPER_URL")
        if not url:
            raise ToolError("[permanent] WEBSCRAPER_URL is not set on the MCP server")
        try:
            timeout = float(os.environ.get("WEBSCRAPER_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        except ValueError:
            timeout = DEFAULT_TIMEOUT_S
        _client = PublicClient(url, timeout=timeout)
    return _client


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _reject(url: str, why: str) -> ToolError:
    return ToolError(f"[permanent] url not allowed ({why}): {url[:100]!r}")


async def check_url(url: object) -> str:
    """Return the url if it may be fetched, else raise `[permanent] url not allowed (...)`."""
    if not isinstance(url, str) or len(url) > 2000:
        raise ToolError("[permanent] url must be a string of at most 2000 chars")
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # raises ValueError on a malformed port
    except ValueError as e:
        raise _reject(url, "malformed") from e
    if parts.scheme not in ("http", "https"):
        raise _reject(url, "scheme must be http or https")
    if parts.username is not None or parts.password is not None:
        raise _reject(url, "credentials in url")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise _reject(url, "no host")
    if port is not None and port not in (80, 443, 8080, 8443):
        raise _reject(url, "non-standard port")
    single_label = "." not in host and ":" not in host  # "webscraper", "mcp": cluster-short names
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or single_label:
        raise _reject(url, "internal host name")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(literal):
            raise _reject(url, "private, loopback or link-local address")
        return url.strip()
    try:
        addresses = await _resolve(host)
    except OSError as e:
        raise _reject(url, "host does not resolve") from e
    if not addresses or not all(_is_public(ipaddress.ip_address(a)) for a in addresses):
        raise _reject(url, "host resolves to a private, loopback or link-local address")
    return url.strip()


@web_mcp.tool()
async def scrape(url: str, max_chars: int = 30000) -> dict:
    """Fetch one public web page (http/https) and return its title and readable text."""
    url = await check_url(url)
    max_chars = max(500, min(max_chars, 200_000))
    client = _webscraper()
    token = os.environ.get("WEBSCRAPER_TOKEN")
    resp = await client.request(
        "POST",
        "/api/scrape/url",
        json={"url": url, "scraper_type": "auto", "timeout": 30.0},
        headers={"X-Tenant-Id": f"Bearer {token}"} if token else None,
    )
    try:
        body = resp.json()
    except ValueError as e:
        raise ToolError("[retryable] non-JSON answer from the webscraper") from e
    if body.get("status") == "failed":
        raise ToolError(f"[permanent] scrape failed: {str(body.get('error') or 'unknown')[:200]}")
    text = body.get("text_content") or body.get("markdown_content") or ""
    text, truncated = truncate(text, max_chars)
    return {
        "url": body.get("url") or url,
        "final_url": body.get("final_url") or body.get("url") or url,
        "title": body.get("title"),
        "text": text,
        "truncated": truncated,
    }
