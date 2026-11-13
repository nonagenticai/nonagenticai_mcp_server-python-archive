# Tool builder: any API, registered by tenants and product platforms

Owner direction (2026-10-25): mcp is a generic tool builder.
- **Who registers sources.** A tenant, or a product platform acting for one of its tenants (an agent platform, a care-bus client), can register any API as a source: an HTTPS URL plus optional auth. The source may be the product's own APIs, a third-party API or a customer API.
- **How tools are built.** Tools are deterministic scripts that run in Kata and call several sources through the broker.
- **What `api.nonagentic.ai` is.** It is the first platform source. It uses a per-tenant licence minted per app, the same way conveyor does.

## Callers of the builder
| Caller | Auth | Acts for |
|---|---|---|
| Tenant, directly (REST or the `builder_*` MCP tools) | `X-Tenant-Id: Bearer <tenant-level mcp licence>`, no binding | its own tenant |
| Product platform (agent, care-bus) | `/mcp/internal/tenants/{t}/builder/*`, service JWT whose azp is on the allow-list, plus that tenant's mcp licence in the body (as bindings/ensure does) | tenant `t`, limited to products the azp may provision |
| Operator | existing admin and platform keys | platform sources and toolkits, shared with tenants by grant |

## Decisions on the design uncertainties
1. **Grants and EXECUTE on `mcp_builder_*`:** both go in `scripts/grant_runtime_role.py`, the same pattern as 0009. gitops runs the grant Jobs.
2. **UUID version checks:** the UUIDv7 CHECKs on `mcp_runs.tenant_id` and `gateway_license_id` are a latent bug, because licences mints v4 tenant ids. Migration 0010 relaxes them to any RFC 9562 UUID.
3. **Pinned egress via httpcore `_pool`:** accepted, with `httpx`/`httpcore` pinned exactly and a test that asserts the pinned backend is actually used.
4. **Tenant toolkits and bindings:** a tenant's own published toolkits are added to its product binding automatically. Ensure and rebind include them, so callers keep one binding token per product.
5. **Spec formats:** JSON only (OpenAPI 3.0.x / 3.1.x). No YAML.
6. **Draft runs:** they return redacted upstream error excerpts (2 KB or less, passed through `leaks()`) to the owning tenant only.
7. **Licence-kind sources:** allowed only on operator-allow-listed hosts. Minted licences never go to arbitrary hosts.

## Blueprint
These are the A–H sections from the design pass. They are summarised here because lanes implement against them.
- **A. Ownership model**
  - `owner_tenant_id` on `mcp_sources` and `mcp_services`; source keys are unique per tenant.
  - `mcp_service_grants` shares platform services with tenants.
  - `mcp_operation_keys` maps operation keys to operations.
  - `mcp_source_specs` caches fetched specs.
  - A reference trigger (tenant tools may use only their own services, or granted or licence-kind platform services).
  - `mcp_runs.mode` is `bound` or `draft`.
  - Trace columns on `mcp_run_api_calls`.
  - Migration `0010_tenant_builder`.
- **B. `AuthorPrincipal`:**
  - `/mcp/builder/*` (tenant licence) and `/mcp/internal/tenants/{t}/builder/*` (product).
  - Quotas under the `MCP_BUILDER_*` settings, counted under a per-tenant advisory lock.
- **C. Egress for tenant sources** (`src/broker/egress.py`):
  - resolve, then pin the connection to the checked IP, with SNI and Host kept on the hostname;
  - refuse the connection if any resolved IP is non-global (v4, v6, mapped, 6to4, teredo or NAT64), a metadata address, or in a cluster CIDR;
  - HTTPS on port 443 only, no redirects, `trust_env=False`;
  - platform sources keep the host allow-list path.
- **D. Auth kinds:**
  - kinds: `none`, `static_bearer`, `static_header`, `query_param`, `basic`, `oauth2_client_credentials` and `licence`;
  - one encrypted secret per source;
  - an in-process OAuth token cache, with single-flight refresh and eviction on 401;
  - httpx/httpcore loggers held at WARNING;
  - `AuthAdapter.SOURCE`.
- **E. Specs and import:**
  - fetch the spec through the source credential (size, timeout and per-hour limits), cached by sha256;
  - `import_openapi` handles local `$ref` (schemas become `$defs`), 3.0→3.1 normalisation, 2xx/204 (`empty_response`) and operation selection;
  - `ArgumentMapping.optional`: an absent optional input is omitted.
- **F. Draft test-run:**
  - `ExecutionService.submit_draft` runs the owner's draft or published tool with no binding;
  - an ephemeral profile;
  - writes need confirmation;
  - the result includes a trace.
