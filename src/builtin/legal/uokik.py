"""Decisions of the President of UOKiK from decyzje.uokik.gov.pl (a Lotus Domino database with no
API): legal_uokik_search, legal_uokik_document.

The site's search form redirects to a Domino full-text SearchView on the WWW-decyzje view; this
module builds that same GET. A decision is the document /bp/dec_prez.nsf/1/{UNID}?editDocument&
act=Decyzja (the link the site itself uses; it renders read-only for anonymous visitors), whose
metadata table links the decision text as a PDF attachment.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote

from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client
from .scrape_util import (
    DEFAULT_MAX_CHARS,
    MAX_PDF_BYTES,
    cached,
    check_text,
    clamp_max_chars,
    clamp_page,
    clean,
    dotted_date,
    fetch_pdf,
    finish,
    pdf_text,
    remember,
)

_S = re.S | re.I
_UNID = re.compile(r"[0-9A-Fa-f]{32}")
MAX_PDFS = 3  # attachments read per decision
# All attachments of one decision together (each one is also capped at MAX_PDF_BYTES).
MAX_DECISION_PDF_BYTES = 25 * 1024 * 1024
# Domino query syntax characters a value must not smuggle in (field refs, grouping, quotes).
_QUERY_META = re.compile(r'[\[\]()"=<>]')


def _value(text: str) -> str:
    return _QUERY_META.sub(" ", text).strip()


def _doc_path(unid: str) -> str:
    return f"/bp/dec_prez.nsf/1/{unid}?editDocument&act=Decyzja"


def _parse_search(page: str) -> list[dict]:
    items = []
    for row in re.split(r"<TR\s*>", page, flags=re.I)[1:]:
        link = re.search(
            r"HREF=\"?/bp/dec_prez\.nsf/1/([0-9A-F]{32})\?editDocument[^>]*>(.*?)</a>", row, _S
        )
        if not link:
            continue
        when = re.search(r"Data decyzji:\s*</b>\s*([\d.]+)", row, _S)
        parties = re.search(r"Data decyzji:\s*</b>\s*[\d.]+\s*<br>(.*?)<br>\s*<b>Rodzaj", row, _S)
        practice = re.search(r"Rodzaj praktyki:\s*</b>(.*?)</text>", row, _S)
        items.append(
            {
                "unid": link.group(1).upper(),
                "decision_number": clean(link.group(2)),
                "date": dotted_date(when.group(1)) if when else None,
                "participants": clean(parties.group(1)) if parties else None,
                "practice": clean(practice.group(1)) if practice else None,
            }
        )
    return items


@legal_mcp.tool()
async def uokik_search(
    query: str | None = None,
    decision_number: str | None = None,
    signature: str | None = None,
    participant: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Search decisions of the President of UOKiK (competition and consumer protection) on
    decyzje.uokik.gov.pl: full-text words, decision number ("DDK-1/2016"), case signature or a
    participant (company) name. Returns unids for legal_uokik_document."""
    words = check_text("query", query, max_len=300)
    number = check_text("decision_number", decision_number, max_len=60)
    sign = check_text("signature", signature, max_len=60)
    party = check_text("participant", participant, max_len=60)
    terms = ["([FORM]=Decyzja)"]
    if number and _value(number):
        terms.append(f'([decyzja_nr] = "{_value(number)}")')
    if sign and _value(sign):
        terms.append(f'([Sygnatura] contains "{_value(sign)}")')
    if party and _value(party):
        terms.append(f'([Uczestnicy_postepowania] contains "{_value(party)}")')
    if words and _value(words):
        terms.append(f"({_value(words)})")
    if len(terms) == 1:
        raise ToolError("[permanent] give a query, decision_number, signature or participant")
    page, page_size = clamp_page(page, page_size)
    path = (
        f"/bp/dec_prez.nsf/WWW-decyzje?SearchView&Query={quote(' AND '.join(terms), safe='')}"
        f"&SearchOrder=3&SearchMax=0&Start={(page - 1) * page_size + 1}&Count={page_size}"
    )
    client = get_client("uokik")
    items = _parse_search(await client.get_text(path))
    for item in items:
        item["url"] = client.url(_doc_path(item["unid"]))
    # Domino reports only the hits on this page, not a total.
    return {"page": page, "items": items, "has_more": len(items) == page_size}


