# nonagenticai_mcp_server caller contract

> **Executable-toolkit branch (P1 merge) — what changed for callers.** The care-bus contract below
> still holds: the same 34 tool names, the same per-`azp` and per-licence scopes, the same
> `[permanent] tool not permitted for this caller` refusal, the same `[retryable]`/`[permanent]`
> error texts, `X-Strata-Token` forwarding, and `/api/health`. These parts differ:
> - The transport is stateless Streamable HTTP. `/mcp` and `/mcp/` are both served with no 307,
>   and no `Mcp-Session-Id` is issued to care-bus callers.
> - An `mcp` licence needs `X-MCP-Binding` and sees only catalog tools. Without a binding it gets
>   401, and the `claude_*`/`tools_*` tools are gone.
> - The admin REST routes (`/mcp/tools/*` and the others) are replaced by binding/catalog management
>   (docs/AUTHENTICATION_BINDINGS.md).
> - No resources or prompts are served.
> - Error bodies are `{"error": "unauthorized"}` (401) and `{"error": "forbidden"}` (403).
> - When the licence authority is unreachable, the response is 503, not 401.
> - The per-call audit is written to `mcp_tool_call_audit`, not `audit_logs`.
> - Code moved: `src/auth/protocol.py`, `src/builtin/scope.py` and `src/builtin/audit.py` replace
>   `src/middleware/auth.py`, `src/tool_scope.py` and `src/tool_call_audit.py`.


What a caller of this service must send and what it gets back. Two kinds of caller exist:
licence-token callers (n8n nodes, tenants, operators) and Keycloak service accounts (for example `carebus-tool-executor` and `carebus-researcher`). This service supersedes an earlier implementation as the care-bus tool host. Source of truth for every rule below is the code it names.

## Endpoint

`POST http://<mcp-host>:8033/mcp` (the host is deployment config, not part of this contract), MCP
Streamable HTTP, exact path: `/mcp/` answers 307 to `/mcp`. `GET /mcp` is the SSE stream of the
same session. Send `Accept: application/json, text/event-stream`; a request to `/mcp` without
`Accept` gets `application/json` injected (`src/asgi.py`).

The admin REST surface lives under `/mcp/...`: `/mcp/tools/*`, `/mcp/tool-versions/*`,
`/mcp/audit/*`, `/mcp/info`, `/mcp/available-tools`, `/mcp/health-status`. Routes are
unversioned (`/api/<resource>` style, never `/api/v1`).

### Claude Code (or any MCP client) on a dev machine

