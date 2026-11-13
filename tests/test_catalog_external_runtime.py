import dataclasses

import pytest
from pydantic import ValidationError

from src.catalog.models import FrProjectTarget, N8nWorkflowTarget
from src.catalog.resolver import ResolvedTool
from tests.fixtures import external_tool, tool
from tests.test_execution import enqueue, setup_runtime  # noqa: F401


@pytest.fixture
async def runtime(database):
    return await setup_runtime(database)


def external_resolved(runtime) -> ResolvedTool:
    return dataclasses.replace(runtime.resolved, manifest_json=external_tool().model_dump_json())


def test_n8n_tool_requires_target():
    with pytest.raises(ValidationError, match="target"):
        external_tool(target=None)
    assert external_tool().runtime == "n8n_workflow"


def test_target_kind_must_match_runtime():
    with pytest.raises(ValidationError, match="kind"):
        external_tool(target=FrProjectTarget(kind="fr_project", project_type="demo"))
    assert external_tool(
        runtime="fr_project", target=FrProjectTarget(kind="fr_project", project_type="demo")
    )
    with pytest.raises(ValidationError):
        N8nWorkflowTarget(kind="n8n_workflow", webhook_path="../escape")
    with pytest.raises(ValidationError):
        N8nWorkflowTarget(kind="n8n_workflow", webhook_path="mcp/demo", result_node="bad/node")
    assert external_tool().target.result_node == "MCP Result"


def test_kata_tool_unchanged():
    manifest = tool()
    assert manifest.runtime == "kata_python" and manifest.target is None
    assert manifest.entrypoint and manifest.runner_image_digest
    with pytest.raises(ValidationError, match="entrypoint"):
        tool(entrypoint=None)
    with pytest.raises(ValidationError, match="target"):
        tool(target=N8nWorkflowTarget(kind="n8n_workflow", webhook_path="mcp/demo"))


def test_external_tool_rejects_entrypoint_and_image():
    with pytest.raises(ValidationError, match="no entrypoint"):
        external_tool(entrypoint="package.main:run")


async def test_accept_persists_runtime(runtime):
    row = await runtime.repository.accept(
        runtime.principal,
        external_resolved(runtime),
        {},
        None,
        await runtime.catalog.repository.generation(),
    )
    assert row["runtime"] == "n8n_workflow" and row["status"] == "queued"
    kata = await enqueue(runtime)
    assert kata["runtime"] == "kata_python"


async def test_external_submit_returns_immediately_queued(database, monkeypatch):
    runtime = await setup_runtime(database, sync_wait_seconds=5)

    async def resolve(principal):
        return (external_resolved(runtime),)

    monkeypatch.setattr(runtime.execution.resolver, "resolve", resolve)
    assert runtime.config.sync_wait_seconds > 0
    result = await runtime.execution.submit(runtime.principal, external_resolved(runtime), {}, None)
    assert result["status"] == "queued"
