"""Tenant publication acceptance over HTTP using the restricted runtime role."""

import asyncio
import json
from uuid import uuid7

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from scripts.grant_runtime_role import grant
from src.asgi import create_app
from tests.fixtures import FILES, TENANT_A, TENANT_B, TOOL, settings, tool
from tests.lifespan import running_app
from tests.test_access_http import ADMIN
from tests.test_authentication import info
from tests.test_bindings import LICENSE


@pytest.fixture
async def catalog_api(database_url, tmp_path):
    role = "catalog_http_" + uuid7().hex
    parsed = make_url(database_url)
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    password = "synthetic-role-only"  # pragma: allowlist secret
    with psycopg.connect(dsn) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    grant(database_url, role)
    runtime = parsed.set(username=role, password=password).render_as_string(hide_password=False)
    key = tmp_path / "admin"
    key.write_text("a" * 32)
    app = create_app(
        settings(
            database_url=runtime,
            license_introspect_url="https://licenses.example/introspect",
            license_app_key="mcp",
            admin_key_file=key,
            admin_tenant_ids={TENANT_A, TENANT_B},
            catalog_page_size=1,
        )
    )
    try:
        async with running_app(app):
            await app.state.licenses.client.aclose()
            app.state.licenses.client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(
                        200,
                        json=info(
                            license_id=str(LICENSE),
                            tenant_id=str(
                                TENANT_B
                                if json.loads(req.content)["token"] == "foreign"
                                else TENANT_A
                            ),
                            app={"key": "mcp"},
                        ),
                    )
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://127.0.0.1",
            ) as client:
                yield app, client
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def upload(manifest=None, files=FILES, **extra):
    return {
        "manifest": (manifest or tool()).model_dump(mode="json"),
        "files": [f.model_dump() for f in files],
        **extra,
    }


