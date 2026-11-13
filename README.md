# NonAgenticAI executable catalog

The platform stores immutable executable tool/toolkit versions, authenticates binding-scoped MCP discovery, and implements a host credential broker, PostgreSQL queue and ephemeral Kata worker. UUIDv7 is required throughout. Runtime configuration is disabled by default; combined cluster acceptance and the coordinated rollout remain required before production enablement.


See the [publication contracts](docs/CATALOG_PUBLICATION.md) and [runtime implementation and rollout](docs/EXECUTION_RUNTIME.md). Schema head is `0006_tool_call_audit`.

## Current behavior

- **Builtin provider** (`src/builtin/`): main's 34 first-party care-bus/researcher tools run in-process for two non-binding callers, a care-bus tenant licence and a Keycloak service JWT. Each caller gets a fixed scope, every call is audited into `mcp_tool_call_audit`, and binding callers never see these tools. See [AUTHENTICATION_BINDINGS](docs/AUTHENTICATION_BINDINGS.md) and [caller contract](docs/caller-contract.md).

- Strict schema-version-1 manifests; UUIDv7 identities throughout; separate ownership and actor attribution; exact operation/service versions.
- Closed, bounded Draft 2020-12 JSON Schema subset; no external reference fetching or uploaded code imports.
- UTF-8 multi-file packages, canonical paths, golden SHA-256 identities, operator-approved image digests.
- Atomic draft snapshots, parent-row-serialized version allocation, expected-revision conflicts, source attribution for copies.
- Database guards protect all snapshot content, including drafts. Lifecycle changes update transactional catalog generation. Tenant operations always require explicit ownership. A separate internal platform path manages service/operation contracts and sharing grants.
- Shared bounded license introspection, immutable request principals, tenant-allowlisted operator keys and pure ASGI authorization on every `/mcp` request.
- Immutable toolkit versions, conjunctive selection, credential aliases, and tenant-scoped binding issuance, revision checks, one-time token return, rotation/revocation and durable session identity.
- Authenticated complete-package tool authoring/publication with transactional exact-version audit; metadata-only listings and tenant-owned package inspection.
- Stateless Streamable HTTP at `/mcp` and `/mcp/`, exact schema-backed tools, bounded signed pagination and live binding authorization at list/lookup/call.
- One ASGI application with `/health`, `/health/live`, and `/health/ready`. Readiness checks the exact revision and catalog without DDL; missing/old schemas return bounded 503 responses. Probes report whether execution acceptance is configured. The default configuration rejects execution and creates no pods.

Legacy tool CRUD, version/backup/audit routes, arithmetic/Claude tools, OAuth/token issuance, gateway, trusted-network bypass, default tenants, and startup schema creation remain removed. `/token` returns 404. Unauthorized `/mcp` requests return 401. The authenticated protocol exposes only binding-selected schemas; with default disabled execution, valid calls return an MCP tool error with `execution_unavailable`. Tool/toolkit/profile/binding management requires explicit operator authorization.

- Durable binding-owned runs, fenced leases, fair global/tenant capacity, idempotency, cancellation, API journal and quarantine/recovery.
- Worker-only credential bundles and a scoped HTTP broker; one non-root/no-network Kata pod per invocation with bounded attach protocol.
- Separate limited platform service/operation authoring, explicit draft OpenAPI import and internal worker probes/metrics.

## Development

Python 3.14, PostgreSQL 18, and uv are required. Explicitly configure a database; there is no host default and no automatic `.env` loading.

```bash
uv sync --frozen --extra worker
# Set DATABASE_URL to an EMPTY, dedicated development database using the migration role.
uv run alembic upgrade head
# Grant a separately created runtime role; configure its password through your secret store.
uv run python -m scripts.grant_runtime_role --role mcp_runtime
# Configure license authentication, the mounted admin-key path and tenant allowlist
# as described in docs/AUTHENTICATION_BINDINGS.md and .env.example.
# Change DATABASE_URL to the runtime role's connection URL.
uv run python -m src.run_mcp
```

The equivalent container command is `uvicorn src.asgi:app --host 0.0.0.0 --port 8033`. The image includes Alembic and all five catalog/access/publication/runtime/control revisions. It contains no Node or Claude CLI. Package content is never executed by the API.

[.env.example](.env.example) lists catalog and authentication settings. All limits must be positive, the connection pool is bounded, and no runner image is approved by default. See [developer guidance](docs/DEVELOPER_GUIDE.md) and [contract details](docs/CATALOG_CONTRACTS.md).

Compose starts a private database and a non-destructive migration container. Set `MCP_LOCAL_DB_PASSWORD`, run `docker compose up --build migrate`, create/grant the runtime role in that database, then set `MCP_RUNTIME_DATABASE_URL`, license settings, the admin-key file and tenant allowlist and run `docker compose up mcp`. Compose binds the API to loopback. The PostgreSQL volume uses the PostgreSQL 18 directory layout and a fresh volume name.

## Reset and release hold

The only supported chain is `0001_executable_catalog` → `0002_toolkits_bindings` → `0003_catalog_publication` → `0004_execution_runtime` → `0005_execution_controls` → `0006_tool_call_audit`. Old revision IDs are intentionally unrecognized. Do not stamp an old database or attempt to upgrade its rows into the new contracts. Existing development data is discarded once during the coordinated rollout; the recurring migration command never resets data.

**Automatic image publication and deployment are held in CI.** The existing GitOps migration init container would otherwise run the fresh chain against the old database. Implementation tests reset only disposable databases; they do not reset the deployed database.

## Checks

```bash
uv run --extra worker ruff check src/ runner/ tests/ migrations/ scripts/
uv run --extra worker ruff format --check src/ runner/ tests/ migrations/ scripts/
uv run ty check .
uv run --extra worker pytest tests/ --cov=src --cov=runner --cov-report=term-missing
scripts/pip-audit.sh
uv run --extra worker bandit -r src/ runner/ scripts/ -ll
git diff --check
```

The required coverage threshold remains 85%. Database tests automatically provision a disposable `postgres:18` container, create isolated databases, and remove them afterward. An explicit `TEST_DATABASE_URL` or `TEST_POSTGRES_URL` may supply a test server with database-create and role-create permissions; only randomly named databases created by this suite are dropped. Runtime `DATABASE_URL` is never consulted for tests. Unavailable database infrastructure fails the suite rather than skipping it. Worktrees are excluded from the image and type-check scope.

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
