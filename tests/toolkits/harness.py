"""A fake broker for toolkit scripts: replays tests/fixtures/coding/<op>.json.

It behaves like the real broker for what matters to a script: arguments are validated against
the operation's input schema and scope rules, mapped through ``src.broker.policy.transport``
(so a missing pointer fails here as it does there), the response is projected and redacted by
``project`` and validated against the output schema, and only the operation's declared
``expected_error_statuses`` reach the script (as ``ApiStatusError``); any other failure is
``RuntimeError("api_call_denied")``.
"""

import copy
import importlib
import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from runner.sdk import ApiStatusError
from src.broker.policy import project, transport
from src.catalog.models import SetMembership
from src.catalog.validation import validate_instance
from src.platform.toolkit_source import ToolkitSource, load_all, package_files
from src.storage.executions import encoded
from src.toolkits.constraints import pointer
from tests.fixtures import settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "coding"
SETTINGS = settings()
TOOLKIT: ToolkitSource = load_all(ROOT / "toolkits")[0]
SHA = "a" * 40


class Status:
    """A fixture override: the upstream answered this HTTP status."""

    def __init__(self, status: int, body: Any = None) -> None:
        self.status, self.body = status, body


def fixture(key: str) -> Any:
    return json.loads((FIXTURES / f"{key}.json").read_text())


class FakeBudget:
    """kit.Budget stand-in: ``remaining`` is a number or a callable of the call count."""

    def __init__(self, remaining: float | Callable[[int], float] = 1000.0) -> None:
        self._remaining = remaining
        self.reads = 0

    def remaining(self) -> float:
        self.reads += 1
        value = self._remaining
        return value(self.reads) if callable(value) else value

    def allows(self, seconds: float) -> bool:
        return self.remaining() >= seconds


class FakeAPI:
    def __init__(self, tool: str, overrides: dict[str, Any] | None = None) -> None:
        self.tool = TOOLKIT.tool(tool)
        self.by_id = {str(TOOLKIT.operations[k].operation_id): k for k in TOOLKIT.operations}
        self.allowed = set(self.tool.op_keys)
        self.overrides = dict(overrides or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sequence: dict[str, int] = {}

    @property
    def keys(self) -> list[str]:
        return [k for k, _ in self.calls]

    def args(self, key: str) -> list[dict[str, Any]]:
        return [a for k, a in self.calls if k == key]

    def _answer(self, key: str, arguments: dict[str, Any]) -> Any:
        value = self.overrides.get(key)
        # a list is a sequence of answers, except for operations whose body is itself an array
        array_body = TOOLKIT.operations[key].output_schema.get("type") == "array"
        if isinstance(value, list) and not array_body:
            index = min(self.sequence.get(key, 0), len(value) - 1)
            self.sequence[key] = self.sequence.get(key, 0) + 1
            value = value[index]
        if callable(value):
            value = value(arguments)
        return fixture(key) if value is None else copy.deepcopy(value)

    def handle(self, operation_id: str, arguments: dict[str, Any], idempotency_key=None):
        """Returns ("ok", projected) or ("status", status, body); raises RuntimeError if denied."""
        assert idempotency_key is None, "operations declare no downstream idempotency key"
        key = self.by_id[str(operation_id)]
        assert key in self.allowed, f"{self.tool.name} called {key} outside allowed_operations"
        operation = TOOLKIT.operations[key]
        validate_instance(operation.input_schema, arguments, SETTINGS)
        for rule in operation.scope_rules:  # only set_membership is used by this toolkit
            assert isinstance(rule, SetMembership)
            if not any(
                encoded(pointer(arguments, rule.argument)) == encoded(v) for v in rule.values
            ):
                raise RuntimeError("api_call_denied")
        transport(operation, arguments, {})
        # journal_intent (src/storage/executions.py): the run's api_calls limit, then the
        # operation's per-run pagination_limit; a denied intent is neither journalled nor sent.
        if len(self.calls) >= self.tool.fields["limits"]["api_calls"]:
            raise RuntimeError("api_call_denied")
        if self.keys.count(key) >= operation.pagination_limit:
            raise RuntimeError("api_call_denied")
        self.calls.append((key, copy.deepcopy(arguments)))
        answer = self._answer(key, arguments)
        if isinstance(answer, Status):
            if answer.status in operation.expected_error_statuses:
                return ("status", answer.status, answer.body)
            raise RuntimeError("api_call_denied")
        if isinstance(answer, Exception):
            raise answer
        projected = project(operation, answer)
        validate_instance(operation.output_schema, projected, SETTINGS)
        return ("ok", projected)

    async def call(self, operation_id, arguments, idempotency_key=None):
        outcome = self.handle(operation_id, arguments, idempotency_key)
        if outcome[0] == "status":
            raise ApiStatusError(outcome[1], outcome[2])
        return outcome[1]


def load_tool(name: str, directory: Path | None = None):
    """Import a tool's package fresh (its own tool/kit/ops/coding modules), as the runner does."""
    tool = TOOLKIT.tool(name)
    root = Path(directory or tempfile.mkdtemp(prefix="pkg-"))
    for entry in package_files(TOOLKIT, tool):
        (root / entry["path"]).write_text(entry["content"], encoding="utf-8")
    for module in ("tool", "kit", "ops", "coding"):
        sys.modules.pop(module, None)
    sys.path.insert(0, str(root))
    try:
        module = importlib.import_module("tool")
        kit = importlib.import_module("kit")
        importlib.import_module("ops")  # kit imports it lazily, after sys.path is restored
    finally:
        sys.path.remove(str(root))
    return SimpleNamespace(main=module.main, module=module, kit=kit)


async def run_tool(name: str, inputs: dict[str, Any], overrides: dict[str, Any] | None = None):
    """Run one tool against the fake broker. Returns (result, fake, loaded)."""
    loaded = load_tool(name)
    fake = FakeAPI(name, overrides)
    result = await loaded.main(inputs, SimpleNamespace(api=fake))
    return result, fake, loaded
