"""Platform toolkit sources: the repo tree under ``toolkits/`` as typed, version-less manifests.

Pure (no database, no network). Layout:

    toolkits/_kit/kit.py                  stdlib helpers, copied into every package as ``kit.py``
    toolkits/<tk>/toolkit.json            {"toolkit_id", "name", "description"}
    toolkits/<tk>/service.json            ServiceManifest without ``version``
    toolkits/<tk>/operations/<key>.json   OperationManifest without ``version`` and ``service``
    toolkits/<tk>/tools/<tool>/tool.json  ToolManifest fields except tenant_id, version,
                                          package_sha256, runner_image_digest;
                                          ``allowed_operations`` is a list of operation keys
    toolkits/<tk>/tools/<tool>/*.py       package files (the package also gets kit.py, ops.py and
                                          toolkits/<tk>/shared/*.py)

A toolkit directory WITHOUT service.json is an EXTERNAL toolkit: no service, no operations, no
runner image. Its tool.json also carries ``runtime`` (not kata_python) and ``target``; the package
is the tool directory's other files (at least one, e.g. README.md), with no kit.py or ops.py.

Versions are not in the source: the seed assigns them (content addressed). Manifests returned
here carry the PLACEHOLDER version 1 (service ref and ``version`` fields); the seed overwrites
them with ``model_copy(update=...)``. ``op_refs`` (key -> (operation_id, version)) feeds the
generated ``ops.py`` and the tool's ``allowed_operations``, so a package digest depends on the
exact published operation versions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from src.catalog.models import (
    ExactReference,
    OperationManifest,
    ServiceManifest,
    ToolManifest,
)
from src.catalog.package import PackageFile, package_digest
from src.toolkit_contract.envelope import RESULT_SCHEMA

PLACEHOLDER_VERSION = 1
DEFAULT_ROOT = Path("toolkits")
KIT_DIR = "_kit"
NO_CONTEXT: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_FIELDS = frozenset(
    {
        "tool_id",
        "public_name",
        "operation",
        "description",
        "entrypoint",
        "input_schema",
        "result_schema",
        "context_schema",
        "limits",
        "effect",
        "allowed_operations",
        "operation_constraints",
        "runtime",
        "target",
    }
)

OpRefs = dict[str, tuple[UUID, int]]


@dataclass(frozen=True)
class ToolSource:
    name: str
    fields: dict[str, Any]
    op_keys: tuple[str, ...]
    files: tuple[tuple[str, str], ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ToolkitSource:
    name: str
    toolkit: dict[str, Any]
    #: None for an external toolkit (no service.json)
    service: ServiceManifest | None
    operations: dict[str, OperationManifest]
    tools: tuple[ToolSource, ...]
    kit: str = field(default="", repr=False)
    #: toolkits/<tk>/shared/*.py: modules every tool package of this toolkit also receives
    shared: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def external(self) -> bool:
        return self.service is None

    def tool(self, name: str) -> ToolSource:
        return next(t for t in self.tools if t.name == name)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_tool(directory: Path, operations: dict[str, OperationManifest]) -> ToolSource:
    raw = _json(directory / "tool.json")
    unknown = set(raw) - TOOL_FIELDS
    if unknown:
        raise ValueError(f"{directory.name}/tool.json: unknown fields {sorted(unknown)}")
    keys = tuple(raw.pop("allowed_operations", ()))
    missing = [k for k in keys if k not in operations]
    if missing or len(set(keys)) != len(keys):
        raise ValueError(f"{directory.name}: allowed_operations unknown or duplicated: {missing}")
    raw.setdefault("result_schema", json.loads(json.dumps(RESULT_SCHEMA)))
    raw.setdefault("context_schema", dict(NO_CONTEXT))
    if raw.get("runtime", "kata_python") != "kata_python":
        if keys or "entrypoint" in raw:
            raise ValueError(
                f"{directory.name}: an external tool takes no operations or entrypoint"
            )
        extra = sorted(p for p in directory.iterdir() if p.is_file() and p.name != "tool.json")
        if not extra:
            raise ValueError(f"{directory.name}: an external tool needs a package file (README.md)")
        files = tuple((p.name, p.read_text(encoding="utf-8")) for p in extra)
        return ToolSource(directory.name, raw, keys, files)
    if "target" in raw:
        raise ValueError(f"{directory.name}: kata_python tools take no target")
    raw.setdefault("entrypoint", "tool:main")
    files = tuple((p.name, p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.py")))
    return ToolSource(directory.name, raw, keys, files)


def _load_tools(
    directory: Path, operations: dict[str, OperationManifest]
) -> tuple[ToolSource, ...]:
    tools = tuple(
        _load_tool(p, operations)
        for p in sorted((directory / "tools").iterdir())
        if (p / "tool.json").is_file()
    )
    names = [t.fields["public_name"] for t in tools]
    if len(set(names)) != len(names):
        raise ValueError(f"{directory.name}: duplicate tool public_name")
    return tools


def _load_toolkit(directory: Path, kit: str) -> ToolkitSource:
    toolkit = _json(directory / "toolkit.json")
    if not (directory / "service.json").is_file():
        tools = _load_tools(directory, {})
        if any(t.fields.get("runtime", "kata_python") == "kata_python" for t in tools):
            raise ValueError(f"{directory.name}: a toolkit without service.json is external-only")
        return ToolkitSource(directory.name, toolkit, None, {}, tools)
    service_raw = _json(directory / "service.json")
    service = ServiceManifest.model_validate_json(
        json.dumps({**service_raw, "version": PLACEHOLDER_VERSION})
    )
    reference = ExactReference(id=service.service_id, version=PLACEHOLDER_VERSION)
    operations: dict[str, OperationManifest] = {}
    for path in sorted((directory / "operations").glob("*.json")):
        document = {
            **_json(path),
            "service": {"id": str(reference.id), "version": reference.version},
            "version": PLACEHOLDER_VERSION,
        }
        operations[path.stem] = OperationManifest.model_validate_json(json.dumps(document))
    ids = [o.operation_id for o in operations.values()]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{directory.name}: duplicate operation_id")
    tools = _load_tools(directory, operations)
    shared = tuple(
        (p.name, p.read_text(encoding="utf-8")) for p in sorted((directory / "shared").glob("*.py"))
    )
    return ToolkitSource(directory.name, toolkit, service, operations, tools, kit, shared)


def load_all(root: Path = DEFAULT_ROOT) -> tuple[ToolkitSource, ...]:
    kit = (root / KIT_DIR / "kit.py").read_text(encoding="utf-8")
    return tuple(
        _load_toolkit(p, kit)
        for p in sorted(root.iterdir())
        if p.is_dir() and p.name != KIT_DIR and (p / "toolkit.json").is_file()
    )


def default_op_refs(tk: ToolkitSource, tool: ToolSource) -> OpRefs:
    return {k: (tk.operations[k].operation_id, PLACEHOLDER_VERSION) for k in tool.op_keys}


def ops_source(tool: ToolSource, op_refs: OpRefs) -> str:
    table = {k: [str(op_refs[k][0]), op_refs[k][1]] for k in sorted(tool.op_keys)}
    return (
        "# Generated: operation id and exact version per key.\nOPS = "
        + json.dumps(table, indent=2, sort_keys=True)
        + "\n"
    )


def package_files(
    tk: ToolkitSource, tool: ToolSource, op_refs: OpRefs | None = None
) -> list[dict[str, str]]:
    """Tool files + kit.py + generated ops.py, sorted by path (deterministic)."""
    if tk.external:
        return [{"path": n, "content": c} for n, c in sorted(tool.files)]
    refs = default_op_refs(tk, tool) if op_refs is None else op_refs
    files = dict(tk.shared)
    clash = {"kit.py", "ops.py"} & (set(files) | {n for n, _ in tool.files})
    if clash or set(files) & {n for n, _ in tool.files}:
        raise ValueError(f"{tool.name}: package file name collision {sorted(clash)}")
    files.update(tool.files)
    files["kit.py"] = tk.kit
    files["ops.py"] = ops_source(tool, refs)
    return [{"path": p, "content": files[p]} for p in sorted(files)]


def tool_manifest(
    tk: ToolkitSource,
    tool: ToolSource,
    tenant_id: UUID,
    runner_image_digest: str | None,
    op_refs: OpRefs | None = None,
    version: int = PLACEHOLDER_VERSION,
) -> ToolManifest:
    refs = default_op_refs(tk, tool) if op_refs is None else op_refs
    files = tuple(PackageFile(**f) for f in package_files(tk, tool, refs))
    document = {
        **tool.fields,
        "tenant_id": str(tenant_id),
        "version": version,
        "package_sha256": package_digest(files),
        "runner_image_digest": None if tk.external else runner_image_digest,
        "allowed_operations": [
            {"id": str(refs[k][0]), "version": refs[k][1]} for k in tool.op_keys
        ],
    }
    return ToolManifest.model_validate_json(json.dumps(document))
