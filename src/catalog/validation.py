"""Closed, bounded Draft 2020-12 subset. No network or regex evaluation."""

import json
from collections import OrderedDict
from itertools import islice
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry

from src.catalog.formats import FORMAT_CHECKER, FORMATS
from src.config import Settings

DIALECT = "https://json-schema.org/draft/2020-12/schema"
SUPPORTED = {
    "$schema",
    "$defs",
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "enum",
    "const",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "title",
    "description",
    "default",
    "format",  # registered names only (src/catalog/formats.py); never raw "pattern"
}
MAPS = {"$defs", "properties"}
SINGLE = {"additionalProperties", "items", "not"}
ARRAYS = {"prefixItems", "allOf", "anyOf", "oneOf"}


def bounded_json(value: Any, max_bytes: int, max_nodes: int, max_depth: int) -> int:
    """Count before serializing, bounding recursion and aggregate validation work."""
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError("JSON complexity exceeds configured limits")
        if isinstance(item, str) and len(item) > max_bytes:
            raise ValueError("JSON string exceeds byte limit")
        if isinstance(item, dict):
            if len(item) > max_nodes:
                raise ValueError("JSON object exceeds node limit")
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, (list, tuple)):
            if len(item) > max_nodes:
                raise ValueError("JSON array exceeds node limit")
            stack.extend((v, depth + 1) for v in item)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > max_bytes:
        raise ValueError("JSON byte limit exceeded")
    return nodes


#: Successful validations by (limits, canonical schema JSON) -> expanded work. Validation is a pure
#: function of those, and published schemas are immutable, yet the catalog resolver re-validates
#: every bound tool's result schema on every request: jsonschema's meta-validation alone made
#: tools/list take > 10 s under the API's CPU limit. Failures are never cached. Bounded LRU.
_VALID: OrderedDict[tuple, int] = OrderedDict()
_VALID_MAX = 1024


def validate_schema(schema: dict, settings: Settings) -> int:
    limits = (
        settings.max_schema_bytes,
        settings.max_schema_nodes,
        settings.max_schema_depth,
        settings.max_schema_references,
    )
    try:
        key = (limits, json.dumps(schema, sort_keys=True, ensure_ascii=False, allow_nan=False))
    except TypeError, ValueError:
        key = None  # not JSON-serializable: the checks below reject it
    if key is not None and key in _VALID:
        _VALID.move_to_end(key)
        return _VALID[key]
    work = _validate_schema(schema, settings)
    if key is not None:
        _VALID[key] = work
        if len(_VALID) > _VALID_MAX:
            _VALID.popitem(last=False)
    return work


def _validate_schema(schema: dict, settings: Settings) -> int:
    bounded_json(
        schema, settings.max_schema_bytes, settings.max_schema_nodes, settings.max_schema_depth
    )
    refs = 0
    work = 0

    def walk(node: Any, depth: int, ancestors: frozenset[str]) -> None:
        nonlocal refs, work
        work += 1
        if work > settings.max_schema_nodes or depth > settings.max_schema_depth:
            raise ValueError("expanded schema exceeds complexity limits")
        if isinstance(node, bool):
            return
        if not isinstance(node, dict) or set(node) - SUPPORTED:
            raise ValueError("unsupported schema keyword")
        if "$schema" in node and node["$schema"] != DIALECT:
            raise ValueError("unsupported JSON Schema dialect")
        if "format" in node and (
            not isinstance(node["format"], str) or node["format"] not in FORMATS
        ):
            raise ValueError("unregistered string format")
        if "$ref" in node:
            refs += 1
            ref = node["$ref"]
            if (
                refs > settings.max_schema_references
                or not isinstance(ref, str)
                or not ref.startswith("#/$defs/")
            ):
                raise ValueError("only bounded bundle-local $defs references are supported")
            if ref in ancestors:
                raise ValueError("recursive schema references are unsupported")
            target: Any = schema
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except KeyError, TypeError:
                raise ValueError("unresolved local schema reference") from None
            walk(target, depth + 1, ancestors | {ref})
        for key, value in node.items():
            if key in MAPS:
                if not isinstance(value, dict):
                    raise ValueError("invalid schema map")
                for child in value.values():
                    walk(child, depth + 1, ancestors)
            elif key in SINGLE:
                walk(value, depth + 1, ancestors)
            elif key in ARRAYS:
                if not isinstance(value, list) or not 1 <= len(value) <= 8:
                    raise ValueError("schema branches must contain 1 to 8 entries")
                for child in value:
                    walk(child, depth + 1, ancestors)

    walk(schema, 0, frozenset())
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("invalid Draft 2020-12 schema") from None
    return work


MAX_PROBLEMS = 20


class InputValidationError(ValueError):
    """The instance does not match the schema. ``problems`` carries only JSON-pointer paths and
    validator keywords: never instance values and never schema text."""

    def __init__(self, problems: list[dict[str, str]]) -> None:
        super().__init__("instance does not match declared schema")
        self.problems = problems


def _pointer(parts) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _problems(errors) -> list[dict[str, str]]:
    problems = []
    for error in errors:
        path = list(error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, dict):
            # Name only the properties the schema requires and the instance lacks.
            for name in error.validator_value:
                if isinstance(name, str) and name not in error.instance:
                    problems.append({"path": _pointer([*path, name]), "rule": "required"})
        else:
            problems.append({"path": _pointer(path), "rule": str(error.validator)})
    return problems[:MAX_PROBLEMS]


def validate_instance(schema: dict, instance: Any, settings: Settings) -> None:
    schema_work = validate_schema(schema, settings)
    instance_work = bounded_json(
        instance,
        settings.max_instance_bytes,
        settings.max_instance_nodes,
        settings.max_instance_depth,
    )
    if schema_work * instance_work > settings.max_validation_work:
        raise ValueError("validation work limit exceeded")
    # Registry is closed, and validate_schema has already forbidden external IDs/references.
    validator = Draft202012Validator(schema, registry=Registry(), format_checker=FORMAT_CHECKER)
    errors = list(islice(validator.iter_errors(instance), MAX_PROBLEMS))
    if errors:
        raise InputValidationError(_problems(errors))
