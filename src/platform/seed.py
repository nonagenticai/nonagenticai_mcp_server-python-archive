"""Idempotent, content-addressed publication of the platform toolkits (``python -m src.platform.seed``).

Run by the migrate init container after ``alembic upgrade head``. For each toolkit under
``toolkits/`` it publishes, under ``settings.platform_tenant_id`` and in dependency order,
service -> operations -> tools -> toolkit. Every artifact id is fixed in the repository; the
version is assigned here by content: the full manifest (including resolved exact references and
the runner image digest) is compared with the latest version of that id.

* identical and published   -> no-op
* identical and draft       -> publish (an earlier run died between create and publish)
* different (or absent)     -> append ``max+1`` (``1``) and publish

Older versions stay published, so existing bindings keep working until they are rotated.
External toolkits (no service.json: tools with an external runtime, no Kata package) need no
runner image and are seeded whether or not the digest is set.
Without ``MCP_RUNNER_IMAGE_DIGEST`` only services and operations are seeded: a tool manifest
pins the digest, so tools and toolkits wait for it. A digest outside
``MCP_APPROVED_RUNNER_IMAGES`` is a hard error. The whole run holds one advisory lock, so
concurrent migrate containers (rolling update, two replicas) serialize.
"""

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_json

from src.catalog.models import (
    Attribution,
    ExactReference,
    Name,
    OperationManifest,
    ServiceManifest,
    ToolManifest,
)
from src.catalog.package import PackageFile, package_digest
from src.catalog.service import CatalogService, PlatformCatalogService
from src.config import Settings
from src.storage.bindings import BindingRepository, latest_toolkit
from src.storage.catalog import CatalogRepository, PlatformCatalogRepository
from src.storage.database import Database
from src.toolkits.models import ToolkitManifest, ToolReference, ToolSelection
from src.toolkits.service import AccessService

log = logging.getLogger("mcp.platform.seed")

#: Audit rows require a licence identity; the seed has none, so it uses this fixed marker.
SEED_LICENCE_ID = UUID("01900000-0000-7000-8000-00000000f002")
ACTOR = Attribution(subject="platform-seed", license_id=SEED_LICENCE_ID)
#: Arbitrary constant naming the seed's advisory lock ("MCPSEED").
LOCK_KEY = 0x4D4350_53454544
LOCK_WAIT_SECONDS = 300.0
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "toolkits"


class SeedError(RuntimeError):
    """The platform catalog cannot be brought to the repository's state."""


@dataclass
class SeedReport:
    #: "<kind>:<name>" -> "created" | "appended" | "published" | "unchanged"
    actions: dict[str, str] = field(default_factory=dict)
    #: "<kind>:<name>" -> the version that is now published (or pending) for the artifact
    versions: dict[str, int] = field(default_factory=dict)
    tools_skipped: bool = False
    #: toolkits left unseeded because their service's source is not registered and active
    sources_missing: dict[str, str] = field(default_factory=dict)

    def count(self, action: str) -> int:
        return sum(1 for a in self.actions.values() if a == action)

    @property
    def changed(self) -> bool:
        return any(a != "unchanged" for a in self.actions.values())


