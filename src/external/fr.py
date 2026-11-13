"""fr project adapter: the MCP server is one of fr's chat channels (spec section 3).

fr's ``mcp`` channel takes the same commands a Telegram user types, as the linked person whose
channel address the client named (the run's ``actor``). A project start parks an action approval
for that person; its outcome arrives as an fr event (``src.routers.fr_channel``), which records the
project id on the run's ``external_ref``. This adapter reads that reference and polls the project.

HTTP only. Never writes mcp_runs, never logs secrets or bodies.
"""

import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from src.config import Settings
from src.external.adapter import ExternalRef, ExternalStartError, ExternalState
from src.external.signing import sign

logger = logging.getLogger(__name__)

INBOUND_PATH = "/api/webhooks/mcp/incoming"
DEFAULT_START_CODE = "external_start_failed"
#: fr's inbound text limit (channels/mcp.ts MAX_TEXT).
TEXT_LIMIT = 4000
START_PASSTHROUGH = ("description", "problem_statement", "desired_outcome", "repository_url")
PHASE_LIMIT = 512
PROJECT_TYPE = re.compile(r"^[a-z0-9_-]{1,128}$")
#: Optional actor fields passed to fr next to ``address`` (see parse_actor).
ACTOR_CONTEXT = ("job", "on_behalf_of")
#: projects.status values (fr db/schema.ts). ``delivered`` is the closure: the deliverable exists.
CANCELLED_STATUSES = frozenset({"cancelled", "archived"})
FAILED_STATUSES = frozenset({"failed"})
#: What the events router records on ``external_ref`` when the start approval did not create one.
START_OUTCOMES = {
    "rejected": ExternalState("cancelled"),
    "expired": ExternalState("failed", code="project_start_expired"),
    "voided": ExternalState("cancelled"),
    "failed": ExternalState("failed", code="project_start_failed"),
}
#: The project fields a tool result carries (start_fr_project and fr_get_project alike).
PROJECT_KEYS = (
    "id",
    "name",
    "project_type",
    "status",
    "phase",
    "url",
    "delivered_at",
    "delivery_evidence",
    "deliverables",
)
#: ``projects.close`` answers with the closed project: the read fields plus how it closed.
CLOSED_KEYS = (*PROJECT_KEYS, "closed", "already_closed", "rm_pm_project_id")
#: The channel commands the catalog may pin to an fr_command tool. A target is fixed by the
#: catalog, so this is the whole write surface of a one-shot fr call.
FR_COMMANDS = frozenset({"projects.get", "projects.close"})
#: A project result is the run's result (jsonb, and the MCP payload): fr evidence is untrusted
#: in size, so a view is bounded here. Serialised UTF-8 bytes of the whole object.
PROJECT_VIEW_MAX_BYTES = 64 * 1024
#: Dropped in this order when a view is too large; the identity/status fields are never dropped.
DROPPABLE_KEYS = ("deliverables", "delivery_evidence")


def sanitize_code(value: object, default: str = DEFAULT_START_CODE) -> str:
    """Map an upstream error code into ``^[a-z_]{1,64}$``."""
    if not isinstance(value, str):
        return default
    code = re.sub(r"[^a-z_]", "_", value.lower())[:64]
    return code if code.strip("_") else default


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def project_view(project: Mapping, keys: tuple[str, ...] = PROJECT_KEYS) -> dict:
    """One fr project as a tool result: the public fields that are present, nothing else.

    Bounded to :data:`PROJECT_VIEW_MAX_BYTES` so evidence fr returns cannot blow the run's
    result (jsonb) or the task payload: ``deliverables`` then ``delivery_evidence`` are dropped
    whole, and as a last resort the remaining strings are truncated."""
    view = {key: project[key] for key in keys if project.get(key) is not None}
    return _bounded(view)


def close_view(project: Mapping) -> dict:
    """The ``projects.close`` answer: the project plus how it closed."""
    return project_view(project, CLOSED_KEYS)


def _encoded_size(value: Mapping) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode())


