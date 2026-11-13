"""Runtime tool-source registry: guard, encryption, operator API, broker, generic import."""

import base64
import json
from uuid import uuid7

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from src.asgi import create_app
from src.execution.models import ExecutionDeniedError
from src.sources.crypto import decode_key, decrypt_token, encrypt_token
from src.sources.guard import checked_base_url, enforce, host_allowed
from src.sources.registry import SourceRegistry
from tests.fixtures import IMAGE, TENANT_A, register_source, service, settings
from tests.lifespan import running_app
from tests.test_authentication import info
from tests.test_bindings import LICENSE
from tests.test_execution import claim, make_broker, setup_runtime
from tests.test_platform_seed import load

KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
SECRET = "super-secret-token-value-123"  # pragma: allowlist secret


# --- guard ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed", "ok"),
    [
        ("https://api.nonagentic.ai/coding", ["api.nonagentic.ai"], True),
        ("https://api.nonagentic.ai", ["*.nonagentic.ai"], True),
        ("https://nonagentic.ai", ["*.nonagentic.ai"], False),
        ("https://evilnonagentic.ai", ["*.nonagentic.ai"], False),
        ("https://api.nonagentic.ai", [], False),
        ("https://API.nonagentic.ai", ["api.nonagentic.ai"], True),
        ("https://api.nonagentic.ai:8443", ["api.nonagentic.ai"], False),
        ("https://api.nonagentic.ai:8443", ["api.nonagentic.ai:8443"], True),
        ("https://api.nonagentic.ai:443", ["api.nonagentic.ai"], True),
        ("https://api.nonagentic.ai", ["api.nonagentic.ai:8443"], False),
        ("https://x.internal:9000/a", ["*.internal:9000"], True),
    ],
)
def test_allow_list(url, allowed, ok):
    assert host_allowed(url, allowed) is ok


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "https://[::1]/x",
        "https://10.0.0.1:8443",
        "https://2130706433",
        "https://0x7f.1",
        "https://127.1",
        "https://user@api.nonagentic.ai",
        "https://:pw@api.nonagentic.ai",
        "http://api.nonagentic.ai",
        "https://api.nonagentic.ai?x=1",
        "https://api.nonagentic.ai/a/../b",
        "https://api.nonagentic.ai:99999",
        "ftp://api.nonagentic.ai",
    ],
)
def test_guard_rejects_unsafe_origins(url):
    with pytest.raises(ValueError):
        enforce(url, False, ["*", "*.ai", "api.nonagentic.ai"])


def test_http_only_when_explicitly_internal():
    assert checked_base_url("http://blocked.internal", True)
    with pytest.raises(ValueError):
        checked_base_url("http://blocked.internal", False)


# --- encryption -------------------------------------------------------------------------------


def test_ciphertext_is_bound_to_its_source():
    one, two = uuid7(), uuid7()
    blob = encrypt_token(SecretStr(KEY), one, SecretStr(SECRET))
    assert SECRET.encode() not in blob
    assert decrypt_token(SecretStr(KEY), one, blob).get_secret_value() == SECRET
    assert blob != encrypt_token(SecretStr(KEY), one, SecretStr(SECRET))  # fresh nonce
    with pytest.raises(ExecutionDeniedError):
        decrypt_token(SecretStr(KEY), two, blob)  # swapped onto another source
    other = base64.urlsafe_b64encode(b"z" * 32).decode()
    with pytest.raises(ExecutionDeniedError):
        decrypt_token(SecretStr(other), one, blob)
    with pytest.raises(ExecutionDeniedError):
        decrypt_token(None, one, blob)
    with pytest.raises(ExecutionDeniedError):
        decrypt_token(SecretStr(KEY), one, blob[:-1] + bytes([blob[-1] ^ 1]))


def test_secret_key_setting_is_validated():
    assert settings(source_secret_key=KEY).source_secret_key is not None
    for bad in ("short", base64.urlsafe_b64encode(b"k" * 31).decode()):
        with pytest.raises(ValueError):
            settings(source_secret_key=bad)
    with pytest.raises(ValueError):
        decode_key("not base64 !!")
    assert settings(source_allowed_hosts="a.example, *.b.example").source_allowed_hosts == (
        "a.example",
        "*.b.example",
    )
    with pytest.raises(ValueError):
        settings(source_allowed_hosts="bad host")


# --- operator API -----------------------------------------------------------------------------

