"""Contract every platform toolkit must satisfy."""

import ast
import json
import re
import sys
from pathlib import Path
from uuid import uuid7

import pytest

from runner.entrypoint import extract
from runner.protocol import decode, encode
from runner.sdk import API, ToolContext
from src.catalog.models import ToolLimits
from src.catalog.package import PackageFile, package_digest, validate_package
from src.catalog.public import output_envelope, validate_public_input
from src.catalog.validation import validate_instance, validate_schema
from src.platform.toolkit_source import load_all, package_files, tool_manifest
from src.toolkit_contract.endpoints import LLM_DENY_LIST, denied_by
from src.toolkit_contract.envelope import RESULT_SCHEMA
from src.toolkit_contract.schema import assert_strict_schema
from tests.fixtures import IMAGE, settings
from tests.toolkits.harness import ROOT, FakeAPI, Status, fixture

ALL_TOOLKITS = load_all(ROOT / "toolkits")
#: The Kata toolkit contract; external-runtime toolkits (no service.json, e.g. fr) have no package
#: code, operations or runner and are covered by tests/test_platform_seed_external.py.
TOOLKITS = tuple(tk for tk in ALL_TOOLKITS if not tk.external)
CONFIG = settings()
TOOLS = [(tk, tool) for tk in TOOLKITS for tool in tk.tools]
STDLIB = set(sys.stdlib_module_names)
EXCLUDED_PATHS = {"/api/repositories/apply-changes", "/api/repositories/ci-wait"}
TENANT = uuid7()


def ids(pairs):
    return [t.name for _, t in pairs]


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def test_toolkits_load_and_are_unique():
    assert [(tk.name, tk.external) for tk in ALL_TOOLKITS] == [("coding", False), ("fr", True)]
    assert [tk.name for tk in TOOLKITS] == ["coding"]
    tools = [t.fields["public_name"] for tk in TOOLKITS for t in tk.tools]
    operations = [t.fields["operation"] for tk in TOOLKITS for t in tk.tools]
    assert len(set(tools)) == len(tools) == 11
    assert len(set(operations)) == len(operations)
    assert len(TOOLKITS[0].operations) == 39


@pytest.mark.parametrize(("tk", "tool"), TOOLS, ids=ids(TOOLS))
def test_tool_identity_and_result_schema(tk, tool):
    fields = tool.fields
    assert fields["public_name"] == tool.name and fields["public_name"].startswith(tk.name + "_")
    assert re.fullmatch(rf"{tk.name}\.[a-z][a-z0-9_]{{0,31}}", fields["operation"])
    assert fields["result_schema"] == RESULT_SCHEMA
    assert fields["entrypoint"] == "tool:main"
    limits = ToolLimits.model_validate_json(json.dumps(fields["limits"]))
    assert limits.api_calls <= 100 and limits.wall_seconds <= 300


@pytest.mark.parametrize(("tk", "tool"), TOOLS, ids=ids(TOOLS))
def test_input_schema_is_strict_public_and_catalog_valid(tk, tool):
    schema = tool.fields["input_schema"]
    assert_strict_schema(schema)
    validate_public_input(schema)
    validate_schema(schema, CONFIG)
    validate_schema(tool.fields["result_schema"], CONFIG)
    validate_schema(output_envelope(tool.fields["result_schema"]), CONFIG)
    assert "container_name" in schema["required"]
    assert schema["properties"]["container_name"]["format"] == "container-name"
    assert not any("pattern" in n for n in walk(schema))


@pytest.mark.parametrize(("tk", "tool"), TOOLS, ids=ids(TOOLS))
def test_write_tools_take_an_idempotency_key(tk, tool):
    props = tool.fields["input_schema"]["properties"]
    if tool.fields["effect"] == "writes":
        assert props["idempotency_key"]["format"] == "idempotency-key"
    mutating = {"coding_provision_repo", "coding_write_code", "coding_open_pr"}
    if tool.name in mutating:
        assert "idempotency_key" in tool.fields["input_schema"]["required"]


@pytest.mark.parametrize(("tk", "tool"), TOOLS, ids=ids(TOOLS))
def test_effects_and_allowed_operations_are_consistent(tk, tool):
    effects = {tk.operations[k].effect for k in tool.op_keys}
    if tool.fields["effect"] == "read_only":
        assert effects == {"read_only"}
    else:
        assert "writes" in effects  # a writes tool that never writes is mislabelled
    manifest = tool_manifest(tk, tool, TENANT, IMAGE)
    assert len(manifest.allowed_operations) == len(tool.op_keys)


