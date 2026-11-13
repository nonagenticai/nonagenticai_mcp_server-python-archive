"""EU law via the Publications Office CELLAR (SPARQL + content negotiation): legal_eurlex_act,
legal_eurlex_text. eur-lex.europa.eu HTML sits behind an AWS WAF challenge, so it is never fetched;
its URL is only returned as a citation link."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client
from .html_text import html_to_text, truncate

_CELEX = re.compile(r"[0-9CE][0-9]{4}[A-Z]{1,2}[0-9A-Z()_-]{1,20}")
_LANG = re.compile(r"[a-z]{3}")
SPARQL_PATH = "/webapi/rdf/sparql"
_LANG_URI = "http://publications.europa.eu/resource/authority/language/"

# The CELEX id is validated by _CELEX before it is placed in the literal (no quotes possible).
_QUERY = """PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?work ?date ?inforce ?eif ?eov ?title_pl ?title_en WHERE {
  ?work cdm:resource_legal_id_celex "%(celex)s"^^xsd:string .
  OPTIONAL { ?work cdm:work_date_document ?date }
  OPTIONAL { ?work cdm:resource_legal_in-force ?inforce }
  OPTIONAL { ?work cdm:resource_legal_date_entry-into-force ?eif }
  OPTIONAL { ?work cdm:resource_legal_date_end-of-validity ?eov }
  OPTIONAL { ?e1 cdm:expression_belongs_to_work ?work ;
                 cdm:expression_uses_language <%(lang)sPOL> ; cdm:expression_title ?title_pl }
  OPTIONAL { ?e2 cdm:expression_belongs_to_work ?work ;
                 cdm:expression_uses_language <%(lang)sENG> ; cdm:expression_title ?title_en }
} LIMIT 200"""


def celex_id(celex: object) -> str:
    if not isinstance(celex, str) or not _CELEX.fullmatch(celex):
        shown = repr(celex)
        raise ToolError(
            f"[permanent] invalid celex: {shown[:40]} (expected e.g. 32016R0679, 62014CJ0362)"
        )
    return celex


def _bool(value: str | None) -> bool | None:
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    return None


def _first(rows: list[dict[str, Any]], var: str) -> str | None:
    for row in rows:
        if var in row:
            return row[var].get("value")
    return None


@legal_mcp.tool()
async def eurlex_act(celex: str) -> dict:
    """Metadata of one EU act by CELEX number (e.g. "32016R0679" = GDPR): Polish and English
    title, document date, whether it is in force, entry-into-force dates, end of validity."""
    celex = celex_id(celex)
    query = _QUERY % {"celex": celex, "lang": _LANG_URI}
    resp = await get_client("cellar").request(
        "POST",
        SPARQL_PATH,
        data={"query": query},
        headers={"Accept": "application/sparql-results+json"},
    )
    try:
        rows = resp.json()["results"]["bindings"]
    except (ValueError, KeyError, TypeError) as e:
        raise ToolError("[retryable] unexpected SPARQL answer from CELLAR") from e
    if not rows:
        raise ToolError("[permanent] celex not found")
    eif = sorted({row["eif"]["value"] for row in rows if "eif" in row})
    return {
        "celex": celex,
        "title_pl": _first(rows, "title_pl"),
        "title_en": _first(rows, "title_en"),
        "date_document": _first(rows, "date"),
        "in_force": _bool(_first(rows, "inforce")),
        "entry_into_force": eif,
        "end_of_validity": _first(rows, "eov"),
        "text_url": f"https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX:{celex}",
    }


@legal_mcp.tool()
async def eurlex_text(celex: str, max_chars: int = 30000, lang: str = "pol") -> dict:
    """Plain text of an EU act by CELEX number, from CELLAR (lang: ISO 639-2 code, default
    "pol"; "eng" for English)."""
    celex = celex_id(celex)
    if not isinstance(lang, str) or not _LANG.fullmatch(lang):
        raise ToolError(f"[permanent] invalid lang: {lang!r} (expected e.g. pol, eng)")
    max_chars = max(500, min(max_chars, 200_000))
    resp = await get_client("cellar").request(
        "GET",
        f"/resource/celex/{quote(celex, safe='')}",
        headers={"Accept": "application/xhtml+xml", "Accept-Language": lang},
        follow_redirects=True,
        not_found=f"celex not found or no {lang} XHTML text",
    )
    if resp.status_code != 200 or not resp.text.strip():
        # 300 Multiple Choices: several manifestations, none negotiated.
        raise ToolError(
            f"[permanent] no single {lang} XHTML text for {celex} (status {resp.status_code})"
        )
    text, truncated = truncate(html_to_text(resp.text), max_chars)
    return {"celex": celex, "lang": lang, "text": text, "truncated": truncated}
