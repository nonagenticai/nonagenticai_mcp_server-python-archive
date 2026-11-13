# Authentication and bindings (phase 2)

This phase adds shared license authentication, operator authorization, exact versioned toolkit selection, credential-reference metadata, and binding issuance/resolution. No uploaded package runs. The catalog plan is implemented by [publication and discovery](CATALOG_PUBLICATION.md); broker, worker, runner, Kata and durable scheduling follow separately. The automatic release hold remains in place.

Every UUID identity that mcp mints is **UUIDv7** (service, operation, tool, toolkit, profile, binding, event, run, ensure and reference IDs). Tenant and licence IDs come from the licence authority, which inserts them by SQL migration as UUIDv4, so those accept any RFC 9562 UUID version (`AuthorityId`; migration `0007` drops their v7 CHECKs). Python generates new identities with `uuid.uuid7()`. Strict contracts and PostgreSQL constraints reject other UUID versions; fixtures and tooling use UUIDv7. Old catalog data has no conversion/compatibility path. The dedicated development reset is still required before release.

## Request authentication

Every `/mcp` request and descendant is protected by pure ASGI middleware, including direct/loopback/private network traffic. Only operational health routes are public. No incoming `Authorization` header is interpreted as a license or operator credential; it is only ever a Keycloak service-JWT candidate.

Binding protocol, run and selection requests require:

```text
X-Tenant-Id: Bearer <MCP application license token>
X-MCP-Binding: <opaque binding token>
```

Two non-binding caller kinds reach only the first-party builtin provider (`src/builtin/`, see spec §6/§7), on `/mcp` and `/mcp/` only (403 elsewhere):

```text
X-Tenant-Id: Bearer <care-bus tenant licence>      # no X-MCP-Binding; optional X-Strata-Token
Authorization: Bearer <Keycloak service JWT>        # azp in KEYCLOAK_ALLOWED_CLIENTS
```

The care-bus licence is introspected with `{"token"}`. It must be `active: true` with `app.key == "care-bus"`, and it gets the care-bus and researcher tools only when `subject_kind == "tenant"`. The Keycloak JWT is validated locally against the realm JWKS (RS256, `iss`, `exp`, required `aud`). Its scope comes from `azp`: `carebus-tool-executor` gets the 13 care-bus tools, `carebus-researcher` gets `legal_*`, `web_scrape` and `research_*`, and any other allowed `azp` gets nothing. An invalid licence is 401 and does not fall through to the JWT. An `mcp` licence without a binding is 401. These callers have no session pinning and no catalog authority. Every builtin call is written to `mcp_tool_call_audit`.

Management requests require:

```text
X-Tenant-Id: Bearer <MCP application license token>
X-MCP-Admin-Key: <mounted operator key>
```

The license must introspect as strictly `active: true`, have explicit tenant/license UUIDs (any version), a subject, the configured `app.key`, and both expiry fields. Upstream `null` expiry means perpetual; a missing, malformed, naive or expired timestamp denies access. The shared HTTP client disables redirects and ambient proxy credentials, bounds response bytes, connection count and total request time, and uses HTTPS unless internal HTTP is explicitly approved. It never logs tokens or introspection bodies.

Positive caching uses `(SHA-256(token), application key)` with a bounded LRU. Authority lasts until the earliest of the configured TTL, license expiry or token expiry, with a conservative expiry skew subtracted. Defaults are 30 seconds TTL, 2 seconds expiry skew and 1,024 entries. Zero TTL disables caching. Outages deny new authority after a cache entry expires. External license revocation latency is at most the configured TTL (maximum 300 seconds). Binding enabled/revoked/expiry/revision, toolkit inclusions, tool sharing/retirement and operation/service publication are read from PostgreSQL for every request; there is no replica-local binding authorization cache.

Operator keys are bounded mounted files, loaded at startup and compared in constant time. A tenant key additionally requires membership in `MCP_ADMIN_TENANT_IDS`; the principal's tenant always comes from introspection. A distinct optional `X-MCP-Platform-Admin-Key` can establish platform authorization. Platform write routes are not exposed in phase 2, and runtime roles cannot write platform contracts or sharing grants. Key file changes require an API restart. No binding token grants management rights.

