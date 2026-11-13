"""Closed run contracts and safe public envelopes."""

from typing import Annotated, Any, Literal

from pydantic import Field

from src.catalog.models import Contract, Name

RunStatus = Literal[
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
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out", "interrupted"})


class RunRequest(Contract):
    tool: Name
    inputs: dict[str, Any]
    # Idempotency is deliberately accepted only through the authenticated header.


class RunFailure(Contract):
    message: Annotated[str, Field(max_length=512)]
    code: Annotated[str, Field(pattern=r"^[a-z_]{1,64}$")]


class ExecutionDeniedError(RuntimeError):
    """No host exception text is safe for a public result."""


class ConfirmationRequiredError(ValueError):
    """A draft run of a tool that writes needs ``confirm_writes`` (it acts on a real API)."""


class QueueFullError(ExecutionDeniedError):
    pass


class OutcomeUnknownError(ExecutionDeniedError):
    pass


def envelope(row: dict) -> dict:
    return {
        "run_id": str(row["run_id"]),
        "status": row["status"],
        "result": row["result"],
        "error": row["error"],
        "payload_expired": row.get("payload_expired_at") is not None,
    }


def _plain(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value.__class__.__name__ == "UUID" else value


def draft_view(row: dict, trace: list[dict]) -> dict:
    """Envelope plus the upstream call trace of an author's test run.

    ``error_excerpt`` is the redacted (<= 2 KB) body of the last failed upstream call, written by
    the broker for draft runs only (``mcp_run_api_calls.error_excerpt``)."""
    excerpts = [call.get("error_excerpt") for call in trace if call.get("error_excerpt")]
    return {
        **envelope(row),
        "mode": "draft",
        "trace": [{k: _plain(v) for k, v in call.items()} for call in trace],
        "error_excerpt": excerpts[-1] if excerpts else None,
    }
