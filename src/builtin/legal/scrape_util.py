"""Shared bits of the scrape-only legal tools (KIO, UOKiK, DUW): document cache, PDF text,
Polish dates and small HTML helpers for the sites' fixed markup."""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Any

from cachetools import TTLCache
from fastmcp.exceptions import ToolError

from .client import PublicClient
from .html_text import strip_tags, truncate
from .pdf_text import pdf_to_text

# One court ruling / decision / journal PDF; real ones are well under 5 MB.
MAX_PDF_BYTES = 15 * 1024 * 1024
# Pages read per PDF, and wall time for one extraction. A timed-out extraction keeps running in
# its worker thread (threads cannot be killed); the page cap is what bounds that leftover work.
MAX_PDF_PAGES = 200
PDF_TIMEOUT_S = 60.0
# Default sized so a whole result stays under the care-bus researcher's 40k stored-step cap (a cut
# result loses its JSON shape); callers may ask for more.
DEFAULT_MAX_CHARS = 25_000

# Published rulings / decisions / journal positions do not change; a day keeps repeat citations
# (search -> document -> verifier re-read) off the source servers.
_doc_cache: TTLCache = TTLCache(maxsize=128, ttl=24 * 3600)


def clear_doc_cache() -> None:
    _doc_cache.clear()


def cached(key: str) -> dict | None:
    return _doc_cache.get(key)


def remember(key: str, doc: dict) -> None:
    _doc_cache[key] = doc


def finish(doc: dict[str, Any], max_chars: int, hit: bool) -> dict[str, Any]:
    """The cached document as one call returns it: text cut to max_chars, flags added."""
    text, truncated = truncate(str(doc["text"]), max_chars)
    # `text` goes LAST: if a consumer cuts the serialised result, the metadata (url, signature) survives
    head = {k: v for k, v in doc.items() if k != "text"}
    return {**head, "truncated": truncated, "cached": hit, "text": text}


def clamp_max_chars(max_chars: int) -> int:
    return max(500, min(max_chars, 300_000))


def clamp_page(page: int, page_size: int, *, max_size: int = 50) -> tuple[int, int]:
    return max(1, page), max(1, min(page_size, max_size))


def check_text(field: str, value: str | None, *, max_len: int = 200) -> str | None:
    """None/blank -> None; too long -> [permanent]; otherwise stripped."""
    if value is None or not value.strip():
        return None
    if len(value) > max_len:
        raise ToolError(f"[permanent] {field} too long (max {max_len} chars)")
    return value.strip()


async def fetch_pdf(
    client: PublicClient, path: str, *, not_found: str, max_bytes: int = MAX_PDF_BYTES
) -> bytes:
    """The PDF's bytes, streamed and refused past `max_bytes` (see PublicClient.request)."""
    resp = await client.request(
        "GET", path, not_found=not_found, follow_redirects=True, max_bytes=max_bytes
    )
    if not resp.content.startswith(b"%PDF"):
        raise ToolError("[permanent] the document file is not a PDF")
    return resp.content


async def pdf_text(data: bytes) -> str:
    """Text of the first MAX_PDF_PAGES pages, within PDF_TIMEOUT_S."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(pdf_to_text, data, MAX_PDF_PAGES), PDF_TIMEOUT_S
        )
    except TimeoutError as e:
        # the same file would time out again: retrying cannot help
        raise ToolError(f"[permanent] reading the PDF text took over {PDF_TIMEOUT_S:g} s") from e
    except Exception as e:  # pypdf raises many types on a damaged file
        raise ToolError(f"[permanent] cannot read the PDF text: {e.__class__.__name__}") from e


async def fetch_pdf_text(client: PublicClient, path: str, *, not_found: str) -> str:
    return await pdf_text(await fetch_pdf(client, path, not_found=not_found))


_MONTHS = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "września": 9,
    "października": 10,
    "listopada": 11,
    "grudnia": 12,
}
_PL_DATE = re.compile(r"\bdnia\s+(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})", re.I)


def polish_date(text: str) -> str | None:
    """ISO date of the first "[z] dnia 25 sierpnia 2022" in `text`, else None. Callers pass the
    head of a ruling, where the first such date is the ruling's own ("WYROK z dnia ...",
    "Warszawa, dnia ...") -- later ones are dates of cited acts."""
    m = _PL_DATE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
    except ValueError:
        return None


def dotted_date(value: str | None) -> str | None:
    """DD.MM.YYYY or DD-MM-YYYY ("09.02.2016") -> ISO ("2016-02-09"); anything else -> None."""
    m = re.fullmatch(r"\s*(\d{1,2})[.-](\d{1,2})[.-](\d{4})\s*", value or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def clean(fragment: str | None) -> str | None:
    """Inline HTML fragment -> one line of text; empty or "-" -> None."""
    if fragment is None:
        return None
    text = strip_tags(fragment).rstrip(" ,;")
    return text if text and text != "-" else None
