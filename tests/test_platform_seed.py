"""Platform toolkit seed on real PostgreSQL: content-addressed, idempotent, serialized."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from src.config import PLATFORM_TENANT_ID
from src.platform.seed import ACTOR, LOCK_KEY, Seeder, SeedError, build, seed
from src.storage.bindings import BindingRepository
from src.storage.bindings_ensure import BindingEnsureService
from src.toolkits.service import AccessService
from tests.fixtures import IMAGE, register_source, settings

FIXTURE = Path(__file__).parent / "fixtures" / "platform_toolkit"
TOOL_ID = UUID("20000000-0000-7000-8000-00000000d001")
TOOLKIT_ID = UUID("01900000-0000-7000-8000-00000000d001")


@dataclass(frozen=True)
class Tool:
    name: str
    fields: dict
    op_keys: tuple[str, ...]
    files: dict[str, str]


@dataclass(frozen=True)
class Kit:
    name: str
    toolkit: dict
    service: dict
    operations: dict
    tools: tuple[Tool, ...]


def load(edit: Any = None) -> tuple[Kit, ...]:
    """The fixture tree in the loader's shape; ``edit(kit_dict)`` mutates the parsed JSON."""
    out = []
    for root in sorted(p for p in FIXTURE.iterdir() if (p / "toolkit.json").exists()):
        read = lambda p: json.loads(p.read_text())  # noqa: E731
        ops = {p.stem: read(p) for p in sorted((root / "operations").glob("*.json"))}
        tools = []
        for d in sorted(p for p in (root / "tools").iterdir() if p.is_dir()):
            fields = read(d / "tool.json")
            keys = tuple(fields.pop("allowed_operations"))
            files = {p.name: p.read_text() for p in d.glob("*.py")}
            tools.append(Tool(d.name, fields, keys, files))
        parts = {
            "toolkit": read(root / "toolkit.json"),
            "service": read(root / "service.json"),
            "operations": ops,
            "tools": tools,
        }
        if edit:
            edit(parts)
        out.append(
            Kit(
                root.name,
                parts["toolkit"],
                parts["service"],
                parts["operations"],
                tuple(parts["tools"]),
            )
        )
    return tuple(out)


def package_files(kit, tool, op_refs):
    ops = {k: [str(i), v] for k, (i, v) in sorted(op_refs.items())}
    return [
        *({"path": n, "content": c} for n, c in sorted(tool.files.items())),
        {"path": "ops.py", "content": "OPS = " + json.dumps(ops, sort_keys=True) + "\n"},
    ]


@pytest.fixture(autouse=True)
async def demo_source(database):
    """The fixture service names source ``demo_backend``; sources are registered at runtime."""
    await register_source(database.pool, "demo_backend", app_key="demo")


def cfg(**overrides):
    return settings(runner_image_digest=IMAGE, **overrides)


async def rows(pool, query, *args):
    async with pool.connection() as conn:
        return await (await conn.execute(query, args)).fetchall()


async def versions(pool, table, column, identity):
    return [
        (r["version"], r["status"])
        for r in await rows(
            pool,
            f"SELECT version,status FROM public.{table} WHERE {column}=%s ORDER BY version",
            identity,
        )
    ]


async def revisions(pool):
    tool = await rows(pool, "SELECT revision FROM public.mcp_tools WHERE tool_id=%s", TOOL_ID)
    kit = await rows(
        pool, "SELECT revision FROM public.mcp_toolkits WHERE toolkit_id=%s", TOOLKIT_ID
    )
    return tool[0]["revision"], kit[0]["revision"]


async def test_seed_publishes_everything_and_is_idempotent(database):
    pool = database.pool
    first = await seed(pool, cfg(), load(), package_files)
    assert first.count("created") == 5  # service, 2 operations, 1 tool, 1 toolkit
    assert not first.tools_skipped
    assert await versions(pool, "mcp_tool_versions", "tool_id", TOOL_ID) == [(1, "published")]
    assert await versions(pool, "mcp_toolkit_versions", "toolkit_id", TOOLKIT_ID) == [
        (1, "published")
    ]
    before = await revisions(pool)
    events = await rows(pool, "SELECT count(*) AS n FROM public.mcp_access_events")

    second = await seed(pool, cfg(), load(), package_files)
    assert not second.changed
    assert second.count("unchanged") == 5
    assert await revisions(pool) == before
    assert await rows(pool, "SELECT count(*) AS n FROM public.mcp_access_events") == events


async def test_toolkit_selection_carries_the_tools_own_limits(database):
    await seed(database.pool, cfg(), load(), package_files)
    (row,) = await rows(
        database.pool,
        "SELECT manifest FROM public.mcp_toolkit_versions WHERE toolkit_id=%s",
        TOOLKIT_ID,
    )
    (selection,) = row["manifest"]["tools"]
    assert selection["limits"]["wall_seconds"] == 30
    assert selection["limits"]["api_calls"] == 5
    assert selection["tool"]["tenant_id"] == str(PLATFORM_TENANT_ID)


async def test_edited_tool_content_appends_v2_and_keeps_v1(database):
    pool = database.pool
    await seed(pool, cfg(), load(), package_files)

    def edit(parts):
        tool = parts["tools"][0]
        parts["tools"][0] = Tool(
            tool.name, tool.fields, tool.op_keys, {"main.py": tool.files["main.py"] + "# v2\n"}
        )

    report = await seed(pool, cfg(), load(edit), package_files)
    assert report.actions["tool:demo.get_item"] == "appended"
    assert report.actions["toolkit:demo"] == "appended"
    assert report.actions["service:demo"] == "unchanged"
    assert await versions(pool, "mcp_tool_versions", "tool_id", TOOL_ID) == [
        (1, "published"),
        (2, "published"),
    ]
    assert await versions(pool, "mcp_toolkit_versions", "toolkit_id", TOOLKIT_ID) == [
        (1, "published"),
        (2, "published"),
    ]
    again = await seed(pool, cfg(), load(edit), package_files)
    assert not again.changed


