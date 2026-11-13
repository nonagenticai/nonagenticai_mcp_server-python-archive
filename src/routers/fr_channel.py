"""fr → MCP channel events . HMAC-authenticated only.

fr's outbound ``mcp`` adapter posts every event to one ``address`` (a linked person). Approvals
fan out to every linked member of the workspace, so one approval arrives once per address; it is
routed to runs by what it names, never by the address:

- ``correlation_id`` (a run id) when fr has one — n8n questions carry the n8n start's run id;
- else ``project_id`` → the fr run whose ``external_ref.project_id`` it is;
- else the approval id → the fr run whose start parked that approval (``external_ref.id``).

``message`` events are chat replies, correlated by the run id this server sent: the parked start
(``approval-request``) and its outcome (``action-result`` / ``action-error``). The outcome is
recorded on ``external_ref`` (it only grows); the driver reads it on its next poll. fr also echoes
every command WE dispatch back as a ``message`` whose correlation_id is ``"<run>:<poll|input>"``,
never a run id: those are dropped before any database work.

Approvals the run was asked about, and their closure, also become activity rows — the feed the
platform shows under ``tasks/get`` ``_meta["ai.nonagentic/fr"]`` (``src.tasks.activity``).
"""

import json
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from src.external.fr import PHASE_LIMIT, elicitation_request
from src.external.signing import verify
from src.tasks.activity import TEXT_LIMIT
from src.tasks.activity import record as record_activity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels/fr")

MAX_BODY_BYTES = 65536
TERMINAL_STATUSES = ["succeeded", "failed", "cancelled", "timed_out", "interrupted"]
EVENT_TYPES = frozenset({"approval.requested", "approval.closed", "message"})
START_ACTION = "projects.create"
#: approval.closed final statuses that end a parked start without a project.
START_ENDINGS = frozenset({"rejected", "expired", "voided"})
NO_CONTENT = 204
LIVE = "status <> ALL(%s) AND runtime <> 'kata_python'"
#: One activity row per approval the run was asked about; the description is the feed text.
APPROVAL_TEXT = "an approval was requested"


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _expires_at(value: object) -> datetime | None:
    """The event's ``expires_at`` as an aware UTC datetime, or None when absent/unparsable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _expired(value: object, now: datetime | None = None) -> bool:
    at = _expires_at(value)
    return at is not None and at <= (now or datetime.now(UTC))


async def _activity(
    pool, run: Mapping, kind: str, text: str, source_ref: str | None = None
) -> None:
    """One feed row for ``tasks/get``; a feed failure never fails the event.

    The approval path fans out to every linked run, so a 500 here would abort the remaining
    runs' inputs and fr (which does not retry) would lose them."""
    try:
        await record_activity(
            pool, run["tenant_id"], run["run_id"], kind, text, source_ref=source_ref
        )
    except Exception as exc:
        logger.warning("activity record failed for %s: %s", run["run_id"], type(exc).__name__)


async def _read_capped(request: Request) -> bytes | None:
    """The raw body, or None once it exceeds the cap (Content-Length is not trusted)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _runs(pool, event: Mapping, approval: str) -> list[dict]:
    """The live runs an approval event is about (see module docstring)."""
    run_id = _uuid(event.get("correlation_id"))
    project = _text(event.get("project_id"))
    if run_id is not None:
        where, args = "run_id=%s", (run_id,)
    elif project is not None:
        where, args = "runtime='fr_project' AND external_ref->>'project_id'=%s", (project,)
    else:
        where, args = "runtime='fr_project' AND external_ref->>'id'=%s", (approval,)
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT run_id,tenant_id,external_ref FROM public.mcp_runs WHERE {where} AND {LIVE}",  # nosec B608
            (*args, TERMINAL_STATUSES),
        )
        return await cur.fetchall()


async def _correlated(pool, event: Mapping) -> dict | None:
    run_id = _uuid(event.get("correlation_id"))
    if run_id is None:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT run_id,tenant_id,runtime,external_ref FROM public.mcp_runs WHERE run_id=%s AND {LIVE}",  # nosec B608
            (run_id, TERMINAL_STATUSES),
        )
        return await cur.fetchone()


async def _record(pool, run: Mapping, facts: dict) -> None:
    """Add what fr reported about the start to ``external_ref``; the first report wins."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE public.mcp_runs SET external_ref=COALESCE(external_ref,'{}'::jsonb)||%s "
            "WHERE tenant_id=%s AND run_id=%s AND runtime='fr_project' AND status <> ALL(%s) "
            "AND NOT (COALESCE(external_ref,'{}'::jsonb) ?| ARRAY['project_id','start_outcome'])",
            (Jsonb({"kind": "fr", **facts}), run["tenant_id"], run["run_id"], TERMINAL_STATUSES),
        )