async def test_operator_complete_tool_lifecycle(catalog_api):
    app, client = catalog_api
    response = await client.post("/mcp/tools", headers=ADMIN, json=upload())
    assert response.status_code == 201, response.text
    assert response.json()["revision"] == 1
    path = f"/mcp/tools/{TOOL}/versions/1"
    inspected = (await client.get(path, headers=ADMIN)).json()
    assert inspected["descriptor"]["manifest"] == tool().model_dump(mode="json")
    assert sorted(inspected["files"], key=lambda f: f["path"]) == sorted(
        upload()["files"], key=lambda f: f["path"]
    )
    before = await app.state.catalog.repository.generation()
    validated = await client.post(path + "/validate", headers=ADMIN)
    assert validated.status_code == 200 and validated.json()["credentials_verified"] is False
    assert await app.state.catalog.repository.generation() == before
    published = await client.post(path + "/publish", headers=ADMIN, json={"expected_revision": 1})
    assert published.json()["status"] == "published"
    # Exact next version is checked under the identity lock.
    assert (
        await client.post(
            f"/mcp/tools/{TOOL}/versions", headers=ADMIN, json=upload(expected_revision=2)
        )
    ).status_code == 409
    responses = await asyncio.gather(
        *[
            client.post(
                f"/mcp/tools/{TOOL}/versions",
                headers=ADMIN,
                json=upload(tool(version=2), expected_revision=2),
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    listing = (await client.get("/mcp/tools/definitions?limit=1&offset=1", headers=ADMIN)).json()
    assert listing["items"][0]["version"] == 2 and "manifest" not in listing["items"][0]
    assert (await client.get("/mcp/tools/definitions?limit=101", headers=ADMIN)).status_code == 422
    assert (
        await client.post(path + "/disable", headers=ADMIN, json={"expected_revision": 3})
    ).json()["status"] == "disabled"
    assert (
        await client.post(path + "/publish", headers=ADMIN, json={"expected_revision": 4})
    ).status_code == 409
    assert (
        await client.post(path + "/retire", headers=ADMIN, json={"expected_revision": 4})
    ).json()["status"] == "retired"
    foreign = {**ADMIN, "X-Tenant-Id": "Bearer foreign"}
    for suffix in ["", "/validate", "/publish"]:
        response = await client.request(
            "GET" if not suffix else "POST",
            path + suffix,
            headers=foreign,
            json={"expected_revision": 5} if suffix == "/publish" else None,
        )
        assert response.status_code == 404
    assert (await client.post("/mcp/tools", headers=foreign, json=upload())).status_code == 403
    async with app.state.database.pool.connection() as conn:
        events = await (
            await conn.execute(
                "SELECT action,artifact_version,revision,license_id FROM mcp_access_events ORDER BY event_id"
            )
        ).fetchall()
        assert [(r["action"], r["artifact_version"], r["revision"]) for r in events] == [
            ("tool.created", 1, 1),
            ("tool.published", 1, 2),
            ("tool.version_created", 2, 3),
            ("tool.disabled", 1, 4),
            ("tool.retired", 1, 5),
        ]
        assert all(r["license_id"] == LICENSE for r in events)


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "schema_version",
        "duplicate",
        "traversal",
        "digest",
        "image",
        "reserved",
        "bound",
        "open",
        "uuid",
        "partial",
        "identity",
        "result_schema",
    ],
)
async def test_invalid_uploads_leave_no_rows(catalog_api, change):
    app, client = catalog_api
    payload = upload()
    if change == "extra":
        payload["actor"] = {"subject": "forged-secret"}
    if change == "schema_version":
        payload["manifest"]["schema_version"] = 2
    if change == "duplicate":
        payload["files"].append(payload["files"][0])
    if change == "traversal":
        payload["files"][0]["path"] = "../private.py"
    if change == "digest":
        payload["manifest"]["package_sha256"] = "b" * 64
    if change == "image":
        payload["manifest"]["runner_image_digest"] = "unapproved@sha256:" + "b" * 64
    if change == "reserved":
        payload["manifest"]["public_name"] = "mcp_run_status"
    if change == "bound":
        payload["manifest"]["input_schema"]["properties"] = {"tenant_id": {"type": "string"}}
    if change == "open":
        payload["manifest"]["input_schema"].pop("additionalProperties")
    if change == "uuid":
        payload["manifest"]["tool_id"] = "20000000-0000-4000-8000-000000000001"
    if change == "partial":
        payload["files"] = []
    if change == "identity":
        payload["manifest"]["version"] = 5
    if change == "result_schema":
        payload["manifest"]["result_schema"] = {"$defs": False}
    response = await client.post("/mcp/tools", headers=ADMIN, json=payload)
    assert response.status_code in {409, 422}, response.text
    assert "forged-secret" not in response.text
    assert (await client.get("/mcp/tools/definitions", headers=ADMIN)).json()["items"] == []
    async with app.state.database.pool.connection() as conn:
        assert (
            await (await conn.execute("SELECT count(*) AS n FROM mcp_access_events")).fetchone()
        )["n"] == 0


async def test_audit_failure_rolls_back_content_and_generation(catalog_api, monkeypatch):
    app, client = catalog_api
    before = await app.state.catalog.repository.generation()

    async def broken(*args, **kwargs):
        raise RuntimeError("private-database-error")

    with monkeypatch.context() as patch:
        patch.setattr("src.storage.bindings.audit", broken)
        response = await client.post("/mcp/tools", headers=ADMIN, json=upload())
        assert response.status_code == 500 and "private-database-error" not in response.text
    assert await app.state.catalog.repository.generation() == before
    assert (await client.get("/mcp/tools/definitions", headers=ADMIN)).json()["items"] == []
    assert (
        await client.post("/mcp/tools", headers=ADMIN, content=b"x" * 262145)
    ).status_code == 413


def test_openapi_describes_strict_upload_and_revision_contracts():
    document = create_app(settings()).openapi()
    for path in [
        "/mcp/tools",
        "/mcp/tools/{identity}/versions",
        "/mcp/tools/{identity}/versions/{version}/publish",
    ]:
        schema = document["paths"][path]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        assert schema["additionalProperties"] is False
        assert schema["$id"].startswith("urn:nonagentic:catalog:")
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)
        payload = (
            upload()
            if path == "/mcp/tools"
            else (
                upload(tool(version=2), expected_revision=1)
                if path.endswith("/versions")
                else {"expected_revision": 1}
            )
        )
        Draft202012Validator(schema).validate(payload)
