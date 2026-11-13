# Licensing for mcp / agent / care-bus early-access tenants

All refs are origin/main unless they point at the p1-merge worktree (WT). No credential files were opened.

## 1. How licensing works today

- **Model** (licenses `models.py`). The tables are `App` (2321), `Plan`, `Bundle` (2383) and `BundleApp`. A tenant holds bundles through `TenantBundle` (2469). `AppRequires` (2524) is a dependency edge with a `caller_owner_key`. `TenantDependencyGrant` (2589) is an app-scoped grant for the calling owner. The other tables are `AppIssuer`, `BundleEnsurer` and `RealmOwner`. There is one licence per (tenant, app), and its subject is either the tenant-level `svc:<tenant>:<app>` or a person.
- **Issuance.**
  - `POST /internal/tenants/{t}/bundles/{b}/ensure` (main.py:11382) mints one per-app licence for every app in the bundle, plus dependency licences for the `app_requires` closure (`is_dependency`, migration 059).
  - `POST /internal/tenants/{t}/bundle-token?app=` (main.py:10961) mints a short-lived token, at most 60 min, on that app's licence. It requires the `bundle-token-minter` role and is scoped to the owner, or to the grant's app for dependency grants.
  - Member licences come from `POST /licenses/user/{sub}`.
- **Introspection** (`POST /license/introspect`, main.py:3701). The request takes `{token}` only (main.py:2960). The `app` field the mcp server sends (WT `src/auth/licenses.py:91`) is silently ignored. The server checks `app.key` itself (licenses.py:107).
  - The response (main.py:2966ff) carries `tenant_id`, `license_id`, `subject_id` and `subject_kind` (tenant/member), plus `app`, `plan`, `metadata`, `expires_at`, `token_expires_at` and `roles`.
  - license-authz maps `/mcp/` to app `mcp` (license-authz `src/main.py` `APP_ROUTE_REGISTRY`).
- **n8n node modules.** Each module is its own app key. Migration 051 puts the six module apps into bundle `n8n-nodes` with a `tenant-pooled` plan. The owner (CP) ensures the bundle for its tenant, so each module gets its own per-app tenant licence. The credential carries licence token + `X-Tenant-Id`. This is the "licence per application" pattern.
- **Early access.**
  - `early_access.md` (v2) says approval creates the tenant, the tenant licences and the member licence before the email goes out.
  - The licences step is a "migration copying 067" for a new app.
  - `early-access-intake` is config only and has no licence logic.
  - `provisioning` exposes `ensure_bundle` and `mint_bundle_token` (`src/clients/licenses_client.py:243,277`).
  - care-bus approval calls ensure for bundle `care-bus` (the care-bus signup licence step).
  - agent approval only registers the tenant and mints a member `agent` licence (agent `crates/agent-api/src/licenses_admin.rs:1-10,290`). It ensures no bundle.

## 2. What exists for mcp, care-bus and agent

| app | licences state |
|---|---|
| `mcp` | Platform app seeded by `seed_apps.py:786`. Issuers come from the default `{platform, fr}` (models.py:1944). It is a member of bundle `non-agentic-ai` (028:137). It has **no `tenant_owner` `mcp`, no minter and no outgoing `app_requires`.** |
| `care-bus` | Defined in 067. Migration 078 adds the `care-bus` bundle, `bundle_ensurer`, `care-bus-bundle-minter` and edges care-bus → mcp, strata-rag, strata-ingest. Migration 080 adds care-bus → laya. Every care-bus tenant therefore already holds a **dependency `mcp` licence, which is unused.** |
| `agent` | Defined in 070: owner, `agent-licenses-admin`, plan, issuer. It has **no bundle, no ensurer, no minter and no edges.** Agent's golden fixtures already assume an `agent-bundle-minter` (agent-api `tests/golden/connector_platform/overview.json:1932`), but licences does not have it. |

**Granularity.** Entitlements stop at the app level, with plan as the tier. Nothing per toolkit or per feature exists: no scopes table, and `roles` is used only for operator tokens. Inside the mcp server, toolkit and tool scoping lives in bindings (WT `docs/AUTHENTICATION_BINDINGS.md:50-56`) and in the builtin scope table (`src/builtin/scope.py`).

## 3. Gaps

**(a) An `mcp` licence for an early-access tenant**
- For care-bus tenants the licence exists through 078, but care-bus presents its care-bus licence instead (`mcp_credentials.rs:4`). The mcp server rejects an `mcp` licence that has no binding (WT `src/auth/protocol.py:97-99`).
- Agent tenants have no mcp licence at all.

**(b) Toolkit bindings**
- Bindings are created only by an operator (`X-MCP-Admin-Key` plus a tenant in `MCP_ADMIN_TENANT_IDS`).
- Nothing auto-provisions a binding for a newly licensed tenant.
- Nothing maps an entitlement to a toolkit.
- `LicenseClient.authenticate` does not check `subject_kind`, so a member's `mcp` licence would be accepted as the gateway licence.

