"""Synthetic in-cluster acceptance: author -> MCP -> durable run -> Kata -> cleanup.

Requires a dedicated disposable acceptance database/catalog and preprovisioned
synthetic license authority, two fixture upstreams, bundle and destination aliases.
Never runs automatically against a developer's current kubectl context.
"""

import argparse
import asyncio
import ipaddress
import json
from pathlib import Path
from typing import Literal
from uuid import uuid7

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import UUID7, SecretStr

from src.catalog.models import Contract
from src.catalog.package import PackageFile, package_digest
from src.config import Settings
from src.execution.kata import KataBackend
from src.storage.database import Database


class Acceptance(Contract):
    synthetic: Literal[True]
    api_url: str
    tenant_id: UUID7
    gateway_license_id: UUID7
    gateway_token: SecretStr
    admin_key: SecretStr
    platform_key: SecretStr
    profile_id: UUID7
    first_license_id: UUID7
    second_license_id: UUID7
    public_probe_ip: str
    cluster_probe_ip: str


PURE = """import os, socket
async def run(inputs, ctx):
    assert not os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token')
    assert not any(key.startswith(('DATABASE_', 'MCP_', 'LICENSE_')) for key in os.environ)
    for ip in inputs['probe_ips']:
        channel = socket.socket()
        channel.settimeout(0.5)
        try:
            channel.connect((ip, 443))
        except OSError:
            pass
        else:
            raise RuntimeError('network isolation failed')
        finally:
            channel.close()
    print('sandbox diagnostic')
    return {'ok': True}
"""


