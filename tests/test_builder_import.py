"""OpenAPI 3.0/3.1 importer: $ref, normalisation, parameters, responses, deny list."""

import copy
import json
from pathlib import Path
from uuid import uuid7

import pytest

from src.builder.openapi import OpenApiImportError, import_openapi, list_operations, spec_info
from src.catalog.models import ExactReference
from src.catalog.validation import validate_schema
from tests.fixtures import settings

SERVICE = ExactReference(id=uuid7(), version=1)
REAL = Path(__file__).parent.parent / "specs" / "coding-gateway.openapi.json"
JSON = "application/json"


def body(schema):
    return {"content": {JSON: {"schema": schema}}}


FASTAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Items", "version": "2"},
    "paths": {
        "/items/{itemId}": {
            "parameters": [
                {"name": "itemId", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "get": {
                "operationId": "readItem",
                "summary": "Read",
                "parameters": [
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "X-Tenant-ID", "in": "header", "schema": {"type": "string"}},
                    {"$ref": "#/components/parameters/Trace"},
                ],
                "responses": {
                    "422": body({"type": "object"}),
                    "200": body({"$ref": "#/components/schemas/Item"}),
                },
            },
            "delete": {"operationId": "dropItem", "responses": {"204": {"description": "gone"}}},
        },
        "/items": {
            "post": {
                "operationId": "makeItem",
                "requestBody": {"required": True, **body({"$ref": "#/components/schemas/Item"})},
                "responses": {"201": body({"$ref": "#/components/schemas/Item"})},
            },
            "put": {
                "operationId": "optBody",
                "requestBody": body({"type": "object"}),
                "responses": {"200": body({"type": "object"})},
            },
        },
        "/tree": {
            "get": {
                "operationId": "tree",
                "responses": {"200": body({"$ref": "#/components/schemas/Node"})},
            }
        },
        "/api/ai/chat": {"get": {"operationId": "chat", "responses": {"200": body({})}}},
        "/c": {
            "get": {
                "operationId": "cookie",
                "parameters": [{"name": "s", "in": "cookie", "schema": {"type": "string"}}],
                "responses": {"200": body({})},
            }
        },
        "/h": {
            "get": {
                "operationId": "hdr",
                "parameters": [
                    {
                        "name": "Authorization",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": body({})},
            }
        },
        "/form": {
            "post": {
                "operationId": "form",
                "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                "responses": {"200": body({})},
            }
        },
        "/ext": {
            "get": {"operationId": "ext", "responses": {"200": body({"$ref": "http://x/y#/z"})}}
        },
    },
    "components": {
        "parameters": {"Trace": {"name": "x-trace", "in": "header", "schema": {"type": "string"}}},
        "schemas": {
            "Item": {
                "type": "object",
                "required": ["name", "id"],
                "properties": {
                    "id": {"type": "integer", "readOnly": True},
                    "name": {"type": "string", "pattern": "^a", "examples": ["x"]},
                    "secret": {"type": "string", "writeOnly": True},
                },
            },
            "Node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/components/schemas/Node"}},
            },
        },
    },
}

LEGACY = {
    "openapi": "3.0.3",
    "info": {"title": "Old", "version": "1"},
    "paths": {
        "/n": {
            "get": {
                "operationId": "n",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 0, "exclusiveMinimum": True},
                    }
                ],
                "responses": {
                    "200": body(
                        {
                            "type": "object",
                            "properties": {
                                "a": {"type": "string", "nullable": True, "example": "z"},
                                "b": {
                                    "allOf": [{"$ref": "#/components/schemas/B"}],
                                    "nullable": True,
                                },
                            },
                            "discriminator": {"propertyName": "a"},
                        }
                    )
                },
            }
        }
    },
    "components": {"schemas": {"B": {"type": "string", "enum": ["x"], "nullable": True}}},
}


def rows(doc):
    return {r["operationId"]: r for r in list_operations(doc)}


def one(doc, op, **kw):
    return import_openapi(doc, SERVICE, [op], settings(), **kw)[0]


def test_listing_marks_importable_and_reasons():
    listed = rows(FASTAPI)
    assert listed["readItem"]["importable"] and listed["readItem"]["summary"] == "Read"
    assert "recursive" in listed["tree"]["reason"]
    assert "deny list" in listed["chat"]["reason"]
    assert "cookie" in listed["cookie"]["reason"]
    assert "required" in listed["hdr"]["reason"]
    assert "JSON" in listed["form"]["reason"]
    assert "external" in listed["ext"]["reason"]
    assert spec_info(FASTAPI) == {"title": "Items", "version": "2"}


