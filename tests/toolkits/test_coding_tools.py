"""The eleven coding tools against the fake broker: preconditions, checks, pending/resume, budgets."""

import json

import pytest

from src.catalog.validation import validate_instance
from src.toolkit_contract.envelope import RESULT_SCHEMA
from tests.toolkits.harness import (
    SETTINGS,
    SHA,
    TOOLKIT,
    FakeBudget,
    Status,
    fixture,
)

CN = "acme-widgets"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", instant)


def limits(tool):
    return TOOLKIT.tool(tool).fields["limits"]["api_calls"]


async def run(tool, inputs, overrides=None, budget=None, monkeypatch=None):
    """Run a tool; the result must always satisfy the platform result schema."""
    from tests.toolkits import harness

    loaded = harness.load_tool(tool)
    if budget is not None:
        loaded.kit.Budget = lambda wall, margin=0: budget
    fake = harness.FakeAPI(tool, overrides)
    from types import SimpleNamespace

    result = await loaded.main(inputs, SimpleNamespace(api=fake))
    validate_instance(RESULT_SCHEMA, result, SETTINGS)
    assert len(fake.calls) <= limits(tool), f"{tool}: {len(fake.calls)} calls"
    return result, fake


def names(result):
    return {c["name"]: c["status"] for c in result["checks"]}


def next_tools(result):
    return [n["tool"] for n in result["next"]]


def graph(count):
    body = fixture("get_graph")
    body["graph"]["node_count"] = count
    return body


PROVISION = {
    "container_name": CN,
    "repo_url": "https://github.com/acme/widgets",
    "branch": "main",
    "idempotency_key": "provision-0001",
}


# --- provision -------------------------------------------------------------------------------


async def test_provision_is_idempotent_when_running_and_indexed():
    result, fake = await run("coding_provision_repo", PROVISION)
    assert result["status"] == "ok" and result["data"]["already_provisioned"] is True
    assert "import_repository" not in fake.keys and "initialize_graph" not in fake.keys
    assert result["data"]["head_sha"] == SHA
    assert names(result) == {
        "workspace_running": "pass",
        "graph_indexed": "pass",
        "head_sha": "pass",
    }


async def test_provision_fresh_repo_imports_polls_and_indexes():
    overrides = {
        "workspace_status": [
            Status(404, {"detail": "no such workspace"}),
            {"message": "m", "container_status": "creating"},
            {"message": "m", "container_status": "creating"},
            {"message": "m", "container_status": "running"},
        ],
    }
    result, fake = await run("coding_provision_repo", PROVISION, overrides)
    assert result["status"] == "ok" and result["data"]["already_provisioned"] is False
    assert fake.keys[:2] == ["workspace_status", "import_repository"]
    assert fake.keys.count("workspace_status") == 4
    assert "initialize_graph" in fake.keys
    # the gateway echoes the GitHub token; the projection must have dropped it
    assert "ghs_SECRET_TOKEN" not in json.dumps(result)


async def test_provision_status_polls_stay_within_the_per_run_operation_budget():
    """Live 2026-10-26: the import was followed by a status poll that the broker denied because
    workspace_status carried a per-run call budget of 1. The fake now enforces it."""
    creating = {"message": "m", "container_status": "creating"}
    running = {"message": "m", "container_status": "running"}
    overrides = {
        "workspace_status": [Status(404, {"detail": "none"})] + [creating] * 13 + [running],
    }
    result, fake = await run("coding_provision_repo", PROVISION, overrides)
    assert result["status"] == "ok"
    assert fake.keys.count("workspace_status") == 15
    assert (
        fake.keys.count("workspace_status")
        <= TOOLKIT.operations["workspace_status"].pagination_limit
    )


async def test_fake_api_denies_a_call_over_the_operation_budget():
    from tests.toolkits import harness

    fake = harness.FakeAPI("coding_provision_repo")
    op = str(TOOLKIT.operations["workspace_status"].operation_id)
    limit = TOOLKIT.operations["workspace_status"].pagination_limit
    for _ in range(limit):
        await fake.call(op, {"container_name": CN})
    with pytest.raises(RuntimeError, match="api_call_denied"):
        await fake.call(op, {"container_name": CN})


async def test_provision_running_without_graph_builds_without_reimport():
    result, fake = await run(
        "coding_provision_repo", PROVISION, {"get_graph": [graph(0), graph(5)]}
    )
    assert result["status"] == "ok"
    assert "import_repository" not in fake.keys and "initialize_graph" in fake.keys


async def test_provision_empty_repository_fails_the_index_check():
    result, _ = await run("coding_provision_repo", PROVISION, {"get_graph": [graph(0), graph(0)]})
    assert result["status"] == "check_failed" and result["verdict"] == "fail"
    assert names(result)["graph_indexed"] == "fail"


async def test_provision_pending_then_resume_does_not_reimport():
    creating = {"message": "m", "container_status": "creating"}
    overrides = {"workspace_status": [Status(404, None)] + [creating] * 40}
    result, fake = await run("coding_provision_repo", PROVISION, overrides)
    assert result["status"] == "pending" and next_tools(result) == ["coding_provision_repo"]
    assert fake.keys.count("workspace_status") == 16  # 1 start + 15 bounded polls
    assert fake.keys.count("import_repository") == 1
    token = result["resume_token"]
    assert result["next"][0]["arguments"]["container_name"] == CN

    resumed, fake2 = await run(
        "coding_provision_repo",
        {**PROVISION, "resume_token": token},
        {"workspace_status": {"message": "m", "container_status": "running"}},
    )
    assert resumed["status"] == "ok"
    assert "import_repository" not in fake2.keys
    assert "initialize_graph" in fake2.keys


async def test_provision_time_exhausted_before_graph_returns_pending_graph_stage():
    result, fake = await run(
        "coding_provision_repo",
        PROVISION,
        {"get_graph": graph(0)},
        budget=FakeBudget(30),
    )
    assert result["status"] == "pending"
    assert "initialize_graph" not in fake.keys
    result2, fake2 = await run(
        "coding_provision_repo",
        {**PROVISION, "resume_token": result["resume_token"]},
        {"get_graph": [graph(7)]},
    )
    assert result2["status"] == "ok" and fake2.keys[0] == "initialize_graph"


def graph_stage_token():
    from tests.toolkits.harness import load_tool

    kit = load_tool("coding_provision_repo").kit
    return kit.encode_token("coding_provision_repo", {"cn": CN, "stage": "graph"})


async def test_provision_graph_already_built_by_the_auto_index_is_not_an_error():
    exists = Status(409, {"detail": "graph already exists"})
    result, fake = await run(
        "coding_provision_repo",
        {
            **PROVISION,
            "resume_token": graph_stage_token(),
        },
        {"initialize_graph": exists, "get_graph": [graph(7)]},
    )
    assert fake.keys[0] == "initialize_graph"
    assert result["status"] == "ok" and names(result)["graph_indexed"] == "pass"


