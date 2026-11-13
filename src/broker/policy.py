"""Concrete invocation policy: conjunctions, typed JSON equality and closed mappings."""

import copy
import re
from urllib.parse import quote

from src.catalog.models import (
    ContextEquality,
    NumericBounds,
    OwnershipRead,
    RelativePath,
    SetMembership,
)
from src.catalog.package import canonical_path
from src.execution.models import ExecutionDeniedError
from src.storage.executions import encoded
from src.toolkits.constraints import pointer


async def enforce(rules, arguments, context, ownership):
    for rule in rules:
        value = pointer(arguments, rule.argument)
        if isinstance(rule, ContextEquality):
            valid = encoded(value) == encoded(pointer(context, rule.context))
        elif isinstance(rule, SetMembership):
            valid = any(encoded(value) == encoded(v) for v in rule.values)
        elif isinstance(rule, NumericBounds):
            valid = type(value) in {int, float} and rule.minimum <= value <= rule.maximum
        elif isinstance(rule, RelativePath):
            path = canonical_path(value)
            valid = path == rule.root or path.startswith(rule.root + "/")
        elif isinstance(rule, OwnershipRead):
            result = await ownership(rule, arguments)
            valid = encoded(pointer(result, rule.result)) == encoded(pointer(context, rule.context))
        else:
            valid = False
        if not valid:
            raise ExecutionDeniedError()


def set_pointer(target, path, value):
    if not path:
        return copy.deepcopy(value)
    if not path.startswith("/"):
        raise ExecutionDeniedError()
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]
    current = target
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = copy.deepcopy(value)
    return target


def transport(operation, arguments, context):
    path, query, headers, body = operation.path_template, {}, {}, None
    for mapping in operation.mappings:
        source = mapping.value
        try:
            value = (
                source.value
                if source.source == "literal"
                else pointer(arguments if source.source == "input" else context, source.pointer)
            )
        except ValueError:
            if mapping.optional:
                continue  # an absent optional input is omitted, never sent as null
            raise
        if mapping.location == "body":
            body = set_pointer({} if body is None else body, mapping.target, value)
            continue
        if type(value) not in {str, int, float, bool}:
            raise ExecutionDeniedError()
        text = str(value)
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ExecutionDeniedError()
        if mapping.location == "path":
            if text in {"", ".", ".."} or any(c in text for c in "/\\%?#"):
                raise ExecutionDeniedError()
            path = path.replace("{" + mapping.target + "}", quote(text, safe=""))
        elif mapping.location == "query":
            query[mapping.target] = text
        elif mapping.location == "header":
            name = mapping.target.replace("_", "-")
            if name.lower() in {
                "authorization",
                "x-tenant-id",
                "host",
                "cookie",
                "connection",
                "content-length",
                "transfer-encoding",
                "proxy-authorization",
                "idempotency-key",
                "x-idempotency-key",
            }:
                raise ExecutionDeniedError()
            headers[name] = text
    if re.search(r"[{}]", path):
        raise ExecutionDeniedError()
    return path, query, headers, body


def project(operation, value):
    result = {}
    for path in operation.response_projection:
        result = set_pointer(result, path, pointer(value, path))
    for path in operation.redact_fields:
        if not path:
            raise ExecutionDeniedError()
        try:
            parent, _, tail = path.rpartition("/")
            container = pointer(result, parent)
            key = tail.replace("~1", "/").replace("~0", "~")
            if isinstance(container, list):
                container[int(key)] = None
            else:
                container.pop(key, None)
        except KeyError, IndexError, ValueError, TypeError:
            pass
    return result
