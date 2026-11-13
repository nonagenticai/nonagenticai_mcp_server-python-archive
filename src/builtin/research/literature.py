"""Scientific literature via discovery-engine: research_literature_search, research_literature_record.

discovery-engine (POST /api/search) fans one query out to public academic sources (PubMed,
Europe PMC, Crossref, Semantic Scholar, arXiv, ...), dedups, stores each hit in its corpus and
answers with that corpus id. GET /api/papers/{id} reads one stored record back. Only these two
read paths are used: no sessions, ingest or push (ingest/push target the retired KAG).

Every item carries its discovery-engine id (`id`), the upstream ids (`doi`, `pmid`, ...) and a
canonical `url`, and the abstract text a quote can be matched against.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastmcp.exceptions import ToolError

from ..legal.html_text import strip_tags, truncate
from ._server import call_json, research_mcp

# The adapters the live engine has enabled (GET /api/search/sources, 2026-10-23); openalex, core
# and google_books are registered there but not configured, so they would only ever fail.
Source = Literal["pubmed", "europe_pmc", "crossref", "semantic_scholar", "arxiv", "openlibrary"]
SortBy = Literal["relevance", "citation_count", "year"]

DE = "discovery_engine"
MAX_PER_SOURCE = 20


def _text(value: str | None) -> str | None:
    """Abstracts from Crossref / Europe PMC carry JATS/HTML markup; return plain text."""
    if not value:
        return None
    return strip_tags(value) if "<" in value else value.strip()


def source_url(external_ids: dict[str, Any], pdf_url: str | None = None) -> str | None:
    """Canonical public URL for a record: DOI, then PubMed, PMC, arXiv, then the PDF link."""
    ids = {k: str(v) for k, v in (external_ids or {}).items() if v}
    if doi := ids.get("doi"):
        return f"https://doi.org/{doi.removeprefix('https://doi.org/')}"
    if pmid := ids.get("pmid") or ids.get("pubmed_id"):
        return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if pmcid := ids.get("pmcid"):
        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
    if arxiv := ids.get("arxiv_id") or ids.get("arxiv"):
        return f"https://arxiv.org/abs/{arxiv}"
    return pdf_url or None


def _authors(raw: list[Any]) -> list[str]:
    out = []
    for a in raw or []:
        name = a.get("name") if isinstance(a, dict) else a
        if name:
            out.append(str(name))
    return out


def _year(field: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1000 <= value <= 2100:
        raise ToolError(f"[permanent] invalid {field}: expected a year 1000-2100")
    return value


def _paper_id(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as e:
        raise ToolError(f"[permanent] invalid id: {str(value)[:60]!r} (expected a uuid)") from e


@research_mcp.tool()
async def literature_search(
    query: str,
    sources: list[Source] | None = None,
    max_results_per_source: int = 3,
    year_min: int | None = None,
    year_max: int | None = None,
    open_access_only: bool = False,
    sort_by: SortBy = "relevance",
    abstract_max_chars: int = 1500,
) -> dict:
    """Search scientific literature (PubMed, Europe PMC, Crossref, Semantic Scholar, arXiv, ...)
    in one federated query. Each item has a stable `id` for research_literature_record, upstream
    ids (doi, pmid), a canonical `url` and the abstract. Default sources: all the engine has
    enabled; `sources_failed` lists any that errored or are not available. Can take ~10-60 s.
    Abstracts are capped (default 1500 chars) to keep the result small; quote from
    research_literature_record, which returns the full abstract."""
    if not query.strip() or len(query) > 1000:
        raise ToolError("[permanent] query must be 1-1000 chars")
    year_min, year_max = _year("year_min", year_min), _year("year_max", year_max)
    if year_min is not None and year_max is not None and year_min > year_max:
        raise ToolError("[permanent] year_min is after year_max")
    filters: dict[str, Any] = {"year_min": year_min, "year_max": year_max}
    if sources:
        filters["sources"] = list(dict.fromkeys(sources))
    body = {
        "query": query.strip(),
        "sources": {k: v for k, v in filters.items() if v is not None},
        "max_results_per_source": max(1, min(max_results_per_source, MAX_PER_SOURCE)),
        "open_access_only": open_access_only,
        "sort_by": sort_by,
    }
    abstract_max_chars = max(200, min(abstract_max_chars, 20_000))
    r = await call_json(DE, "POST", "/api/search", body=body)
    items = []
    for raw in r.get("results", []):
        ext = {k: str(v) for k, v in (raw.get("external_ids") or {}).items() if v}
        abstract, truncated = truncate(_text(raw.get("abstract")) or "", abstract_max_chars)
        items.append(
            {
                "id": raw.get("id") or raw.get("paper_id"),
                "title": raw.get("title"),
                "authors": _authors(raw.get("authors", [])),
                "year": raw.get("year"),
                "venue": raw.get("venue"),
                "doi": ext.get("doi"),
                "pmid": ext.get("pmid") or ext.get("pubmed_id"),
                "external_ids": ext,
                "url": source_url(ext, raw.get("pdf_url")),
                "pdf_url": raw.get("pdf_url"),
                "source": raw.get("source"),
                "citation_count": raw.get("citation_count"),
                "abstract": abstract or None,
                "abstract_truncated": truncated,
            }
        )
    return {
        "query": r.get("query", body["query"]),
        "total": r.get("total_results", len(items)),
        "sources_queried": r.get("sources_queried", []),
        "sources_failed": r.get("sources_failed", []),
        "items": items,
    }


@research_mcp.tool()
async def literature_record(id: str) -> dict:
    """One literature record by the `id` research_literature_search returned: title, authors
    (with affiliations), year, venue, upstream ids, canonical url and the full abstract."""
    paper_id = _paper_id(id)
    raw = await call_json(DE, "GET", f"/api/papers/{paper_id}", not_found="paper not found")
    ext = {k: str(v) for k, v in (raw.get("external_ids") or {}).items() if v}
    authors = [
        {"name": a.get("name"), "affiliation": a.get("affiliation")}
        for a in raw.get("authors", [])
        if isinstance(a, dict) and a.get("name")
    ]
    return {
        "id": raw.get("id", paper_id),
        "title": raw.get("title"),
        "authors": authors,
        "year": raw.get("year"),
        "venue": raw.get("venue"),
        "doi": ext.get("doi"),
        "pmid": ext.get("pmid") or ext.get("pubmed_id"),
        "external_ids": ext,
        "url": source_url(ext, raw.get("pdf_url")),
        "pdf_url": raw.get("pdf_url"),
        "is_open_access": raw.get("is_open_access"),
        "citation_count": raw.get("citation_count"),
        "abstract": _text(raw.get("abstract")),
    }
