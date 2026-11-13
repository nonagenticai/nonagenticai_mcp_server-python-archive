# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-11-13

First public release.

### Added

- Versioned executable catalogue: immutable tool and toolkit versions, UUIDv7 identities
  throughout, atomic draft snapshots and parent-row-serialised version allocation.
- Strict schema-version-1 manifests validated against a closed, bounded Draft 2020-12 JSON
  Schema subset — no external reference fetching and no uploaded code imports.
- Stateless Streamable HTTP MCP endpoint at `/mcp`, with binding-scoped discovery: a caller
  sees and calls only the tools its binding selects, authorised on every request.
- Host credential broker and a scoped HTTP broker for worker invocations.
- Durable run queue with fenced leases, fair global and per-tenant capacity, idempotency,
  cancellation and quarantine/recovery.
- One non-root, network-isolated Kata pod per invocation, with a bounded attach protocol.
- Per-call audit log in `mcp_tool_call_audit`; an audit write failure never fails the call.
- Separate operator paths for service, operation and binding management, requiring explicit
  authorisation.
- Health endpoints `/health`, `/health/live` and `/health/ready`, where readiness checks the
  exact schema revision and catalogue without running DDL.
- Alembic migrations to schema head `0006_tool_call_audit`.

### Security

- Execution is disabled by default: the default configuration rejects execution and creates
  no pods.
- Keycloak service JWTs and tenant licences are authenticated per binding; there is no
  default tenant and no trusted-network bypass.
- Startup never creates schema; a missing or old schema returns a bounded 503.
