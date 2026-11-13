"""Service-to-service routes under /mcp/internal (Keycloak service JWT, azp allow-list).

POST /mcp/internal/tenants/{tenant_id}/bindings/ensure provisions the one binding of a product
for a tenant (src.storage.bindings_ensure). Product provisioning calls it on approval with the
tenant's mcp licence (id and token) and stores the returned binding token; the token is in the
response only when it was issued.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from src.auth.licenses import AuthorityUnavailableError
from src.auth.principal import ServicePrincipal
from src.catalog.models import AuthorityId, Contract
from src.config import PRODUCT_PATTERN
from src.storage.bindings_ensure import BindingEnsureService

router = APIRouter(prefix="/mcp/internal")


class EnsureRequest(Contract):
    product: Annotated[str, Field(pattern=PRODUCT_PATTERN)]
    #: The tenant's mcp licence, held by the calling product: its id and its token. The token is
    #: introspected (never stored or logged) and must be that tenant-level licence.
    licence_id: AuthorityId
    licence_token: Annotated[SecretStr, Field(min_length=1, max_length=4096)]
    rotate: bool = False


def get_service(request: Request) -> ServicePrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, ServicePrincipal):
        raise HTTPException(status_code=403, detail="service authorization required")
    return principal


def get_ensure(request: Request) -> BindingEnsureService:
    service = getattr(request.app.state, "binding_ensure", None)
    if service is None:
        raise HTTPException(status_code=503, detail="binding provisioning unavailable")
    return service


async def ensure_request(request: Request) -> EnsureRequest:
    return EnsureRequest.model_validate_json(await request.body())


@router.post("/tenants/{tenant_id}/bindings/ensure")
async def ensure_binding(
    tenant_id: UUID,
    payload: Annotated[EnsureRequest, Depends(ensure_request)],
    principal: Annotated[ServicePrincipal, Depends(get_service)],
    service: Annotated[BindingEnsureService, Depends(get_ensure)],
):
    if tenant_id.version is None:
        raise ValueError("invalid tenant identity")
    try:
        result = await service.ensure(
            tenant_id,
            payload.product,
            principal.azp,
            licence_id=payload.licence_id,
            licence_token=payload.licence_token.get_secret_value(),
            rotate=payload.rotate,
        )
    except AuthorityUnavailableError:
        return JSONResponse({"error": "authority_unavailable"}, status_code=503)
    return JSONResponse(result, status_code=201 if "token" in result else 200)
