"""Live-test follow-ups: no server-side prepares behind pgbouncer, resilient run poll,
agent-fixable ``invalid_arguments`` (paths and keywords only, never values)."""

import json
from types import SimpleNamespace
from uuid import uuid7

import pytest

from src.auth.context import RequestAuthority, authority
from src.auth.principal import Principal
from src.catalog.provider import CatalogTool
from src.catalog.resolver import ResolvedTool
from src.catalog.validation import InputValidationError, validate_instance
from src.execution import service as execution_service
from src.execution.service import ExecutionService
from src.storage.database import Database
from src.toolkits.models import BoundContext, EffectiveTool, ToolReference
from tests.fixtures import TENANT_A, TOOL, settings, tool
from tests.test_bindings import LICENSE

SECRET = "sk-live-SECRET-0123456789"  # pragma: allowlist secret
SCHEMA = {
    "type": "object",
    "properties": {
        "branch": {"type": "string", "format": "git-branch"},
        "count": {"type": "integer", "minimum": 1},
        "token": {"type": "string"},
    },
    "required": ["branch", "token"],
    "additionalProperties": False,
}


def test_every_pool_disables_server_side_prepares():
    pool = Database(settings()).pool
    assert pool.kwargs["prepare_threshold"] is None


def test_validation_problems_are_paths_and_keywords_only():
    with pytest.raises(InputValidationError) as caught:
        validate_instance(SCHEMA, {"count": 0, "extra": SECRET}, settings())
    assert {(p["path"], p["rule"]) for p in caught.value.problems} == {
        ("/branch", "required"),
        ("/token", "required"),
        ("", "additionalProperties"),
        ("/count", "minimum"),
    }
    assert SECRET not in json.dumps(caught.value.problems)
    assert isinstance(caught.value, ValueError)


def test_problems_are_capped_and_pointer_escaped():
    schema = {"type": "object", "properties": {"a/b": {"type": "string"}}}
    with pytest.raises(InputValidationError) as caught:
        validate_instance(schema, {"a/b": 1}, settings())
    assert caught.value.problems == [{"path": "/a~1b", "rule": "type"}]
    many = {"type": "array", "items": {"type": "string"}}
    with pytest.raises(InputValidationError) as caught:
        validate_instance(many, [1] * 100, settings())
    assert len(caught.value.problems) == 20


def resolved() -> ResolvedTool:
    manifest = tool(input_schema=SCHEMA)
    effective = EffectiveTool(
        tool=ToolReference(id=TOOL, version=1, tenant_id=TENANT_A), public_name="synthetic_tool"
    )
    return ResolvedTool(
        manifest.model_dump_json(),
        effective.model_dump_json(),
        BoundContext(tenant_id=TENANT_A).model_dump_json(),
    )


class Boom:
    async def resolve(self, principal):
        raise RuntimeError(f"db down {SECRET}")

    access = SimpleNamespace(settings=settings())


async def call(arguments):
    context = authority.set(
        RequestAuthority(Principal(TENANT_A, LICENSE, "caller", uuid7(), 1), None)
    )
    try:
        return await CatalogTool(resolved(), Boom(), None).run(arguments)
    finally:
        authority.reset(context)


async def test_invalid_arguments_is_distinct_and_never_echoes_values():
    result = await call({"branch": f"bad..{SECRET}", "count": 0})
    body = json.loads(result.content[0].text)
    assert result.is_error and body["error"] == "invalid_arguments"
    assert {(p["path"], p["rule"]) for p in body["problems"]} == {
        ("/branch", "format"),
        ("/count", "minimum"),
        ("/token", "required"),
    }
    assert SECRET not in result.content[0].text
    missing = json.loads((await call({})).content[0].text)
    assert {"path": "/branch", "rule": "required"} in missing["problems"]


async def test_other_failures_stay_opaque():
    result = await call({"branch": "main", "token": SECRET})
    assert result.is_error and result.content[0].text == "tool_request_denied"


class FlakyRepository:
    def __init__(self, failures):
        self.settings = settings(sync_wait_seconds=5)
        self.failures, self.calls = failures, 0

    async def get(self, tenant, run_id):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("cached plan must not change result type")
        return {"run_id": run_id, "status": "succeeded"}


async def test_poll_survives_one_transient_error():
    repository = FlakyRepository(1)
    service = ExecutionService(repository, None)
    row = await service._wait(
        {"run_id": "r", "status": "queued"}, lambda: repository.get(TENANT_A, "r")
    )
    assert row["status"] == "succeeded" and repository.calls == 2


async def test_poll_returns_last_known_row_after_persistent_errors():
    repository = FlakyRepository(10**6)
    service = ExecutionService(repository, None)
    queued = {"run_id": "r", "status": "queued"}
    row = await service._wait(queued, lambda: repository.get(TENANT_A, "r"))
    assert row is queued and repository.calls == execution_service.POLL_RETRIES + 1


def test_denial_site_names_the_raising_src_line_without_the_message():
    from src.broker.client import denial_site
    from src.execution.models import ExecutionDeniedError

    def check():
        raise ExecutionDeniedError("secret-token-value-123")

    try:
        try:
            check()
        except ExecutionDeniedError as inner:
            raise ExecutionDeniedError() from inner
    except ExecutionDeniedError as error:
        site = denial_site(error)
    # Test files live outside src/, so no src frame raised here: the fixed fallback is used.
    assert site == "unknown"
    assert "secret" not in site


def test_denial_site_reports_src_frames():
    from src.broker.client import denial_site
    from src.catalog.validation import validate_instance

    try:
        validate_instance({"type": "object", "required": ["a"]}, {}, None)
    except Exception as error:  # noqa: BLE001 - any validation failure carries a src frame
        site = denial_site(error)
    assert site.startswith("src/") and ":" in site


def test_denial_site_follows_context_hidden_by_raise_from_none():
    from src.broker.client import denial_site
    from src.catalog.validation import validate_instance
    from src.execution.models import ExecutionDeniedError

    try:
        try:
            validate_instance({"type": "object", "required": ["a"]}, {}, None)
        except Exception:  # noqa: BLE001 - the broker's catch-all
            raise ExecutionDeniedError() from None
    except ExecutionDeniedError as error:
        assert error.__cause__ is None and error.__suppress_context__
        site = denial_site(error)
    assert site.startswith("src/catalog/validation.py:")