def test_read_operation_refs_defs_params_and_readonly():
    op = one(FASTAPI, "readItem")
    m = op.manifest
    assert op.op_key == "read_item" and m.method == "GET" and m.effect == "read_only"
    assert m.path_template == "/items/{item_id}"
    props = m.input_schema["properties"]
    assert set(props) == {"item_id", "verbose", "x_trace"}  # reserved optional header dropped
    assert (
        m.input_schema["required"] == ["item_id"]
        and m.input_schema["additionalProperties"] is False
    )
    optional = {x.target: x.optional for x in m.mappings}
    assert optional == {"item_id": False, "verbose": True, "x_trace": True}
    out = m.output_schema
    assert out["$ref"] == "#/$defs/Item"
    item = out["$defs"]["Item"]
    assert "pattern" not in item["properties"]["name"]
    assert set(item["properties"]) == {"id", "name"}  # writeOnly dropped on output
    assert item["required"] == ["name", "id"]
    validate_schema(m.input_schema, settings())
    validate_schema(out, settings())


def test_write_operation_body_required_and_input_direction():
    m = one(FASTAPI, "makeItem").manifest
    assert m.effect == "writes" and m.input_schema["required"] == ["body"]
    item = m.input_schema["$defs"]["Item"]
    assert (
        "id" not in item["properties"] and "secret" in item["properties"]
    )  # readOnly dropped on input
    assert item["required"] == ["name"]
    assert [x.optional for x in m.mappings] == [False]
    assert m.output_schema["$defs"]["Item"]["properties"].keys() == {"id", "name"}


def test_optional_body_and_204():
    assert one(FASTAPI, "optBody").manifest.mappings[0].optional is True
    drop = one(FASTAPI, "dropItem").manifest
    assert drop.empty_response and drop.effect == "writes" and drop.method == "DELETE"
    assert one(FASTAPI, "makeItem").manifest.empty_response is False  # 201 JSON


def test_selection_by_method_path_and_overrides():
    op = import_openapi(
        FASTAPI,
        SERVICE,
        [{"method": "get", "path": "/items/{itemId}"}],
        settings(),
        {
            "op_key": "fetch",
            "expected_error_statuses": [404],
            "timeout_seconds": 5,
            "response_projection": [""],
        },
    )[0]
    assert op.op_key == "fetch" and op.manifest.expected_error_statuses == (404,)
    assert op.manifest.timeout_seconds == 5
    two = import_openapi(
        FASTAPI, SERVICE, ["readItem", "makeItem"], settings(), {"readItem": {"timeout_seconds": 3}}
    )
    assert [o.manifest.timeout_seconds for o in two] == [3, 10]
    with pytest.raises(OpenApiImportError):
        import_openapi(FASTAPI, SERVICE, ["readItem", "makeItem"], settings(), {"op_key": "x"})


@pytest.mark.parametrize("name", ["tree", "chat", "cookie", "hdr", "form", "ext"])
def test_refused_operations_cannot_be_selected(name):
    with pytest.raises(OpenApiImportError):
        one(FASTAPI, name)


def test_unknown_selection_and_bad_documents():
    with pytest.raises(OpenApiImportError):
        one(FASTAPI, "nope")
    with pytest.raises(OpenApiImportError):
        import_openapi(FASTAPI, SERVICE, [], settings())
    for bad in ({"swagger": "2.0"}, {"openapi": "3.2.0", "paths": {}}, {"openapi": "3.0.0"}):
        with pytest.raises(OpenApiImportError):
            list_operations(bad)


def test_openapi_30_normalisation():
    m = one(LEGACY, "n").manifest
    assert m.input_schema["properties"]["limit"] == {"type": "integer", "exclusiveMinimum": 0}
    out = m.output_schema
    assert out["properties"]["a"] == {"type": ["string", "null"]}
    assert out["properties"]["b"]["anyOf"][0] == {"allOf": [{"$ref": "#/$defs/B"}]}
    assert out["$defs"]["B"] == {"type": ["string", "null"], "enum": ["x", None]}
    assert "discriminator" not in out
    validate_schema(out, settings())


def test_real_gateway_spec():
    doc = json.loads(REAL.read_text())
    listed = list_operations(doc, settings())
    assert len(listed) > 300
    good = [r for r in listed if r["importable"]]
    assert 20 < len(good) < len(listed)
    assert all(r["reason"] for r in listed if not r["importable"])
    assert any("deny list" in (r["reason"] or "") for r in listed)
    imported = import_openapi(
        doc, SERVICE, [{"method": r["method"].lower(), "path": r["path"]} for r in good], settings()
    )
    assert len(imported) == len(good)
    assert len({o.op_key for o in imported}) == len(good)
    assert not any(o.manifest.path_template.startswith("/api/ai") for o in imported)


def test_input_not_mutated():
    before = copy.deepcopy(FASTAPI)
    one(FASTAPI, "readItem")
    list_operations(FASTAPI)
    assert FASTAPI == before
