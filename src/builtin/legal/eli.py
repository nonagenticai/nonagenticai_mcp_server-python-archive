"""Polish legislation via the Sejm ELI API (api.sejm.gov.pl/eli): legal_eli_search, legal_eli_act,
legal_eli_act_text."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from cachetools import TTLCache
from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client
from .html_text import element_text, html_to_text, truncate
from .pdf_text import AmbiguousArticleError, cut_article, pdf_to_text

_ELI = re.compile(r"(DU|MP)/(\d{4})/(\d{1,5})")
# "133", "133a", "133^1", "133(1)", "133_1", "133¹" -> ELI html id suffix "133", "133a", "133_1"
_ARTICLE = re.compile(
    r"(\d{1,5})([a-z]{0,3})(?:\^(\d{1,2})|\((\d{1,2})\)|_(\d{1,2})|([¹²³⁴⁵⁶⁷⁸⁹⁰]{1,2}))?"
)
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
MAX_REFS_PER_GROUP = 50


def _in_force(value: object) -> bool | None:
    if value == "IN_FORCE":
        return True
    if value == "NOT_IN_FORCE":
        return False
    return None


def _shown(value: object) -> str:
    s = repr(value)
    return s if len(s) <= 40 else s[:40] + "..."


def eli_path(eli: object) -> str:
    """Validate an ELI id ("DU/1964/59") and return its URL path ("/eli/acts/DU/1964/59")."""
    if not isinstance(eli, str) or not _ELI.fullmatch(eli):
        raise ToolError(
            f"[permanent] invalid eli: {_shown(eli)} (expected PUBLISHER/YEAR/POSITION, "
            "e.g. DU/1964/59; publisher DU or MP)"
        )
    return f"/eli/acts/{eli}"


def article_id(article: object) -> str:
    """Normalise a caller's article number to the ELI html id suffix after "arti_"."""
    if not isinstance(article, str):
        raise ToolError(f"[permanent] invalid article: {_shown(article)}")
    m = _ARTICLE.fullmatch(article.strip().lower().removeprefix("art.").strip())
    if not m:
        raise ToolError(
            f"[permanent] invalid article: {_shown(article)} (expected e.g. 133, 133a, 133^1)"
        )
    number, letters, sup1, sup2, sup3, sup4 = m.groups()
    sup = sup1 or sup2 or sup3 or (sup4.translate(_SUPERSCRIPT) if sup4 else None)
    return f"{number}{letters}" + (f"_{sup}" if sup else "")


def _item(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "eli": raw.get("ELI"),
        "title": raw.get("title"),
        "address": raw.get("displayAddress"),
        "type": raw.get("type"),
        "status": raw.get("status"),
        "in_force": _in_force(raw.get("inForce")),
        "promulgation": raw.get("promulgation"),
    }


