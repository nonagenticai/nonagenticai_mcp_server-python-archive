"""HTML -> plain text with the stdlib parser (no lxml / selectolax in this image).

Used for ELI act texts, SAOS judgment bodies and CELLAR XHTML. Block elements become line breaks,
<sup> becomes "^" (so "Art. 449<sup>1</sup>" reads "Art. 449^1"), script/style/head are dropped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser

_BLOCK = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "blockquote",
        "hr",
        "dd",
        "dt",
    }
)
_CELL = frozenset({"td", "th"})
_SKIP = frozenset({"script", "style", "head", "title", "noscript"})
# Elements that never get an end tag in HTML; they must not change the nesting depth.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

Predicate = Callable[[str, dict[str, str]], bool]


class _TextParser(HTMLParser):
    """Collect text; with `match`, only inside the first element the predicate selects."""

    def __init__(self, match: Predicate | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.match = match
        self.parts: list[str] = []
        self.skip_depth = 0
        # depth of open elements inside the matched element; None = not (yet) inside
        self.inside: int | None = None if match else 0
        self.found = match is None
        self.done = False

    def _active(self) -> bool:
        return self.inside is not None and not self.done and self.skip_depth == 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if self.inside is None:
            assert self.match is not None
            if tag not in _VOID and self.match(tag, {k: v or "" for k, v in attrs}):
                self.inside = 1
                self.found = True
            return
        if tag not in _VOID and self.match is not None:
            self.inside += 1
        if tag in _SKIP:
            self.skip_depth += 1
            return
        if not self._active():
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _CELL:
            self.parts.append(" ")
        elif tag == "sup":
            self.parts.append("^")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/>, <hr/>: never opens an element.
        if self._active() and tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.done or self.inside is None:
            return
        if tag in _SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif self._active() and tag in _BLOCK:
            self.parts.append("\n")
        if self.match is not None and tag not in _VOID:
            self.inside -= 1
            if self.inside == 0:
                self.done = True

    def handle_data(self, data: str) -> None:
        if self._active():
            self.parts.append(data)


def _normalise(parts: list[str]) -> str:
    text = "".join(parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    out = "\n".join(line for line in lines if line)
    return out.replace("^ ", "^")


def html_to_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return _normalise(parser.parts)


def element_text(html: str, match: Predicate) -> str | None:
    """Plain text of the first element `match(tag, attrs)` selects, or None if there is none."""
    parser = _TextParser(match)
    parser.feed(html)
    parser.close()
    return _normalise(parser.parts) if parser.found else None


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def strip_tags(fragment: str) -> str:
    """Inline snippets (SAOS <em> highlights): tags removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", html_to_text(fragment).replace("\n", " ")).strip()
