"""Process-wide access to the BuilderService for the builtin builder_* tools.

The FastMCP tool functions have no request object, so lifespan installs the service here (and on
``app.state.builder`` for REST). One API process serves one application: ``uninstall`` clears it
on shutdown.
"""

from __future__ import annotations

from typing import Any

from src.builder.service import BuilderService, BuilderUnavailableError

_service: BuilderService | None = None


def build(state: Any, *, fetcher: Any = None) -> BuilderService:
    """Compose the service from the lifespan's ``app.state`` (everything is already built)."""
    from src.sources.registry import SourceRegistry
    from src.sources.repository import TenantSourceRepository
    from src.storage.builder import BuilderCatalogRepository

    pool = state.database.pool
    settings = state.settings
    return BuilderService(
        settings=settings,
        pool=pool,
        sources=TenantSourceRepository.from_settings(pool, settings),
        builder=BuilderCatalogRepository(pool),
        catalog=state.catalog,
        access=state.access,
        execution=getattr(state, "execution", None),
        fetcher=fetcher,
        registry=SourceRegistry(pool, settings) if fetcher is not None else None,
        ensure=getattr(state, "binding_ensure", None),
    )


def install(state: Any, *, fetcher: Any = None) -> BuilderService | None:
    """Install the service when the builder is enabled; None (and nothing installed) otherwise."""
    global _service
    if not state.settings.builder_enabled:
        _service = None
        state.builder = None
        return None
    _service = build(state, fetcher=fetcher)
    state.builder = _service
    return _service


def uninstall(state: Any | None = None) -> None:
    global _service
    _service = None
    if state is not None:
        state.builder = None


def get() -> BuilderService:
    if _service is None:
        raise BuilderUnavailableError("the tool builder is not enabled")
    return _service
