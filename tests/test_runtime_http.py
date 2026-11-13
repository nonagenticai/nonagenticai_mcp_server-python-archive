"""HTTP runtime contracts, operation authoring and real durable acceptance."""

from uuid import uuid7

import httpx
import pytest
from pydantic import SecretStr

from src.asgi import create_app
from src.catalog.importer import OperationImport, import_drafts
from src.catalog.models import ExactReference
from src.execution.health import worker_app
from tests.fixtures import TENANT_A, operation, service, settings
from tests.lifespan import running_app
from tests.test_authentication import info
from tests.test_bindings import LICENSE
from tests.test_execution import FakeBackend, setup_runtime
from tests.test_mcp_catalog import listening


async def test_http_submission_inspection_cancellation_and_operator_boundary(database, tmp_path):
    runtime = await setup_runtime(database)
    key = tmp_path / "admin"
    key.write_text("a" * 32)
    app = create_app(
        runtime.config.model_copy(
            update={
                "database_url": SecretStr(database.pool.conninfo),
                "admin_key_file": key,
                "admin_tenant_ids": frozenset({TENANT_A}),
            }
        )
    )
    async with running_app(app):
        await app.state.licenses.client.aclose()
        app.state.licenses.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json=info(license_id=str(LICENSE), tenant_id=str(TENANT_A), app={"key": "mcp"}),
                )
            )
        )
        headers = {
            "X-Tenant-Id": "Bearer synthetic",
            "X-MCP-Binding": runtime.token,
            "X-MCP-Idempotency-Key": "same",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/mcp/runs", headers=headers, json={"tool": "synthetic_tool", "inputs": {}}
            )
            assert response.status_code == 202, response.text
            run = response.json()
            assert run["status"] == "queued"
            assert (
                await client.post(
                    "/mcp/runs", headers=headers, json={"tool": "synthetic_tool", "inputs": {}}
                )
            ).json() == run
            path = "/mcp/runs/" + run["run_id"]
            assert (await client.get(path, headers=headers)).json() == run
            assert len((await client.get(path + "/events", headers=headers)).json()["items"]) == 1
            assert (await client.post(path + "/cancel", headers=headers)).json()["cancel_requested"]
            admin = {"X-Tenant-Id": "Bearer synthetic", "X-MCP-Admin-Key": "a" * 32}
            assert (await client.get(path, headers=admin)).status_code == 200
            assert (
                await client.post(
                    "/mcp/runs", headers=admin, json={"tool": "synthetic_tool", "inputs": {}}
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/mcp/runs", headers=headers, json={"tool": "hidden", "inputs": {}}
                )
            ).status_code == 403
            app.state.submission.settings = runtime.config.model_copy(
                update={"execution_enabled": False}
            )
            assert (
                await client.post(
                    "/mcp/runs", headers=headers, json={"tool": "synthetic_tool", "inputs": {}}
                )
            ).status_code == 503


async def test_real_mcp_accepts_durable_runs_and_preserves_terminal_error_envelope(database):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from src.catalog.validation import validate_instance

    runtime = await setup_runtime(database)
    app = create_app(
        runtime.config.model_copy(update={"database_url": SecretStr(database.pool.conninfo)})
    )
    async with running_app(app):
        await app.state.licenses.client.aclose()
        app.state.licenses.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json=info(license_id=str(LICENSE), tenant_id=str(TENANT_A), app={"key": "mcp"}),
                )
            )
        )
        headers = {
            "X-Tenant-Id": "Bearer synthetic",
            "X-MCP-Binding": runtime.token,
            "X-MCP-Idempotency-Key": "durable-mcp",
        }
        async with (
            listening(app) as base,
            Client(StreamableHttpTransport(base + "/mcp", headers=headers)) as mcp,
        ):
            accepted = await mcp.call_tool("synthetic_tool", {})
            assert accepted.structured_content["status"] == "queued"
            claimed = await runtime.scheduler.claim(uuid7())
            await runtime.repository.finish(
                claimed, "failed", code="synthetic_failure", absent=True
            )
            failed = await mcp.call_tool("synthetic_tool", {}, raise_on_error=False)
            assert failed.is_error and failed.structured_content["status"] == "failed"
            validate_instance(
                runtime.resolved.public()["outputSchema"], failed.structured_content, runtime.config
            )


