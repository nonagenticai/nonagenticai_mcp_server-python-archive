"""Strict input schemas for first-party tools, and validation before any backend call.

A strict schema is closed and bounded everywhere: every object sets
``additionalProperties: false`` and lists only declared ``required`` keys, every string is
bounded (``maxLength``, ``enum`` or ``const``), every number has both bounds, and every array has
``items`` and ``maxItems``. ``assert_strict_schema`` enforces that at definition time, so a tool
with a loose schema cannot be constructed.

The keyword set is the catalog's closed Draft 2020-12 subset. String shape is constrained only by
``format: "<name>"`` from the registry in ``src/catalog/formats.py`` (fixed, anchored,
length-bounded validators). Raw ``pattern`` is rejected: a regex on untrusted input is a ReDoS
risk, and the catalog could not publish such a schema anyway.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator, SchemaError
from jsonschema.exceptions import best_match
from referencing import Registry

from src.catalog.formats import FORMAT_CHECKER, FORMATS
from src.catalog.validation import SUPPORTED, bounded_json

KEYWORDS = SUPPORTED
TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}

IDEMPOTENCY_KEY = "idempotency_key"
IDEMPOTENCY_KEY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 8,
    "maxLength": 128,
    "format": "idempotency-key",
    "description": "Caller-chosen key; repeating it with the same arguments replays the result.",
}


class SchemaViolationError(ValueError):
    """A tool's declared schema is not strict. Raised at definition time (a code bug)."""


class InputRejectedError(ToolError):
    """Arguments failed validation. Raised before any backend call; nothing was attempted."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"[permanent] invalid input: {detail}")


@dataclass(frozen=True)
class InputLimits:
    max_bytes: int = 262144
    max_nodes: int = 4096
    max_depth: int = 16


def strict_object(
    properties: Mapping[str, Any],
    required: Iterable[str] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Closed object schema. ``required`` defaults to every property."""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(properties if required is None else required),
    }
    if description:
        schema["description"] = description
    return schema


def _types(node: Mapping[str, Any]) -> set[str]:
    declared = node.get("type")
    if declared is None:
        return set()
    return {declared} if isinstance(declared, str) else set(declared)


def assert_strict_schema(schema: Mapping[str, Any]) -> None:
    """Raise SchemaViolationError unless ``schema`` is a closed, bounded object schema."""
    if not isinstance(schema, Mapping) or _types(schema) != {"object"}:
        raise SchemaViolationError("root schema must be type object")

    def resolve(ref: Any) -> Any:
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            raise SchemaViolationError("only local #/$defs references are allowed")
        target: Any = schema
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except KeyError, TypeError:
            raise SchemaViolationError(f"unresolved reference {ref}") from None
        return target

    def walk(node: Any, where: str, seen: frozenset[str]) -> None:
        if not isinstance(node, Mapping):
            raise SchemaViolationError(f"{where}: subschema must be an object")
        unknown = set(node) - KEYWORDS
        if unknown:
            raise SchemaViolationError(f"{where}: unsupported keywords {sorted(unknown)}")
        if "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                raise SchemaViolationError(f"{where}: recursive reference")
            walk(resolve(ref), where, seen | {ref})
            return
        for key in ("anyOf", "oneOf", "allOf"):
            for i, branch in enumerate(node.get(key, ())):
                walk(branch, f"{where}/{key}/{i}", seen)
        if "format" in node:
            fmt = node["format"]
            if not isinstance(fmt, str) or fmt not in FORMATS:
                raise SchemaViolationError(f"{where}: unregistered format {str(fmt)[:64]!r}")
            if _types(node) != {"string"}:
                raise SchemaViolationError(f"{where}: format applies only to type string")
        if "enum" in node or "const" in node:
            return
        types = _types(node)
        if not types:
            if any(k in node for k in ("anyOf", "oneOf", "allOf")):
                return
            raise SchemaViolationError(f"{where}: type, enum or const is required")
        if types - TYPES:
            raise SchemaViolationError(f"{where}: unknown type")
        if "string" in types:
            if "maxLength" not in node:
                raise SchemaViolationError(f"{where}: string needs maxLength")
        if types & {"integer", "number"}:
            if not ({"minimum", "exclusiveMinimum"} & set(node)) or not (
                {"maximum", "exclusiveMaximum"} & set(node)
            ):
                raise SchemaViolationError(f"{where}: number needs lower and upper bounds")
        if "array" in types:
            if "maxItems" not in node or "items" not in node:
                raise SchemaViolationError(f"{where}: array needs items and maxItems")
            walk(node["items"], f"{where}/items", seen)
        if "object" in types:
            if node.get("additionalProperties") is not False:
                raise SchemaViolationError(f"{where}: object needs additionalProperties false")
            properties = node.get("properties")
            if not isinstance(properties, Mapping):
                raise SchemaViolationError(f"{where}: object needs properties")
            required = node.get("required", [])
            if not isinstance(required, list) or not set(required) <= set(properties):
                raise SchemaViolationError(f"{where}: required names undeclared properties")
            for name, child in properties.items():
                walk(child, f"{where}/{name}", seen)

    walk(schema, "", frozenset())
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise SchemaViolationError(f"invalid Draft 2020-12 schema: {exc.message}") from None


def validate_input(
    schema: Mapping[str, Any], arguments: Any, limits: InputLimits = InputLimits()
) -> dict[str, Any]:
    """Return the arguments when they match; raise InputRejectedError otherwise.

    The message names the failing location and keyword, never the offending value, so a large or
    sensitive argument is not echoed back to the caller.
    """
    if not isinstance(arguments, dict):
        raise InputRejectedError("arguments must be an object")
    try:
        bounded_json(arguments, limits.max_bytes, limits.max_nodes, limits.max_depth)
    except ValueError as exc:
        raise InputRejectedError(str(exc)) from None
    validator = Draft202012Validator(
        dict(schema), registry=Registry(), format_checker=FORMAT_CHECKER
    )
    error = best_match(validator.iter_errors(arguments))
    if error is not None:
        where = "/" + "/".join(str(p) for p in error.absolute_path)
        detail = f"{where} violates {error.validator}"
        if error.validator == "additionalProperties":
            extra = sorted(set(error.instance) - set(error.schema.get("properties", {})))
            detail = f"{where} has undeclared properties {[k[:64] for k in extra[:8]]}"
        elif error.validator == "required":
            detail = f"{where} is missing required properties"
        raise InputRejectedError(detail)
    return arguments
