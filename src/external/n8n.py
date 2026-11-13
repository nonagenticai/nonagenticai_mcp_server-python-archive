"""n8n workflow adapter (spec §7). HTTP only; the driver owns every mcp_runs write.

Never logs the API key or upstream bodies, and never copies upstream text into
``ExternalState.message``.
"""

import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from src.config import Settings
from src.external.adapter import ExternalRef, ExternalStartError, ExternalState

RESULT_LIMIT_BYTES = 64 * 1024
BODY_LIMIT_BYTES = 4 * 1024 * 1024
DEFAULT_RESULT_NODE = "MCP Result"
WAITING_MESSAGE = "waiting for approval in fr"
_RUNNING = frozenset({"new", "running", "waiting"})


def _target(row: Mapping) -> dict:
    manifest = row["authority"]["manifest"]
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    return manifest["target"]


#: Lifetime of the webhook start JWT. n8n stores every request header in the execution record,
#: so only a short-lived signed token may travel there, never the shared secret itself.
WEBHOOK_TOKEN_TTL_SECONDS = 120


def webhook_token(secret: str, run_id: str, now: float | None = None) -> str:
    """HS256 JWT checked by the n8n Webhook node's JWT Auth credential (same secret)."""
    issued = int(time.time() if now is None else now)
    claims = {"sub": run_id, "iat": issued, "exp": issued + WEBHOOK_TOKEN_TTL_SECONDS}
    return jwt.encode(claims, secret, algorithm="HS256")


class N8nAdapter:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self._client = client
        self._settings = settings
        self._unknown: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        key = self._settings.n8n_api_key
        assert key is not None
        return {"X-N8N-API-KEY": key.get_secret_value()}

    def _api_configured(self) -> bool:
        return bool(self._settings.n8n_base_url and self._settings.n8n_api_key)

    def _execution_url(self, row: Mapping, suffix: str = "") -> str:
        base = str(self._settings.n8n_base_url).rstrip("/")
        return f"{base}/api/v1/executions/{quote(row['external_ref']['id'], safe='')}{suffix}"

    async def start(self, row: Mapping) -> ExternalRef:
        if not self._settings.n8n_webhook_base_url:
            raise ExternalStartError("external_unconfigured")
        path = _target(row)["webhook_path"].lstrip("/")
        url = f"{str(self._settings.n8n_webhook_base_url).rstrip('/')}/webhook/{path}"
        body = {
            "mcp_run_id": str(row["run_id"]),
            "tenant_id": str(row["tenant_id"]),
            "inputs": row["inputs"],
        }
        secret = self._settings.n8n_webhook_secret
        headers = (
            {
                "Authorization": f"Bearer {webhook_token(secret.get_secret_value(), body['mcp_run_id'])}"
            }
            if secret
            else {}
        )
        try:
            response = await self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise ExternalStartError("external_start_failed") from error
        if not response.is_success:
            raise ExternalStartError("external_start_failed")
        try:
            payload = response.json()
        except ValueError as error:
            raise ExternalStartError("external_start_failed") from error
        execution_id = payload.get("executionId") if isinstance(payload, dict) else None
        if not isinstance(execution_id, str) or not execution_id:
            raise ExternalStartError("external_start_failed")
        return ExternalRef("n8n", execution_id)

    async def poll(self, row: Mapping) -> ExternalState:
        if not self._api_configured():
            return ExternalState("failed", code="external_unconfigured")
        run_key = str(row["run_id"])
        async with self._client.stream(
            "GET",
            self._execution_url(row),
            # n8n 2.46.1 drops `data` above EXECUTIONS_DATA_MAX_DISPLAY_SIZE unless asked; the
            # body cap below still bounds what we read.
            params={"includeData": "true", "ignoreDataSizeLimit": "true"},
            headers=self._headers(),
        ) as response:
            if response.status_code == 404:
                self._unknown.pop(run_key, None)
                return ExternalState("failed", code="external_missing")
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > BODY_LIMIT_BYTES:
                    self._unknown.pop(run_key, None)
                    return ExternalState("failed", code="external_result_invalid")
                chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
        except ValueError:
            return ExternalState("failed", code="external_result_invalid")
        status = payload.get("status") if isinstance(payload, dict) else None

        if status == "unknown":
            count = self._unknown.get(run_key, 0) + 1
            if count >= self._settings.n8n_unknown_status_limit:
                self._unknown.pop(run_key, None)
                return ExternalState("failed", code="external_status_unknown")
            self._unknown[run_key] = count
            return ExternalState("running")
        self._unknown.pop(run_key, None)

        if status in _RUNNING:
            return ExternalState(
                "running", message=WAITING_MESSAGE if status == "waiting" else None
            )
        if status == "success":
            return self._succeeded(row, payload)
        if status == "canceled":
            return ExternalState("cancelled")
        return ExternalState("failed", code="external_failed")

    @staticmethod
    def _succeeded(row: Mapping, payload: Mapping[str, Any]) -> ExternalState:
        node = _target(row).get("result_node") or DEFAULT_RESULT_NODE
        data = payload.get("data")
        if payload.get("dataTooLargeToDisplay") or not isinstance(data, dict):
            return ExternalState("failed", code="external_result_invalid")
        redaction = data.get("redactionInfo")
        if isinstance(redaction, dict) and redaction.get("isRedacted"):
            # The workflow's redaction policy hides its output (revealing needs execution:reveal).
            return ExternalState("failed", code="external_result_redacted")
        try:
            result = payload["data"]["resultData"]["runData"][node][-1]["data"]["main"][0][-1][
                "json"
            ]
        except KeyError, IndexError, TypeError:
            return ExternalState("succeeded", result={})
        if not isinstance(result, dict):
            return ExternalState("failed", code="external_result_invalid")
        encoded = json.dumps(result, separators=(",", ":")).encode()
        if len(encoded) > RESULT_LIMIT_BYTES:
            return ExternalState("failed", code="external_result_invalid")
        return ExternalState("succeeded", result=result)

    async def cancel(self, row: Mapping) -> None:
        if not row.get("external_ref") or not self._api_configured():
            return
        response = await self._client.post(
            self._execution_url(row, "/stop"), headers=self._headers()
        )
        if response.status_code != 404:
            response.raise_for_status()