def test_every_operation_is_valid_and_llm_free():
    tk = TOOLKITS[0]
    assert {str(o.operation_id) for o in tk.operations.values()} and len(
        {o.operation_id for o in tk.operations.values()}
    ) == len(tk.operations)
    for key, op in tk.operations.items():
        concrete = re.sub(r"\{[a-z_]+\}", "x", op.path_template)
        assert denied_by(concrete, LLM_DENY_LIST) is None, key
        assert op.path_template not in EXCLUDED_PATHS, key
        assert op.max_retries == 0 and op.idempotency == "none", key
        validate_schema(op.input_schema, CONFIG)
        validate_schema(op.output_schema, CONFIG)
        assert not any("pattern" in n for n in walk(op.input_schema)), key
        assert not any("pattern" in n for n in walk(op.output_schema)), key
        assert_strict_schema(op.input_schema)
        props = op.input_schema["properties"]
        # every input is required unless its mapping is declared optional (absent -> omitted)
        optional = {m.value.pointer[1:] for m in op.mappings if m.optional}
        assert set(op.input_schema["required"]) == set(props) - optional, key
        used = {m.value.pointer[1:] for m in op.mappings if m.value.source == "input"}
        assert used == set(props), f"{key}: inputs {set(props) ^ used} are unmapped or undeclared"
        assert op.response_projection and all(
            p == "" or p.startswith("/") for p in op.response_projection
        )
        assert all(s not in {401, 403, 407} for s in op.expected_error_statuses)
        assert op.timeout_seconds <= 300 and op.max_response_bytes <= 262144


def test_expected_statuses_the_tools_rely_on():
    ops = TOOLKITS[0].operations
    assert 404 in ops["workspace_status"].expected_error_statuses
    assert 404 in ops["security_result"].expected_error_statuses
    assert ops["import_repository"].redact_fields == ("/container/github_access_token",)
    assert ops["cycle_check"].effect == "writes" and ops["exec"].effect == "writes"


def test_exec_is_limited_to_the_exact_compile_commands():
    import importlib

    tk = TOOLKITS[0]
    (rule,) = tk.operations["exec"].scope_rules
    assert rule.kind == "set_membership" and rule.argument == "/command"
    shared = dict(tk.shared)["coding.py"]
    namespace: dict = {}
    sys.path.insert(0, str(ROOT / "toolkits" / "_kit"))
    try:
        sys.modules.pop("kit", None)
        kit_module = importlib.import_module("kit")
        sys.modules["kit"] = kit_module
        exec(compile(shared, "coding.py", "exec"), namespace)  # noqa: S102 - our own source
    finally:
        sys.path.remove(str(ROOT / "toolkits" / "_kit"))
        sys.modules.pop("kit", None)
    assert [list(v) for v in rule.values] == list(namespace["COMPILE"].values())
    assert all(isinstance(c, list) for c in rule.values)


@pytest.mark.parametrize(("tk", "tool"), TOOLS, ids=ids(TOOLS))
def test_package_uses_only_stdlib_kit_and_ops(tk, tool):
    shared = {Path(name).stem for name, _ in tk.shared}
    allowed_local = {"kit", "ops"} | shared
    op_keys = set(tk.operations)
    for entry in package_files(tk, tool):
        tree = ast.parse(entry["content"], entry["path"])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, entry["path"]
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            bad = roots - STDLIB - allowed_local
            assert not bad, f"{entry['path']} imports {bad}"
        if entry["path"] == "tool.py":
            used = {
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in op_keys
            }
            assert used <= set(tool.op_keys), f"{tool.name} names operations it may not call"
            assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == "main" for n in tree.body)


@pytest.mark.parametrize(("tk", "tool"), TOOLS, ids=ids(TOOLS))
def test_package_fits_runner_limits_and_is_deterministic(tk, tool):
    files = package_files(tk, tool)
    assert files == package_files(tk, tool)
    assert [f["path"] for f in files] == sorted(f["path"] for f in files)
    assert {"kit.py", "ops.py", "tool.py", "coding.py"} <= {f["path"] for f in files}
    assert len(files) <= 64
    assert all(len(f["content"].encode()) <= 262144 for f in files)
    assert sum(len(f["content"].encode()) for f in files) <= 1048576
    package = tuple(PackageFile(**f) for f in files)
    manifest = tool_manifest(tk, tool, TENANT, IMAGE)
    validate_package(package, manifest.entrypoint, package_digest(package), CONFIG)
    assert manifest.package_sha256 == package_digest(package)
    ops = dict(
        ast.literal_eval(
            next(f for f in files if f["path"] == "ops.py")["content"].split("OPS = ", 1)[1]
        )
    )
    assert set(ops) == set(tool.op_keys)


