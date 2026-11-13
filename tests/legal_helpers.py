"""Test helpers for the legal_* / web_* tools: fixture loading and a recording MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "legal"

Answer = Exception | Callable[[httpx.Request], httpx.Response]


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Routes:
    """MockTransport keyed by (METHOD, path); unknown routes answer 404. Records every request."""

    def __init__(self, routes: dict[tuple[str, str], Answer]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"message": "not found"})
        if isinstance(answer, Exception):
            raise answer
        return answer(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def json_answer(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(status, json=body)


def text_answer(
    status: int, body: str, headers: dict[str, str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(status, text=body, headers=headers)