- **G. The `builder_*` builtin MCP toolkit:**
  - scope `licence:mcp`, tenant-level, no binding;
  - in-process `BuilderService`;
  - tool packaging reuses `src/platform/toolkit_source`.
- **H. Lanes and tests:** see the PR.

## Lane 1 contracts (data layer, migration `0010_tenant_builder`)
Everything below is implemented and tested; lanes 2–4 call it, they do not re-derive it.

**Facts that differ from the blueprint**
- Decision 2 is already true: 0007 dropped the UUIDv7 CHECKs on `mcp_runs.tenant_id`, `gateway_license_id` and `mcp_run_api_calls.license_id`. 0010 adds no relaxation; a test proves UUIDv4 tenants and licences insert.
- `mcp_run_api_calls.license_id` stays NOT NULL. For sources without a licence (none, static, query, basic, oauth2) record the run's `gateway_license_id`.
- A platform service is shared with every tenant when its source is licence kind **or not registered**, and needs a grant when its source exists with any other kind (`mcp_tenant_may_use_service`). The broker re-checks per call (`CatalogRepository.may_use_service`).
- `src/sources/registry.py` still resolves by `source_key` only. Lane 2 MUST switch it to `resolve_row(conn, service_owner, destination_alias)`; with per-owner uniqueness a bare key can match a tenant row.
- `toolkits/service.py` (profile validation) and `storage/bindings_ensure.py` read `manifest.license_app_key`. A tenant service (`auth_adapter == "source"`) has none: lane 4 must skip such services when building credential refs.

**Errors** (`src.storage.catalog`): `CatalogConflictError` (revision/version/duplicate), `CatalogNotFoundError` (missing or not yours), `QuotaExceededError(.resource)` (subclass of conflict, map it first), `BuilderForbiddenError` (platform tenant), `ValueError` (CHECK violation, message is safe). Database SQLSTATEs: MB001 conflict, MB002 not found, MB003 quota, MB004 forbidden.

**Transactions.** Every repository method below takes `conn=` (a pooled `AsyncConnection` already in your transaction) and uses a savepoint on it; without it, it opens its own transaction. `in_transaction(pool, conn=None)` is exported from `src.storage.catalog`.

**Contracts** (`src.catalog.models`, `src.sources.models`)
- `AuthAdapter.SOURCE = "source"`. `ServiceManifest.license_app_key: AppKey | None` (required unless adapter is `source`). `OperationManifest.empty_response: bool = False`. `ArgumentMapping.optional: bool = False` (input source only, never `path`). Old manifests compare equal in `seed.same`.
- Auth union `SourceAuth` (discriminator `kind`): `NoAuth`, `LicenceAuth{app_key,scheme,bundle?}`, `StaticBearerAuth`, `StaticHeaderAuth{header}`, `QueryParamAuth{param}`, `BasicAuth{username}`, `OAuth2ClientCredentialsAuth{token_url,client_id,scope?,audience?,client_auth=basic|post}`. `needs_secret(auth)`; `SECRETLESS_KINDS = {licence, none}`. One write-only `token: SecretStr` carries the secret (basic: password, may contain spaces; oauth2: client secret).
- Tenant bodies: `TenantSourceCreate{source_key,base_url,auth,token?,spec_path?,idempotency_header?,max_*_bytes?}`, `TenantSourceUpdate{...,expected_revision}` (no `allow_internal_http`, no `ca_file`; public https only: `checked_tenant_url`). `TokenUpdate`, `RevisionOnly` unchanged. `SourceView` adds `owner_tenant_id`, `spec_path`; never a secret.
- Crypto: `encrypt_token`/`decrypt_token(key, source_id, blob)` unchanged; storage accepts printable ASCII incl. space (`valid_secret`); `valid_token` (no space) is what models apply to every kind except basic.

**Sources** (`src.sources.repository`)
- `TenantSourceRepository.from_settings(pool, settings)`; methods (all keyed by `tenant_id` first): `create(tenant, body, actor)`, `update(tenant, key, body, actor)` (omitted token keeps the secret; secretless kinds clear it), `set_secret(tenant, key, TokenUpdate, actor)`, `set_status(tenant, key, expected_revision, "disabled"|"active", actor)`, `get(tenant, key)`, `page(tenant, limit, offset)`, `put_spec(tenant, key, sha256, document, size)` (keeps last 3), `get_spec(tenant, key, sha256=None) -> {sha256,bytes,fetched_at,document}|None`. Registration checks `builder_allowed_ports` and, for licence kind, `source_allowed_hosts`. Host SSRF policy at call time stays lane 2.
- `resolve_row(conn, owner_tenant_id, source_key) -> dict|None`: the broker row (`source_id, owner_tenant_id, revision, status, base_url, auth, token_ciphertext, spec_path, idempotency_header, ca_file, max_*`). Owner = the service's owner (`CatalogRepository.service_owner(service_id)`).
- The operator `SourceRepository` is unchanged in API and now scoped to platform-owned rows.