async def test_provision_initialize_graph_failure_is_a_backend_error():
    result, _ = await run(
        "coding_provision_repo",
        {
            **PROVISION,
            "resume_token": graph_stage_token(),
        },
        {"initialize_graph": Status(422, {"detail": "bad"})},
    )
    assert result["status"] == "backend_error" and "initialize_graph" in result["error"]["message"]


def test_commit_always_sends_the_toolkit_author_identity():
    # coding-containers sets user.name/user.email only when both are sent; a workspace
    # has no git identity, so a commit without them exits 128 (HTTP 500).
    from src.broker.policy import transport

    op = TOOLKIT.operations["commit"]
    _, _, _, body = transport(op, {"container_name": CN, "message": "m", "files": ["*"]}, {})
    assert body["author_name"] == "nonagentic coding toolkit"
    assert body["author_email"] == "coding-toolkit@nonagentic.ai"


@pytest.mark.parametrize("name", ["9lives", "1abc"])
async def test_provision_rejects_names_the_gateway_would_refuse(name):
    result, fake = await run("coding_provision_repo", {**PROVISION, "container_name": name})
    assert result["status"] == "precondition_failed"
    assert result["reason"] == "invalid_container_name" and fake.calls == []


async def test_provision_bad_and_foreign_resume_tokens_attempt_nothing():
    loaded_kit = None
    from tests.toolkits.harness import load_tool

    loaded_kit = load_tool("coding_provision_repo").kit
    foreign = loaded_kit.encode_token("coding_open_pr", {"cn": CN, "stage": "graph"})
    other = loaded_kit.encode_token("coding_provision_repo", {"cn": "other", "stage": "graph"})
    for token in ("garbage.value", foreign, other):
        result, fake = await run("coding_provision_repo", {**PROVISION, "resume_token": token})
        assert result["status"] == "precondition_failed"
        assert result["reason"] == "invalid_resume_token" and fake.calls == []


async def test_provision_expired_token_rejected():
    from tests.toolkits.harness import load_tool

    kit = load_tool("coding_provision_repo").kit
    old = kit.encode_token("coding_provision_repo", {"cn": CN, "stage": "graph"}, now=1)
    result, _ = await run("coding_provision_repo", {**PROVISION, "resume_token": old})
    assert result["reason"] == "invalid_resume_token"


async def test_provision_import_refusal_is_a_backend_error_not_a_crash():
    overrides = {
        "workspace_status": Status(404, None),
        "import_repository": Status(409, {"message": "already importing"}),
    }
    result, _ = await run("coding_provision_repo", PROVISION, overrides)
    assert result["status"] == "backend_error"
    assert "409" in result["error"]["message"]


async def test_provision_denied_call_becomes_backend_error():
    overrides = {"workspace_status": Status(404, None), "import_repository": Status(502, None)}
    result, _ = await run("coding_provision_repo", PROVISION, overrides)
    assert result["status"] == "backend_error"
    assert "read the state back" in result["error"]["message"]


# --- repo_status -----------------------------------------------------------------------------


async def test_repo_status_not_provisioned():
    result, fake = await run(
        "coding_repo_status", {"container_name": CN}, {"workspace_status": Status(404, None)}
    )
    assert result["status"] == "ok" and result["data"]["provisioned"] is False
    assert next_tools(result) == ["coding_provision_repo"] and fake.keys == ["workspace_status"]


async def test_repo_status_reports_head_and_dirty():
    dirty = fixture("git_status")
    dirty["modified_files"] = ["src/app.py"]
    result, _ = await run("coding_repo_status", {"container_name": CN}, {"git_status": dirty})
    assert result["data"]["head_sha"] == SHA and result["data"]["dirty"] is True
    assert names(result) == {
        "workspace_running": "pass",
        "graph_indexed": "pass",
        "clean_tree": "degraded",
    }
    assert result["verdict"] == "degraded"


async def test_repo_status_stopped_and_unindexed():
    stopped = {"message": "m", "container_status": "stopped"}
    result, fake = await run(
        "coding_repo_status", {"container_name": CN}, {"workspace_status": stopped}
    )
    assert result["data"]["running"] is False and next_tools(result) == ["coding_provision_repo"]
    assert "get_graph" not in fake.keys
    result, _ = await run("coding_repo_status", {"container_name": CN}, {"get_graph": graph(0)})
    assert result["data"]["indexed"] is False and next_tools(result) == ["coding_provision_repo"]


# --- query_graph -----------------------------------------------------------------------------


async def test_query_graph_preconditions():
    q = {"container_name": CN, "query": "nodes"}
    result, fake = await run("coding_query_graph", q, {"workspace_status": Status(404, None)})
    assert result["reason"] == "not_provisioned" and fake.keys == ["workspace_status"]
    stopped = {"message": "m", "container_status": "creating"}
    result, _ = await run("coding_query_graph", q, {"workspace_status": stopped})
    assert result["reason"] == "workspace_not_running"
    result, fake = await run("coding_query_graph", q, {"get_graph": graph(0)})
    assert result["reason"] == "not_indexed" and "list_nodes" not in fake.keys
    assert next_tools(result) == ["coding_provision_repo"]


@pytest.mark.parametrize(
    "extra,key",
    [
        ({"query": "nodes"}, "list_nodes"),
        ({"query": "nodes", "node_type": "Class"}, "list_nodes_by_type"),
        ({"query": "nodes", "name_pattern": "run"}, "list_nodes_by_name"),
        ({"query": "edges"}, "list_edges"),
        ({"query": "edges", "edge_type": "IMPORTS"}, "list_edges_by_type"),
        ({"query": "architecture"}, "graph_augmented"),
        ({"query": "dependencies"}, "arch_dependencies"),
    ],
)
async def test_query_graph_one_query(extra, key):
    result, fake = await run("coding_query_graph", {"container_name": CN, **extra})
    assert result["status"] == "ok" and fake.keys[-1] == key and len(fake.calls) == 3


async def test_query_graph_rejects_filters_that_do_not_apply():
    for extra in (
        {"query": "edges", "node_type": "Class"},
        {"query": "nodes", "node_type": "Class", "name_pattern": "x"},
        {"query": "dependencies", "edge_type": "CALLS"},
    ):
        result, fake = await run("coding_query_graph", {"container_name": CN, **extra})
        assert result["reason"] == "invalid_query_filter" and fake.calls == []


