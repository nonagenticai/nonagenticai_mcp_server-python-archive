import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from src.catalog.models import (
    ArgumentMapping,
    Constraint,
    ExactReference,
    LiteralValue,
    OperationConstraints,
    OperationManifest,
    ServiceManifest,
    SourcePointer,
    ToolLimits,
    ToolManifest,
)
from src.catalog.package import PackageFile, canonical_path, package_digest, validate_package
from src.catalog.validation import DIALECT, bounded_json, validate_instance, validate_schema
from src.config import Settings
from tests.fixtures import FILES, IMAGE, TOOL, operation, settings, tool


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": "1"},
        {"version": 0},
        {"version": -1},
        {"version": 2147483648},
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"unknown": "field"},
        {"runtime": "in_process"},
        {"runner_image_digest": "runner:latest"},
        {"tenant_id": "tenant"},
        {"public_name": "../tool"},
        {"status": "published"},
    ],
)
def test_strict_tool_contract(changes):
    with pytest.raises(ValidationError):
        tool(**changes)


def test_fixture_roundtrips_and_exact_refs():
    data = json.loads(Path("tests/fixtures/contracts.json").read_text())
    for kind, model in [("services", ServiceManifest), ("operations", OperationManifest)]:
        for item in data[kind]:
            assert model.model_validate_json(json.dumps(item)).schema_version == 1
    assert ToolManifest.model_validate_json(json.dumps(data["tool"])) == tool()
    with pytest.raises(ValidationError):
        ExactReference(id=TOOL, version=0)
    reference = ExactReference(id=TOOL, version=1)
    with pytest.raises(ValidationError, match="duplicate"):
        tool(allowed_operations=(reference, reference))
    with pytest.raises(ValidationError, match="undeclared"):
        tool(operation_constraints=(OperationConstraints(operation=reference, rules=()),))
    constraint = OperationConstraints(operation=reference, rules=())
    with pytest.raises(ValidationError, match="duplicate"):
        tool(allowed_operations=(reference,), operation_constraints=(constraint, constraint))


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "context_equal", "argument": "/project", "context": "/project"},
        {"kind": "set_membership", "argument": "/stage", "values": ["todo", "done"]},
        {"kind": "numeric_bounds", "argument": "/count", "minimum": 1.0, "maximum": 10.0},
        {"kind": "relative_path", "argument": "/path", "root": "workspace/src"},
        {
            "kind": "ownership_read",
            "argument": "/resource",
            "operation": {"id": str(TOOL), "version": 1},
            "result": "/tenant",
            "context": "/tenant",
        },
    ],
)
def test_constraint_union(value):
    assert TypeAdapter(Constraint).validate_json(json.dumps(value)).kind == value["kind"]


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "expression", "code": "True"},
        {"kind": "numeric_bounds", "argument": "bad", "minimum": 0.0, "maximum": 1.0},
        {"kind": "numeric_bounds", "argument": "/a", "minimum": 10.0, "maximum": 1.0},
        {"kind": "relative_path", "argument": "/a", "root": "../a"},
        {"kind": "set_membership", "argument": "/a", "values": []},
    ],
)
def test_invalid_constraints(value):
    with pytest.raises(ValidationError):
        TypeAdapter(Constraint).validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "changes",
    [
        {"path_template": "https://evil.test/a"},
        {"path_template": "//evil/a"},
        {"path_template": "/a/../b"},
        {"path_template": "/a?token=x"},
        {"path_template": "/a%2fb"},
        {"path_template": "/a/{bad"},
        {"path_template": "/a/{project}"},
        {"header_fields": ("authorization",)},
        {"header_fields": ("x_tenant_id",)},
        {"method": "DELETE"},
        {"effect": "writes", "max_retries": 1},
    ],
)
def test_operation_fixed_transport(changes):
    with pytest.raises(ValidationError):
        operation(**changes)


def test_operation_mappings_and_limits():
    mapping = ArgumentMapping(
        location="path", target="project", value=SourcePointer(source="context", pointer="/project")
    )
    assert operation(path_template="/projects/{project}", mappings=(mapping,))
    with pytest.raises(ValidationError, match="duplicate"):
        operation(path_template="/projects/{project}", mappings=(mapping, mapping))
    for location in ("query", "header"):
        with pytest.raises(ValidationError, match="undeclared"):
            operation(
                mappings=(
                    ArgumentMapping(
                        location=location,
                        target="forbidden",
                        value=LiteralValue(source="literal", value="x"),
                    ),
                )
            )
    with pytest.raises(ValidationError, match="frame"):
        ToolLimits(frame_bytes=100, output_bytes=10)
    assert operation(effect="writes", max_retries=1, idempotency="downstream_key")


def test_golden_packages():
    for fixture in json.loads(Path("tests/fixtures/package-golden.json").read_text()):
        files = tuple(PackageFile(**f) for f in fixture["files"])
        assert package_digest(files) == fixture["sha256"]
        assert package_digest(tuple(reversed(files))) == fixture["sha256"]
        validate_package(files, fixture["entrypoint"], fixture["sha256"], settings())


