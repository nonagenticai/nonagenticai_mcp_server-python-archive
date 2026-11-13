"""Dziennik Urzędowy Województwa Dolnośląskiego (edzienniki.duw.pl): legal_duw_search,
legal_duw_document.

There is no published API; the site is an AngularJS front-end over its own JSON endpoints, which
these tools call the way the page does: POST /api/SearchLegalAct (title search; the server always
answers with up to 500 positions, newest first, whatever page size is asked, so the answer is
cached briefly and paged here), GET /api/legalact?year&journal=0&position (one position) and
GET /GetActPdf.ashx (the published PDF, the authoritative text). The canonical, citable URL is the
position page /legalact/{year}/{position}/.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from cachetools import TTLCache
from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client
from .scrape_util import (
    DEFAULT_MAX_CHARS,
    cached,
    check_text,
    clamp_max_chars,
    clamp_page,
    fetch_pdf_text,
    finish,
    remember,
)

JOURNAL = "Dziennik Urzędowy Województwa Dolnośląskiego"
FIRST_YEAR = 1999  # the voivodeship (and its journal) exists since 1999
# (title, year) -> the server's whole answer; it is ~0.5 MB, so paging must not refetch it.
_search_cache: TTLCache = TTLCache(maxsize=32, ttl=3600)


def clear_search_cache() -> None:
    _search_cache.clear()


def _year(year: int | None) -> int | None:
    if year is None:
        return None
    if isinstance(year, bool) or not isinstance(year, int) or not FIRST_YEAR <= year <= 2100:
        raise ToolError(f"[permanent] invalid year: {year!r}")
    return year


def _day(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _position_url(year: Any, position: Any) -> str:
    return get_client("duw").url(f"/legalact/{year}/{position}/")


@legal_mcp.tool()
async def duw_search(
    query: str,
    year: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Search the official journal of the Dolnośląskie voivodeship (local law: municipal and
    county resolutions, voivode orders) by words of the act title, optionally one year. Newest
    first. Returns (year, position) for legal_duw_document."""
    title = check_text("query", query, max_len=200)
    if title is None:
        raise ToolError("[permanent] query must be 1-200 chars")
    year = _year(year)
    page, page_size = clamp_page(page, page_size)
    key = (title.lower(), year)
    answer = _search_cache.get(key)
    if answer is None:
        body: dict[str, Any] = {
            "Title": title,
            "PageNumber": 1,
            "SelectedKeywords": [],
            "SelectedLegalActType": [],
            "PublisherName": "",
        }
        if year is not None:
            body["Year"] = year
        resp = await get_client("duw").request(
            "POST", "/api/SearchLegalAct", json=body, headers={"Accept": "application/json"}
        )
        try:
            answer = resp.json()
        except ValueError as e:
            raise ToolError("[retryable] non-JSON answer from POST /api/SearchLegalAct") from e
        _search_cache[key] = answer
    acts = [a for a in answer.get("LegalActs") or [] if isinstance(a, dict)]
    window = acts[(page - 1) * page_size : page * page_size]
    items = [
        {
            "year": a.get("Year"),
            "position": a.get("Position"),
            "title": a.get("Title"),
            "act_type": a.get("LegalActType"),
            "number": a.get("CaseNumber"),
            "publisher": a.get("Publisher"),
            "act_date": _day(a.get("ActDate")),
            "publication_date": _day(a.get("PublicationDate")),
            "url": _position_url(a.get("Year"), a.get("Position")),
        }
        for a in window
    ]
    return {
        "total": answer.get("TotalItems"),
        "returned_by_source": len(acts),
        "page": page,
        "items": items,
        "has_more": page * page_size < len(acts),
    }


@legal_mcp.tool()
async def duw_document(year: int, position: int, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """One position of the Dolnośląskie official journal by (year, position): title, act type
    and number, issuing body, act and publication dates, keywords, status and the act text
    (plain, from the published PDF). Cite `url`."""
    _year(year)
    if isinstance(position, bool) or not isinstance(position, int) or not 0 < position < 10**6:
        raise ToolError(f"[permanent] invalid position: {position!r}")
    max_chars = clamp_max_chars(max_chars)
    key = f"duw:{year}:{position}"
    doc = cached(key)
    hit = doc is not None
    if doc is None:
        client = get_client("duw")
        meta = await client.get_json(
            "/api/legalact",
            {"year": year, "journal": 0, "position": position},
            not_found="journal position not found",
        )
        if not isinstance(meta, dict):
            raise ToolError("[retryable] unexpected answer from GET /api/legalact")
        pdf_path = f"/GetActPdf.ashx?year={year}&book=0&position={position}"
        text = await fetch_pdf_text(client, pdf_path, not_found="act PDF not found")
        status = meta.get("ActStatus") or {}
        publishers = [p.get("Name") for p in meta.get("Publishers") or [] if p.get("Name")]
        doc = {
            "year": year,
            "position": position,
            "journal": JOURNAL,
            "position_number": meta.get("ActPositionNumber"),
            "title": meta.get("Title"),
            "act_type": meta.get("LegalActType"),
            "number": meta.get("CaseNumber"),
            "publisher": publishers[0] if publishers else meta.get("PublisherGenetive"),
            "act_date": _day(meta.get("ActDate")),
            "publication_date": _day(meta.get("PublicationDate")),
            "keywords": [k.get("Name") for k in meta.get("Keywords") or [] if k.get("Name")],
            "invalid": bool(status.get("IsInvalid")),
            "partially_invalid": bool(status.get("IsPartialInvalid")),
            "status_note": status.get("Description") or None,
            "text": text,
            "url": _position_url(year, position),
            "pdf_url": client.url(pdf_path),
        }
        remember(key, doc)
    return finish(doc, max_chars, hit)