Absent headers, wrong identities, duplicate credentials, invalid/expired/revoked bindings and session switching return bounded 401 errors. Unconfigured/unavailable authority or unavailable dependencies return bounded 503 errors. Authenticated route validation/conflict/ownership errors use 422/409/403/404 without echoing request data, secrets or database exception text. Management JSON is bounded before framework parsing. Responses are marked `Cache-Control: no-store`.

## Toolkit and profile lifecycle

Toolkit versions pin exact tenant/tool/version references and exact same-tenant toolkit inclusions. Bindings may also pin platform toolkits (see [Platform toolkits](#platform-toolkits)). Flattened public-name membership, operation restrictions and minimum limits are stored atomically with immutable snapshots. Inclusion depth is at most 16, total visited inclusions 256 and effective tools 128. Self-inclusion, duplicates, missing/draft/disabled dependencies and conflicting public names are rejected. Repeated identical tools retain the conjunction of all restrictions. Phase and card stage are separate applicability fields; required context must match each included toolkit.

Publication revalidates and locks dependencies. The runtime role uses a narrowly scoped owner-executed function to lock published platform operation/service dependencies, without receiving platform UPDATE privileges. Published membership and all draft snapshot content are immutable; changes append a version. Toolkit publication/disable, profile changes and binding changes update the durable catalog generation transactionally.

Credential profiles contain only tenant-owned service aliases and expected application/license identities. They accept no secret values or filesystem paths. A profile's gateway credential declares the MCP application license identity used by its bindings. Required service/application coverage is checked against selected operation contracts. API validation explicitly returns `credentials_verified: false`: worker bundle existence, tenant ownership and actual service license validity can only be established by the later worker/broker. Database references are not claims of successful credential verification.

## Binding lifecycle and scope

Creating a binding generates a UUIDv7 ID and random 256-bit token. The token is returned once in that response. PostgreSQL stores only its SHA-256 digest; inspect/list/audit responses never return it. Rotation issues a new token, invalidates the previous digest and sessions, and increments revision. Revocation is irreversible. Updates, rotation, revocation and toolkit/profile mutations require an expected revision; concurrent stale updates return a conflict. Knowing another tenant's UUID grants no access.

Bindings pin exact toolkit versions, effective tool versions, optional exact subsets, typed context, conjunctive operation constraints, limits and expiry. They cannot select a tool outside toolkit membership or change their gateway license identity. Public input must be closed and cannot declare host-bound tenant/project/workspace/stage/command/commit fields. Context schemas, context pointers and applicability must be satisfied. Static conflicts between numeric, set, context equality and normalized path restrictions reject the binding; enforcement against actual arguments still belongs to the broker.

The current host context verifier accepts tenant scope only. Project/workspace ownership, stage provenance, fixed commands and reviewed commits require an approved host verifier, which is deliberately not available as a caller flag. Requests to bind these values fail closed until that integration exists. Required context is never filled from agent input.

Access changes and issuance/rotation/revocation create append-only, tenant-indexed `mcp_access_events`, in the same transaction as the change. Events carry UUIDv7 entity/event/license identities and separate actor attribution, never tokens, token hashes, context bodies or credentials. Run/API journals and broader runtime audit are phase 4–6 work.

## Platform toolkits

First-party toolkits are published by one reserved **platform tenant** (`MCP_PLATFORM_TENANT_ID`, the UUIDv7 constant `PLATFORM_TENANT_ID` in `src/config.py`). Migration `0008` carries the same constant as `public.mcp_platform_tenant()`; readiness fails if the two differ, so the value is fixed by migration, not by deployment. The platform tenant authors and publishes its tools and toolkits through the ordinary operator routes (its licence tenant must be in `MCP_ADMIN_TENANT_IDS`).

A binding's `spec.toolkits[]` entries are `{tenant_id, id, version}`. `tenant_id` is the toolkit's owner: the binding's own tenant or the platform tenant. Omitted, it is the binding's tenant; stored specs always carry it. Any other owner is `404`, exactly like an unknown toolkit, so knowing another tenant's toolkit ID reveals nothing. Platform toolkits are read-only for tenants: they are pinned to an exact published version, and a tenant cannot draft, publish or disable them. A tenant's own toolkit still cannot select a platform tool or include a platform toolkit unless that tool is explicitly shared. Disabling a platform toolkit version reaches every binding that pins it on the next resolution.

The database enforces the same rule. `mcp_binding_toolkits.owner_tenant_id` references the owner's toolkit version, and a CHECK allows only `owner_tenant_id = tenant_id` or `owner_tenant_id = mcp_platform_tenant()`. The binding completeness trigger matches each row against the spec's `{tenant_id, id, version}`. Tests in `tests/test_platform_toolkits.py` prove tenant A never sees tenant B's toolkits: not through bindings, management routes or direct inserts.

Every tool version declares `operation`, the agent platform's lowercase `object.verb` (for example `coding.provision_repo`; pattern in `src/catalog/models.py`, CHECK in `0008`). MCP `tools/list` and `/mcp/available-tools` emit it as `_meta.operation`; tool names keep the catalog `Name` pattern. A binding whose effective tools would repeat an operation is rejected (`409`), so the agent runner maps operations to tools one to one.

## Binding provisioning (bindings/ensure)

`POST /mcp/internal/tenants/{tenant_id}/bindings/ensure` takes `{"product", "licence_id", "licence_token", "rotate"?}` and a Keycloak service JWT (`Authorization` only; azp in both `KEYCLOAK_ALLOWED_CLIENTS` and `MCP_BINDING_ENSURE_CLIENTS`).

- **Licence.** The calling product holds the tenant's mcp licence and sends its id and token. mcp introspects the token (never stores or logs it). It must be active for the mcp application, `subject_kind: tenant`, for the path tenant, with exactly `licence_id`. Anything else is `403` and nothing is written; a missing field is `422`. mcp never mints an `app=mcp` licence. The platform tenant itself cannot be provisioned through a product (`403`).
- **Toolkits.** The product maps through `MCP_BINDING_PRODUCTS` to toolkit names. Each name resolves to the **platform tenant's** latest published version, pinned in the binding; a tenant's own toolkit of the same name is ignored. Unpublished toolkits, and toolkits whose backend licences cannot be minted, are listed in `skipped`.
- **Bundle.** Each product maps to a licences bundle through `MCP_PRODUCT_BUNDLES` (default `agent → agent`, `care-bus → care-bus`; every product needs one). Backend credential references in the `ensure_<product>` profile record that bundle, and the minter sends `bundle=<key>` on every `bundle-token` call, so a tenant holding several bundles that contain the same app never gets `409 ambiguous_bundle`. The `mcp_binding_ensures` row records each binding's product.
- **Write.** In one transaction it writes the `ensure_<product>` credential profile, one binding per (tenant, product), the `mcp_binding_ensures` row and a `binding.ensured` access event. The token is returned only on create (201) or `rotate: true`; a re-ensure returns `token_already_issued: true`. A binding's gateway licence must introspect with `subject_kind: tenant`.

The minting resolver never holds the gateway licence (`gateway_credentials = False`): the broker skips re-minting it at preflight, because the run's gateway licence is the caller's and was authenticated on the request that created the run.

## Protocol session boundary

The middleware checks each HTTP protocol request, including SSE/reconnect and DELETE. A future mounted transport's successful initial response can issue a session ID, which is durably bound to the initial tenant, gateway license, binding and binding-token digest before headers reach the client. Incoming sessions must already have that identity; guessed IDs or credential switching fail. Session rows store only ID/token hashes and bounded expiry (minimum of license authority, binding expiry and configured session TTL). Rotation/revocation removes sessions. Session expiry cleanup is bounded and opportunistic. WebSocket protocol requests are refused.

Phase 2 uses mounted ASGI fixtures to prove conditional session pinning. The implemented [catalog transport](CATALOG_PUBLICATION.md) mounts real stateless FastMCP HTTP, issues no session ID and offers no persistent GET stream. `/mcp/available-tools` and operator previews now expose the same public schemas as MCP. Real HTTP SDK tests cover initialize/list/call and unavailable production dispatch. No execution or run-creation endpoint is enabled.

## Routes and configuration

Implemented operator routes:

- `POST /mcp/toolkits`, `GET /mcp/toolkits` (bounded pagination).
- `POST /mcp/toolkits/{id}/versions`, `GET .../{version}`, `POST .../{version}/publish`, `POST .../{version}/disable`.
- `POST /mcp/credential-profiles`, `PUT /mcp/credential-profiles/{id}`, `POST .../{id}/validate`.
- `POST /mcp/bindings`, `GET /mcp/bindings/{id}`, `PATCH /mcp/bindings/{id}`.
- `POST /mcp/bindings/{id}/rotate-token`, `POST .../revoke`, `GET .../effective-tools`.

Payload models are in `src/toolkits/models.py`. A create body is the corresponding manifest/profile/spec. Version/profile/binding updates wrap that object as `manifest`, `profile` or `spec` alongside `expected_revision`. Lifecycle actions use `{"expected_revision": 1}`. Binding creation and rotation return `{"binding": ..., "token": ...}`. Bindings target the gateway license declared by the operator-managed profile; this may differ from the issuing operator's license, but the connecting license must match the binding.

The current schema head is `0008_platform_toolkits`; phase 2 introduced `0002_toolkits_bindings`. Apply migrations and rerun `scripts.grant_runtime_role` with the migration owner. Runtime grants allow only required access DML, selected column updates, session cleanup and alias/membership replacement; toolkit history and access events cannot be deleted. Deferred checks require complete relational references matching JSON snapshots. Startup verifies all required relations without DDL.

Set `LICENSE_INTROSPECT_URL`, `MCP_LICENSE_APP_KEY`, `MCP_ADMIN_KEY_FILE`, and a nonempty JSON `MCP_ADMIN_TENANT_IDS`. Optional settings, bounds and defaults are in `.env.example` and `src/config.py`. Omitting license configuration keeps probes usable but protected work unavailable. Configuring an operator key without license configuration/allowlisted tenants fails startup. Compose requires explicit auth settings and mounts the tenant key read-only. No development bypass exists.

GitOps rollout must supply the approved introspection destination, explicit app identity, secret key mount readable by UID 1000, tenant allowlist and separate migration/runtime credentials. The edge must preserve the custom headers and protect streaming time budgets. Application authorization also applies to in-cluster calls. Do not mount worker bundles, add runner RBAC or unhold automatic release at this phase. No live database or external GitOps files are changed by this implementation.

## Validation evidence (2026-10-24)

- 185 tests pass against disposable PostgreSQL 18 with 96.97% source coverage (85% required), with no skipped database tests. Coverage includes the toolkit/profile/binding lifecycle under a restricted runtime role, tenant/license isolation, rotation/revocation, and durable protocol-session authorization using mounted transport fixtures.
- Ruff lint/format, ty, the Bandit medium/high gate, workflow lint, configured Dockerfile lint and whitespace checks pass. The locked dependency audit reports no known vulnerabilities.
- The baseline-aware detect-secrets hook passes for current repository files, and Gitleaks passes for changed/new files using the shared CI fleet rules. One synthetic rejected URL in an authentication test has an explicit non-secret annotation. All 27 local Markdown links resolve.
- The local image builds and passes `scripts.verify_catalog_image`: both migrations apply twice, shipped tooling grants the runtime role, and the API runs non-root/read-only with capabilities dropped. Probes report execution unavailable, protected requests require authentication, `/token` remains absent, and the test removes its containers/network. The packaged head is `0002_toolkits_bindings`; the CI route check uses generated OpenAPI paths to include FastAPI's router wrappers correctly.
- This is historical phase-2 evidence. Current protocol evidence and limitations are documented in [publication](CATALOG_PUBLICATION.md). Live reset, deployment and release remain pending; broker and Kata execution remain subsequent work.
