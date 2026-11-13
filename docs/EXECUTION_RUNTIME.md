# Executable runtime implementation and rollout


The repository now contains the credential broker, independent runner, Kata backend and durable executor. Schema head is `0006_tool_call_audit`. Execution is disabled by default. The automatic release hold remains in place. Local tests and Docker execution do not satisfy the specification's real Kata isolation, multi-worker crash injection and operational rollout gates.

## Process and authorization boundaries

The API validates binding authority, public inputs and exact packages, then atomically accepts a PostgreSQL run. It never imports package code, reads worker credential bundles or manages pods. MCP and `POST /mcp/runs` share the submission service; `X-MCP-Idempotency-Key` is host metadata. Repeating a key and input returns the same durable run; changing the input conflicts. Calls without a key can create separate runs. A disconnected client does not cancel accepted work.

`GET /mcp/runs/{uuid7}`, `GET .../events?after=0&limit=50` and `POST .../cancel` require the owning binding or a tenant operator. Revoked/expired bindings cannot poll. Operator requests use the application license and admin key. Responses omit code, credentials, raw diagnostics and management attribution. Optional MCP run-control tools are not advertised; REST polling is available.

Workers claim durable slots across replicas and serialize tenant allowance checks. Least-recently-served tenant queues are selected before FIFO runs. Global and per-tenant queue limits reject overload; REST returns 429 and Retry-After. UUID identities are generated with Python 3.14 `uuid.uuid7()` and validated as version 7; PostgreSQL run, worker and permit checks enforce the same rule. Ordered event cursors and capacity slot numbers are integer counters, not UUID identities.

Each execution has an immutable authority/profile/input snapshot and a monotonically increasing lease fence. Live binding, image, version, sharing and credential checks can narrow or invalidate that authority. Context/profile changes invalidate an existing run; widening never replaces its snapshot. Every broker operation validates the closed frame, exact operation version, conjunctive constraints, credential identity, budget and fence. Ownership rules invoke only approved nonrecursive host-side read contracts and consume the same budget and journal. Such contracts must accept the declared argument object. Resource/stage binding context continues to fail closed unless a trusted `ContextVerifier` implementation verifies actual provenance; model input and operator booleans cannot attest ownership.

One Kata pod serves one invocation. The Python Kubernetes client uses in-cluster configuration, an explicit RuntimeClass, a pinned approved image and attach with binary framing. No exec, local executor, default-runtime fallback, package installer or warm pool exists. Uploaded code loads only in the runner image. Runner stdout/stderr is separated from protocol stdout; the host treats every frame as untrusted. Successful results require a validated final frame **and** exit code zero for the same pod UID.

Cancellation, deadlines and channel/authority loss stop the pod. Mutation intent commits before HTTP I/O; a lost/invalid outcome is marked `outcome_unknown` and interrupts the invocation. Only explicitly declared downstream idempotency permits mutation retries, preserving the same key; the composite tool is never replayed. Expired leases fence the old worker. Only runs with no pod intent, start or API call may requeue. Capacity remains quarantined until the exact pod is confirmed absent. A delete response is insufficient. ResourceQuota is the physical backstop for delayed create responses and worker failures.

## Mounted configuration

Use `.env.example` for all settings. API: set `MCP_EXECUTION_ENABLED=true` only after combined acceptance. Worker additionally requires a worker database URL, license introspection settings, `MCP_CREDENTIAL_BUNDLE_FILE`, `MCP_SERVICE_DESTINATIONS_FILE`, `MCP_RUN_NAMESPACE=mcp-runs`, `MCP_KATA_RUNTIME_CLASS`, `MCP_RUNNER_IMAGE_DIGEST` and the matching approved image list. Lease duration must exceed three heartbeat intervals.

The credential bundle is schema version 1:

```json
{"schema_version":1,"entries":[{
  "tenant_id":"<UUIDv7>","profile_id":"<UUIDv7>",
  "service":"mcp","alias":"gateway_alias",
  "license_id":"<UUIDv7>","app_key":"mcp","token":"<mounted secret>"
}]}
```