def _bounded(view: dict) -> dict:
    if _encoded_size(view) <= PROJECT_VIEW_MAX_BYTES:
        return view
    for key in DROPPABLE_KEYS:
        view.pop(key, None)
        if _encoded_size(view) <= PROJECT_VIEW_MAX_BYTES:
            return view
    # Last resort: no droppable field is left, so every string is cut to an equal share and
    # the share halves until the object fits. Non-strings here are only the fixed scalars.
    texts = {k: v for k, v in view.items() if isinstance(v, str)}
    other = {k: v for k, v in view.items() if not isinstance(v, str)}
    share = PROJECT_VIEW_MAX_BYTES // max(1, len(view))
    bounded = {**other, **{k: v[:share] for k, v in texts.items()}}
    while _encoded_size(bounded) > PROJECT_VIEW_MAX_BYTES and share > 64:
        share //= 2
        bounded = {**other, **{k: v[:share] for k, v in texts.items()}}
    return bounded


def elicitation_request(message: str, change_summary: object = None) -> dict:
    """The spec section 5 ``elicitation/create`` request for one fr approval."""
    text = message or ""
    if isinstance(change_summary, str) and change_summary:
        text = f"{text}\n\n{change_summary}"
    return {
        "method": "elicitation/create",
        "params": {
            "message": text,
            "requestedSchema": {
                "type": "object",
                "required": ["decision"],
                "properties": {
                    "decision": {"type": "string", "enum": ["approved", "rejected"]},
                    "reason": {"type": "string"},
                    "comment": {"type": "string"},
                },
            },
        },
    }


def decision_text(approval_id: str, content: Mapping) -> str | None:
    """fr's chat grammar for an answer: ``approve <id> [comment]`` / ``reject <id> <reason>``."""
    decision = content.get("decision")
    if decision == "approved":
        comment = _one_line(content.get("comment") or "")
        return f"approve {approval_id} {comment}".strip()
    if decision == "rejected":
        reason = _one_line(content.get("reason") or "")
        return f"reject {approval_id} {reason}" if reason else None
    return None


