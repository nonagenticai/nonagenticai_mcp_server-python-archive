"""legal_* tools against recorded real API answers (tests/fixtures/legal) via httpx.MockTransport."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from urllib.parse import parse_qs

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from src.builtin.legal import apply_source_gates, mount_legal
from src.builtin.legal._server import legal_mcp
from src.builtin.legal.client import USER_AGENT, PublicClient, set_client
from src.builtin.legal.eli import clear_pdf_cache
from src.builtin.legal.html_text import element_text, html_to_text
from src.builtin.legal.vat import clear_cache
from tests.carebus_helpers import call_tool, list_tool_names
from tests.legal_helpers import (
    FIXTURES,
    Routes,
    fixture_json,
    fixture_text,
    json_answer,
    text_answer,
)

CONTRACT = {
    "legal_eli_search",
    "legal_eli_act",
    "legal_eli_act_text",
    "legal_saos_search",
    "legal_saos_judgment",
    "legal_eurlex_act",
    "legal_eurlex_text",
    "legal_krs_extract",
    "legal_vat_whitelist",
    "legal_kio_search",
    "legal_kio_document",
    "legal_uokik_search",
    "legal_uokik_document",
    "legal_duw_search",
    "legal_duw_document",
}

SERVICES = ("eli", "saos", "cellar", "krs", "mf_wl")


@pytest.fixture
def api() -> Iterator[Callable[[str, dict], Routes]]:
    def install(service: str, routes: dict) -> Routes:
        rec = Routes(routes)
        set_client(service, PublicClient(f"https://{service}.test", transport=rec.transport))
        return rec

    clear_cache()
    clear_pdf_cache()
    yield install
    for service in SERVICES:
        set_client(service, None)
    clear_cache()
    clear_pdf_cache()


def _call(tool: str, args: dict) -> dict:
    return call_tool(legal_mcp, tool, args)


def _params(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}


def test_mounted_names_equal_the_contract_exactly(monkeypatch):
    monkeypatch.setenv("LEGAL_UOKIK_ENABLED", "true")
    root = FastMCP(name="legal-root")
    mount_legal(root)
    assert list_tool_names(root) == CONTRACT


def test_uokik_tools_are_hidden_unless_enabled(monkeypatch):
    monkeypatch.delenv("LEGAL_UOKIK_ENABLED", raising=False)
    root = FastMCP(name="legal-root")
    mount_legal(root)
    assert list_tool_names(root) == CONTRACT - {"legal_uokik_search", "legal_uokik_document"}
    monkeypatch.setenv("LEGAL_UOKIK_ENABLED", "true")
    apply_source_gates()
    assert list_tool_names(root) == CONTRACT


# --- ELI ---------------------------------------------------------------------------------------


def test_eli_search_maps_items_and_sends_filters(api):
    rec = api(
        "eli", {("GET", "/eli/acts/search"): json_answer(200, fixture_json("eli_search.json"))}
    )
    out = _call("eli_search", {"title": "kodeks rodzinny i opiekuńczy", "limit": 500})
    assert out["total"] == 26
    first = out["items"][0]
    assert first == {
        "eli": "DU/2026/236",
        "title": first["title"],
        "address": "Dz.U. 2026 poz. 236",
        "type": "Obwieszczenie",
        "status": "obowiązujący",
        "in_force": True,
        "promulgation": "2026-02-27",
    }
    params = _params(rec.last)
    assert params == {
        "publisher": "DU",
        "title": "kodeks rodzinny i opiekuńczy",
        "inForce": "1",
        "limit": "100",  # clamped
        "offset": "0",
    }
    assert rec.last.headers["user-agent"] == USER_AGENT


def test_eli_search_without_in_force_filter_and_mp(api):
    rec = api("eli", {("GET", "/eli/acts/search"): json_answer(200, {"count": 0, "items": []})})
    out = _call("eli_search", {"keyword": "alimenty", "in_force_only": False, "publisher": "MP"})
    assert out == {"total": 0, "items": []}
    assert "inForce" not in _params(rec.last)
    assert _params(rec.last)["publisher"] == "MP"


def test_eli_act_maps_metadata_and_titled_references(api):
    rec = api(
        "eli",
        {
            ("GET", "/eli/acts/DU/1964/59"): json_answer(200, fixture_json("eli_act.json")),
            ("GET", "/eli/acts/DU/1964/59/references"): json_answer(
                200, fixture_json("eli_refs.json")
            ),
        },
    )
    out = _call("eli_act", {"eli": "DU/1964/59"})
    assert out["title"] == "Ustawa z dnia 25 lutego 1964 r. - Kodeks rodzinny i opiekuńczy."
    assert out["in_force"] is True
    assert out["entry_into_force"] == "1965-01-01"
    assert out["valid_from"] == "1965-01-01"
    assert out["has_html"] is True
    assert "alimenty" in out["keywords"]
    assert out["references"]["Akty zmieniające"][0] == {
        "eli": "DU/2025/897",
        "title": "Ustawa z dnia 24 czerwca 2025 r. o zmianie ustawy - Kodeks rodzinny i opiekuńczy",
        "date": "2025-10-08",
    }
    assert out["text_url_html"] == "https://eli.test/eli/acts/DU/1964/59/text.html"
    assert out["text_url_pdf"] == "https://eli.test/eli/acts/DU/1964/59/text.pdf"
    assert len(rec.requests) == 2


def test_eli_act_falls_back_to_untitled_references(api):
    api(
        "eli",
        {
            ("GET", "/eli/acts/DU/1964/59"): json_answer(200, fixture_json("eli_act.json")),
            ("GET", "/eli/acts/DU/1964/59/references"): json_answer(503, {}),
        },
    )
    out = _call("eli_act", {"eli": "DU/1964/59"})
    first = out["references"]["Akty zmieniające"][0]
    assert first == {"eli": "DU/2025/897", "title": None, "date": "2025-10-08"}


def test_eli_act_caps_references_per_group(api):
    act = fixture_json("eli_act.json")
    act["references"] = {"Akty zmieniające": [{"id": f"DU/2000/{i}"} for i in range(80)]}
    api(
        "eli",
        {
            ("GET", "/eli/acts/DU/1964/59"): json_answer(200, act),
            ("GET", "/eli/acts/DU/1964/59/references"): json_answer(500, {}),
        },
    )
    assert len(_call("eli_act", {"eli": "DU/1964/59"})["references"]["Akty zmieniające"]) == 50


@pytest.mark.parametrize(
    ("value", "expected"), [("NOT_IN_FORCE", False), ("UNKNOWN", None), (None, None)]
)
def test_eli_in_force_mapping(api, value, expected):
    act = {**fixture_json("eli_act.json"), "inForce": value}
    api("eli", {("GET", "/eli/acts/DU/1964/59"): json_answer(200, act)})
    assert _call("eli_act", {"eli": "DU/1964/59"})["in_force"] is expected


def test_eli_act_404_is_permanent(api):
    api("eli", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] act not found$"):
        _call("eli_act", {"eli": "DU/1964/99999"})


def test_eli_act_5xx_is_retryable(api):
    api("eli", {("GET", "/eli/acts/DU/1964/59"): json_answer(502, {"message": "bad gateway"})})
    with pytest.raises(ToolError, match=r"^\[retryable\] 502"):
        _call("eli_act", {"eli": "DU/1964/59"})


_BAD_ELI = [
    "../../admin",
    "DU/1964/59/../../x",
    "DU/1964/59?x=1",
    "XX/1964/59",
    "DU/64/59",
    "DU/1964/123456",
    "",
    "du/1964/59",
    "DU/1964/59/",
]


@pytest.mark.parametrize("bad", _BAD_ELI)
@pytest.mark.parametrize("tool", ["eli_act", "eli_act_text"])
def test_bad_eli_is_rejected_before_any_request(api, tool, bad):
    rec = api("eli", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid eli"):
        _call(tool, {"eli": bad})
    assert rec.requests == []


def test_eli_act_text_cuts_one_article(api):
    rec = api(
        "eli",
        {
            ("GET", "/eli/acts/DU/1964/59/text.html"): text_answer(
                200, fixture_text("eli_text_excerpt_arti_133.html")
            )
        },
    )
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "133", "wording": "original"})
    assert out["article"] == "133"
    assert out["wording"] == "original" and out["source_eli"] == "DU/1964/59"
    assert out["source_format"] == "html" and out["consolidation_date"] is None
    assert out["truncated"] is False
    text = out["text"]
    assert text.startswith("Art. 133.")
    assert "§ 1." in text and "Rodzice obowiązani są do świadczeń alimentacyjnych" in text
    assert "znajduje się w niedostatku." in text
    assert "Art. 134" not in text and "rodzeństwa" not in text
    assert "<" not in text
    assert rec.last.url.path == "/eli/acts/DU/1964/59/text.html"


_SUP_HTML = """<html><body>
<div class="unit unit_arti" id="book_III-titl_VI-arti_449" data-id="arti_449">
  <h3><b>Art.&nbsp;449.</b></h3><div>Plain article.</div></div>