async def test_edited_operation_cascades_to_tool_and_toolkit(database):
    pool = database.pool
    await seed(pool, cfg(), load(), package_files)

    def edit(parts):
        parts["operations"]["list_items"]["timeout_seconds"] = 20

    report = await seed(pool, cfg(), load(edit), package_files)
    assert report.actions["operation:demo.list_items"] == "appended"
    assert report.actions["operation:demo.get_item"] == "unchanged"
    assert report.actions["tool:demo.get_item"] == "appended"
    assert report.actions["toolkit:demo"] == "appended"
    op = UUID("40000000-0000-7000-8000-00000000d002")
    assert await versions(pool, "mcp_operation_versions", "operation_id", op) == [
        (1, "published"),
        (2, "published"),
    ]


async def test_edited_call_budget_republishes_operation_tool_and_toolkit(database):
    """pagination_limit is manifest content: changing it re-versions the operation and cascades."""
    pool = database.pool
    await seed(pool, cfg(), load(), package_files)

    def edit(parts):
        parts["operations"]["list_items"]["pagination_limit"] = 3

    report = await seed(pool, cfg(), load(edit), package_files)
    assert report.actions["operation:demo.list_items"] == "appended"
    assert report.actions["tool:demo.get_item"] == "appended"
    assert report.actions["toolkit:demo"] == "appended"
    assert not (await seed(pool, cfg(), load(edit), package_files)).changed


async def test_edited_service_cascades_through_operations(database):
    pool = database.pool
    await seed(pool, cfg(), load(), package_files)

    def edit(parts):
        parts["service"]["max_response_bytes"] = 16384

    report = await seed(pool, cfg(), load(edit), package_files)
    assert report.actions["service:demo"] == "appended"
    assert report.actions["operation:demo.get_item"] == "appended"
    assert report.actions["tool:demo.get_item"] == "appended"
    assert report.actions["toolkit:demo"] == "appended"


async def test_unset_digest_seeds_service_and_operations_only(database, caplog):
    pool = database.pool
    with caplog.at_level("WARNING", logger="mcp.platform.seed"):
        report = await seed(pool, settings(), load(), package_files)
    assert report.tools_skipped
    assert report.count("created") == 3
    assert "tools and toolkits are skipped" in caplog.text
    assert await rows(pool, "SELECT 1 FROM public.mcp_tools") == []
    assert await rows(pool, "SELECT 1 FROM public.mcp_toolkits") == []
    # A later run with the digest completes the rest and leaves the earlier artifacts alone.
    later = await seed(pool, cfg(), load(), package_files)
    assert later.actions["service:demo"] == "unchanged"
    assert later.actions["tool:demo.get_item"] == "created"


async def test_unapproved_digest_is_refused(database):
    with pytest.raises(SeedError, match="APPROVED_RUNNER_IMAGES"):
        await seed(
            database.pool,
            settings(runner_image_digest="registry.example/other@sha256:" + "b" * 64),
            load(),
            package_files,
        )
    assert await rows(database.pool, "SELECT 1 FROM public.mcp_services") == []


async def test_a_draft_left_by_a_dead_run_is_published(database):
    pool = database.pool
    seeder = Seeder(pool, cfg(), package_files)
    from src.catalog.models import ServiceManifest

    (kit,) = load()
    await seeder.platform_service.add_service(
        build(ServiceManifest, **kit.service, version=1), ACTOR
    )
    assert await versions(
        pool, "mcp_service_versions", "service_id", UUID(kit.service["service_id"])
    ) == [(1, "draft")]
    report = await seed(pool, cfg(), load(), package_files)
    assert report.actions["service:demo"] == "published"
    assert await versions(
        pool, "mcp_service_versions", "service_id", UUID(kit.service["service_id"])
    ) == [(1, "published")]


async def test_binding_ensure_discovers_the_seeded_toolkit(database):
    pool = database.pool
    await seed(pool, cfg(), load(), package_files)
    ensure = BindingEnsureService(AccessService(BindingRepository(pool), cfg()))
    async with pool.connection() as conn:
        resolved, skipped = await ensure.discover(conn, ("demo", "missing"))
    assert skipped == [{"toolkit": "missing", "reason": "not_published"}]
    (found,) = resolved
    assert found["toolkit_id"] == TOOLKIT_ID
    assert found["version"] == 1
    assert found["services"] == {"demo": "demo"}


async def test_concurrent_seeds_serialize(database):
    pool = database.pool
    reports = await asyncio.gather(*(seed(pool, cfg(), load(), package_files) for _ in range(4)))
    assert sum(r.count("created") for r in reports) == 5  # exactly one run created the artifacts
    assert await versions(pool, "mcp_tool_versions", "tool_id", TOOL_ID) == [(1, "published")]
    assert await versions(pool, "mcp_toolkit_versions", "toolkit_id", TOOLKIT_ID) == [
        (1, "published")
    ]


async def test_seed_waits_for_a_held_lock_then_times_out(database):
    pool = database.pool
    async with pool.connection() as holder:
        await holder.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        await holder.commit()
        with pytest.raises(SeedError, match="holds the lock"):
            await seed(pool, cfg(), load(), package_files, lock_timeout=0.5)
        await holder.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        await holder.commit()
    assert (await seed(pool, cfg(), load(), package_files)).count("created") == 5
