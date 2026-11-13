"""io.modelcontextprotocol/tasks over a real socket, real ExecutionService, external tool."""

import dataclasses
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from src.asgi import create_app
from src.storage.task_inputs import TaskInputRepository
from src.tasks.models import TASKS_EXTENSION_ID
from tests.fixtures import TENANT_A, TENANT_B, external_tool
from tests.lifespan import running_app
from tests.test_authentication import info
from tests.test_bindings import LICENSE, PRINCIPAL
from tests.test_execution import setup_runtime
from tests.test_mcp_catalog import MODERN, listening, modern_meta, modern_rpc, rpc

EXT = {TASKS_EXTENSION_ID: {}}
ACTOR_META = "ai.nonagentic/actor"
GOOD_ACTOR = {"channel": "mcp", "address": "tester@example.com"}
ACCEPT = {"action": "accept", "content": {"decision": "approved"}}


@asynccontextmanager
async def environment(database, *, tasks_enabled=True):
    runtime = await setup_runtime(database, tasks_enabled=tasks_enabled)
    flags = {"foreign": False}
    app = create_app(
        runtime.config.model_copy(update={"database_url": SecretStr(database.pool.conninfo)})
    )
    async with running_app(app):
        await app.state.licenses.client.aclose()

        def respond(request):
            tenant = TENANT_B if flags["foreign"] else TENANT_A
            return httpx.Response(
                200,
                json=info(license_id=str(LICENSE), tenant_id=str(tenant), app={"key": "mcp"}),
            )

        app.state.licenses.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        resolver = app.state.catalog_resolver
        original = resolver.resolve

        async def resolve(principal):
            return tuple(
                dataclasses.replace(r, manifest_json=external_tool().model_dump_json())
                for r in await original(principal)
            )

        resolver.resolve = resolve
        async with listening(app) as base, httpx.AsyncClient() as http:
            yield SimpleNamespace(
                runtime=runtime,
                app=app,
                base=base + "/mcp",
                http=http,
                headers={"X-Tenant-Id": "Bearer synthetic", "X-MCP-Binding": runtime.token},
                inputs=TaskInputRepository(database.pool),
                flags=flags,
            )


async def call(env, *, extensions=EXT, headers=None, meta=None, arguments=None):
    _, payload = await rpc(
        env.http,
        env.base,
        "tools/call",
        {
            "name": "synthetic_tool",
            "arguments": arguments or {},
            "_meta": {**modern_meta(extensions), **(meta or {})},
        },
        {
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": "tools/call",
            **env.headers,
            "Mcp-Name": "synthetic_tool",
            **(headers or {}),
        },
    )
    return payload


async def task_rpc(env, method, task_id, *, extensions=EXT, extra=None, meta=None, headers=None):
    params = {"taskId": task_id, **(extra or {})}
    params["_meta"] = {**modern_meta(extensions), **(meta or {})}
    _, payload = await rpc(
        env.http,
        env.base,
        method,
        params,
        {
            "MCP-Protocol-Version": MODERN,
            "Mcp-Method": method,
            "Mcp-Name": task_id,
            **env.headers,
            **(headers or {}),
        },
    )
    # JSON-RPC errors travel with a non-200 status; rpc() hands those back as text.
    return json.loads(payload["http_error"]) if "http_error" in payload else payload


async def new_task(env, meta=None, arguments=None):
    payload = await call(env, meta=meta, arguments=arguments)
    return payload["result"]["taskId"]


def code(payload):
    return payload["error"]["code"]


async def test_discover_advertises_tasks_extension(database):
    async with environment(database) as env:
        _, payload = await modern_rpc(env.http, env.base, "server/discover", {}, env.headers)
        assert payload["result"]["capabilities"]["extensions"] == EXT
        assert payload["result"]["capabilities"]["tools"] == {"listChanged": False}


async def test_tasks_disabled_changes_nothing(database):
    async with environment(database, tasks_enabled=False) as env:
        _, payload = await modern_rpc(env.http, env.base, "server/discover", {}, env.headers)
        assert payload["result"]["capabilities"] == {"tools": {"listChanged": False}}
        result = (await call(env))["result"]
        assert result["resultType"] == "complete"
        assert result["structuredContent"]["status"] == "queued"


async def test_tools_call_returns_task_when_declared(database):
    async with environment(database) as env:
        result = (await call(env))["result"]
        assert result["resultType"] == "task"
        run = await env.runtime.repository.get(TENANT_A, UUID(result["taskId"]))
        assert str(run["run_id"]) == result["taskId"] and result["status"] == "working"


async def test_tools_call_without_extension_returns_envelope(database):
    async with environment(database) as env:
        result = (await call(env, extensions=None))["result"]
        assert result["resultType"] == "complete" and "taskId" not in result
        assert result["structuredContent"]["status"] == "queued"
        result = (await call(env, extensions={}))["result"]
        assert result["structuredContent"]["status"] == "queued"


