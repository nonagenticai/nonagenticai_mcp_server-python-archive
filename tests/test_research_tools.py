"""research_* tools against discovery-engine / scientific-processor answers via httpx.MockTransport.

The backend answers below are trimmed copies of what the live services returned from the mcp pod
on 2026-10-23 (POST /api/search, GET /api/papers/{id}, POST /api/stats/*).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from src.builtin.legal.client import USER_AGENT, PublicClient
from src.builtin.research import mount_research
from src.builtin.research._server import research_mcp
from src.builtin.research.client import auth_headers, get_client, set_client
from src.builtin.research.literature import source_url
from src.builtin.research.stats import _finite
from tests.carebus_helpers import call_tool, list_tool_names
from tests.legal_helpers import Routes, json_answer

CONTRACT = {
    "research_literature_search",
    "research_literature_record",
    "research_stats_meta_analysis",
    "research_stats_confidence_interval",
    "research_stats_trend",
}

SERVICES = ("discovery_engine", "scientific_processor")
PAPER_ID = "01a1673c-78ed-74c8-b7a1-1bf043ceb236"

SEARCH = {
    "session_id": None,
    "query": "probiotics antibiotic associated diarrhea",
    "total_results": 2,
    "results": [
        {
            "id": PAPER_ID,
            "external_ids": {"pmid": "42898607", "doi": "10.5187/jast.2600114"},
            "title": "A multi-omics characterization of the gut microbiota of weaned piglets.",
            "authors": [{"name": "Nae-Ho Park", "external_ids": {}}, "Minho Song"],
            "year": 2026,
            "abstract": "Weaning is a critical developmental stage in piglets.",
            "source": "pubmed",
            "citation_count": None,
            "venue": "J Anim Sci Technol",
            "is_open_access": None,
            "pdf_url": None,
            "relevance_rank": 0,
        },
        {
            "id": "01a1673c-78ed-74c8-b7a1-1bf043ceb237",
            "external_ids": {"arxiv_id": "2401.00001"},
            "title": "Second",
            "authors": [],
            "year": 2024,
            "abstract": "<jats:p>Probiotics <jats:italic>reduce</jats:italic> risk.</jats:p>",
            "source": "arxiv",
            "pdf_url": "https://arxiv.org/pdf/2401.00001",
        },
    ],
    "sources_queried": ["pubmed", "arxiv"],
    "sources_failed": ["openalex"],
    "per_source_counts": {"pubmed": 1, "arxiv": 1},
    "raw_total": 2,
    "deduped_total": 2,
}

PAPER = {
    "id": PAPER_ID,
    "title": "A multi-omics characterization of the gut microbiota of weaned piglets.",
    "abstract": "Weaning is a critical developmental stage in piglets. Six piglets were assigned.",
    "year": 2026,
    "venue": "J Anim Sci Technol",
    "external_ids": {"pmid": "42898607", "doi": "10.5187/jast.2600114"},
    "citation_count": 0,
    "is_open_access": True,
    "pdf_url": None,
    "metadata": {},
    "authors": [
        {"name": "Nae-Ho Park", "affiliation": "Chungnam National University", "h_index": None}
    ],
    "created_at": "2026-10-23T05:29:38Z",
    "updated_at": "2026-10-23T05:29:38Z",
}

META = {
    "pooled_effect": 0.36875,
    "pooled_se": 0.094992,
    "ci_lower": 0.18257,
    "ci_upper": 0.55493,
    "heterogeneity_q": 1.2308,
    "heterogeneity_i_squared": 0.1875,
    "method": "random",
    "study_weights": [72.727273, 38.095238],
    "computation_time_ms": 2.76,
}
CI = {
    "mean": 3.0,
    "ci_lower": 1.036757,
    "ci_upper": 4.963243,
    "confidence_level": 0.95,
    "method": "normal",
    "computation_time_ms": 25.53,
}
TREND = {
    "trend": "increasing",
    "p_value": 2e-6,
    "statistic": 1.547619,
    "slope": 1.547619,
    "intercept": 0.333333,
    "computation_time_ms": 8.5,
}


@pytest.fixture
def api(monkeypatch) -> Iterator[Callable[[str, dict], Routes]]:
    for var in ("DISCOVERY_ENGINE_TOKEN", "SCIENTIFIC_PROCESSOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def install(service: str, routes: dict) -> Routes:
        rec = Routes(routes)
        set_client(service, PublicClient(f"http://{service}.test", transport=rec.transport))
        return rec

    yield install
    for service in SERVICES:
        set_client(service, None)


def _call(tool: str, args: dict) -> dict:
    return call_tool(research_mcp, tool, args)


def _body(req: httpx.Request) -> dict:
    return json.loads(req.content)


def test_mounted_names_equal_the_contract_exactly():
    root = FastMCP(name="research-root")
    mount_research(root)
    assert list_tool_names(root) == CONTRACT


# --- literature ---------------------------------------------------------------------------------


def test_literature_search_sends_the_engine_request_and_maps_items(api):
    rec = api("discovery_engine", {("POST", "/api/search"): json_answer(200, SEARCH)})
    out = _call(
        "literature_search",
        {
            "query": "  probiotics antibiotic associated diarrhea ",
            "sources": ["pubmed", "arxiv", "pubmed"],
            "max_results_per_source": 500,
            "year_min": 2020,
            "sort_by": "year",
        },
    )
    assert _body(rec.last) == {
        "query": "probiotics antibiotic associated diarrhea",
        "sources": {"year_min": 2020, "sources": ["pubmed", "arxiv"]},
        "max_results_per_source": 20,
        "open_access_only": False,
        "sort_by": "year",
    }
    assert rec.last.headers["User-Agent"] == USER_AGENT
    assert "X-Tenant-Id" not in rec.last.headers  # trusted-network default tenant, no identity
    assert out["total"] == 2
    assert out["sources_failed"] == ["openalex"]
    first, second = out["items"]
    assert first == {
        "id": PAPER_ID,
        "title": SEARCH["results"][0]["title"],
        "authors": ["Nae-Ho Park", "Minho Song"],
        "year": 2026,
        "venue": "J Anim Sci Technol",
        "doi": "10.5187/jast.2600114",
        "pmid": "42898607",
        "external_ids": {"pmid": "42898607", "doi": "10.5187/jast.2600114"},
        "url": "https://doi.org/10.5187/jast.2600114",
        "pdf_url": None,
        "source": "pubmed",
        "citation_count": None,
        "abstract": "Weaning is a critical developmental stage in piglets.",
        "abstract_truncated": False,
    }
    assert second["url"] == "https://arxiv.org/abs/2401.00001"
    assert second["abstract"] == "Probiotics reduce risk."


def test_literature_search_omits_sources_when_not_given_and_truncates_abstracts(api):
    long = {**SEARCH["results"][0], "abstract": "x" * 1000}
    rec = api(
        "discovery_engine",
        {("POST", "/api/search"): json_answer(200, {**SEARCH, "results": [long]})},
    )
    out = _call("literature_search", {"query": "q", "abstract_max_chars": 10})
    assert _body(rec.last)["sources"] == {}
    assert _body(rec.last)["max_results_per_source"] == 3
    item = out["items"][0]
    assert len(item["abstract"]) == 200 and item["abstract_truncated"] is True  # floor is 200


def test_literature_search_sends_the_licence_token_when_configured(api, monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENGINE_TOKEN", "lic-de")
    rec = api("discovery_engine", {("POST", "/api/search"): json_answer(200, SEARCH)})
    _call("literature_search", {"query": "q"})
    assert rec.last.headers["X-Tenant-Id"] == "Bearer lic-de"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"query": "  "}, "query must be 1-1000 chars"),
        ({"query": "x" * 1001}, "query must be 1-1000 chars"),
        ({"query": "q", "year_min": 99}, "invalid year_min"),
        ({"query": "q", "year_min": 2020, "year_max": 2010}, "year_min is after year_max"),
    ],
)
def test_literature_search_rejects_bad_arguments_without_calling(api, args, message):
    rec = api("discovery_engine", {})
    with pytest.raises(ToolError, match=f"\\[permanent\\] {message}"):
        _call("literature_search", args)
    assert rec.requests == []


def test_literature_search_unknown_source_is_refused_by_the_schema(api):
    # The route answers 200: only the schema's allow-list can make this raise, and no request is sent.
    rec = api("discovery_engine", {("POST", "/api/search"): json_answer(200, SEARCH)})
    with pytest.raises(ToolError):
        _call("literature_search", {"query": "q", "sources": ["google"]})
    assert rec.requests == []


def test_literature_search_reads_the_pmid_semantic_scholar_stores_as_pubmed_id(api):
    s2 = {**SEARCH["results"][0], "external_ids": {"pubmed_id": "123456"}}
    api(
        "discovery_engine", {("POST", "/api/search"): json_answer(200, {**SEARCH, "results": [s2]})}
    )
    item = _call("literature_search", {"query": "q"})["items"][0]
    assert item["pmid"] == "123456"
    assert item["url"] == "https://pubmed.ncbi.nlm.nih.gov/123456/"


def test_literature_search_5xx_is_retryable_and_422_permanent(api):
    api("discovery_engine", {("POST", "/api/search"): json_answer(503, {"message": "down"})})
    with pytest.raises(ToolError, match=r"^\[retryable\] 503"):
        _call("literature_search", {"query": "q"})
    api("discovery_engine", {("POST", "/api/search"): json_answer(422, {"detail": "bad"})})
    with pytest.raises(ToolError, match=r"^\[permanent\] 422"):
        _call("literature_search", {"query": "q"})


def test_literature_search_401_is_a_permanent_result(api):
    api(
        "discovery_engine",
        {("POST", "/api/search"): json_answer(401, {"message": "Authentication required"})},
    )
    with pytest.raises(ToolError, match=r"^\[permanent\] 401 .*Authentication required"):
        _call("literature_search", {"query": "q"})


def test_literature_search_timeout_is_retryable(api):
    api("discovery_engine", {("POST", "/api/search"): httpx.ReadTimeout("slow")})
    with pytest.raises(ToolError, match=r"^\[retryable\] timeout"):
        _call("literature_search", {"query": "q"})


def test_literature_search_non_json_is_retryable(api):
    api(
        "discovery_engine",
        {("POST", "/api/search"): lambda _r: httpx.Response(200, text="<html>")},
    )
    with pytest.raises(ToolError, match=r"^\[retryable\] non-JSON"):
        _call("literature_search", {"query": "q"})


def test_literature_record_maps_the_paper(api):
    rec = api(
        "discovery_engine",
        {("GET", f"/api/papers/{PAPER_ID}"): json_answer(200, PAPER)},
    )
    out = _call("literature_record", {"id": PAPER_ID.upper()})
    assert rec.last.url.path == f"/api/papers/{PAPER_ID}"
    assert out == {
        "id": PAPER_ID,
        "title": PAPER["title"],
        "authors": [{"name": "Nae-Ho Park", "affiliation": "Chungnam National University"}],
        "year": 2026,
        "venue": "J Anim Sci Technol",
        "doi": "10.5187/jast.2600114",
        "pmid": "42898607",
        "external_ids": PAPER["external_ids"],
        "url": "https://doi.org/10.5187/jast.2600114",
        "pdf_url": None,
        "is_open_access": True,
        "citation_count": 0,
        "abstract": PAPER["abstract"],
    }


def test_literature_record_404_and_bad_id(api):
    rec = api("discovery_engine", {})
    with pytest.raises(ToolError, match=r"^\[permanent\] paper not found$"):
        _call("literature_record", {"id": PAPER_ID})
    with pytest.raises(ToolError, match=r"^\[permanent\] invalid id"):
        _call("literature_record", {"id": "../health"})
    assert len(rec.requests) == 1


@pytest.mark.parametrize(
    ("ids", "pdf", "url"),
    [
        ({"doi": "https://doi.org/10.1/x"}, None, "https://doi.org/10.1/x"),
        ({"pmid": "1"}, None, "https://pubmed.ncbi.nlm.nih.gov/1/"),
        ({"pubmed_id": "2"}, None, "https://pubmed.ncbi.nlm.nih.gov/2/"),
        ({"pmcid": "PMC3"}, None, "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3/"),
        ({"arxiv": "2401.1"}, None, "https://arxiv.org/abs/2401.1"),
        ({"s2_id": "abc"}, "https://x/p.pdf", "https://x/p.pdf"),
        ({}, None, None),
    ],
)
def test_source_url(ids, pdf, url):
    assert source_url(ids, pdf) == url


# --- statistics ---------------------------------------------------------------------------------

STUDIES = [
    {"effect_size": 0.3, "standard_error": 0.1, "sample_size": 100, "label": "A"},
    {"effect_size": 0.5, "standard_error": 0.15, "sample_size": 80},
]


def test_meta_analysis(api):
    rec = api(
        "scientific_processor",
        {("POST", "/api/stats/meta-analysis"): json_answer(200, META)},
    )
    out = _call("stats_meta_analysis", {"studies": STUDIES})
    assert _body(rec.last) == {
        "studies": [
            {"effect_size": 0.3, "standard_error": 0.1, "sample_size": 100, "label": "A"},
            {"effect_size": 0.5, "standard_error": 0.15, "sample_size": 80},
        ],
        "method": "random",
        "confidence_level": 0.95,
    }
    assert out == {
        "method": "random",
        "confidence_level": 0.95,
        "studies_count": 2,
        "pooled_effect": 0.36875,
        "pooled_se": 0.094992,
        "ci_lower": 0.18257,
        "ci_upper": 0.55493,
        "heterogeneity_q": 1.2308,
        "heterogeneity_i_squared": 0.1875,
        "study_weights": [
            {"label": "A", "weight": 72.727273},
            {"label": None, "weight": 38.095238},
        ],
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"studies": STUDIES[:1]}, "studies must have 2-1000 entries"),
        (
            {"studies": [STUDIES[0], {**STUDIES[1], "standard_error": 0}]},
            r"studies\[1\].standard_error must be > 0",
        ),
        (
            {"studies": [STUDIES[0], {**STUDIES[1], "sample_size": 0}]},
            r"studies\[1\].sample_size must be > 0",
        ),
        ({"studies": STUDIES, "confidence_level": 1.0}, "confidence_level must be in"),
    ],
)
def test_meta_analysis_rejects_bad_input_without_calling(api, args, message):
    rec = api("scientific_processor", {})
    with pytest.raises(ToolError, match=f"\\[permanent\\] {message}"):
        _call("stats_meta_analysis", args)
    assert rec.requests == []


def test_confidence_interval(api):
    rec = api(
        "scientific_processor",
        {("POST", "/api/stats/confidence-interval"): json_answer(200, CI)},
    )
    out = _call("stats_confidence_interval", {"values": [1, 2, 3, 4, 5]})
    assert _body(rec.last) == {
        "values": [1.0, 2.0, 3.0, 4.0, 5.0],
        "method": "normal",
        "confidence_level": 0.95,
        "n_bootstrap": 10000,
    }
    assert out == {
        "method": "normal",
        "confidence_level": 0.95,
        "values_count": 5,
        "mean": 3.0,
        "ci_lower": 1.036757,
        "ci_upper": 4.963243,
    }


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"values": [1]}, "values must have 2-100000 entries"),
        ({"values": [1, 2], "n_bootstrap": 5}, "n_bootstrap must be 100-100000"),
    ],
)
def test_confidence_interval_rejects_bad_input(api, args, message):
    rec = api("scientific_processor", {})
    with pytest.raises(ToolError, match=f"\\[permanent\\] {message}"):
        _call("stats_confidence_interval", args)
    assert rec.requests == []


def test_non_finite_numbers_never_reach_the_service(api):
    # JSON has no Infinity/NaN: over MCP they arrive as null and fail schema validation; the
    # in-process guard covers direct callers.
    rec = api("scientific_processor", {})
    with pytest.raises(ToolError):
        _call("stats_confidence_interval", {"values": [1, float("inf")]})
    with pytest.raises(ToolError, match=r"^\[permanent\] x must be a finite number$"):
        _finite("x", float("nan"))
    assert rec.requests == []


def test_trend(api):
    rec = api("scientific_processor", {("POST", "/api/stats/trend"): json_answer(200, TREND)})
    data = [{"timestamp": i, "value": i * 1.5} for i in range(4)]
    out = _call("stats_trend", {"data": data, "test": "linear_regression"})
    assert _body(rec.last) == {
        "data": [{"timestamp": float(i), "value": i * 1.5} for i in range(4)],
        "test": "linear_regression",
    }
    assert out == {
        "test": "linear_regression",
        "points_count": 4,
        "trend": "increasing",
        "p_value": 2e-6,
        "statistic": 1.547619,
        "slope": 1.547619,
        "intercept": 0.333333,
    }


def test_trend_rejects_short_series_and_sends_token(api, monkeypatch):
    rec = api("scientific_processor", {("POST", "/api/stats/trend"): json_answer(200, TREND)})
    with pytest.raises(ToolError, match=r"\[permanent\] data must have 3-10000 points"):
        _call("stats_trend", {"data": [{"timestamp": 0, "value": 1}]})
    assert rec.requests == []
    monkeypatch.setenv("SCIENTIFIC_PROCESSOR_TOKEN", "lic-sp")
    _call("stats_trend", {"data": [{"timestamp": i, "value": i} for i in range(3)]})
    assert rec.last.headers["X-Tenant-Id"] == "Bearer lic-sp"


def test_stats_backend_unreachable_is_retryable(api):
    api("scientific_processor", {("POST", "/api/stats/trend"): httpx.ConnectError("refused")})
    with pytest.raises(ToolError, match=r"^\[retryable\] cannot reach"):
        _call("stats_trend", {"data": [{"timestamp": i, "value": i} for i in range(3)]})


# --- client config ------------------------------------------------------------------------------


def test_clients_default_to_the_in_cluster_services(monkeypatch):
    for var in (
        "DISCOVERY_ENGINE_URL",
        "SCIENTIFIC_PROCESSOR_URL",
        "DISCOVERY_ENGINE_TIMEOUT_S",
        "DISCOVERY_ENGINE_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SCIENTIFIC_PROCESSOR_TIMEOUT_S", "not-a-number")
    try:
        de, sp = get_client("discovery_engine"), get_client("scientific_processor")
        assert de.base_url == "http://discovery-engine.discovery-engine:8807"
        assert de.timeout == 90.0
        assert sp.base_url == ("http://scientific-processor.scientific-processor:8808")
        assert sp.timeout == 30.0
        assert auth_headers("discovery_engine") is None
    finally:
        for service in SERVICES:
            set_client(service, None)


def test_client_url_and_timeout_from_env(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENGINE_URL", "http://de.example:1/")
    monkeypatch.setenv("DISCOVERY_ENGINE_TIMEOUT_S", "12")
    set_client("discovery_engine", None)
    try:
        client = get_client("discovery_engine")
        assert (client.base_url, client.timeout) == ("http://de.example:1", 12.0)
    finally:
        set_client("discovery_engine", None)
    with pytest.raises(KeyError):
        set_client("nope", None)