async def test_query_graph_pages():
    result, fake = await run(
        "coding_query_graph", {"container_name": CN, "query": "nodes", "page": 3, "limit": 10}
    )
    assert fake.args("list_nodes")[0]["page"] == 3 and fake.args("list_nodes")[0]["limit"] == 10


# --- validate_references ---------------------------------------------------------------------


def refs(*items):
    return {"container_name": CN, "references": [{"kind": k, "name": n} for k, n in items]}


async def test_validate_references_all_exist():
    result, fake = await run(
        "coding_validate_references",
        refs(("file", "src/app.py"), ("function", "main"), ("class", "App")),
    )
    assert result["status"] == "ok" and result["verdict"] == "pass"
    assert fake.keys[2:] == ["validate_file", "validate_function", "validate_class"]


async def test_validate_references_missing_and_unverifiable():
    missing = {"valid": False, "file_path": "src/nope.py", "suggestion": "src/app.py"}
    result, _ = await run(
        "coding_validate_references", refs(("file", "src/nope.py")), {"validate_file": missing}
    )
    assert result["status"] == "check_failed"
    assert result["data"]["references"][0]["suggestion"] == "src/app.py"
    unknown = {"valid": None, "function_name": "f", "language_supported": False}
    result, _ = await run(
        "coding_validate_references", refs(("function", "f")), {"validate_function": unknown}
    )
    assert result["status"] == "ok" and result["verdict"] == "degraded"


async def test_validate_references_unsafe_file_path_is_reported_without_a_call():
    result, fake = await run("coding_validate_references", refs(("file", "../etc/passwd")))
    assert result["status"] == "check_failed"
    assert "validate_file" not in fake.keys


async def test_validate_references_preconditions_and_worst_case_calls():
    result, fake = await run(
        "coding_validate_references",
        refs(("file", "a.py")),
        {"workspace_status": Status(404, None)},
    )
    assert result["reason"] == "not_provisioned" and fake.keys == ["workspace_status"]
    result, _ = await run(
        "coding_validate_references", refs(("file", "a.py")), {"get_graph": graph(0)}
    )
    assert result["reason"] == "not_indexed"
    result, fake = await run(
        "coding_validate_references", refs(*[("class", f"C{i}") for i in range(20)])
    )
    assert len(fake.calls) == 22


# --- write_code ------------------------------------------------------------------------------

WRITE = {
    "container_name": CN,
    "language": "python",
    "files": [{"path": "src/app.py", "content": "x = 1\n"}],
    "idempotency_key": "write-0001",
}


def changed(*paths, untracked=()):
    body = fixture("git_diff")
    body["files"] = [{"status": "M", "path": p} for p in paths]
    body["untracked_files"] = list(untracked)
    body["diff"] = "diff --git a/src/app.py b/src/app.py\n+x = 1\n"
    return body


async def test_write_code_runs_every_check_in_order():
    result, fake = await run(
        "coding_write_code", WRITE, {"git_diff": [fixture("git_diff"), changed("src/app.py")]}
    )
    assert result["status"] == "ok" and result["verdict"] == "pass"
    assert names(result) == {
        "write_verified": "pass",
        "lint": "pass",
        "compile": "pass",
        "security_scan": "pass",
        "cycle_gate": "pass",
    }
    order = fake.keys
    # baseline is captured BEFORE the first write, the gate runs after it
    assert order.index("cycle_baseline") < order.index("write_file") < order.index("cycle_check")
    assert fake.args("cycle_check")[0]["baseline_cycles"] == [["a", "b"]]
    assert fake.args("quality_analyze_sync")[0]["paths"] == ["src/app.py"]
    assert result["data"]["head_sha"] == SHA and "+x = 1" in result["data"]["diff"]


async def test_write_code_precondition_and_duplicate_paths():
    result, fake = await run("coding_write_code", WRITE, {"workspace_status": Status(404, None)})
    assert result["reason"] == "not_provisioned" and "write_file" not in fake.keys
    dup = {**WRITE, "files": WRITE["files"] * 2}
    result, fake = await run("coding_write_code", dup)
    assert result["reason"] == "duplicate_paths" and fake.calls == []


async def test_write_code_typescript_compile_is_degraded_with_reason():
    result, fake = await run(
        "coding_write_code",
        {
            **WRITE,
            "language": "typescript",
            "files": [{"path": "src/a.ts", "content": "export {}"}],
        },
        {"git_diff": [fixture("git_diff"), changed("src/a.ts")]},
    )
    assert names(result)["compile"] == "degraded" and result["verdict"] == "degraded"
    compile_check = next(c for c in result["checks"] if c["name"] == "compile")
    assert "tsc" in compile_check["summary"] and "exec" not in fake.keys


async def test_write_code_go_compile_uses_the_allowed_command():
    result, fake = await run(
        "coding_write_code",
        {**WRITE, "language": "go", "files": [{"path": "main.go", "content": "package main"}]},
        {"git_diff": [fixture("git_diff"), changed("main.go")]},
    )
    assert fake.args("exec")[0]["command"] == ["go", "build", "./..."]
    assert result["status"] == "ok"


async def test_write_code_failing_checks_make_check_failed():
    bad_exec = {"exit_code": 1, "stdout": "bad.py: invalid syntax", "stderr": ""}
    lint = {
        "issues": [{"severity": "high", "message": "m", "file_path": "src/app.py", "tool": "ruff"}],
        "total_issues": 1,
    }
    scan = fixture("security_quick")
    scan["result"]["high_count"] = 2
    cycle = {"success": False, "grounded": True}
    result, _ = await run(
        "coding_write_code",
        WRITE,
        {
            "git_diff": [fixture("git_diff"), changed("src/app.py")],
            "exec": bad_exec,
            "quality_analyze_sync": lint,
            "security_quick": scan,
            "cycle_check": cycle,
        },
    )
    assert result["status"] == "check_failed" and result["verdict"] == "fail"
    assert {n: s for n, s in names(result).items() if s == "fail"} == {
        "lint": "fail",
        "compile": "fail",
        "security_scan": "fail",
        "cycle_gate": "fail",
    }


async def test_write_code_degrades_when_evidence_is_missing():
    result, _ = await run(
        "coding_write_code",
        WRITE,
        {
            "git_diff": [fixture("git_diff"), fixture("git_diff")],  # nothing visible as changed
            "quality_analyze_sync": Status(422, {"detail": "paths match no files"}),
            "cycle_baseline": {"success": True},  # no cycles key: no usable baseline
            "security_quick": _degraded_scan(),
        },
    )
    assert result["status"] == "ok" and result["verdict"] == "degraded"
    got = names(result)
    assert got["write_verified"] == got["lint"] == got["security_scan"] == "degraded"
    assert got["cycle_gate"] == "degraded"