async def test_legacy_initialize_wire_returns_envelope(database):
    async with environment(database) as env:
        headers = {"Accept": "application/json, text/event-stream", **env.headers}
        init = await env.http.post(
            env.base,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"extensions": EXT},
                    "clientInfo": {"name": "legacy", "version": "1"},
                },
            },
        )
        assert init.status_code == 200, init.text
        _, payload = await rpc(
            env.http,
            env.base,
            "tools/call",
            {
                "name": "synthetic_tool",
                "arguments": {},
                "_meta": {"io.modelcontextprotocol/clientCapabilities": {"extensions": EXT}},
            },
            {**env.headers, "MCP-Protocol-Version": "2025-06-18"},
        )
        assert "taskId" not in str(payload) and "queued" in str(payload)


async def test_task_result_survives_serializer(database):
    async with environment(database) as env:
        response = await env.http.post(
            env.base,
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": MODERN,
                "Mcp-Method": "tools/call",
                "Mcp-Name": "synthetic_tool",
                **env.headers,
            },
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "synthetic_tool",
                    "arguments": {},
                    "_meta": modern_meta(EXT),
                },
            },
        )
        assert response.status_code == 200
        assert '"taskId"' in response.text and '"resultType":"task"' in response.text.replace(
            " ", ""
        )


async def test_tasks_get_working_then_completed(database):
    async with environment(database) as env:
        task = await new_task(env)
        view = (await task_rpc(env, "tasks/get", task))["result"]
        assert view["resultType"] == "complete" and view["status"] == "working"
        await _finish(env, task)
        view = (await task_rpc(env, "tasks/get", task))["result"]
        assert view["status"] == "completed" and view["result"]["isError"] is False


async def _finish(env, task):
    # External runs are never claimed by the Kata scheduler: move it through the driver's states.
    async with env.runtime.database.pool.connection() as conn:
        for status in ("starting", "running"):
            await conn.execute(
                "UPDATE public.mcp_runs SET status=%s, started_at=coalesce(started_at, now()) "
                "WHERE run_id=%s",
                (status, UUID(task)),
            )
        await conn.execute(
            "UPDATE public.mcp_runs SET status='succeeded', finished_at=now(), result='{}'::jsonb "
            "WHERE run_id=%s",
            (UUID(task),),
        )
    return task


async def test_tasks_get_other_tenant_is_invalid_params(database):
    async with environment(database) as env:
        task = await new_task(env)
        unknown = await task_rpc(env, "tasks/get", str(uuid4()))
        assert code(unknown) == -32602
        env.flags["foreign"] = True
        foreign = {"X-Tenant-Id": "Bearer foreign"}
        payload = await task_rpc(env, "tasks/get", task, headers=foreign)
        # The foreign tenant cannot resolve the binding at all; never a task view.
        assert "result" not in payload


async def test_same_tenant_different_binding_is_invalid_params(database):
    async with environment(database) as env:
        task = await new_task(env)
        runtime = env.runtime
        spec = runtime.binding.spec
        _, token = await runtime.access.binding_write(PRINCIPAL, spec)
        other = {"X-Tenant-Id": "Bearer synthetic", "X-MCP-Binding": token}
        own = await task_rpc(env, "tasks/get", task)
        assert own["result"]["status"] == "working"
        foreign = await task_rpc(env, "tasks/get", task, headers=other)
        assert code(foreign) == -32602 and foreign["error"]["message"] == "task not found"
        unknown = await task_rpc(env, "tasks/get", str(uuid4()))
        assert unknown["error"] == foreign["error"]


async def test_expired_payload_is_invalid_params(database):
    async with environment(database) as env:
        task = await new_task(env)
        await _finish(env, task)
        async with env.runtime.database.pool.connection() as conn:
            await conn.execute(
                "UPDATE public.mcp_runs SET payload_expired_at=now(), inputs='{}', authority='{}', "
                "profile_snapshot='{}', result=NULL WHERE run_id=%s",
                (UUID(task),),
            )
        assert code(await task_rpc(env, "tasks/get", task)) == -32602


async def test_non_uuid_task_id_is_invalid_params(database):
    async with environment(database) as env:
        assert code(await task_rpc(env, "tasks/get", "not-a-uuid")) == -32602
        assert code(await task_rpc(env, "tasks/cancel", "not-a-uuid")) == -32602


async def test_tasks_get_undeclared_is_32021(database):
    async with environment(database) as env:
        task = await new_task(env)
        for extensions in (None, {}):
            payload = await task_rpc(env, "tasks/get", task, extensions=extensions)
            assert code(payload) == -32021
            assert payload["error"]["data"] == {"requiredCapabilities": {"extensions": EXT}}


async def test_mcp_name_mismatch_is_32020(database):
    async with environment(database) as env:
        task = await new_task(env)
        payload = await task_rpc(env, "tasks/get", task, headers={"Mcp-Name": str(uuid4())})
        assert code(payload) == -32020


