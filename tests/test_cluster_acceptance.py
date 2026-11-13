"""The disposable acceptance generator must preserve the production role boundaries."""

import json

import httpx
import pytest

from scripts.acceptance_fixtures import app, counts
from scripts.provision_cluster_acceptance import manifests
from scripts.verify_execution_cluster import Acceptance
from tests.fixtures import IMAGE


def test_acceptance_manifests_are_isolated_and_complete():
    candidate = manifests(IMAGE, IMAGE)
    objects = candidate["items"]
    secret = next(o for o in objects if o["kind"] == "Secret")
    config = Acceptance.model_validate_json(secret["stringData"]["config.json"])
    assert config.synthetic and str(config.tenant_id) in secret["stringData"]["bundle.json"]
    assert {o["metadata"]["name"] for o in objects if o["kind"] == "Namespace"} == {
        "mcp-acceptance",
        "mcp-runs",
    }
    role = next(o for o in objects if o["kind"] == "Role")
    assert {r for rule in role["rules"] for r in rule["resources"]} == {"pods", "pods/attach"}
    assert all(o["metadata"].get("namespace") != "mcp" for o in objects)
    network = next(o for o in objects if o["kind"] == "NetworkPolicy")
    assert network["spec"]["egress"] == network["spec"]["ingress"] == []
    with pytest.raises(ValueError):
        manifests(IMAGE, IMAGE, namespace="mcp")
    with pytest.raises(ValueError):
        manifests("mutable:latest", IMAGE)


async def test_fixture_authority_rejects_wrong_app_and_upstream_requires_its_own_license(
    tmp_path, monkeypatch
):
    candidate = manifests(IMAGE, IMAGE)
    data = next(o for o in candidate["items"] if o["kind"] == "Secret")["stringData"]
    for name in ("config.json", "bundle.json"):
        (tmp_path / name).write_text(data[name])
    monkeypatch.setenv("MCP_ACCEPTANCE_FIXTURE_DIRECTORY", str(tmp_path))
    bundle = json.loads(data["bundle.json"])
    entries = bundle["entries"]
    counts.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                "/introspect", json={"token": entries[1]["token"], "app": "acceptance_2"}
            )
        ).json() == {"active": False}
        assert (
            await client.post(
                "/introspect", json={"token": entries[1]["token"], "app": "acceptance_1"}
            )
        ).json()["active"]
        assert (
            await client.get("/fixture", headers={"Authorization": "Bearer " + entries[0]["token"]})
        ).status_code == 401
        assert (
            await client.get("/fixture", headers={"Authorization": "Bearer " + entries[1]["token"]})
        ).json() == {"ok": True}
        assert (await client.get("/evidence")).json() == {"calls": {"acceptance_1": 1}}
