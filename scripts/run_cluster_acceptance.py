"""Run the complete synthetic stack and crash matrix in a disposable cluster Job."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid7

import httpx
import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from scripts.grant_runtime_role import grant, grant_execution
from scripts.verify_execution_cluster import Acceptance, verify
from src.config import Settings
from src.execution.kata import KataBackend
from src.execution.models import TERMINAL
from src.storage.database import Database


async def run(root):
    config = Acceptance.model_validate_json((root / "config.json").read_bytes())
    configuration = json.loads((root / "runtime.json").read_bytes())
    owner_dsn = configuration.pop("MCP_ACCEPTANCE_OWNER_DATABASE_URL")
    api_dsn = configuration["DATABASE_URL"]
    worker_dsn = configuration.pop("MCP_ACCEPTANCE_WORKER_DATABASE_URL")
    platform_dsn = configuration["MCP_PLATFORM_DATABASE_URL"]
    for attempt in range(120):
        try:
            with psycopg.connect(owner_dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            break
        except psycopg.OperationalError:
            if attempt == 119:
                raise RuntimeError("disposable acceptance database not ready") from None
            await asyncio.sleep(1)
    migration = Config("alembic.ini")
    migration.attributes["database_url"] = owner_dsn
    await asyncio.to_thread(command.upgrade, migration, "head")
    # A new throwaway PostgreSQL instance owns only synthetic acceptance data.
    with psycopg.connect(owner_dsn) as conn:
        for role, dsn in (
            ("acceptance_api", api_dsn),
            ("acceptance_worker", worker_dsn),
            ("acceptance_platform", platform_dsn),
        ):
            password = conninfo_to_dict(dsn)["password"]
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        conn.execute("INSERT INTO public.mcp_execution_slots(slot_id) VALUES (1),(2)")
    grant(owner_dsn, "acceptance_api")
    with psycopg.connect(owner_dsn) as conn:
        conn.execute("GRANT USAGE ON SCHEMA public TO acceptance_worker")
        grant_execution(conn, "acceptance_worker", worker=True)
    platform_env = {**os.environ, "DATABASE_URL": owner_dsn}
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.grant_runtime_role",
            "--role",
            "acceptance_platform",
            "--platform",
        ],
        env=platform_env,
        check=True,
    )
    os.environ.update({k: str(v) for k, v in configuration.items()})
    settings = Settings(database_url=SecretStr(worker_dsn))
    database = Database(settings)
    await database.open()
    backend = KataBackend(settings)
    scratch = tempfile.TemporaryDirectory(prefix="mcp-acceptance-")
    checkpoint_file = Path(scratch.name) / "checkpoint.json"
    processes = []
    workers = []
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABASE_", "MCP_", "LICENSE_"))
    }
    base_env.update({k: str(v) for k, v in configuration.items()})
    base_env["MCP_ACCEPTANCE_FIXTURE_DIRECTORY"] = str(root)

    def start(module, *, env=None, args=()):
        process = subprocess.Popen([sys.executable, "-m", module, *args], env=env or base_env)
        processes.append(process)
        return process

    def start_worker(port, point=None):
        env = {**base_env, "DATABASE_URL": worker_dsn, "MCP_WORKER_PROBE_PORT": str(port)}
        for key in (
            "MCP_ADMIN_KEY_FILE",
            "MCP_PLATFORM_ADMIN_KEY_FILE",
            "MCP_PLATFORM_DATABASE_URL",
        ):
            env.pop(key, None)
        env["MCP_CREDENTIAL_BUNDLE_FILE"] = str(root / "bundle.json")
        if point:
            env["MCP_ACCEPTANCE_CRASH_POINT"] = point
            env["MCP_ACCEPTANCE_CHECKPOINT_FILE"] = str(checkpoint_file)
        process = start("scripts.acceptance_worker" if point else "src.execution.worker", env=env)
        workers.append(process)
        return process

    async def stop_worker(process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), 15)
            except TimeoutError:
                process.kill()
                await asyncio.to_thread(process.wait)
        if process in workers:
            workers.remove(process)

    async def wait_ready(url):
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(300):
                try:
                    response = await client.get(url, timeout=1)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if any(p.poll() is not None for p in processes if p not in workers):
                    raise RuntimeError("acceptance process stopped")
                await asyncio.sleep(0.1)
        raise RuntimeError("acceptance startup deadline")

    async def terminal(client, headers, run_id):
        for _ in range(1200):
            response = await client.get(f"/mcp/runs/{run_id}", headers=headers)
            response.raise_for_status()
            result = response.json()
            if result["status"] in TERMINAL:
                return result
            await asyncio.sleep(0.1)
        raise RuntimeError("acceptance recovery deadline")

    async def clean():
        for _ in range(200):
            async with database.pool.connection() as conn:
                counts = await (
                    await conn.execute(
                        "SELECT count(*) AS n FROM mcp_execution_slots WHERE run_id IS NOT NULL OR quarantined"
                    )
                ).fetchone()
                assert counts is not None
                count = counts["n"]
            if not await backend.list() and count == 0:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("acceptance cleanup deadline")

    async def lifecycle(client, headers, cases, refs):
        secondary = json.loads((root / "secondary.json").read_bytes())
        second_admin = {
            "X-Tenant-Id": "Bearer " + secondary["gateway_token"],
            "X-MCP-Admin-Key": config.admin_key.get_secret_value(),
        }

        async def second_post(path, payload=None):
            response = await client.post(path, headers=second_admin, json=payload)
            response.raise_for_status()
            return response.json()

        # Same public tool name in another tenant, with distinct licenses/profile.
        original_ref = refs[1]["tool"]
        response = await client.get(f"/mcp/tools/{original_ref['id']}/versions/1")
        response.raise_for_status()
        original = response.json()
        second_tool, second_kit = str(uuid7()), str(uuid7())
        manifest = {
            **original["descriptor"]["manifest"],
            "tenant_id": secondary["tenant_id"],
            "tool_id": second_tool,
        }
        await second_post("/mcp/tools", {"manifest": manifest, "files": original["files"]})
        await second_post(f"/mcp/tools/{second_tool}/versions/1/validate")
        await second_post(f"/mcp/tools/{second_tool}/versions/1/publish", {"expected_revision": 1})
        references = secondary["references"]
        await second_post(
            "/mcp/credential-profiles",
            {
                "schema_version": 1,
                "tenant_id": secondary["tenant_id"],
                "profile_id": secondary["profile_id"],
                "name": "secondary",
                "gateway": references[0],
                "services": references[1:],
            },
        )
        await second_post(
            "/mcp/toolkits",
            {
                "schema_version": 1,
                "tenant_id": secondary["tenant_id"],
                "toolkit_id": second_kit,
                "version": 1,
                "name": "secondary",
                "description": "Synthetic second tenant",
                "tools": [
                    {"tool": {"tenant_id": secondary["tenant_id"], "id": second_tool, "version": 1}}
                ],
            },
        )
        await second_post(
            f"/mcp/toolkits/{second_kit}/versions/1/publish", {"expected_revision": 1}
        )
        issued = await second_post(
            "/mcp/bindings",
            {
                "schema_version": 1,
                "credential_profile_id": secondary["profile_id"],
                "toolkits": [{"id": second_kit, "version": 1}],
                "context": {"tenant_id": secondary["tenant_id"]},
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            },
        )
        second_headers = {
            "X-Tenant-Id": second_admin["X-Tenant-Id"],
            "X-MCP-Binding": issued["token"],
        }

        async def submit(name, key=None):
            request_headers = {**headers, **({"X-MCP-Idempotency-Key": key} if key else {})}
            response = await client.post(
                "/mcp/runs", headers=request_headers, json={"tool": name, "inputs": {}}
            )
            response.raise_for_status()
            return response.json()

        # Queued cancellation and stable durable acceptance without any workers.
        first = await submit("acceptance_broker", "acceptance-deduplication")
        assert (await submit("acceptance_broker", "acceptance-deduplication"))["run_id"] == first[
            "run_id"
        ]
        await client.post(f"/mcp/runs/{first['run_id']}/cancel", headers=headers)
        start_worker(8034)
        assert (await terminal(client, headers, first["run_id"]))["status"] == "cancelled"
        await clean()
        for worker in tuple(workers):
            await stop_worker(worker)

        checkpoints = [
            "before_create",
            "after_create",
            "before_attach",
            "after_attach",
            "before_mutation",
            "mutation_before_outcome",
            "after_mutation",
            "before_result",
            "after_result",
        ]
        for point in checkpoints:
            marker = Path(str(checkpoint_file))
            marker.unlink(missing_ok=True)
            doomed = start_worker(8034, point)
            run = await submit(
                "acceptance_mutation" if "mutation" in point else "acceptance_broker"
            )
            for attempt in range(900):
                if attempt in {150, 300}:
                    # A stalled case records the doomed worker's task/thread stacks.
                    doomed.send_signal(signal.SIGUSR2)
                    doomed.send_signal(signal.SIGUSR1)
                if marker.exists():
                    assert json.loads(marker.read_bytes()) == {
                        "point": point,
                        "run_id": run["run_id"],
                    }
                    break
                if doomed.poll() is not None:
                    raise RuntimeError(f"crash worker exited before {point}")
                await asyncio.sleep(0.1)
            else:
                doomed.send_signal(signal.SIGUSR2)
                doomed.send_signal(signal.SIGUSR1)
                await asyncio.sleep(2)
                raise RuntimeError(f"crash checkpoint not reached: {point}")
            doomed.kill()  # actual process death: no finally/cleanup handlers
            await asyncio.to_thread(doomed.wait)
            workers.remove(doomed)
            recovery = start_worker(8035)
            result = await terminal(client, headers, run["run_id"])
            assert result["status"] == (
                "succeeded" if point == "after_result" else "interrupted"
            ), (point, result)
            await clean()
            async with database.pool.connection() as conn:
                journals = await (
                    await conn.execute(
                        "SELECT status FROM mcp_run_api_calls WHERE run_id=%s",
                        (UUID(run["run_id"]),),
                    )
                ).fetchall()
            if point in {"before_mutation", "mutation_before_outcome"}:
                assert journals and journals[-1]["status"] == "outcome_unknown"
            # Recovery must never replay any package/API request.
            if point == "before_create":
                assert not journals
            await stop_worker(recovery)
            print(json.dumps({"crash_checkpoint": point, "status": "passed"}), flush=True)

        # Two actual worker processes share the same durable capacity. Caller
        # disconnect is represented by submission followed by no waiting request.
        runs = []
        for _ in range(3):
            runs.append((await submit("acceptance_broker"), headers))
            response = await client.post(
                "/mcp/runs",
                headers=second_headers,
                json={"tool": "acceptance_broker", "inputs": {}},
            )
            response.raise_for_status()
            runs.append((response.json(), second_headers))
        start_worker(8034)
        start_worker(8035)
        saw_shared = False
        for _ in range(600):
            async with database.pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT tenant_id,worker_id FROM mcp_runs WHERE status IN ('starting','running')"
                    )
                ).fetchall()
                queue_counts = await (
                    await conn.execute("SELECT count(*) AS n FROM mcp_runs WHERE status='queued'")
                ).fetchone()
                assert queue_counts is not None
                queued = queue_counts["n"]
            assert len(rows) <= 2 and len({r["tenant_id"] for r in rows}) == len(rows)
            if len(rows) == 2:
                assert len({r["worker_id"] for r in rows}) == 2
                saw_shared = True
            if not rows and not queued:
                break
            await asyncio.sleep(0.05)
        assert saw_shared, "two tenants/workers did not share live global capacity"
        for run, owner_headers in runs:
            assert (await terminal(client, owner_headers, run["run_id"]))["status"] == "succeeded"
        await second_post(
            f"/mcp/bindings/{issued['binding']['binding_id']}/revoke", {"expected_revision": 1}
        )
        print(
            json.dumps(
                {"multi_worker_tenant_fairness": "passed", "global_slots": 2, "tenant_slots": 1}
            ),
            flush=True,
        )
        active = await submit("acceptance_loop")
        for _ in range(100):
            status = (await client.get(f"/mcp/runs/{active['run_id']}", headers=headers)).json()[
                "status"
            ]
            if status == "running":
                break
            await asyncio.sleep(0.1)
        await client.post(f"/mcp/runs/{active['run_id']}/cancel", headers=headers)
        assert (await terminal(client, headers, active["run_id"]))["status"] == "cancelled"
        await clean()
        # Restart both actual processes, then execute against the same database.
        for process in tuple(workers):
            await stop_worker(process)
        start_worker(8034)
        start_worker(8035)
        resumed = await submit("acceptance_broker")
        assert (await terminal(client, headers, resumed["run_id"]))["status"] == "succeeded"
        await clean()
        print(json.dumps({"durable_restart_cancellation": "passed", "workers": 2}), flush=True)

    try:
        start(
            "uvicorn",
            args=(
                "scripts.acceptance_fixtures:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8040",
                "--no-access-log",
                "--log-level",
                "warning",
            ),
        )
        await wait_ready("http://127.0.0.1:8040/evidence")
        api_env = dict(base_env)
        api_env.pop("MCP_CREDENTIAL_BUNDLE_FILE", None)
        api_env.pop("MCP_SERVICE_DESTINATIONS_FILE", None)
        start(
            "uvicorn",
            env=api_env,
            args=(
                "src.asgi:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8033",
                "--no-access-log",
                "--log-level",
                "warning",
            ),
        )
        await wait_ready("http://127.0.0.1:8033/health/ready")
        await verify(config, settings, lifecycle=lifecycle)
    finally:
        for process in tuple(workers):
            await stop_worker(process)
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 10)
                except TimeoutError:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        # Only the dedicated acceptance namespace's labeled runner pods exist here.
        for pod in await backend.list():
            await backend.stop(pod)
        await backend.close()
        await database.close()
        scratch.cleanup()


def main():
    asyncio.run(run(Path(os.environ["MCP_ACCEPTANCE_FIXTURE_DIRECTORY"])))


if __name__ == "__main__":
    main()