Off-cluster clients use the same host through your ingress, e.g. `https://<mcp-host>/mcp`. The edge strips a client's `X-Tenant-Id`, so the
tenant's `mcp` licence goes in `Authorization: Bearer <licence>`, together with `X-MCP-Binding`
(#55). A `.mcp.json` entry:

```json
{"mcpServers": {"nonagentic": {"type": "http", "url": "https://<mcp-host>/mcp",
  "headers": {"Authorization": "Bearer ${NA_MCP_TOKEN}", "X-MCP-Binding": "${NA_MCP_BINDING}"}}}}
```

Set `MCP_TOOL_TIMEOUT=600000` (ms) in the client's environment. A coding-toolkit call can run up
to the tool's wall limit (300 s), and Claude Code's default of about 60 s gives up first. Do not
route long calls through `api.nonagentic.ai/mcp`: Cloudflare cuts requests off after about 100 s.

## Public paths (no credential)

`/`, `/api/health`, `/openapi.json`, `/favicon.ico`, and `/health`, `/docs`, `/redoc` together
with anything below them (`/health/live`, `/health/ready`, `/docs/oauth2-redirect`). A prefix
matches only on a `/` segment boundary: `/health-admin`, `/docsX`, `/openapi.jsonx` are NOT
public (`src/middleware/auth.py::is_public_path`, `tests/test_auth_exempt_boundary.py`).
`/mcp` itself is not public. `/api/health` is the care-bus-api overview probe
(`{MCP_URL minus /mcp}/api/health`); a token sent there is ignored.

## Credentials

Every other path needs exactly one of these two (`src/middleware/auth.py`). There is no
trusted-network / no-token branch, and a bare (non-`Bearer`) `X-Tenant-Id` is never trusted.

### (a) Licence token: `X-Tenant-Id: Bearer <licence>`

Introspected at `LICENSE_INTROSPECT_URL` (cached 5 min). The licence's `tenant_id` is the request
tenant (`subject_id` only if the payload has no tenant). Checked FIRST: if `X-Tenant-Id: Bearer`
is present and the licence is invalid, the answer is 401 and the `Authorization` header is not
tried. A licence is never read from `Authorization`.

Scope, by the introspected licence's `app.key` (`src/tool_scope.py`):

| Licence app | Tools | Resources / prompts |
|---|---|---|
| `mcp` | every tool EXCEPT the 13 care-bus tools (so `claude_*`, `claude_auth_*`, `tools_*`, `legal_*`, `web_*`, `research_*`) | all |
| `care-bus`, tenant-level (introspection `subject_kind == "tenant"`, licenses #305) | the 13 care-bus tools + the `carebus-researcher` set | none |
| `care-bus` with `subject_kind` `member` or absent, any other app, no app | none | none |

`/mcp/...` admin routes stay open to any active licence, tenant-filtered (unchanged).

#### care-bus licence callers

care-bus calls `/mcp` for tenant T with:

- `X-Tenant-Id: Bearer <T's care-bus licence token>`: mint it with licenses
  `POST /internal/tenants/{T}/bundle-token?app=care-bus&bundle=care-bus`
  (`care-bus-bundle-minter`, short-lived). It lands on T's tenant-level care-bus licence. Only
  owner care-bus issues `care-bus` licences.
- `X-Strata-Token: <T's strata-rag licence token>` (the same route, `app=strata-rag`; a bare token or
  `Bearer <token>`). `faq_ask` sends it to strata_rag as `Authorization: Bearer` together with
  `X-Tenant-Id: T`, in place of the pinned `STRATA_RAG_TOKEN`, so strata searches T and answers 403
  `tenant_mismatch` if the token is for another tenant. Without it, `faq_ask` returns
  `[permanent] X-Strata-Token ... is required for a licence caller` and strata is never called.

The Keycloak path below is unchanged for the transition (pinned `STRATA_RAG_TOKEN`).

### (b) Keycloak access token: `Authorization: Bearer <jwt>`

Only when all four of `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `KEYCLOAK_AUDIENCE`,
`KEYCLOAK_ALLOWED_CLIENTS` are set; a partial configuration disables the path (fail closed). The
token comes from a `client_credentials` grant. Validated locally by `src/auth/keycloak.py`:

| Claim | Rule | Config (env) | On failure |
|---|---|---|---|
| signature | RS256 only, key chosen by `kid` from the realm JWKS (`{iss}/protocol/openid-connect/certs`, cached 300 s, unknown `kid` refetches) | `KEYCLOAK_URL`, `KEYCLOAK_REALM` | 401 |
| `exp` | required, in the future | none | 401 |
| `iss` | required, `== {KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}` | same | 401 |
| `aud` | REQUIRED, must contain one of the values | `KEYCLOAK_AUDIENCE` (comma list) | 401 |
| `azp` | required, in the allowed set | `KEYCLOAK_ALLOWED_CLIENTS` (comma list) | 401 |

Deployment values: `KEYCLOAK_URL=https://auth.nonagentic.ai`,
`KEYCLOAK_REALM=portal`, `KEYCLOAK_AUDIENCE=nonagenticai-mcp`,
`KEYCLOAK_ALLOWED_CLIENTS=carebus-tool-executor`.

Unlike enterprise-mcp, `aud` is not opt-in. It reaches the token only through the
`nonagenticai-mcp-audience` mapper on `carebus-tool-executor` (realm patch
`ensure_audience_mapper`, `PATCH_ONLY=care-bus`). If the realm is ever rebuilt, run the care-bus
realm patch BEFORE this service takes care-bus traffic, or every care-bus tool call is 401.

Consequences for the caller:
- Fetch the token from the host in `KEYCLOAK_URL`: Keycloak stamps `iss` from the request host,
  so a token from an in-cluster Keycloak Service name carries another `iss` and is refused.
- Any other client (a portal user's browser token, another service account) is refused unless
  added to `KEYCLOAK_ALLOWED_CLIENTS` AND given the audience mapper.
- A JWT caller has no tenant unless the token carries a uuid `tenant_id` claim. `sub` is never a
  tenant, and `X-Tenant-Id` without `Bearer` does not add one.

Scope (least privilege, `src/tool_scope.py` `PRINCIPAL_SCOPES`, keyed by `azp`): a JWT caller
may use `/mcp` only.
- Any other protected path, every `/mcp/...` admin route included, is **403**.
- `tools/list` returns exactly its principal's tools: `carebus-tool-executor` the 13 care-bus
  tools below, `carebus-researcher` the research tools below, any other `azp` NONE (an azp must be
  in both `KEYCLOAK_ALLOWED_CLIENTS` and `PRINCIPAL_SCOPES` to use anything).
- `tools/call` of any other tool returns MCP `isError` with
  `[permanent] tool not permitted for this caller`, audited with status `denied`.
- `resources/list`, `resources/templates/list`, `prompts/list` are empty; `resources/read` and
  `prompts/get` are refused (`[permanent] resource|prompt not permitted for this caller`).

The scope fails closed: any caller that is not a licence-token caller is restricted, and one with
no known principal (unknown azp, no auth state, no HTTP request) gets the empty set.

## Error responses

| Status | `error.code` | When |
|---|---|---|
| 401 | `AUTHENTICATION_REQUIRED` | no credential (or `Authorization: Bearer` with Keycloak not configured) |
| 401 | `INVALID_TOKEN` | licence inactive / unknown, or JWT failed a check in the table |
| 401 | `MISSING_TENANT` | active licence without tenant context |
| 403 | `FORBIDDEN` | JWT caller on any path other than `/mcp` |

Shape: `{"error": {"code": "...", "message": "...", "status": 401}}`; every 401 also carries
`WWW-Authenticate: Bearer`. Messages never echo the reason a JWT was rejected; that goes to the
server log.

## Tools

`tools/list` for `carebus-tool-executor` returns exactly these 13 (pinned by
`tests/test_carebus_mount.py::CONTRACT`, `tests/test_tool_scope.py`,
`tests/test_asgi_mcp_http.py`):

```
crm_verify_customer  crm_get_start_data
order_get_order  order_get_payments  order_create_rma  order_create_warranty_claim  order_issue_refund
delivery_track_shipment
faq_ask
rules_check_return_window  rules_check_warranty  rules_check_payment_issue
procedures_get_procedure
```

`faq_ask(question, leaf_ids?)`: with `leaf_ids` (1-256 store uuids) strata_rag searches exactly those
leaves instead of routing from the tenant's root, and the answer carries `scope: {"leaf_ids": [...]}`,
the filter applied. care-bus sends it for a tenant with legal-matter walls (the stores outside every
wall) and drops any answer that does not confirm it, so an older server that ignores the filter can
never feed it walled documents.

`carebus-researcher` gets exactly these 10 (pinned by `tests/test_legal_tools.py::CONTRACT`,
`tests/test_tool_scope.py`):

```
legal_eli_search  legal_eli_act  legal_eli_act_text
legal_saos_search  legal_saos_judgment
legal_eurlex_act  legal_eurlex_text
legal_krs_extract  legal_vat_whitelist
web_scrape
```

They call public APIs (Sejm ELI, SAOS, CELLAR, KRS, MF whitelist) and the internal webscraper;
every LLM-supplied id is allow-listed before it reaches a URL, and `web_scrape` refuses
non-http(s), credentialed, non-standard-port, private/loopback/link-local and cluster-internal
URLs. Same `[retryable]`/`[permanent]` prefixes (`src/tools/legal/client.py`).

An `mcp` licence caller additionally sees `claude_*`, `claude_auth_*` and `tools_*`, which run
against this pod's `ANTHROPIC_API_KEY` and are never offered to a JWT or care-bus licence caller.

### Tool error prefixes

A failing care-bus tool returns MCP `isError` whose text starts with one of two prefixes
(`src/tools/carebus/client.py`). Branch on the prefix, not on the rest of the message:

| Prefix | Meaning | Caller action |
|---|---|---|
| `[retryable] ` | timeout (incl. `faq_ask`'s `FAQ_TIMEOUT_S`, 18 s), backend unreachable, backend 429 or 5xx | retry with backoff |
| `[permanent] ` | backend 4xx, invalid argument, a `*_URL` not set on the server, tool / resource / prompt not permitted | do not retry; surface it |

Backend base URLs are logged, never returned in the message. Every tool call writes one
`audit_logs` row with the caller's tenant, auth method and principal.

## Care-bus client notes

- The care-bus MCP client retries a 401 once with a fresh token (covers expiry). A 401
  that survives that retry is a configuration error (wrong client, issuer, audience, or the
  mapper missing) and should surface as such, not be retried further.
- The session is tenant-less (the tool-executor token has no `tenant_id` claim); the care-bus
  tools are tenant-agnostic. Tenant-scoped `/mcp/...` admin routes are not available to it at
  all (403).
