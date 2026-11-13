"""Tool packages for tenant-authored tools: the author's script files plus the host's ``kit.py``
and generated ``ops.py``.

Reuses the platform toolkit packaging (``src.platform.toolkit_source``): the same ``kit.py`` every
platform tool gets and the same ``ops.py`` table (``OPS = {key: [operation_id, version]}``). The
author never writes either file, so a tool can only call operations the host pinned for it.
"""

import json
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from uuid import UUID

from src.builder.models import HOST_FILES, ToolOperation
from src.catalog.models import ExactReference, ToolLimits, ToolManifest
from src.catalog.package import PackageFile, package_digest
from src.platform.toolkit_source import KIT_DIR, NO_CONTEXT, ToolSource, ops_source
from src.toolkit_contract.envelope import RESULT_SCHEMA

#: toolkits/_kit/kit.py of this checkout (the image ships ``toolkits/`` next to ``src/``).
KIT_FILE = Path(__file__).resolve().parents[2] / "toolkits" / KIT_DIR / "kit.py"
OPS_PREFIX = "OPS = "

OpRefs = Mapping[str, tuple[UUID, int]]


@cache
def kit_source() -> str:
    return KIT_FILE.read_text(encoding="utf-8")


def user_files(files: Iterable[PackageFile]) -> tuple[PackageFile, ...]:
    """The author's own files (the host files are regenerated on every version)."""
    return tuple(f for f in files if f.path not in HOST_FILES)


def parse_ops(files: Iterable[PackageFile]) -> dict[str, tuple[UUID, int]]:
    """The ``{script_key: (operation_id, version)}`` table of a stored package's ops.py."""
    for file in files:
        if file.path == "ops.py":
            text = file.content.split(OPS_PREFIX, 1)[1]
            return {k: (UUID(v[0]), int(v[1])) for k, v in json.loads(text).items()}
    return {}


def build_package(files: Iterable[PackageFile], refs: OpRefs) -> tuple[PackageFile, ...]:
    """Author files + ``kit.py`` + generated ``ops.py``, sorted by path."""
    authored = user_files(files)
    generated = ops_source(ToolSource("tool", {}, tuple(refs)), dict(refs))
    out = [*authored, PackageFile(path="kit.py", content=kit_source())]
    out.append(PackageFile(path="ops.py", content=generated))
    return tuple(sorted(out, key=lambda f: f.path))


def tool_manifest(
    *,
    tool_id: UUID,
    tenant_id: UUID,
    version: int,
    fields: Mapping[str, object],
    files: tuple[PackageFile, ...],
    refs: Iterable[ExactReference],
    runner_image_digest: str,
) -> ToolManifest:
    """The manifest the host fills in: tenant, runner image, package digest, pinned operations."""
    document = {
        "tool_id": str(tool_id),
        "tenant_id": str(tenant_id),
        "version": version,
        "package_sha256": package_digest(files),
        "runner_image_digest": runner_image_digest,
        "allowed_operations": [
            {"id": str(r.id), "version": r.version} for r in sorted(refs, key=lambda r: str(r.id))
        ],
        **fields,
    }
    return ToolManifest.model_validate_json(json.dumps(document))


def manifest_fields(
    *,
    public_name: str,
    operation: str,
    description: str,
    entrypoint: str,
    input_schema: dict,
    result_schema: dict | None,
    context_schema: dict | None,
    effect: str,
    limits: ToolLimits | None,
) -> dict[str, object]:
    return {
        "public_name": public_name,
        "operation": operation,
        "description": description,
        "entrypoint": entrypoint,
        "input_schema": input_schema,
        "result_schema": result_schema
        if result_schema is not None
        else json.loads(json.dumps(RESULT_SCHEMA)),
        "context_schema": context_schema if context_schema is not None else dict(NO_CONTEXT),
        "effect": effect,
        **({"limits": limits.model_dump(mode="json")} if limits is not None else {}),
    }


def script_keys(operations: Iterable[ToolOperation]) -> list[str]:
    return [o.script_key for o in operations]
