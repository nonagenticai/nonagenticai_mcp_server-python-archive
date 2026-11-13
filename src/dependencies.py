"""Internal dependency for future explicitly authenticated management routes."""

from fastapi import HTTPException, Request

from src.auth.principal import AuthorPrincipal, Principal
from src.builder.service import BuilderService
from src.catalog.service import CatalogService
from src.toolkits.service import AccessService


def get_catalog(request: Request) -> CatalogService:
    return request.app.state.catalog


def get_access(request: Request) -> AccessService:
    return request.app.state.access


def get_operator(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal) or not principal.management:
        raise HTTPException(status_code=403, detail="management authorization required")
    return principal


def get_author(request: Request) -> AuthorPrincipal:
    """The tenant author the builder middleware authenticated (never an operator principal)."""
    author = request.scope.get("state", {}).get("author")
    if not isinstance(author, AuthorPrincipal):
        raise HTTPException(status_code=403, detail="builder authorization required")
    return author


def get_builder(request: Request) -> BuilderService:
    service = getattr(request.app.state, "builder", None)
    if service is None:
        raise HTTPException(status_code=404, detail="not_found")
    return service