Add separate entries for each upstream service, matching the exact profile references and license/application identities. Optional `oauth_bearer` supplies independently provisioned upstream OAuth only when the service uses `license_header`; its license goes in X-Tenant-Id and OAuth in Authorization. The license-bearer adapter uses Authorization exclusively. No incoming Authorization is forwarded. Bundle values are bounded and masked; aliases are names, never paths. Mount read-only with mode 0440, owned by root or worker UID. Replace files atomically for rotation; one opened inode is validated per read. Do not use Kubernetes subPath mounts for bundles requiring projected-secret rotation. Missing/wrong references fail before creating a pod. Metadata validation in the API continues to return `credentials_verified: false`.

Destinations are runtime tool sources, not a file. `POST /mcp/sources` (platform key) registers `source_key`, `base_url` (https; an optional fixed path prefix such as `https://api.example/coding`) and an `auth` block: `{"kind":"licence","app_key":"<app>","scheme":"bearer"|"x_tenant_id_bearer"}` (the calling tenant's licence for that application, minted per call) or `{"kind":"static_bearer"}` / `{"kind":"static_header","header":"X-Api-Key"}` with a write-only `token`, stored AES-256-GCM encrypted (`MCP_SOURCE_SECRET_KEY`, 32 bytes base64url; the source id is the associated data). A service manifest's `destination_alias` names a `source_key`; publishing the service fails unless that source is active and agrees (`license_app_key`, `auth_adapter`). `MCP_SOURCE_ALLOWED_HOSTS` (exact hosts, `*.suffix`, `host:port` for non-default ports; empty allows nothing) is enforced on registration and again on every call; IP literals and userinfo are always refused. Tokens never appear in responses, logs, audit rows or errors. `PUT /mcp/sources/{key}`, `PUT /mcp/sources/{key}/token`, `POST /mcp/sources/{key}/disable|enable` take `expected_revision`. `POST /mcp/toolkits/import` publishes a toolkit bundle through the same content-addressed path as `python -m src.platform.seed`.

Internal HTTP requires explicit `allow_internal_http: true` on that source. Redirects are refused. Operation mappings control path/query/body/approved headers. Projection, redaction, schema and byte limits apply before a response reaches a runner. Credential echoes are rejected. Concurrency permits and tenant/service minute counters are shared in PostgreSQL; retries count as actual attempts. Pagination is not followed implicitly; code must make separately authorized, budgeted calls, with the operation pagination limit also capping its attempts per run.

Platform service/operation routes require both tenant and platform management keys and a separate limited `MCP_PLATFORM_DATABASE_URL`. This is an operator role, never the migration-owner URL. Tenant API and worker roles cannot mutate platform contracts. OpenAPI import supports a bounded bundled 3.1.0 subset with path/query parameters, optional JSON body and one JSON 200 response. Import creates drafts; review scope, effect, projection and idempotency before publishing. Unsupported forms fail rather than generating permissive mappings.

## Build and provision

```bash
uv sync --frozen --extra worker
docker build --tag mcp-api:candidate .
docker build --target worker --tag mcp-worker:candidate .
docker build --file runner/Dockerfile --tag mcp-runner:candidate .
uv run --extra worker python -m scripts.verify_runner_image --image mcp-runner:candidate
```

The API image excludes the Kubernetes dependency; the worker installs the locked optional extra. The runner uses only the Python standard library, has its own dependency-free lock, and receives no database/license/cloud credentials. Publish the reviewed runner image and configure its immutable registry digest, not a mutable tag.

Using the migration role against the verified target, run ordinary forward migration for an existing fresh-chain catalog; use the separate controlled reset procedure only for the discarded legacy schema:

```bash
uv run alembic upgrade head
uv run python -m scripts.grant_runtime_role --role mcp_runtime
uv run python -m scripts.grant_runtime_role --role mcp_executor --worker
uv run python -m scripts.grant_runtime_role --role mcp_platform --platform
# All workers stopped; shared capacity chosen from verified node/quota commitments.
uv run python -m scripts.configure_execution_capacity --slots 1
```

Start API and worker using their respective runtime URLs. Never put a migration-owner URL or worker bundle in the API deployment. Workers refuse an absent/mismatched capacity set; startup performs no DDL. Worker probes on port 8034 check schema, capacity, bundle and Kubernetes/RuntimeClass availability without launching a pod. Internal `/metrics` reports queue age/depth, active/starting runs, owned/quarantined slots and bounded recent run/API durations and status counts. Restrict probe/metric access with NetworkPolicy.

## GitOps and acceptance gates

The cluster integration — namespace isolation, quota/limits, restricted runner RBAC, worker deployment and network allowlist, and an acceptance Job — belongs in the operator's own deployment repository. Worker replicas are zero, execution is false, image references are deliberately unresolved, and the API-server network address is TEST-NET until the target rollout supplies verified values. Pin images, configure registry pulls, destinations, Kubernetes API CIDR and the actual RuntimeClass resource name. Review existing additive NetworkPolicies: another allow policy can widen access. No shared RuntimeClass changes are included.

The one-slot candidate quota assumes a RuntimeClass with 192Mi overhead plus a 256Mi runner (448Mi total). Recalculate from actual RuntimeClass overhead, node allocatable memory and co-tenant commitments before enabling. Both the database slot count and namespace quota must agree with the chosen capacity. Do not derive safe concurrency from container memory alone.

The standalone acceptance Job invokes `scripts.verify_execution_cluster` **inside the cluster** with the worker identity. A cluster acceptance harness additionally provisions synthetic infrastructure and exercises actual worker process crashes. Supply a dedicated disposable database/catalog, synthetic license authority, two synthetic upstreams serving `/fixture` with `{"ok":true}`, bundle entries named `acceptance_gateway`, `acceptance_1`, `acceptance_2`, destination aliases/app keys `acceptance_1/2`, and a mounted strict `Acceptance` configuration. All configured tenant/license/profile IDs must be UUIDv7. Supply reachable public and cluster probe IPs so deny-all behavior is actually tested. The job authors/publishes services, operations, tools, toolkit and binding; discovers/calls them through the real MCP client; polls durable results; tests broker composition, forged operation denial, network/SA isolation, stdout spam, memory pressure and infinite loops; and checks zero pods/owned or quarantined slots before revoking its binding. It leaves immutable synthetic catalog history in the disposable acceptance database.

Also run the specification's restart, multi-worker fairness and crash-injection matrix on that deployment: kill a worker before/after create, attach, mutation and result persistence; verify fencing, ambiguous journal outcomes and confirmed cleanup without replay. Exercise REST cancellation and caller disconnect, license/profile/binding revocation, retries and upstream rate ceilings. The checked-in local fake-backend tests are evidence of controller/state-machine behavior only. Do not remove the release hold or enable client-uploaded code until this combined cluster gate passes and the coordinated database/GitOps rollout is approved.

## Local validation evidence

Validation performed on 2026-10-24 uses disposable PostgreSQL, synthetic licenses/upstreams, real FastMCP HTTP, test-only backend crash scenarios and Docker runner execution. The runner image smoke test proves source execution and stdout separation under non-root/read-only/network-none/capabilities-dropped Docker restrictions; it does not prove Kata attach or cluster network isolation. The final test/security/image results are recorded with the implementation handoff. No live database reset, image promotion or production deployment has occurred.

Final local gate: 310 tests pass, with 93.10% combined source/runner coverage (85% required). Ruff lint/format, ty, Bandit, dependency audit, baseline-aware detect-secrets, changed-file fleet Gitleaks, workflow/Dockerfile lint, migration/role checks, local documentation targets and hardened API/runner image smoke checks pass. API, worker and runner images build. Kubernetes generated-client attach options are exercised without a live cluster; actual Kata execution/crash/isolation acceptance remains unperformed.

## Execution controls

Migration `0005_execution_controls` adds one-way payload expiration and draining slots. `MCP_EXECUTION_RETENTION_DAYS` defaults to seven days. A bounded, owner-executed function erases inputs, public context/authority, profile references and results only for terminal runs older than the cutoff with no worker, slot, permit or unresolved intent. It preserves run identity, input hash, idempotency receipts and audit/journal metadata. Expired responses report `payload_expired: true`. API roles cannot invoke retention; workers cannot restore or alter expired snapshots. Reconciliation expires at most 128 payloads per pass.

`configure_execution_capacity` marks excess slots draining and preserves active/quarantined ownership. Claims exclude draining slots; shrinking never kills a run. Reconfigure all worker settings to the desired new capacity. Readiness verifies the count of non-draining slots; idle draining rows can be removed by a later operator reconciliation.

`MCP_API_CONCURRENCY_GLOBAL` and `MCP_API_CONCURRENCY_PER_SERVICE` bound PostgreSQL permits across all workers and destinations. `MCP_API_MAX_INFLIGHT` also narrows each service and the local HTTP pool. `MCP_API_RATE_LIMITS_FILE` accepts a closed schema-version-1 object with a `limits` array: each entry has `service_id`, optional `tenant_id`, `calls_per_minute`, and `concurrency`. A missing tenant applies its ceiling to every tenant using that service. Entries only narrow defaults; duplicate identities and oversized/invalid files fail startup. Per-tenant/service minute counters are durable. Fixed denial events and internal metrics report rate/concurrency rejection without tenant labels. `MCP_API_TIMEOUT_SECONDS` and `MCP_API_MAX_RESPONSE_BYTES` narrow operation/service ceilings. Startup validates queue, frame/output and sync/transport relationships.

The runner startup deadline also bounds silence after attach. Final cleanup marks any abandoned API intent `outcome_unknown` before releasing ownership, including graceful interruption. `MCP_WORKER_PROBE_PORT` defaults to 8034 and permits independently probed worker processes in the synthetic harness.

## External runtimes (n8n_workflow, fr_project)

Migration `0011_tasks_external_runs` adds `mcp_runs.runtime` (immutable; `kata_python` default), `external_ref` (set once) and `status_message`, plus `mcp_task_inputs` (open questions per run); `0012_runtime_fr_command` widens the `runtime` CHECK to admit `fr_command`. Tool manifests declare `runtime` and, for external runtimes, a `target` (`{"kind":"n8n_workflow","webhook_path","result_node"}`, `{"kind":"fr_project","project_type"}` or `{"kind":"fr_command","command":"projects.get"}`); they carry no entrypoint or runner image, and the builder cannot edit them.

The Kata scheduler never claims external runs and they never hold an execution slot. `python -m src.external.driver` (deployment `mcp-external-driver`, probe `MCP_EXTERNAL_PROBE_PORT`=8036, worker database role) is their only writer:

- claims queued external runs (fenced lease), writes a `dispatch` event, then starts them in n8n (`POST /webhook/<path>` → `executionId`, with a short-lived `Authorization: Bearer` JWT signed with `MCP_N8N_WEBHOOK_SECRET` when it is set) or fr (a signed `projects.create {json}` chat command as the run's actor address on fr's `mcp` channel, `correlation_id = run_id`);
- a `starting` run with a `dispatch` event but no saved `external_ref` is `interrupted` (`external_start_unknown`), never re-dispatched; one without a `dispatch` event is started by whichever driver adopts it;
- polls every `MCP_EXTERNAL_POLL_SECONDS`, adopts runs whose lease expired, applies cancel requests, and times runs out after `MCP_EXTERNAL_RUN_MAX_SECONDS` (default 7 days; `MCP_RUN_MAX_SECONDS` is Kata's);
- forwards every answered task input to fr as `approve <id>` / `reject <id> <reason>` from the answering human's address (n8n questions are fr approvals too); a refused answer re-opens the input.

n8n results come from the run data of the target's `result_node` (default `MCP Result`) via `GET /api/v1/executions/{id}?includeData=true&ignoreDataSizeLimit=true` (4 MiB read cap, 64 KiB result; a redacted or missing `data` fails the run); n8n 2.46.1 truncates custom data to 512 characters, so it cannot carry results. fr posts HMAC-signed events (`X-Channel-Signature`, 300 s skew) to `POST /api/channels/fr/events` on the API: approvals open/close task inputs (routed by `correlation_id`, else `project_id`, else the start approval), `message` events record the project start's outcome on `external_ref`; the driver polls `projects.get` and alone applies terminal state (`delivered` = done).

Cutover: set `MCP_FR_CHANNEL_SECRET` on the API, allow the channel's namespace to reach it through the ingress policy, set `MCP_TASKS_ENABLED=true` and run the driver with `replicas: 1`. <!-- pragma: allowlist secret -->
