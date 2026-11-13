"""Pure mapping from a ``mcp_runs`` row (plus its inputs) to the task wire shape. No I/O."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.config import Settings
from src.execution.models import envelope
from src.tasks.models import FR_META_KEY, TaskStatus

_WORKING = frozenset({"queued", "starting", "running"})
_FAILURES = frozenset({"failed", "timed_out", "interrupted"})
_MS_PER_DAY = 86_400_000


def task_status(row: Mapping, open_inputs: int) -> TaskStatus:
    status = row["status"]
    if status in _WORKING:
        return "input_required" if open_inputs > 0 else "working"
    if status == "cancelling":
        return "working"  # a pending cancel wins over open inputs
    if status == "cancelled":
        return "cancelled"
    return "completed"  # succeeded and every failure kind (failed is JSON-RPC-only)


def task_result(row: Mapping) -> dict:
    body = envelope(dict(row))
    return {
        "content": [
            {"type": "text", "text": json.dumps(body, ensure_ascii=False, allow_nan=False)}
        ],
        "structuredContent": body,
        "isError": row["status"] in _FAILURES,
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _open(inputs: Sequence[Mapping], now: datetime | None = None) -> list[Mapping]:
    """Open inputs that have not expired; an expired approval is not a question any more."""
    moment = now or datetime.now(UTC)
    opened = []
    for item in inputs:
        if item.get("state") != "open":
            continue
        expires = item.get("expires_at")
        if expires is not None and _utc(expires) <= moment:
            continue
        opened.append(item)
    return opened


def _last_updated(row: Mapping, inputs: Sequence[Mapping]) -> datetime:
    stamps = [row.get("finished_at"), row.get("started_at"), row["created_at"]]
    for item in inputs:
        stamps += [item.get("created_at"), item.get("answered_at")]
    return max(_utc(stamp) for stamp in stamps if stamp is not None)


def _poll_interval_ms(row: Mapping, settings: Settings) -> int:
    if row.get("runtime", "kata_python") == "kata_python":
        return settings.task_poll_interval_ms
    return int(settings.external_poll_seconds * 1000)


def _task_fields(row: Mapping, inputs: Sequence[Mapping], settings: Settings) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "taskId": str(row["run_id"]),
        "status": task_status(row, len(_open(inputs))),
        "createdAt": _utc(row["created_at"]).isoformat(),
        "lastUpdatedAt": _last_updated(row, inputs).isoformat(),
        "ttlMs": settings.execution_retention_days * _MS_PER_DAY,
        "pollIntervalMs": _poll_interval_ms(row, settings),
    }
    if row.get("status_message"):
        fields["statusMessage"] = row["status_message"]
    return fields


def _with_meta(fields: dict[str, Any], fr_meta: Mapping | None) -> dict:
    """The fr block rides under ``_meta``: rmcp drops unknown TOP-LEVEL task fields."""
    return fields if fr_meta is None else {**fields, "_meta": {FR_META_KEY: dict(fr_meta)}}


def task_view(
    row: Mapping,
    inputs: Sequence[Mapping],
    settings: Settings,
    fr_meta: Mapping | None = None,
) -> dict:
    fields = _with_meta(_task_fields(row, inputs, settings), fr_meta)
    view = {**fields, "resultType": "complete"}
    if view["status"] == "input_required":
        view["inputRequests"] = {item["input_key"]: item["request"] for item in _open(inputs)}
    elif view["status"] == "completed":
        view["result"] = task_result(row)
    return view


def create_task_result(row: Mapping, settings: Settings, fr_meta: Mapping | None = None) -> dict:
    return {"resultType": "task", **_with_meta(_task_fields(row, [], settings), fr_meta)}
