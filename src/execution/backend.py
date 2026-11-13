"""Backend boundary. There is no local uploaded-code execution implementation."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Pod:
    name: str
    uid: str
    run_id: str
    fence: int
    phase: str
    exit_code: int | None = None


class Channel(Protocol):
    async def send(self, data: bytes) -> None: ...
    async def receive(self) -> tuple[bytes, bytes] | None: ...
    async def close(self) -> None: ...


class Backend(Protocol):
    async def ready(self) -> None: ...
    async def start(self, row, limits) -> Pod: ...
    async def inspect(self, name: str) -> Pod | None: ...
    async def attach(self, pod: Pod) -> Channel: ...
    async def stop(self, pod: Pod) -> bool: ...
    async def list(self) -> builtins.list[Pod]: ...
