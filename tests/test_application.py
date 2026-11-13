import asyncio
from collections.abc import Callable
from typing import cast
from unittest.mock import patch

import psycopg
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.asgi import create_app
from src.config import Settings
from src.dependencies import get_catalog
from src.run_mcp import main
from src.storage.database import Database
from tests.fixtures import settings


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/"),
        ("POST", "/mcp"),
        ("GET", "/mcp"),
        ("DELETE", "/mcp"),
        ("POST", "/mcp/tools/add"),
        ("POST", "/mcp/tools/add-multi"),
        ("GET", "/mcp/tools/list"),
        ("GET", "/mcp/tools/backup"),
        ("POST", "/mcp/tools/restore"),
        ("GET", "/mcp/tool-versions/example"),
        ("GET", "/mcp/audit/logs"),
        ("GET", "/mcp/info"),
        ("GET", "/mcp/available-tools"),
        ("GET", "/mcp/health-status"),
        ("POST", "/token"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
def test_removed_routes_have_no_handlers(method, path):
    # No lifespan/database needed: no route can reach the removed implementations.
    response = TestClient(create_app(settings())).request(method, path, json={})
    assert response.status_code == (401 if path == "/mcp" or path.startswith("/mcp/") else 404)


def test_application_readiness_and_restart(database_url):
    configuration = Settings(database_url=SecretStr(database_url), pool_min_size=0)
    app = create_app(configuration)
    for _ in range(2):
        with TestClient(app) as client:
            assert client.get("/health/live").json() == {
                "status": "alive",
                "execution": "unavailable",
            }
            assert client.get("/health").status_code == 200
            assert client.get("/health/ready").json() == {
                "status": "ready",
                "execution": "unavailable",
            }
            request = Request({"type": "http", "app": app})
            assert get_catalog(request) is app.state.catalog
        assert app.state.database.pool.closed


def test_unmigrated_readiness_is_bounded_without_ddl(empty_database):
    configuration = Settings(database_url=SecretStr(empty_database), pool_min_size=0)
    with TestClient(create_app(configuration)) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "execution": "unavailable"}
    with psycopg.connect(configuration.dsn) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ).fetchone() == (0,)


def test_readiness_timeout_and_cleanup():
    class StalledDatabase:
        def __init__(self, configuration):
            self.pool = self
            self.closed = False

        async def open(self):
            pass

        async def close(self):
            self.closed = True

        def connection(self):
            return self

        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, *args):
            pass

    app = create_app(
        settings(health_timeout_seconds=0.01), cast(Callable[[Settings], Database], StalledDatabase)
    )
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 503
    assert app.state.database.closed


def test_same_asgi_entrypoint():
    with patch("src.run_mcp.uvicorn.run") as run:
        main()
    run.assert_called_once_with("src.asgi:app", host="127.0.0.1", port=8033)


def test_transport_startup_failure_closes_dependencies(database_url, monkeypatch):
    app = create_app(settings(database_url=database_url))

    def broken(provider, builtin=None, **_):
        raise RuntimeError("synthetic transport startup failure")

    monkeypatch.setattr("src.asgi.create_protocol", broken)
    with pytest.raises(RuntimeError, match="transport startup"):
        with TestClient(app):
            pass
    assert app.state.database.pool.closed
    assert app.state.licenses.client.is_closed