async def _approval(request: Request, kind: str, event: Mapping) -> None:
    approval = _text(event.get("approval_id"))
    if approval is None:
        logger.info("fr channel event type=%s ignored reason=no_approval_id", kind)
        return
    if kind == "approval.requested" and _expired(event.get("expires_at")):
        # fr's approval window already closed: opening an input would ask an unanswerable
        # question (fr would refuse the answer) and the feed would claim one is waiting.
        logger.info(
            "fr channel event type=%s approval=%s ignored reason=expired",
            kind,
            approval[:64],
        )
        return
    pool, inputs = request.app.state.database.pool, request.app.state.task_inputs
    runs = await _runs(pool, event, approval)
    # Ids and counts only (never text): enough to tell "not sent" from "sent but unmatched".
    logger.info(
        "fr channel event type=%s approval=%s final=%s runs=%d",
        kind,
        approval[:64],
        event.get("final_status") if isinstance(event.get("final_status"), str) else "-",
        len(runs),
    )
    if kind == "approval.requested":
        prompt = elicitation_request(
            str(event.get("description") or ""), event.get("change_summary")
        )
        project = _text(event.get("project_id"))
        description = (_text(event.get("description")) or APPROVAL_TEXT)[:TEXT_LIMIT]
        for run in runs:
            await inputs.open(
                run["tenant_id"],
                run["run_id"],
                approval,
                prompt,
                expires_at=_expires_at(event.get("expires_at")),
            )
            # fr names the project on an approval raised inside it: a run started before that
            # (its start approval carried no project) adopts the id here, so its poll can read
            # the project instead of waiting forever for the start outcome.
            if project and not (run["external_ref"] or {}).get("project_id"):
                await _record(pool, run, {"project_id": project})
            await _activity(pool, run, "approval_requested", description, source_ref=approval)
        return
    await inputs.close_everywhere(approval)
    final = event.get("final_status")
    closed_text = (
        f"approval closed: {final}" if isinstance(final, str) and final else "approval closed"
    )
    for run in runs:
        # A tombstone, so a late approval.requested for this run is ignored.
        await inputs.close(run["tenant_id"], run["run_id"], approval)
        ref = run["external_ref"] or {}
        if final in START_ENDINGS and ref.get("id") == approval and not ref.get("project_id"):
            await _record(pool, run, {"start_outcome": final})
        await _activity(pool, run, "approval_closed", closed_text[:TEXT_LIMIT], source_ref=approval)


async def _message(request: Request, event: Mapping) -> None:
    if _uuid(event.get("correlation_id")) is None:
        # fr echoes every channel command it dispatches back as a ``message`` event whose
        # correlation_id is "<run id>:<poll|input key>", not a run id: our own poll, replayed.
        # Dropped before any database work — live this was >12k events in 72 h, all self-echo.
        logger.debug("fr channel message ignored reason=non_uuid_correlation")
        return
    pool = request.app.state.database.pool
    run = await _correlated(pool, event)
    kind_name = _text(event.get("kind")) or "?"
    logger.info("fr channel event type=message kind=%s run=%s", kind_name[:32], bool(run))
    if run is None:
        return
    meta = _mapping(event.get("meta"))
    kind = event.get("kind")
    text = _text(event.get("text"))
    if kind == "approval-request":
        approval = _text(meta.get("approvalId"))
        if approval and text:
            await request.app.state.task_inputs.open(
                run["tenant_id"], run["run_id"], approval, elicitation_request(text)
            )
        return
    if meta.get("actionName") == START_ACTION and run["runtime"] == "fr_project":
        result = _mapping(meta.get("result"))
        project = _text(result.get("project_id"))
        if kind == "action-result" and meta.get("outcome") == "approved" and project:
            await _record(pool, run, {"project_id": project})
        elif kind == "action-result" and meta.get("outcome") == "rejected":
            await _record(pool, run, {"start_outcome": "rejected"})
        elif kind == "action-error":
            await _record(pool, run, {"start_outcome": "failed"})
        return
    if kind in ("text", "report") and text:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE public.mcp_runs SET status_message=%s "
                "WHERE tenant_id=%s AND run_id=%s AND status <> ALL(%s)",
                (text[:PHASE_LIMIT], run["tenant_id"], run["run_id"], TERMINAL_STATUSES),
            )


@router.post("/events")
async def events(request: Request) -> Response:
    body = await _read_capped(request)
    if body is None:
        return JSONResponse({"error": "payload_too_large"}, status_code=413)
    secret = request.app.state.settings.fr_channel_secret
    if secret is None:
        return JSONResponse({"error": "channel_unconfigured"}, status_code=503)
    if not verify(body, request.headers, secret.get_secret_value(), int(time.time())):
        return JSONResponse({"error": "invalid_signature"}, status_code=401)

    try:
        event = json.loads(body)
    except ValueError:
        return Response(status_code=NO_CONTENT)
    kind = event.get("type") if isinstance(event, dict) else None
    if kind not in EVENT_TYPES:
        logger.info("fr channel event ignored type=%s", kind if isinstance(kind, str) else "?")
        return Response(status_code=NO_CONTENT)
    if kind == "message":
        await _message(request, event)
    else:
        await _approval(request, kind, event)
    return Response(status_code=NO_CONTENT)