class FrRefusedError(Exception):
    """fr answered but did not do it (unknown sender, no action, error). ``code`` is public."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class FrAdapter:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self._client = client
        self._base = (settings.fr_base_url or "").rstrip("/")
        self._secret = settings.fr_channel_secret

    @property
    def configured(self) -> bool:
        return bool(self._base and self._secret and self._secret.get_secret_value())

    def _require(self) -> None:
        if not self.configured:
            raise RuntimeError("external_unconfigured")

    @staticmethod
    def _actor(value: object) -> Mapping | None:
        """A stored actor (the run's or an answer's) with an address, else None."""
        if isinstance(value, str | bytes):
            value = json.loads(value)
        if not isinstance(value, Mapping):
            return None
        address = value.get("address")
        return value if isinstance(address, str) and address else None

    @staticmethod
    def _project_type(row: Mapping) -> str:
        """The call's ``project_type`` input, else the tool target's default."""
        requested = (row.get("inputs") or {}).get("project_type")
        if requested is not None:
            if not isinstance(requested, str) or not PROJECT_TYPE.fullmatch(requested):
                raise ExternalStartError("invalid_project_type")
            return requested
        manifest = row["authority"]["manifest"]
        if isinstance(manifest, str | bytes):
            manifest = json.loads(manifest)
        return str(manifest["target"]["project_type"])

    @staticmethod
    def _in_tenant(row: Mapping, actor: Mapping) -> bool:
        """An agent member address names its tenant (``agent:<tenant_id>``): it must be the run's
        own (binding) tenant, never another tenant's and never the tool owner's. A platform-owned
        fr tool is callable by every agent tenant, so this is what keeps a client in tenant X from
        acting as tenant Y's agent member. Other address schemes are fr-resolved as before."""
        address = str(actor["address"])
        return not address.startswith("agent:") or address == f"agent:{row['tenant_id']}"

    async def _send(self, actor: Mapping, text: str, correlation: str) -> dict:
        """One signed channel message. Returns fr's dispatch body (``status`` is fr's outcome).

        Raises ``FrRefusedError`` for an answered refusal and ``httpx.HTTPStatusError`` for 401/400/5xx
        (the caller decides whether that is retryable)."""
        envelope = {"address": actor["address"], "text": text, "correlation_id": correlation}
        # Optional identity context (fr#1683): which agent job, on whose behalf. fr ignores
        # unknown keys until it reads them.
        envelope.update({k: actor[k] for k in ACTOR_CONTEXT if actor.get(k)})
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
        assert self._secret is not None
        headers = sign(body, self._secret.get_secret_value(), int(time.time()))
        headers["Content-Type"] = "application/json"
        response = await self._client.post(self._base + INBOUND_PATH, content=body, headers=headers)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            raise FrRefusedError("external_result_invalid") from None
        if not isinstance(data, dict):
            raise FrRefusedError("external_result_invalid")
        status = data.get("status")
        if status == "unknown-sender":
            raise FrRefusedError("actor_unresolved")
        if status in ("no-action", "error"):
            raise FrRefusedError(sanitize_code(data.get("code") or f"fr_{status}"))
        return data

    async def start(self, row: Mapping) -> ExternalRef:
        if not self.configured:
            raise ExternalStartError("external_unconfigured")
        actor = self._actor(row.get("actor"))
        if actor is None:
            raise ExternalStartError("actor_required")
        if not self._in_tenant(row, actor):
            raise ExternalStartError("actor_tenant_mismatch")
        inputs = row.get("inputs") or {}
        title = _one_line(inputs.get("title") or "")
        if not title:
            raise ExternalStartError("title_required")
        payload: dict[str, Any] = {"title": title, "project_type": self._project_type(row)}
        payload.update({k: inputs[k] for k in START_PASSTHROUGH if inputs.get(k)})
        text = "projects.create " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if len(text) > TEXT_LIMIT:
            raise ExternalStartError("inputs_too_large")
        try:
            data = await self._send(actor, text, str(row["run_id"]))
        except FrRefusedError as refused:
            raise ExternalStartError(refused.code) from None
        except httpx.HTTPStatusError as exc:
            # Answered before dispatch (bad signature, bad body, dispatch off): nothing started.
            logger.warning("fr start refused status=%s", exc.response.status_code)
            raise ExternalStartError(
                "fr_unavailable" if exc.response.status_code >= 500 else "fr_rejected"
            ) from None
        if data.get("status") == "pending-approval" and isinstance(data.get("approvalId"), str):
            return ExternalRef("fr", data["approvalId"])
        result = data.get("result")
        if data.get("status") == "result" and isinstance(result, Mapping):
            project = result.get("project_id")
            if isinstance(project, str) and project:
                # The approval policy let it run at once: the id is the project itself.
                return ExternalRef("fr", project, project_id=project)
        raise ExternalStartError(DEFAULT_START_CODE)

    async def poll(self, row: Mapping) -> ExternalState:
        self._require()
        if row["status"] == "cancelling":
            # fr has no customer cancel (owner ruling Q4): the task ends, the project goes on.
            return ExternalState("cancelled")
        ref = row.get("external_ref") or {}
        project = ref.get("project_id")
        if not project:
            outcome = ref.get("start_outcome")
            if outcome in START_OUTCOMES:
                return START_OUTCOMES[outcome]
            return ExternalState("running", message="waiting for the project start to be approved")
        query = json.dumps({"id": project}, separators=(",", ":"))
        try:
            data = await self._send(
                self._actor(row.get("actor")) or {"address": ""},
                f"projects.get {query}",
                f"{row['run_id']}:poll",
            )
        except FrRefusedError as refused:
            # A read refusal (link removed, membership lost) is not the project's outcome: retry.
            raise RuntimeError(refused.code) from None
        project_state = data.get("result")
        if not isinstance(project_state, Mapping):
            raise RuntimeError("external_result_invalid")
        status = project_state.get("status")
        if status == "delivered":
            return ExternalState("succeeded", result=project_view(project_state))
        if status in CANCELLED_STATUSES:
            return ExternalState("cancelled")
        if status in FAILED_STATUSES:
            return ExternalState("failed", code="project_failed")
        phase = project_state.get("phase") or status
        return ExternalState("running", message=str(phase)[:PHASE_LIMIT] if phase else None)

    async def cancel(self, row: Mapping) -> None:
        """Best effort: withdraw a start that is still waiting for approval. A running project
        cannot be cancelled from a channel; the task is cancelled, the project is not."""
        self._require()
        ref = row.get("external_ref") or {}
        actor = self._actor(row.get("actor"))
        if ref.get("project_id") or not ref.get("id") or actor is None:
            return
        try:
            await self._send(
                actor, f"reject {ref['id']} cancelled by the client", f"{row['run_id']}:cancel"
            )
        except FrRefusedError as refused:
            logger.info("fr start withdrawal refused: %s", refused.code)

    async def forward(self, row: Mapping, item: Mapping) -> str | None:
        """Relay one human answer as that human. Returns a refusal code to show the client, or
        None once fr took it (or there was nothing to send)."""
        self._require()
        response = item.get("response") or {}
        if response.get("action") != "accept":
            return None  # declined/cancelled here: the approval stays open in fr's other channels
        actor = self._actor(item.get("actor"))
        text = decision_text(str(item["source_ref"]), response.get("content") or {})
        if actor is None or text is None:
            return None
        if not self._in_tenant(row, actor):
            return "actor_tenant_mismatch"
        try:
            await self._send(actor, text, f"{row['run_id']}:{item['input_key']}")
        except FrRefusedError as refused:
            logger.info("fr refused an answer: %s", refused.code)
            return refused.code
        return None


