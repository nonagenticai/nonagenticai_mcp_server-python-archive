"""HTTP client for the public legal sources: the APIs (ELI/Sejm, SAOS, CELLAR, KRS, MF VAT
whitelist) and the scrape-only sites (KIO/UZP rulings, UOKiK decisions, the Dolnośląskie official
journal).

Same error contract as the care-bus client: every failure leaves as a ToolError whose message
starts with "[retryable] " (timeout, unreachable, 429, 5xx) or "[permanent] " (other 4xx, invalid
argument). The care-bus researcher branches on that prefix.

Base URLs are overridable per service (LEGAL_*_URL) so tests can point them anywhere; the test
hook is set_client().

Scrape-only sites are public HTML front-ends of small government servers, so their clients are
built with a per-host minimum interval between requests (LEGAL_SCRAPE_MIN_INTERVAL_S, default 1 s):
every request to that host waits for its slot, across tools and concurrent calls. The slots live
in this process: several replicas/workers each pace on their own (accepted -- one replica today).
A redirect hop is paced like a request, and a paced client refuses a redirect to another host
(it would reach a server outside the pacing) as [permanent].

Bodies are streamed and capped: a scraped client refuses anything over MAX_SCRAPE_BODY_BYTES
(callers may pass a different max_bytes, e.g. for PDFs) -- by Content-Length before reading, and
by decoded bytes while reading, so an oversized or gzip-bombed answer never lands in memory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

USER_AGENT = "nonagenticai-mcp-legal/1.0 (+https://nonagentic.ai)"
DEFAULT_TIMEOUT_S = 20.0

# service -> (env var, default public base URL)
_ENV: dict[str, tuple[str, str]] = {
    "eli": ("LEGAL_ELI_URL", "https://api.sejm.gov.pl"),
    "saos": ("LEGAL_SAOS_URL", "https://www.saos.org.pl"),
    "cellar": ("LEGAL_CELLAR_URL", "https://publications.europa.eu"),
    "krs": ("LEGAL_KRS_URL", "https://api-krs.ms.gov.pl"),
    "mf_wl": ("LEGAL_MF_WL_URL", "https://wl-api.mf.gov.pl"),
    "kio": ("LEGAL_KIO_URL", "https://orzeczenia.uzp.gov.pl"),
    "uokik": ("LEGAL_UOKIK_URL", "https://decyzje.uokik.gov.pl"),
    "duw": ("LEGAL_DUW_URL", "https://edzienniki.duw.pl"),
}
# Scrape-only sites: polite per-host pacing (see module docstring).
SCRAPED = frozenset({"kio", "uokik", "duw"})
DEFAULT_SCRAPE_INTERVAL_S = 1.0
# HTML pages and JSON answers of the scraped sites; the biggest (DUW search JSON) is ~0.5 MB.
MAX_SCRAPE_BODY_BYTES = 5 * 1024 * 1024
# Headers that describe the wire body, not the decoded one a capped read keeps.
_WIRE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
# host -> monotonic time of the next free request slot
_next_slot: dict[str, float] = {}
_clients: dict[str, PublicClient | None] = {name: None for name in _ENV}


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:200]
        if body.get("message"):  # MF whitelist: {"code": "WL-113", "message": "..."}
            return str(body["message"])[:200]
    return resp.text[:200].strip() or resp.reason_phrase


class PublicClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        min_interval: float = 0.0,
        max_body_bytes: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_body_bytes = max_body_bytes
        self.host = httpx.URL(self.base_url).host

    async def _pace(self) -> None:
        """Wait for this host's next request slot. Reserving the slot has no await in between,
        so concurrent callers queue up one interval apart instead of racing."""
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        slot = max(now, _next_slot.get(self.host, 0.0))
        _next_slot[self.host] = slot + self.min_interval
        if slot > now:
            await asyncio.sleep(slot - now)

    async def request(
        self,
        method: str,
        path: str,
        *,
        not_found: str | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        max_bytes: int | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """One call. A 404 raises `[permanent] {not_found}` when given, else the generic 4xx.
        `max_bytes` (default: the client's max_body_bytes; None = no cap) bounds the decoded body."""
        cap = max_bytes if max_bytes is not None else self.max_body_bytes
        await self._pace()
        started = time.monotonic()
        hops = 0

        async def on_request(req: httpx.Request) -> None:
            nonlocal hops
            hops += 1
            if hops == 1 or self.min_interval <= 0:
                return
            if req.url.host != self.host:
                raise ToolError(
                    f"[permanent] {method} {path} redirects to another host ({req.url.host})"
                )
            await self._pace()

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                headers={"User-Agent": USER_AGENT, **(headers or {})},
                follow_redirects=follow_redirects,
                event_hooks={"request": [on_request]},
            ) as client:
                async with asyncio.timeout(self.timeout):
                    async with client.stream(method, path, **kwargs) as streamed:
                        body = await _read_capped(streamed, cap, method, path)
        except (httpx.TimeoutException, TimeoutError) as e:
            ms = int((time.monotonic() - started) * 1000)
            raise ToolError(f"[retryable] timeout after {ms} ms calling {method} {path}") from e
        except httpx.TransportError as e:
            logger.warning("legal API %s unreachable for %s %s: %r", self.base_url, method, path, e)
            raise ToolError(
                f"[retryable] cannot reach {self.base_url} for {method} {path}: "
                f"{e.__class__.__name__}"
            ) from e
        resp = httpx.Response(
            streamed.status_code,
            headers=[(k, v) for k, v in streamed.headers.items() if k not in _WIRE_HEADERS],
            content=body,
            request=streamed.request,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ToolError(
                f"[retryable] {resp.status_code} from {method} {path}: {_message(resp)}"
            )
        if resp.status_code == 404 and not_found:
            raise ToolError(f"[permanent] {not_found}")
        if resp.status_code >= 400:
            raise ToolError(
                f"[permanent] {resp.status_code} from {method} {path}: {_message(resp)}"
            )
        return resp

    async def get_json(
        self, path: str, params: dict | None = None, *, not_found: str | None = None
    ) -> Any:
        resp = await self.request(
            "GET",
            path,
            params=_clean(params),
            not_found=not_found,
            headers={"Accept": "application/json"},
        )
        try:
            return resp.json()
        except ValueError as e:
            raise ToolError(f"[retryable] non-JSON answer from GET {path}") from e

    async def get_text(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        not_found: str | None = None,
        follow_redirects: bool = False,
    ) -> str:
        resp = await self.request(
            "GET", path, headers=headers, not_found=not_found, follow_redirects=follow_redirects
        )
        return resp.text

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


async def _read_capped(resp: httpx.Response, cap: int | None, method: str, path: str) -> bytes:
    """The decoded body, refused as [permanent] once it would exceed `cap` bytes."""
    if cap is None:
        return await resp.aread()
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise ToolError(
            f"[permanent] answer to {method} {path} too large ({declared} bytes, max {cap})"
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > cap:
            raise ToolError(f"[permanent] answer to {method} {path} too large (over {cap} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


def _clean(params: dict | None) -> dict | None:
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None and v != ""}


def timeout_s() -> float:
    try:
        return float(os.environ.get("LEGAL_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def scrape_interval_s() -> float:
    try:
        return max(
            0.0, float(os.environ.get("LEGAL_SCRAPE_MIN_INTERVAL_S", DEFAULT_SCRAPE_INTERVAL_S))
        )
    except ValueError:
        return DEFAULT_SCRAPE_INTERVAL_S


def get_client(service: str) -> PublicClient:
    client = _clients[service]
    if client is None:
        env_var, default = _ENV[service]
        client = PublicClient(
            os.environ.get(env_var) or default,
            timeout=timeout_s(),
            min_interval=scrape_interval_s() if service in SCRAPED else 0.0,
            max_body_bytes=MAX_SCRAPE_BODY_BYTES if service in SCRAPED else None,
        )
        _clients[service] = client
    return client


def set_client(service: str, client: PublicClient | None) -> None:
    """Test hook: inject a client (or None to rebuild from env on next use)."""
    if service not in _clients:
        raise KeyError(service)
    _clients[service] = client
