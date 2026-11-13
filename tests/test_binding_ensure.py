"""POST /mcp/internal/tenants/{t}/bindings/ensure on real PostgreSQL, fake licences + Keycloak.

Covers: service-JWT/azp authentication of /mcp/internal, the declarative product table with
skipped toolkits, one binding per (tenant, product), token returned once (digest stored),
idempotent re-ensure, explicit rotation, licence change, audit, concurrency, UUIDv4 licence
authority identities end to end, and the tenant-level (subject_kind) gateway requirement.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid7

import httpx
import pytest

from src.asgi import create_app
from src.auth.licenses import LicenseIdentity, digest_token
from src.auth.principal import Principal
from src.config import PLATFORM_TENANT_ID
from src.storage.bindings_ensure import BindingEnsureService, actor_for
from src.toolkits.models import ToolkitManifest, ToolReference, ToolSelection
from tests.fixtures import ACTOR, FILES, settings, tool
from tests.test_authentication import info
from tests.test_keycloak_auth import AUDIENCE, CLIENT, KC_URL, REALM, _token, kc_env  # noqa: F401

PROVISIONER = "other-client"  # in KEYCLOAK_ALLOWED_CLIENTS (kc_env) and the ensure allow-list
TENANT = UUID("10000000-0000-4000-8000-00000000e001")  # licences SQL-minted tenants are v4
LICENCE = uuid4()
TOOL_ID = UUID("20000000-0000-7000-8000-00000000e001")
PLATFORM = PLATFORM_TENANT_ID
PRODUCTS = {
    "agent": ("coding", "knowledge"),
    "care-bus": ("care_bus_ops",),
    "empty": ("nothing_yet",),
}

BUNDLES = {"agent": "agent", "care-bus": "care-bus", "empty": "agent"}


def path(tenant=TENANT):
    return f"/mcp/internal/tenants/{tenant}/bindings/ensure"


def service_headers(azp=PROVISIONER):
    return {"Authorization": f"Bearer {_token(azp=azp)}"}


@pytest.fixture
async def ensure_api(database_url, tmp_path, kc_env):  # noqa: F811 -- pytest fixture
    key = tmp_path / "admin"
    key.write_text("a" * 32)
    app = create_app(
        settings(
            database_url=database_url,
            license_introspect_url="https://licenses.example/introspect",
            license_app_key="mcp",
            admin_key_file=key,
            admin_tenant_ids={TENANT},
            binding_ensure_clients=frozenset({PROVISIONER}),
            internal_keycloak_url=KC_URL,
            internal_keycloak_realm=REALM,
            internal_keycloak_audience=AUDIENCE,
            internal_keycloak_allowed_clients=f"{PROVISIONER}, {CLIENT}",
            binding_products=PRODUCTS,
            product_bundles=BUNDLES,
        )
    )
    from tests.lifespan import running_app

    async with running_app(app):
        await app.state.licenses.client.aclose()
        kinds = {"member": "member"}

        def introspect(req):
            token = json.loads(req.content)["token"]
            return httpx.Response(
                200,
                json=info(
                    tenant_id=str(TENANT if token != "foreign" else uuid4()),
                    license_id=str(LICENCE if token != "rotated-licence" else ROTATED),
                    subject_kind=kinds.get(token, "tenant"),
                    active=token != "inactive",
                ),
            )

        app.state.licenses.client = httpx.AsyncClient(transport=httpx.MockTransport(introspect))
        # First-party toolkits are published by the platform tenant, never by the tenant.
        await app.state.catalog.create_draft(PLATFORM, tool(PLATFORM, TOOL_ID), FILES, ACTOR)
        await app.state.catalog.transition(
            PLATFORM, TOOL_ID, 1, "published", ACTOR, expected_revision=1
        )
        await publish_toolkit(app, "coding")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield app, client


ROTATED = uuid4()
PLATFORM_OPERATOR = Principal(PLATFORM, uuid4(), "platform-operator", management=True)


async def publish_toolkit(app, name, operator=PLATFORM_OPERATOR, tool_id=TOOL_ID):
    tenant = operator.tenant_id
    manifest = ToolkitManifest(
        toolkit_id=uuid7(),
        tenant_id=tenant,
        version=1,
        name=name,
        description=f"{name} toolkit",
        tools=(ToolSelection(tool=ToolReference(tenant_id=tenant, id=tool_id, version=1)),),
    )
    await app.state.access.toolkit_write(operator, manifest)
    await app.state.access.toolkit_transition(operator, manifest.toolkit_id, 1, "published", 1)
    return manifest


GATEWAY = {"licence_id": str(LICENCE), "licence_token": "gateway"}


def binding_headers(token, licence="gateway"):
    return {"X-Tenant-Id": f"Bearer {licence}", "X-MCP-Binding": token}


async def test_ensure_issues_once_and_reensure_is_idempotent(ensure_api):
    app, client = ensure_api
    body = {"product": "agent", "licence_id": str(LICENCE), "licence_token": "gateway"}
    created = await client.post(path(), json=body, headers=service_headers())
    assert created.status_code == 201, created.text
    data = created.json()
    token = data.pop("token")
    assert data["status"] == "created" and data["token_already_issued"] is False
    assert data["tenant_id"] == str(TENANT) and data["gateway_license_id"] == str(LICENCE)
    # mcp-minted identities are UUIDv7; licence-authority identities here are v4.
    assert UUID(data["binding_id"]).version == 7 and UUID(data["ensure_id"]).version == 7
    assert [t["name"] for t in data["toolkits"]] == ["coding"]
    assert data["skipped"] == [{"toolkit": "knowledge", "reason": "not_published"}]
    assert created.headers["cache-control"] == "no-store"

    # The token works on the protocol path with the tenant's (v4) mcp licence.
    tools = await client.get("/mcp/available-tools", headers=binding_headers(token))
    assert tools.status_code == 200, tools.text
    assert [t["name"] for t in tools.json()["tools"]] == ["synthetic_tool"]
    # A member's per-login mcp licence can never use a binding.
    member = await client.get("/mcp/available-tools", headers=binding_headers(token, "member"))
    assert member.status_code == 401

    again = await client.post(path(), json=body, headers=service_headers())
    assert again.status_code == 200
    repeat = again.json()
    assert "token" not in repeat and repeat["token_already_issued"] is True
    assert repeat["status"] == "unchanged" and repeat["binding_id"] == data["binding_id"]
    assert repeat["binding_revision"] == data["binding_revision"]

    async with app.state.database.pool.connection() as conn:
        rows = await (await conn.execute("SELECT * FROM mcp_bindings")).fetchall()
        assert len(rows) == 1 and rows[0]["token_sha256"] == digest_token(token)
        # The binding is written with the ceiling tools narrow below, 240 s of queue budget.
        assert rows[0]["spec"]["limits"]["queue_seconds"] == 240
        ensures = await (await conn.execute("SELECT * FROM mcp_binding_ensures")).fetchall()
        assert len(ensures) == 1 and ensures[0]["revision"] == 3
        assert ensures[0]["token_issued_at"] is not None
        events = await (
            await conn.execute(
                "SELECT * FROM mcp_access_events WHERE action='binding.ensured' ORDER BY event_id"
            )
        ).fetchall()
        assert [e["subject"] for e in events] == [f"service:{PROVISIONER}"] * 2
        assert all(e["entity_id"] == ensures[0]["ensure_id"] for e in events)
        everything = await (await conn.execute("SELECT * FROM mcp_access_events")).fetchall()
        assert token not in repr(everything) and token not in repr(ensures)


async def test_rotate_newly_published_toolkits_and_licence_change(ensure_api):
    app, client = ensure_api
    body = {"product": "agent", "licence_id": str(LICENCE), "licence_token": "gateway"}
    first = (await client.post(path(), json=body, headers=service_headers())).json()

    rotated = await client.post(path(), json={**body, "rotate": True}, headers=service_headers())
    assert rotated.status_code == 201
    data = rotated.json()
    assert data["status"] == "rotated" and data["binding_id"] == first["binding_id"]
    assert data["token"] != first["token"]
    assert (
        await client.get("/mcp/available-tools", headers=binding_headers(first["token"]))
    ).status_code == 401
    assert (
        await client.get("/mcp/available-tools", headers=binding_headers(data["token"]))
    ).status_code == 200

    # A toolkit published later is added to the same binding without reissuing its token.
    await publish_toolkit(app, "knowledge")
    updated = await client.post(path(), json=body, headers=service_headers())
    assert updated.status_code == 200
    grown = updated.json()
    assert grown["status"] == "updated" and grown["binding_id"] == first["binding_id"]
    assert sorted(t["name"] for t in grown["toolkits"]) == ["coding", "knowledge"]
    assert grown["skipped"] == [] and grown["token_already_issued"] is True
    assert (
        await client.get("/mcp/available-tools", headers=binding_headers(data["token"]))
    ).status_code == 200

    # A different tenant mcp licence revokes the old binding and issues a new one.
    changed = await client.post(
        path(),
        json={"product": "agent", "licence_id": str(ROTATED), "licence_token": "rotated-licence"},
        headers=service_headers(),
    )
    assert changed.status_code == 201
    fresh = changed.json()
    assert fresh["status"] == "created" and fresh["binding_id"] != first["binding_id"]
    assert (
        await client.get(
            "/mcp/available-tools", headers=binding_headers(fresh["token"], "rotated-licence")
        )
    ).status_code == 200
    async with app.state.database.pool.connection() as conn:
        old = await (
            await conn.execute(
                "SELECT revoked FROM mcp_bindings WHERE binding_id=%s",
                (UUID(first["binding_id"]),),
            )
        ).fetchone()
        assert old == {"revoked": True}


async def test_pending_product_and_products_are_separate(ensure_api):
    _, client = ensure_api
    pending = await client.post(
        path(),
        json={"product": "empty", "licence_id": str(LICENCE), "licence_token": "gateway"},
        headers=service_headers(),
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending" and pending.json()["binding_id"] is None
    assert pending.json()["token_already_issued"] is False
    rotate_pending = await client.post(
        path(),
        json={**GATEWAY, "product": "empty", "rotate": True},
        headers=service_headers(),
    )
    assert rotate_pending.status_code == 200 and "token" not in rotate_pending.json()
    agent = await client.post(
        path(),
        json={"product": "agent", "licence_id": str(LICENCE), "licence_token": "gateway"},
        headers=service_headers(),
    )
    care = await client.post(
        path(), json={**GATEWAY, "product": "care-bus"}, headers=service_headers()
    )
    assert agent.status_code == 201 and care.status_code == 200
    assert care.json()["skipped"] == [{"toolkit": "care_bus_ops", "reason": "not_published"}]


async def test_concurrent_ensures_create_one_binding(ensure_api):
    app, client = ensure_api
    body = {"product": "agent", "licence_id": str(LICENCE), "licence_token": "gateway"}
    results = await asyncio.gather(
        *(client.post(path(), json=body, headers=service_headers()) for _ in range(4))
    )
    assert sorted(r.status_code for r in results) == [200, 200, 200, 201]
    assert len({r.json()["binding_id"] for r in results}) == 1
    async with app.state.database.pool.connection() as conn:
        count = await (await conn.execute("SELECT count(*) AS n FROM mcp_bindings")).fetchone()
        assert count == {"n": 1}


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({**GATEWAY, "product": "unknown"}, 404),
        ({"product": "agent"}, 422),
        ({"product": "agent", "licence_id": str(LICENCE)}, 422),
        ({"product": "agent", "licence_token": "gateway"}, 422),
        ({**GATEWAY, "product": "agent", "licence_token": ""}, 422),
        ({**GATEWAY, "product": "Agent"}, 422),
        (
            {**GATEWAY, "product": "agent", "licence_id": "00000000-0000-0000-0000-000000000000"},
            422,
        ),
        ({**GATEWAY, "product": "agent", "extra": 1}, 422),
        ({**GATEWAY, "product": "agent", "rotate": "yes"}, 422),
        # The licence must be this tenant's tenant-level mcp licence with exactly that id.
        ({**GATEWAY, "product": "agent", "licence_id": str(uuid4())}, 403),
        ({**GATEWAY, "product": "agent", "licence_token": "member"}, 403),
        ({**GATEWAY, "product": "agent", "licence_token": "foreign"}, 403),
        ({**GATEWAY, "product": "agent", "licence_token": "inactive"}, 403),
    ],
)
async def test_invalid_ensure_requests(ensure_api, body, status):
    app, client = ensure_api
    response = await client.post(path(), json=body, headers=service_headers())
    assert response.status_code == status
    assert str(LICENCE) not in response.text and "gateway" not in response.text
    async with app.state.database.pool.connection() as conn:
        count = await (await conn.execute("SELECT count(*) AS n FROM mcp_bindings")).fetchone()
        assert count == {"n": 0}


async def test_platform_tenant_is_not_provisioned(ensure_api):
    _, client = ensure_api
    response = await client.post(
        path(PLATFORM), json={**GATEWAY, "product": "agent"}, headers=service_headers()
    )
    assert response.status_code == 403


async def test_tenant_owned_toolkits_never_satisfy_a_product(ensure_api):
    """Only the platform's toolkits are resolved; a tenant's same-named toolkit is ignored."""
    app, client = ensure_api
    own_tool = UUID("20000000-0000-7000-8000-00000000e0a1")
    await app.state.catalog.create_draft(
        TENANT, tool(TENANT, own_tool, operation="own.knowledge"), FILES, ACTOR
    )
    await app.state.catalog.transition(TENANT, own_tool, 1, "published", ACTOR, expected_revision=1)
    tenant_operator = Principal(TENANT, LICENCE, "tenant-operator", management=True)
    await publish_toolkit(app, "knowledge", tenant_operator, own_tool)
    data = (
        await client.post(path(), json={**GATEWAY, "product": "agent"}, headers=service_headers())
    ).json()
    assert [t["name"] for t in data["toolkits"]] == ["coding"]
    assert data["toolkits"][0]["tenant_id"] == str(PLATFORM)
    # the tenant's own published toolkit is added automatically (tool-builder decision 4) unless
    # its tool names collide with the product's: then it is reported, never silently shadowing
    assert data["skipped"] == [
        {"toolkit": "knowledge", "reason": "not_published"},
        {"toolkit": "knowledge", "reason": "tool_name_collision"},
    ]
    async with app.state.database.pool.connection() as conn:
        rows = await (await conn.execute("SELECT * FROM mcp_binding_toolkits")).fetchall()
        assert [(r["tenant_id"], r["owner_tenant_id"]) for r in rows] == [(TENANT, PLATFORM)]


async def test_invalid_tenant_path(ensure_api):
    _, client = ensure_api
    bad = await client.post(
        path("not-a-uuid"), json={"product": "agent"}, headers=service_headers()
    )
    assert bad.status_code == 422


async def test_internal_routes_require_an_allowed_service_jwt(ensure_api, monkeypatch):
    app, client = ensure_api
    body = {"product": "agent", "licence_id": str(LICENCE), "licence_token": "gateway"}
    assert (await client.post(path(), json=body)).status_code == 401
    assert (
        await client.post(path(), json=body, headers={"Authorization": "Bearer garbage"})
    ).status_code == 401
    # A valid realm token for a client outside the ensure allow-list.
    assert (
        await client.post(path(), json=body, headers=service_headers(CLIENT))
    ).status_code == 403
    # Licence, binding or operator credentials are never accepted on internal routes.
    for extra in (
        {"X-Tenant-Id": "Bearer gateway"},
        {"X-MCP-Binding": "token"},
        {"X-MCP-Admin-Key": "a" * 32},
    ):
        response = await client.post(path(), json=body, headers={**service_headers(), **extra})
        assert response.status_code == 401
    # The service principal is no operator and no binding principal.
    assert (await client.get("/mcp/toolkits", headers=service_headers())).status_code == 403
    too_large = await client.post(
        path(), content=b"{" + b" " * 300000 + b"}", headers=service_headers()
    )
    assert too_large.status_code == 413
    # Unconfigured allow-list: unavailable, not open.
    app.state.settings = app.state.settings.model_copy(
        update={"binding_ensure_clients": frozenset()}
    )
    assert (await client.post(path(), json=body, headers=service_headers())).status_code == 503


async def test_keycloak_disabled_is_unavailable(ensure_api, monkeypatch):
    app, client = ensure_api
    token = service_headers()
    app.state.settings = app.state.settings.model_copy(update={"internal_keycloak_url": None})
    response = await client.post(path(), json={"product": "agent"}, headers=token)
    assert response.status_code == 503


async def test_service_unavailable_without_ensure_state(ensure_api):
    app, client = ensure_api
    service = app.state.binding_ensure
    app.state.binding_ensure = None
    try:
        response = await client.post(
            path(),
            json={"product": "agent", "licence_id": str(LICENCE), "licence_token": "gateway"},
            headers=service_headers(),
        )
        assert response.status_code == 503
    finally:
        app.state.binding_ensure = service


# --- gateway licence and backend licences through the minter (fakes) -------------------------


class FakeMinted(SimpleNamespace):
    pass


class FakeMinter:
    def __init__(self, licences=None, error=None):
        self.licences = licences or {}
        self.error = error
        self.calls: list[tuple[UUID, str, str | None]] = []

    async def mint(self, tenant, app, bundle=None):
        from pydantic import SecretStr

        self.calls.append((tenant, app, bundle))
        if self.error:
            raise self.error
        return FakeMinted(license_id=self.licences[app], token=SecretStr(f"minted-{app}"))

    async def licence_id(self, tenant, app, bundle=None):
        return (await self.mint(tenant, app, bundle)).license_id


class FakeLicences:
    def __init__(self, identity=None, error=None):
        self.identity, self.error = identity, error

    async def authenticate(self, token, app):
        if self.error:
            raise self.error
        return self.identity


def service_for(minter=None, licences=None, **updates):
    config = settings(
        license_introspect_url="https://l.example/i", license_app_key="mcp", **updates
    )
    access = SimpleNamespace(settings=config, repository=SimpleNamespace(pool=None))
    return BindingEnsureService(access, licences, minter)  # ty: ignore[invalid-argument-type]


async def test_gateway_licence_is_introspected_and_must_be_tenant_level():
    tenant_identity = LicenseIdentity(TENANT, LICENCE, "svc", float("inf"), "tenant")
    minter = FakeMinter({"mcp": LICENCE})
    service = service_for(minter, FakeLicences(tenant_identity))
    assert await service.gateway_licence(TENANT, LICENCE, "held") == LICENCE
    assert minter.calls == []  # mcp never mints its own application licence
    with pytest.raises(PermissionError):
        await service.gateway_licence(TENANT, uuid4(), "held")
    for wrong in (
        LicenseIdentity(TENANT, LICENCE, "person", float("inf"), "member"),
        LicenseIdentity(TENANT, LICENCE, "person", float("inf"), None),
        LicenseIdentity(uuid4(), LICENCE, "svc", float("inf"), "tenant"),
    ):
        with pytest.raises(PermissionError):
            await service_for(minter, FakeLicences(wrong)).gateway_licence(TENANT, LICENCE, "t")


async def test_gateway_licence_failures_map_to_forbidden_or_unavailable():
    from src.auth.licenses import AuthenticationError, AuthorityUnavailableError

    with pytest.raises(PermissionError, match="did not authenticate"):
        await service_for(None, FakeLicences(error=AuthenticationError())).gateway_licence(
            TENANT, LICENCE, "t"
        )
    with pytest.raises(AuthorityUnavailableError):
        await service_for(None, FakeLicences(error=AuthorityUnavailableError())).gateway_licence(
            TENANT, LICENCE, "t"
        )
    with pytest.raises(AuthorityUnavailableError):
        await service_for().gateway_licence(TENANT, LICENCE, "t")


async def test_backend_licences_come_from_the_minter_and_missing_grants_skip():
    from src.auth.licenses import AuthorityUnavailableError
    from src.credentials.minter import DependencyGrantScopeError, MinterUnavailableError

    coding, rag = uuid4(), uuid4()
    resolved = [
        {"name": "coding", "services": {"coding_api": "coding"}},
        {"name": "knowledge", "services": {"strata_rag": "strata-rag", "laya": "laya"}},
        {"name": "rules", "services": {}},
    ]

    class Partial(FakeMinter):
        async def mint(self, tenant, app, bundle=None):
            if app == "laya":
                self.calls.append((tenant, app, bundle))
                raise DependencyGrantScopeError()
            return await super().mint(tenant, app, bundle)

    minter = Partial({"coding": coding, "strata-rag": rag})
    skipped: list[dict[str, str]] = []
    kept, references = await service_for(minter).service_references(
        TENANT, resolved, skipped, "agent"
    )
    assert [t["name"] for t in kept] == ["coding", "rules"]
    assert skipped == [{"toolkit": "knowledge", "reason": "service_credentials_unavailable"}]
    assert set(references) == {"coding_api"}
    assert references["coding_api"].license_id == coding
    assert references["coding_api"].app_key == "coding"
    # The product's bundle is named on every mint and recorded on the credential reference.
    assert references["coding_api"].bundle == "agent"
    assert {bundle for _, _, bundle in minter.calls} == {"agent"}

    # Without a minter no backend licence can be established: toolkits needing one are skipped.
    skipped = []
    kept, references = await service_for().service_references(TENANT, resolved, skipped)
    assert [t["name"] for t in kept] == ["rules"] and references == {}
    assert len(skipped) == 2

    with pytest.raises(AuthorityUnavailableError):
        await service_for(FakeMinter(error=MinterUnavailableError())).service_references(
            TENANT, resolved, []
        )


async def test_external_tool_toolkit_needs_no_backend_credentials(ensure_api):
    """A platform toolkit of fr_project tools reaches fr over the HMAC channel, not a backend
    licence: discovery finds no services, so it is never skipped for missing credentials."""
    from src.catalog.models import FrProjectTarget
    from tests.fixtures import external_tool

    app, _ = ensure_api
    fr_tool = uuid7()
    manifest = external_tool(
        tenant_id=PLATFORM,
        tool_id=fr_tool,
        public_name="start_fr_project",
        runtime="fr_project",
        target=FrProjectTarget(kind="fr_project", project_type="it"),
    )
    await app.state.catalog.create_draft(PLATFORM, manifest, FILES, ACTOR)
    await app.state.catalog.transition(
        PLATFORM, fr_tool, 1, "published", ACTOR, expected_revision=1
    )
    await publish_toolkit(app, "fr", tool_id=fr_tool)
    service = app.state.binding_ensure
    async with app.state.database.pool.connection() as conn:
        resolved, skipped = await service.discover(conn, ("fr",), TENANT)
    assert [t["name"] for t in resolved] == ["fr"] and resolved[0]["services"] == {}
    kept, references = await service.service_references(TENANT, resolved, skipped, "agent")
    assert [t["name"] for t in kept] == ["fr"] and references == {} and skipped == []


def test_actor_attribution_and_product_table_validation():
    actor = actor_for("x" * 300, LICENCE)
    assert len(actor.subject) == 256 and actor.license_id == LICENCE
    assert settings().binding_products["agent"] == ("coding", "knowledge", "cases", "rules", "fr")
    assert settings().binding_products["care-bus"] == (
        "care_bus_ops",
        "knowledge",
        "research",
        "legal",
    )
    for table in (
        {"Agent": ("coding",)},
        {"agent": ()},
        {"agent": ("care-bus-ops",)},
        {"agent": ("coding", "coding")},
    ):
        with pytest.raises(ValueError):
            settings(binding_products=table)
    with pytest.raises(ValueError):
        settings(binding_ensure_clients=frozenset({"provisioner"}))
    with pytest.raises(ValueError):
        settings(
            binding_ensure_clients=frozenset({""}),
            license_app_key="mcp",
            license_introspect_url="https://l.example/i",
        )


def test_migration_keeps_v7_only_for_mcp_minted_identities(database_url):
    import psycopg

    with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT conname FROM pg_constraint WHERE contype='c' AND conname LIKE 'mcp_%'"
            )
        }
        assert {"mcp_bindings_binding_id_v7", "mcp_toolkits_toolkit_id_v7"} <= names
        assert {
            "mcp_bindings_tenant_id_v7",
            "mcp_bindings_gateway_license_id_v7",
            "mcp_credential_refs_license_id_v7",
            "mcp_toolkit_version_tools_owner_tenant_id_v7",
            "mcp_runs_tenant_id_check",
            "mcp_run_api_calls_license_id_check",
        }.isdisjoint(names)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO mcp_binding_ensures(ensure_id,tenant_id,product,created_by,updated_by)"
                " VALUES (%s,%s,'agent','s','s')",
                (uuid4(), TENANT),
            )
    with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        conn.execute(
            "INSERT INTO mcp_binding_ensures(ensure_id,tenant_id,product,created_by,updated_by)"
            " VALUES (%s,%s,'agent','s','s')",
            (uuid7(), TENANT),
        )
        for statement in (
            "DELETE FROM mcp_binding_ensures",
            "UPDATE mcp_binding_ensures SET product='other',revision=revision+1",
            "UPDATE mcp_binding_ensures SET skipped='[]'",
        ):
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(statement)