def _degraded_scan():
    scan = fixture("security_quick")
    scan["result"]["scan_degraded"] = True
    scan["result"]["degraded_reasons"] = ["timeout"]
    return scan


async def test_write_code_without_graph_still_checks_but_degrades_the_cycle_gate():
    result, fake = await run(
        "coding_write_code",
        WRITE,
        {"get_graph": graph(0), "git_diff": [fixture("git_diff"), changed("src/app.py")]},
    )
    assert "cycle_baseline" not in fake.keys
    assert names(result)["cycle_gate"] == "degraded"


async def test_write_code_pending_hands_over_to_run_checks_when_out_of_time():
    result, fake = await run(
        "coding_write_code",
        WRITE,
        {"git_diff": [fixture("git_diff"), changed("src/app.py")]},
        budget=FakeBudget(10),
    )
    assert result["status"] == "pending" and next_tools(result) == ["coding_run_checks"]
    assert result["next"][0]["arguments"] == {"container_name": CN, "language": "python"}
    assert "write_file" in fake.keys and "quality_analyze_sync" not in fake.keys
    assert names(result)["write_verified"] == "pass"


async def test_write_code_failed_write_stops_with_a_clear_backend_error():
    result, _ = await run(
        "coding_write_code", WRITE, {"write_file": Status(400, {"detail": "bad path"})}
    )
    assert result["status"] == "backend_error" and "400" in result["error"]["message"]
    result, _ = await run("coding_write_code", WRITE, {"write_file": Status(500, None)})
    assert result["status"] == "backend_error"


async def test_write_code_worst_case_ten_files():
    files = [{"path": f"src/f{i}.py", "content": "x = 1\n"} for i in range(10)]
    result, fake = await run(
        "coding_write_code",
        {**WRITE, "files": files},
        {"git_diff": [fixture("git_diff"), changed(*[f["path"] for f in files])]},
    )
    assert fake.keys.count("write_file") == 10 and len(fake.calls) == 19


# --- run_checks ------------------------------------------------------------------------------

CHECKS = {"container_name": CN, "language": "python"}


async def test_run_checks_all_stages_and_head_sha():
    modified = fixture("git_status")
    modified["modified_files"] = ["src/app.py", "weird name.py"]
    result, fake = await run("coding_run_checks", CHECKS, {"git_status": modified})
    assert result["status"] == "ok"
    assert list(names(result)) == [
        "lint",
        "compile",
        "tests",
        "security_scan",
        "dependencies",
        "quality_gate",
    ]
    assert result["data"]["head_sha"] == SHA
    assert result["data"]["quality_gate"]["commit_sha"] == SHA
    assert fake.args("quality_analyze_sync")[0]["paths"] == ["src/app.py"]
    assert "unsafe" not in json.dumps(result) and "1 paths skipped" in json.dumps(result)
    assert result["data"]["tests"]["output_tail"] == "3 passed"


async def test_run_checks_lockfile_only_change_has_nothing_to_lint():
    modified = fixture("git_status")
    modified["modified_files"] = ["uv.lock", "pyproject.toml", "README.md"]
    result, fake = await run("coding_run_checks", CHECKS, {"git_status": modified})
    assert names(result)["lint"] == "pass" and "quality_analyze_sync" not in fake.keys
    assert "no changed source files to lint" in json.dumps(result)


async def test_run_checks_failures_and_inconclusive_gate():
    gate = fixture("quality_gate")
    gate["passed"] = False
    gate["gate_checks"] = [
        {"name": "score", "description": "d", "passed": False, "actual": 40, "threshold": 80}
    ]
    tests = fixture("tests_execute_all")
    tests["success"], tests["exit_code"] = False, 1
    deps = fixture("deps_scan")
    deps["summary"]["critical_vulnerabilities"] = 1
    result, _ = await run(
        "coding_run_checks",
        CHECKS,
        {"quality_gate": gate, "tests_execute_all": tests, "deps_scan": deps},
    )
    assert result["status"] == "check_failed"
    got = names(result)
    assert got["quality_gate"] == got["tests"] == got["dependencies"] == "fail"
    assert "score" in next(c for c in result["checks"] if c["name"] == "quality_gate")["summary"]
    gate.update(tools_failed=["mypy"], recommendations=["r1", "r2", "r3", "r4"])
    result, _ = await run("coding_run_checks", CHECKS, {"quality_gate": gate})
    summary = next(c for c in result["checks"] if c["name"] == "quality_gate")["summary"]
    assert "score 91.0" in summary and "mypy" in summary and "score 40 vs 80" in summary
    assert "r3" in summary and "r4" not in summary
    shown = result["data"]["quality_gate"]
    assert shown["tools_failed"] == ["mypy"] and shown["recommendations"] == ["r1", "r2", "r3"]
    assert shown["failing_checks"][0]["name"] == "score"
    gate["inconclusive"] = True
    result, _ = await run("coding_run_checks", CHECKS, {"quality_gate": gate})
    assert names(result)["quality_gate"] == "error"


async def test_run_checks_test_path_scopes_the_test_run_and_missing_output_is_tolerated():
    result, fake = await run(
        "coding_run_checks",
        {**CHECKS, "test_path": "tests"},
        {"test_result": Status(500, None)},
    )
    assert fake.args("tests_execute")[0]["test_path"] == "tests"
    assert names(result)["tests"] == "pass" and result["data"]["tests"]["output_tail"] is None


async def test_run_checks_pending_and_resume_continue_at_the_stage():
    def budget(reads):
        return 1000 if reads <= 2 else 5  # lint, compile fit; tests do not

    result, fake = await run("coding_run_checks", CHECKS, budget=FakeBudget(budget))
    assert result["status"] == "pending" and next_tools(result) == ["coding_run_checks"]
    assert list(names(result)) == ["lint", "compile"]
    token = result["resume_token"]
    assert result["next"][0]["arguments"]["resume_token"] == token

    resumed, fake2 = await run("coding_run_checks", {**CHECKS, "resume_token": token})
    assert resumed["status"] == "ok"
    assert list(names(resumed)) == [
        "lint",
        "compile",
        "tests",
        "security_scan",
        "dependencies",
        "quality_gate",
    ]
    assert "quality_analyze_sync" not in fake2.keys and "exec" not in fake2.keys


async def test_run_checks_rejects_a_token_for_another_workspace_or_tool():
    from tests.toolkits.harness import load_tool

    kit = load_tool("coding_run_checks").kit
    for token in (
        kit.encode_token(
            "coding_run_checks", {"cn": "other", "language": "python", "stage": 1, "checks": []}
        ),
        kit.encode_token("coding_open_pr", {"cn": CN}),
        kit.encode_token(
            "coding_run_checks", {"cn": CN, "language": "python", "stage": 99, "checks": []}
        ),
    ):
        result, fake = await run("coding_run_checks", {**CHECKS, "resume_token": token})
        assert result["reason"] == "invalid_resume_token" and fake.calls == []


