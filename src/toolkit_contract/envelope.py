"""The one result envelope every first-party toolkit tool returns (product plan §1).

    {status, data, checks[{name, status, summary}], verdict, next[], reason?, error?, resume_token?}

Invariants are enforced at construction, so a tool cannot return a half-formed envelope:
a ``precondition_failed`` carries a reason and the tool that fixes it, a ``pending`` carries a
resume token and the status tool to call with it, a ``check_failed`` has a failing check, and
an ``ok`` never hides one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, computed_field, model_validator

from src.catalog.models import NAME_PATTERN
from src.catalog.models import OPERATION_PATTERN as OPERATION_PATTERN

REASON_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
# MCP tool names: the catalog's Name pattern (also inside LLM providers' ^[a-zA-Z0-9_-]{1,64}$).
# Shape is <toolkit>_<verb>[_<object>]; the agent platform's canonical object.verb operation
# travels in tool metadata instead (``_meta.operation``).
TOOL_NAME_PATTERN = NAME_PATTERN
SUMMARY_MAX = 500

ToolName = Annotated[str, Field(pattern=TOOL_NAME_PATTERN)]
Reason = Annotated[str, Field(pattern=REASON_PATTERN)]


class Status(StrEnum):
    OK = "ok"
    PRECONDITION_FAILED = "precondition_failed"
    CHECK_FAILED = "check_failed"
    PENDING = "pending"
    BACKEND_ERROR = "backend_error"


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    DEGRADED = "degraded"  # the check could only partly run (e.g. scan_degraded)
    ERROR = "error"  # the check itself could not run; counts as a failure


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    DEGRADED = "degraded"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def clip(text: str, limit: int = SUMMARY_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Check(_Model):
    name: Reason
    status: CheckStatus
    summary: Annotated[str, Field(max_length=SUMMARY_MAX)] = ""


class NextStep(_Model):
    tool: ToolName
    reason: Annotated[str, Field(max_length=SUMMARY_MAX)] = ""
    arguments: dict[str, JsonValue] | None = None


class BackendErrorInfo(_Model):
    message: Annotated[str, Field(min_length=1, max_length=SUMMARY_MAX)]
    retryable: bool


def verdict(checks: tuple[Check, ...] | list[Check]) -> Verdict:
    """Any fail or error -> fail; else any degraded -> degraded; else pass (also for none)."""
    statuses = {c.status for c in checks}
    if statuses & {CheckStatus.FAIL, CheckStatus.ERROR}:
        return Verdict.FAIL
    if CheckStatus.DEGRADED in statuses:
        return Verdict.DEGRADED
    return Verdict.PASS


class ToolResult(_Model):
    status: Status
    data: JsonValue = None
    checks: tuple[Check, ...] = ()
    next: tuple[NextStep, ...] = ()
    reason: Reason | None = None
    error: BackendErrorInfo | None = None
    resume_token: Annotated[str, Field(min_length=1, max_length=2048)] | None = None

    @computed_field
    @property
    def verdict(self) -> Verdict | None:
        return verdict(self.checks) if self.checks else None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        names = [c.name for c in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("check names must be unique")
        failing = self.verdict is Verdict.FAIL
        match self.status:
            case Status.OK:
                if failing:
                    raise ValueError("ok result cannot carry a failing check")
            case Status.CHECK_FAILED:
                if not failing:
                    raise ValueError("check_failed requires a failing check")
            case Status.PRECONDITION_FAILED:
                if self.reason is None or not self.next:
                    raise ValueError("precondition_failed requires reason and next")
            case Status.PENDING:
                if self.resume_token is None or not self.next:
                    raise ValueError("pending requires resume_token and next")
            case Status.BACKEND_ERROR:
                if self.error is None:
                    raise ValueError("backend_error requires error")
        if self.resume_token is not None and self.status is not Status.PENDING:
            raise ValueError("resume_token is only valid on pending")
        if self.reason is not None and self.status is not Status.PRECONDITION_FAILED:
            raise ValueError("reason is only valid on precondition_failed")
        if self.error is not None and self.status is not Status.BACKEND_ERROR:
            raise ValueError("error is only valid on backend_error")
        return self

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def completed(data: JsonValue = None, checks: tuple[Check, ...] = ()) -> ToolResult:
    """ok, or check_failed when the checks' verdict is fail."""
    failing = verdict(checks) is Verdict.FAIL
    return ToolResult(
        status=Status.CHECK_FAILED if failing else Status.OK, data=data, checks=checks
    )


def precondition_failed(
    reason: str, next_steps: tuple[NextStep, ...], checks: tuple[Check, ...] = ()
) -> ToolResult:
    return ToolResult(
        status=Status.PRECONDITION_FAILED, reason=reason, next=next_steps, checks=checks
    )


def pending(
    resume_token: str, next_steps: tuple[NextStep, ...], data: JsonValue = None
) -> ToolResult:
    return ToolResult(status=Status.PENDING, resume_token=resume_token, next=next_steps, data=data)


def backend_error(message: str, retryable: bool, checks: tuple[Check, ...] = ()) -> ToolResult:
    return ToolResult(
        status=Status.BACKEND_ERROR,
        error=BackendErrorInfo(message=clip(message), retryable=retryable),
        checks=checks,
    )


# Result schema for the catalog's ``result_schema`` field. Hand-written (not pydantic-generated)
# so it stays inside the catalog's closed JSON Schema subset (src/catalog/validation.py).
_STR = {"type": "string", "maxLength": SUMMARY_MAX}
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "checks", "next"],
    "properties": {
        "status": {"enum": [s.value for s in Status]},
        "data": {},
        "checks": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "status", "summary"],
                "properties": {
                    "name": {"type": "string", "maxLength": 64},
                    "status": {"enum": [s.value for s in CheckStatus]},
                    "summary": _STR,
                },
            },
        },
        "verdict": {"enum": [v.value for v in Verdict]},
        "next": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool"],
                "properties": {
                    "tool": {"type": "string", "maxLength": 64},
                    "reason": _STR,
                    "arguments": {"type": "object"},
                },
            },
        },
        "reason": {"type": "string", "maxLength": 64},
        "error": {
            "type": "object",
            "additionalProperties": False,
            "required": ["message", "retryable"],
            "properties": {"message": _STR, "retryable": {"type": "boolean"}},
        },
        "resume_token": {"type": "string", "maxLength": 2048},
    },
}
