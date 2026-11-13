"""OpenAPI document fetching for tenant sources (JSON only; docs/TOOL_BUILDER.md decision 5).

The fetch itself goes through the broker (lane 2): same pinned egress and the same source
credential as a tool call. This module owns what the surface adds around it: the same-origin path
rule, the size/time bounds, JSON-only parsing, the content hash and the per-hour rate limit.
"""

import hashlib
import json
import re
import time
from collections import defaultdict, deque
from typing import Any, Protocol
from uuid import UUID

_SPEC_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,255}$")
SPEC_TIMEOUT_SECONDS = 20.0
HOUR = 3600.0


class DocumentFetcher(Protocol):
    """``src.broker.client.Broker.fetch_document`` (lane 2). ``source`` is the broker's resolved
    source (``src.sources.registry.ResolvedSource``); ``path`` is relative to its ``base_url``
    origin. Returns the raw body, raising when it exceeds ``max_bytes``, is not a 2xx, or the
    egress policy refuses the host."""

    async def fetch_document(
        self, source: Any, path: str, max_bytes: int, timeout: float
    ) -> bytes: ...


class RateLimit:
    """Sliding window per tenant, in process (spec fetches are not stored per lane 1)."""

    def __init__(self, limit: int, window: float = HOUR, clock=time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self.events: dict[UUID, deque[float]] = defaultdict(deque)

    def take(self, tenant: UUID) -> bool:
        now = self.clock()
        events = self.events[tenant]
        while events and now - events[0] >= self.window:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


def checked_spec_path(path: str | None, default: str | None) -> str:
    chosen = path or default
    if not chosen:
        raise ValueError("the source has no spec_path; pass a path")
    if not _SPEC_PATH.fullmatch(chosen):
        raise ValueError("invalid spec path")
    return chosen


def parse_document(raw: bytes, max_bytes: int) -> dict[str, Any]:
    """The spec as a JSON object. YAML and other formats are refused with a clear message."""
    if len(raw) > max_bytes:
        raise ValueError("the spec is larger than the allowed size")
    try:
        document = json.loads(raw)
    except ValueError, RecursionError:
        raise ValueError(
            "the spec is not JSON (YAML is not supported); serve the OpenAPI document as JSON"
        ) from None
    if not isinstance(document, dict):
        raise ValueError("the spec must be a JSON object")
    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith(("3.0", "3.1")):
        raise ValueError("only OpenAPI 3.0.x and 3.1.x documents are supported")
    return document


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def title_version(document: dict[str, Any]) -> tuple[str | None, str | None]:
    info = document.get("info")
    if not isinstance(info, dict):
        return None, None
    title, version = info.get("title"), info.get("version")
    return (
        title[:200] if isinstance(title, str) else None,
        version[:100] if isinstance(version, str) else None,
    )
