# Tool builder blueprint

This is the detailed design behind TOOL_BUILDER.md, based on origin/main 96d7c7f. Where they differ, the decisions in TOOL_BUILDER.md win.

## Current state
- Tools, toolkits, bindings and profiles are tenant-scoped by query (`WHERE tenant_id=%s`). There is no RLS.
- `mcp_sources`, `mcp_services` and `mcp_*_versions` are global, and only `platform_pool` writes them.
- The broker looks up sources with `sources.get(service.destination_alias)`, using a global `source_key`.
- `transport()` calls `pointer()`, which raises when a field is missing, so optional fields cannot be omitted.
- The broker always runs `json.loads` on the body, so a 204 or an empty body becomes a denial.
- The importer accepts only 3.1, a single 200 response, and no `$ref`. `validate_schema` already accepts `#/$defs/*` references.
- `authenticate_caller` refuses an mcp licence without a binding (401 "binding required").
- The `principal_kind` CHECK on `mcp_tool_call_audit` allows two kinds.
- `mcp_runs` has `binding_id`, `binding_revision` and `credential_profile_id` as NOT NULL.

## A. Ownership: migration 0010_tenant_builder
**Approach**
- Query scoping plus SECURITY DEFINER `mcp_builder_*` write functions. The runtime role gets EXECUTE on them.
- These functions reject `owner_tenant_id = mcp_platform_tenant()`.
- The platform keeps `platform_pool`.

**`mcp_sources`**
- Add `owner_tenant_id uuid NOT NULL DEFAULT mcp_platform_tenant()`.
- Replace UNIQUE(`source_key`) with UNIQUE(`owner_tenant_id`, `source_key`).
- Auth kinds: `licence`, `none`, `static_bearer`, `static_header`, `query_param`, `basic`, `oauth2_client_credentials`.
- `(kind IN ('licence','none')) = (token_ciphertext IS NULL)`.
- Tenant rows must be `https://` with `allow_internal_http` false and `ca_file` NULL.
- Add `spec_path text`.
- The guard trigger also freezes `owner_tenant_id`.
- `mcp_source_events` gets `owner_tenant_id`.

**`mcp_services`**
- Add `owner_tenant_id`, defaulting to platform.
- A builder service is internal and 1:1 with a tenant source. Its key is `t<12 hex of service_id>_<source_key>`, truncated to 64 characters, so the global `service_key` UNIQUE stays.
- The broker resolves the source as (`service.owner_tenant_id`, `destination_alias`).

**New tables**
- `mcp_service_grants(service_id, grantee_tenant_id, granted_by, created_at)`: platform-only writes. Licence-kind platform services are shared by default; static-token platform services need an explicit grant.
- `mcp_operation_keys(service_id, op_key, operation_id UNIQUE)`, UNIQUE(`service_id`, `op_key`).
- `mcp_source_specs(owner_tenant_id, source_id, sha256, fetched_at, bytes, document jsonb)`, PK(`source_id`, `sha256`). Keep the last 3.

**Reference rule**
- A tenant tool may reference operations whose service is owned by the tenant, or by the platform with a grant (or licence kind).
- Enforce it in a BEFORE INSERT trigger on `mcp_tool_version_operations`, in `mcp_check_publication`, and again in the broker.

**Runs and other changes**
- `mcp_runs.mode` is `bound` or `draft` (default `bound`). The three binding columns become nullable, with CHECK `(mode='bound') = (all three NOT NULL)` and draft ⇒ `owner_tenant_id = tenant_id`.
- Relax the UUIDv7 CHECKs on `mcp_runs.tenant_id` and `gateway_license_id` to any RFC UUID.
- `mcp_run_api_calls` gets `method`, `path` (expanded, ≤1024), `http_status`, `duration_ms` and `reason` (`ssrf_blocked`, `redirect`, `status`, `too_large`, `schema_mismatch`, `auth_failed`, `timeout`, `transport`).
- The audit `principal_kind` CHECK adds `tenant_author`.
- Bump `EXPECTED_REVISION` and the smoke assertion in `scripts/verify_catalog_image.py`.

## B. Tenant author auth
- `AuthorPrincipal(tenant_id, license_id, subject)` with an `actor` property. It is NOT `Principal`, so `get_operator` can never accept it.
- Middleware on `/mcp/builder/*`: `licenses.authenticate` with app `mcp` and `tenant_level` required.
  - A request that also carries `x-mcp-admin-key` or `x-mcp-binding` gets 401.
  - The body is buffered.
  - Add a `get_author` dependency.
- Product path: `/mcp/internal/tenants/{t}/builder/*`. It takes the service JWT (`internal_keycloak`, an azp in `binding_ensure_clients`) plus that tenant's mcp licence (id and token) in the request; the licence is introspected and must be tenant-level for `t`.
- Settings, all `MCP_BUILDER_*`:

| Setting | Default |
|---|---|
| `enabled` | false |
| `max_sources` | 20 |
| `max_operations` | 300 |
| `max_tools` | 50 |
| `max_toolkits` | 10 |
| `spec_max_bytes` | 5 MiB |
| `spec_fetches_per_hour` | 30 |
| `test_runs_per_hour` | 60 |
| `source_calls_per_minute` | 60 |
| `source_concurrency` | 2 |
| `allowed_ports` | (443,) |
| `deny_cidrs` | () |
| `oauth_token_max_ttl` | 3600 |

- Quotas are counted under `pg_advisory_xact_lock(hashtext('builder:'||tenant))`.

## C. Egress (`src/broker/egress.py`)
**Pinning**
- `PinnedBackend(httpcore.AsyncNetworkBackend)` wraps `AnyIOBackend`. In `connect_tcp` it:
  1. checks the port;
  2. resolves with `anyio.getaddrinfo`;
  3. refuses if ANY address fails the policy;
  4. connects to the IP literal.
- httpcore's `start_tls(server_hostname=origin.host)` keeps SNI and certificate checks on the hostname, and httpx sends `Host` from the URL.
- `TenantTransport(httpx.AsyncHTTPTransport)` replaces `_pool` with `httpcore.AsyncConnectionPool(network_backend=PinnedBackend, http2=False)`.
- Pin httpx and httpcore exactly, and add a test that the backend is used.
- Client options: `follow_redirects=False`, `trust_env=False`, `verify=True`, no `ca_file`. A 3xx is a denial.

**Addresses rejected:** anything not `is_global`, plus multicast, unspecified, reserved, loopback, link-local, private and CGNAT. Also reject the embedded IPv4 inside mapped, 6to4, teredo and 64:ff9b::/96 addresses, the metadata addresses (169.254.169.254, fd00:ec2::254, 100.100.100.200) and `deny_cidrs`.

**Hostnames rejected:** IP literals, userinfo, single-label names, `localhost`, `*.local`, `*.internal`, `*.svc` and `*.cluster.local`. Checked at registration and again at call time.

**Platform vs tenant**
- Platform sources keep `enforce(source_allowed_hosts)` and `client_for(ca_file)`.
- Tenant sources always use the tenant transport.
- Rate limit: cap the existing `permit` window per (tenant, service) with `source_calls_per_minute` and `source_concurrency`.

## D. Auth adapters (`src/broker/auth.py`)
**Kinds**

| Kind | Non-secret fields | Secret |
|---|---|---|
| `none` | — | — |
| `static_bearer` | — | token |
| `static_header{header}` | header name | header value |
| `query_param{param}` | parameter name | parameter value |
| `basic{username}` | username | password (characters 32–126 allowed) |
| `oauth2_client_credentials{token_url, client_id, scope?, audience?, client_auth: basic\|post}` | as listed | client secret |
| `licence{app_key, scheme, bundle?}` | as listed | minted, allow-listed hosts only |

- There is one encrypted secret per source, using the existing AES-GCM with the source id as associated data.
- `AuthAdapter.SOURCE = "source"`: `license_app_key` is optional when the adapter is `source`.

**OAuth2 cache**
- In-process, keyed by (`source_id`, `revision`).
- TTL = `min(expires_in, max_ttl) − 30 s`, with single-flight refresh.
- The token URL goes through egress. The response is limited to 64 KB, must contain `access_token`, and `token_type` must be `bearer`.
- An upstream 401 evicts the token.

**Secrets in logs**
- Every secret is added to `leaks()`.
- The query secret is added last, and a mapping that collides with its name is rejected.
- Set the `httpx` and `httpcore` loggers to WARNING.
- The trace shows parameter names only, with `***` for the secret.

## E. Specs and import
**Endpoints**
- `POST /mcp/builder/sources/{key}/spec/fetch {path?}`
  - Same origin, through the source credential and egress, JSON only.
  - Limited to `spec_max_bytes` and 20 s; cached by sha256.
  - Returns `{sha256, title, version, operations:[{method, path, operationId, summary, importable, reason}]}`.
- `GET /mcp/builder/sources/{key}/spec/{sha}/operations`
- `POST /mcp/builder/sources/{key}/operations/import`
  - Body: `{spec_sha256, select:[operationId | {method, path}], overrides:{op_key, effect, expected_error_statuses, response_projection, timeout_seconds}}`.
  - Creates draft operations plus `mcp_operation_keys` rows.