async def verify(config: Acceptance, settings: Settings, lifecycle=None):
    if not settings.execution_enabled:
        raise ValueError(
            "acceptance requires explicit execution enablement in the isolated test deployment"
        )
    headers = {
        "X-Tenant-Id": "Bearer " + config.gateway_token.get_secret_value(),
        "X-MCP-Admin-Key": config.admin_key.get_secret_value(),
        "X-MCP-Platform-Admin-Key": config.platform_key.get_secret_value(),
    }
    database = Database(settings)
    backend = KataBackend(settings)
    binding_id = None
    accepted = []
    async with httpx.AsyncClient(
        base_url=config.api_url,
        headers=headers,
        follow_redirects=False,
        trust_env=False,
        timeout=15,
    ) as client:

        async def post(path, payload=None):
            response = await client.post(path, json=payload)
            if response.status_code >= 300:
                raise RuntimeError(
                    f"acceptance management failed: {path} status {response.status_code}"
                )
            return response.json()

        try:
            await database.open()
            await backend.ready()
            # Acceptance uses a dedicated empty runner namespace/queue. Never
            # delete someone else's active pods as part of fixture cleanup.
            if await backend.list():
                raise RuntimeError("acceptance requires no pre-existing runner pods")
            # Positive controls prevent an unreachable target from falsely proving
            # isolation. The same addresses must be reachable outside the runner.
            for address in (config.public_probe_ip, config.cluster_probe_ip):
                ipaddress.ip_address(address)
                _, writer = await asyncio.wait_for(asyncio.open_connection(address, 443), 5)
                writer.close()
                await writer.wait_closed()
            operation_ids = []
            for number in (1, 2):
                await post(
                    "/mcp/sources",
                    {
                        "source_key": f"acceptance_{number}",
                        "base_url": "http://localhost:8040",
                        "allow_internal_http": True,
                        "auth": {"kind": "licence", "app_key": f"acceptance_{number}"},
                    },
                )
            for number, license_id in (
                (1, config.first_license_id),
                (2, config.second_license_id),
                (3, config.first_license_id),
            ):
                service_id, operation_id = uuid7(), uuid7()
                await post(
                    "/mcp/services",
                    {
                        "schema_version": 1,
                        "service_id": str(service_id),
                        "version": 1,
                        "service_key": f"acceptance_{min(number, 2)}"
                        if number < 3
                        else "acceptance_mutation",
                        "destination_alias": f"acceptance_{min(number, 2)}",
                        "license_app_key": f"acceptance_{min(number, 2)}"
                        if number < 3
                        else "acceptance_1",
                        "auth_adapter": "license_bearer",
                        "credential_profile_key": f"acceptance_{number}",
                        "max_request_bytes": 4096,
                        "max_response_bytes": 65536,
                    },
                )
                await post(f"/mcp/services/{service_id}/versions/1/publish")
                operation_ids.append(operation_id)
                await post(
                    f"/mcp/operations/{operation_id}/versions",
                    {
                        "schema_version": 1,
                        "operation_id": str(operation_id),
                        "version": 1,
                        "service": {"id": str(service_id), "version": 1},
                        "method": "POST" if number == 3 else "GET",
                        "path_template": "/fixture",
                        "input_schema": {"type": "object", "additionalProperties": False},
                        "output_schema": {
                            "type": "object",
                            "properties": {"ok": {"const": True}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                        "response_projection": ["/ok"],
                        "effect": "writes" if number == 3 else "read_only",
                        "timeout_seconds": 5,
                        "pagination_limit": 1,
                        "max_response_bytes": 4096,
                    },
                )
                await post(f"/mcp/operations/{operation_id}/versions/1/publish")
            await post(
                "/mcp/credential-profiles",
                {
                    "schema_version": 1,
                    "tenant_id": str(config.tenant_id),
                    "profile_id": str(config.profile_id),
                    "name": "acceptance",
                    "gateway": {
                        "service": "mcp",
                        "alias": "acceptance_gateway",
                        "license_id": str(config.gateway_license_id),
                        "app_key": settings.license_app_key,
                    },
                    "services": [
                        {
                            "service": f"acceptance_{number}",
                            "alias": f"acceptance_{number}",
                            "license_id": str(license_id),
                            "app_key": f"acceptance_{number}",
                        }
                        for number, license_id in (
                            (1, config.first_license_id),
                            (2, config.second_license_id),
                        )
                    ]
                    + [
                        {
                            "service": "acceptance_mutation",
                            "alias": "acceptance_1",
                            "license_id": str(config.first_license_id),
                            "app_key": "acceptance_1",
                        }
                    ],
                },
            )
            compound = "async def run(inputs, ctx):\n"
            for operation_id in operation_ids[:2]:
                compound += (
                    f"    assert (await ctx.api.call('{operation_id}', {{}}))['ok'] is True\n"
                )
            compound += "    return {'ok': True}\n"
            cases = {
                "acceptance_isolation": PURE,
                "acceptance_broker": compound,
                "acceptance_forgery": f"async def run(inputs, ctx):\n    await ctx.api.call('{uuid7()}', {{}})\n    return {{'ok': True}}\n",
                "acceptance_loop": "async def run(inputs, ctx):\n    while True: pass\n",
                "acceptance_memory": "async def run(inputs, ctx):\n    chunks=[]\n    while True: chunks.append(bytearray(16*1024*1024))\n",
                "acceptance_stdout": "async def run(inputs, ctx):\n    print('x'*65536)\n    return {'ok': True}\n",
                "acceptance_mutation": f"async def run(inputs, ctx):\n    await ctx.api.call('{operation_ids[2]}', {{}})\n    return {{'ok': True}}\n",
            }
            refs = []
            for name, source in cases.items():
                tool_id = uuid7()
                files = (PackageFile(path="main.py", content=source),)
                await post(
                    "/mcp/tools",
                    {
                        "manifest": {
                            "schema_version": 1,
                            "tenant_id": str(config.tenant_id),
                            "tool_id": str(tool_id),
                            "version": 1,
                            "public_name": name,
                            "operation": "acceptance." + name[:32],
                            "description": "Synthetic cluster acceptance",
                            "entrypoint": "main:run",
                            "package_sha256": package_digest(files),
                            "runner_image_digest": settings.runner_image_digest,
                            "runtime": "kata_python",
                            "effect": "writes" if name == "acceptance_mutation" else "read_only",
                            "input_schema": {
                                "type": "object",
                                "properties": {
                                    "probe_ips": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "maxItems": 2,
                                    }
                                },
                                "additionalProperties": False,
                            },
                            "context_schema": {
                                "type": "object",
                                "properties": {"tenant_id": {"type": "string"}},
                                "additionalProperties": False,
                            },
                            "result_schema": {
                                "type": "object",
                                "properties": {"ok": {"const": True}},
                                "required": ["ok"],
                                "additionalProperties": False,
                            },
                            "allowed_operations": [
                                {"id": str(identity), "version": 1}
                                for identity in (
                                    operation_ids
                                    if name == "acceptance_mutation"
                                    else operation_ids[:2]
                                )
                            ],
                            "limits": {
                                "wall_seconds": 10,
                                "queue_seconds": 60,
                                "memory_mi": 256,
                                "cpu_millicores": 250,
                                "api_calls": 4,
                                "frame_bytes": 65536,
                                "output_bytes": 262144,
                            },
                        },
                        "files": [f.model_dump() for f in files],
                    },
                )
                await post(f"/mcp/tools/{tool_id}/versions/1/validate")
                await post(f"/mcp/tools/{tool_id}/versions/1/publish", {"expected_revision": 1})
                refs.append(
                    {"tool": {"tenant_id": str(config.tenant_id), "id": str(tool_id), "version": 1}}
                )
            toolkit_id = uuid7()
            await post(
                "/mcp/toolkits",
                {
                    "schema_version": 1,
                    "tenant_id": str(config.tenant_id),
                    "toolkit_id": str(toolkit_id),
                    "version": 1,
                    "name": "acceptance",
                    "description": "Synthetic acceptance",
                    "tools": refs,
                },
            )
            await post(f"/mcp/toolkits/{toolkit_id}/versions/1/publish", {"expected_revision": 1})
            from datetime import UTC, datetime, timedelta

            issued = await post(
                "/mcp/bindings",
                {
                    "schema_version": 1,
                    "credential_profile_id": str(config.profile_id),
                    "toolkits": [{"id": str(toolkit_id), "version": 1}],
                    "context": {"tenant_id": str(config.tenant_id)},
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                },
            )
            binding_id = issued["binding"]["binding_id"]
            agent_headers = {
                "X-Tenant-Id": headers["X-Tenant-Id"],
                "X-MCP-Binding": issued["token"],
            }
            if lifecycle:
                await lifecycle(client, agent_headers, cases, refs)
            async with Client(
                StreamableHttpTransport(config.api_url.rstrip("/") + "/mcp", headers=agent_headers)
            ) as mcp:
                listed = await mcp.list_tools()
                if {t.name for t in listed} != set(cases):
                    raise RuntimeError("acceptance discovery mismatch")
                for name in cases:
                    inputs = (
                        {"probe_ips": [config.public_probe_ip, config.cluster_probe_ip]}
                        if name == "acceptance_isolation"
                        else {}
                    )
                    call = await mcp.call_tool(name, inputs, raise_on_error=False)
                    run = call.structured_content
                    if not run or "run_id" not in run:
                        raise RuntimeError("acceptance run not accepted")
                    accepted.append(run["run_id"])
                    stop = asyncio.get_running_loop().time() + 120
                    while run["status"] not in {
                        "succeeded",
                        "failed",
                        "timed_out",
                        "interrupted",
                        "cancelled",
                    }:
                        response = await client.get(
                            "/mcp/runs/" + run["run_id"], headers=agent_headers
                        )
                        response.raise_for_status()
                        run = response.json()
                        if asyncio.get_running_loop().time() > stop:
                            raise RuntimeError("acceptance deadline")
                        await asyncio.sleep(0.2)
                    expected_success = name in {
                        "acceptance_isolation",
                        "acceptance_broker",
                        "acceptance_mutation",
                    }
                    if (run["status"] == "succeeded") != expected_success:
                        raise RuntimeError(
                            f"acceptance result mismatch: {name} {run['status']} {run.get('error')}"
                        )
            for _ in range(80):
                async with database.pool.connection() as conn:
                    counts = await (
                        await conn.execute(
                            "SELECT count(*) AS n FROM mcp_execution_slots WHERE run_id IS NOT NULL OR quarantined"
                        )
                    ).fetchone()
                    if counts is None:
                        raise RuntimeError("acceptance slot inspection failed")
                    count = counts["n"]
                if not await backend.list() and count == 0:
                    break
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("acceptance cleanup not confirmed")
            print(
                json.dumps(
                    {
                        "kata_acceptance": "passed",
                        "cases": list(cases),
                        "pods_remaining": 0,
                        "slots_owned": 0,
                    }
                )
            )
        finally:
            for run_id in accepted:
                await client.post(f"/mcp/runs/{run_id}/cancel")
            if binding_id:
                await post(f"/mcp/bindings/{binding_id}/revoke", {"expected_revision": 1})
            await backend.close()
            await database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-config", type=Path, required=True)
    args = parser.parse_args()
    config = Acceptance.model_validate_json(args.synthetic_config.read_bytes())
    asyncio.run(verify(config, Settings()))  # ty: ignore[missing-argument]


if __name__ == "__main__":
    main()