HEADERS = {
    "X-Tenant-Id": "Bearer synthetic",
    "X-MCP-Admin-Key": "a" * 32,
    "X-MCP-Platform-Admin-Key": "p" * 32,
}


@pytest.fixture
async def api(database_url, tmp_path):
    admin, platform = tmp_path / "admin", tmp_path / "platform"
    admin.write_text("a" * 32)
    platform.write_text("p" * 32)
    app = create_app(
        settings(
            database_url=database_url,
            license_introspect_url="https://licenses.example/introspect",
            license_app_key="mcp",
            admin_key_file=admin,
            admin_tenant_ids={TENANT_A},
            platform_admin_key_file=platform,
            platform_database_url=database_url,
            source_secret_key=KEY,
            source_allowed_hosts=("*.example", "*.nonagentic.ai"),
            runner_image_digest=IMAGE,
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            yield app, client, database_url


def rows(url, query, *args):
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        return conn.execute(query, args).fetchall()


STATIC = {
    "source_key": "coding",
    "base_url": "https://api.nonagentic.ai/coding",
    "auth": {"kind": "static_bearer"},
    "token": SECRET,
    "idempotency_header": "Idempotency-Key",
    "max_response_bytes": 4096,
}


async def test_source_lifecycle_never_exposes_the_token(api, caplog):
    app, client, url = api
    created = await client.post("/mcp/sources", headers=HEADERS, json=STATIC)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["has_token"] is True and body["revision"] == 1 and body["status"] == "active"
    assert body["auth"] == {"kind": "static_bearer"}
    seen = [created.text]
    seen.append((await client.get("/mcp/sources/coding", headers=HEADERS)).text)
    seen.append((await client.get("/mcp/sources", headers=HEADERS)).text)
    # Duplicate key and wrong shapes.
    assert (await client.post("/mcp/sources", headers=HEADERS, json=STATIC)).status_code == 409
    for bad in (
        {**STATIC, "source_key": "b", "token": None},  # static needs a token
        {**STATIC, "source_key": "b", "auth": {"kind": "licence", "app_key": "x"}},  # no token
        {**STATIC, "source_key": "b", "base_url": "https://evil.example.org"},  # not allowed
        {**STATIC, "source_key": "b", "base_url": "https://127.0.0.1"},
        {
            **STATIC,
            "source_key": "b",
            "base_url": "https://user:pw@api.nonagentic.ai",  # pragma: allowlist secret
        },
        {**STATIC, "source_key": "b", "token": "has space"},
        {**STATIC, "source_key": "b", "auth": {"kind": "static_header", "header": "Host"}},
        {**STATIC, "source_key": "b", "extra": 1},
    ):
        refused = await client.post("/mcp/sources", headers=HEADERS, json=bad)
        assert refused.status_code == 422, bad
        seen.append(refused.text)
    # Ciphertext at rest, bound to the row id.
    stored = rows(url, "SELECT token_ciphertext, source_id FROM mcp_sources")
    assert len(stored) == 1 and SECRET.encode() not in bytes(stored[0][0])
    assert (
        decrypt_token(SecretStr(KEY), stored[0][1], bytes(stored[0][0])).get_secret_value()
        == SECRET
    )
    # Update URL/auth with the revision; stale revision conflicts; token stays.
    update = {
        "expected_revision": 1,
        "base_url": "https://api.nonagentic.ai/coding2",
        "auth": {"kind": "static_header", "header": "X-Api-Key"},
    }
    assert (
        await client.put(
            "/mcp/sources/coding", headers=HEADERS, json={**update, "expected_revision": 9}
        )
    ).status_code == 409
    updated = await client.put("/mcp/sources/coding", headers=HEADERS, json=update)
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    assert updated.json()["auth"] == {"kind": "static_header", "header": "X-Api-Key"}
    seen.append(updated.text)
    # Rotate: write-only.
    rotated = await client.put(
        "/mcp/sources/coding/token",
        headers=HEADERS,
        json={"token": SECRET + "-rotated", "expected_revision": 2},
    )
    assert rotated.status_code == 200 and rotated.json()["revision"] == 3
    seen.append(rotated.text)
    after = rows(url, "SELECT token_ciphertext FROM mcp_sources")[0][0]
    assert bytes(after) != bytes(stored[0][0])
    # Licence kind drops the token; disabling and enabling advance the revision.
    licence = {**update, "expected_revision": 3, "auth": {"kind": "licence", "app_key": "coding"}}
    assert (
        await client.put("/mcp/sources/coding", headers=HEADERS, json=licence)
    ).status_code == 200
    assert rows(url, "SELECT token_ciphertext IS NULL FROM mcp_sources")[0][0] is True
    assert (
        await client.put(
            "/mcp/sources/coding/token",
            headers=HEADERS,
            json={"token": "x", "expected_revision": 4},
        )
    ).status_code == 422
    disabled = await client.post(
        "/mcp/sources/coding/disable", headers=HEADERS, json={"expected_revision": 4}
    )
    assert disabled.json()["status"] == "disabled" and disabled.json()["revision"] == 5
    assert (
        await client.post(
            "/mcp/sources/coding/disable", headers=HEADERS, json={"expected_revision": 5}
        )
    ).status_code == 409
    enabled = await client.post(
        "/mcp/sources/coding/enable", headers=HEADERS, json={"expected_revision": 5}
    )
    assert enabled.json()["status"] == "active"
    assert (await client.get("/mcp/sources/missing", headers=HEADERS)).status_code == 404
    # Audit trail: every write, never any credential.
    events = rows(url, "SELECT event, revision FROM mcp_source_events ORDER BY event_id")
    assert [e[0] for e in events] == [
        "created",
        "updated",
        "token_set",
        "updated",
        "disabled",
        "enabled",
    ]
    everything = " ".join(seen) + repr(rows(url, "SELECT * FROM mcp_source_events")) + caplog.text
    assert SECRET not in everything
    # Immutable identity and no deletion, enforced by the database.
    for statement in (
        "UPDATE mcp_sources SET source_key='other'",
        "UPDATE mcp_sources SET status='disabled'",  # revision must advance
        "DELETE FROM mcp_sources",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            rows(url, statement)


async def test_sources_need_the_platform_key_and_a_secret_key(api, database_url):
    app, client, _ = api
    normal = {k: v for k, v in HEADERS.items() if k != "X-MCP-Platform-Admin-Key"}
    for method, path in (("GET", "/mcp/sources"), ("GET", "/mcp/sources/x")):
        assert (await client.request(method, path, headers=normal)).status_code == 403
    assert (await client.post("/mcp/sources", headers=normal, json=STATIC)).status_code == 403
    # Without MCP_SOURCE_SECRET_KEY a static token cannot be stored; licence sources still can.
    app.state.platform_sources.secret_key = None
    refused = await client.post("/mcp/sources", headers=HEADERS, json=STATIC)
    assert refused.status_code == 503 and "source_secret_unavailable" in refused.text
    licence = {**STATIC, "auth": {"kind": "licence", "app_key": "coding"}}
    licence.pop("token")
    assert (await client.post("/mcp/sources", headers=HEADERS, json=licence)).status_code == 201


async def test_publishing_a_service_needs_an_active_agreeing_source(api):
    app, client, _ = api
    manifest = service(destination_alias="svc_source").model_dump(mode="json")
    assert (await client.post("/mcp/services", headers=HEADERS, json=manifest)).status_code == 201
    publish = f"/mcp/services/{manifest['service_id']}/versions/1/publish"
    missing = await client.post(publish, headers=HEADERS)
    assert missing.status_code == 409 and missing.json() == {"error": "source_unavailable"}
    source = {
        "source_key": "svc_source",
        "base_url": "https://one.example",
        "auth": {"kind": "licence", "app_key": "wrong_app"},
    }
    assert (await client.post("/mcp/sources", headers=HEADERS, json=source)).status_code == 201
    assert (await client.post(publish, headers=HEADERS)).status_code == 409  # app_key differs
    fixed = {
        **source,
        "expected_revision": 1,
        "auth": {
            "kind": "licence",
            "app_key": manifest["license_app_key"],
            "scheme": "x_tenant_id_bearer",
        },
    }
    fixed.pop("source_key")
    assert (
        await client.put("/mcp/sources/svc_source", headers=HEADERS, json=fixed)
    ).status_code == 200
    assert (await client.post(publish, headers=HEADERS)).status_code == 409  # adapter differs
    fixed["expected_revision"], fixed["auth"]["scheme"] = 2, "bearer"
    assert (
        await client.put("/mcp/sources/svc_source", headers=HEADERS, json=fixed)
    ).status_code == 200
    assert (
        await client.post(
            "/mcp/sources/svc_source/disable", headers=HEADERS, json={"expected_revision": 3}
        )
    ).status_code == 200
    assert (await client.post(publish, headers=HEADERS)).status_code == 409  # disabled
    assert (
        await client.post(
            "/mcp/sources/svc_source/enable", headers=HEADERS, json={"expected_revision": 4}
        )
    ).status_code == 200
    assert (await client.post(publish, headers=HEADERS)).status_code == 200


# --- generic toolkit import -------------------------------------------------------------------


def bundle() -> dict:
    kit = load()[0]
    return {
        "toolkit": kit.toolkit,
        "service": kit.service,
        "operations": kit.operations,
        "tools": [
            {
                "fields": t.fields,
                "op_keys": list(t.op_keys),
                "files": [{"path": p, "content": c} for p, c in sorted(t.files.items())],
            }
            for t in kit.tools
        ],
    }


async def test_generic_toolkit_import(api):
    app, client, url = api
    payload = bundle()
    alias = payload["service"]["destination_alias"]
    missing = await client.post("/mcp/toolkits/import", headers=HEADERS, json=payload)
    assert missing.status_code == 409 and "not registered" in missing.text
    source = {
        "source_key": alias,
        "base_url": "https://demo.example",
        "auth": {"kind": "licence", "app_key": payload["service"]["license_app_key"]},
    }
    assert (await client.post("/mcp/sources", headers=HEADERS, json=source)).status_code == 201
    normal = {k: v for k, v in HEADERS.items() if k != "X-MCP-Platform-Admin-Key"}
    assert (
        await client.post("/mcp/toolkits/import", headers=normal, json=payload)
    ).status_code == 403
    imported = await client.post("/mcp/toolkits/import", headers=HEADERS, json=payload)
    assert imported.status_code == 201, imported.text
    result = imported.json()
    assert result["changed"] is True and result["tools_skipped"] is False
    assert set(result["actions"].values()) == {"created"}
    assert (
        result["versions"]["toolkit:demo"] == 1
        and result["versions"]["tool:demo.demo_get_item"] == 1
    )
    assert rows(url, "SELECT status FROM mcp_toolkit_versions") == [("published",)]
    again = (await client.post("/mcp/toolkits/import", headers=HEADERS, json=payload)).json()
    assert again["changed"] is False and set(again["actions"].values()) == {"unchanged"}
    # Edited content appends the next version through the same path.
    edited = bundle()
    edited["tools"][0]["files"][0]["content"] += "\n# edit\n"
    appended = (await client.post("/mcp/toolkits/import", headers=HEADERS, json=edited)).json()
    assert appended["versions"]["tool:demo.demo_get_item"] == 2
    assert appended["versions"]["toolkit:demo"] == 2
    # Contract errors.
    reserved = bundle()
    reserved["tools"][0]["files"].append({"path": "ops.py", "content": "OPS = {}"})
    unknown = bundle()
    unknown["tools"][0]["op_keys"] = ["nope"]
    for bad in (reserved, unknown, {**bundle(), "extra": 1}):
        assert (
            await client.post("/mcp/toolkits/import", headers=HEADERS, json=bad)
        ).status_code == 422


# --- broker -----------------------------------------------------------------------------------


async def static_broker(runtime, tmp_path, key, auth, token, handler=None):
    """Broker over the runtime's services with source ``destination_1`` re-registered as static."""
    from src.catalog.models import Attribution
    from src.sources.models import SourceUpdate
    from src.sources.repository import SourceRepository
    from tests.fixtures import ACTOR

    config = runtime.config.model_copy(
        update={"source_secret_key": SecretStr(KEY), "source_cache_ttl_seconds": 0}
    )
    repository = SourceRepository(runtime.database.pool, ("*.example",), SecretStr(KEY))
    await repository.update(
        "destination_1",
        SourceUpdate.model_validate_json(
            json.dumps(
                {
                    "expected_revision": 1,
                    "base_url": "https://service1.example/prefix",
                    "auth": auth,
                    "token": token,
                    "idempotency_header": "X-Idempotency-Key",
                }
            )
        ),
        Attribution(subject=ACTOR.subject, license_id=ACTOR.license_id),
    )
    broker, seen, tokens, _ = await make_broker(runtime, tmp_path, handler)
    broker.sources = SourceRegistry(runtime.database.pool, config)
    # A platform service whose source is not licence kind is shared only by explicit grant.
    from src.storage.catalog import PlatformCatalogRepository

    operation, _ = await runtime.catalog.repository.operation(runtime.refs[0])
    await PlatformCatalogRepository(runtime.database.pool).grant_service(
        operation.service.id, TENANT_A, ACTOR
    )
    return broker, seen, repository, config


@pytest.mark.parametrize(
    ("auth", "header", "expected"),
    [
        ({"kind": "static_bearer"}, "Authorization", "Bearer " + SECRET),
        ({"kind": "static_header", "header": "X-Api-Key"}, "X-Api-Key", SECRET),
    ],
)
async def test_broker_uses_the_stored_token_from_the_registry(
    database, tmp_path, auth, header, expected
):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, seen, repository, config = await static_broker(
        runtime, tmp_path, "destination_1", auth, SECRET
    )
    try:
        await broker.preflight(row)  # a stored-token service needs no tenant licence check
        assert await broker.call(row, str(runtime.refs[0].id), {}) == {}
        assert seen[0].headers[header] == expected
        assert str(seen[0].url) == "https://service1.example/prefix/records"
        # The other service still goes through the tenant licence.
        assert await broker.call(row, str(runtime.refs[1].id), {}) == {}
        assert seen[1].headers["Authorization"] == "Bearer synthetic_service_2"
        async with runtime.database.pool.connection() as conn:
            journal = await (await conn.execute("SELECT * FROM mcp_run_api_calls")).fetchall()
        assert SECRET not in repr(journal)
        # Disabling takes effect on the next call.
        await repository.set_status("destination_1", 2, "disabled", runtime_actor())
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
        assert len(seen) == 2
    finally:
        await broker.close()


def runtime_actor():
    from src.catalog.models import Attribution
    from tests.fixtures import ACTOR

    return Attribution(subject=ACTOR.subject, license_id=ACTOR.license_id)


async def test_broker_denies_swapped_ciphertext_wrong_key_and_hosts_outside_the_allow_list(
    database, tmp_path
):
    from src.sources.models import SourceCreate

    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, seen, repository, config = await static_broker(
        runtime, tmp_path, "destination_1", {"kind": "static_bearer"}, SECRET
    )
    ref = str(runtime.refs[0].id)
    try:
        # Out-of-band row edit pointing at a host the allow-list does not name.
        outside = config.model_copy(update={"source_allowed_hosts": ("x.example",)})
        broker.sources = SourceRegistry(runtime.database.pool, outside)
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, ref, {})
        # A different key behaves like corruption.
        wrong = base64.urlsafe_b64encode(b"z" * 32).decode()
        broker.sources = SourceRegistry(
            runtime.database.pool, config.model_copy(update={"source_secret_key": SecretStr(wrong)})
        )
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, ref, {})
        # Another source's ciphertext copied onto destination_1 fails authentication (AAD).
        await repository.create(
            SourceCreate.model_validate_json(
                json.dumps(
                    {
                        "source_key": "other_src",
                        "base_url": "https://other.example",
                        "auth": {"kind": "static_bearer"},
                        "token": "another-token",
                    }
                )
            ),
            runtime_actor(),
        )
        async with runtime.database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_sources SET revision=revision+1, token_ciphertext=("
                "SELECT token_ciphertext FROM mcp_sources WHERE source_key='other_src') "
                "WHERE source_key='destination_1'"
            )
        broker.sources = SourceRegistry(runtime.database.pool, config)
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, ref, {})
        assert seen == []
    finally:
        await broker.close()


