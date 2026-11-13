"""Strict input schemas: definition-time lint and pre-backend validation (product plan §1)."""

import pytest

from src.toolkit_contract import (
    IDEMPOTENCY_KEY_SCHEMA,
    InputLimits,
    InputRejectedError,
    SchemaViolationError,
    assert_strict_schema,
    strict_object,
    validate_input,
)

CONTAINER = {"type": "string", "maxLength": 63, "format": "container-name"}
SCHEMA = strict_object(
    {
        "container_name": CONTAINER,
        "mode": {"enum": ["fast", "full"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "paths": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200}},
        "options": strict_object({"force": {"type": "boolean"}}, required=[]),
    },
    required=["container_name", "mode"],
    description="d",
)


def test_strict_object_defaults_to_all_required():
    schema = strict_object({"a": {"type": "boolean"}})
    assert schema == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "boolean"}},
        "required": ["a"],
    }
    assert_strict_schema(schema)


def test_complete_schema_is_strict():
    assert_strict_schema(SCHEMA)
    assert_strict_schema(strict_object({"idempotency_key": IDEMPOTENCY_KEY_SCHEMA}))


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "string", "maxLength": 3}, "root schema must be type object"),
        ({"type": "object", "properties": {}}, "additionalProperties false"),
        (
            {"type": "object", "additionalProperties": True, "properties": {}},
            "additionalProperties false",
        ),
        ({"type": "object", "additionalProperties": False}, "needs properties"),
        (strict_object({"a": {"type": "string"}}), "string needs maxLength"),
        (strict_object({"a": {"type": "integer", "minimum": 0}}), "lower and upper bounds"),
        (strict_object({"a": {"type": "number", "maximum": 1}}), "lower and upper bounds"),
        (strict_object({"a": {"type": "array", "items": {"type": "boolean"}}}), "maxItems"),
        (strict_object({"a": {"type": "array", "maxItems": 2}}), "maxItems"),
        (strict_object({"a": {"description": "untyped"}}), "type, enum or const"),
        (strict_object({"a": {"type": "date"}}), "unknown type"),
        (strict_object({"a": {"type": "string", "maxLength": 3, "format": "uri"}}), "unregistered"),
        (
            strict_object({"a": {"type": "string", "maxLength": 3, "format": ["uuid"]}}),
            "unregistered",
        ),
        (strict_object({"a": {"type": "integer", "format": "uuid"}}), "only to type string"),
        (strict_object({"a": {"type": "string", "maxLength": 3, "pattern": "^a$"}}), "pattern"),
        (strict_object({"a": {"$ref": "https://x/y"}}), "only local"),
        (strict_object({"a": {"$ref": "#/$defs/missing"}}), "unresolved"),
        (strict_object({"a": {"type": "boolean"}}, required=["b"]), "undeclared properties"),
        (strict_object({"a": True}), "subschema must be an object"),
        (strict_object({"a": strict_object({"b": {"type": "string"}})}), "/a/b: string"),
        (
            strict_object(
                {"a": {"anyOf": [{"type": "string"}, {"type": "boolean"}]}},
            ),
            "anyOf/0: string needs maxLength",
        ),
        (strict_object({"a": {"type": "integer", "minimum": 0, "maximum": "9"}}), "Draft 2020-12"),
    ],
)
def test_loose_schemas_rejected(schema, message):
    with pytest.raises(SchemaViolationError, match=message):
        assert_strict_schema(schema)


def test_refs_and_branches_are_walked():
    schema = strict_object({"a": {"$ref": "#/$defs/name"}, "b": {"anyOf": [{"const": 1}]}})
    schema["$defs"] = {"name": {"type": "string", "maxLength": 5}}
    assert_strict_schema(schema)
    schema["$defs"] = {"name": {"type": "string"}}
    with pytest.raises(SchemaViolationError, match="maxLength"):
        assert_strict_schema(schema)
    loop = strict_object({"a": {"$ref": "#/$defs/x"}})
    loop["$defs"] = {"x": {"$ref": "#/$defs/x"}}
    with pytest.raises(SchemaViolationError, match="recursive"):
        assert_strict_schema(loop)


def test_valid_input_passes_through():
    args = {"container_name": "repo-1", "mode": "fast", "paths": ["a.py"]}
    assert validate_input(SCHEMA, args) is args


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"container_name": "repo-1"}, "missing required"),
        ({"container_name": "repo-1", "mode": "fast", "tenant_id": "x"}, "undeclared properties"),
        ({"container_name": "Repo_1", "mode": "fast"}, "/container_name violates format"),
        ({"container_name": "r" * 64, "mode": "fast"}, "violates maxLength"),
        ({"container_name": "r", "mode": "slow"}, "/mode violates enum"),
        ({"container_name": "r", "mode": "fast", "limit": 0}, "/limit violates minimum"),
        ({"container_name": "r", "mode": "fast", "limit": True}, "/limit violates type"),
        ({"container_name": "r", "mode": "fast", "paths": ["a"] * 11}, "violates maxItems"),
        (
            {"container_name": "r", "mode": "fast", "options": {"force": True, "x": 1}},
            "/options has undeclared properties",
        ),
        ([], "arguments must be an object"),
    ],
)
def test_invalid_input_rejected(args, message):
    with pytest.raises(InputRejectedError, match=message) as info:
        validate_input(SCHEMA, args)
    assert str(info.value).startswith("[permanent] invalid input: ")


def test_rejection_does_not_echo_values():
    secret = "s3cr3t-value-that-must-not-leak"
    with pytest.raises(InputRejectedError) as info:
        validate_input(SCHEMA, {"container_name": secret.upper(), "mode": "fast"})
    assert secret.upper() not in str(info.value)


def test_input_size_bounded_before_schema_validation():
    with pytest.raises(InputRejectedError, match="complexity"):
        validate_input(SCHEMA, {"paths": [[[[1]]]]}, InputLimits(max_depth=2))
    with pytest.raises(InputRejectedError, match="byte limit"):
        validate_input(SCHEMA, {"container_name": "x" * 100}, InputLimits(max_bytes=50))


def test_idempotency_key_schema():
    schema = strict_object({"idempotency_key": IDEMPOTENCY_KEY_SCHEMA})
    validate_input(schema, {"idempotency_key": "run-2026-10-24:abc"})
    for bad in ("short", "has space in it", "x" * 129):
        with pytest.raises(InputRejectedError):
            validate_input(schema, {"idempotency_key": bad})