async def test_tasks_cancel_sets_cancel_requested(database):
    async with environment(database) as env:
        task = await new_task(env)
        payload = await task_rpc(env, "tasks/cancel", task)
        assert payload["result"]["resultType"] == "complete"
        row = await env.runtime.repository.get(TENANT_A, UUID(task))
        assert row["cancel_requested"] is True


async def test_tasks_update_records_answer_and_flips_status(database):
    async with environment(database) as env:
        task = await new_task(env)
        key = await env.inputs.open(TENANT_A, UUID(task), "ap-1", {"message": "ok?"})
        view = (await task_rpc(env, "tasks/get", task))["result"]
        assert view["status"] == "input_required" and key in view["inputRequests"]
        payload = await task_rpc(
            env,
            "tasks/update",
            task,
            extra={"inputResponses": {key: ACCEPT}},
            meta={ACTOR_META: GOOD_ACTOR},
        )
        assert payload["result"]["resultType"] == "complete"
        assert (await task_rpc(env, "tasks/get", task))["result"]["status"] == "working"
        states = [i["state"] for i in await env.inputs.open_for(TENANT_A, UUID(task))]
        assert states == ["answered"]


@pytest.mark.parametrize(
    "actor",
    [
        None,
        "tester",
        {"channel": "web", "address": "a"},
        {"channel": "mcp"},
        {"channel": "mcp", "address": ""},
        {"channel": "mcp", "address": "x" * 257},
        {"channel": "mcp", "address": 5},
    ],
)
async def test_tasks_update_bad_or_missing_actor_is_invalid_params(database, actor):
    async with environment(database) as env:
        task = await new_task(env)
        key = await env.inputs.open(TENANT_A, UUID(task), "ap-1", {})
        meta = {} if actor is None else {ACTOR_META: actor}
        payload = await task_rpc(
            env, "tasks/update", task, extra={"inputResponses": {key: ACCEPT}}, meta=meta
        )
        assert code(payload) == -32602
        states = [i["state"] for i in await env.inputs.open_for(TENANT_A, UUID(task))]
        assert states == ["open"]


async def test_tasks_update_input_responses_must_be_object(database):
    async with environment(database) as env:
        task = await new_task(env)
        payload = await task_rpc(
            env, "tasks/update", task, extra={"inputResponses": []}, meta={ACTOR_META: GOOD_ACTOR}
        )
        assert payload == {"error": "invalid_protocol_request"}


async def test_tasks_at_legacy_protocol_version_is_method_not_found(database):
    async with environment(database) as env:
        task = await new_task(env)
        _, payload = await rpc(
            env.http,
            env.base,
            "tasks/get",
            {"taskId": task},
            {**env.headers, "MCP-Protocol-Version": "2025-06-18"},
        )
        assert "result" not in payload


async def test_tools_call_stores_valid_actor_on_run(database):
    async with environment(database) as env:
        task = await new_task(env, meta={ACTOR_META: {**GOOD_ACTOR, "extra": "dropped"}})
        run = await env.runtime.repository.get(TENANT_A, UUID(task))
        assert run["actor"] == GOOD_ACTOR


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {ACTOR_META: "tester"},
        {ACTOR_META: {"channel": "web", "address": "a"}},
        {ACTOR_META: {"channel": "mcp"}},
        {ACTOR_META: {"channel": "mcp", "address": "x" * 257}},
    ],
)
async def test_tools_call_without_valid_actor_stores_null(database, meta):
    async with environment(database) as env:
        payload = await call(env, meta=meta)
        assert payload["result"]["resultType"] == "task"
        run = await env.runtime.repository.get(TENANT_A, UUID(payload["result"]["taskId"]))
        assert run["actor"] is None


@pytest.mark.parametrize(
    "actor, expected",
    [
        ({"channel": "mcp", "address": "a"}, {"channel": "mcp", "address": "a"}),
        (
            {"channel": "mcp", "address": "a", "job": "01900000-0000-7000-8000-000000000001"},
            {"channel": "mcp", "address": "a", "job": "01900000-0000-7000-8000-000000000001"},
        ),
        (
            {"channel": "mcp", "address": "a", "on_behalf_of": "sub", "x": 1},
            {"channel": "mcp", "address": "a", "on_behalf_of": "sub"},
        ),
        ({"channel": "mcp", "address": "a", "job": "not-a-uuid"}, None),
        ({"channel": "mcp", "address": "a", "job": 7}, None),
        ({"channel": "mcp", "address": "a", "on_behalf_of": ""}, None),
        ({"channel": "mcp", "address": "a", "on_behalf_of": "x" * 257}, None),
        ({"channel": "other", "address": "a"}, None),
        ({"channel": "mcp"}, None),
    ],
)
def test_parse_actor_optional_job_and_on_behalf_of(actor, expected):
    from src.tasks.extension import ACTOR_META_KEY, parse_actor

    assert parse_actor({"_meta": {ACTOR_META_KEY: actor}}) == expected