**`import_openapi`**
- Accepts 3.0 and 3.1.
- Local `$ref` only: components/schemas become `$defs`, everything else is inlined.
- 3.0 conversion: `nullable` and boolean `exclusive*` are converted. `discriminator`, `xml`, `example` and `externalDocs` are dropped, as are `readOnly` properties on inputs and `writeOnly` properties on outputs.
- Parameters in `path`, `query` and `header`, with reserved headers refused. A `cookie` parameter makes the operation non-importable.
- Response: the lowest 2xx (200–204) with JSON. A 204 sets `OperationManifest.empty_response`, and the broker returns `{}`.
- `ArgumentMapping.optional: bool = False`, valid only for input sources and never for path parameters. In `transport()` an optional mapping with an absent pointer is skipped, and no body is sent if nothing was mapped.
- The importer marks non-required parameters and a non-required body as optional.

## F. Draft test-run
- `ExecutionService.submit_draft(author, tool_id, version, inputs, confirm_writes)`:
  - the version must be owned by the tenant and be `draft` or `published`;
  - a `writes` tool needs `confirm_writes`;
  - limits are narrowed by settings;
  - the profile is ephemeral: the gateway is the caller's mcp licence, plus the minter's `licence_id` per licence-kind service;
  - `test_runs_per_hour` applies.
- `live(row)` draft branch: check the fence, re-read the tool version (same manifest, still owned), re-check the grants, and skip the binding and profile checks.
- The broker accepts draft-status operations and services only for `mode='draft'` and services owned by the run's tenant.
- `journal_outcome` records `http_status`, `duration_ms` and `reason`.
- `GET /mcp/builder/runs/{id}` returns the envelope plus `trace[]`. For draft runs only, it adds a redacted body excerpt (≤2 KB) for non-2xx responses.

## G. Authoring MCP toolkit
**Hosting**
- Builtin FastMCP server `src/builder/tools.py`, namespace `builder`.
- Tenant comes from `current_caller().principal.tenant_id`.
- `BuilderService` is installed at lifespan through `src/builder/runtime.install(state)`.
- `authenticate_caller`: when the licence app is `mcp`, the licence is tenant-level, there is no binding and the builder is enabled, return `CallerPrincipal(TENANT_AUTHOR, subject, "licence:mcp", tenant)`.
- `LICENCE_SCOPES["mcp"] = BUILDER_SERVERS`. Every call is audited.

**Tools**

| Group | Tools |
|---|---|
| Sources | `builder_source_create`, `_update`, `_set_secret`, `_list`, `_disable` |
| Spec | `builder_spec_fetch`, `builder_spec_operations` |
| Operations | `builder_operations_import`, `builder_operation_list`, `builder_operation_update` |
| Tools | `builder_tool_create`, `builder_tool_update`, `builder_tool_test`, `builder_run_get` |
| Publish | `builder_tool_publish` |
| Toolkits | `builder_toolkit_create`, `builder_toolkit_publish`, `builder_toolkit_bind` |

- `builder_tool_create`/`_update` take files, schemas and `ops [{source_key, op_key}]`. The host fills `tenant_id`, the runner digest, `kit.py`, `ops.py` and `package_sha256`, reusing `toolkit_source` packaging.
- `builder_tool_publish` publishes the draft service and operation dependencies in the same transaction.

## H. Lanes
**Lane 1, Data (owns the contracts and goes first):**
- `migrations/versions/0010_tenant_builder.py`
- `src/storage/schema.py`
- `src/catalog/models.py`
- `src/sources/{models,repository,crypto}.py`
- `src/storage/catalog.py`
- `src/config.py`
- `scripts/grant_runtime_role.py`
- `scripts/verify_catalog_image.py`

**Lane 2, Egress and broker:**
- `src/broker/egress.py` (new)
- `src/broker/auth.py` (new)
- `src/broker/{client,policy}.py`
- `src/sources/{registry,guard}.py`

**Lane 3, Draft execution and importer:**
- `src/execution/{service,models,controller}.py`
- `src/storage/executions.py`
- `src/catalog/importer.py`
- `src/builder/openapi.py`

**Lane 4, Surface:**
- `src/auth/{protocol,principal,context}.py`
- `src/dependencies.py`
- `src/routers/builder.py`
- `src/routers/internal.py`
- `src/builder/{service,spec,runtime,tools,packaging}.py`
- `src/builtin/{scope,__init__,audit}.py`
- `src/asgi.py`
- `src/storage/bindings_ensure.py` (decision 4)

**Tests**
- Migrations from 0009, including tenant isolation and the reference trigger.
- Egress IP matrix, DNS rebinding pinning, and SNI/Host against a local TLS server.
- Every auth kind, the OAuth cache, and query secrets never logged.
- Importer fixtures for 3.0 and 3.1, including a real api.nonagentic.ai spec.
- Draft-run isolation and trace redaction.
- Auth-path separation.
- An end-to-end builder flow.