**(c) Per-app backend licences for the broker**
- The broker reads a **static mounted file** of long-lived tokens (WT `src/credentials/resolver.py:14-25`, `config.py:91`), which does not scale to self-serve tenants.
- Builtins use shared env tokens (`src/builtin/carebus/client.py:28-30`, `research/client.py:63`, `web/scrape.py:113`).
- webscraper, discovery-engine and scientific-processor have no licence app key. coding, laya, strata-* and tools do.

**Risk.** Licence and tenant IDs default to `gen_random_uuid()`, which is v4, when rows are inserted by SQL (models.py:2965-2970). mcp rejects non-v7 IDs (licenses.py:111). Before cutover, audit the IDs of existing care-bus and agent tenant licences.

## 4. Proposed flow

1. **Sold app + bundle per product** (the n8n pattern):
   - bundle `care-bus` = {care-bus}, which exists today;
   - new bundle `agent` = {agent}, with `bundle_ensurer` agent and api_client `agent-bundle-minter`.
2. **Dependency closure carries mcp and its backends:**
   - `app_requires` agent → mcp (caller `agent`), which care-bus → mcp already has;
   - new `app_requires` mcp → coding, strata-rag, laya, … (caller owner `mcp`), giving a transitive closure;
   - ensure writes a `tenant_dependency_grant(t, owner mcp, app)` per backend.
3. **Product → mcp.** On every call the product mints a tenant-level `mcp` token (`bundle-token?app=mcp`) and sends the following, with `X-MCP-Binding` taken from the product's own vault:
   ```
   X-Tenant-Id: Bearer <mcp licence>
   X-MCP-Binding: <binding token>
   ```
4. **Entitlement → binding.**
   - mcp gets a reconciler endpoint, `POST /mcp/internal/tenants/{t}/bindings/ensure`, called by the product's provisioning with a service JWT (azp allow-list).
   - It reads a declarative table: product → toolkit ids, for example agent → {coding, legal, research} and care-bus → {care-bus-ops, research}.
   - It creates or reuses one binding per (tenant, product) on the tenant's `mcp` licence and returns the binding token once, for the product to store.
   - Toolkit choice comes from that table. Licences stays app-level, because the binding *is* the finer-grained entitlement.
   - Optionally the plan code selects a toolkit tier.
5. **Broker.** Add a `LicenceMinterCredentialResolver` beside `FileCredentialResolver`. As `mcp-bundle-minter` it mints `bundle-token?app=<backend>` for the binding's tenant, cached until the token's expiry minus skew. A dependency grant scopes it to granted apps only, and the 403 `dependency_grant_scope` fails closed.
6. **Builtin path.** care-bus moves from "care-bus licence, no binding" to step 3. The care-bus licence caller path and the KC-JWT path are deleted afterwards (plan P5).

## Change list

**licenses**
- Migration 081:
  - `tenant_owner` `mcp`;
  - api_client `mcp-bundle-minter`, which needs the gitops `BUNDLE_MINTER_CLIENTS` realm patch;
  - `app_requires` mcp → {coding, strata-rag, strata-ingest, laya};
  - agent bundle, ensurer, `agent-bundle-minter` and agent → mcp.
- Mirror these in `APP_REQUIRES_SEED`, `BUNDLE_ENSURER_SEED` and `seed_apps.py`.
- Add app keys `webscraper`, `discovery-engine` and `scientific-processor` if they should be metered per tenant.
- Verify that the `app_requires` closure is transitive in `services.bundle_ensure`.

**provisioning / early-access-intake**
- intake: none.
- provisioning: optional `ensure_bundle` + `mcp ensure-bindings` step for products that use it.

**agent**
- The approve journal adds `ensure bundle agent` and `mcp bindings/ensure`, and stores the binding token (DEK/KEK, P6 Task 4).
- The P6 Task 10 `McpTransport` `TokenSource` = `bundle-token?app=mcp` + `X-MCP-Binding`, instead of an RFC 8707 OAuth token for the first-party mcp.

**care-bus**
- The approve path adds the bindings ensure.
- `mcp_credentials.rs` mints `app=mcp` and sends `X-MCP-Binding`.
- `X-Strata-Token` is dropped once the broker mints strata-rag.

**mcp server**
- Reject `subject_kind != tenant` gateway licences.
- Add a bindings-ensure endpoint plus a product→toolkit table.
- Add the minter credential resolver.
- Expose builtins to bindings through toolkit opt-in (plan P1).
- Drop the unused `app` field from the introspect body, or ask licences to honour it.

**gitops**
- Keycloak realm patch: `mcp-bundle-minter` and `agent-bundle-minter` (bundle-token-minter role), plus the azp allow-list for the ensure endpoint.
- NetworkPolicy: mcp worker → licences.
