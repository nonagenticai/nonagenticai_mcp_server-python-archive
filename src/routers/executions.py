"""Binding-owned durable execution routes; operators may inspect/cancel tenant runs."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import UUID7

from src.auth.context import current_authority
from src.catalog.submission import SubmissionError
from src.execution.models import QueueFullError, RunRequest, envelope
from src.routers.bindings import json_contract

router = APIRouter(prefix="/mcp/runs")


@router.post("", status_code=202)
async def submit(request: Request):
    if request.state.principal.management:
        raise PermissionError("submission requires binding authority")
    payload = await json_contract(RunRequest)(request)
    state = request.app.state
    tool = next(
        (
            t
            for t in await state.catalog_resolver.resolve(request.state.principal)
            if t.manifest.public_name == payload.tool
        ),
        None,
    )
    if tool is None:
        raise PermissionError("tool unavailable")
    try:
        return await state.submission.submit(
            request.state.principal, tool, payload.inputs, current_authority().idempotency_key
        )
    except QueueFullError:
        raise HTTPException(429, "execution_queue_full", headers={"Retry-After": "1"}) from None
    except SubmissionError:
        raise HTTPException(503, "execution_unavailable") from None


@router.get("/{identity}")
async def inspect(identity: UUID7, request: Request):
    row = await request.app.state.execution.inspect(request.state.principal, identity)
    return envelope(row)


@router.get("/{identity}/events")
async def events(
    identity: UUID7,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    service = request.app.state.execution
    row = await service.inspect(request.state.principal, identity)
    return {"items": await service.repository.events(row["tenant_id"], identity, after, limit)}


@router.post("/{identity}/cancel")
async def cancel(identity: UUID7, request: Request):
    service = request.app.state.execution
    row = await service.inspect(request.state.principal, identity)
    await service.repository.cancel(row["tenant_id"], identity)
    # Workers transition active runs after confirming cleanup.
    return {**envelope(row), "cancel_requested": True}