def fields_of(value: Any) -> dict[str, Any]:
    """Source parts arrive as plain dicts (or already-validated models); normalise to JSON dicts."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(dict(value)))


def same(stored: BaseModel, candidate: BaseModel) -> bool:
    """Content equality ignoring only the version the seed assigns."""
    skip = {"version"}
    return stored.model_dump(mode="json", exclude=skip) == candidate.model_dump(
        mode="json", exclude=skip
    )


def build(model: type[BaseModel], **values: Any) -> Any:
    """Validate through JSON, the way every stored manifest is (the contracts are strict)."""
    return model.model_validate_json(to_json(values))


def parse(model: type[BaseModel], value: Any) -> Any:
    return model.model_validate_json(json.dumps(value))


class Seeder:
    def __init__(
        self,
        pool,
        settings: Settings,
        package_files: Callable[..., Sequence[Mapping[str, str]]] | None = None,
        platform_pool=None,
        strict_sources: bool = False,
    ) -> None:
        self.pool = pool
        self.settings = settings
        self.tenant = settings.platform_tenant_id
        #: Services and operations are platform contracts: the HTTP import writes them with the
        #: platform role's pool, tools and toolkits with the tenant pool.
        self.platform_pool = platform_pool or pool
        self.strict_sources = strict_sources
        self.platform = PlatformCatalogRepository(self.platform_pool)
        self.catalog = CatalogService(CatalogRepository(pool), settings)
        self.platform_service = PlatformCatalogService(self.platform, self.catalog)
        self.access = AccessService(BindingRepository(pool), settings)
        self.report = SeedReport()
        if package_files is None:
            from src.platform.toolkit_source import package_files as loader_package_files

            package_files = loader_package_files
        self.package_files = package_files

    def note(self, kind: str, name: str, action: str, version: int | None = None) -> None:
        self.report.actions[f"{kind}:{name}"] = action
        if version is not None:
            self.report.versions[f"{kind}:{name}"] = version
        log.info("%s %s: %s", kind, name, action)

    # -- services and operations ------------------------------------------------------------

    async def service(self, name: str, source: Any) -> ExactReference:
        values = fields_of(source) | {"version": 1}
        candidate = build(ServiceManifest, **values)
        latest = await self.platform.latest_service(candidate.service_id)
        ref = ExactReference(id=candidate.service_id, version=1)
        if latest is None:
            await self.platform_service.add_service(candidate, ACTOR)
            await self.platform.transition("service", ref, "published", ACTOR)
            self.note("service", name, "created", 1)
            return ref
        ref = ExactReference(id=candidate.service_id, version=latest["version"])
        if same(parse(ServiceManifest, latest["manifest"]), candidate):
            return await self.settle("service", name, ref, latest["status"])
        ref = ExactReference(id=candidate.service_id, version=latest["version"] + 1)
        await self.platform_service.add_service(
            candidate.model_copy(update={"version": ref.version}),
            ACTOR,
            expected_revision=latest["revision"],
        )
        await self.platform.transition("service", ref, "published", ACTOR)
        self.note("service", name, "appended", ref.version)
        return ref

    async def operation(
        self, name: str, source: Any, service: ExactReference, service_name: str
    ) -> ExactReference:
        values = fields_of(source) | {"version": 1, "service": service.model_dump(mode="json")}
        candidate = build(OperationManifest, **values)
        latest = await self.platform.latest_operation(candidate.operation_id)
        label = f"{service_name}.{name}"
        if latest is None:
            ref = ExactReference(id=candidate.operation_id, version=1)
            await self.platform_service.add_operation(candidate, ACTOR)
            await self.platform.transition("operation", ref, "published", ACTOR)
            self.note("operation", label, "created", 1)
            return ref
        ref = ExactReference(id=candidate.operation_id, version=latest["version"])
        if same(parse(OperationManifest, latest["manifest"]), candidate):
            return await self.settle("operation", label, ref, latest["status"])
        ref = ExactReference(id=candidate.operation_id, version=latest["version"] + 1)
        await self.platform_service.add_operation(
            candidate.model_copy(update={"version": ref.version}), ACTOR
        )
        await self.platform.transition("operation", ref, "published", ACTOR)
        self.note("operation", label, "appended", ref.version)
        return ref

    async def settle(
        self, kind: str, name: str, ref: ExactReference, status: str
    ) -> ExactReference:
        """The identical version already exists: publish a draft, accept a published one."""
        if status == "draft":
            await self.platform.transition(kind, ref, "published", ACTOR)
            self.note(kind, name, "published", ref.version)
        elif status == "published":
            self.note(kind, name, "unchanged", ref.version)
        else:
            raise SeedError(
                f"{kind} {name} v{ref.version} is {status} in the catalog but is the repository's "
                "current content; a human disabled it, so the seed will not resurrect it"
            )
        return ref

    # -- tools and toolkits -------------------------------------------------------------------

    async def tool(
        self,
        toolkit: Any,
        source: Any,
        refs: Mapping[str, ExactReference],
        digest: str | None,
    ) -> ToolReference:
        files = tuple(
            PackageFile(path=f["path"], content=f["content"])
            for f in self.package_files(
                toolkit, source, {k: (r.id, r.version) for k, r in refs.items()}
            )
        )
        values = fields_of(source.fields)
        if getattr(toolkit, "service", True) is None:
            digest = None  # external tools run in a service of their own, not in a runner image
        for key in ("tenant_id", "version", "package_sha256", "runner_image_digest"):
            values.pop(key, None)
        values.pop("allowed_operations", None)
        try:
            allowed = tuple(refs[key] for key in source.op_keys)
        except KeyError as error:
            raise SeedError(f"tool {source.name} uses unknown operation {error}") from None
        candidate = build(
            ToolManifest,
            **values,
            tenant_id=self.tenant,
            version=1,
            package_sha256=package_digest(files),
            runner_image_digest=digest,
            allowed_operations=allowed,
        )
        label = f"{toolkit.name}.{source.name}"
        latest = await self.catalog.repository.latest_version(self.tenant, candidate.tool_id)
        if latest is None:
            created = await self.catalog.create_draft(self.tenant, candidate, files, ACTOR)
            await self.catalog.transition(
                self.tenant,
                candidate.tool_id,
                1,
                "published",
                ACTOR,
                expected_revision=created.revision,
            )
            self.note("tool", label, "created", 1)
            return ToolReference(tenant_id=self.tenant, id=candidate.tool_id, version=1)
        if same(latest.manifest, candidate):
            if latest.status == "draft":
                await self.catalog.transition(
                    self.tenant,
                    candidate.tool_id,
                    latest.version,
                    "published",
                    ACTOR,
                    expected_revision=latest.revision,
                )
                self.note("tool", label, "published", latest.version)
            elif latest.status == "published":
                self.note("tool", label, "unchanged", latest.version)
            else:
                raise SeedError(f"tool {label} v{latest.version} is {latest.status} in the catalog")
            return ToolReference(
                tenant_id=self.tenant, id=candidate.tool_id, version=latest.version
            )
        version = latest.version + 1
        appended = await self.catalog.append_draft(
            self.tenant,
            candidate.tool_id,
            candidate.model_copy(update={"version": version}),
            files,
            ACTOR,
            expected_revision=latest.revision,
            exact_version=True,
        )
        await self.catalog.transition(
            self.tenant,
            candidate.tool_id,
            version,
            "published",
            ACTOR,
            expected_revision=appended.revision,
        )
        self.note("tool", label, "appended", version)
        return ToolReference(tenant_id=self.tenant, id=candidate.tool_id, version=version)

    async def toolkit(self, source: Any, tools: Sequence[tuple[ToolReference, ToolManifest]]):
        values = fields_of(source.toolkit)
        for key in ("tenant_id", "version", "tools"):
            values.pop(key, None)
        candidate = build(
            ToolkitManifest,
            **values,
            tenant_id=self.tenant,
            version=1,
            tools=tuple(ToolSelection(tool=ref, limits=manifest.limits) for ref, manifest in tools),
        )
        async with self.pool.connection() as conn, conn.transaction():
            latest = await latest_toolkit(conn, self.tenant, candidate.toolkit_id)
            if latest is None:
                draft = await self.access.write_toolkit(conn, self.tenant, ACTOR, candidate)
                await self.access.transition_toolkit(
                    conn, self.tenant, ACTOR, candidate.toolkit_id, 1, "published", draft.revision
                )
                self.note("toolkit", source.name, "created", 1)
            elif same(latest.manifest, candidate):
                if latest.status == "draft":
                    await self.access.transition_toolkit(
                        conn,
                        self.tenant,
                        ACTOR,
                        candidate.toolkit_id,
                        latest.manifest.version,
                        "published",
                        latest.revision,
                    )
                    self.note("toolkit", source.name, "published", latest.manifest.version)
                elif latest.status == "published":
                    self.note("toolkit", source.name, "unchanged", latest.manifest.version)
                else:
                    raise SeedError(f"toolkit {source.name} is {latest.status} in the catalog")
            else:
                draft = await self.access.write_toolkit(
                    conn, self.tenant, ACTOR, candidate, latest.revision
                )
                await self.access.transition_toolkit(
                    conn,
                    self.tenant,
                    ACTOR,
                    candidate.toolkit_id,
                    draft.manifest.version,
                    "published",
                    draft.revision,
                )
                self.note("toolkit", source.name, "appended", draft.manifest.version)

    async def source_active(self, key: str) -> bool:
        async with self.platform_pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT status FROM public.mcp_sources "
                "WHERE owner_tenant_id=public.mcp_platform_tenant() AND source_key=%s",
                (key,),
            )
            row = await cursor.fetchone()
        return row is not None and row["status"] == "active"

    async def run(self, sources: Sequence[Any]) -> SeedReport:
        digest = self.settings.runner_image_digest
        if digest and digest not in self.settings.approved_runner_images:
            raise SeedError("MCP_RUNNER_IMAGE_DIGEST is not in MCP_APPROVED_RUNNER_IMAGES")
        if not digest:
            self.report.tools_skipped = True
            log.warning(
                "MCP_RUNNER_IMAGE_DIGEST is unset: seeding services and operations only; "
                "tools and toolkits are skipped"
            )
        for source in sources:
            if source.service is None:
                await self.publish_toolkit(source, {}, None)
                continue
            service_fields = fields_of(source.service)
            service_name = service_fields["service_key"]
            if not await self.source_active(service_fields["destination_alias"]):
                if self.strict_sources:
                    raise SeedError(
                        f"source {service_fields['destination_alias']} is not registered and active"
                    )
                self.report.sources_missing[source.name] = service_fields["destination_alias"]
                log.warning(
                    "toolkit %s skipped: source %s is not registered and active (POST /mcp/sources)",
                    source.name,
                    service_fields["destination_alias"],
                )
                continue
            service = await self.service(service_name, source.service)
            refs = {
                key: await self.operation(key, op, service, service_name)
                for key, op in source.operations.items()
            }
            if not digest:
                continue
            await self.publish_toolkit(source, refs, digest)
        return self.report

    async def publish_toolkit(
        self, source: Any, refs: Mapping[str, ExactReference], digest: str | None
    ) -> None:
        published: list[tuple[ToolReference, ToolManifest]] = []
        for tool in source.tools:
            ref = await self.tool(source, tool, refs, digest)
            stored = await self.catalog.repository.get_version(self.tenant, ref.id, ref.version)
            published.append((ref, stored.manifest))
        await self.toolkit(source, published)


async def acquire_lock(conn, timeout: float = LOCK_WAIT_SECONDS) -> None:
    """Session advisory lock by polling: the pool's statement_timeout must not abort a wait."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK_KEY,))
        row = await cur.fetchone()
        await conn.commit()
        if row and row["ok"]:
            return
        if loop.time() >= deadline:
            raise SeedError("another platform seed holds the lock")
        await asyncio.sleep(0.2)


