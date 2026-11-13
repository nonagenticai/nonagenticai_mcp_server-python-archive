"""Per-toolkit backend endpoint allow-lists and the no-LLM deny-list (product plan §1).

A toolkit declares every (service, method, path template) it may call. Two checks keep LLM
endpoints out:

* static: ``assert_no_llm_endpoints`` fails if any declared template matches the deny-list. CI
  runs it over every registered toolkit (tests/test_toolkit_contract_no_llm.py).
* runtime: ``EndpointAllowList.authorize`` checks each concrete request path against the
  toolkit's templates AND the deny-list again, so a template variable (``/api/{x}/insights``
  with ``x=ai``) cannot reach a denied path.

Deny patterns are global (not per service) and case-insensitive, so they fail closed: ``*``
matches any run of characters, including ``/``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import Literal

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
SERVICE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SEGMENT = re.compile(r"^(?:[A-Za-z0-9._~-]|\{[a-z][a-z0-9_]{0,63}\})+$")
_CONCRETE_SEGMENT = re.compile(r"^[A-Za-z0-9._~:@!$&'()*+,;=-]+$")


class EndpointNotAllowedError(RuntimeError):
    """A tool tried to call an endpoint outside its toolkit's allow-list. A code bug."""


def _segments(path: str, pattern: re.Pattern[str]) -> list[str]:
    if not path.startswith("/") or len(path) > 1024:
        raise ValueError(f"endpoint path must be absolute and bounded: {path!r}")
    if path == "/":
        return []
    parts = path[1:].split("/")
    for part in parts:
        if part in {"", ".", ".."} or not pattern.fullmatch(part):
            raise ValueError(f"noncanonical endpoint path: {path!r}")
    return parts


@dataclass(frozen=True)
class Endpoint:
    service: str
    method: Method
    path: str

    def __post_init__(self) -> None:
        if not SERVICE_PATTERN.fullmatch(self.service):
            raise ValueError(f"invalid service key: {self.service!r}")
        if self.method not in METHODS:
            raise ValueError(f"invalid method: {self.method!r}")
        _segments(self.path, _SEGMENT)

    @cached_property
    def regex(self) -> re.Pattern[str]:
        """Concrete-path matcher: each ``{var}`` matches exactly one non-empty segment."""
        parts = re.split(r"(\{[a-z][a-z0-9_]*\})", self.path)
        body = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
        return re.compile(f"^{body}$")

    def __str__(self) -> str:
        return f"{self.service} {self.method} {self.path}"


class DenyClass(StrEnum):
    LLM = "llm"
    UNCLASSIFIED = "unclassified"  # blocked until confirmed LLM-free


@dataclass(frozen=True)
class DenyRule:
    pattern: str
    classification: DenyClass
    why: str

    @cached_property
    def regex(self) -> re.Pattern[str]:
        body = ".*".join(re.escape(p) for p in self.pattern.lower().split("*"))
        return re.compile(f"^{body}$")

    def matches(self, path: str) -> bool:
        normalized = path.lower().rstrip("/") or "/"
        return bool(self.regex.match(normalized))


def _rules(patterns: Iterable[str], cls: DenyClass, why: str) -> tuple[DenyRule, ...]:
    return tuple(DenyRule(p, cls, why) for p in patterns)


LLM_DENY_LIST: tuple[DenyRule, ...] = (
    *_rules(("/api/ai", "/api/ai/*"), DenyClass.LLM, "coding-ai (PydanticAI)"),
    *_rules(("/api/tests/ai", "/api/tests/ai/*"), DenyClass.LLM, "coding testing AI endpoints"),
    *_rules(("*/ai-augmented", "*/ai-augmented/*"), DenyClass.LLM, "graphs ai-augmented"),
    *_rules(("*/execute-agent", "*/execute-agent/*"), DenyClass.LLM, "runs agent CLI"),
    *_rules(("*/chat/completions*",), DenyClass.LLM, "workers chatCompletions"),
    *_rules(("/workers/*/message*",), DenyClass.LLM, "workers sendWorkerMessage"),
    *_rules(("*/query/ask*",), DenyClass.LLM, "kag askQuery"),
    *_rules(("/api/ask", "/api/ask/*"), DenyClass.LLM, "strata-rag answer endpoint"),
    *_rules(("*/bug-hunter", "*/bug-hunter/*"), DenyClass.UNCLASSIFIED, "bug-hunter"),
    *_rules(("*/semantic/analyze-codebase*",), DenyClass.UNCLASSIFIED, "semantic/analyze-codebase"),
    *_rules(
        ("*/static/advanced-validation*",), DenyClass.UNCLASSIFIED, "static/advanced-validation"
    ),
    *_rules(("*/static/documentation*",), DenyClass.UNCLASSIFIED, "static/documentation"),
)


def denied_by(path: str, deny_list: Iterable[DenyRule] = LLM_DENY_LIST) -> DenyRule | None:
    return next((rule for rule in deny_list if rule.matches(path)), None)


@dataclass(frozen=True)
class EndpointAllowList:
    """Every backend endpoint one toolkit may call."""

    toolkit: str
    endpoints: tuple[Endpoint, ...] = field(default=())

    def __post_init__(self) -> None:
        keys = [(e.service, e.method, e.path) for e in self.endpoints]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate endpoint in {self.toolkit} allow-list")

    def __contains__(self, endpoint: object) -> bool:
        return endpoint in self.endpoints

    def authorize(self, service: str, method: str, path: str) -> Endpoint:
        """Return the declared endpoint a concrete request matches, or raise."""
        try:
            _segments(path, _CONCRETE_SEGMENT)
        except ValueError:
            raise EndpointNotAllowedError(f"noncanonical request path for {service}") from None
        rule = denied_by(path)
        if rule is not None:
            raise EndpointNotAllowedError(f"{method} {path} is denied ({rule.classification})")
        for endpoint in self.endpoints:
            if (
                endpoint.service == service
                and endpoint.method == method
                and endpoint.regex.match(path)
            ):
                return endpoint
        raise EndpointNotAllowedError(
            f"{service} {method} {path} is not in the {self.toolkit} allow-list"
        )


@dataclass(frozen=True)
class DenyViolation:
    toolkit: str
    endpoint: Endpoint
    rule: DenyRule

    def __str__(self) -> str:
        return f"{self.toolkit}: {self.endpoint} matches {self.rule.pattern} ({self.rule.why})"


def find_denied(
    allow_lists: Iterable[EndpointAllowList], deny_list: Iterable[DenyRule] = LLM_DENY_LIST
) -> list[DenyViolation]:
    rules = tuple(deny_list)
    violations = []
    for allow in allow_lists:
        for endpoint in allow.endpoints:
            rule = denied_by(endpoint.path, rules)
            if rule is not None:
                violations.append(DenyViolation(allow.toolkit, endpoint, rule))
    return violations


def assert_no_llm_endpoints(
    allow_lists: Iterable[EndpointAllowList], deny_list: Iterable[DenyRule] = LLM_DENY_LIST
) -> None:
    """Test helper: fail if any toolkit's allow-list names a denied endpoint."""
    violations = find_denied(allow_lists, deny_list)
    if violations:
        raise AssertionError(
            "toolkit allow-lists contain LLM or unclassified endpoints:\n"
            + "\n".join(str(v) for v in violations)
        )
