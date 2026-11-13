"""The ``fr`` platform toolkit: an external toolkit (no service, operations or runner image)."""

import json
from pathlib import Path
from uuid import UUID

import pytest

from src.catalog.models import ToolManifest
from src.platform.seed import seed
from src.platform.toolkit_source import load_all
from tests.fixtures import IMAGE, settings

ROOT = Path(__file__).resolve().parents[1] / "toolkits"
FR_TOOL_ID = UUID("01a1914e-652b-7596-97bc-22a9f0b4bf7d")
FR_CLOSE_TOOL_ID = UUID("01a1b302-6efc-75a4-b598-d983d9dedb40")
FR_TOOLKIT_ID = UUID("01a1914e-6406-7583-b9a6-37b1507228bc")
FR_TOOL_NAMES = {"start_fr_project", "fr_get_project", "fr_close_project"}


def fr_only():
    return tuple(s for s in load_all(ROOT) if s.name == "fr")


async def rows(pool, query, *args):
    async with pool.connection() as conn:
        return await (await conn.execute(query, args)).fetchall()


def test_load_all_loads_coding_and_fr():
    by_name = {s.name: s for s in load_all(ROOT)}
    assert {"coding", "fr"} <= set(by_name)
    assert not by_name["coding"].external
    fr = by_name["fr"]
    assert fr.external
    assert fr.toolkit["toolkit_id"] == str(FR_TOOLKIT_ID)
    tools = {tool.fields["public_name"]: tool for tool in fr.tools}
    assert set(tools) == FR_TOOL_NAMES
    assert tools["start_fr_project"].fields["tool_id"] == str(FR_TOOL_ID)
    assert tools["start_fr_project"].fields["runtime"] == "fr_project"
    assert tools["fr_get_project"].fields["runtime"] == "fr_command"
    assert tools["fr_close_project"].fields["tool_id"] == str(FR_CLOSE_TOOL_ID)
    assert tools["fr_close_project"].fields["runtime"] == "fr_command"
    assert tools["fr_close_project"].fields["target"] == {
        "kind": "fr_command",
        "command": "projects.close",
    }
    assert tools["fr_close_project"].fields["effect"] == "writes"


def test_every_fr_result_schema_admits_every_project_field():
    """The adapter may emit any PROJECT_KEYS field; a closed result schema must list them all."""
    from src.external.fr import PROJECT_KEYS

    by_name = {s.name: s for s in load_all(ROOT)}
    for tool in by_name["fr"].tools:
        properties = tool.fields["result_schema"]["properties"]
        assert set(PROJECT_KEYS) <= set(properties), tool.fields["public_name"]


async def test_fr_seeds_without_a_runner_digest_and_skips_kata_tools(database):
    pool = database.pool
    report = await seed(pool, settings(), load_all(ROOT))
    assert report.tools_skipped
    assert report.actions["tool:fr.start_fr_project"] == "created"
    assert report.actions["tool:fr.fr_get_project"] == "created"
    assert report.actions["tool:fr.fr_close_project"] == "created"
    assert report.actions["toolkit:fr"] == "created"
    assert not any(k.startswith("tool:coding.") for k in report.actions)
    (row,) = await rows(
        pool, "SELECT manifest FROM public.mcp_tool_versions WHERE tool_id=%s", FR_TOOL_ID
    )
    manifest = ToolManifest.model_validate_json(json.dumps(row["manifest"]))
    assert manifest.runtime == "fr_project"
    assert manifest.target is not None
    assert manifest.target.kind == "fr_project"
    assert manifest.target.project_type == "it"
    assert manifest.entrypoint is None
    assert manifest.runner_image_digest is None
    assert manifest.allowed_operations == ()
    (kit,) = await rows(
        pool, "SELECT status FROM public.mcp_toolkit_versions WHERE toolkit_id=%s", FR_TOOLKIT_ID
    )
    assert kit["status"] == "published"


@pytest.mark.parametrize("digest", [False, True])
async def test_second_seed_is_a_no_op_for_fr(database, digest):
    cfg = settings(runner_image_digest=IMAGE) if digest else settings()
    await seed(database.pool, cfg, fr_only())
    again = await seed(database.pool, cfg, fr_only())
    assert not again.changed
    for name in FR_TOOL_NAMES:
        assert again.actions[f"tool:fr.{name}"] == "unchanged"
    assert again.actions["toolkit:fr"] == "unchanged"


async def test_close_tool_seeds_with_the_same_contract_as_the_read(database):
    pool = database.pool
    report = await seed(pool, settings(), fr_only())
    assert report.actions["tool:fr.fr_close_project"] == "created"
    (row,) = await rows(
        pool, "SELECT manifest FROM public.mcp_tool_versions WHERE tool_id=%s", FR_CLOSE_TOOL_ID
    )
    manifest = ToolManifest.model_validate_json(json.dumps(row["manifest"]))
    assert manifest.runtime == "fr_command" and manifest.effect == "writes"
    assert manifest.target is not None and manifest.target.command == "projects.close"
    assert manifest.limits.wall_seconds == 60 and manifest.limits.api_calls == 2
    assert set(manifest.input_schema["properties"]) == {"id"}
    assert manifest.input_schema["required"] == ["id"]