async def seed(
    pool,
    settings: Settings,
    sources: Sequence[Any],
    package_files: Callable[..., Sequence[Mapping[str, str]]] | None = None,
    lock_timeout: float = LOCK_WAIT_SECONDS,
    *,
    platform_pool=None,
    strict_sources: bool = False,
) -> SeedReport:
    async with pool.connection() as lock:
        await acquire_lock(lock, lock_timeout)
        try:
            return await Seeder(pool, settings, package_files, platform_pool, strict_sources).run(
                sources
            )
        finally:
            try:
                await lock.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                await lock.commit()
            except Exception:
                await lock.close()  # the session ends, which releases the lock


# -- generic import ------------------------------------------------------------------------------


class BundleFile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    path: Annotated[str, Field(min_length=1, max_length=256)]
    content: Annotated[str, Field(max_length=262144)]


class BundleTool(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    #: ToolManifest fields except tenant_id, version, package_sha256, runner_image_digest and
    #: allowed_operations.
    fields: dict[str, Any]
    #: keys into the bundle's ``operations``
    op_keys: Annotated[tuple[Name, ...], Field(min_length=1, max_length=64)]
    files: Annotated[tuple[BundleFile, ...], Field(min_length=1, max_length=64)]

    @property
    def name(self) -> str:
        return str(self.fields.get("public_name", ""))


class ToolkitBundle(BaseModel):
    """A toolkit as plain JSON: the same parts as ``toolkits/<tk>/`` in the repository."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    toolkit: dict[str, Any]
    service: dict[str, Any]
    operations: Annotated[dict[Name, dict[str, Any]], Field(min_length=1, max_length=128)]
    tools: Annotated[tuple[BundleTool, ...], Field(min_length=1, max_length=64)]

    @property
    def name(self) -> str:
        return str(self.toolkit.get("name", ""))

    @model_validator(mode="after")
    def consistent(self):
        if any(f.path in {"ops.py"} for t in self.tools for f in t.files):
            raise ValueError("ops.py is generated")
        if len({t.name for t in self.tools}) != len(self.tools):
            raise ValueError("duplicate tool name")
        for tool in self.tools:
            if any(key not in self.operations for key in tool.op_keys):
                raise ValueError("tool uses an unknown operation key")
            if len({f.path for f in tool.files}) != len(tool.files):
                raise ValueError("duplicate package file")
        return self


def bundle_package_files(_toolkit, tool: BundleTool, op_refs) -> list[dict[str, str]]:
    """The bundle's files plus the generated ``ops.py`` (operation id and exact version per key)."""
    table = {k: [str(op_refs[k][0]), op_refs[k][1]] for k in sorted(tool.op_keys)}
    files = {f.path: f.content for f in tool.files}
    files["ops.py"] = (
        "# Generated: operation id and exact version per key.\nOPS = "
        + json.dumps(table, indent=2, sort_keys=True)
        + "\n"
    )
    return [{"path": p, "content": files[p]} for p in sorted(files)]


async def import_bundle(
    pool, settings: Settings, bundle: ToolkitBundle, platform_pool=None
) -> SeedReport:
    """Publish one toolkit bundle under the platform tenant through the same content-addressed
    path as ``python -m src.platform.seed``. A missing or disabled source is an error here."""
    return await seed(
        pool,
        settings,
        [bundle],
        bundle_package_files,
        platform_pool=platform_pool,
        strict_sources=True,
    )


async def main() -> int:
    from src.platform.toolkit_source import load_all

    settings = Settings()  # ty: ignore[missing-argument]
    sources = load_all(DEFAULT_ROOT)
    database = Database(settings)
    await database.open()
    try:
        await database.pool.wait(timeout=30)
        report = await seed(database.pool, settings, sources)
    except SeedError as error:
        log.error("platform seed failed: %s", error)
        return 1
    finally:
        await database.close()
    log.info(
        "platform seed done: %d changed, %d unchanged%s",
        len(report.actions) - report.count("unchanged"),
        report.count("unchanged"),
        " (tools skipped: no runner image digest)" if report.tools_skipped else "",
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(asyncio.run(main()))
