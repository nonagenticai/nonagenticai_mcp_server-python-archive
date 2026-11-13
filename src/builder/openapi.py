"""OpenAPI 3.0.x / 3.1.x (JSON) -> draft ``OperationManifest`` importer.

Pure: no network, no database. Local ``$ref`` only. ``components/schemas`` become ``$defs`` of the
operation's own schemas (only the reachable ones); every other component is inlined. 3.0 schemas
are normalised to the 2020-12 subset the catalog validates (``src.catalog.validation``): ``nullable``
and boolean ``exclusive*`` are converted, annotation-only keywords are dropped, and keywords the
validator cannot enforce (``pattern`` etc.) are dropped as well, so the upstream stays the
authority for them. Anything that cannot be represented exactly makes ONE operation
non-importable with a short, safe reason; it never fails the whole document.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid7

from pydantic import ValidationError

from src.catalog.formats import FORMATS
from src.catalog.models import NAME_PATTERN, ExactReference, OperationManifest
from src.catalog.validation import validate_schema
from src.toolkit_contract.endpoints import denied_by

METHODS = ("get", "post", "put", "patch", "delete")
SCALARS = {"string", "integer", "number", "boolean"}
RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "proxy-authorization",
        "x-tenant-id",
        "connection",
        "content-length",
        "content-type",
        "accept",
        "transfer-encoding",
        "idempotency-key",
        "x-idempotency-key",
        "user-agent",
    }
)
OVERRIDE_FIELDS = frozenset(
    {"op_key", "effect", "expected_error_statuses", "response_projection", "timeout_seconds"}
)
#: Validation keywords the catalog subset supports and that are kept.
KEEP = frozenset(
    {
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
        "format",
    }
)
#: Keywords that change what a schema means and cannot be dropped: refuse the operation.
STRUCTURAL = frozenset(
    {"$id", "$anchor", "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$defs", "definitions"}
)
MAX_DEPTH = 48
MAX_DESCRIPTION = 300
DEFAULT_TIMEOUT = 10
DEFAULT_PAGINATION = 10
DEFAULT_RESPONSE_BYTES = 262144


class OpenApiImportError(ValueError):
    """The document or a selected operation cannot be imported. ``reason`` is safe to show."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _RefusedError(Exception):
    """One operation is not importable."""


@dataclass(frozen=True, slots=True)
class ImportedOperation:
    op_key: str
    manifest: OperationManifest
    operation_id: str | None  # the document's operationId
    method: str
    path: str
    summary: str