async def test_run_checks_precondition():
    result, _ = await run("coding_run_checks", CHECKS, {"workspace_status": Status(404, None)})
    assert result["reason"] == "not_provisioned"


# --- run_tests -------------------------------------------------------------------------------


async def test_run_tests_pass_fail_and_scope():
    result, fake = await run(
        "coding_run_tests", {"container_name": CN, "language": "python", "test_path": "tests/unit"}
    )
    assert result["status"] == "ok" and fake.args("tests_execute")[0]["test_path"] == "tests/unit"
    assert fake.args("tests_execute")[0]["timeout"] == 120
    tests = fixture("tests_execute_all")
    tests["success"], tests["exit_code"] = False, 2
    result, fake = await run(
        "coding_run_tests",
        {"container_name": CN, "language": "go", "test_type": "coverage", "timeout": 30},
        {"tests_execute_all": tests},
    )
    assert result["status"] == "check_failed" and "exit 2" in result["checks"][0]["summary"]
    assert fake.args("tests_execute_all")[0]["test_type"] == "coverage"


async def test_run_tests_precondition_and_backend_refusal():
    result, _ = await run(
        "coding_run_tests",
        {"container_name": CN, "language": "python"},
        {"workspace_status": Status(404, None)},
    )
    assert result["reason"] == "not_provisioned"
    result, _ = await run(
        "coding_run_tests",
        {"container_name": CN, "language": "python"},
        {"tests_execute_all": Status(422, {"detail": "bad language"})},
    )
    assert result["status"] == "check_failed" and result["checks"][0]["status"] == "error"


# --- security_audit --------------------------------------------------------------------------


async def test_security_audit_full_flow():
    running = {"status": "running", "task_id": "task-1"}
    result, fake = await run(
        "coding_security_audit",
        {"container_name": CN},
        {"security_result": [running, running, fixture("security_result")]},
    )
    assert result["status"] == "ok"
    assert fake.keys.count("security_result") == 3
    assert list(names(result)) == ["security_scan", "dependencies", "vulnerable_packages", "sbom"]
    assert fake.args("security_result")[0] == {"task_id": "task-1"}


async def test_security_audit_degraded_sbom_carries_the_backend_message():
    empty = {"status": "degraded", "message": "SBOM found 0 packages although manifests exist"}
    result, _ = await run("coding_security_audit", {"container_name": CN}, {"sbom": empty})
    sbom = next(c for c in result["checks"] if c["name"] == "sbom")
    assert sbom["status"] == "degraded" and "0 packages although manifests" in sbom["summary"]


async def test_security_audit_reports_findings_and_scan_degraded():
    done = fixture("security_result")
    done["result"]["scan_degraded"] = True
    done["result"]["degraded_reasons"] = ["gateway budget"]
    vulnerable = [
        {"name": "pyyaml", "version": "5.0", "severity": "HIGH", "fixed_versions": ["5.4"]}
    ]
    result, _ = await run(
        "coding_security_audit",
        {"container_name": CN},
        {"security_result": done, "deps_vulnerable": vulnerable},
    )
    got = names(result)
    assert got["security_scan"] == "degraded" and got["vulnerable_packages"] == "fail"
    assert result["status"] == "check_failed"
    assert (
        "scan_degraded"
        in next(c for c in result["checks"] if c["name"] == "security_scan")["summary"]
    )
    assert result["data"]["vulnerable_packages"][0]["name"] == "pyyaml"


async def test_security_audit_falls_back_to_quick_scan_when_async_is_unavailable():
    result, fake = await run(
        "coding_security_audit", {"container_name": CN}, {"security_async": Status(503, None)}
    )
    assert "security_result" not in fake.keys and "security_quick" in fake.keys
    assert names(result)["scan_degraded"] == "degraded" and result["verdict"] == "degraded"
    result, _ = await run(
        "coding_security_audit",
        {"container_name": CN},
        {"deps_vulnerable": Status(503, None), "security_async": Status(503, None)},
    )
    assert names(result)["vulnerable_packages"] == "error"


async def test_security_audit_pending_and_resume():
    running = {"status": "running", "task_id": "task-1"}
    result, fake = await run(
        "coding_security_audit", {"container_name": CN}, {"security_result": running}
    )
    assert result["status"] == "pending" and fake.keys.count("security_result") == 24
    assert "deps_scan" not in fake.keys
    resumed, fake2 = await run(
        "coding_security_audit",
        {"container_name": CN, "resume_token": result["resume_token"]},
    )
    assert resumed["status"] == "ok" and "security_async" not in fake2.keys
    assert fake2.args("security_result")[0] == {"task_id": "task-1"}


async def test_security_audit_failed_task_and_unknown_task():
    result, _ = await run(
        "coding_security_audit",
        {"container_name": CN},
        {"security_result": {"status": "failed", "task_id": "task-1"}},
    )
    assert names(result)["security_scan"] == "error" and result["status"] == "check_failed"
    result, _ = await run(
        "coding_security_audit", {"container_name": CN}, {"security_result": Status(404, None)}
    )
    assert names(result)["security_scan"] == "error"


async def test_security_audit_precondition():
    result, _ = await run(
        "coding_security_audit", {"container_name": CN}, {"workspace_status": Status(404, None)}
    )
    assert result["reason"] == "not_provisioned"


# --- open_pr ---------------------------------------------------------------------------------

PR = {
    "container_name": CN,
    "branch": "feat/widgets",
    "commit_message": "feat: widgets",
    "title": "Widgets",
    "body": "Adds widgets",
    "original_owner": "acme",
    "original_repo": "widgets",
    "idempotency_key": "pr-0000001",
}


def with_changes(*paths):
    body = fixture("git_status")
    body["modified_files"] = list(paths)
    return body


async def test_open_pr_happy_path():
    result, fake = await run("coding_open_pr", PR, {"git_status": with_changes("src/app.py")})
    assert result["status"] == "ok"
    assert fake.keys == [
        "workspace_status",
        "ensure_branch",
        "git_status",
        "commit",
        "git_diff",
        "quality_gate",
        "push",
        "create_pr",
        "pr_checks",
    ]
    assert fake.args("commit")[0]["files"] == ["src/app.py"]
    pr = fake.args("create_pr")[0]
    assert (pr["fork_owner"], pr["fork_repo"]) == ("nonagentic-bot", "widgets")
    assert (pr["original_owner"], pr["original_repo"], pr["base_branch"]) == (
        "acme",
        "widgets",
        "main",
    )
    assert fake.args("pr_checks")[0] == {"owner": "acme", "repo": "widgets", "pr_number": 7}
    assert result["data"]["pr_number"] == 7
    assert "nonagentic-bot" not in json.dumps(result["data"])  # no remote url echoed