@pytest.mark.parametrize(
    "path", json.loads(Path("tests/fixtures/invalid-packages.json").read_text())["paths"]
)
def test_bad_paths(path):
    with pytest.raises(ValueError):
        canonical_path(path)


@pytest.mark.parametrize(
    "entrypoint",
    json.loads(Path("tests/fixtures/invalid-packages.json").read_text())["entrypoints"],
)
def test_bad_entrypoints(entrypoint):
    with pytest.raises(ValueError):
        validate_package(FILES, entrypoint, package_digest(FILES), settings())


def test_package_boundaries_and_no_import(tmp_path):
    marker = tmp_path / "executed"
    files = (PackageFile(path="main.py", content=f"raise RuntimeError({str(marker)!r})\n"),)
    validate_package(files, "main:run", package_digest(files), settings())
    assert not marker.exists()
    for bad_files, entry, digest, configuration in [
        ((), "main:run", "a" * 64, settings()),
        (FILES, "package.main:run", "a" * 64, settings()),
        (FILES + (FILES[0],), "package.main:run", package_digest(FILES), settings()),
        (
            FILES + (PackageFile(path="package", content=""),),
            "package.main:run",
            package_digest(FILES),
            settings(),
        ),
        (FILES, "missing:run", package_digest(FILES), settings()),
        (FILES, "package.main:run", package_digest(FILES), settings(max_package_files=1)),
        (FILES, "package.main:run", package_digest(FILES), settings(max_package_file_bytes=1)),
        (
            FILES,
            "package.main:run",
            package_digest(FILES),
            settings(max_package_bytes=50, max_package_file_bytes=50),
        ),
    ]:
        with pytest.raises(ValueError):
            validate_package(bad_files, entry, digest, configuration)
    with pytest.raises(ValidationError):
        PackageFile.model_validate({"path": "main.py", "content": "", "symlink": "/etc/passwd"})
    with pytest.raises(ValueError):
        canonical_path("a" * 513)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://evil.test/schema"},
        {"$ref": "file:///etc/passwd"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        {"$dynamicRef": "#x"},
        {"$id": "https://evil.test"},
        {"pattern": "(a+)+$"},
        {"format": "uri"},
        {"$ref": "#/$defs/missing"},
        {"$ref": "#"},
        {"$defs": {"cycle": {"$ref": "#/$defs/cycle"}}},
        {"anyOf": [{}] * 9},
        {"properties": []},
        {"items": 5},
    ],
)
def test_closed_schema_subset(schema, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("schema attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(ValueError):
        validate_schema(schema, settings())


def test_schema_local_refs_and_work_bounds():
    schema = {
        "$schema": DIALECT,
        "$defs": {"count": {"type": "integer", "minimum": 0}},
        "type": "object",
        "properties": {"count": {"$ref": "#/$defs/count"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    validate_instance(schema, {"count": 3}, settings())
    with pytest.raises(ValueError, match="match"):
        validate_instance(schema, {"count": -1}, settings())
    for configuration in [
        settings(max_schema_bytes=5),
        settings(max_schema_nodes=3),
        settings(max_schema_depth=1),
        settings(max_validation_work=1),
    ]:
        with pytest.raises(ValueError):
            validate_instance(schema, {"count": 1}, configuration)
    refs = {"$defs": {"a": {}}, "allOf": [{"$ref": "#/$defs/a"}, {"$ref": "#/$defs/a"}]}
    with pytest.raises(ValueError, match="bounded"):
        validate_schema(refs, settings(max_schema_references=1))
    with pytest.raises(ValueError):
        bounded_json({1: "non-string key"}, 100, 100, 10)
    with pytest.raises(ValueError):
        bounded_json(float("nan"), 100, 100, 10)
    assert validate_schema({"additionalProperties": False, "items": True}, settings())
    for value, limits in [
        ("x" * 11, (10, 100, 10)),
        ([0] * 11, (100, 10, 10)),
        ({str(i): 0 for i in range(11)}, (100, 10, 10)),
    ]:
        with pytest.raises(ValueError):
            bounded_json(value, *limits)
    with pytest.raises(ValueError, match="invalid Draft"):
        validate_schema({"type": "made_up"}, settings())


def test_settings_fail_closed(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(**{})
    for updates in [
        dict(database_url="sqlite:///a"),
        dict(database_url="postgresql://localhost"),
        dict(database_url="postgresql://host/db", pool_min_size=3, pool_max_size=1),
        dict(database_url="postgresql://host/db", max_package_bytes=1),
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate(updates)
    configuration = settings()
    assert configuration.dsn == "postgresql://unused/unused"
    assert "unused/unused" not in repr(configuration)
    monkeypatch.setenv("MCP_POOL_MAX_SIZE", "12")
    assert settings().pool_max_size == 12
    assert IMAGE in settings().approved_runner_images