<div class="unit unit_arti" id="book_III-titl_VI_1-arti_449_1" data-id="arti_449_1">
  <h3><b>Art.&nbsp;449<sup>1</sup>.</b></h3>
  <div class="unit unit_para" id="book_III-titl_VI_1-arti_449_1-para_1"><br/>
  <div>Producent odpowiada za szkodę.<img src="x.png"></div></div></div>
<div class="unit unit_arti" id="book_III-titl_VI_1-arti_449a" data-id="arti_449a">
  <div>Letter article.</div></div>
</body></html>"""


@pytest.mark.parametrize("article", ["449^1", "449(1)", "449_1", "449¹", "Art. 449^1"])
def test_eli_act_text_superscript_article_forms(api, article):
    api("eli", {("GET", "/eli/acts/DU/1964/93/text.html"): text_answer(200, _SUP_HTML)})
    out = _call("eli_act_text", {"eli": "DU/1964/93", "article": article, "wording": "original"})
    assert out["text"] == "Art. 449^1.\nProducent odpowiada za szkodę."


def test_eli_act_text_plain_and_letter_articles_do_not_bleed(api):
    api("eli", {("GET", "/eli/acts/DU/1964/93/text.html"): text_answer(200, _SUP_HTML)})
    assert _call("eli_act_text", {"eli": "DU/1964/93", "article": "449", "wording": "original"})[
        "text"
    ] == ("Art. 449.\nPlain article.")
    assert _call("eli_act_text", {"eli": "DU/1964/93", "article": "449a", "wording": "original"})[
        "text"
    ] == ("Letter article.")


def test_eli_act_text_unknown_article_is_permanent(api):
    api("eli", {("GET", "/eli/acts/DU/1964/93/text.html"): text_answer(200, _SUP_HTML)})
    with pytest.raises(ToolError, match=r"^\[permanent\] article 999 not found"):
        _call("eli_act_text", {"eli": "DU/1964/93", "article": "999", "wording": "original"})


@pytest.mark.parametrize("bad", ["133; drop", "../1", "abc", "133^", "1" * 7, "133-para_1"])
def test_eli_act_text_bad_article_is_rejected_before_any_request(api, bad):
    rec = api("eli", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid article"):
        _call("eli_act_text", {"eli": "DU/1964/59", "article": bad})
    assert rec.requests == []


def test_eli_act_text_empty_body_means_pdf_only(api):
    api("eli", {("GET", "/eli/acts/DU/2026/236/text.html"): text_answer(200, "")})
    with pytest.raises(ToolError, match=r"^\[permanent\] no HTML text \(PDF only\): .*text\.pdf$"):
        _call("eli_act_text", {"eli": "DU/2026/236", "wording": "original"})


def test_eli_act_text_whole_act_is_truncated(api):
    api("eli", {("GET", "/eli/acts/DU/1964/59/text.html"): text_answer(200, _SUP_HTML * 20)})
    out = _call("eli_act_text", {"eli": "DU/1964/59", "max_chars": 600, "wording": "original"})
    assert out["truncated"] is True and len(out["text"]) == 600
    assert out["article"] is None


# --- ELI current wording (newest consolidated text) ---------------------------------------------

KRO = "/eli/acts/DU/1964/59"
TJ = "/eli/acts/DU/2026/236"
TJ_PDF = f"{TJ}/text/T/D20260236L.pdf"


def _pdf_answer(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=(FIXTURES / "du_2026_236_tj_pages.pdf").read_bytes(),
        headers={"Content-Type": "application/pdf"},
    )


def _kro_routes(refs: dict | None = None, tj_meta: dict | None = None) -> dict:
    return {
        ("GET", f"{KRO}/references"): json_answer(200, refs or fixture_json("eli_refs.json")),
        ("GET", TJ): json_answer(200, tj_meta or fixture_json("eli_act_du_2026_236.json")),
        ("GET", TJ_PDF): _pdf_answer,
    }


def test_current_wording_cuts_article_from_consolidated_pdf(api):
    rec = api("eli", _kro_routes())
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    assert out["wording"] == "current"
    assert out["source_eli"] == "DU/2026/236"
    assert out["source_format"] == "pdf"
    assert out["consolidation_date"] == "2026-02-19"
    assert out["amended_after_consolidation"] == []
    assert out["consolidation_date_inferred"] is False
    text = out["text"]
    assert text.startswith("Art. 133. § 1. Rodzice obowiązani są do świadczeń alimentacyjnych")
    # § 3 exists only in the amended wording, and spans a page break in the PDF
    assert "§ 3. Rodzice mogą uchylić się od świadczeń alimentacyjnych" in text
    assert "samodzielnego utrzymania się." in text
    assert "Art. 134" not in text and "Dziennik Ustaw" not in text and "Kancelaria" not in text
    assert "wychowania" in text  # "wycho-\nwania" re-joined
    assert [r.url.path for r in rec.requests] == [f"{KRO}/references", TJ, TJ_PDF]


@pytest.mark.parametrize("article", ["61^1", "61(1)", "61¹"])
def test_current_wording_superscript_article_from_pdf(api, article):
    api("eli", _kro_routes())
    text = _call("eli_act_text", {"eli": "DU/1964/59", "article": article})["text"]
    assert text.startswith("Art. 61^1. § 1. Jeżeli między małżonkami nastąpił zupełny rozkład")
    assert "Art. 61^2" not in text


def test_plain_article_is_not_confused_with_superscript_one(api):
    api("eli", _kro_routes())
    with pytest.raises(ToolError, match=r"^\[permanent\] article 611 not found in DU/2026/236"):
        _call("eli_act_text", {"eli": "DU/1964/59", "article": "611"})


def test_flattened_superscript_form_is_accepted_after_its_base_article():
    from src.builtin.legal.pdf_text import cut_article

    text = "Art. 449. Base.\nArt. 4491. § 1. Producent.\nArt. 4492. Next.\nArt. 450. After."
    assert cut_article(text, "449_1") == "Art. 4491. § 1. Producent."
    assert cut_article(text, "449_2") == "Art. 4492. Next."


def test_caret_form_wins_over_a_real_flattened_lookalike():
    from src.builtin.legal.pdf_text import cut_article

    text = "Art. 43. A.\nArt. 43^1. Sup.\nArt. 44. B.\nArt. 430. C.\nArt. 431. Real."
    assert cut_article(text, "43_1") == "Art. 43^1. Sup."
    assert cut_article(text, "431") == "Art. 431. Real."


@pytest.mark.parametrize(
    "text",
    [
        "Art. 430. C.\nArt. 431. Real 431.",  # previous heading is 430: a real Art. 431
        "Art. 43. A.\nArt. 431. X.\nArt. 430. Y.\nArt. 431. Z.",  # two candidates
        "Art. 431. First line.",  # nothing before it
    ],
)
def test_flattened_form_that_could_be_a_real_article_is_ambiguous(text):
    from src.builtin.legal.pdf_text import AmbiguousArticleError, cut_article

    with pytest.raises(AmbiguousArticleError):
        cut_article(text, "43_1")


def test_ambiguous_article_is_a_permanent_tool_error(api, monkeypatch):
    from src.builtin.legal import eli

    monkeypatch.setattr(eli, "pdf_to_text", lambda _data: "Art. 430. C.\nArt. 431. Real.")
    api("eli", _kro_routes())
    with pytest.raises(ToolError, match=r"^\[permanent\] article ambiguous: 43\^1"):
        _call("eli_act_text", {"eli": "DU/1964/59", "article": "43^1"})


def test_bracketed_superscript_pdf_form_is_normalised():
    # DU/2026/795 (KC consolidated 2026) prints superscripts as "Art. 449[1]."
    from src.builtin.legal.pdf_text import cut_article, pdf_to_text

    text = pdf_to_text((FIXTURES / "du_2026_795_tj_page.pdf").read_bytes())
    art = cut_article(text, "449_1")
    assert art is not None
    assert art.startswith("Art. 449^1. § 1. Kto wytwarza w zakresie swojej działalności")
    assert art.endswith("podobny produkt\nulepszony.")
    assert "[1]" not in text and "Art. 449^2." in text


def test_pdf_is_parsed_once_and_cached(api):
    rec = api("eli", _kro_routes())
    _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    _call("eli_act_text", {"eli": "DU/1964/59", "article": "61^1"})
    assert [r.url.path for r in rec.requests].count(TJ_PDF) == 1


def test_amendments_after_the_consolidation_cut_off_are_listed(api):
    refs = fixture_json("eli_refs.json")
    refs["Akty zmieniające"] += [
        {"act": {"ELI": "DU/2026/900", "promulgation": "2026-06-01"}, "date": "2026-07-01"},
        {"act": {"ELI": "DU/2026/100", "promulgation": "2026-02-18"}, "date": "2026-03-01"},
        {"act": {"ELI": "DU/2026/240", "promulgation": "2026-02-19"}, "date": "2026-03-05"},
    ]
    api("eli", _kro_routes(refs=refs))
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    assert out["amended_after_consolidation"] == [
        {"eli": "DU/2026/240", "promulgation": "2026-02-19", "date_unknown": False},
        {"eli": "DU/2026/900", "promulgation": "2026-06-01", "date_unknown": False},
    ]


def test_amending_act_without_promulgation_is_listed_not_dropped(api):
    refs = fixture_json("eli_refs.json")
    refs["Akty zmieniające"] += [
        {"act": {"ELI": "DU/2026/999"}, "date": None},
        {"act": {"ELI": "DU/1993/78"}, "date": None},  # earlier year than the cut-off: covered
    ]
    api("eli", _kro_routes(refs=refs))
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    assert out["amended_after_consolidation"] == [
        {"eli": "DU/2026/999", "promulgation": None, "date_unknown": True}
    ]


def test_missing_legal_status_date_is_inferred_conservatively(api):
    meta = {
        k: v for k, v in fixture_json("eli_act_du_2026_236.json").items() if k != "legalStatusDate"
    }
    api("eli", _kro_routes(tj_meta=meta))
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    # previous consolidation DU/2023/2809 (promulgated 2023-12-29), NOT the announcementDate
    assert out["consolidation_date"] == "2023-12-29"
    assert out["consolidation_date_inferred"] is True
    assert [a["eli"] for a in out["amended_after_consolidation"]] == ["DU/2025/897"]


def test_missing_legal_status_date_without_previous_consolidation_lists_everything(api):
    refs = fixture_json("eli_refs.json")
    refs["Inf. o tekście jednolitym"] = refs["Inf. o tekście jednolitym"][:1]
    meta = {
        k: v for k, v in fixture_json("eli_act_du_2026_236.json").items() if k != "legalStatusDate"
    }
    api("eli", _kro_routes(refs=refs, tj_meta=meta))
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    assert out["consolidation_date"] is None and out["consolidation_date_inferred"] is True
    assert [a["eli"] for a in out["amended_after_consolidation"]] == [
        "DU/2023/1615",
        "DU/2023/1843",
        "DU/2025/897",
    ]


def test_newest_consolidation_is_chosen_by_promulgation(api):
    refs = fixture_json("eli_refs.json")
    refs["Inf. o tekście jednolitym"].reverse()  # order must not matter
    rec = api("eli", _kro_routes(refs=refs))
    assert _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})["source_eli"] == (
        "DU/2026/236"
    )
    assert rec.requests[1].url.path == TJ


def test_consolidated_html_is_preferred_over_pdf(api):
    meta = {**fixture_json("eli_act_du_2026_236.json"), "textHTML": True}
    routes = _kro_routes(tj_meta=meta)
    routes[("GET", f"{TJ}/text.html")] = text_answer(200, _SUP_HTML)
    rec = api("eli", routes)
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "449^1"})
    assert out["source_format"] == "html"
    assert out["text"] == "Art. 449^1.\nProducent odpowiada za szkodę."
    assert TJ_PDF not in [r.url.path for r in rec.requests]


def test_empty_consolidated_html_falls_back_to_pdf(api):
    meta = {**fixture_json("eli_act_du_2026_236.json"), "textHTML": True}
    routes = _kro_routes(tj_meta=meta)
    routes[("GET", f"{TJ}/text.html")] = text_answer(200, "")
    api("eli", routes)
    assert _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})["source_format"] == "pdf"


def test_pdf_without_typed_file_uses_text_pdf(api):
    meta = {
        **fixture_json("eli_act_du_2026_236.json"),
        "texts": [{"type": "T", "fileName": "../x.pdf"}],
    }
    routes = _kro_routes(tj_meta=meta)
    routes[("GET", f"{TJ}/text.pdf")] = _pdf_answer
    rec = api("eli", routes)
    _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    assert rec.last.url.path == f"{TJ}/text.pdf"


def test_without_consolidation_the_act_itself_is_used_and_all_amendments_listed(api):
    refs = {
        k: v for k, v in fixture_json("eli_refs.json").items() if k != "Inf. o tekście jednolitym"
    }
    api(
        "eli",
        {
            ("GET", f"{KRO}/references"): json_answer(200, refs),
            ("GET", KRO): json_answer(200, fixture_json("eli_act.json")),
            ("GET", f"{KRO}/text.html"): text_answer(
                200, fixture_text("eli_text_excerpt_arti_133.html")
            ),
        },
    )
    out = _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})
    assert out["source_eli"] == "DU/1964/59" and out["source_format"] == "html"
    assert out["consolidation_date"] is None
    assert [a["eli"] for a in out["amended_after_consolidation"]] == [
        "DU/2023/1615",
        "DU/2023/1843",
        "DU/2025/897",
    ]
    assert out["text"].startswith("Art. 133.")


def test_damaged_pdf_is_permanent(api):
    routes = _kro_routes()
    routes[("GET", TJ_PDF)] = lambda _req: httpx.Response(200, content=b"%PDF-1.4 garbage")
    api("eli", routes)
    with pytest.raises(ToolError, match=r"^\[permanent\] cannot read the PDF text"):
        _call("eli_act_text", {"eli": "DU/1964/59", "article": "133"})


def test_current_wording_404_is_permanent(api):
    api("eli", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] act not found$"):
        _call("eli_act_text", {"eli": "DU/1964/99999"})


# --- SAOS --------------------------------------------------------------------------------------


def test_saos_search_maps_items_and_strips_highlights(api):
    rec = api(
        "saos",
        {("GET", "/api/search/judgments"): json_answer(200, fixture_json("saos_search.json"))},
    )
    out = _call(
        "saos_search",
        {"query": "alimenty", "court_type": "COMMON", "date_from": "2026-01-01", "page_size": 3},
    )
    assert out["total"] == 17935
    first = out["items"][0]
    assert first["id"] == 554405
    assert first["case_numbers"] == ["VI Ca 188/26"]
    assert first["judgment_date"] == "2026-10-15"
    assert first["judgment_type"] == "REASONS"
    assert first["court"] == "Sąd Okręgowy w Olsztynie"
    assert first["division"] == "VI Wydział Cywilny Rodzinny"
    assert first["keywords"] == ["podwyższenie alimentów"]
    assert "<em>" not in first["snippet"] and "podwyższenie alimentów" in first["snippet"]
    assert _params(rec.last) == {
        "all": "alimenty",
        "courtType": "COMMON",
        "judgmentDateFrom": "2026-01-01",
        "pageSize": "3",
        "pageNumber": "0",
        "sortingField": "JUDGMENT_DATE",
        "sortingDirection": "DESC",
    }


@pytest.mark.parametrize(
    "args",
    [{"query": " "}, {"query": "x" * 501}, {"query": "a", "date_to": "15.10.2026"}],
)
def test_saos_search_rejects_bad_input(api, args):
    rec = api("saos", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] "):
        _call("saos_search", args)
    assert rec.requests == []


def test_saos_judgment_maps_and_converts_html(api):
    rec = api(
        "saos",
        {("GET", "/api/judgments/554405"): json_answer(200, fixture_json("saos_judgment.json"))},
    )
    out = _call("saos_judgment", {"id": 554405})
    assert rec.last.url.path == "/api/judgments/554405"
    assert out["case_numbers"] == ["VI Ca 188/26"]
    assert out["court"] == "Sąd Okręgowy w Olsztynie"
    assert out["judges"] == ["Robert Kłosowski"]
    assert out["legal_bases"] == ["art. 138 kro"]
    assert any("Kodeks rodzinny" in r for r in out["referenced_regulations"])
    assert out["summary"] is None
    assert out["text"].startswith("Sygn. akt VI Ca 188/26\nUZASADNIENIE")
    assert "<" not in out["text"]
    assert out["url"].startswith("https://apiorzeczenia.wroclaw.sa.gov.pl/")


def test_saos_judgment_truncates(api):
    api(
        "saos",
        {("GET", "/api/judgments/554405"): json_answer(200, fixture_json("saos_judgment.json"))},
    )
    out = _call("saos_judgment", {"id": 554405, "max_chars": 500})
    assert out["truncated"] is True and len(out["text"]) == 500


def test_saos_judgment_404_is_permanent(api):
    api("saos", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] judgment not found$"):
        _call("saos_judgment", {"id": 1})


@pytest.mark.parametrize("bad", [0, -5, 10**11])
def test_saos_judgment_rejects_bad_id(api, bad):
    rec = api("saos", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid id"):
        _call("saos_judgment", {"id": bad})
    assert rec.requests == []


# --- EUR-Lex / CELLAR --------------------------------------------------------------------------


def test_eurlex_act_maps_sparql_and_sends_typed_literal(api):
    rec = api(
        "cellar",
        {("POST", "/webapi/rdf/sparql"): json_answer(200, fixture_json("eurlex_sparql.json"))},
    )
    out = _call("eurlex_act", {"celex": "32016R0679"})
    assert out["celex"] == "32016R0679"
    assert out["title_pl"].startswith(
        "Rozporządzenie Parlamentu Europejskiego i Rady (UE) 2016/679"
    )
    assert out["title_en"].startswith("Regulation (EU) 2016/679")
    assert out["date_document"] == "2016-04-27"
    assert out["in_force"] is True
    assert out["entry_into_force"] == ["2016-05-24", "2018-05-25"]
    assert out["end_of_validity"] == "9999-12-31"
    assert out["text_url"].endswith("CELEX:32016R0679")
    sent = parse_qs(rec.last.content.decode())["query"][0]
    assert '"32016R0679"^^xsd:string' in sent
    assert rec.last.headers["accept"] == "application/sparql-results+json"


def test_eurlex_act_unknown_celex_is_permanent(api):
    api(
        "cellar",
        {("POST", "/webapi/rdf/sparql"): json_answer(200, {"results": {"bindings": []}})},
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] celex not found$"):
        _call("eurlex_act", {"celex": "32099R9999"})


def test_eurlex_act_5xx_is_retryable(api):
    api("cellar", {("POST", "/webapi/rdf/sparql"): json_answer(503, {})})
    with pytest.raises(ToolError, match=r"^\[retryable\] 503"):
        _call("eurlex_act", {"celex": "32016R0679"})


_BAD_CELEX = [
    '32016R0679" } ; DROP',
    "../32016R0679",
    "32016R0679/../x",
    "32016r0679",
    "A2016R0679",
    "3201",
    "",
    "32016R0679 ",
]


@pytest.mark.parametrize("bad", _BAD_CELEX)
@pytest.mark.parametrize("tool", ["eurlex_act", "eurlex_text"])
def test_bad_celex_is_rejected_before_any_request(api, tool, bad):
    rec = api("cellar", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid celex"):
        _call(tool, {"celex": bad})
    assert rec.requests == []


def test_eurlex_text_follows_redirect_and_strips_html(api):
    manifestation = "/resource/cellar/3e485e15.0018.03/DOC_1"
    rec = api(
        "cellar",
        {
            ("GET", "/resource/celex/32016R0679"): lambda req: httpx.Response(
                303, headers={"Location": f"https://cellar.test{manifestation}"}
            ),
            ("GET", manifestation): text_answer(
                200,
                fixture_text("cellar_32016R0679_pl_xhtml_excerpt.html"),
                {"Content-Type": "application/xhtml+xml;charset=UTF-8"},
            ),
        },
    )
    out = _call("eurlex_text", {"celex": "32016R0679", "max_chars": 2000})
    assert "ROZPORZĄDZENIE PARLAMENTU EUROPEJSKIEGO I RADY (UE) 2016/679" in out["text"]
    assert "L_2016119PL" not in out["text"]  # <title> is dropped
    assert out["truncated"] is True and len(out["text"]) == 2000
    first = rec.requests[0]
    assert first.headers["accept"] == "application/xhtml+xml"
    assert first.headers["accept-language"] == "pol"


def test_eurlex_text_404_is_permanent(api):
    api("cellar", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] celex not found or no eng XHTML text"):
        _call("eurlex_text", {"celex": "32016R0679", "lang": "eng"})


def test_eurlex_text_multiple_choices_is_permanent(api):
    api("cellar", {("GET", "/resource/celex/32016R0679"): text_answer(300, "<html>choices</html>")})
    with pytest.raises(ToolError, match=r"^\[permanent\] no single pol XHTML text"):
        _call("eurlex_text", {"celex": "32016R0679"})


@pytest.mark.parametrize("bad", ["pl", "POL", "po/l", "pol;x"])
def test_eurlex_text_rejects_bad_lang(api, bad):
    rec = api("cellar", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid lang"):
        _call("eurlex_text", {"celex": "32016R0679", "lang": bad})
    assert rec.requests == []


# --- KRS ---------------------------------------------------------------------------------------


def test_krs_extract_maps_and_left_pads(api):
    rec = api(
        "krs",
        {
            ("GET", "/api/krs/OdpisAktualny/0000028860"): json_answer(
                200, fixture_json("krs_odpis.json")
            )
        },
    )
    out = _call("krs_extract", {"krs": "28860"})
    assert _params(rec.last) == {"rejestr": "P", "format": "json"}
    assert out["krs"] == "0000028860"
    assert out["as_of"] == "09.10.2026"
    assert out["name"] == "ORLEN SPÓŁKA AKCYJNA"
    assert out["legal_form"] == "SPÓŁKA AKCYJNA"
    assert out["nip"] == "7740001454"
    assert out["regon"] == "61018820100000"
    assert out["address"] == "CHEMIKÓW 7, 09-411 PŁOCK, POLSKA"
    assert out["capital"] == {"amount": "1451177561,25", "currency": "PLN"}
    rep = out["representation"]
    assert rep["body"] == "ZARZĄD"
    assert rep["method"].startswith("- DWAJ CZŁONKOWIE ZARZĄDU")
    assert rep["members"][0] == {"name": "I******* F*****", "function": "PREZES ZARZĄDU"}
    acts = out["business_activity"]
    assert len(acts) == 10
    assert acts[0] == {
        "code": "19.20.Z",
        "description": "WYTWARZANIE I PRZETWARZANIE PRODUKTÓW RAFINACJI ROPY NAFTOWEJ ORAZ "
        "PRODUKTÓW Z PALIW KOPALNYCH",
        "predominant": True,
    }
    assert not any(a["predominant"] for a in acts[1:])


def test_krs_extract_register_s_and_404(api):
    rec = api("krs", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] KRS entry not found"):
        _call("krs_extract", {"krs": "0000000001", "register": "S"})
    assert _params(rec.last)["rejestr"] == "S"


@pytest.mark.parametrize("bad", ["12345678901", "../1", "12a", "", "0000028860?x"])
def test_krs_extract_rejects_bad_number(api, bad):
    rec = api("krs", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid krs"):
        _call("krs_extract", {"krs": bad})
    assert rec.requests == []


# --- MF VAT whitelist --------------------------------------------------------------------------


def test_vat_whitelist_maps_and_caches(api):
    rec = api(
        "mf_wl",
        {("GET", "/api/search/nip/7740001454"): json_answer(200, fixture_json("mf_vat_nip.json"))},
    )
    out = _call("vat_whitelist", {"nip": "774-000-14-54", "date": "2026-10-22"})
    assert out == {
        "nip": "7740001454",
        "date": "2026-10-22",
        "found": True,
        "name": "ORLEN SPÓŁKA AKCYJNA",
        "status_vat": "Czynny",
        "regon": "610188201",
        "krs": "0000028860",
        "registration_date": "1993-07-05",
        "removal_date": None,
        "accounts_count": out["accounts_count"],
        "request_id": "adO3M-9938hnd",
        "cached": False,
    }
    assert out["accounts_count"] > 50
    assert _params(rec.last) == {"date": "2026-10-22"}
    again = _call("vat_whitelist", {"nip": "7740001454", "date": "2026-10-22"})
    assert again["cached"] is True and again["name"] == "ORLEN SPÓŁKA AKCYJNA"
    assert len(rec.requests) == 1


def test_vat_whitelist_defaults_date_to_today(api):
    rec = api("mf_wl", {("GET", "/api/search/nip/7740001454"): json_answer(200, {"result": {}})})
    out = _call("vat_whitelist", {"nip": "7740001454"})
    assert out["found"] is False and out["status_vat"] is None
    assert len(_params(rec.last)["date"]) == 10


def test_vat_whitelist_account_check(api):
    nrb = "30102010260000140203550589"
    rec = api(
        "mf_wl",
        {
            ("GET", f"/api/check/nip/7740001454/bank-account/{nrb}"): json_answer(
                200, {"result": {"accountAssigned": "TAK", "requestId": "r-1"}}
            )
        },
    )
    out = _call(
        "vat_whitelist",
        {
            "nip": "7740001454",
            "date": "2026-10-22",
            "account": "PL30 1020 1026 0000 1402 0355 0589",
        },
    )
    assert out == {
        "nip": "7740001454",
        "date": "2026-10-22",
        "account": nrb,
        "account_assigned": True,
        "request_id": "r-1",
    }
    assert len(rec.requests) == 1


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"nip": "7740001455"}, "checksum"),
        ({"nip": "../1234567"}, "invalid nip"),
        ({"nip": "123"}, "invalid nip"),
        ({"nip": "7740001454", "date": "22.10.2026"}, "invalid date"),
        ({"nip": "7740001454", "account": "1234/../x"}, "invalid account"),
    ],
)
def test_vat_whitelist_rejects_bad_input(api, args, match):
    rec = api("mf_wl", {})
    with pytest.raises(ToolError, match=rf"^\[permanent\] .*{match}"):
        _call("vat_whitelist", args)
    assert rec.requests == []


def test_vat_whitelist_400_carries_mf_message(api):
    api(
        "mf_wl",
        {
            ("GET", "/api/search/nip/7740001454"): json_answer(
                400, {"code": "WL-113", "message": "Pole 'date' ma nieprawidłowy format."}
            )
        },
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] 400 .*nieprawidłowy format"):
        _call("vat_whitelist", {"nip": "7740001454", "date": "2026-10-22"})


# --- client error mapping ----------------------------------------------------------------------


def test_timeout_is_retryable(api):
    api("krs", {("GET", "/api/krs/OdpisAktualny/0000028860"): httpx.ReadTimeout("slow")})
    with pytest.raises(ToolError, match=r"^\[retryable\] timeout"):
        _call("krs_extract", {"krs": "28860"})


def test_connect_error_is_retryable(api):
    api("saos", {("GET", "/api/judgments/5"): httpx.ConnectError("refused")})
    with pytest.raises(ToolError, match=r"^\[retryable\] cannot reach"):
        _call("saos_judgment", {"id": 5})


def test_429_is_retryable(api):
    api("mf_wl", {("GET", "/api/search/nip/7740001454"): json_answer(429, {"message": "limit"})})
    with pytest.raises(ToolError, match=r"^\[retryable\] 429 .*limit"):
        _call("vat_whitelist", {"nip": "7740001454", "date": "2026-10-22"})


def test_base_url_comes_from_env(monkeypatch):
    from src.builtin.legal.client import get_client

    monkeypatch.setenv("LEGAL_SAOS_URL", "http://saos.override:1234/")
    monkeypatch.setenv("LEGAL_TIMEOUT_S", "7")
    set_client("saos", None)
    try:
        client = get_client("saos")
        assert client.base_url == "http://saos.override:1234"
        assert client.timeout == 7.0
    finally:
        set_client("saos", None)


# --- html_text ---------------------------------------------------------------------------------


def test_html_to_text_drops_script_style_and_keeps_blocks():
    html = "<html><head><title>T</title><style>x{}</style></head><body><p>a&nbsp;b</p>"
    html += "<script>evil()</script><table><tr><td>c</td><td>d</td></tr></table></body></html>"
    assert html_to_text(html) == "a b\nc d"


def test_element_text_none_when_absent():
    assert element_text("<div id='a'>x</div>", lambda t, a: a.get("id") == "b") is None
