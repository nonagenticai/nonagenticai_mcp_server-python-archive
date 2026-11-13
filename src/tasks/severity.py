"""Severity mapping and stall/wait signals for the fr run activity feed. Pure: no I/O.

``tasks/get`` carries the feed under ``_meta["ai.nonagentic/fr"]``; every activity entry and
every signal names one severity from :data:`SEVERITIES`. The two thresholds are settings, so a
signal is derived at read time from the rows and the clock — nothing is stored for it.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from src.config import Settings

SEVERITIES = ("info", "low", "medium", "high", "critical")
#: Activity kind and signal code -> severity. A kind the platform does not know is ``info``:
#: a new feed entry must never be presented as alarming by an older server.
SEVERITY = {
    "started": "low",
    "phase": "low",
    "approval_requested": "high",
    "approval_closed": "info",
    "refused": "medium",
    "failed": "critical",
    "delivered": "info",
    "approval_waiting": "high",
    "phase_stalled": "medium",
    "project_failed": "critical",
    "start_refused": "critical",
}
DEFAULT_SEVERITY = "info"
#: ``signals[].detail`` on the wire.
DETAIL_LIMIT = 200
#: Run statuses whose work may still move; wait/stall signals make no sense when it cannot.
ACTIVE_STATUSES = frozenset({"queued", "starting", "running", "cancelling"})
#: Terminal failure kinds (``src.execution.models.TERMINAL``). ``interrupted`` is a terminal
#: failure whose outcome is unknown — the most ambiguous one, so it must never be the silent
#: one: without it a failed run showed ``isError`` on the wire and no critical signal.
FAILED_STATUSES = frozenset({"failed", "timed_out", "interrupted"})
#: ``external_ref.start_outcome`` values that mean fr never created the project.
START_REFUSED_OUTCOMES = frozenset({"rejected", "voided", "expired"})
#: Kinds that report progress of the project itself, for ``phase_stalled``.
PROGRESS_KINDS = ("phase", "started")


def severity_for(kind: str) -> str:
    """The severity of one activity kind or signal code."""
    return SEVERITY.get(kind, DEFAULT_SEVERITY)


def _moment(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _detail(text: object) -> str:
    return " ".join(str(text).split())[:DETAIL_LIMIT] or "signal"


def _signal(code: str, detail: object) -> dict:
    return {"code": code, "severity": severity_for(code), "detail": _detail(detail)}


def _age_seconds(now: datetime, row: Mapping) -> float | None:
    at = _moment(row.get("created_at"))
    return None if at is None else (now - at).total_seconds()


def _minutes(seconds: float) -> str:
    return f"{max(0, int(seconds // 60))} min"


def _waiting_approval(activity: Sequence[Mapping]) -> Mapping | None:
    """The newest ``approval_requested`` that no ``approval_closed`` for the same ref followed."""
    closed = {row.get("source_ref") for row in activity if row.get("kind") == "approval_closed"}
    for row in reversed(activity):
        if row.get("kind") == "approval_requested" and row.get("source_ref") not in closed:
            return row
    return None


def _last_progress(activity: Sequence[Mapping]) -> Mapping | None:
    for row in reversed(activity):
        if row.get("kind") in PROGRESS_KINDS:
            return row
    return None


def signals(
    row: Mapping,
    activity: Sequence[Mapping],
    settings: Settings,
    now: datetime | None = None,
) -> list[dict]:
    """The advisory signals for one run, in a stable order (most severe first).

    ``activity`` are ``mcp_run_activity`` rows (``created_at`` datetimes, newest last);
    ``now`` is the read time, so tests and a clock-skewed poller see the same rule."""
    moment = now or datetime.now(UTC)
    status = row.get("status")
    reference = row.get("external_ref") or {}
    found: list[dict] = []
    if status in FAILED_STATUSES:
        error = row.get("error") or {}
        found.append(
            _signal(
                "project_failed",
                error.get("code") or row.get("status_message") or "the run failed",
            )
        )
    outcome = reference.get("start_outcome")
    if outcome in START_REFUSED_OUTCOMES:
        found.append(_signal("start_refused", f"the project start was {outcome}"))
    if status in ACTIVE_STATUSES:
        waiting = _waiting_approval(activity)
        if waiting is not None:
            age = _age_seconds(moment, waiting)
            if age is not None and age >= settings.fr_approval_wait_seconds:
                found.append(_signal("approval_waiting", f"an approval has waited {_minutes(age)}"))
        progress = _last_progress(activity)
        if progress is not None:
            age = _age_seconds(moment, progress)
            if age is not None and age >= settings.fr_phase_stall_seconds:
                found.append(_signal("phase_stalled", f"no progress for {_minutes(age)}"))
    return found
