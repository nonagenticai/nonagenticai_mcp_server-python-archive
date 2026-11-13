"""The hand-written coding operation manifests against the pinned live gateway spec.

Replaces the old client drift test: the toolkit's manifests are now the surface, so they are
what is checked against ``specs/coding-gateway.openapi.json``. No network.
"""

import json
import re
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.toolkits.harness import ROOT, TOOLKIT, fixture

SPEC: dict[str, Any] = json.loads((ROOT / "specs" / "coding-gateway.openapi.json").read_text())
OPS = sorted(TOOLKIT.operations)


def resolve(node: Any, seen: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in seen:
                return {}
            target: Any = SPEC
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return resolve(target, (*seen, ref))
        return {k: resolve(v, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(v, seen) for v in node]
    return node


def object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The object branch of an optional (anyOf [object, null]) request/response schema."""
    for branch in schema.get("anyOf", [schema]):
        if branch.get("type") == "object":
            return branch
    return {}


def spec_operation(key: str) -> dict[str, Any]:
    op = TOOLKIT.operations[key]
    return SPEC["paths"][op.path_template][op.method.lower()]


def success_schema(spec_op: dict[str, Any]) -> dict[str, Any]:
    for status in ("200", "201", "202"):
        if status in spec_op["responses"]:
            content = resolve(spec_op["responses"][status]).get("content", {})
            return content.get("application/json", {}).get("schema", {})
    raise AssertionError("no success response")


@pytest.mark.parametrize("key", OPS)
def test_method_path_and_parameters_exist_in_the_pinned_spec(key):
    op = TOOLKIT.operations[key]
    assert op.path_template in SPEC["paths"], op.path_template
    spec_op = SPEC["paths"][op.path_template][op.method.lower()]
    params = [resolve(p) for p in spec_op.get("parameters", [])]
    assert set(op.query_fields) <= {p["name"] for p in params if p["in"] == "query"}
    path_names = set(re.findall(r"\{([a-z_]+)\}", op.path_template))
    assert path_names <= {p["name"] for p in params if p["in"] == "path"}
    assert spec_op.get("x-operation-role") in {"producer", "observer"}
    # a mutating gateway operation must not be modelled as read-only
    if spec_op["x-operation-role"] == "producer":
        assert op.effect == "writes" or key in {"security_async"}, key


@pytest.mark.parametrize("key", OPS)
def test_body_targets_exist_in_the_request_schema(key):
    op = TOOLKIT.operations[key]
    body = [m for m in op.mappings if m.location == "body"]
    if not body:
        return
    spec_op = spec_operation(key)
    request = resolve(spec_op["requestBody"]["content"]["application/json"]["schema"])
    properties = object_schema(request).get("properties", {})
    for mapping in body:
        if mapping.target == "":
            continue
        assert mapping.target.split("/")[1] in properties, f"{key}: {mapping.target}"
        if mapping.value.source == "literal":
            schema = properties[mapping.target.split("/")[1]]
            Draft202012Validator(schema).validate(mapping.value.value)


@pytest.mark.parametrize("key", OPS)
def test_literal_query_values_are_valid_for_the_spec_parameter(key):
    op = TOOLKIT.operations[key]
    params = {p["name"]: p for p in map(resolve, spec_operation(key).get("parameters", []))}
    for mapping in op.mappings:
        if mapping.location == "query" and mapping.value.source == "literal":
            value = mapping.value.value
            if isinstance(value, bool) or value in ("true", "false"):
                continue  # the broker stringifies; FastAPI parses "true"/"false"
            Draft202012Validator(params[mapping.target]["schema"]).validate(value)


@pytest.mark.parametrize("key", OPS)
def test_output_contract_and_projection_match_the_spec(key):
    op = TOOLKIT.operations[key]
    schema = success_schema(spec_operation(key))
    declared = object_schema(schema) if schema.get("type") != "array" else {}
    if schema.get("type") == "array":
        assert op.output_schema["type"] == "array"
        return
    properties = declared.get("properties")
    if not properties:  # untyped in the spec: nothing to cross-check beyond the fixture
        return
    required = set(declared.get("required", []))
    # a tool may only require what the spec guarantees
    assert set(op.output_schema.get("required", [])) <= required, key
    for pointer in op.response_projection:
        if pointer == "":
            continue
        head = pointer.split("/")[1]
        assert head in properties, f"{key}: projection {pointer} not in the spec response"


@pytest.mark.parametrize("key", OPS)
def test_fixture_bodies_match_the_spec_response_shape(key):
    schema = success_schema(spec_operation(key))
    Draft202012Validator(schema).validate(fixture(key))


def test_projected_pointers_are_always_present_in_the_spec():
    """A projected pointer the gateway may omit would turn a good answer into a denial."""
    for key, op in TOOLKIT.operations.items():
        schema = success_schema(spec_operation(key))
        declared = object_schema(schema)
        required = set(declared.get("required", []))
        for pointer in op.response_projection:
            if pointer and declared.get("properties"):
                head = pointer.split("/")[1]
                assert head in required, f"{key}: {pointer} is not a required response field"
