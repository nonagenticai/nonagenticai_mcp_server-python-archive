"""legal_kio_* / legal_uokik_* / legal_duw_* against trimmed copies of real pages fetched from
orzeczenia.uzp.gov.pl, decyzje.uokik.gov.pl and edzienniki.duw.pl (tests/fixtures/legal), served by
httpx.MockTransport. The PDFs are page 1 of the real files with embedded font programs removed."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from fastmcp.exceptions import ToolError

from src.builtin.legal import client as legal_client
from src.builtin.legal._server import legal_mcp
from src.builtin.legal.client import USER_AGENT, PublicClient, get_client, set_client
from src.builtin.legal.duw import clear_search_cache
from src.builtin.legal.scrape_util import clear_doc_cache, dotted_date, polish_date
from tests.carebus_helpers import call_tool
from tests.legal_helpers import (
    FIXTURES,
    Routes,
    fixture_json,
    fixture_text,
    json_answer,
    text_answer,
)

SERVICES = ("kio", "uokik", "duw")


def _pdf(name: str) -> Callable[[httpx.Request], httpx.Response]:
    data = (FIXTURES / name).read_bytes()
    return lambda _req: httpx.Response(
        200, content=data, headers={"Content-Type": "application/pdf"}
    )


@pytest.fixture
def api() -> Iterator[Callable[[str, dict], Routes]]:
    def install(service: str, routes: dict) -> Routes:
        rec = Routes(routes)
        set_client(service, PublicClient(f"https://{service}.test", transport=rec.transport))
        return rec

    clear_doc_cache()
    clear_search_cache()
    yield install
    for service in SERVICES:
        set_client(service, None)
    clear_doc_cache()
    clear_search_cache()


def _call(tool: str, args: dict) -> dict:
    return call_tool(legal_mcp, tool, args)


def _form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


# --- KIO ---------------------------------------------------------------------------------------

KIO_DETAILS = "/Home/Details/18119"
KIO_CONTENT = "/Home/ContentHtml/18119"


def _kio_doc_routes() -> dict:
    return {
        ("GET", KIO_DETAILS): text_answer(200, fixture_text("kio_details_18119.html")),
        ("GET", KIO_CONTENT): text_answer(200, fixture_text("kio_content_18119.html")),
    }


def test_kio_search_posts_the_site_form_and_maps_hits(api):
    rec = api(
        "kio", {("POST", "/Home/GetResults"): text_answer(200, fixture_text("kio_results.html"))}
    )
    out = _call("kio_search", {"query": "rażąco niska cena", "document_type": "wyrok"})
    form = _form(rec.last)
    assert form["Phrase"] == "rażąco niska cena"
    assert form["Kind"] == "KIO" and form["Pg"] == "1" and form["KIO.DecT"] == "wyrok"
    assert "Sign" not in form
    assert rec.last.headers["User-Agent"] == USER_AGENT
    assert out["total"] == 8719
    first, second = out["items"]
    assert first["id"] == 20846 and first["signature"] == "KIO 3766/24"
    assert first["document_type"] == "wyrok"
    assert first["body"] == "Krajowa Izba Odwoławcza"
    assert first["date"] is None  # the site prints "-"
    assert "rażąco" in first["snippet"] and "<span" not in first["snippet"]
    assert first["url"] == "https://kio.test/Home/Details/20846"
    assert second["id"] == 18119 and second["signature"] == "KIO 2105/22"


def test_kio_search_normalises_a_listed_date(api):
    page = fixture_text("kio_results.html").replace(
        "<label>Data wydania:</label>\n        -",
        "<label>Data wydania:</label>\n        05-03-2026",
        1,
    )
    api("kio", {("POST", "/Home/GetResults"): text_answer(200, page)})
    dates = [i["date"] for i in _call("kio_search", {"query": "wadium"})["items"]]
    assert dates == ["2026-03-05", None]


def test_kio_search_by_signature_only(api):
    rec = api(
        "kio", {("POST", "/Home/GetResults"): text_answer(200, fixture_text("kio_results.html"))}
    )
    _call("kio_search", {"signature": "KIO 2105/22", "page": 2})
    form = _form(rec.last)
    assert form["Sign"] == "KIO 2105/22" and form["Pg"] == "2" and "Phrase" not in form


@pytest.mark.parametrize(
    "args", [{}, {"query": "  "}, {"query": "x" * 501}, {"signature": "x" * 101}]
)
def test_kio_search_rejects_bad_input_before_any_request(api, args):
    rec = api("kio", {})
    with pytest.raises(ToolError, match=r"^\[permanent\]"):
        _call("kio_search", args)
    assert rec.requests == []


def test_kio_search_5xx_is_retryable(api):
    api("kio", {("POST", "/Home/GetResults"): text_answer(503, "busy")})
    with pytest.raises(ToolError, match=r"^\[retryable\] 503"):
        _call("kio_search", {"query": "wadium"})


def test_kio_document_maps_metadata_and_text(api):
    rec = api("kio", _kio_doc_routes())
    out = _call("kio_document", {"id": 18119})
    assert out["signature"] == "KIO 2105/22"
    assert out["signatures"] == [{"signature": "KIO 2105/22", "outcome": "oddalone"}]
    assert out["date"] == "2022-08-25"  # the details page has none; taken from "z dnia ..."
    assert out["body"] == "Krajowa Izba Odwoławcza"
    assert out["document_type"] == "wyrok"
    assert out["chairman"] == "Magdalena Rams"
    assert out["contracting_authority"] == "Generalna Dyrekcja Dróg Krajowych i Autostrad"
    assert out["city"] == "Warszawa"
    assert out["procedure"] == "przetarg nieograniczony"
    assert out["contract_type"] is None
    assert "art. 224 ust. 1" in out["key_provisions"]
    assert "zasada uczciwej konkurencji" in out["topics"]
    assert out["text"].startswith("Sygn. akt: KIO 2105/22\nWYROK\nz dnia 25 sierpnia 2022 r.")
    assert "Oddala odwołanie." in out["text"]
    assert out["url"] == "https://kio.test/Home/Details/18119"
    assert out["pdf_url"] == "https://kio.test/Home/PdfContent/18119?Kind=KIO"
    assert out["truncated"] is False and out["cached"] is False
    assert rec.requests[1].url.params["Kind"] == "KIO"


def test_kio_document_is_cached_and_truncated_per_call(api):
    rec = api("kio", _kio_doc_routes())
    _call("kio_document", {"id": 18119})
    again = _call("kio_document", {"id": 18119, "max_chars": 500})
    assert len(rec.requests) == 2
    assert again["cached"] is True and again["truncated"] is True and len(again["text"]) == 500


def test_kio_document_404_is_permanent(api):
    api("kio", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] ruling not found$"):
        _call("kio_document", {"id": 99999999})


@pytest.mark.parametrize("bad", [0, -1, 10**9])
def test_kio_document_rejects_bad_id(api, bad):
    rec = api("kio", {})
    with pytest.raises(ToolError, match=r"^\[permanent\]"):
        _call("kio_document", {"id": bad})
    assert rec.requests == []


# --- UOKiK -------------------------------------------------------------------------------------

# Public Domino document ids (UNIDs), not secrets.
UOKIK_UNID = "EFC05E0C47FEE157C1257F8F0031EF80"  # pragma: allowlist secret
LISTED_UNID = "86734DC793D1A570C125880D0037E958"  # pragma: allowlist secret
JUDGMENT_UNID = "CD2E9D4E218F6DD7C12583A1004C8C61"  # pragma: allowlist secret
UOKIK_PDF = (
    "/bp/dec_prez.nsf/43104c28a7a1be23c1257eac006d8dd4/efc05e0c47fee157c1257f8f0031ef80/"
    "$FILE/DDK 1_2016 - Allegro  - w. jawna.pdf"
)


def _uokik_query(req: httpx.Request) -> str:
    raw = req.url.query.decode()
    assert raw.startswith("SearchView&Query=")
    return unquote(raw.split("&")[1].removeprefix("Query="))


def test_uokik_search_builds_the_domino_query_and_maps_rows(api):
    rec = api(
        "uokik",
        {
            ("GET", "/bp/dec_prez.nsf/WWW-decyzje"): text_answer(
                200, fixture_text("uokik_searchview.html")
            )
        },
    )
    out = _call("uokik_search", {"participant": "Allegro", "page_size": 5})
    assert _uokik_query(rec.last) == (
        '([FORM]=Decyzja) AND ([Uczestnicy_postepowania] contains "Allegro")'
    )
    params = dict(p.split("=", 1) for p in rec.last.url.query.decode().split("&")[2:])
    assert params == {"SearchOrder": "3", "SearchMax": "0", "Start": "1", "Count": "5"}
    assert len(out["items"]) == 5 and out["has_more"] is True
    first, second = out["items"][:2]
    assert first["unid"] == LISTED_UNID
    assert first["decision_number"] == "DKK-69/2022"
    assert first["date"] == "2022-03-02"
    assert first["practice"] == "Kontrola koncentracji"
    assert first["participants"].startswith("Allegro.pl sp. z o.o. z siedzibą w Poznaniu")
    assert second["participants"].startswith("Allegro.pl sp. z o.o.")
    assert "<a" not in second["participants"]
    assert first["url"] == (
        "https://uokik.test/bp/dec_prez.nsf/1/86734DC793D1A570C125880D0037E958"
        "?editDocument&act=Decyzja"
    )


def test_uokik_search_all_criteria_and_paging(api):
    rec = api("uokik", {("GET", "/bp/dec_prez.nsf/WWW-decyzje"): text_answer(200, "<html></html>")})
    out = _call(
        "uokik_search",
        {
            "query": "klauzule niedozwolone",
            "decision_number": "RKR-1/2020",
            "signature": "RKR.610.1.2019",
            "page": 3,
            "page_size": 10,
        },
    )
    assert _uokik_query(rec.last) == (
        '([FORM]=Decyzja) AND ([decyzja_nr] = "RKR-1/2020") AND '
        '([Sygnatura] contains "RKR.610.1.2019") AND (klauzule niedozwolone)'
    )
    assert "Start=21&Count=10" in rec.last.url.query.decode()
    assert out == {"page": 3, "items": [], "has_more": False}


def test_uokik_search_values_cannot_inject_domino_syntax(api):
    rec = api("uokik", {("GET", "/bp/dec_prez.nsf/WWW-decyzje"): text_answer(200, "")})
    _call("uokik_search", {"participant": 'x") OR ([FORM]=*', "query": "a [Kara]=Tak"})
    query = _uokik_query(rec.last)
    tail = query.removeprefix("([FORM]=Decyzja) AND ")
    assert "[" not in tail.replace("[Uczestnicy_postepowania]", "")
    assert tail.count('"') == 2


def test_uokik_search_needs_a_criterion(api):
    rec = api("uokik", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] give a query"):
        _call("uokik_search", {"query": " "})
    assert rec.requests == []


def test_uokik_document_maps_metadata_and_pdf_text(api):
    rec = api(
        "uokik",
        {
            ("GET", f"/bp/dec_prez.nsf/1/{UOKIK_UNID}"): text_answer(
                200, fixture_text("uokik_decision_EFC05E0C.html")
            ),
            ("GET", UOKIK_PDF): _pdf("uokik_ddk_1_2016_p1.pdf"),
        },
    )
    out = _call("uokik_document", {"unid": UOKIK_UNID.lower()})
    assert rec.requests[0].url.query.decode() == "editDocument&act=Decyzja"
    assert out["unid"] == UOKIK_UNID
    assert out["decision_number"] == "DDK-1/2016"
    assert out["date"] == "2016-02-09"  # page prints 02/09/2016 (MM/DD/YYYY)
    assert out["case_signature"] == "DDK-61-6/14/MN"
    assert out["participants"].startswith("Grupa Allegro spółka z ograniczoną odpowiedzialnością")
    assert out["practice"] == "Ochrona zbiorowych interesów konsumentów"
    assert out["penalty"] == "Nie" and out["appealed"] == "Tak"
    assert out["region"] == "Wielkopolskie"
    assert out["related_judgments"][0] == {
        "signature": "XVII Ama 51/16",
        "date": "2019-01-25",
        "unid": JUDGMENT_UNID,
    }
    assert "DECYZJA Nr DDK 1/2016" in out["text"]
    assert "Warszawa, dn. 9 lutego 2016 r." in out["text"]
    assert (
        out["url"] == f"https://uokik.test/bp/dec_prez.nsf/1/{UOKIK_UNID}?editDocument&act=Decyzja"
    )
    assert out["pdf_urls"] == [
        "https://uokik.test/bp/dec_prez.nsf/43104c28a7a1be23c1257eac006d8dd4/"
        "efc05e0c47fee157c1257f8f0031ef80/$FILE/DDK%201_2016%20-%20Allegro%20%20-%20w.%20jawna.pdf"
    ]
    assert _call("uokik_document", {"unid": UOKIK_UNID})["cached"] is True
    assert len(rec.requests) == 2


def test_uokik_document_404_is_permanent(api):
    api("uokik", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] decision not found$"):
        _call("uokik_document", {"unid": "0" * 32})


def test_uokik_document_attachment_that_is_not_a_pdf_is_permanent(api):
    api(
        "uokik",
        {
            ("GET", f"/bp/dec_prez.nsf/1/{UOKIK_UNID}"): text_answer(
                200, fixture_text("uokik_decision_EFC05E0C.html")
            ),
            ("GET", UOKIK_PDF): text_answer(200, "<html>login</html>"),
        },
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] the document file is not a PDF$"):
        _call("uokik_document", {"unid": UOKIK_UNID})


@pytest.mark.parametrize("bad", ["", "EFC05E0C", "../admin", "Z" * 32])
def test_uokik_document_rejects_bad_unid(api, bad):
    rec = api("uokik", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid unid"):
        _call("uokik_document", {"unid": bad})
    assert rec.requests == []


# --- DUW (Dziennik Urzędowy Województwa Dolnośląskiego) ----------------------------------------


def test_duw_search_posts_json_and_maps_acts(api):
    rec = api(
        "duw", {("POST", "/api/SearchLegalAct"): json_answer(200, fixture_json("duw_search.json"))}
    )
    out = _call("duw_search", {"query": "targow", "year": 2026})
    body = json.loads(rec.last.content)
    assert body["Title"] == "targow" and body["Year"] == 2026
    assert out["total"] == 1243 and out["returned_by_source"] == 3
    first = out["items"][0]
    assert first == {
        "year": 2026,
        "position": 4019,
        "title": first["title"],
        "act_type": "Uchwała",
        "number": "XXX/303/2026",
        "publisher": "Rady Miejskiej w Kątach Wrocławskich",
        "act_date": "2026-08-27",
        "publication_date": "2026-09-11",
        "url": "https://duw.test/legalact/2026/4019/",
    }
    assert first["title"].startswith("Uchwała nr XXX/303/2026 Rady Miejskiej w Kątach Wrocławskich")


def test_duw_search_pages_from_one_cached_answer(api):
    rec = api(
        "duw", {("POST", "/api/SearchLegalAct"): json_answer(200, fixture_json("duw_search.json"))}
    )
    one = _call("duw_search", {"query": "targow", "page_size": 2})
    two = _call("duw_search", {"query": "Targow", "page": 2, "page_size": 2})
    assert "Year" not in json.loads(rec.last.content)
    assert len(rec.requests) == 1
    assert [i["position"] for i in one["items"]] == [4019, 3896] and one["has_more"] is True
    assert [i["position"] for i in two["items"]] == [3300] and two["has_more"] is False


@pytest.mark.parametrize(
    "args", [{"query": ""}, {"query": "x" * 201}, {"query": "a", "year": 1990}]
)
def test_duw_search_rejects_bad_input(api, args):
    rec = api("duw", {})
    with pytest.raises(ToolError, match=r"^\[permanent\]"):
        _call("duw_search", args)
    assert rec.requests == []


def test_duw_document_maps_metadata_and_pdf_text(api):
    rec = api(
        "duw",
        {
            ("GET", "/api/legalact"): json_answer(200, fixture_json("duw_legalact_2026_4019.json")),
            ("GET", "/GetActPdf.ashx"): _pdf("duw_2026_4019_p1.pdf"),
        },
    )
    out = _call("duw_document", {"year": 2026, "position": 4019})
    assert dict(rec.requests[0].url.params) == {"year": "2026", "journal": "0", "position": "4019"}
    assert dict(rec.requests[1].url.params) == {"year": "2026", "book": "0", "position": "4019"}
    assert out["position_number"] == "2026.4019"
    assert out["journal"] == "Dziennik Urzędowy Województwa Dolnośląskiego"
    assert out["act_type"] == "Uchwała" and out["number"] == "XXX/303/2026"
    assert out["publisher"] == "Rada Miejska w Kątach Wrocławskich"
    assert out["act_date"] == "2026-08-27"
    assert out["invalid"] is False
    assert "UCHWAŁA NR XXX/303/2026" in out["text"]
    assert "z bonifikatą w wysokości 70%" in out["text"]
    assert out["url"] == "https://duw.test/legalact/2026/4019/"
    assert out["pdf_url"] == "https://duw.test/GetActPdf.ashx?year=2026&book=0&position=4019"


def test_duw_document_404_is_permanent(api):
    api("duw", {("GET", "/api/legalact"): text_answer(404, "Nie znaleziono pozycji")})
    with pytest.raises(ToolError, match=r"^\[permanent\] journal position not found$"):
        _call("duw_document", {"year": 2026, "position": 999999})


@pytest.mark.parametrize("args", [{"year": 2026, "position": 0}, {"year": 1900, "position": 1}])
def test_duw_document_rejects_bad_input(api, args):
    rec = api("duw", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid"):
        _call("duw_document", args)
    assert rec.requests == []


# --- shared: politeness, dates ------------------------------------------------------------------


def _timed_gets(client: PublicClient, n: int) -> float:
    async def run() -> float:
        started = time.monotonic()
        await asyncio.gather(*(client.request("GET", "/x") for _ in range(n)))
        return time.monotonic() - started

    return asyncio.run(run())


def test_scrape_clients_are_paced_per_host(monkeypatch):
    monkeypatch.setattr(legal_client, "_next_slot", {})
    ok = httpx.MockTransport(lambda _r: httpx.Response(200, text="ok"))
    paced = PublicClient("https://paced.test", transport=ok, min_interval=0.2)
    assert _timed_gets(paced, 3) >= 0.4  # three requests, two intervals
    other = PublicClient("https://other.test", transport=ok, min_interval=0.2)
    assert _timed_gets(other, 1) < 0.2  # another host has its own slot


def test_only_scraped_services_get_the_interval(monkeypatch):
    monkeypatch.setenv("LEGAL_SCRAPE_MIN_INTERVAL_S", "2.5")
    for service in (*SERVICES, "saos"):
        set_client(service, None)
    try:
        assert {s: get_client(s).min_interval for s in (*SERVICES, "saos")} == {
            "kio": 2.5,
            "uokik": 2.5,
            "duw": 2.5,
            "saos": 0.0,
        }
        assert get_client("kio").base_url == "https://orzeczenia.uzp.gov.pl"
    finally:
        for service in (*SERVICES, "saos"):
            set_client(service, None)


def test_date_helpers():
    assert polish_date("WYROK z dnia 3 września 2021 r.") == "2021-09-03"
    assert polish_date("z dnia 31 lutego 2021") is None
    assert polish_date("bez daty") is None
    head = "WYROK\nWarszawa, dnia 4 listopada 2024 r.\nustawy z dnia 11 września 2019 r."
    assert polish_date(head) == "2024-11-04"  # the ruling's date, not the cited act's
    assert dotted_date("09.02.2016") == "2016-02-09"
    assert dotted_date("05-03-2026") == "2026-03-05"  # the KIO list's format
    assert dotted_date("-") is None


def test_finish_puts_text_last_so_a_cut_result_keeps_its_url():
    from src.builtin.legal.scrape_util import DEFAULT_MAX_CHARS, finish

    out = finish(
        {"text": "x" * 10, "url": "https://example.test/1", "signature": "KIO 1/26"}, 5, False
    )
    assert list(out)[-1] == "text" and out["truncated"] is True
    assert DEFAULT_MAX_CHARS <= 25_000


# --- response caps, PDF limits, redirects -----------------------------------------------------


class _Unread(httpx.AsyncByteStream):
    """A body that fails the test if anything starts reading it."""

    async def __aiter__(self):
        raise AssertionError("body was read")
        yield b""  # pragma: no cover


def test_oversize_content_length_is_refused_before_the_body_is_read():
    def big(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "999999"}, stream=_Unread())

    client = PublicClient(
        "https://cap.test", transport=httpx.MockTransport(big), max_body_bytes=1000
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] answer to GET /x too large \(999999"):
        asyncio.run(client.request("GET", "/x"))


def test_oversize_streamed_body_is_aborted():
    reads: list[int] = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                reads.append(1)
                yield b"x" * 400

    client = PublicClient(
        "https://cap.test",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, stream=Chunks())),
        max_body_bytes=1000,
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] answer to GET /x too large \(over 1000"):
        asyncio.run(client.request("GET", "/x"))
    assert len(reads) == 3  # stopped at the chunk that crossed the cap, not after 100


def test_gzip_body_is_capped_on_decoded_bytes():
    import gzip

    packed = gzip.compress(b"a" * 50_000)
    client = PublicClient(
        "https://cap.test",
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, content=packed, headers={"Content-Encoding": "gzip"})
        ),
        max_body_bytes=10_000,
    )
    assert len(packed) < 10_000
    with pytest.raises(ToolError, match=r"too large"):
        asyncio.run(client.request("GET", "/x"))
    ok = asyncio.run(client.request("GET", "/x", max_bytes=60_000))
    assert ok.text == "a" * 50_000  # decoded once, not twice


def test_scraped_clients_get_the_body_cap():
    for service in (*SERVICES, "saos"):
        set_client(service, None)
    try:
        assert get_client("kio").max_body_bytes == legal_client.MAX_SCRAPE_BODY_BYTES
        assert get_client("saos").max_body_bytes is None
    finally:
        for service in (*SERVICES, "saos"):
            set_client(service, None)


def test_pdf_page_cap_is_honoured(monkeypatch):
    import io

    from pypdf import PdfWriter

    from src.builtin.legal import pdf_text as pdf_mod

    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    seen: list[int] = []
    monkeypatch.setattr(pdf_mod, "_page_text", lambda _p: (seen.append(1), f"line{len(seen)}")[1])
    out = pdf_mod.pdf_to_text(buf.getvalue(), max_pages=3)
    assert len(seen) == 3
    assert out == "line1\nline2\nline3\n[... text cut after page 3 of 5]"
    seen.clear()
    assert pdf_mod.pdf_to_text(buf.getvalue()).endswith("line5")


def test_pdf_extraction_passes_the_page_cap_and_times_out(monkeypatch):
    from src.builtin.legal import scrape_util

    calls: list[int | None] = []

    def slow(_data: bytes, max_pages: int | None = None) -> str:
        calls.append(max_pages)
        time.sleep(0.3)
        return "late"

    monkeypatch.setattr(scrape_util, "pdf_to_text", slow)
    monkeypatch.setattr(scrape_util, "PDF_TIMEOUT_S", 0.05)
    with pytest.raises(ToolError, match=r"^\[permanent\] reading the PDF text took over 0.05 s"):
        asyncio.run(scrape_util.pdf_text(b"%PDF"))
    assert calls == [scrape_util.MAX_PDF_PAGES]


def test_uokik_document_caps_the_total_of_its_attachments(api, monkeypatch):
    from src.builtin.legal import uokik

    rec = api(
        "uokik",
        {
            ("GET", f"/bp/dec_prez.nsf/1/{UOKIK_UNID}"): text_answer(
                200, fixture_text("uokik_decision_EFC05E0C.html")
            ),
            ("GET", UOKIK_PDF): _pdf("uokik_ddk_1_2016_p1.pdf"),
        },
    )
    monkeypatch.setattr(uokik, "MAX_DECISION_PDF_BYTES", 1000)
    with pytest.raises(ToolError, match=r"^\[permanent\] answer to GET .* too large"):
        _call("uokik_document", {"unid": UOKIK_UNID})
    assert len(rec.requests) == 2


def _redirecting(target: str) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/start":
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, text=f"landed {req.url.host}")

    return httpx.MockTransport(handle)


def test_paced_client_refuses_a_cross_host_redirect(monkeypatch):
    monkeypatch.setattr(legal_client, "_next_slot", {})
    client = PublicClient(
        "https://paced.test",
        transport=_redirecting("https://elsewhere.test/end"),
        min_interval=0.01,
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] GET /start redirects to another host"):
        asyncio.run(client.request("GET", "/start", follow_redirects=True))


def test_paced_client_paces_a_same_host_redirect_hop(monkeypatch):
    monkeypatch.setattr(legal_client, "_next_slot", {})
    client = PublicClient("https://paced.test", transport=_redirecting("/end"), min_interval=0.2)

    async def run() -> tuple[str, float]:
        started = time.monotonic()
        resp = await client.request("GET", "/start", follow_redirects=True)
        return resp.text, time.monotonic() - started

    text, took = asyncio.run(run())
    assert text == "landed paced.test"
    assert took >= 0.2  # the hop waited for the host's next slot
