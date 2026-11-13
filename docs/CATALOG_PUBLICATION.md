# Authenticated publication and MCP catalog

The catalog change plan is implemented. Tool packages remain inert data. Production uses `UnavailableSubmission`: valid MCP calls return `isError: true` with `execution_unavailable`, no structured run envelope and no accepted run. Test dispatch records exact authorization and returns synthetic envelopes without importing uploaded files. Broker enforcement, durable submission/journaling, workers, Kata and cluster acceptance remain subsequent work under the target specification.

## Tenant authoring

All authoring uses the gateway license and tenant-allowlisted operator key from [authentication](AUTHENTICATION_BINDINGS.md). Binding tokens cannot author. Platform authorization never changes the tenant predicate or grants platform-contract DML to the API role.

| Request | Contract |
|---|---|
| `POST /mcp/tools` | `{manifest, files}`; complete schema-version-1 manifest, version 1; 201 committed descriptor |
| `POST /mcp/tools/{id}/versions` | `{manifest, files, expected_revision}`; exact next version checked under the identity lock; 201 descriptor |
| `GET /mcp/tools/definitions?limit=50&offset=0` | Owned metadata only, ordered by ID/version; limit 1–100, offset ≥0 |
| `GET /mcp/tools/{id}/versions/{version}` | `{descriptor, files}`; exact owned manifest and complete file set |
| `POST .../{version}/validate` | Revalidates immutable package/contracts/dependencies; no audit mutation or run; credentials unverified |
| `POST .../{version}/publish`, `/disable`, `/retire` | `{expected_revision}`; committed descriptor; permitted lifecycle transitions only |

`files` is a list of `{path, content}` entries, preserving duplicate detection. See [contracts](CATALOG_CONTRACTS.md) for manifest fields and canonical digest calculation. Upload complete packages, including unchanged files; patches and legacy formats are rejected. Appending a historical/arbitrary manifest version returns 409. Internal copy helpers still allocate the next version from a template.

Every successful create, append, publish, disable and retire transaction records subject, gateway license, tenant, tool ID, parent revision and exact artifact version in append-only access audit. Audit failure rolls back the entire change, including generation. No package or input data appears in these events. Validation-only requests do not publish or add lifecycle events. Disabling/retiring a version is irreversible; new work requires a new draft version. Identity retirement remains an internal operation.

Reserved names `mcp_run_status` and `mcp_run_cancel` cannot be authored. Public inputs must be closed objects and omit all host-bound field names, including nested properties. The same rule is checked when selecting a binding. Image approval is checked at authoring, publication and live resolution. Discovery exposes name, description, input/output schemas and effect annotations; source files, entrypoint, destination aliases, credential references and attribution are management-only.

The encoded management body defaults to 256 KiB, independently of the 1 MiB source-package ceiling. Exceeding the body limit returns 413 before parsing. Operators can explicitly increase `MCP_MAX_MANAGEMENT_BYTES` within its 1 MiB ceiling; JSON encoding overhead still counts. A package fitting its source ceiling does not necessarily fit an HTTP upload. Schema, file-count, individual-file and validation-work ceilings also apply.

## Protocol and authority

FastMCP **4.1.4**, MCP SDK **2.3.0** and HTTPX **0.28.1** are locked and tested with Python 3.14. Both the handshake-era wire (`initialize`, 2025 versions) and the stateless 2026-07-28 wire (`server/discover`, per-request `_meta`, `MCP-Protocol-Version` header) are served. The compatibility boundary is `src/protocol.py` and `src/catalog/provider.py`. It registers only ping, tool listing and calls (plus the `tasks/*` methods when tasks are enabled, below), bypasses the SDK's public-name schema cache and advertises only tools with `listChanged: false`. Stored schemas are neither dereferenced nor inferred from Python signatures. No filesystem/provider imports or dynamic compilation load packages.

`/mcp` and `/mcp/` accept stateless Streamable HTTP without redirects. Responses use bounded SSE framing; no persistent GET notification stream, transport session ID, resumability, legacy SSE/messages endpoint or prompts/resources are offered. Unknown descendant paths remain 404 after authorization. Explicit REST routes precede the mount. Authentication also covers reverse-proxy root paths and direct/private-network calls.

Each HTTP request supplies both `X-Tenant-Id: Bearer <gateway-license>` and `X-MCP-Binding: <binding-token>`. Incoming session IDs must pass the existing durable identity checks; this transport issues none. Authorization uses immutable principal/context snapshots, and bearer headers are removed before entering FastMCP's HTTP context. Concurrent callers never share authorization snapshots. A retained wrapper re-resolves the current caller immediately before submission and compares the exact manifest, context and effective constraints/limits. The later durable executor must also authorize atomically when accepting work; this stage does not claim to remove all revocation races beyond its read boundary.

Listing, lookup, invocation, `/mcp/available-tools` and operator `/mcp/bindings/{id}/effective-tools` share one projection. It rechecks binding revision/license/expiry/revocation, profile coverage, exact toolkits/inclusions, tool sharing/publication, operation/service state and approved image. Generation changes retry resolution once, then fail boundedly. No cross-request authorization cache or process-local change notification is required. Two API instances observe committed changes independently.

Listings use deterministic names and bounded pages. Cursors are authenticated with the binding token hash, bound to tenant/license/binding/revision, catalog generation and exact resolved descriptors. They work across replicas with the same database. Forged, foreign, rotated or stale continuations fail and require a fresh list. Nothing in a cursor grants access outside a live selection.

