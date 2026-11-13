"""End-to-end MCP tasks: real app, real driver ticks, fake n8n and fake fr, shared PostgreSQL."""

import dataclasses
import functools
import json
import time
from uuid import UUID, uuid7

import httpx
import pytest
from pydantic import SecretStr

from src.catalog.models import FrProjectTarget
from src.external.driver import ExternalDriver
from src.external.fr import FrAdapter, elicitation_request
from src.external.n8n import N8nAdapter
from src.external.signing import sign, verify
from tests import test_tasks_protocol as protocol
from tests.fixtures import TENANT_A, external_tool
from tests.test_tasks_protocol import ACCEPT, ACTOR_META, GOOD_ACTOR, environment, task_rpc

SECRET = "synthetic-e2e-fr-secret"  # pragma: allowlist secret
EVENTS_PATH = "/api/channels/fr/events"
TITLE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "additionalProperties": False,
}


class FakeN8n:
    def __init__(self):
        self.webhooks = []
        self.execution = {"status": "running"}
        self.webhook_response = {"executionId": "exec-1"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/webhook/"):
            self.webhooks.append(json.loads(request.content))
            return httpx.Response(200, json=self.webhook_response)
        if path.startswith("/api/v1/executions/"):
            return httpx.Response(200, json=self.execution)
        return httpx.Response(404)


class FakeFr:
    """fr's ``mcp`` channel: one signed ``{address,text,correlation_id}`` in, a status out."""

    def __init__(self):
        self.received = []
        self.project_status = "active"
        self.create_response = {"status": "pending-approval", "approvalId": "ap-start"}
        self.answer_response = {"status": "result", "result": {"ok": True}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert verify(request.content, dict(request.headers), SECRET, int(time.time()))
        assert request.url.path == "/api/webhooks/mcp/incoming"
        body = json.loads(request.content)
        self.received.append(body)
        text = body["text"]
        if text.startswith("projects.create "):
            return httpx.Response(200, json=self.create_response)
        if text.startswith("projects.get "):
            project = json.loads(text.removeprefix("projects.get "))["id"]
            result = {"id": project, "name": "Demo", "status": self.project_status}
            return httpx.Response(200, json={"status": "result", "result": result})
        if text.startswith(("approve ", "reject ")):
            return httpx.Response(200, json=self.answer_response)
        return httpx.Response(200, json={"status": "no-action"})

    def texts(self, prefix):
        return [m for m in self.received if m["text"].startswith(prefix)]


@pytest.fixture
def n8n():
    return FakeN8n()


@pytest.fixture
def fr():
    return FakeFr()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        protocol,
        "setup_runtime",
        functools.partial(
            protocol.setup_runtime,
            n8n_base_url="http://n8n.test",
            n8n_webhook_base_url="http://n8n.test",
            n8n_api_key=SecretStr("synthetic-n8n-key"),  # pragma: allowlist secret
            fr_base_url="http://fr.test",
            fr_channel_secret=SecretStr(SECRET),
        ),
    )


def make_driver(env, n8n, fr):
    config = env.runtime.config
    adapters = {
        "n8n_workflow": N8nAdapter(
            httpx.AsyncClient(transport=httpx.MockTransport(n8n.handler)), config
        ),
        "fr_project": FrAdapter(
            httpx.AsyncClient(transport=httpx.MockTransport(fr.handler)), config
        ),
    }
    return ExternalDriver(env.runtime.repository, env.inputs, adapters, config, uuid7())


def use_fr_project(env):
    resolver = env.app.state.catalog_resolver
    original = resolver.resolve

    async def resolve(principal):
        return tuple(
            dataclasses.replace(
                r,
                manifest_json=external_tool(
                    runtime="fr_project",
                    target=FrProjectTarget(kind="fr_project", project_type="demo"),
                    input_schema=TITLE_SCHEMA,
                ).model_dump_json(),
            )
            for r in await original(principal)
        )

    resolver.resolve = resolve


async def view(env, task):
    return (await task_rpc(env, "tasks/get", task))["result"]


async def post_event(env, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    base = env.base.removesuffix("/mcp")
    return await env.http.post(
        base + EVENTS_PATH, content=raw, headers=sign(raw, SECRET, int(time.time()))
    )


def envelope(event, task, /, **fields):
    return {"type": event, "tenant_id": str(TENANT_A), "correlation_id": task, **fields}


async def test_n8n_task_lifecycle(database, n8n, fr):
    async with environment(database) as env:
        driver = make_driver(env, n8n, fr)
        task = await protocol.new_task(env)
        assert (await view(env, task))["status"] == "working"
        # The fr block exists only on fr runs: rmcp preserves _meta, nothing else is added.
        # The fr block exists only on fr runs (the server always stamps serverInfo in _meta).
        assert "ai.nonagentic/fr" not in ((await view(env, task)).get("_meta") or {})

        await driver.tick()
        assert len(n8n.webhooks) == 1
        assert n8n.webhooks[0] == {"mcp_run_id": task, "tenant_id": str(TENANT_A), "inputs": {}}
        assert (await view(env, task))["status"] == "working"

        n8n.execution = {
            "status": "success",
            "data": {
                "resultData": {
                    "runData": {"MCP Result": [{"data": {"main": [[{"json": {"answer": 42}}]]}}]}
                }
            },
        }
        await driver.tick()
        done = await view(env, task)
        assert done["status"] == "completed"
        assert done["result"]["isError"] is False
        assert done["result"]["structuredContent"]["result"] == {"answer": 42}


ACTOR = {"channel": "mcp", "address": GOOD_ACTOR["address"]}
WITH_ACTOR = {ACTOR_META: GOOD_ACTOR}


async def answer(env, task, key, actor=GOOD_ACTOR):
    return await task_rpc(
        env,
        "tasks/update",
        task,
        extra={"inputResponses": {key: ACCEPT}},
        meta={ACTOR_META: actor},
    )


def message(task, kind, text=None, **meta):
    return {
        "type": "message",
        "tenant_id": str(TENANT_A),
        "correlation_id": task,
        "kind": kind,
        "text": text,
        "meta": meta,
    }


def project_created(task, project="proj-1"):
    return message(
        task,
        "action-result",
        "Project created",
        actionName="projects.create",
        outcome="approved",
        result={"project_id": project},
    )


async def fr_task_with_project(env, driver, fr):
    """A task whose start has been approved: the project exists, the first poll has run."""
    use_fr_project(env)
    task = await protocol.new_task(env, meta=WITH_ACTOR, arguments={"title": "Demo"})
    await driver.tick()
    assert (await post_event(env, project_created(task))).status_code == 204
    await driver.tick()
    return task


async def test_fr_task_meta_carries_project_activity_and_signals(database, n8n, fr):
    """The whole conveyor contract: project_id, activity and signals inside _meta, end to end."""
    async with environment(database) as env:
        use_fr_project(env)
        driver = make_driver(env, n8n, fr)
        created = (await protocol.call(env, meta=WITH_ACTOR, arguments={"title": "Demo"}))["result"]
        assert created["resultType"] == "task"
        block = created["_meta"]["ai.nonagentic/fr"]
        assert block == {"project_id": None, "activity": [], "signals": []}
        task = created["taskId"]

        await driver.tick()
        started = await view(env, task)
        assert [e["kind"] for e in started["_meta"]["ai.nonagentic/fr"]["activity"]] == [
            "started",
            "phase",
        ]
        assert started["_meta"]["ai.nonagentic/fr"]["project_id"] is None

        assert (await post_event(env, project_created(task))).status_code == 204
        await driver.tick()
        polling = (await view(env, task))["_meta"]["ai.nonagentic/fr"]
        assert polling["project_id"] == "proj-1"
        assert [(e["kind"], e["text"]) for e in polling["activity"]] == [
            ("started", "The project has started."),
            ("phase", "waiting for the project start to be approved"),
            ("phase", "active"),
        ]
        assert all(
            e["severity"] in {"info", "low", "medium", "high", "critical"}
            for e in polling["activity"]
        )
        assert polling["signals"] == []

        # A tick that sees the same phase adds nothing: the feed is a change log, not a sampler.
        await driver.tick()
        assert (await view(env, task))["_meta"]["ai.nonagentic/fr"] == polling

        fr.project_status = "delivered"
        await driver.tick()
        done = await view(env, task)
        assert done["status"] == "completed"
        final = done["_meta"]["ai.nonagentic/fr"]
        assert [(e["kind"], e["severity"]) for e in final["activity"]][-1] == (
            "delivered",
            "info",
        )
        assert [e["seq"] for e in final["activity"]] == sorted(e["seq"] for e in final["activity"])
        assert all(e["at"].endswith("+00:00") for e in final["activity"])


async def test_fr_task_with_approval(database, n8n, fr):
    async with environment(database) as env:
        use_fr_project(env)
        driver = make_driver(env, n8n, fr)
        task = await protocol.new_task(env, meta=WITH_ACTOR, arguments={"title": "Demo"})

        await driver.tick()
        (start,) = fr.texts("projects.create ")
        assert start["address"] == GOOD_ACTOR["address"] and start["correlation_id"] == task
        assert json.loads(start["text"].removeprefix("projects.create "))["project_type"] == "demo"
        assert (await view(env, task))["status"] == "working"

        question = message(task, "approval-request", "Create the project?", approvalId="ap-start")
        assert (await post_event(env, question)).status_code == 204
        waiting = await view(env, task)
        assert waiting["status"] == "input_required"
        ((key, request),) = waiting["inputRequests"].items()
        assert request == elicitation_request("Create the project?")

        update = await answer(env, task, key)
        assert update["result"]["resultType"] == "complete"
        assert (await view(env, task))["status"] == "working"

        await driver.tick()
        (approve,) = fr.texts("approve ")
        assert approve["text"] == "approve ap-start"
        assert approve["address"] == GOOD_ACTOR["address"]
        assert approve["correlation_id"] == f"{task}:{key}"

        closed = {"type": "approval.closed", "approval_id": "ap-start", "final_status": "approved"}
        assert (await post_event(env, closed)).status_code == 204
        assert (await post_event(env, project_created(task))).status_code == 204
        await driver.tick()
        (poll,) = fr.texts("projects.get ")
        assert poll["text"] == 'projects.get {"id":"proj-1"}'
        assert poll["correlation_id"] == f"{task}:poll"
        assert (await view(env, task))["status"] == "working"

        fr.project_status = "delivered"
        await driver.tick()
        done = await view(env, task)
        assert done["status"] == "completed" and done["result"]["isError"] is False
        assert "proj-1" in json.dumps(done["result"])


async def test_fr_conveyor_question_surfaces_after_project_exists(database, n8n, fr):
    async with environment(database) as env:
        driver = make_driver(env, n8n, fr)
        task = await fr_task_with_project(env, driver, fr)
        assert (await view(env, task))["status"] == "working"

        question = {
            "type": "approval.requested",
            "approval_id": "ap-2",
            "project_id": "proj-1",
            "address": GOOD_ACTOR["address"],
            "description": "Approve the plan",
            "change_summary": "Adds a table",
        }
        assert (await post_event(env, question)).status_code == 204
        waiting = await view(env, task)
        assert waiting["status"] == "input_required"
        ((key, request),) = waiting["inputRequests"].items()
        assert request == elicitation_request("Approve the plan", "Adds a table")

        await answer(env, task, key)
        await driver.tick()
        (approve,) = fr.texts("approve ")
        assert approve["text"] == "approve ap-2" and approve["address"] == GOOD_ACTOR["address"]

        closed = {"type": "approval.closed", "approval_id": "ap-2", "project_id": "proj-1"}
        assert (await post_event(env, closed)).status_code == 204
        assert (await view(env, task))["status"] == "working"


async def test_refused_answer_reopens_input(database, n8n, fr):
    async with environment(database) as env:
        driver = make_driver(env, n8n, fr)
        task = await fr_task_with_project(env, driver, fr)
        question = {
            "type": "approval.requested",
            "approval_id": "ap-2",
            "project_id": "proj-1",
            "description": "Approve the plan",
        }
        await post_event(env, question)
        ((key, _),) = (await view(env, task))["inputRequests"].items()

        fr.answer_response = {"status": "unknown-sender"}
        await answer(env, task, key)
        await driver.tick()
        reopened = await view(env, task)
        assert reopened["status"] == "input_required" and key in reopened["inputRequests"]
        assert reopened["statusMessage"] == "fr refused the answer: actor_unresolved"

        fr.answer_response = {"status": "result", "result": {"ok": True}}
        await answer(env, task, key)
        await driver.tick()
        assert len(fr.texts("approve ")) == 2
        assert (await view(env, task))["status"] == "working"


async def test_fr_task_without_actor_fails_actor_required(database, n8n, fr):
    async with environment(database) as env:
        use_fr_project(env)
        driver = make_driver(env, n8n, fr)
        task = await protocol.new_task(env)
        await driver.tick()
        done = await view(env, task)
        assert done["status"] == "completed" and done["result"]["isError"] is True
        assert done["result"]["structuredContent"]["error"]["code"] == "actor_required"
        assert fr.received == []


async def test_n8n_task_input_arrives_by_run_id_and_is_answered_through_fr(database, n8n, fr):
    async with environment(database) as env:
        driver = make_driver(env, n8n, fr)
        task = await protocol.new_task(env, meta=WITH_ACTOR)
        await driver.tick()
        assert len(n8n.webhooks) == 1

        question = {
            "type": "approval.requested",
            "correlation_id": task,
            "approval_id": "ap-n8n",
            "description": "Send the email?",
        }
        assert (await post_event(env, question)).status_code == 204
        waiting = await view(env, task)
        assert waiting["status"] == "input_required"
        ((key, _),) = waiting["inputRequests"].items()

        await answer(env, task, key)
        await driver.tick()
        (approve,) = fr.texts("approve ")
        assert approve["text"] == "approve ap-n8n"
        assert approve["address"] == GOOD_ACTOR["address"]
        assert fr.texts("projects.") == []
        assert (await view(env, task))["status"] == "working"


async def test_cancel_fr_task_while_start_pending_withdraws_it(database, n8n, fr):
    async with environment(database) as env:
        use_fr_project(env)
        driver = make_driver(env, n8n, fr)
        task = await protocol.new_task(env, meta=WITH_ACTOR, arguments={"title": "Demo"})
        await driver.tick()
        assert len(fr.texts("projects.create ")) == 1

        cancel = await task_rpc(env, "tasks/cancel", task)
        assert cancel["result"]["resultType"] == "complete"
        await driver.tick()
        (reject,) = fr.texts("reject ")
        assert reject["text"] == "reject ap-start cancelled by the client"
        assert reject["address"] == GOOD_ACTOR["address"]
        assert (await view(env, task))["status"] == "cancelled"


async def test_cancel_fr_task_after_project_exists_sends_nothing(database, n8n, fr):
    async with environment(database) as env:
        driver = make_driver(env, n8n, fr)
        task = await fr_task_with_project(env, driver, fr)
        before = len(fr.received)

        await task_rpc(env, "tasks/cancel", task)
        await driver.tick()
        assert fr.texts("reject ") == [] and fr.texts("approve ") == []
        assert all(m["text"].startswith("projects.get") for m in fr.received[before:])
        assert (await view(env, task))["status"] == "cancelled"


async def test_n8n_failure_is_completed_with_is_error(database, n8n, fr):
    async with environment(database) as env:
        driver = make_driver(env, n8n, fr)
        task = await protocol.new_task(env)
        await driver.tick()
        n8n.execution = {"status": "error"}
        await driver.tick()
        done = await view(env, task)
        assert done["status"] == "completed" and done["result"]["isError"] is True
        run = await env.runtime.repository.get(TENANT_A, UUID(task))
        assert run["status"] == "failed"


async def test_webhook_without_execution_id_fails_run(database, n8n, fr):
    async with environment(database) as env:
        n8n.webhook_response = {}
        driver = make_driver(env, n8n, fr)
        task = await protocol.new_task(env)
        await driver.tick()
        done = await view(env, task)
        assert done["status"] == "completed" and done["result"]["isError"] is True
        assert done["result"]["structuredContent"]["error"]["code"] == "external_start_failed"
