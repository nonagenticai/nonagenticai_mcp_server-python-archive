"""Public schema policy shared by authoring and binding selection."""

from copy import deepcopy

from src.toolkits.models import BoundContext

RESERVED_NAMES = frozenset({"mcp_run_status", "mcp_run_cancel"})
RUN_STATUSES = [
    "queued",
    "starting",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
]


def validate_public_input(schema: dict) -> None:
    def overlaps(node):
        if isinstance(node, dict):
            properties = node.get("properties", {})
            if not isinstance(properties, dict):
                raise ValueError("invalid public schema properties")
            if set(properties) & BoundContext.model_fields.keys():
                return True
            return any(overlaps(v) for v in node.values())
        return isinstance(node, list) and any(overlaps(v) for v in node)

    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or overlaps(schema)
    ):
        raise ValueError("public input must be a closed object and omit bound fields")


def output_envelope(result_schema: dict) -> dict:
    # Walk schema positions only: literal defaults/enum values may contain '$ref'.
    from src.catalog.validation import ARRAYS, MAPS, SINGLE

    def rebase(node):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#"):
                raise ValueError("external result reference")
            node["$ref"] = "#/$defs/tool_result" + ref[1:]
        for key, value in node.items():
            if key in MAPS:
                for child in value.values():
                    rebase(child)
            elif key in SINGLE:
                rebase(value)
            elif key in ARRAYS:
                for child in value:
                    rebase(child)

    schema = deepcopy(result_schema)
    rebase(schema)
    return {
        "type": "object",
        "additionalProperties": False,
        "$defs": {"tool_result": schema},
        "required": ["run_id", "status", "result", "error"],
        "properties": {
            "run_id": {"type": "string", "minLength": 36, "maxLength": 36},
            "status": {"enum": RUN_STATUSES},
            "payload_expired": {"type": "boolean"},
            "result": {"anyOf": [{"$ref": "#/$defs/tool_result"}, {"type": "null"}]},
            "error": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["code", "message"],
                        "properties": {
                            "code": {"type": "string", "maxLength": 64},
                            "message": {"type": "string", "maxLength": 512},
                        },
                    },
                ]
            },
        },
    }