def snake(name: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not text or not text[0].isalpha():
        text = "p_" + text
    return text[:64].rstrip("_") or "p"


def _unescape(part: str) -> str:
    return part.replace("~1", "/").replace("~0", "~")


def document_version(doc: Any) -> str:
    if type(doc) is not dict or type(doc.get("openapi")) is not str:
        raise OpenApiImportError("not an OpenAPI document")
    version = doc["openapi"]
    if not re.fullmatch(r"3\.[01]\.\d+(?:-[A-Za-z0-9.]+)?", version):
        raise OpenApiImportError("only OpenAPI 3.0.x and 3.1.x are supported")
    if type(doc.get("paths")) is not dict:
        raise OpenApiImportError("paths are required")
    return version


def spec_info(doc: dict) -> dict[str, str | None]:
    info: dict = doc["info"] if type(doc.get("info")) is dict else {}
    title, version = info.get("title"), info.get("version")
    return {
        "title": title[:200] if type(title) is str else None,
        "version": version[:64] if type(version) is str else None,
    }


class _Converter:
    """Schema conversion for ONE direction of ONE operation, collecting its ``$defs``."""

    def __init__(self, doc: dict, direction: str) -> None:
        self.doc, self.direction = doc, direction
        self.defs: dict[str, Any] = {}
        self.names: dict[str, str] = {}
        self.inline: list[str] = []

    def pointer(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise _RefusedError("external $ref is not supported")
        target: Any = self.doc
        for part in ref[2:].split("/"):
            try:
                target = target[_unescape(part)]
            except KeyError, TypeError:
                raise _RefusedError("unresolved $ref") from None
        return target

    def reference(self, ref: Any) -> dict:
        if type(ref) is not str:
            raise _RefusedError("invalid $ref")
        match = re.fullmatch(r"#/components/schemas/([^/]+)", ref)
        if match is None:
            if ref in self.inline or len(self.inline) > 16:
                raise _RefusedError("recursive $ref outside components/schemas")
            target = self.pointer(ref)
            self.inline.append(ref)
            try:
                return self.schema(target, 0)
            finally:
                self.inline.pop()
        name = _unescape(match.group(1))
        if name not in self.names:
            base = re.sub(r"[^A-Za-z0-9_]", "_", name) or "schema"
            candidate, n = base, 1
            while candidate in self.defs:
                n += 1
                candidate = f"{base}_{n}"
            self.names[name] = candidate
            self.defs[candidate] = {}
            target = self.pointer(ref)
            self.defs[candidate] = self.schema(target, 0)
        return {"$ref": "#/$defs/" + self.names[name]}

    def schema(self, node: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise _RefusedError("schema nesting is too deep")
        if type(node) is bool:
            return node
        if type(node) is not dict:
            raise _RefusedError("invalid schema")
        if bad := STRUCTURAL & set(node):
            raise _RefusedError(f"unsupported schema keyword {sorted(bad)[0]}")
        out: dict[str, Any] = {}
        if "$ref" in node:
            out = self.reference(node["$ref"])
            # 3.1 allows siblings; an annotation-only wrapper is enough to keep.
            if len(out) == 1 and "$ref" in out:
                for key in ("title", "description"):
                    if key in node:
                        out[key] = self.annotation(node[key], key)
            if node.get("nullable") is True:
                out = {"anyOf": [out, {"type": "null"}]}
            return out
        for key, value in node.items():
            if key not in KEEP:
                continue
            if key in ("title", "description"):
                out[key] = self.annotation(value, key)
            elif key == "properties":
                out[key] = self.properties(value, depth)
            elif key in ("additionalProperties", "items", "not"):
                out[key] = self.schema(value, depth + 1)
            elif key in ("prefixItems", "allOf", "anyOf", "oneOf"):
                if type(value) is not list:
                    raise _RefusedError("invalid schema")
                out[key] = [self.schema(v, depth + 1) for v in value]
            elif key == "format":
                if value in FORMATS:
                    out[key] = value
            elif key in ("exclusiveMinimum", "exclusiveMaximum"):
                bound = "minimum" if key == "exclusiveMinimum" else "maximum"
                if type(value) is bool:  # 3.0: a modifier of minimum/maximum
                    if value and type(node.get(bound)) in {int, float}:
                        out[key] = node[bound]
                        out.pop(bound, None)
                else:
                    out[key] = value
            elif key in ("minimum", "maximum"):
                excl = node.get("exclusiveMinimum" if key == "minimum" else "exclusiveMaximum")
                if excl is not True:
                    out[key] = value
            else:
                out[key] = value
        if type(out.get("required")) is list and "properties" in out:
            # A property dropped for the direction (readOnly/writeOnly) is no longer required.
            kept = out["properties"]
            out["required"] = [
                r for r in out["required"] if r in kept or r not in node["properties"]
            ]
        if node.get("nullable") is True:
            kind = out.get("type")
            if type(kind) is str:
                out["type"] = [kind, "null"]
            elif type(kind) is list:
                out["type"] = [*kind, "null"] if "null" not in kind else kind
            else:
                out = {"anyOf": [out, {"type": "null"}]}
            if "enum" in out and None not in out["enum"] and "anyOf" not in out:
                out["enum"] = [*out["enum"], None]
        return out

    @staticmethod
    def annotation(value: Any, key: str) -> str:
        if type(value) is not str:
            raise _RefusedError("invalid schema")
        return value[: 120 if key == "title" else MAX_DESCRIPTION]

    def properties(self, value: Any, depth: int) -> dict:
        if type(value) is not dict:
            raise _RefusedError("invalid schema")
        result = {}
        for name, child in value.items():
            if type(child) is dict:
                if (self.direction == "input" and child.get("readOnly") is True) or (
                    self.direction == "output" and child.get("writeOnly") is True
                ):
                    continue
            result[name] = self.schema(child, depth + 1)
        return result

    def finish(self, root: Any) -> dict:
        schema = root if type(root) is dict else {}
        # Reachable defs only, and no cycles (the catalog validator forbids recursive refs).
        graph = {
            name: set(re.findall(r'"#/\$defs/([A-Za-z0-9_]+)"', json.dumps(body)))
            for name, body in self.defs.items()
        }
        state: dict[str, int] = {}

        def visit(name: str) -> None:
            if state.get(name) == 1:
                raise _RefusedError("recursive schemas are not supported")
            if state.get(name) == 2:
                return
            state[name] = 1
            for child in graph.get(name, ()):
                visit(child)
            state[name] = 2

        for name in graph:
            visit(name)
        result = dict(schema)
        if self.defs:
            result["$defs"] = self.defs
        return result


def _deref(doc: dict, node: Any) -> Any:
    seen = 0
    while type(node) is dict and "$ref" in node:
        seen += 1
        ref = node["$ref"]
        if seen > 16 or type(ref) is not str or not ref.startswith("#/"):
            raise _RefusedError("unsupported $ref")
        target: Any = doc
        for part in ref[2:].split("/"):
            try:
                target = target[_unescape(part)]
            except KeyError, TypeError:
                raise _RefusedError("unresolved $ref") from None
        node = target
    return node


def _scalar_schema(doc: dict, schema: Any) -> bool:
    node = _deref(doc, schema)
    if type(node) is not dict:
        return True
    kind = node.get("type")
    kinds = {kind} if type(kind) is str else set(kind) if type(kind) is list else set()
    return not (kinds & {"array", "object"})


def _json_content(content: Any) -> Any:
    """The schema of the preferred JSON media type, ``False`` when there is none."""
    if type(content) is not dict:
        return False
    for media, body in content.items():
        if type(media) is str and (
            media.split(";")[0].strip().lower() == "application/json"
            or re.fullmatch(r"application/[a-z0-9.+-]+\+json", media.split(";")[0].strip().lower())
        ):
            return body.get("schema", {}) if type(body) is dict else {}
    return False


@dataclass(frozen=True, slots=True)
class _Built:
    method: str
    path_template: str
    query_fields: tuple[str, ...]
    header_fields: tuple[str, ...]
    mappings: tuple[dict, ...]
    input_schema: dict
    output_schema: dict
    empty_response: bool
    effect: str


def _build(doc: dict, path: str, method: str, item: dict, operation: dict) -> _Built:
    if denied := denied_by(path):
        raise _RefusedError(f"blocked by the LLM endpoint deny list ({denied.classification})")
    params: dict[tuple[str, str], dict] = {}
    for raw in [*(item.get("parameters") or []), *(operation.get("parameters") or [])]:
        param = _deref(doc, raw)
        if type(param) is not dict or type(param.get("name")) is not str:
            raise _RefusedError("invalid parameter")
        params[(param["name"], str(param.get("in")))] = param
    inputs = _Converter(doc, "input")
    properties: dict[str, Any] = {}
    required: list[str] = []
    mappings: list[dict] = []
    query_fields: list[str] = []
    header_fields: list[str] = []
    template = path
    tokens = re.findall(r"\{([^{}]+)\}", path)
    declared = {name for name, where in params if where == "path"}
    if set(tokens) != declared:
        raise _RefusedError("path parameters do not match the path template")
    ordered = sorted(
        params.items(),
        key=lambda kv: (
            ("path", "query", "header").index(kv[0][1])
            if kv[0][1] in ("path", "query", "header")
            else 9
        ),
    )
    for (name, where), param in ordered:
        if where == "cookie":
            raise _RefusedError("cookie parameters are not supported")
        if where not in ("path", "query", "header"):
            raise _RefusedError("unsupported parameter location")
        if "content" in param or "schema" not in param:
            raise _RefusedError("parameter without a plain schema is not supported")
        if not _scalar_schema(doc, param["schema"]):
            raise _RefusedError(f"{where} parameter {name!r} must be a scalar")
        is_required = where == "path" or param.get("required") is True
        if where == "path":
            prop = snake(name)
            if not re.fullmatch(NAME_PATTERN, prop):
                raise _RefusedError("unrepresentable path parameter name")
            template = template.replace("{" + name + "}", "{" + prop + "}")
            target = prop
        elif where == "query":
            if not re.fullmatch(NAME_PATTERN, name):
                raise _RefusedError(f"query parameter {name!r} needs a lowercase snake_case name")
            prop = target = name
            query_fields.append(name)
        else:
            wire = name.lower()
            if wire in RESERVED_HEADERS or wire.startswith(("x-forwarded-", "proxy-")):
                if param.get("required") is True:
                    raise _RefusedError(f"reserved header {wire!r} is required")
                continue  # the platform sets reserved headers; an optional one is dropped
            if "_" in name:
                raise _RefusedError("header names with underscores are not supported")
            target = wire.replace("-", "_")
            if not re.fullmatch(NAME_PATTERN, target):
                raise _RefusedError(f"unrepresentable header name {name!r}")
            prop = target
            header_fields.append(target)
        if prop in properties:
            prop = f"{prop}_{where}"
            if prop in properties:
                raise _RefusedError("duplicate parameter name")
        properties[prop] = inputs.schema(param["schema"], 0)
        if "description" in param and type(properties[prop]) is dict:
            properties[prop].setdefault("description", param["description"][:MAX_DESCRIPTION])
        if is_required:
            required.append(prop)
        mappings.append(
            {
                "location": where,
                "target": target,
                "value": {"source": "input", "pointer": "/" + prop},
                "optional": not is_required,
            }
        )
    if "requestBody" in operation:
        body = _deref(doc, operation["requestBody"])
        schema = _json_content(body.get("content") if type(body) is dict else None)
        if schema is False:
            raise _RefusedError("only JSON request bodies are supported")
        prop = "body" if "body" not in properties else "request_body"
        if prop in properties:
            raise _RefusedError("request body name collision")
        properties[prop] = inputs.schema(schema, 0)
        body_required = body.get("required") is True
        if body_required:
            required.append(prop)
        mappings.append(
            {
                "location": "body",
                "target": "",
                "value": {"source": "input", "pointer": "/" + prop},
                "optional": not body_required,
            }
        )
    input_schema = inputs.finish(
        {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
    )
    # Lowest 2xx in 200-204; JSON gives the schema, 204/no content gives an empty response.
    responses = operation.get("responses")
    if type(responses) is not dict:
        raise _RefusedError("no responses declared")
    codes = sorted(c for c in responses if re.fullmatch(r"20[0-4]", str(c)))
    if not codes:
        raise _RefusedError("no 200-204 response")
    response = _deref(doc, responses[codes[0]])
    content = response.get("content") if type(response) is dict else None
    outputs = _Converter(doc, "output")
    if codes[0] == "204" or not content:
        output_schema, empty = {"type": "object"}, True
    else:
        schema = _json_content(content)
        if schema is False:
            raise _RefusedError("the success response is not JSON")
        output_schema, empty = outputs.finish(outputs.schema(schema, 0)), False
    return _Built(
        method=method.upper(),
        path_template=template,
        query_fields=tuple(query_fields),
        header_fields=tuple(header_fields),
        mappings=tuple(mappings),
        input_schema=input_schema,
        output_schema=output_schema,
        empty_response=empty,
        effect="read_only" if method == "get" else "writes",
    )


def _operations(doc: dict) -> Iterable[tuple[str, str, dict, dict | None, str | None]]:
    """(method, path, path item, operation or None, refusal) for every candidate in a document."""
    for path in sorted(doc["paths"]):
        item = doc["paths"][path]
        if type(item) is not dict:
            continue
        item = item if "$ref" not in item else _deref_safe(doc, item)
        if type(item) is not dict:
            continue
        for method in ("get", "put", "post", "delete", "options", "head", "patch", "trace"):
            operation = item.get(method)
            if operation is None:
                continue
            if type(operation) is not dict:
                yield method, path, item, None, "invalid operation"
            elif method not in METHODS:
                yield method, path, item, operation, f"{method.upper()} is not supported"
            else:
                yield method, path, item, operation, None


def _deref_safe(doc: dict, node: dict) -> Any:
    try:
        return _deref(doc, node)
    except _RefusedError:
        return None


def _make(
    doc: dict,
    method: str,
    path: str,
    item: dict,
    operation: dict,
    service: ExactReference | None,
    settings: Any,
    overrides: Mapping[str, Any],
) -> tuple[_Built, OperationManifest | None]:
    built = _build(doc, path, method, item, operation)
    if overrides.get("effect") in ("read_only", "writes"):
        built = _replace_effect(built, overrides["effect"])
    if settings is not None:
        try:
            validate_schema(built.input_schema, settings)
            validate_schema(built.output_schema, settings)
        except ValueError as error:
            raise _RefusedError(f"schema not supported: {error}") from None
    if service is None:
        return built, None
    payload: dict[str, Any] = {
        "operation_id": str(uuid7()),
        "version": 1,
        "service": service.model_dump(mode="json"),
        "method": built.method,
        "path_template": built.path_template,
        "query_fields": built.query_fields,
        "header_fields": built.header_fields,
        "mappings": built.mappings,
        "input_schema": built.input_schema,
        "output_schema": built.output_schema,
        "response_projection": list(overrides.get("response_projection") or [""]),
        "effect": built.effect,
        "timeout_seconds": overrides.get("timeout_seconds", DEFAULT_TIMEOUT),
        "expected_error_statuses": list(overrides.get("expected_error_statuses") or []),
        "empty_response": built.empty_response,
        "pagination_limit": DEFAULT_PAGINATION,
        "max_response_bytes": DEFAULT_RESPONSE_BYTES,
    }
    try:
        return built, OperationManifest.model_validate_json(json.dumps(payload))
    except ValidationError as error:
        first = error.errors()[0]
        raise _RefusedError(f"invalid operation: {first['msg']}") from None


def _replace_effect(built: _Built, effect: str) -> _Built:
    return replace(built, effect=effect)


def list_operations(doc: dict, settings: Any = None) -> list[dict]:
    """Every operation of a spec, for the preview: whether it can be imported, and if not why."""
    document_version(doc)
    result = []
    for method, path, item, operation, refusal in _operations(doc):
        reason = refusal
        if reason is None and operation is not None:
            try:
                _make(doc, method, path, item, operation, None, settings, {})
            except _RefusedError as error:
                reason = str(error)
        op_id = operation.get("operationId") if operation else None
        summary = operation.get("summary") if operation else None
        result.append(
            {
                "method": method.upper(),
                "path": path,
                "operationId": op_id if type(op_id) is str else None,
                "summary": summary[:200] if type(summary) is str else None,
                "importable": reason is None,
                "reason": reason,
            }
        )
    return result


def _selector(entry: Any) -> tuple[str | None, str | None, str | None]:
    if type(entry) is str:
        return entry, None, None
    if type(entry) is dict and type(entry.get("method")) is str and type(entry.get("path")) is str:
        return None, entry["method"].lower(), entry["path"]
    raise OpenApiImportError("a selection is an operationId or {method, path}")


def _overrides_for(
    overrides: Mapping[str, Any] | None, selected: int, label: str, op_id: str | None
) -> dict:
    if not overrides:
        return {}
    if set(overrides) <= OVERRIDE_FIELDS:
        if "op_key" in overrides and selected != 1:
            raise OpenApiImportError("op_key can only be overridden when one operation is selected")
        return dict(overrides)
    # Per-operation form: {"<operationId>" | "METHOD /path": {...}}
    found = overrides.get(op_id) if op_id else None
    found = found if found is not None else overrides.get(label)
    if found is None:
        return {}
    if type(found) is not dict or set(found) - OVERRIDE_FIELDS:
        raise OpenApiImportError("unknown override field")
    return dict(found)


def import_openapi(
    doc: dict,
    service_ref: ExactReference,
    selection: Iterable[Any],
    settings: Any,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[ImportedOperation, ...]:
    """Draft ``OperationManifest`` per selected operation (``operationId`` or ``{method, path}``).

    ``overrides`` is one dict of {op_key, effect, expected_error_statuses, response_projection,
    timeout_seconds} for every selected operation (``op_key`` needs exactly one selection), or a
    mapping from ``operationId`` / ``"METHOD /path"`` to such a dict.
    """
    document_version(doc)
    wanted = [_selector(e) for e in selection]
    if not wanted:
        raise OpenApiImportError("select at least one operation")
    index = {(m, p): (item, op, refusal) for m, p, item, op, refusal in _operations(doc)}
    by_id: dict[str, tuple[str, str]] = {}
    for (m, p), (_, op, _r) in index.items():
        if op and type(op.get("operationId")) is str:
            by_id.setdefault(op["operationId"], (m, p))
    chosen: list[tuple[str, str]] = []
    for op_id, method, path in wanted:
        key = by_id.get(op_id) if op_id is not None else None
        if op_id is None and method is not None and path is not None:
            key = (method, path)
        if key is None or key not in index:
            raise OpenApiImportError("operation not found in the spec")
        if key not in chosen:
            chosen.append(key)
    result, used = [], set()
    for method, path in chosen:
        item, operation, refusal = index[(method, path)]
        label = f"{method.upper()} {path}"
        if refusal or operation is None:
            raise OpenApiImportError(f"{label} cannot be imported: {refusal}")
        op_id = operation.get("operationId") if type(operation.get("operationId")) is str else None
        override = _overrides_for(overrides, len(chosen), label, op_id)
        try:
            built, manifest = _make(
                doc, method, path, item, operation, service_ref, settings, override
            )
        except _RefusedError as error:
            raise OpenApiImportError(f"{label} cannot be imported: {error}") from None
        assert manifest is not None
        key = override.get("op_key") or snake(op_id or f"{method}_{path}")
        if not re.fullmatch(NAME_PATTERN, key):
            raise OpenApiImportError("op_key must be lowercase snake_case, 64 characters or fewer")
        base, n = key, 1
        while key in used:
            n += 1
            key = f"{base[:60]}_{n}"
        used.add(key)
        summary = operation.get("summary")
        result.append(
            ImportedOperation(
                key,
                manifest,
                op_id,
                built.method,
                path,
                summary[:200] if type(summary) is str else "",
            )
        )
    return tuple(result)