NEW = "d" * 40


def failing_gate(**extra):
    return {
        **fixture("quality_gate"),
        "passed": False,
        "overall_score": 61.5,
        "grade": "D",
        "partial": True,
        "tools_failed": ["mypy"],
        "gate_checks": [
            {
                "name": "minimum_score",
                "description": "d",
                "passed": False,
                "actual": 61.5,
                "threshold": 80,
            },
            {"name": "ok_check", "description": "d", "passed": True, "actual": 1, "threshold": 1},
        ],
        "recommendations": ["fix a", "fix b", "fix c", "fix d"],
        **extra,
    }


async def test_open_pr_commits_before_grading_the_gate_at_the_new_head():
    diff = {**fixture("git_diff"), "base_sha": NEW}
    gate = {**fixture("quality_gate"), "commit_sha": NEW}
    result, fake = await run(
        "coding_open_pr",
        PR,
        {"git_status": with_changes("a.py"), "git_diff": diff, "quality_gate": gate},
    )
    assert result["status"] == "ok" and result["data"]["head_sha"] == NEW
    assert fake.keys.index("commit") < fake.keys.index("git_diff") < fake.keys.index("quality_gate")
    assert fake.keys.index("quality_gate") < fake.keys.index("push")


async def test_open_pr_gate_failure_after_commit_reports_details_and_never_pushes():
    result, fake = await run(
        "coding_open_pr",
        PR,
        {"quality_gate": failing_gate(commit_sha=SHA), "git_status": with_changes("a.py")},
    )
    assert result["reason"] == "quality_gate_not_passed"
    assert next_tools(result) == ["coding_run_checks", "coding_write_code"]
    assert "commit" in fake.keys and not {"push", "create_pr"} & set(fake.keys)
    summary = next(c for c in result["checks"] if c["name"] == "quality_gate")["summary"]
    assert "61.5" in summary and "mypy" in summary and "minimum_score 61.5 vs 80" in summary
    assert "ok_check" not in summary
    shown = result["data"]["quality_gate"]
    assert shown["recommendations"] == ["fix a", "fix b", "fix c"]
    assert shown["tools_failed"] == ["mypy"] and shown["partial"] is True
    assert "not pushed" in json.dumps(result["next"])


async def test_open_pr_stale_gate_sha_is_quality_gate_stale():
    body = {**fixture("quality_gate"), "commit_sha": "c" * 40}
    result, fake = await run(
        "coding_open_pr", PR, {"quality_gate": body, "git_status": with_changes("a.py")}
    )
    assert result["reason"] == "quality_gate_stale" and next_tools(result) == ["coding_run_checks"]
    assert not {"push", "create_pr"} & set(fake.keys)


async def test_open_pr_precondition_not_provisioned_and_gate_unavailable():
    result, fake = await run("coding_open_pr", PR, {"workspace_status": Status(404, None)})
    assert result["reason"] == "not_provisioned" and fake.keys == ["workspace_status"]
    result, _ = await run("coding_open_pr", PR, {"quality_gate": Status(404, None)})
    assert result["reason"] == "quality_gate_not_passed"


async def test_open_pr_nothing_to_commit_reuses_head():
    result, fake = await run("coding_open_pr", PR)
    assert "commit" not in fake.keys and "push" in fake.keys
    assert names(result)["commit"] == "degraded"
    assert result["data"]["head_sha"] == SHA and "quality_gate" in fake.keys


async def test_open_pr_stops_on_branch_conflict_and_unsafe_paths():
    result, fake = await run(
        "coding_open_pr",
        PR,
        {"ensure_branch": Status(409, {"error_code": "branch_switch_conflict"})},
    )
    assert result["status"] == "backend_error" and "409" in result["error"]["message"]
    assert "commit" not in fake.keys
    result, fake = await run("coding_open_pr", PR, {"git_status": with_changes("has space.py")})
    assert result["reason"] == "unsafe_paths" and "commit" not in fake.keys


async def test_open_pr_unidentifiable_fork_is_a_backend_error():
    push = {**fixture("push"), "remote_url": "ssh://elsewhere/x"}
    result, fake = await run(
        "coding_open_pr", PR, {"push": push, "git_status": with_changes("a.py")}
    )
    assert result["status"] == "backend_error" and "create_pr" not in fake.keys


async def test_open_pr_no_pr_number_is_a_backend_error():
    pr = {**fixture("create_pr"), "pr_number": None, "no_changes": True}
    result, _ = await run("coding_open_pr", PR, {"create_pr": pr})
    assert result["status"] == "backend_error"


async def test_open_pr_ci_failure_pending_and_resume():
    failed = {**fixture("pr_checks"), "verdict": "failed", "failed": 1, "failed_names": ["lint"]}
    result, _ = await run("coding_open_pr", PR, {"pr_checks": failed})
    assert result["status"] == "check_failed" and names(result)["ci_checks"] == "fail"
    pending = {**fixture("pr_checks"), "verdict": "pending", "completed": 0}
    result, fake = await run("coding_open_pr", PR, {"pr_checks": pending})
    assert result["status"] == "pending" and fake.keys.count("pr_checks") == 10
    assert next_tools(result) == ["coding_open_pr"]
    token = result["resume_token"]
    assert result["next"][0]["arguments"]["resume_token"] == token
    resumed, fake2 = await run("coding_open_pr", {**PR, "resume_token": token})
    assert resumed["status"] == "ok" and fake2.keys == ["pr_checks"]
    assert resumed["data"]["pr_number"] == 7


async def test_open_pr_rejects_foreign_resume_tokens():
    from tests.toolkits.harness import load_tool

    kit = load_tool("coding_open_pr").kit
    for token in (
        kit.encode_token("coding_run_checks", {"cn": CN, "pr_number": 7}),
        kit.encode_token("coding_open_pr", {"cn": "other", "pr_number": 7}),
        kit.encode_token("coding_open_pr", {"cn": CN}),
    ):
        result, fake = await run("coding_open_pr", {**PR, "resume_token": token})
        assert result["reason"] == "invalid_resume_token" and fake.calls == []


# --- update_dependencies / read_file ---------------------------------------------------------

UPDATE = {"container_name": CN, "idempotency_key": "deps-0001"}


def lock_diff():
    body = fixture("git_diff")
    body["files"] = [{"path": "uv.lock", "status": "modified"}]
    body["diff"] = '-version = "0.16.9"\n+version = "0.17.2"\n'
    return body


