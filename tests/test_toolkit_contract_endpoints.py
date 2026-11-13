"""Endpoint allow-lists and the no-LLM deny-list (product plan §1)."""

import pytest

from src.toolkit_contract import (
    LLM_DENY_LIST,
    DenyClass,
    Endpoint,
    EndpointAllowList,
    EndpointNotAllowedError,
    assert_no_llm_endpoints,
    find_denied,
)
from src.toolkit_contract.endpoints import denied_by


def ep(path: str, method="GET", service="coding") -> Endpoint:
    return Endpoint(service, method, path)


DENIED = [
    "/api/ai",
    "/api/ai/insights",
    "/api/ai/archimate/classify",
    "/api/tests/ai/smart-selection",
    "/api/tests/ai",
    "/api/graphs/{container_name}/ai-augmented",
    "/api/graphs/{container_name}/ai-augmented/nodes",
    "/api/workspaces/{c}/execute-agent",
    "/chat/completions",
    "/v1/chat/completions",
    "/workers/{worker_name}/message",
    "/query/ask",
    "/api/kag/query/ask",
    "/api/ask",
    "/api/bug-hunter/{c}/hunt",
    "/api/analysis/bug-hunter",
    "/api/analysis/semantic/analyze-codebase",
    "/api/static/advanced-validation/{c}",
    "/api/static/documentation",
    "/API/AI/Insights",  # case-insensitive
    "/api/ai/",  # trailing slash
]
ALLOWED = [
    "/api/repositories/import",
    "/api/workspaces/{container_name}/status",
    "/api/graphs/initialize",
    "/api/graphs/{container_name}",
    "/api/quality/analyze-sync",
    "/api/tests/run",
    "/api/aid/records",
    "/api/search",
    "/api/askew",
    "/workers",
    "/api/static/lint",
]


@pytest.mark.parametrize("path", DENIED)
def test_deny_list_matches_llm_and_unclassified_paths(path):
    assert denied_by(path) is not None


@pytest.mark.parametrize("path", ALLOWED)
def test_deny_list_leaves_deterministic_paths(path):
    assert denied_by(path) is None


def test_unclassified_are_marked_separately():
    unclassified = {r.why for r in LLM_DENY_LIST if r.classification is DenyClass.UNCLASSIFIED}
    assert unclassified == {
        "bug-hunter",
        "semantic/analyze-codebase",
        "static/advanced-validation",
        "static/documentation",
    }


def test_assert_no_llm_endpoints_fails_and_names_violations():
    clean = EndpointAllowList("coding", tuple(ep(p) for p in ALLOWED))
    assert_no_llm_endpoints([clean])
    dirty = EndpointAllowList(
        "coding", (ep("/api/graphs/{c}"), ep("/api/graphs/{c}/ai-augmented", "POST"))
    )
    with pytest.raises(AssertionError, match=r"coding: coding POST /api/graphs/\{c\}/ai-augmented"):
        assert_no_llm_endpoints([clean, dirty])
    violations = find_denied([dirty])
    assert [str(v.endpoint) for v in violations] == ["coding POST /api/graphs/{c}/ai-augmented"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("Coding", "GET", "/x"), "service"),
        (("coding", "HEAD", "/x"), "method"),
        (("coding", "GET", "x"), "absolute"),
        (("coding", "GET", "/a//b"), "noncanonical"),
        (("coding", "GET", "/a/../b"), "noncanonical"),
        (("coding", "GET", "/a?b=1"), "noncanonical"),
        (("coding", "GET", "/a%2Fb"), "noncanonical"),
        (("coding", "GET", "/a/{Bad}"), "noncanonical"),
        (("coding", "GET", "/" + "a" * 1030), "bounded"),
    ],
)
def test_endpoint_declarations_validated(args, message):
    with pytest.raises(ValueError, match=message):
        Endpoint(*args)


def test_root_path_and_duplicates():
    assert Endpoint("coding", "GET", "/").regex.match("/")
    with pytest.raises(ValueError, match="duplicate"):
        EndpointAllowList("coding", (ep("/a"), ep("/a")))
    assert ep("/a") in EndpointAllowList("coding", (ep("/a"),))


def test_authorize_matches_templates_one_segment_per_variable():
    allow = EndpointAllowList(
        "coding",
        (ep("/api/workspaces/{c}/status"), ep("/api/workspaces/{c}/write-file", "POST")),
    )
    assert allow.authorize("coding", "GET", "/api/workspaces/repo-1/status").path.endswith("status")
    with pytest.raises(EndpointNotAllowedError, match="not in the coding allow-list"):
        allow.authorize("coding", "GET", "/api/workspaces/a/b/status")
    with pytest.raises(EndpointNotAllowedError, match="not in the coding allow-list"):
        allow.authorize("coding", "POST", "/api/workspaces/repo-1/status")
    with pytest.raises(EndpointNotAllowedError, match="not in the coding allow-list"):
        allow.authorize("strata-rag", "GET", "/api/workspaces/repo-1/status")


def test_authorize_rechecks_deny_list_on_concrete_path():
    allow = EndpointAllowList("coding", (ep("/api/{area}/insights"),))
    assert find_denied([allow]) == []  # the template alone looks clean
    with pytest.raises(EndpointNotAllowedError, match="denied"):
        allow.authorize("coding", "GET", "/api/ai/insights")


@pytest.mark.parametrize(
    "path", ["/api/workspaces/../status", "/api/workspaces/a%2F..", "relative", "/a//b", "/a?x=1"]
)
def test_authorize_rejects_noncanonical_requests(path):
    allow = EndpointAllowList("coding", (ep("/api/workspaces/{c}"),))
    with pytest.raises(EndpointNotAllowedError, match="noncanonical"):
        allow.authorize("coding", "GET", path)
