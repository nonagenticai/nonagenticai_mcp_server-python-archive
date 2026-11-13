"""Explicit platform-only service/operation authoring using a separate limited role."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg import sql
from pydantic import UUID7

from src.catalog.importer import OperationImport, import_drafts
from src.catalog.models import Contract, ExactReference, OperationManifest, ServiceManifest
from src.catalog.service import PlatformCatalogService
from src.catalog.validation import validate_schema
from src.routers.bindings import Operator, json_contract
from src.storage.catalog import snapshot

router = APIRouter(prefix="/mcp")


def platform(request: Request, principal: Operator):
    if not principal.platform:
        raise PermissionError("platform authorization required")
    service = getattr(request.app.state, "platform_catalog", None)
    if service is None:
        raise HTTPException(503, "platform_authoring_unavailable")
    return service


Platform = Annotated[PlatformCatalogService, Depends(platform)]


class ServiceAppend(Contract):
    manifest: ServiceManifest
    expected_revision: int


@router.post("/services", status_code=201)
async def create_service(
    payload: Annotated[ServiceManifest, Depends(json_contract(ServiceManifest))],
    service: Platform,
    principal: Operator,
):
    if payload.version != 1:
        raise ValueError("first version required")
    await service.add_service(payload, principal.actor)
    return {"manifest": payload, "status": "draft"}


@router.post("/services/{identity}/versions", status_code=201)
async def append_service(
    identity: UUID7,
    payload: Annotated[ServiceAppend, Depends(json_contract(ServiceAppend))],
    service: Platform,
    principal: Operator,
):
    if identity != payload.manifest.service_id:
        raise ValueError("identity mismatch")
    await service.add_service(
        payload.manifest, principal.actor, expected_revision=payload.expected_revision
    )
    return {"manifest": payload.manifest, "status": "draft"}


@router.post("/operations/{identity}/versions", status_code=201)
async def append_operation(
    identity: UUID7,
    payload: Annotated[OperationManifest, Depends(json_contract(OperationManifest))],
    service: Platform,
    principal: Operator,
):
    if identity != payload.operation_id:
        raise ValueError("identity mismatch")
    await service.add_operation(payload, principal.actor)
    return {"manifest": payload, "status": "draft"}


@router.post("/operations/import", status_code=201)
async def import_operations(
    payload: Annotated[OperationImport, Depends(json_contract(OperationImport))],
    service: Platform,
    principal: Operator,
):
    operations = import_drafts(payload, service.catalog.settings)
    parent, _ = await service.catalog.repository.service(payload.service)
    if any(o.max_response_bytes > parent.max_response_bytes for o in operations):
        raise ValueError("import response limit exceeds service ceiling")
    async with service.repository.pool.connection() as conn, conn.transaction():
        for manifest in operations:
            await conn.execute(
                "INSERT INTO public.mcp_operation_versions(operation_id,version,service_id,service_version,manifest,created_by,updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    manifest.operation_id,
                    1,
                    manifest.service.id,
                    manifest.service.version,
                    snapshot(manifest),
                    snapshot(principal.actor),
                    snapshot(principal.actor),
                ),
            )
    return {"items": operations, "status": "draft"}


def lifecycle(kind, status):
    async def change(identity: UUID7, version: int, service: Platform, principal: Operator):
        reference = ExactReference(id=identity, version=version)
        if kind == "operation":
            manifest, _ = await service.catalog.repository.operation(reference)
            validate_schema(manifest.input_schema, service.catalog.settings)
            validate_schema(manifest.output_schema, service.catalog.settings)
            await service.catalog._ownership_reads(manifest.scope_rules)
        else:
            await service.catalog.repository.service(reference)
        if status:
            await service.repository.transition(kind, reference, status, principal.actor)
        return {
            "id": identity,
            "version": version,
            "status": status or "valid",
            "credentials_verified": False,
        }

    return change


def listing(kind):
    async def items(
        service: Platform,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        table, column = (
            ("mcp_service_versions", "service_id")
            if kind == "service"
            else ("mcp_operation_versions", "operation_id")
        )
        async with service.repository.pool.connection() as conn:
            cur = await conn.execute(
                sql.SQL(
                    "SELECT {},version,status FROM public.{} ORDER BY {},version LIMIT %s OFFSET %s"
                ).format(sql.Identifier(column), sql.Identifier(table), sql.Identifier(column)),
                (limit, offset),
            )
            return {"items": await cur.fetchall()}

    return items


def detail(kind):
    async def item(identity: UUID7, version: int, service: Platform):
        manifest, status = await getattr(service.catalog.repository, kind)(
            ExactReference(id=identity, version=version)
        )
        return {"manifest": manifest, "status": status}

    return item


for kind, plural in (("service", "services"), ("operation", "operations")):
    router.add_api_route("/" + plural, listing(kind), methods=["GET"], name="list_" + plural)
    router.add_api_route(
        "/" + plural + "/{identity}/versions/{version}",
        detail(kind),
        methods=["GET"],
        name="inspect_" + kind,
    )
    for action, status in (("validate", None), ("publish", "published"), ("disable", "disabled")):
        router.add_api_route(
            "/" + plural + "/{identity}/versions/{version}/" + action,
            lifecycle(kind, status),
            methods=["POST"],
            name=action + "_" + kind,
        )
