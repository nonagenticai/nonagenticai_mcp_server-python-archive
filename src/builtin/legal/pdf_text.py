"""PDF -> plain text for Sejm consolidated texts (pypdf, pure Python), and article cutting.

Superscript article numbers ("Art. 61<sup>1</sup>.") are either separate, smaller, raised text
runs (older PDFs) or bracketed text "Art. 449[1]." (newer ones). For raised runs pypdf's plain
extract_text() glues them on ("Art. 611."), which is ambiguous with a real Art. 611, so the text
is rebuilt from pypdf's text visitor and a raised smaller run after a character is written as
"^1". Both forms end up as "Art. 61^1." / "Art. 449^1.".
"""

from __future__ import annotations

import io
import re

from pypdf import PdfReader

# Page furniture of Dziennik Ustaw / Kancelaria Sejmu PDFs.
_FURNITURE = [
    re.compile(r"^©\s*Kancelaria Sejmu\b.*$"),
    re.compile(r"^Dziennik Ustaw\s+[–-]\s*\d+\s*[–-]\s*Poz\.\s*\d+$"),
    re.compile(r"^Monitor Polski\s+[–-]\s*\d+\s*[–-]\s*Poz\.\s*\d+$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^s\.\s*\d+/\d+$"),
]
_HEADING = re.compile(
    r"^(?:Art\.\s*\d|DZIAŁ\b|Dział\b|ROZDZIAŁ\b|Rozdział\b|TYTUŁ\b|Tytuł\b|KSIĘGA\b|Księga\b)"
)


def _page_text(page) -> str:
    out: list[str] = []
    state = {"y": None, "size": None}

    def visit(text, cm, tm, font_dict, font_size) -> None:
        if not text:
            return
        size = abs(font_size * tm[3] * cm[3]) or font_size
        y = tm[5] * cm[3] + cm[5]
        last_y, body = state["y"], state["size"]
        raised = (
            last_y is not None
            and body is not None
            and 0.5 < y - last_y < 8
            and size < body * 0.8
            and text.strip()
        )
        if raised:
            if out and out[-1] and out[-1][-1:].isalnum():
                out.append("^")
            out.append(text)
            return  # a superscript does not move the baseline
        if last_y is not None and abs(y - last_y) > 1 and out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(text)
        state["y"] = y
        if text.strip():
            state["size"] = size

    page.extract_text(visitor_text=visit)
    return "".join(out)


def pdf_to_text(data: bytes, max_pages: int | None = None) -> str:
    """Plain text of the PDF; with `max_pages`, only that many pages are read and a closing line
    says where the text was cut."""
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    pages = (
        list(reader.pages)
        if max_pages is None
        else [reader.pages[i] for i in range(min(total, max_pages))]
    )
    lines: list[str] = []
    for page in pages:
        for raw in _page_text(page).split("\n"):
            line = re.sub(r"[ \t\xa0]+", " ", raw).strip()
            if not line or any(f.match(line) for f in _FURNITURE):
                continue
            lines.append(line)
    text = "\n".join(lines)
    # Newer Kancelaria Sejmu PDFs print superscripts as bracketed text: "Art. 449[1]." -> "449^1".
    text = re.sub(r"(\d)\^?\[(\d{1,2})\]", r"\1^\2", text)
    # Re-join words hyphenated across lines ("wycho-\nwania").
    text = re.sub(r"(\w)-\n(?=[a-ząćęłńóśźż])", r"\1", text)
    if len(pages) < total:
        text += f"\n[... text cut after page {len(pages)} of {total}]"
    return text


class AmbiguousArticleError(ValueError):
    """A flattened superscript heading ("Art. 431.") that may be Art. 43^1 or a real Art. 431."""


_ART_HEAD = re.compile(r"^Art\.\s*(\d+[a-z]*(?:\^\d+)?)\.(?:\s|$)")


def _article_end(lines: list[str], i: int) -> str:
    end = i + 1
    while end < len(lines) and not _HEADING.match(lines[end]):
        end += 1
    return "\n".join(lines[i:end])


def cut_article(text: str, suffix: str) -> str | None:
    """The article whose ELI suffix is `suffix` ("133", "133a", "449_1"), up to the next
    article or structural heading; None if absent.

    A superscript article ("43_1") is first looked up as "Art. 43^1.". Only if that is absent is
    the flattened form some PDFs give ("Art. 431.") accepted, and only when it is unambiguous:
    exactly one such heading, and the article heading before it is the base article ("Art. 43."
    for 43^1) or the previous superscript one ("Art. 43^1." / "Art. 431." for 43^2). Anything else
    raises AmbiguousArticleError instead of guessing between 43^1 and a real Art. 431.
    """
    number, _, sup = suffix.partition("_")
    lines = text.split("\n")
    heads = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _ART_HEAD.match(line))]
    exact = f"{number}^{sup}" if sup else number
    for i, label in heads:
        if label == exact:
            return _article_end(lines, i)
    if not sup:
        return None
    flat = f"{number}{sup}"
    hits = [k for k, (_, label) in enumerate(heads) if label == flat]
    if not hits:
        return None
    previous_ok = {number}
    if int(sup) > 1:
        previous_ok |= {f"{number}^{int(sup) - 1}", f"{number}{int(sup) - 1}"}
    if len(hits) != 1 or hits[0] == 0 or heads[hits[0] - 1][1] not in previous_ok:
        raise AmbiguousArticleError(flat)
    return _article_end(lines, heads[hits[0]][0])
