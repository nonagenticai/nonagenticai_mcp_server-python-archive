"""Check bound-context requirements and reject statically empty policy intersections.

These checks do not execute operation policy: the broker must enforce every rule
again on each concrete request, including host-side ownership reads.
"""

import json
from typing import Any

from src.catalog.models import (
    ContextEquality,
    NumericBounds,
    OwnershipRead,
    RelativePath,
    SetMembership,
)
from src.catalog.package import canonical_path


def pointer(value: Any, path: str) -> Any:
    try:
        for component in path.split("/")[1:]:
            key = component.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except KeyError, TypeError, ValueError, IndexError:
        raise ValueError("required bound context is missing") from None


def check_rules(rules: tuple, context: dict) -> None:
    arguments: dict[str, list] = {}
    for rule in rules:
        arguments.setdefault(rule.argument, []).append(rule)
        if isinstance(rule, OwnershipRead):
            pointer(context, rule.context)
    for group in arguments.values():
        minimum, maximum = float("-inf"), float("inf")
        values = None
        root = None
        for rule in group:
            candidates = None
            if isinstance(rule, ContextEquality):
                candidates = [pointer(context, rule.context)]
            elif isinstance(rule, SetMembership):
                candidates = list(rule.values)
            elif isinstance(rule, NumericBounds):
                minimum, maximum = max(minimum, rule.minimum), min(maximum, rule.maximum)
            elif isinstance(rule, RelativePath):
                if root is not None and not (
                    root == rule.root
                    or root.startswith(rule.root + "/")
                    or rule.root.startswith(root + "/")
                ):
                    raise ValueError("incompatible relative path restrictions")
                root = rule.root if root is None or len(rule.root) > len(root) else root
            if candidates is not None:
                encoded = {json.dumps(v, sort_keys=True, allow_nan=False) for v in candidates}
                values = encoded if values is None else values & encoded
        if minimum > maximum:
            raise ValueError("incompatible numeric restrictions")
        if values is not None:
            possible = []
            for encoded in values:
                candidate = json.loads(encoded)
                if (minimum != float("-inf") or maximum != float("inf")) and (
                    type(candidate) not in {int, float} or not minimum <= candidate <= maximum
                ):
                    continue
                if root is not None:
                    try:
                        path = canonical_path(candidate)
                    except ValueError, TypeError, AttributeError:
                        continue
                    if path != root and not path.startswith(root + "/"):
                        continue
                possible.append(encoded)
            if not possible:
                raise ValueError("incompatible value restrictions")
