"""Exercise the built image against disposable PostgreSQL, leaving no resources."""

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid7

from scripts.catalog_image_fixture import ACCEPTANCE, AUTHORITY

# CI may point this at an internal mirror; Docker Hub stays the default.
POSTGRES_IMAGE = os.environ.get("MCP_VERIFY_POSTGRES_IMAGE", "postgres:18")


def docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], check=False, text=True, capture_output=True, timeout=60
    )
    if result.returncode != 0:
        # Surface the container's own error: CalledProcessError alone hides stderr in CI logs.
        print(
            f"docker {args[0]} failed ({result.returncode}):\n{result.stderr[-4000:]}", flush=True
        )
        raise subprocess.CalledProcessError(
            result.returncode, ["docker", *args], result.stdout, result.stderr
        )
    return result.stdout.strip()


def verify(image: str) -> None:
    suffix = uuid7().hex[-12:]
    pg, api, authority = [f"mcp-image-{name}-{suffix}" for name in ("pg", "api", "auth")]
    # Every other container joins the database container's network namespace and talks over
    # loopback. CI's dind shares the runner pod's netns, where the ambient mesh's redirect
    # rules intercept bridged container-to-container TCP ("server closed the connection
    # unexpectedly", nothing in the postgres log); loopback is never redirected.
    network = f"container:{pg}"
    secret_directory = tempfile.TemporaryDirectory(prefix="mcp-image-key-")
    key_path = Path(secret_directory.name) / "admin-key"
    key_path.write_text("a" * 32)
    # The bind-mounted synthetic key must be readable by the image's uid 1000,
    # which differs from the CI runner's uid; it guards nothing real.
    os.chmod(secret_directory.name, 0o755)
    os.chmod(key_path, 0o644)
    label = "nonagentic.test-image=nonagenticai_mcp_server"
    try:
        docker(
            "run",
            "--rm",
            "-d",
            "--name",
            pg,
            "--label",
            label,
            "-e",
            "POSTGRES_PASSWORD=synthetic-image-only",  # pragma: allowlist secret
            "-e",
            "POSTGRES_DB=mcp_catalog",
            POSTGRES_IMAGE,
        )
        deadline = time.monotonic() + 60
        while True:
            # The postgres entrypoint runs initdb's temporary server, then restarts. A TCP probe
            # can win the race against that restart (seen in CI dind), so require the final
            # server's second "ready to accept connections" log line first.
            logs = subprocess.run(
                ["docker", "logs", pg], text=True, capture_output=True, timeout=30
            )
            if (logs.stdout + logs.stderr).count("ready to accept connections") < 2:
                if time.monotonic() >= deadline:
                    raise RuntimeError("disposable image test database did not become ready")
                time.sleep(0.2)
                continue
            try:
                # Use TCP: pg_isready during init can see the temporary Unix-only server.
                docker(
                    "exec",
                    "-e",
                    "PGPASSWORD=synthetic-image-only",
                    pg,
                    "psql",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "postgres",
                    "-d",
                    "mcp_catalog",
                    "-c",
                    "SELECT 1",
                )
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "disposable image test database did not become ready"
                    ) from None
                time.sleep(0.2)
        owner = "postgresql://postgres:synthetic-image-only@127.0.0.1:5432/mcp_catalog"  # pragma: allowlist secret
        runtime = "postgresql://mcp_runtime:synthetic-image-only@127.0.0.1:5432/mcp_catalog"  # pragma: allowlist secret
        options = (
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
        )
        migrate = ("run", *options, "-e", f"DATABASE_URL={owner}", "--entrypoint", "alembic")
        # Twice: the second run proves the chain is idempotent at head.
        for _ in range(2):
            docker(*migrate, image, "upgrade", "head")
        docker(
            "exec",
            pg,
            "psql",
            "-U",
            "postgres",
            "-d",
            "mcp_catalog",
            "-c",
            "CREATE ROLE mcp_runtime LOGIN PASSWORD 'synthetic-image-only'",
        )
        docker(
            "run",
            *options,
            "-e",
            f"DATABASE_URL={owner}",
            "--entrypoint",
            "python",
            image,
            "-m",
            "scripts.grant_runtime_role",
            "--role",
            "mcp_runtime",
        )
        docker(
            "run",
            "-d",
            "--name",
            authority,
            "--label",
            label,
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "python",
            image,
            "-c",
            AUTHORITY,
        )
        docker(
            "run",
            "-d",
            "--name",
            api,
            "--label",
            label,
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "-e",
            f"DATABASE_URL={runtime}",
            "-e",
            "LICENSE_INTROSPECT_URL=http://127.0.0.1:8080/introspect",
            "-e",
            "MCP_LICENSE_APP_KEY=mcp",
            "-e",
            "MCP_LICENSE_ALLOW_HTTP=true",
            "-e",
            "MCP_ADMIN_KEY_FILE=/run/mcp/admin-key",  # pragma: allowlist secret (mount path)
            "-e",
            'MCP_ADMIN_TENANT_IDS=["10000000-0000-7000-8000-000000000001"]',
            "-e",
            'MCP_APPROVED_RUNNER_IMAGES=["registry.example/runner@sha256:' + "a" * 64 + '"]',
            "--mount",
            f"type=bind,source={key_path},target=/run/mcp/admin-key,readonly",
            image,
        )
        check = """import urllib.request, urllib.error, json
for path in ('/health','/health/live','/health/ready'):
    result=json.load(urllib.request.urlopen('http://127.0.0.1:8033'+path,timeout=3))
    assert result['execution']=='unavailable',result
for path in ('/mcp','/token','/mcp/tools/list'):
    try:
        urllib.request.urlopen('http://127.0.0.1:8033'+path,timeout=3)
        raise AssertionError(path)
    except urllib.error.HTTPError as error:
        assert error.code == (404 if path == '/token' else 401)
print('image probes, authentication boundary and removed token route verified')
"""
        deadline = time.monotonic() + 30
        while True:
            try:
                print(docker("exec", api, "python", "-c", check), flush=True)
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("image did not become ready with the runtime role") from None
                time.sleep(0.2)
        assert (
            docker(
                "exec",
                pg,
                "psql",
                "-U",
                "postgres",
                "-d",
                "mcp_catalog",
                "-Atc",
                "SELECT count(*) FROM mcp_tools",
            )
            == "0"
        )
        print(docker("exec", api, "python", "-c", ACCEPTANCE), flush=True)
        docker(
            "run",
            *options,
            "--entrypoint",
            "python",
            image,
            "-c",
            "import importlib.util,os,shutil; assert os.getuid()==1000; "
            "assert shutil.which('node') is None; assert importlib.util.find_spec('fastmcp') is not None; "
            "assert importlib.util.find_spec('RestrictedPython') is None; "
            "assert importlib.util.find_spec('kubernetes') is None; "
            "from importlib.metadata import version; assert version('fastmcp')=='4.1.4'; assert version('mcp')=='2.3.0'; "
            "from src.catalog.provider import CatalogProvider; "
            # The image's own migrations are the authority: a hardcoded revision here went stale the
            # moment 0013 landed and failed the build for a correct change. This compares the
            # constant the app checks at boot with the head the image actually ships.
            "from alembic.config import Config; from alembic.script import ScriptDirectory; "
            "head=ScriptDirectory.from_config(Config('/app/alembic.ini')).get_current_head(); "
            "from src.storage.schema import EXPECTED_REVISION; "
            "assert EXPECTED_REVISION==head, (EXPECTED_REVISION, head)",
        )
        print(
            json.dumps({"image": image, "status": "verified", "execution": "unavailable"}),
            flush=True,
        )
    finally:
        for name in (api, authority, pg):
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False
            )
        secret_directory.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    verify(parser.parse_args().image)


if __name__ == "__main__":
    main()