async def test_licence_source_must_agree_with_the_service(database, tmp_path):
    runtime = await setup_runtime(database)
    row = await claim(runtime)
    broker, _, _, _ = await make_broker(runtime, tmp_path)
    try:
        async with runtime.database.pool.connection() as conn:
            await conn.execute(
                "UPDATE mcp_sources SET revision=revision+1, auth=jsonb_set(auth,'{app_key}','\"other\"') "
                "WHERE source_key='destination_1'"
            )
        broker.sources = SourceRegistry(
            runtime.database.pool, runtime.config.model_copy(update={"source_cache_ttl_seconds": 0})
        )
        with pytest.raises(ExecutionDeniedError):
            await broker.call(row, str(runtime.refs[0].id), {})
        with pytest.raises(ExecutionDeniedError):
            await broker.preflight(row)
    finally:
        await broker.close()


async def test_cli_seed_skips_toolkits_whose_source_is_not_registered(database):
    from src.platform.seed import seed
    from tests.test_platform_seed import package_files

    report = await seed(database.pool, settings(runner_image_digest=IMAGE), load(), package_files)
    assert report.sources_missing == {"demo": "demo_backend"} and report.actions == {}
    await register_source(database.pool, "demo_backend", app_key="demo")
    again = await seed(database.pool, settings(runner_image_digest=IMAGE), load(), package_files)
    assert again.sources_missing == {} and again.count("created") == 5