async def test_platform_http_authoring_uses_explicit_key_and_pool(database_url, tmp_path):
    key = tmp_path / "admin"
    key.write_text("a" * 32)
    platform_key = tmp_path / "platform"
    platform_key.write_text("p" * 32)
    app = create_app(
        settings(
            database_url=database_url,
            license_introspect_url="https://licenses.example/introspect",
            license_app_key="mcp",
            admin_key_file=key,
            admin_tenant_ids={TENANT_A},
            platform_admin_key_file=platform_key,
            platform_database_url=database_url,
            source_allowed_hosts=("*.example",),
        )
    )
    headers = {
        "X-Tenant-Id": "Bearer synthetic",
        "X-MCP-Admin-Key": "a" * 32,
        "X-MCP-Platform-Admin-Key": "p" * 32,
    }
    async with running_app(app):
        await app.state.licenses.client.aclose()
        app.state.licenses.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json=info(license_id=str(LICENSE), tenant_id=str(TENANT_A), app={"key": "mcp"}),
                )
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            normal = {k: v for k, v in headers.items() if k != "X-MCP-Platform-Admin-Key"}
            assert (
                await client.post(
                    "/mcp/services", headers=normal, json=service().model_dump(mode="json")
                )
            ).status_code == 403
            response = await client.post(
                "/mcp/services", headers=headers, json=service().model_dump(mode="json")
            )
            assert response.status_code == 201, response.text
            assert (
                await client.post(
                    "/mcp/services", headers=headers, json=service().model_dump(mode="json")
                )
            ).status_code == 409
            base = f"/mcp/services/{service().service_id}/versions/1"
            assert (await client.get(base, headers=headers)).json()["status"] == "draft"
            assert (await client.get("/mcp/services?limit=1", headers=headers)).status_code == 200
            assert (await client.post(base + "/validate", headers=headers)).json()[
                "credentials_verified"
            ] is False
            # Publishing needs the named source to be registered and active first.
            refused = await client.post(base + "/publish", headers=headers)
            assert refused.status_code == 409 and refused.json() == {"error": "source_unavailable"}
            registered = await client.post(
                "/mcp/sources",
                headers=headers,
                json={
                    "source_key": service().destination_alias,
                    "base_url": "https://service1.example",
                    "auth": {"kind": "licence", "app_key": service().license_app_key},
                },
            )
            assert registered.status_code == 201, registered.text
            assert (await client.post(base + "/publish", headers=headers)).status_code == 200
            op = operation()
            opbase = f"/mcp/operations/{op.operation_id}/versions"
            assert (
                await client.post(opbase, headers=headers, json=op.model_dump(mode="json"))
            ).status_code == 201
            assert (await client.post(opbase + "/1/validate", headers=headers)).status_code == 200
            assert (await client.post(opbase + "/1/publish", headers=headers)).status_code == 200
            assert (await client.get(opbase + "/1", headers=headers)).json()[
                "status"
            ] == "published"
            assert (await client.get("/mcp/operations", headers=headers)).status_code == 200
            assert (await client.post(opbase + "/1/disable", headers=headers)).status_code == 200
            append = {
                "manifest": service(version=2).model_dump(mode="json"),
                "expected_revision": 1,
            }
            assert (
                await client.post(
                    f"/mcp/services/{service().service_id}/versions", headers=headers, json=append
                )
            ).status_code == 201


def openapi():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/records/{record}": {
                "post": {
                    "parameters": [
                        {
                            "in": "path",
                            "name": "record",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
                    },
                }
            }
        },
    }


def test_openapi_import_creates_strict_unpublished_drafts():
    payload = OperationImport(
        service=ExactReference(id=service().service_id, version=1), document=openapi()
    )
    drafts = import_drafts(payload, settings())
    assert len(drafts) == 1 and drafts[0].effect == "writes"
    assert drafts[0].input_schema["additionalProperties"] is False and drafts[0].scope_rules == ()
    for bad in [
        {"openapi": "3.0.0", "paths": {}},
        {"openapi": "3.1.0", "paths": {}},
        {"openapi": "3.1.0", "paths": {"/x": {"trace": {}}}},
    ]:
        with pytest.raises(ValueError):
            import_drafts(payload.model_copy(update={"document": bad}), settings())


async def test_internal_worker_probes_and_metrics(database, tmp_path):
    runtime = await setup_runtime(database)

    class Credentials:
        def read(self):
            return None

    backend = FakeBackend()
    app = worker_app(runtime.repository, runtime.scheduler, backend, Credentials())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 200
        metrics = (await client.get("/metrics")).text
        assert "mcp_execution_queued 0" in metrics
        assert "tenant_id" not in metrics
        runtime.scheduler.settings = runtime.config.model_copy(update={"execution_global_slots": 1})
        assert (await client.get("/health/ready")).status_code == 503
