"""Disposable postgres:18 container provisioning, removed by its exact container ID.

Only explicit TEST_DATABASE_URL/TEST_POSTGRES_URL may replace this infrastructure.
Runtime DATABASE_URL is deliberately ignored by the test suite.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from types import FrameType

#: Env vars consulted, in precedence order.
PRECEDENCE = ("TEST_DATABASE_URL", "TEST_POSTGRES_URL")

# CI dind pulls via the registry mirror (MCP_TEST_POSTGRES_IMAGE); locally Docker Hub.
EPHEMERAL_IMAGE = os.environ.get("MCP_TEST_POSTGRES_IMAGE", "postgres:18")
EPHEMERAL_DATABASE = "mcp_test"
#: Label on every container this module starts, so a leak (SIGKILL) is findable.
EPHEMERAL_LABEL = "nonagentic.test-db=nonagenticai_mcp_server"


class MissingTestDatabaseURLError(RuntimeError):
    """None of :data:`PRECEDENCE` is set."""


def missing_url_message() -> str:
    return (
        "nonagenticai_mcp_server tests: no test database — set TEST_DATABASE_URL (or "
        "TEST_POSTGRES_URL), or make `docker` available so the suite auto-starts an "
        "ephemeral postgres:18; there is no host default. Manually:\n"
        "  CID=$(docker run --rm -d --tmpfs /var/lib/postgresql -p 127.0.0.1::5432 \\\n"
        f"        -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB={EPHEMERAL_DATABASE} {EPHEMERAL_IMAGE})\n"
        "  PORT=$(docker port \"$CID\" 5432/tcp | head -1 | awk -F: '{print $NF}')\n"
        "  export TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:$PORT/"  # pragma: allowlist secret
        f"{EPHEMERAL_DATABASE}\n"
        '  # ...run the suite, then: docker rm -f "$CID"'
    )


def resolve_test_database_url() -> str:
    """First of :data:`PRECEDENCE` that is set; raises when none is."""
    for name in PRECEDENCE:
        value = os.getenv(name)
        if value:
            return value
    raise MissingTestDatabaseURLError(missing_url_message())


def docker_available() -> bool:
    """True when a `docker` CLI exists AND its daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                timeout=20,
                check=False,
            ).returncode
            == 0
        )
    except OSError, subprocess.SubprocessError:
        return False


class EphemeralPostgres:
    """A container this process started; :meth:`stop` removes it by ITS id only."""

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id
        self.dsn = ""
        self._stopped = False

    def stop(self) -> None:
        if self._stopped or not self.container_id:
            return
        self._stopped = True
        try:
            subprocess.run(
                ["docker", "rm", "-f", self.container_id],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            sys.stderr.write(
                f"could not remove test container {self.container_id}; run: docker rm -f {self.container_id}\n"
            )


def _remove_on_signal(pg: EphemeralPostgres) -> None:
    """Remove ``pg`` on SIGTERM/SIGHUP too — ``atexit`` does NOT run on a signal death.

    The handler removes the container (idempotent), chains any previous Python
    handler, then restores ``SIG_DFL`` and re-sends the signal so the process dies
    of it (exit 128+signum). ``sys.exit`` is NOT used: a ``SystemExit`` raised
    inside a running test is recorded by pytest as a failure and the session
    carries on. Only installable from the main thread — the conftest import in the
    (xdist controller) process, the only one that starts a container.
    """
    previous: dict[int, Callable[[int, FrameType | None], None] | int | None] = {}

    def _handler(signum: int, frame: FrameType | None) -> None:
        pg.stop()
        prev = previous.get(signum)
        if prev is not None and not isinstance(prev, int):
            prev(signum, frame)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, _handler)
        except ValueError:  # not the main thread: atexit + pytest_unconfigure still apply
            return


def _select_one_error(dsn: str) -> str | None:
    """``None`` when a TCP connection to ``dsn`` answers ``SELECT 1``."""
    import psycopg

    try:
        with psycopg.connect(
            dsn.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=3
        ) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
        return f"{type(exc).__name__}: {exc}"
    return None


def start_ephemeral_postgres(
    database: str = EPHEMERAL_DATABASE, timeout_s: float = 90.0
) -> EphemeralPostgres:
    """Start a throwaway ``postgres:18`` on a RANDOM 127.0.0.1 port; wait for it.

    Never binds a fixed host port. Ready means a real TCP ``SELECT 1`` against the
    named database (``POSTGRES_DB`` creates it; the image's init-phase server is
    unix-socket only, so TCP success implies the final server).
    """
    # atexit + signal handlers go in BEFORE the container exists: no window in which
    # a SIGTERM kills this process and leaves a running container behind.
    pg = EphemeralPostgres("")
    atexit.register(pg.stop)
    _remove_on_signal(pg)
    cid = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--label",
            EPHEMERAL_LABEL,
            "--tmpfs",
            "/var/lib/postgresql",
            "-p",
            "127.0.0.1::5432",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            f"POSTGRES_DB={database}",
            EPHEMERAL_IMAGE,
        ],  # fmt: skip
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    ).stdout.strip()
    pg.container_id = cid
    try:
        port_out = subprocess.run(
            ["docker", "port", cid, "5432/tcp"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        port = port_out.splitlines()[0].rsplit(":", 1)[1].strip()
        dsn = f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/{database}"  # pragma: allowlist secret
        deadline = time.monotonic() + timeout_s
        error = _select_one_error(dsn)
        while error is not None:
            if time.monotonic() > deadline:
                raise RuntimeError(f"ephemeral postgres {cid[:12]} not ready: {error}")
            time.sleep(0.5)
            error = _select_one_error(dsn)
        pg.dsn = dsn
        return pg
    except BaseException:
        pg.stop()
        raise
