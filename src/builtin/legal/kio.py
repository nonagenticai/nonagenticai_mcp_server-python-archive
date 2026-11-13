"""KIO (Krajowa Izba Odwoławcza) rulings from the UZP search site orzeczenia.uzp.gov.pl, which has
no API: legal_kio_search, legal_kio_document.

The site's own search form posts to /Home/GetResults (an HTML fragment, 10 hits per page); a
ruling is /Home/Details/{id} (metadata) plus /Home/ContentHtml/{id}?Kind=KIO (the text the
details page shows in an iframe). The canonical, citable URL is the details page.
"""

from __future__ import annotations

import re
from typing import Literal

from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client
from .html_text import html_to_text
from .scrape_util import (
    DEFAULT_MAX_CHARS,
    cached,
    check_text,
    clamp_max_chars,
    clean,
    dotted_date,
    finish,
    polish_date,
    remember,
)

DocumentType = Literal["wyrok", "postanowienie", "uchwała"]
_S = re.S | re.I


def _field(block: str, label: str) -> str | None:
    m = re.search(rf"<label[^>]*>\s*{re.escape(label)}:?\s*</label>(.*?)</(?:p|div)>", block, _S)
    return clean(m.group(1)) if m else None


def _parse_results(page: str) -> tuple[int | None, list[dict]]:
    total_m = re.search(r"Liczba znalezionych dokument[^:]*:\s*(\d+)", page)
    items = []
    for block in page.split('<div class="search-list-item"')[1:]:
        id_m = re.search(r'href="/Home/Details/(\d+)"', block)
        if not id_m:
            continue
        fragment = re.search(r'<p class="fragment">(.*?)</p>', block, _S)
        items.append(
            {
                "id": int(id_m.group(1)),
                "signature": _field(block, "Sygnatura"),
                "document_type": _field(block, "Rodzaj dokumentu"),
                "date": dotted_date(_field(block, "Data wydania")),
                "body": _field(block, "Organ wydający"),
                "snippet": clean(fragment.group(1)) if fragment else None,
            }
        )
    return (int(total_m.group(1)) if total_m else None), items


@legal_mcp.tool()
async def kio_search(
    query: str | None = None,
    signature: str | None = None,
    document_type: DocumentType | None = None,
    page: int = 1,
) -> dict:
    """Search KIO (National Appeal Chamber, public procurement) rulings on orzeczenia.uzp.gov.pl
    by phrase (e.g. "rażąco niska cena", "art. 224 ust. 1") and/or signature ("KIO 2105/22").
    10 hits per page, most relevant first. Returns ids for legal_kio_document."""
    phrase = check_text("query", query, max_len=500)
    sign = check_text("signature", signature, max_len=100)
    if phrase is None and sign is None:
        raise ToolError("[permanent] give a query or a signature")
    form = {
        "Phrase": phrase,
        "Sign": sign,
        "Fle": "1",  # match inflected forms, as the site's form does by default
        "SCnt": "1",  # search the ruling text, not only the metadata
        "Kind": "KIO",
        "KIO.DecT": document_type,
        "Pg": str(max(1, page)),
        "CountStats": "True",
    }
    resp = await get_client("kio").request(
        "POST", "/Home/GetResults", data={k: v for k, v in form.items() if v}
    )
    total, items = _parse_results(resp.text)
    client = get_client("kio")
    for item in items:
        item["url"] = client.url(f"/Home/Details/{item['id']}")
    return {"total": total, "page": max(1, page), "items": items}


def _parse_details(page: str) -> dict:
    fields: dict[str, str | None] = {}
    # a label's value runs to the end of its <p>, never into the next label
    for m in re.finditer(r"<label[^>]*>([^<]*)</label>((?:(?!<label).)*?)</p>", page, _S):
        fields[clean(m.group(1)) or ""] = clean(m.group(2))
    outcomes = []
    ul = re.search(r"Sygnatura akt / Spos[^<]*</label>\s*<ul>(.*?)</ul>", page, _S)
    for li in re.findall(r"<li>(.*?)</li>", ul.group(1) if ul else "", _S):
        sig, _, outcome = (clean(li) or "").partition(" / ")
        outcomes.append({"signature": sig.strip(), "outcome": outcome.strip() or None})

    def links_after(heading: str) -> list[str]:
        m = re.search(rf"{heading}.*?<p[^>]*>(.*?)</p>", page, _S)
        return (
            [t for a in re.findall(r"<a[^>]*>(.*?)</a>", m.group(1), _S) if (t := clean(a))]
            if m
            else []
        )

    title = re.search(r'<h2 class="section-title"[^>]*>\s*([^<]+?)\s*<', page)
    return {
        "title_signature": title.group(1).strip() if title else None,
        "body": fields.get("Organ wydający"),
        "document_type": fields.get("Rodzaj dokumentu"),
        "date": fields.get("Data wydania rozstrzygnięcia"),
        "chairman": fields.get("Przewodniczący"),
        "contracting_authority": fields.get("Zamawiający"),
        "city": fields.get("Miejscowość"),
        "procedure": fields.get("Tryb postępowania"),
        "contract_type": fields.get("Rodzaj zamówienia"),
        "signatures": outcomes,
        "key_provisions": links_after("Kluczowe przepisy"),
        "topics": links_after("Zagadnienia merytoryczne"),
    }


@legal_mcp.tool()
async def kio_document(id: int, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """One KIO ruling by the id from legal_kio_search: signature(s) and outcome, date, body,
    contracting authority, key Pzp provisions and the full ruling text (plain). Cite `url`."""
    if isinstance(id, bool) or not isinstance(id, int) or not 0 < id < 10**9:
        raise ToolError(f"[permanent] invalid id: {id!r}")
    max_chars = clamp_max_chars(max_chars)
    key = f"kio:{id}"
    doc = cached(key)
    hit = doc is not None
    if doc is None:
        client = get_client("kio")
        details = await client.get_text(f"/Home/Details/{id}", not_found="ruling not found")
        meta = _parse_details(details)
        body = await client.get_text(
            f"/Home/ContentHtml/{id}?Kind=KIO", not_found="ruling not found"
        )
        text = html_to_text(body)
        signatures = meta.pop("signatures")
        title_signature = meta.pop("title_signature")
        doc = {
            "id": id,
            "signature": signatures[0]["signature"] if signatures else title_signature,
            "signatures": signatures,
            **meta,
            "date": dotted_date(meta["date"]) or polish_date(text[:1000]),
            "text": text,
            "url": client.url(f"/Home/Details/{id}"),
            "pdf_url": client.url(f"/Home/PdfContent/{id}?Kind=KIO"),
        }
        remember(key, doc)
    return finish(doc, max_chars, hit)