def test_package_digest_follows_exact_operation_versions():
    tk = TOOLKITS[0]
    tool = tk.tool("coding_repo_status")
    base = tool_manifest(tk, tool, TENANT, IMAGE).package_sha256
    refs = {k: (tk.operations[k].operation_id, 2) for k in tool.op_keys}
    bumped = tool_manifest(tk, tool, TENANT, IMAGE, op_refs=refs)
    assert bumped.package_sha256 != base
    assert all(r.version == 2 for r in bumped.allowed_operations)


# --- through the real runner protocol ----------------------------------------------------------


class Loopback:
    """The runner's reader/writer pair answered by the fake broker (a fake controller)."""

    def __init__(self, fake: FakeAPI, maximum: int) -> None:
        self.fake, self.maximum, self.frames = fake, maximum, []

    def write(self, data):
        self.frames.append(decode(data, self.maximum))

    def flush(self):
        pass

    def readline(self, _limit):
        frame = self.frames.pop()
        error = result = None
        try:
            outcome = self.fake.handle(
                frame["operation_id"], frame["arguments"], frame["idempotency_key"]
            )
            if outcome[0] == "ok":
                result = outcome[1]
            else:
                error = {"code": "http_status", "status": outcome[1], "body": outcome[2]}
        except RuntimeError:
            error = {"code": "api_call_denied"}
        return encode(
            {"type": "api_result", "sequence": frame["sequence"], "result": result, "error": error},
            self.maximum,
        )


async def run_in_runner(tmp_path, tool_name, inputs, overrides=None):
    tk = TOOLKITS[0]
    tool = tk.tool(tool_name)
    files = package_files(tk, tool)
    digest = package_digest(tuple(PackageFile(**f) for f in files))
    frame = {"files": files, "digest": digest, "entrypoint": tool.fields["entrypoint"]}
    extract(frame, tmp_path)
    for module in ("tool", "kit", "ops", "coding"):
        sys.modules.pop(module, None)
    sys.path.insert(0, str(tmp_path))
    try:
        import importlib

        function = getattr(importlib.import_module("tool"), "main")
        fake = FakeAPI(tool_name, overrides)
        loop = Loopback(fake, 262144)
        result = await function(inputs, ToolContext({}, API(loop, loop, 262144)))
    finally:
        sys.path.remove(str(tmp_path))
    return result, fake


async def test_tool_runs_end_to_end_through_the_runner_protocol(tmp_path):
    result, fake = await run_in_runner(
        tmp_path, "coding_repo_status", {"container_name": "acme-widgets"}
    )
    validate_instance(RESULT_SCHEMA, result, CONFIG)
    assert result["status"] == "ok" and result["data"]["head_sha"] == "a" * 40
    assert fake.keys == ["workspace_status", "get_graph", "git_status", "git_diff"]


async def test_expected_status_travels_as_api_status_error_through_the_protocol(tmp_path):
    result, fake = await run_in_runner(
        tmp_path,
        "coding_query_graph",
        {"container_name": "acme-widgets", "query": "nodes"},
        {"workspace_status": Status(404, {"detail": "not found"})},
    )
    assert result["status"] == "precondition_failed" and result["reason"] == "not_provisioned"


async def test_denied_status_is_a_backend_error_through_the_protocol(tmp_path):
    result, _ = await run_in_runner(
        tmp_path,
        "coding_repo_status",
        {"container_name": "acme-widgets"},
        {"workspace_status": Status(502, None)},
    )
    assert result["status"] == "backend_error"


def test_fixtures_cover_every_operation():
    tk = TOOLKITS[0]
    for key in tk.operations:
        assert fixture(key) is not None


def test_operation_call_budgets_cover_but_never_exceed_what_tools_can_call():
    """pagination_limit is the per-run call budget journal_intent enforces per operation: at least
    one call, and no more than the largest limits.api_calls of any tool that references it."""
    for tk in TOOLKITS:
        for key, op in tk.operations.items():
            users = [t for t in tk.tools if key in t.op_keys]
            assert users, f"{tk.name}.{key} is referenced by no tool"
            ceiling = max(t.fields["limits"]["api_calls"] for t in users)
            assert 1 <= op.pagination_limit <= ceiling, (
                f"{tk.name}.{key}: pagination_limit {op.pagination_limit} outside 1..{ceiling}"
            )