async def test_update_dependencies_upgrades_every_outdated_package_without_committing():
    result, fake = await run("coding_update_dependencies", UPDATE, {"git_diff": lock_diff()})
    assert result["status"] == "ok" and result["verdict"] == "pass"
    assert fake.keys == ["workspace_status", "deps_outdated", "deps_upgrade", "git_diff"]
    sent = fake.args("deps_upgrade")[0]
    assert sent["packages"] == [{"name": "ruff"}, {"name": "ty"}] and "dry_run" not in sent
    assert result["data"]["upgraded"][0] == {"name": "ruff", "from": "0.16.9", "to": "0.17.2"}
    assert result["data"]["changed_files"] == ["uv.lock"] and "0.17.2" in result["data"]["diff"]
    assert names(result) == {"outdated": "pass", "upgrade": "pass", "tree_changed": "pass"}
    assert next_tools(result) == ["coding_run_checks", "coding_open_pr"]
    assert result["next"][0]["arguments"]["language"] == "python"
    assert "commit" not in fake.keys


async def test_update_dependencies_commit_and_open_pr_are_forced_false_on_the_wire():
    from src.broker.policy import transport

    op = TOOLKIT.operations["deps_upgrade"]
    args = {"container_name": CN, "packages": [{"name": "ruff"}], "dry_run": True}
    _, _, _, body = transport(op, args, {})
    assert body["commit"] is False and body["open_pr"] is False and "branch_name" not in body
    assert body["project_path"] == "." and body["dry_run"] is True
    for forbidden in ("commit", "open_pr", "branch_name", "pr_title", "draft"):
        with pytest.raises(ValueError):
            validate_instance(op.input_schema, {**args, forbidden: True}, SETTINGS)
    _, _, _, body = transport(op, {k: v for k, v in args.items() if k != "dry_run"}, {})
    assert "dry_run" not in body


async def test_update_dependencies_nothing_outdated_falls_back_to_declared_packages():
    result, fake = await run(
        "coding_update_dependencies", UPDATE, {"deps_outdated": [], "git_diff": lock_diff()}
    )
    assert fake.keys == [
        "workspace_status",
        "deps_outdated",
        "read_file",
        "deps_upgrade",
        "git_diff",
    ]
    assert fake.args("read_file")[0] == {"container_name": CN, "path": "pyproject.toml"}
    assert fake.args("deps_upgrade")[0]["packages"] == [{"name": "ruff"}, {"name": "ty"}]
    assert result["status"] == "ok" and result["data"]["selection"] == "declared"
    assert names(result) == {"outdated": "pass", "upgrade": "pass", "tree_changed": "pass"}
    assert "pyproject.toml declares" in result["checks"][0]["summary"]


async def test_update_dependencies_nothing_outdated_or_declared_is_degraded_and_repeatable():
    for responses in (
        {"deps_outdated": [], "read_file": Status(404, {"detail": "not found"})},
        {"deps_outdated": [], "read_file": {**fixture("read_file"), "content": "[project]\n"}},
        {"deps_outdated": [], "read_file": {**fixture("read_file"), "content": "not = [toml"}},
    ):
        for _ in range(2):
            result, fake = await run("coding_update_dependencies", UPDATE, responses)
            assert result["status"] == "ok" and result["next"] == []
            assert names(result) == {"outdated": "degraded"}
            assert "pass `packages`" in result["checks"][0]["summary"]
            assert "deps_upgrade" not in fake.keys


async def test_update_dependencies_declared_fallback_reads_every_dependency_table():
    text = (
        '[project]\ndependencies = ["httpx[http2]>=0.28", "pydantic ; python_version>\'3.10\'"]\n'
        '[project.optional-dependencies]\npg = ["psycopg>=3.3"]\n'
        '[dependency-groups]\ndev = ["ruff>=0.14", {include-group = "test"}, "httpx"]\n'
        'test = ["pytest>=9"]\n[tool.uv]\ndev-dependencies = ["ty"]\n'
    )
    pyproject = {**fixture("read_file"), "content": text}
    _, fake = await run(
        "coding_update_dependencies",
        UPDATE,
        {"deps_outdated": [], "read_file": pyproject, "git_diff": lock_diff()},
    )
    sent = [p["name"] for p in fake.args("deps_upgrade")[0]["packages"]]
    assert sent == ["httpx", "pydantic", "psycopg", "ruff", "pytest", "ty"]


async def test_update_dependencies_explicit_packages_skip_the_outdated_lookup():
    inputs = {**UPDATE, "packages": ["ruff", "ruff"], "dry_run": True}
    result, fake = await run("coding_update_dependencies", inputs)
    assert fake.keys == ["workspace_status", "deps_upgrade"]
    assert fake.args("deps_upgrade")[0]["packages"] == [{"name": "ruff"}]
    assert fake.args("deps_upgrade")[0]["dry_run"] is True
    assert result["status"] == "ok" and result["next"] == []  # a dry run changes nothing


async def test_update_dependencies_held_back_by_constraints_is_degraded_not_failed():
    held = fixture("deps_upgrade")
    for r in held["results"]:
        r["new_version"] = r["previous_version"]
    result, fake = await run("coding_update_dependencies", UPDATE, {"deps_upgrade": held})
    assert result["status"] == "ok" and result["verdict"] == "degraded" and result["next"] == []
    assert result["data"]["unchanged"] == ["ruff", "ty"] and "git_diff" not in fake.keys


async def test_update_dependencies_failed_upgrade_is_check_failed_with_no_next_step():
    bad = fixture("deps_upgrade")
    bad["exit_code"] = 1
    bad["stderr"] = "error: No solution found when resolving dependencies"
    bad["results"][0].update(success=False, error="no solution", new_version=None)
    result, fake = await run("coding_update_dependencies", UPDATE, {"deps_upgrade": bad})
    assert result["status"] == "check_failed" and names(result)["upgrade"] == "fail"
    assert "No solution found" in result["checks"][-1]["summary"] and result["next"] == []
    assert "git_diff" not in fake.keys


async def test_update_dependencies_preconditions():
    result, fake = await run(
        "coding_update_dependencies", UPDATE, {"workspace_status": Status(404, None)}
    )
    assert result["reason"] == "not_provisioned" and fake.keys == ["workspace_status"]
    assert next_tools(result) == ["coding_provision_repo"]
    stopped = {"message": "m", "container_status": "stopped"}
    result, fake = await run("coding_update_dependencies", UPDATE, {"workspace_status": stopped})
    assert result["reason"] == "workspace_not_running" and fake.keys == ["workspace_status"]


async def test_update_dependencies_rejected_upgrade_is_a_backend_error():
    result, _ = await run(
        "coding_update_dependencies", UPDATE, {"deps_upgrade": Status(422, {"detail": "bad"})}
    )
    assert result["status"] == "backend_error" and "422" in result["error"]["message"]


