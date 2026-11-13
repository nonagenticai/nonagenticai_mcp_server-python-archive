"""Synthetic license/upstream server for a disposable cluster acceptance deployment.

This server never accesses real licenses. Mount the generated acceptance fixture
configuration and credential bundle; never expose it outside its test namespace.
"""

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
counts: dict[str, int] = {}


def fixtures():
    root = Path(os.environ.get("MCP_ACCEPTANCE_FIXTURE_DIRECTORY", "/run/acceptance"))
    config = json.loads((root / "config.json").read_bytes())
    bundle = json.loads((root / "bundle.json").read_bytes())
    if config.get("synthetic") is not True:
        raise ValueError("synthetic fixtures required")
    return config, bundle["entries"]


@app.post("/introspect")
async def introspect(request: Request):
    payload = await request.json()
    _, entries = fixtures()
    match = next(
        (
            e
            for e in entries
            if e["token"] == payload.get("token") and e["app_key"] == payload.get("app")
        ),
        None,
    )
    if not match:
        return {"active": False}
    return {
        "active": True,
        "tenant_id": match["tenant_id"],
        "license_id": match["license_id"],
        "subject_id": "synthetic-acceptance",
        "app": {"key": match["app_key"]},
        "expires_at": None,
        "token_expires_at": None,
    }


@app.get("/fixture")
@app.post("/fixture")
async def upstream(request: Request):
    _, entries = fixtures()
    match = next(
        (
            e
            for e in entries
            if e["app_key"].startswith("acceptance_")
            and request.headers.get("authorization") == "Bearer " + e["token"]
        ),
        None,
    )
    if not match:
        return JSONResponse({"code": "fixture_credential_rejected"}, status_code=401)
    counts[match["app_key"]] = counts.get(match["app_key"], 0) + 1
    return {"ok": True}


@app.get("/evidence")
async def evidence():
    # Application names/counts only; no bearer values, headers or request bodies.
    return {"calls": counts}
