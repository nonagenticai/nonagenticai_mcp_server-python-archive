"""The contract between the external-run driver and one external system (n8n, fr).

Adapters do HTTP only. They never write mcp_runs; the driver is the single writer.
"""

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ExternalRef:
    kind: Literal["n8n", "fr", "fr_command"]
    #: n8n: the execution id. fr: the start approval id (or the project id if none was needed).
    #: fr_command: the project id the read answered for.
    id: str
    #: fr: the project, once it exists. Stored keys only grow (migration 0011 guard).
    project_id: str | None = None
    #: fr_command: the answer of the one-shot read, so a crash between start and finish loses
    #: nothing (the poll returns it without dispatching the command again).
    result: dict | None = None

    def stored(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclass(frozen=True)
class ExternalState:
    status: Literal["running", "succeeded", "failed", "cancelled"]
    result: dict | None = None
    #: Public failure code, ``^[a-z_]{1,64}$`` (mcp_runs.error.code).
    code: str | None = None
    #: Short progress text for statusMessage (never secrets, never upstream bodies).
    message: str | None = None


class ExternalStartError(Exception):
    """The external system refused or could not start the run. Nothing is running there."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ExternalAdapter(Protocol):
    async def start(self, row: Mapping) -> ExternalRef: ...

    async def poll(self, row: Mapping) -> ExternalState: ...

    async def cancel(self, row: Mapping) -> None: ...


class AnswerForwarder(Protocol):
    """Every task input is an fr approval (n8n questions are routed through fr too), so every
    answer goes back to fr, whatever the run's runtime. Returns a refusal code, or None."""

    async def forward(self, row: Mapping, item: Mapping) -> str | None: ...
