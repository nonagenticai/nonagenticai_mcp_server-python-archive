"""Tenant tool builder REST (docs/TOOL_BUILDER.md).

Two mounts of ONE route set, both acting only for the authenticated tenant:
  /mcp/builder/*                           the tenant's own tenant-level mcp licence
  /mcp/internal/tenants/{tenant_id}/builder/*   a product platform (service JWT + that tenant's
                                           licence) acting for tenant ``tenant_id``
The middleware (src.auth.protocol) authenticates both and answers 404 when the builder is
disabled; handlers see only the ``AuthorPrincipal`` and never a tenant from the request.
Responses never carry a secret: sources are viewed through ``SourceView``.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import UUID7, BaseModel

from src.auth.principal import AuthorPrincipal
from src.builder.models import (
    BindRequest,
    ImportRequest,
    OperationPatch,
    SpecFetchRequest,
    ToolCreate,
    ToolkitPublishRequest,
    ToolkitRequest,
    ToolPublishRequest,
    ToolTestRequest,
    ToolUpdate,
)
from src.builder.service import BuilderInputError, BuilderService, parse
from src.catalog.models import NAME_PATTERN, Digest
from src.dependencies import get_author, get_builder
from src.sources.models import RevisionOnly, TenantSourceCreate, TenantSourceUpdate, TokenUpdate
from src.toolkits.models import RevisionRequest

Author = Annotated[AuthorPrincipal, Depends(get_author)]
Builder = Annotated[BuilderService, Depends(get_builder)]
Key = Annotated[str, Path(pattern=NAME_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def body_of(model: type[BaseModel], *, optional: bool = False):
    async def read(request: Request):
        raw = await request.body()
        if optional and not raw.strip():
            raw = b"{}"
        return parse(model, raw)

    return read


def build_router(prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.get("/usage")
    async def usage(service: Builder, author: Author):
        return await service.usage(author)

    # --- sources ---
    @router.post("/sources", status_code=201)
    async def create_source(
        payload: Annotated[TenantSourceCreate, Depends(body_of(TenantSourceCreate))],
        service: Builder,
        author: Author,
    ):
        return await service.source_create(author, payload)

    @router.get("/sources")
    async def list_sources(service: Builder, author: Author, limit: Limit = 50, offset: Offset = 0):
        return await service.source_list(author, limit, offset)

    @router.get("/sources/{key}")
    async def get_source(key: Key, service: Builder, author: Author):
        return await service.source_get(author, key)

    @router.put("/sources/{key}")
    async def update_source(
        key: Key,
        payload: Annotated[TenantSourceUpdate, Depends(body_of(TenantSourceUpdate))],
        service: Builder,
        author: Author,
    ):
        return await service.source_update(author, key, payload)

    @router.put("/sources/{key}/secret")
    async def set_secret(
        key: Key,
        payload: Annotated[TokenUpdate, Depends(body_of(TokenUpdate))],
        service: Builder,
        author: Author,
    ):
        return await service.source_set_secret(author, key, payload)

    @router.post("/sources/{key}/disable")
    async def disable_source(
        key: Key,
        payload: Annotated[RevisionOnly, Depends(body_of(RevisionOnly))],
        service: Builder,
        author: Author,
    ):
        return await service.source_set_status(author, key, payload, "disabled")

    @router.post("/sources/{key}/enable")
    async def enable_source(
        key: Key,
        payload: Annotated[RevisionOnly, Depends(body_of(RevisionOnly))],
        service: Builder,
        author: Author,
    ):
        return await service.source_set_status(author, key, payload, "active")

    # --- spec and operations ---
    @router.post("/sources/{key}/spec/fetch")
    async def fetch_spec(
        key: Key,
        payload: Annotated[SpecFetchRequest, Depends(body_of(SpecFetchRequest, optional=True))],
        service: Builder,
        author: Author,
    ):
        return await service.spec_fetch(author, key, payload)

    @router.get("/sources/{key}/spec/{sha}/operations")
    async def spec_operations(
        key: Key,
        sha: Annotated[str, Path(pattern=r"^[a-f0-9]{64}$")],
        service: Builder,
        author: Author,
    ):
        return await service.spec_operations(author, key, sha)

    @router.post("/sources/{key}/operations/import", status_code=201)
    async def import_operations(
        key: Key,
        payload: Annotated[ImportRequest, Depends(body_of(ImportRequest))],
        service: Builder,
        author: Author,
    ):
        return await service.operations_import(author, key, payload)

    @router.get("/sources/{key}/operations")
    async def list_operations(key: Key, service: Builder, author: Author):
        return await service.operation_list(author, key)

    @router.patch("/sources/{key}/operations/{op_key}")
    async def update_operation(
        key: Key,
        op_key: Key,
        payload: Annotated[OperationPatch, Depends(body_of(OperationPatch))],
        service: Builder,
        author: Author,
    ):
        return await service.operation_update(author, key, op_key, payload)

    # --- tools ---
    @router.post("/tools", status_code=201)
    async def create_tool(
        payload: Annotated[ToolCreate, Depends(body_of(ToolCreate))],
        service: Builder,
        author: Author,
    ):
        return await service.tool_create(author, payload)

    @router.get("/tools")
    async def list_tools(service: Builder, author: Author, limit: Limit = 50, offset: Offset = 0):
        return await service.tool_list(author, limit, offset)

    @router.get("/tools/{identity}")
    async def get_tool(
        identity: UUID7,
        service: Builder,
        author: Author,
        version: Annotated[int | None, Query(ge=1)] = None,
    ):
        return await service.tool_get(author, identity, version)

    @router.post("/tools/{identity}/versions", status_code=201)
    async def update_tool(
        identity: UUID7,
        payload: Annotated[ToolUpdate, Depends(body_of(ToolUpdate))],
        service: Builder,
        author: Author,
    ):
        return await service.tool_update(author, identity, payload)

    @router.post("/tools/{identity}/versions/{version}/test")
    async def test_tool(
        identity: UUID7,
        version: Annotated[int, Path(ge=1)],
        payload: Annotated[ToolTestRequest, Depends(body_of(ToolTestRequest, optional=True))],
        service: Builder,
        author: Author,
    ):
        return await service.tool_test(author, identity, version, payload)

    @router.post("/tools/{identity}/versions/{version}/publish")
    async def publish_tool(
        identity: UUID7,
        version: Annotated[int, Path(ge=1)],
        payload: Annotated[ToolPublishRequest, Depends(body_of(ToolPublishRequest))],
        service: Builder,
        author: Author,
    ):
        return await service.tool_publish(author, identity, version, payload)

    for action, status in (("disable", "disabled"), ("retire", "retired")):

        def transition(status: str):
            async def run(
                identity: UUID7,
                version: Annotated[int, Path(ge=1)],
                payload: Annotated[RevisionRequest, Depends(body_of(RevisionRequest))],
                service: Builder,
                author: Author,
            ):
                return await service.tool_set_status(
                    author, identity, version, status, payload.expected_revision
                )

            return run

        router.add_api_route(
            f"/tools/{{identity}}/versions/{{version}}/{action}",
            transition(status),
            methods=["POST"],
            name=f"{action}_builder_tool",
        )

    @router.get("/runs/{run_id}")
    async def get_run(run_id: UUID7, service: Builder, author: Author):
        return await service.run_get(author, run_id)

    # --- toolkits ---
    @router.post("/toolkits", status_code=201)
    async def create_toolkit(
        payload: Annotated[ToolkitRequest, Depends(body_of(ToolkitRequest))],
        service: Builder,
        author: Author,
    ):
        if payload.toolkit_id is not None:
            raise BuilderInputError("use POST /toolkits/{id}/versions for a new version")
        return await service.toolkit_create(author, payload)

    @router.post("/toolkits/bind")
    async def bind_toolkits(
        payload: Annotated[BindRequest, Depends(body_of(BindRequest, optional=True))],
        service: Builder,
        author: Author,
    ):
        return await service.rebind(author, payload)

    @router.post("/toolkits/{identity}/versions", status_code=201)
    async def append_toolkit(
        identity: UUID7,
        payload: Annotated[ToolkitRequest, Depends(body_of(ToolkitRequest))],
        service: Builder,
        author: Author,
    ):
        if payload.toolkit_id != identity:
            raise BuilderInputError("toolkit_id must equal the path identity")
        return await service.toolkit_create(author, payload)

    @router.post("/toolkits/{identity}/versions/{version}/publish")
    async def publish_toolkit(
        identity: UUID7,
        version: Annotated[int, Path(ge=1)],
        payload: Annotated[ToolkitPublishRequest, Depends(body_of(ToolkitPublishRequest))],
        service: Builder,
        author: Author,
    ):
        return await service.toolkit_publish(author, identity, version, payload)

    return router


router = build_router("/mcp/builder")
product_router = build_router("/mcp/internal/tenants/{tenant_id}/builder")

__all__ = ["Digest", "product_router", "router"]