**Services and operations** (`src.storage.builder.BuilderCatalogRepository(pool)`)
- `create_service(tenant, ServiceManifest, actor)` (version 1, adapter `source`, 1:1 with an active own source named by `destination_alias`; builder key = `t<service_id.hex[-12:]>_<source_key>`[:64]); `append_service(tenant, manifest, actor, expected_revision=)`.
- `create_operation(tenant, OperationManifest, actor, op_key=, max_operations=)`: draft; `op_key` required for version 1; later versions keep the key; `manifest.version` must be next.
- `publish(tenant, actor, services=[ExactReference], operations=[ExactReference])` (services first, idempotent); `set_status(tenant, "service"|"operation", ref, status, actor)`. To publish a tool with its dependencies atomically: one `conn`, `publish(..., conn=conn)` then `CatalogRepository.transition(..., conn=conn)`. `CatalogRepository.create/append/transition` take `conn=`.
- Reads: `service_for_source(tenant, source_key) -> ServiceRecord|None`, `operations(tenant, source_key)`, `operation_by_key(tenant, source_key, op_key) -> OperationRecord|None` (newest version, `.op_key .operation_id .service_id .version .status .manifest`), `visible_platform_services(tenant) -> [{service_id, service_key, version, via: grant|licence}]`, `usage(tenant)`.
- Quotas: sources and operations are enforced inside their functions. For tools and toolkits call `enforce_quota(conn, tenant, "tools"|"toolkits", settings.builder_max_*)` inside the transaction that inserts (takes the per-tenant advisory lock). Spec-fetch and test-run rates (`builder_spec_fetches_per_hour`, `builder_test_runs_per_hour`) are not stored by lane 1: lane 3/4 count test runs from `mcp_runs` and rate-limit spec fetches in process.
- Platform operator (`PlatformCatalogRepository`): `grant_service(service_id, tenant, actor)`, `revoke_service(service_id, tenant)`, `add_operation_key(service_id, op_key, operation_id)`; grants bump the catalog generation.