def _us_date(value: str | None) -> str | None:
    """The document page prints dates as MM/DD/YYYY (the list shows the same day DD.MM.YYYY)."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def _parse_decision(page: str) -> dict:
    fields: dict[str, str] = {}
    for m in re.finditer(
        r"<tr[^>]*>\s*<td[^>]*>\s*<b>(.*?):?</b>\s*</td>\s*<td[^>]*>(.*?)</td>", page, _S
    ):
        fields[clean(m.group(1)) or ""] = m.group(2)
    pdfs = re.findall(
        r'href="(/bp/dec_prez\.nsf/[^"]*/\$FILE/[^"]+\.pdf)"', fields.get("Decyzja", ""), _S
    )
    related = [
        {"signature": clean(sig), "date": dotted_date(when), "unid": unid.upper()}
        for unid, sig, when in re.findall(
            r'HREF="/bp/wyroki\.nsf/0/([0-9A-F]{32})\?editDocument&act=Wyrok">(.*?)</a>'
            r"\s*<BR>\s*<b>Data wyroku:\s*</B>\s*([\d.]+)",
            page,
            _S,
        )
    ]
    return {
        "decision_number": clean(fields.get("Numer decyzji")),
        "date": _us_date(fields.get("Data wydania decyzji")),
        "case_signature": clean(fields.get("Sygnatura akt")),
        "participants": clean(fields.get("Uczestnicy postępowania")),
        "practice": clean(fields.get("Rodzaj praktyki")),
        "penalty": clean(fields.get("Kara")),
        "sector": clean(fields.get("Branża")),
        "region": clean(fields.get("Region")),
        "appealed": clean(fields.get("Odwołanie do sądu")),
        "related_judgments": related,
        "pdf_paths": pdfs[:MAX_PDFS],
    }


@legal_mcp.tool()
async def uokik_document(unid: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """One UOKiK President's decision by the unid from legal_uokik_search: number, date, case
    signature, participants, practice, penalty, appeal, related court judgments and the decision
    text (plain, from the published PDF). Cite `url`."""
    if not isinstance(unid, str) or not _UNID.fullmatch(unid):
        raise ToolError("[permanent] invalid unid: expected 32 hex characters")
    unid = unid.upper()
    max_chars = clamp_max_chars(max_chars)
    key = f"uokik:{unid}"
    doc = cached(key)
    hit = doc is not None
    if doc is None:
        client = get_client("uokik")
        meta = _parse_decision(
            await client.get_text(_doc_path(unid), not_found="decision not found")
        )
        if meta["decision_number"] is None:
            raise ToolError("[permanent] decision not found (not a decision document)")
        paths = meta.pop("pdf_paths")
        texts = []
        budget = MAX_DECISION_PDF_BYTES
        for p in paths:
            if budget <= 0:
                raise ToolError(
                    f"[permanent] decision PDFs too large in total (max {MAX_DECISION_PDF_BYTES})"
                )
            data = await fetch_pdf(
                client, p, not_found="decision PDF not found", max_bytes=min(MAX_PDF_BYTES, budget)
            )
            budget -= len(data)
            texts.append(await pdf_text(data))
        doc = {
            "unid": unid,
            "body": "Prezes Urzędu Ochrony Konkurencji i Konsumentów",
            **meta,
            "text": "\n\n".join(texts),
            "url": client.url(_doc_path(unid)),
            "pdf_urls": [client.url(p) for p in paths],
        }
        remember(key, doc)
    return finish(doc, max_chars, hit)
