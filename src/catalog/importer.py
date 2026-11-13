"""Bounded OpenAPI 3.1 draft import. Publication still requires reviewed scope policy."""

from uuid import uuid7

from pydantic import Field

from src.catalog.models import Contract, ExactReference, OperationManifest
from src.catalog.validation import validate_schema


class OperationImport(Contract):
    service: ExactReference
    document: dict
    max_operations: int = Field(default=32, ge=1, le=128)


def import_drafts(payload, settings):
    document = payload.document
    if document.get("openapi") != "3.1.0" or set(document) - {"openapi", "info", "paths"}:
        raise ValueError("v1 importer requires a bundled OpenAPI 3.1 document without references")
    paths = document.get("paths")
    if type(paths) is not dict:
        raise ValueError("paths required")
    result = []
    for path, methods in paths.items():
        if type(methods) is not dict:
            raise ValueError("invalid path")
        for method, definition in methods.items():
            if (
                method not in {"get", "post", "put", "patch", "delete"}
                or type(definition) is not dict
            ):
                raise ValueError("unsupported OpenAPI operation")
            if set(definition) - {
                "summary",
                "description",
                "operationId",
                "requestBody",
                "responses",
                "parameters",
            }:
                raise ValueError("unsupported OpenAPI operation field")
            properties, required, mappings = {}, [], []
            for parameter in definition.get("parameters", []):
                if set(parameter) - {
                    "name",
                    "in",
                    "required",
                    "schema",
                    "description",
                } or parameter.get("in") not in {"path", "query"}:
                    raise ValueError("unsupported parameter")
                name, location = parameter["name"], parameter["in"]
                if name in properties:
                    raise ValueError("duplicate parameter")
                properties[name] = parameter["schema"]
                if parameter.get("required"):
                    required.append(name)
                mappings.append(
                    {
                        "location": location,
                        "target": name,
                        "value": {"source": "input", "pointer": "/" + name},
                    }
                )
            if "requestBody" in definition:
                if "body" in properties:
                    raise ValueError("body parameter collision")
                properties["body"] = definition["requestBody"]["content"]["application/json"][
                    "schema"
                ]
                if definition["requestBody"].get("required"):
                    required.append("body")
                mappings.append(
                    {
                        "location": "body",
                        "target": "",
                        "value": {"source": "input", "pointer": "/body"},
                    }
                )
            responses = definition.get("responses", {})
            if set(responses) != {"200"}:
                raise ValueError("import requires one explicit JSON 200 response")
            output = responses["200"]["content"]["application/json"]["schema"]
            inputs = {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
            validate_schema(inputs, settings)
            validate_schema(output, settings)
            result.append(
                OperationManifest.model_validate_json(
                    __import__("json").dumps(
                        {
                            "operation_id": str(uuid7()),
                            "version": 1,
                            "service": payload.service.model_dump(mode="json"),
                            "method": method.upper(),
                            "path_template": path,
                            "query_fields": [
                                m["target"] for m in mappings if m["location"] == "query"
                            ],
                            "mappings": mappings,
                            "input_schema": inputs,
                            "output_schema": output,
                            "response_projection": [""],
                            "effect": "read_only" if method == "get" else "writes",
                            "timeout_seconds": 10,
                            "pagination_limit": 1,
                            "max_response_bytes": 65536,
                        }
                    )
                )
            )
            if len(result) > payload.max_operations:
                raise ValueError("import operation limit")
    if not result:
        raise ValueError("empty import")
    return tuple(result)