async def test_update_dependencies_skips_names_the_gateway_would_refuse():
    outdated = fixture("deps_outdated") + [
        {"name": "--index-url", "current_version": "1", "latest_version": "2"}
    ]
    result, fake = await run(
        "coding_update_dependencies", UPDATE, {"deps_outdated": outdated, "git_diff": lock_diff()}
    )
    assert fake.args("deps_upgrade")[0]["packages"] == [{"name": "ruff"}, {"name": "ty"}]
    assert result["data"]["skipped_outdated_entries"] == 1


async def test_update_dependencies_caps_the_outdated_selection_at_fifty():
    many = [{"name": f"pkg{i}", "current_version": "1", "latest_version": "2"} for i in range(60)]
    result, fake = await run(
        "coding_update_dependencies", UPDATE, {"deps_outdated": many, "git_diff": lock_diff()}
    )
    assert len(fake.args("deps_upgrade")[0]["packages"]) == 50
    assert "capped" in result["checks"][0]["summary"]


READ = {"container_name": CN, "path": "pyproject.toml"}


async def test_read_file_returns_content_hash_and_line_count():
    result, fake = await run("coding_read_file", READ)
    assert result["status"] == "ok" and fake.keys == ["workspace_status", "read_file"]
    assert fake.args("read_file")[0] == {"container_name": CN, "path": "pyproject.toml"}
    data = result["data"]
    assert data["type"] == "file" and "ruff" in data["content"] and data["lines_total"] == 7
    assert len(data["sha256"]) == 64 and data["truncated"] is False and "entries" not in data


async def test_read_file_span_and_directory_listing():
    result, fake = await run("coding_read_file", {**READ, "start": 2, "end": 4})
    assert fake.args("read_file")[0]["start"] == 2 and fake.args("read_file")[0]["end"] == 4
    listing = {
        "path": "/workspace/src",
        "type": "directory",
        "entries": [
            {
                "permissions": "-rw-r--r--",
                "size": "1240",
                "modified": "Mar 12 09:15",
                "name": "main.py",
                "type": "file",
            }
        ],
    }
    result, fake = await run("coding_read_file", {"container_name": CN}, {"read_file": listing})
    assert "path" not in fake.args("read_file")[0]
    assert (
        result["data"]["type"] == "directory" and result["data"]["entries"][0]["name"] == "main.py"
    )
    assert "content" not in result["data"]


async def test_read_file_missing_path_is_a_precondition_pointing_at_the_parent():
    result, _ = await run(
        "coding_read_file",
        {"container_name": CN, "path": "src/nope.py"},
        {"read_file": Status(404, {"detail": "not found"})},
    )
    assert result["status"] == "precondition_failed" and result["reason"] == "path_not_found"
    assert result["next"][0]["arguments"] == {"container_name": CN, "path": "src"}


async def test_read_file_not_provisioned_and_bad_span():
    result, fake = await run("coding_read_file", READ, {"workspace_status": Status(404, None)})
    assert result["reason"] == "not_provisioned" and fake.keys == ["workspace_status"]
    result, fake = await run("coding_read_file", {**READ, "start": 1, "end": 2500})
    assert result["reason"] == "span_too_large" and fake.calls == []
    result, fake = await run("coding_read_file", {**READ, "start": 9, "end": 3})
    assert result["reason"] == "invalid_span" and fake.calls == []


async def test_read_file_retries_a_too_large_unbounded_read_as_the_first_400_lines():
    def big_then_ok(arguments):
        if "end" not in arguments:
            raise RuntimeError("api_call_denied")
        return fixture("read_file")

    result, fake = await run("coding_read_file", READ, {"read_file": big_then_ok})
    assert result["status"] == "ok"
    assert fake.args("read_file")[1]["start"] == 1 and fake.args("read_file")[1]["end"] == 400


# --- worst-case call counts (the limits in tool.json are sized from these) ---------------------


async def test_worst_case_call_counts_fit_the_declared_limits():
    creating = {"message": "m", "container_status": "creating"}
    running = {"message": "m", "container_status": "running"}
    pending_ci = {**fixture("pr_checks"), "verdict": "pending", "completed": 0}
    still = {"status": "running", "task_id": "task-1"}
    worst = {}
    _, fake = await run(
        "coding_provision_repo",
        PROVISION,
        {
            "workspace_status": [Status(404, None)] + [creating] * 14 + [running],
            "get_graph": graph(120),
        },
    )
    worst["coding_provision_repo"] = len(fake.calls)
    _, fake = await run("coding_repo_status", {"container_name": CN})
    worst["coding_repo_status"] = len(fake.calls)
    _, fake = await run("coding_query_graph", {"container_name": CN, "query": "edges"})
    worst["coding_query_graph"] = len(fake.calls)
    _, fake = await run(
        "coding_validate_references", refs(*[("function", f"f{i}") for i in range(20)])
    )
    worst["coding_validate_references"] = len(fake.calls)
    files = [{"path": f"src/f{i}.py", "content": "x"} for i in range(10)]
    _, fake = await run("coding_write_code", {**WRITE, "files": files})
    worst["coding_write_code"] = len(fake.calls)
    _, fake = await run("coding_run_checks", CHECKS, {"git_status": with_changes("src/app.py")})
    worst["coding_run_checks"] = len(fake.calls)
    _, fake = await run("coding_run_tests", {"container_name": CN, "language": "python"})
    worst["coding_run_tests"] = len(fake.calls)
    _, fake = await run(
        "coding_security_audit",
        {"container_name": CN},
        {"security_result": [still] * 23 + [fixture("security_result")]},
    )
    worst["coding_security_audit"] = len(fake.calls)
    _, fake = await run(
        "coding_open_pr",
        PR,
        {
            "git_status": with_changes("a.py"),
            "pr_checks": [pending_ci] * 9 + [fixture("pr_checks")],
        },
    )
    worst["coding_open_pr"] = len(fake.calls)
    _, fake = await run(
        "coding_update_dependencies", UPDATE, {"deps_outdated": [], "git_diff": lock_diff()}
    )
    worst["coding_update_dependencies"] = len(fake.calls)
    _, fake = await run("coding_read_file", READ, {"read_file": lambda a: fixture("read_file")})
    worst["coding_read_file"] = len(fake.calls)
    assert worst == {
        "coding_provision_repo": 20,
        "coding_repo_status": 4,
        "coding_query_graph": 3,
        "coding_validate_references": 22,
        "coding_write_code": 19,
        "coding_run_checks": 10,
        "coding_run_tests": 3,
        "coding_security_audit": 29,
        "coding_open_pr": 18,
        "coding_update_dependencies": 5,
        "coding_read_file": 2,
    }
