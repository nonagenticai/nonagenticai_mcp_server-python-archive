"""Future durable executor port. Production never accepts an execution here."""

from typing import Any, Protocol

from src.auth.principal import Principal
from src.catalog.resolver import ResolvedTool


class SubmissionError(Exception):
    """Bounded public submission failure."""

    code = "execution_unavailable"


class Submission(Protocol):
    async def submit(
        self,
        principal: Principal,
        tool: ResolvedTool,
        inputs: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict: ...


class UnavailableSubmission:
    async def submit(
        self,
        principal: Principal,
        tool: ResolvedTool,
        inputs: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict:
        raise SubmissionError()
