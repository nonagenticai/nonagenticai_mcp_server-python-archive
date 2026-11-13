"""Explicit tenant-owned tool lifecycle; no platform writes or execution paths."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import UUID7

from src.catalog.http_models import ToolAppend, ToolUpload
from src.catalog.service import CatalogService
from src.dependencies import get_catalog
from src.routers.bindings import Operator, json_contract
from src.toolkits.models import RevisionRequest

router = APIRouter(prefix="/mcp/tools")
Service = Annotated[CatalogService, Depends(get_catalog)]


def body_schema(model) -> dict:
    schema = model.model_json_schema()
    # Give inline JSON Schema its own resource root so local $defs references
    # resolve inside this envelope, rather than the root OpenAPI document.
    schema["$id"] = "urn:nonagentic:catalog:" + model.__name__
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


@router.post("", status_code=201, openapi_extra=body_schema(ToolUpload))
async def create_tool(
    payload: Annotated[ToolUpload, Depends(json_contract(ToolUpload))],
    service: Service,
    principal: Operator,
):
    if payload.manifest.tenant_id != principal.tenant_id:
        raise PermissionError("tenant mismatch")
    return await service.create_draft(
        principal.tenant_id, payload.manifest, payload.files, principal.actor
    )


@router.post("/{identity}/versions", status_code=201, openapi_extra=body_schema(ToolAppend))
async def append_tool(
    identity: UUID7,
    payload: Annotated[ToolAppend, Depends(json_contract(ToolAppend))],
    service: Service,
    principal: Operator,
):
    if payload.manifest.tenant_id != principal.tenant_id:
        raise PermissionError("tenant mismatch")
    if payload.manifest.tool_id != identity:
        raise ValueError("path and manifest identity differ")
    return await service.append_draft(
        principal.tenant_id,
        identity,
        payload.manifest,
        payload.files,
        principal.actor,
        expected_revision=payload.expected_revision,
        exact_version=True,
    )


@router.get("/definitions")
async def definitions(
    service: Service,
    principal: Operator,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return {
        "items": await service.repository.definitions(principal.tenant_id, limit, offset),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{identity}/versions/{version}")
async def inspect(
    identity: UUID7, version: Annotated[int, Path(ge=1)], service: Service, principal: Operator
):
    return {
        "descriptor": await service.repository.get_version(principal.tenant_id, identity, version),
        "files": await service.repository.files(principal.tenant_id, identity, version),
    }


@router.post("/{identity}/versions/{version}/validate")
async def validate(identity: UUID7, version: int, service: Service, principal: Operator):
    original = await service.repository.get_version(principal.tenant_id, identity, version)
    await service.validate(
        original.manifest, await service.repository.files(principal.tenant_id, identity, version)
    )
    return {
        "tool_id": identity,
        "version": version,
        "metadata_valid": True,
        "credentials_verified": False,
        "execution": "unavailable",
    }


def transition_route(status: str):
    async def transition(
        identity: UUID7,
        version: int,
        payload: Annotated[RevisionRequest, Depends(json_contract(RevisionRequest))],
        service: Service,
        principal: Operator,
    ):
        return await service.transition(
            principal.tenant_id,
            identity,
            version,
            status,
            principal.actor,
            expected_revision=payload.expected_revision,
        )

    return transition


for action, status in (("publish", "published"), ("disable", "disabled"), ("retire", "retired")):
    router.add_api_route(
        "/{identity}/versions/{version}/" + action,
        transition_route(status),
        methods=["POST"],
        name=action + "_tool",
        openapi_extra=body_schema(RevisionRequest),
    )
