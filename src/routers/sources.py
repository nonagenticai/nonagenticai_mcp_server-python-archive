"""Platform-operator tool-source registry and generic toolkit import.

Same authorization as ``src.routers.operations`` (platform admin key). Tokens are accepted
write-only; no response, log line, audit row or error carries one.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.platform.seed import SeedError, ToolkitBundle, import_bundle
from src.routers.bindings import Operator, json_contract
from src.sources.models import RevisionOnly, SourceCreate, SourceUpdate, TokenUpdate
from src.sources.repository import SourceRepository, SourceSecretUnavailableError

router = APIRouter(prefix="/mcp")


def sources(request: Request, principal: Operator) -> SourceRepository:
    if not principal.platform:
        raise PermissionError("platform authorization required")
    repository = getattr(request.app.state, "platform_sources", None)
    if repository is None:
        raise HTTPException(503, "platform_authoring_unavailable")
    return repository


Sources = Annotated[SourceRepository, Depends(sources)]


@router.post("/sources", status_code=201)
async def create_source(
    payload: Annotated[SourceCreate, Depends(json_contract(SourceCreate))],
    repository: Sources,
    principal: Operator,
):
    return await secret_guard(repository.create(payload, principal.actor))


@router.get("/sources")
async def list_sources(
    repository: Sources,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return {"items": await repository.page(limit, offset), "limit": limit, "offset": offset}


@router.get("/sources/{key}")
async def get_source(key: str, repository: Sources):
    return await repository.get(key)


@router.put("/sources/{key}")
async def update_source(
    key: str,
    payload: Annotated[SourceUpdate, Depends(json_contract(SourceUpdate))],
    repository: Sources,
    principal: Operator,
):
    return await secret_guard(repository.update(key, payload, principal.actor))


@router.put("/sources/{key}/token")
async def rotate_source_token(
    key: str,
    payload: Annotated[TokenUpdate, Depends(json_contract(TokenUpdate))],
    repository: Sources,
    principal: Operator,
):
    return await secret_guard(repository.set_token(key, payload, principal.actor))


@router.post("/sources/{key}/disable")
async def disable_source(
    key: str,
    payload: Annotated[RevisionOnly, Depends(json_contract(RevisionOnly))],
    repository: Sources,
    principal: Operator,
):
    return await repository.set_status(key, payload.expected_revision, "disabled", principal.actor)


@router.post("/sources/{key}/enable")
async def enable_source(
    key: str,
    payload: Annotated[RevisionOnly, Depends(json_contract(RevisionOnly))],
    repository: Sources,
    principal: Operator,
):
    return await repository.set_status(key, payload.expected_revision, "active", principal.actor)


async def secret_guard(awaitable):
    try:
        return await awaitable
    except SourceSecretUnavailableError:
        raise HTTPException(503, "source_secret_unavailable") from None


@router.post("/toolkits/import", status_code=201)
async def import_toolkit(
    payload: Annotated[ToolkitBundle, Depends(json_contract(ToolkitBundle))],
    request: Request,
    principal: Operator,
):
    if not principal.platform:
        raise PermissionError("platform authorization required")
    state = request.app.state
    pool = getattr(state, "platform_pool", None)
    if pool is None:
        raise HTTPException(503, "platform_authoring_unavailable")
    try:
        report = await import_bundle(state.database.pool, state.settings, payload, pool)
    except SeedError as error:
        raise HTTPException(409, {"error": "import_failed", "detail": str(error)}) from None
    return {
        "actions": report.actions,
        "versions": report.versions,
        "tools_skipped": report.tools_skipped,
        "changed": report.changed,
    }
