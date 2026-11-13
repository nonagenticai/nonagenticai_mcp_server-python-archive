# Catalog contract v1

All stored manifests use `schema_version: 1`, forbidden unknown fields, positive 32-bit artifact versions, exact UUID/version references, and explicit ownership. Lifecycle status, creator/update attribution, timestamps, source version, and identity revision are stored separately from manifest content. Alembic's `0001_executable_catalog` is a different version domain.

Services are operator-managed: a stable key, configured destination alias, application key, explicit license authentication adapter, credential-profile key, and request/response ceilings. Operations fix service/version, method/path template, declared query/header fields, typed input/context/literal mappings, scope rules, input/output schemas, projection/redaction, effect, retries/idempotency, timeout, pagination and response ceilings. Mutation retries require downstream idempotency. Context equality, set membership, numeric bounds, relative-path containment and exact approved ownership reads are the only constraint types. These models validate syntax; runtime policy evaluation is deferred.

Tools fix tenant/tool/version/public name, description, `kata_python`, entrypoint, input/result/context schemas, allowed exact operations, narrowing constraints, package digest, approved immutable image digest, effect and bounded limits. HTTP appends must name exactly MAX(version)+1, checked under the same parent lock as allocation. Internal copy helpers allocate the next version from a template. Read-only tools cannot grant write operations. Publication revalidates packages/contracts and requires all exact operation/service dependencies to be published. Disabling dependencies increments generation; the live catalog projection consumes that generation and denies disabled dependencies. Future brokers must enforce the same boundary.

## Package identity

The wire representation is an ordered collection of `{path, content}` text entries, not a map or archive. Duplicate paths therefore remain detectable. Entries must be UTF-8 and relative canonical NFC POSIX paths of at most 512 UTF-8 bytes. Absolute/drive/UNC paths, backslashes, colons, control characters, empty/dot/traversal segments, normalization aliases, duplicate paths, links and file/directory collisions are rejected. Entrypoints use ASCII Python `package.module:function` syntax and require the corresponding `.py` file; validation never imports or executes it. Dependencies are supplied by an approved runner image in a future stage.

SHA-256 input is UTF-8 JSON of sorted `[path, content]` pairs, sorted by canonical path, using compact `,`/`:` separators, `ensure_ascii=False`, no NaN, no trailing newline. Content is preserved exactly, including whitespace, newlines, and Unicode. No manifest fields are folded into this digest; the immutable manifest independently fixes entrypoint, image and limits. [Golden fixtures](../tests/fixtures/package-golden.json) are the shared runner contract.

Defaults: 64 files, 256 KiB per file, 1 MiB total content. File paths have their own byte bound. Unsupported link/archive metadata fails strict entry validation; no archive extraction occurs.

## JSON Schema subset

Draft 2020-12 is the sole dialect. Bundle-local `#/$defs/...` JSON Pointers resolve against the containing schema. There is no external resolver, network fetch, `$id`, dynamic reference, recursion, regex, `format`, custom keyword or uploaded expression evaluation.

Supported keywords: `$schema`, `$defs`, `$ref`, `type`, `properties`, `required`, `additionalProperties`, `items`, `prefixItems`, `minItems`, `maxItems`, `minProperties`, `maxProperties`, `minLength`, `maxLength`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `enum`, `const`, `allOf`, `anyOf`, `oneOf`, `not`, `title`, `description`, `default`. Each branch array has 1–8 entries. Boolean subschemas are supported; roots are objects.

Defaults: 64 KiB/schema, depth 16, 1,024 raw/expanded nodes, 32 expanded references; instances 1 MiB, depth 32 and 10,000 nodes. Expanded schema work times instance nodes must be at most 100,000. The same configured ceilings apply to all schemas. A closed registry is also passed to the validator. Unsupported or over-budget schemas fail before instance validation. Tests assert external resolution cannot perform network access.

## UUID identities

All UUID identity/reference fields in the clean replacement require UUIDv7, including tenant, service, operation, tool, attribution license and later access objects. Generate IDs with Python 3.14 `uuid.uuid7()`. Strict JSON contracts and database constraints reject other UUID versions; fixtures are synthetic UUIDv7 values. No legacy ID mapping or data conversion is supported. See [phase 2](AUTHENTICATION_BINDINGS.md).

Tenant HTTP payloads, public-schema restrictions, exact-version audit and run-envelope composition are documented in [catalog publication](CATALOG_PUBLICATION.md). Reserved run-control names cannot be authored.
