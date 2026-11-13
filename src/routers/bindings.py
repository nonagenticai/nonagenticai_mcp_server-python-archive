"""Tenant operator access management. Responses never serialize credential values."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import UUID7

from src.auth.principal import Principal
from src.dependencies import get_access, get_operator
from src.storage.bindings import get_toolkit, parse
from src.toolkits.models import (
    BindingSpec,
    BindingUpdate,
    CredentialProfile,
    ProfileUpdate,
    RevisionRequest,
    ToolkitManifest,
    ToolkitUpdate,
)
from src.toolkits.service import AccessService

router = APIRouter(prefix="/mcp")
Service = Annotated[AccessService, Depends(get_access)]
Operator = Annotated[Principal, Depends(get_operator)]


def json_contract(model):
    async def read(request: Request):
        # Validate JSON directly: strict UUID/datetime/tuple fields have explicit
        # JSON representations; Python-mode validation would reject those strings.
        return model.model_validate_json(await request.body())

    return read


@router.post("/toolkits", status_code=201)
async def create_toolkit(
    manifest: Annotated[ToolkitManifest, Depends(json_contract(ToolkitManifest))],
    service: Service,
    principal: Operator,
):
    return await service.toolkit_write(principal, manifest)


@router.post("/toolkits/{identity}/versions", status_code=201)
async def append_toolkit(
    identity: UUID7,
    payload: Annotated[ToolkitUpdate, Depends(json_contract(ToolkitUpdate))],
    service: Service,
    principal: Operator,
):
    if payload.manifest.toolkit_id != identity:
        raise ValueError("toolkit identity mismatch")
    return await service.toolkit_write(principal, payload.manifest, payload.expected_revision)


@router.get("/toolkits")
async def list_toolkits(service: Service, principal: Operator, limit: int = 50, offset: int = 0):
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("invalid pagination")
    async with service.repository.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT toolkit_id,name,revision FROM public.mcp_toolkits WHERE tenant_id=%s ORDER BY toolkit_id LIMIT %s OFFSET %s",
            (principal.tenant_id, limit, offset),
        )
        return {"items": await cur.fetchall(), "limit": limit, "offset": offset}


@router.get("/toolkits/{identity}/versions/{version}")
async def inspect_toolkit(identity: UUID7, version: int, service: Service, principal: Operator):
    async with service.repository.pool.connection() as conn:
        return await get_toolkit(conn, principal.tenant_id, identity, version)


@router.post("/toolkits/{identity}/versions/{version}/publish")
async def publish_toolkit(
    identity: UUID7,
    version: int,
    payload: Annotated[RevisionRequest, Depends(json_contract(RevisionRequest))],
    service: Service,
    principal: Operator,
):
    return await service.toolkit_transition(
        principal, identity, version, "published", payload.expected_revision
    )


@router.post("/toolkits/{identity}/versions/{version}/disable")
async def disable_toolkit(
    identity: UUID7,
    version: int,
    payload: Annotated[RevisionRequest, Depends(json_contract(RevisionRequest))],
    service: Service,
    principal: Operator,
):
    return await service.toolkit_transition(
        principal, identity, version, "disabled", payload.expected_revision
    )


@router.post("/credential-profiles", status_code=201)
async def create_profile(
    profile: Annotated[CredentialProfile, Depends(json_contract(CredentialProfile))],
    service: Service,
    principal: Operator,
):
    return await service.profile_write(principal, profile)


@router.put("/credential-profiles/{identity}")
async def update_profile(
    identity: UUID7,
    payload: Annotated[ProfileUpdate, Depends(json_contract(ProfileUpdate))],
    service: Service,
    principal: Operator,
):
    if payload.profile.profile_id != identity:
        raise ValueError("profile identity mismatch")
    return await service.profile_write(principal, payload.profile, payload.expected_revision)


@router.post("/credential-profiles/{identity}/validate")
async def validate_profile(identity: UUID7, service: Service, principal: Operator):
    async with service.repository.pool.connection() as conn:
        row = await service.profile_row(conn, principal.tenant_id, identity)
        profile = parse(CredentialProfile, row["manifest"])
        if profile.gateway.app_key != service.settings.license_app_key:
            raise ValueError("gateway application mismatch")
        return {
            "profile_id": identity,
            "revision": row["revision"],
            "metadata_valid": True,
            "credentials_verified": False,
        }


@router.post("/bindings", status_code=201)
async def create_binding(
    spec: Annotated[BindingSpec, Depends(json_contract(BindingSpec))],
    service: Service,
    principal: Operator,
):
    binding, token = await service.binding_write(principal, spec)
    return {"binding": binding, "token": token}


@router.get("/bindings/{identity}")
async def inspect_binding(identity: UUID7, service: Service, principal: Operator):
    return await service.repository.inspect(principal.tenant_id, identity)


@router.patch("/bindings/{identity}")
async def update_binding(
    identity: UUID7,
    payload: Annotated[BindingUpdate, Depends(json_contract(BindingUpdate))],
    service: Service,
    principal: Operator,
):
    binding, _ = await service.binding_write(
        principal, payload.spec, identity, payload.expected_revision
    )
    return binding


@router.post("/bindings/{identity}/rotate-token")
async def rotate_binding(
    identity: UUID7,
    payload: Annotated[RevisionRequest, Depends(json_contract(RevisionRequest))],
    service: Service,
    principal: Operator,
):
    binding, token = await service.binding_action(principal, identity, payload.expected_revision)
    return {"binding": binding, "token": token}


@router.post("/bindings/{identity}/revoke")
async def revoke_binding(
    identity: UUID7,
    payload: Annotated[RevisionRequest, Depends(json_contract(RevisionRequest))],
    service: Service,
    principal: Operator,
):
    binding, _ = await service.binding_action(
        principal, identity, payload.expected_revision, revoke=True
    )
    return binding


@router.get("/bindings/{identity}/effective-tools")
async def preview_binding(identity: UUID7, request: Request, service: Service, principal: Operator):
    tools = await request.app.state.catalog_resolver.preview(principal, identity)
    return {
        "tools": [t.public() for t in tools],
        "execution": "enabled" if request.app.state.settings.execution_enabled else "unavailable",
    }


@router.get("/available-tools")
async def available_tools(request: Request, service: Service):
    tools = await request.app.state.catalog_resolver.resolve(request.state.principal)
    return {
        "tools": [t.public() for t in tools],
        "execution": "enabled" if request.app.state.settings.execution_enabled else "unavailable",
    }
