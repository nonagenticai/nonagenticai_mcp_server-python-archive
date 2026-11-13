"""Polish court judgments via SAOS (www.saos.org.pl/api): legal_saos_search, legal_saos_judgment."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client
from .html_text import html_to_text, strip_tags, truncate

CourtType = Literal[
    "COMMON", "SUPREME", "ADMINISTRATIVE", "CONSTITUTIONAL_TRIBUNAL", "NATIONAL_APPEAL_CHAMBER"
]


def _date(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as e:
        raise ToolError(f"[permanent] invalid {field}: expected YYYY-MM-DD") from e


def _court(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """(court name, division name). Common courts nest the court in the division; the other
    court types have no court object, so the court type stands in for the court."""
    division = raw.get("division") or {}
    court = (division.get("court") or {}).get("name") if isinstance(division, dict) else None
    division_name = division.get("name") if isinstance(division, dict) else None
    if not division_name and isinstance(raw.get("divisions"), list) and raw["divisions"]:
        division_name = raw["divisions"][0].get("name")
    return court or raw.get("courtType"), division_name


def _case_numbers(raw: dict[str, Any]) -> list[str]:
    return [c.get("caseNumber") for c in raw.get("courtCases", []) if c.get("caseNumber")]


@legal_mcp.tool()
async def saos_search(
    query: str,
    court_type: CourtType | None = None,
    case_number: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page_size: int = 10,
    page: int = 0,
) -> dict:
    """Full-text search of Polish court judgments (SAOS), newest first. Dates YYYY-MM-DD.
    Returns judgment ids for legal_saos_judgment."""
    if not query.strip() or len(query) > 500:
        raise ToolError("[permanent] query must be 1-500 chars")
    if case_number is not None and len(case_number) > 100:
        raise ToolError("[permanent] case_number too long (max 100 chars)")
    params = {
        "all": query,
        "courtType": court_type,
        "caseNumber": case_number,
        "judgmentDateFrom": _date("date_from", date_from),
        "judgmentDateTo": _date("date_to", date_to),
        "pageSize": max(1, min(page_size, 100)),
        "pageNumber": max(0, page),
        "sortingField": "JUDGMENT_DATE",
        "sortingDirection": "DESC",
    }
    r = await get_client("saos").get_json("/api/search/judgments", params)
    items = []
    for raw in r.get("items", []):
        court, division = _court(raw)
        items.append(
            {
                "id": raw.get("id"),
                "case_numbers": _case_numbers(raw),
                "judgment_date": raw.get("judgmentDate"),
                "judgment_type": raw.get("judgmentType"),
                "court": court,
                "division": division,
                "keywords": raw.get("keywords", []),
                "snippet": strip_tags(raw.get("textContent") or ""),
            }
        )
    return {"total": (r.get("info") or {}).get("totalResults"), "items": items}


@legal_mcp.tool()
async def saos_judgment(id: int, max_chars: int = 60000) -> dict:
    """One Polish court judgment from SAOS by numeric id: court, judges, legal bases,
    referenced regulations and the full text (plain)."""
    if isinstance(id, bool) or not isinstance(id, int) or not 0 < id < 10**10:
        raise ToolError(f"[permanent] invalid id: {id!r}")
    max_chars = max(500, min(max_chars, 300_000))
    r = await get_client("saos").get_json(f"/api/judgments/{id}", not_found="judgment not found")
    raw = r.get("data") or {}
    court, division = _court(raw)
    text, truncated = truncate(html_to_text(raw.get("textContent") or ""), max_chars)
    source = raw.get("source") or {}
    summary = raw.get("summary")
    return {
        "id": raw.get("id", id),
        "case_numbers": _case_numbers(raw),
        "judgment_date": raw.get("judgmentDate"),
        "judgment_type": raw.get("judgmentType"),
        "court": court,
        "division": division,
        "judges": [j.get("name") for j in raw.get("judges", []) if j.get("name")],
        "legal_bases": raw.get("legalBases", []),
        "referenced_regulations": [
            r.get("text") for r in raw.get("referencedRegulations", []) if r.get("text")
        ],
        "summary": strip_tags(summary) if summary else None,
        "text": text,
        "truncated": truncated,
        "url": source.get("judgmentUrl") or raw.get("href"),
    }
