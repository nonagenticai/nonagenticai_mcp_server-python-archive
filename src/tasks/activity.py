"""Run activity feed: the progress rows and the ``ai.nonagentic/fr`` metadata block (DB only).

The feed is advisory and append-only. A write is best effort — the caller must never fail a poll
or an event because the feed could not be written — and it is deduplicated against the newest
row, so a driver that sees the same phase on many ticks records it once.

``recent`` returns rows **newest-last** (ascending ``seq``): the newest entry is ``rows[-1]``.
That is the order an operator reads a log in, and it makes ``signals`` (which looks at recent
history) a plain scan. Everything on the wire is built here; ``src.tasks.mapping`` only wraps
the block under ``_meta``.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from src.config import Settings
from src.tasks.severity import severity_for, signals

#: Rows ``tasks/get`` carries: enough for a progress panel, never unbounded.
ACTIVITY_LIMIT = 50
#: Rows the SIGNALS look at. Deliberately wider than the wire: a stall or a waiting approval is
#: usually the oldest thing in the feed, and ACTIVITY_LIMIT busy-project rows hide it.
ACTIVITY_SIGNAL_WINDOW = 500
#: ``activity[].text`` cap (matches the column check in migration 0013).
TEXT_LIMIT = 512


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


RECORD_SQL = (
    "INSERT INTO public.mcp_run_activity(tenant_id,run_id,kind,text,severity,source_ref) "
    "SELECT %s,%s,%s,%s,%s,%s "
    "WHERE EXISTS(SELECT 1 FROM public.mcp_runs WHERE tenant_id=%s AND run_id=%s) "
    "AND NOT EXISTS(SELECT 1 FROM public.mcp_run_activity "
    "WHERE run_id=%s AND seq=(SELECT max(seq) FROM public.mcp_run_activity WHERE run_id=%s) "
    "AND kind=%s AND text=%s AND source_ref IS NOT DISTINCT FROM %s) "
    "RETURNING seq"
)

RECENT_SQL = (
    "SELECT seq,kind,text,severity,source_ref,created_at FROM ("
    "SELECT seq,kind,text,severity,source_ref,created_at FROM public.mcp_run_activity "
    "WHERE tenant_id=%s AND run_id=%s ORDER BY seq DESC LIMIT %s) newest ORDER BY seq"
)


async def record(
    pool,
    tenant: UUID,
    run_id: UUID,
    kind: str,
    text: str,
    severity: str | None = None,
    source_ref: str | None = None,
) -> int | None:
    """Append one activity row; returns its ``seq``, or None when it was not written.

    It is not written when the newest row of the run already has the same kind and text (a poll
    repeating a phase) or the run is unknown to this tenant. Concurrent writers may both pass the
    dedupe check; a repeated feed line is harmless, a missing one is not."""
    body = " ".join(str(text or "").split())[:TEXT_LIMIT] or kind
    level = severity or severity_for(kind)
    source = str(source_ref)[:200] if source_ref else None
    async with pool.connection() as conn:
        cur = await conn.execute(
            RECORD_SQL,
            (
                tenant,
                run_id,
                kind,
                body,
                level,
                source,
                tenant,
                run_id,
                run_id,
                run_id,
                kind,
                body,
                source,
            ),
        )
        row = await cur.fetchone()
    return row["seq"] if row else None


async def recent(pool, tenant: UUID, run_id: UUID, limit: int = ACTIVITY_LIMIT) -> list[Mapping]:
    """The newest ``limit`` rows of the run, NEWEST-LAST (ascending ``seq``)."""
    async with pool.connection() as conn:
        cur = await conn.execute(RECENT_SQL, (tenant, run_id, max(1, limit)))
        return await cur.fetchall()


def wire(rows: Sequence[Mapping]) -> list[dict]:
    """The contract's ``activity`` array: no tenant, no source ref, ISO-8601 ``at``."""
    return [
        {
            "seq": row["seq"],
            "at": _utc(row["created_at"]).isoformat(),
            "kind": row["kind"],
            "text": row["text"],
            "severity": row["severity"],
        }
        for row in rows
    ]


async def fr_meta(
    pool, tenant: UUID, row: Mapping, settings: Settings, now: datetime | None = None
) -> dict | None:
    """The ``ai.nonagentic/fr`` block of one run, or None when the run is not an fr project.

    ``project_id`` is always present (null until fr reports it): the client must be able to tell
    "not known yet" from "no such field"."""
    if row.get("runtime") != "fr_project":
        return None
    rows = await recent(pool, tenant, row["run_id"])
    # Signals are not bounded by the wire's window: an approval that has waited for hours is
    # exactly the one a busy project has pushed past ACTIVITY_LIMIT, and it must still signal.
    # Only a full first page can hide older rows, so the wider read is skipped otherwise.
    history = rows
    if len(rows) >= ACTIVITY_LIMIT:
        history = await recent(pool, tenant, row["run_id"], ACTIVITY_SIGNAL_WINDOW)
    reference = row.get("external_ref") or {}
    return {
        "project_id": reference.get("project_id"),
        "activity": wire(rows),
        "signals": signals(row, history, settings, now or datetime.now(UTC)),
    }