`X-MCP-Idempotency-Key` accepts 1–128 characters from letters, digits, `_ . : -`. It is host metadata, excluded from tool arguments; JSON-RPC IDs are not idempotency keys. Inputs undergo the existing bounded validator without coercion before submission. Output schemas compose the declared result under a run envelope and rebase local schema references. The declared states include `cancelling`; run IDs must be UUIDv7. Production never fabricates this envelope while execution is unavailable.

| Setting | Default | Hard ceiling |
|---|---:|---:|
| `MCP_PROTOCOL_INPUT_BYTES` | 262144 | 1048576 |
| `MCP_PROTOCOL_OUTPUT_BYTES` | 2097152 | 8388608 |
| `MCP_PROTOCOL_MAX_INFLIGHT` | 32 | 256 |
| `MCP_PROTOCOL_TIMEOUT_SECONDS` | 15 | 60 |
| `MCP_CATALOG_PAGE_SIZE` | 10 | 100 |

These positive bounds limit HTTP resources per API process; they are unrelated to shared execution capacity. Oversized input is rejected before dispatch, overloaded admission returns 503 without queuing, and bounded response accumulation prevents partial oversized responses. Responses are buffered within the deadline before being exposed; SSE wire framing is preserved. Shutdown closes transport tasks before license/database resources. Readiness checks schema and transport initialization; all probes and previews report `execution: unavailable`.

## MCP tasks (`io.modelcontextprotocol/tasks`)

With `MCP_TASKS_ENABLED=true` the server advertises `capabilities.extensions["io.modelcontextprotocol/tasks"] = {}` and serves `tasks/get`, `tasks/update` and `tasks/cancel` on the 2026-07-28 wire only (SEP-2663 shapes; state lives in Postgres, not Docket/Redis). A task *is* an `mcp_runs` row: `taskId = run_id`.

- `tools/call` from a binding caller that declares the extension in the request `_meta` returns a flat `CreateTaskResult` (`resultType: "task"`) while the run is non-terminal, and the normal `CallToolResult` when it is already terminal. Without the declaration, at an older protocol version, or with tasks disabled, the run envelope is returned exactly as before.
- Status: queued/starting/running/cancelling → `working`; a non-terminal (non-cancelling) run with an open input → `input_required`; succeeded → `completed`; failed/timed_out/interrupted → `completed` with `result.isError: true`; cancelled → `cancelled`. `failed` is reserved for JSON-RPC-level errors.
- `tasks/*` without the extension declared → -32021 (with `data.requiredCapabilities`); `Mcp-Name` ≠ `taskId` → -32020; unknown, foreign (other tenant, binding or licence), expired-payload or malformed ids → -32602, indistinguishable from each other.
- `tasks/update` requires `_meta["ai.nonagentic/actor"] = {"channel": "mcp", "address": "<≤256 chars>"}`. Unknown or already-answered keys are acknowledged as no-ops; a rejection without `reason` stays open with `statusMessage: "reason required"`.
- `ttlMs` = `MCP_EXECUTION_RETENTION_DAYS`; `pollIntervalMs` = `MCP_TASK_POLL_INTERVAL_MS` (Kata) or `MCP_EXTERNAL_POLL_SECONDS` (external runtimes).
- mcp 2.3.0 passes a non-core `resultType` through unsieved on the 2026-07-28 wire, so no serializer patch is needed.

## Migration and release

The head is `0003_catalog_publication`, adding nullable positive `artifact_version` to access events and requiring it for the new tool lifecycle actions. Existing phase-2 event rows remain unchanged with null versions. Ordinary phase-2 upgrades preserve packages, toolkit pins, credential references, binding token hashes, sessions and generation; do not reset a phase-2 database. Reapply runtime grants with the migration owner if provisioning a new runtime role. The future execution migration is `0004_execution_runtime`.

Automatic publication/deployment remains held. The old-system database still requires the coordinated one-time rollout procedure. No live reset, GitOps deployment, upstream customer call or Kata execution is part of this stage's local acceptance.

Validation uses disposable PostgreSQL, the restricted runtime role, actual HTTP sockets with the MCP SDK, concurrent tenants/bindings, two API instances, live invalidation, strict uploads, migration preservation and audit rollback. The hardened image smoke test adds a synthetic license authority and temporary operator-key mount, exercises authoring through unavailable dispatch, verifies the packaged dependency/head, and removes all disposable containers/network. This proves protocol/dispatch integration and unavailable production behavior; it does not prove uploaded Python execution.

## Validation evidence (2026-10-24)

- 219 tests pass against disposable PostgreSQL 18 with 96.54% source coverage (85% required), including the restricted-runtime-role HTTP lifecycle and populated phase-2 forward upgrade. No database tests are skipped.
- Real MCP SDK initialization/list/call works over HTTP sockets at both endpoint forms. Injected dispatch proves exact versions, constraints, contexts and host idempotency metadata; production calls return only unavailable errors. Concurrent same-name tenant schemas, retained wrappers, two API instances, revocation, reverse-proxy routing, nested-alias rejection and signed pagination are covered.
- Ruff lint/format, ty, Bandit medium/high gate, locked dependency audit, workflow/Dockerfile lint, local Markdown links, whitespace, baseline-aware detect-secrets and installed shared fleet Gitleaks rules pass. The dependency audit reports no known vulnerabilities. Upstream TestClient/WebSocket deprecation warnings remain.
- The final image builds and passes authenticated container acceptance at head `0003_catalog_publication`, with a separate runtime role, UID 1000, read-only filesystem and all capabilities dropped. The test fixture's package raises if imported; publication/discovery/unavailable calls succeed without importing it. Disposable containers/network are removed. This is local packaging evidence, not deployed-cluster or Kata evidence.
- The coordinated live reset/rollout remains pending and automatic release remains held. Credential/broker, runner/Kata, durable executor and operational release gates remain incomplete.