**Runs and trace** (columns only; writers are lane 3)
- `mcp_runs.mode` `bound|draft`. Draft: `binding_id`, `binding_revision`, `credential_profile_id` NULL and `owner_tenant_id = tenant_id`; `gateway_license_id` and `profile_snapshot` stay NOT NULL (the caller's mcp licence, the ephemeral profile). Partial unique index on `(tenant_id, tool_id, tool_version, idempotency_key)` for drafts.
- `mcp_run_api_calls`: `method`, `path` (≤1024) are set at INSERT (intent); `http_status`, `duration_ms`, `reason` (`ssrf_blocked|redirect|status|too_large|schema_mismatch|auth_failed|timeout|transport`) with the outcome UPDATE. Nothing else is updatable.
- `mcp_tool_call_audit.principal_kind` accepts `tenant_author` (auth_method `license`).

**Settings** (`MCP_BUILDER_*`): `builder_enabled=False, builder_max_sources=20, builder_max_operations=300, builder_max_tools=50, builder_max_toolkits=10, builder_spec_max_bytes=5MiB, builder_spec_max_total_bytes_per_tenant=25MiB (all stored specs of a tenant; last 3 per source), builder_spec_fetches_per_hour=30, builder_test_runs_per_hour=60, builder_source_calls_per_minute=60, builder_source_concurrency=2, builder_allowed_ports=(443,), builder_deny_cidrs=(), builder_oauth_token_max_ttl=3600`. Enabling requires licence authentication.

**Grants** (`scripts/grant_runtime_role.py`): API role gets SELECT on the new tables and EXECUTE on `mcp_builder_*` and `mcp_tenant_may_use_service`, no DML. Worker (`--worker`) gets SELECT on sources, services, grants, op keys, specs and EXECUTE on the reference rule; trace columns ride its existing INSERT/UPDATE on `mcp_run_api_calls`. Platform (`--platform`) additionally gets INSERT/DELETE on grants and INSERT on op keys. `verify_schema` expects revision `0010_tenant_builder`.

## Integration contract for product platforms
Facts below are taken from the code in this branch (`src/auth/protocol.py`, `src/routers/internal.py`, `src/routers/builder.py`, `src/builder/tools.py`, `src/catalog/provider.py`). Enable the builder with `MCP_BUILDER_ENABLED=true`; until then every builder route answers 404.

### 1. A product platform authors tools for a tenant
Callers are the Keycloak service clients `agent-bundle-minter` and `care-bus-bundle-minter` (realm `secure-apps`, audience `nonagenticai-mcp-internal`). Their azp must be in `MCP_BINDING_ENSURE_CLIENTS`; the same list gates `bindings/ensure`.

- **Service JWT:** `Authorization: Bearer <client-credentials token>`.
- **Tenant licence.** Send the tenant's tenant-level mcp licence as `X-Tenant-Id: Bearer <licence token>` plus `X-MCP-Licence-Id: <licence uuid>`. Alternatively put `licence_id` and `licence_token` in the JSON body, as `bindings/ensure` does. The path tenant must match the licence's tenant. The platform tenant is refused.
- **Routes.** The REST routes are the same set as the tenant's own, under `/mcp/internal/tenants/{tenant_id}/builder/...`:
  - `POST /sources`, `PUT /sources/{key}/secret`
  - `POST /sources/{key}/spec/fetch`, `POST /sources/{key}/operations/import`
  - `POST /tools`, `POST /tools/{id}/versions/{n}/test`, `GET /runs/{run_id}`, `POST /tools/{id}/versions/{n}/publish`
  - `POST /toolkits`, `POST /toolkits/{id}/versions/{n}/publish`, `POST /toolkits/bind`
- **Binding.** Call `POST /mcp/internal/tenants/{t}/bindings/ensure` with `{product, licence_id, licence_token, rotate?}`; `product` is `agent` or `care-bus`. The tenant's own published toolkits are added to the product binding automatically. After publishing a new toolkit, call `POST .../builder/toolkits/bind` (or `bindings/ensure` again) so the existing binding includes it. The binding token comes back in `token` only when it is issued. Store it per tenant and product; `token_already_issued: true` means keep what you have.
- A product never sees another tenant's data. The azp only proves that the caller may act for tenants it presents a licence for.

### 2. A tenant agent authors tools itself
Connect to `POST /mcp` with `X-Tenant-Id: Bearer <tenant-level mcp licence>` and **no** `X-MCP-Binding`. The server then exposes only the `builder_*` MCP tools (20 of them): `builder_source_create`, `builder_source_set_secret`, `builder_spec_fetch`, `builder_operations_import`, `builder_tool_create`, `builder_tool_test`, `builder_run_get`, `builder_tool_publish`, `builder_toolkit_create`, `builder_toolkit_publish`, `builder_toolkit_bind` and the update, list and get variants. The same flow is available over REST at `/mcp/builder/*` with the same header. A member-level licence is refused. Every call is audited with `principal_kind=tenant_author`.

### 3. Agents call bound tools
- Request headers:
  - `X-Tenant-Id: Bearer <tenant mcp licence>`
  - `X-MCP-Binding: <binding token from bindings/ensure>`
  - optional `X-MCP-Idempotency-Key`
- Transport: `POST /mcp`, JSON-RPC `tools/list` and `tools/call` (MCP streamable HTTP).
- URL inside the cluster: `http://<service>.<namespace>:8033/mcp` (path `/mcp`, port 8033).
- Public URL: `https://api.nonagentic.ai/mcp`. **Cloudflare cuts a request at 100 s (HTTP 524).** The API waits up to `MCP_SYNC_WAIT_SECONDS` (300 s in the strato executor config), so a run that can take longer than about 90 s must not use the public URL synchronously. Use the in-cluster URL, or submit with `POST /mcp/runs` `{tool, inputs}` and poll `GET /mcp/runs/{run_id}`.
- Result of `tools/call`: `structuredContent` (also JSON-encoded in `content[0].text`) is the run envelope `{run_id, status, result, error, payload_expired}`.
  - `status` is one of `queued|running|succeeded|failed|cancelled|timed_out|interrupted`; `isError` is true for the last four failures.
  - When `succeeded`, `result` is the tool's own envelope `{status: ok|precondition_failed|check_failed|pending|backend_error, data, checks[], verdict, next[], reason?, error?, resume_token?}`.
  - If the run is still queued or running when the sync wait ends, poll `GET /mcp/runs/{run_id}` with the same headers until `status` is terminal.
  - `GET /mcp/runs/{run_id}/events` gives the event stream; `POST /mcp/runs/{run_id}/cancel` cancels.
- Test runs (`tool_test`) use no binding and return the envelope plus `trace[]` (method, path, http_status, duration_ms, reason per upstream call) and, for non-2xx upstream answers, `error_excerpt`: at most 2 KB, redacted, shown only to the owning tenant.