@legal_mcp.tool()
async def eli_search(
    title: str | None = None,
    keyword: str | None = None,
    year: int | None = None,
    type: str | None = None,
    in_force_only: bool = True,
    publisher: Literal["DU", "MP"] = "DU",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search Polish legal acts (Dziennik Ustaw / Monitor Polski) in the Sejm ELI database.

    title: words of the act title (e.g. "kodeks rodzinny"); keyword: ELI subject keyword;
    type: act type such as "Ustawa", "Rozporządzenie", "Obwieszczenie". Returns ELI ids
    (e.g. "DU/1964/59") to pass to legal_eli_act / legal_eli_act_text.
    """
    if year is not None and not 1918 <= year <= 2100:
        raise ToolError(f"[permanent] invalid year: {year}")
    for name, value in (("title", title), ("keyword", keyword), ("type", type)):
        if value is not None and len(value) > 200:
            raise ToolError(f"[permanent] {name} too long (max 200 chars)")
    params = {
        "publisher": publisher,
        "title": title,
        "keyword": keyword,
        "year": year,
        "type": type,
        "inForce": 1 if in_force_only else None,
        "limit": max(1, min(limit, 100)),
        "offset": max(0, offset),
    }
    r = await get_client("eli").get_json("/eli/acts/search", params)
    return {
        "total": r.get("totalCount", r.get("count")),
        "items": [_item(i) for i in r.get("items", [])],
    }


def _refs_from_endpoint(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for group, entries in raw.items():
        out[group] = [
            {
                "eli": (e.get("act") or {}).get("ELI"),
                "title": (e.get("act") or {}).get("title"),
                "date": e.get("date"),
            }
            for e in entries[:MAX_REFS_PER_GROUP]
        ]
    return out


def _refs_from_act(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        group: [
            {"eli": e.get("id"), "title": None, "date": e.get("date")}
            for e in entries[:MAX_REFS_PER_GROUP]
        ]
        for group, entries in raw.items()
    }


@legal_mcp.tool()
async def eli_act(eli: str) -> dict:
    """Metadata of one Polish act by ELI id (e.g. "DU/1964/59"): status, whether it is in force,
    dates, keywords, and related acts (amending acts, consolidated texts "Inf. o tekście
    jednolitym", Constitutional Tribunal rulings, ...)."""
    path = eli_path(eli)
    client = get_client("eli")
    act, refs = await asyncio.gather(
        client.get_json(path, not_found="act not found"),
        client.get_json(f"{path}/references", not_found="act not found"),
        return_exceptions=True,
    )
    if isinstance(act, BaseException):
        raise act
    # The references endpoint adds titles; the act's own reference list (ids only) is the fallback.
    references = (
        _refs_from_endpoint(refs)
        if isinstance(refs, dict)
        else _refs_from_act(act.get("references") or {})
    )
    has_html = bool(act.get("textHTML"))
    return {
        "eli": act.get("ELI", eli),
        "title": act.get("title"),
        "address": act.get("displayAddress"),
        "type": act.get("type"),
        "status": act.get("status"),
        "in_force": _in_force(act.get("inForce")),
        "entry_into_force": act.get("entryIntoForce"),
        "valid_from": act.get("validFrom"),
        "change_date": act.get("changeDate"),
        "has_html": has_html,
        "keywords": act.get("keywords", []),
        "references": references,
        "text_url_html": client.url(f"{path}/text.html") if has_html else None,
        "text_url_pdf": client.url(f"{path}/text.pdf") if act.get("textPDF") else None,
    }


TJ_GROUP = "Inf. o tekście jednolitym"
AMENDING_GROUP = "Akty zmieniające"
MAX_PDF_BYTES = 40 * 1024 * 1024
_PDF_FILE = re.compile(r"[A-Za-z0-9_.-]{1,64}\.pdf")
# Parsed consolidated PDFs (seconds of CPU each); they never change once published.
_pdf_cache: TTLCache = TTLCache(maxsize=8, ttl=24 * 3600)


def clear_pdf_cache() -> None:
    _pdf_cache.clear()


def _article_from_html(html: str, suffix: str) -> str | None:
    # ids look like "titl_II-bran_III-arti_133"; its § units are "...-arti_133-para_1".
    wanted = f"arti_{suffix}"

    def is_article(tag: str, attrs: dict[str, str]) -> bool:
        unit_id = attrs.get("id", "")
        return unit_id == wanted or unit_id.endswith(f"-{wanted}")

    return element_text(html, is_article)


def _newest(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    acts = [e.get("act") or {} for e in entries]
    acts = [a for a in acts if isinstance(a.get("ELI"), str) and _ELI.fullmatch(a["ELI"])]
    if not acts:
        return None
    return max(
        acts, key=lambda a: (a.get("promulgation") or a.get("announcementDate") or "", a["ELI"])
    )


def _previous_promulgation(entries: list[dict[str, Any]], newest: dict[str, Any]) -> str | None:
    """Promulgation date of the consolidation before `newest`; None if there is none."""
    mine = newest.get("promulgation") or ""
    earlier = [
        p
        for p in ((e.get("act") or {}).get("promulgation") for e in entries)
        if isinstance(p, str) and p and p < mine
    ]
    return max(earlier) if earlier else None


def _pdf_path(source_path: str, meta: dict[str, Any]) -> str:
    """The consolidated PDF: texts[] type T (Kancelaria Sejmu text), then U, else text.pdf."""
    files = {t.get("type"): t.get("fileName") for t in meta.get("texts") or []}
    for kind in ("T", "U"):
        name = files.get(kind)
        if isinstance(name, str) and _PDF_FILE.fullmatch(name):
            return f"{source_path}/text/{kind}/{name}"
    return f"{source_path}/text.pdf"


async def _pdf_text(path: str) -> str:
    cached = _pdf_cache.get(path)
    if cached is not None:
        return cached
    resp = await get_client("eli").request("GET", path, not_found="consolidated PDF not found")
    if len(resp.content) > MAX_PDF_BYTES:
        raise ToolError(f"[permanent] consolidated PDF too large ({len(resp.content)} bytes)")
    try:
        text = await asyncio.to_thread(pdf_to_text, resp.content)
    except Exception as e:  # pypdf raises many types on a damaged file
        raise ToolError(f"[permanent] cannot read the PDF text: {e.__class__.__name__}") from e
    _pdf_cache[path] = text
    return text


async def _original(eli: str, path: str, suffix: str | None, article: str | None) -> str:
    client = get_client("eli")
    html = await client.get_text(f"{path}/text.html", not_found="act not found")
    if not html.strip():
        raise ToolError(f"[permanent] no HTML text (PDF only): {client.url(f'{path}/text.pdf')}")
    if suffix is None:
        return html_to_text(html)
    found = _article_from_html(html, suffix)
    if found is None:
        raise ToolError(f"[permanent] article {article} not found in the HTML text of {eli}")
    return found


async def _current(eli: str, path: str, suffix: str | None, article: str | None) -> dict:
    client = get_client("eli")
    refs = await client.get_json(f"{path}/references", not_found="act not found")
    newest = _newest(refs.get(TJ_GROUP) or [])
    source_eli = newest["ELI"] if newest else eli
    source_path = eli_path(source_eli)
    meta = await client.get_json(source_path, not_found="act not found")
    # A consolidated text covers amendments published before its cut-off date (legalStatusDate,
    # "ogłoszonych przed dniem ..."); later ones are not in it. Without legalStatusDate the cut-off
    # is inferred conservatively (over-report, never under-report): the previous consolidation's
    # promulgation, else no cut-off at all (every amending act is listed).
    inferred = False
    consolidation_date: str | None = None
    if newest:
        consolidation_date = meta.get("legalStatusDate")
        if not consolidation_date:
            inferred = True
            consolidation_date = _previous_promulgation(refs.get(TJ_GROUP) or [], newest)
    amended = []
    for a in ((e.get("act") or {}) for e in refs.get(AMENDING_GROUP) or []):
        if not isinstance(a.get("ELI"), str):
            continue
        promulgation = a.get("promulgation")
        if not promulgation:
            # A DU/MP position is published in its ELI year, so an act from a year before the
            # cut-off's year is provably covered. Otherwise it cannot be proven: list it.
            year = a["ELI"].split("/")[1] if _ELI.fullmatch(a["ELI"]) else ""
            if consolidation_date is not None and year and year < consolidation_date[:4]:
                continue
            amended.append({"eli": a["ELI"], "promulgation": None, "date_unknown": True})
        elif consolidation_date is None or promulgation >= consolidation_date:
            amended.append({"eli": a["ELI"], "promulgation": promulgation, "date_unknown": False})
    amended.sort(key=lambda a: (a["promulgation"] or "9999", a["eli"]))
    html = ""
    if meta.get("textHTML"):
        html = await client.get_text(f"{source_path}/text.html", not_found="act not found")
    if html.strip():
        source_format = "html"
        if suffix is None:
            text = html_to_text(html)
        else:
            text = _article_from_html(html, suffix)
    else:
        source_format = "pdf"
        full = await _pdf_text(_pdf_path(source_path, meta))
        try:
            text = full if suffix is None else cut_article(full, suffix)
        except AmbiguousArticleError as e:
            raise ToolError(
                f"[permanent] article ambiguous: {article} -- {source_eli} prints it as "
                f"'Art. {e}.' which could also be a real article {e}; check the PDF"
            ) from e
    if text is None:
        raise ToolError(
            f"[permanent] article {article} not found in {source_eli} ({source_format})"
        )
    return {
        "text": text,
        "source_eli": source_eli,
        "source_format": source_format,
        "consolidation_date": consolidation_date,
        "consolidation_date_inferred": inferred,
        "amended_after_consolidation": amended,
    }


@legal_mcp.tool()
async def eli_act_text(
    eli: str,
    article: str | None = None,
    wording: Literal["current", "original"] = "current",
    max_chars: int = 30000,
) -> dict:
    """Text of a Polish act by ELI id; with `article` ("133", "133a", "133^1") only that article.

    wording="current" (default): the newest consolidated text ("tekst jednolity") of the act,
    HTML or else its PDF. Amendments published after that consolidation are NOT in it; they are
    listed in amended_after_consolidation ({eli, promulgation, date_unknown}) -- tell the reader to
    check them. consolidation_date_inferred=true means the cut-off was not published and a
    conservative earlier date was used, so the list may over-report. Without any
    consolidation the act's own text is used and every amending act is listed.
    wording="original": the act's HTML text as originally published.
    """
    path = eli_path(eli)
    suffix = article_id(article) if article is not None else None
    max_chars = max(500, min(max_chars, 200_000))
    if wording == "original":
        out = {
            "text": await _original(eli, path, suffix, article),
            "source_eli": eli,
            "source_format": "html",
            "consolidation_date": None,
            "consolidation_date_inferred": False,
            "amended_after_consolidation": None,
        }
    else:
        out = await _current(eli, path, suffix, article)
    text, truncated = truncate(str(out.pop("text")), max_chars)
    return {
        "eli": eli,
        "article": article,
        "wording": wording,
        **out,
        "text": text,
        "truncated": truncated,
    }