class FrCommandAdapter(FrAdapter):
    """A one-shot fr channel command: ``projects.get`` / ``projects.close`` answer at once, so
    there is nothing to poll and nothing to withdraw. The answer is kept on the run's reference,
    which is what makes a crash between the send and the status write harmless: ``poll`` replays
    it instead of dispatching the command a second time.

    The command comes from the tool's catalog target, never from a call: a target is a fixed
    command and the only input is the project id."""

    async def start(self, row: Mapping) -> ExternalRef:
        self._require()
        actor = self._actor(row.get("actor"))
        if actor is None:
            raise ExternalStartError("actor_required")
        if not self._in_tenant(row, actor):
            raise ExternalStartError("actor_tenant_mismatch")
        manifest = row["authority"]["manifest"]
        if isinstance(manifest, str | bytes):
            manifest = json.loads(manifest)
        command = manifest["target"]["command"]
        if command not in FR_COMMANDS:
            raise ExternalStartError("unsupported_command")
        project_id = (row.get("inputs") or {}).get("id")
        if not isinstance(project_id, str) or not project_id:
            raise ExternalStartError("invalid_project_id")
        payload = {"id": project_id}
        text = f"{command} " + json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(text) > TEXT_LIMIT:
            raise ExternalStartError("inputs_too_large")
        try:
            data = await self._send(actor, text, str(row["run_id"]))
        except FrRefusedError as refused:
            raise ExternalStartError(refused.code) from None
        except httpx.HTTPStatusError as exc:
            logger.warning("fr command refused status=%s", exc.response.status_code)
            raise ExternalStartError(
                "fr_unavailable" if exc.response.status_code >= 500 else "fr_rejected"
            ) from None
        state = data.get("result")
        if data.get("status") != "result" or not isinstance(state, Mapping):
            raise ExternalStartError("external_result_invalid")
        answer = close_view(state) if command == "projects.close" else project_view(state)
        return ExternalRef("fr_command", project_id, project_id=project_id, result=answer)

    async def poll(self, row: Mapping) -> ExternalState:
        result = (row.get("external_ref") or {}).get("result")
        if isinstance(result, Mapping):
            return ExternalState("succeeded", result=dict(result))
        return await super().poll(row)

    async def cancel(self, row: Mapping) -> None:
        # A read has nothing to withdraw: the task ends, the answer (if any) stays on the ref.
        return None
