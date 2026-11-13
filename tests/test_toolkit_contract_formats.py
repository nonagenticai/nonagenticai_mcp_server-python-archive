"""Named-format registry: fixed validators replace raw regex in schemas (catalog + contract)."""

import pytest

from src.catalog.formats import FORMAT_CHECKER, FORMATS
from src.catalog.validation import validate_instance, validate_schema
from src.toolkit_contract import InputRejectedError, strict_object, validate_input
from tests.fixtures import settings

VALID = {
    "container-name": ["a", "repo-1", "a" * 63, "0abc"],
    "git-branch": ["main", "feat/toolkit-t0", "release/1.2.3", "a_b"],
    "repo-url-https": [
        "https://github.com/nonagenticai/mcp",
        "https://github.com/o/r.git",
        "https://git.internal:8443/team/sub/repo",
    ],
    "relative-path": ["a.py", "src/pkg/mod.py", ".github/workflows/ci.yaml", "a/b-c_d@1+2"],
    "package-name": ["ruff", "ty", "typing_extensions", "@types/node", "github.com/x/y", "a+b"],
    "uuid": ["0192f0c4-1c2d-7abc-8def-0123456789ab", "0192F0C4-1C2D-7ABC-8DEF-0123456789AB"],
    "idempotency-key": ["key-0001", "run:2026-10-24.a_b"],
    "resume-token": ["a.b", "AbC_-1.xYz"],
}
INVALID = {
    "container-name": ["", "-a", "a-", "Repo", "a_b", "a" * 64, "a.b"],
    "git-branch": [
        "",
        "-x",
        "/x",
        "x/",
        ".x",
        "x.",
        "a..b",
        "a//b",
        "a/.b",
        "x.lock",
        "a b",
        "a@{1}",
        "a~1",
        "a" * 201,
    ],
    "repo-url-https": [
        "http://github.com/o/r",
        "https://user:pw@github.com/o/r",
        "https://github.com/o",
        "https://github.com/o/r?x=1",
        "https://github.com/o/../r",
        "https:///o/r",
        "https://Git.com/o/r",
        "https://github.com:0/o/r",
        "https://github.com:99999/o/r",
        "https://github.com:x/o/r",
        "https://github.com/o//r",
        "https://github.com/" + "a" * 600,
        "https://-bad.com/o/r",
    ],
    "relative-path": [
        "",
        "/etc/passwd",
        "../x",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "a\x00b",
        "a b",
        "x" * 1025,
    ],
    "package-name": [
        "",
        "-e",
        "--index-url",
        "a b",
        "a==1",
        "a;b",
        "a..b",
        "a//b",
        "a/",
        "x" * 129,
    ],
    "uuid": [
        "",
        "0192f0c41c2d7abc8def0123456789ab",
        "not-a-uuid-not-a-uuid-not-a-uuid-123",
        "{0192f0c4-1c2d-7abc-8def-0123456789}",
    ],
    "idempotency-key": ["short", "has space x", "x" * 129],
    "resume-token": ["nodot", "a.b.c", "a.", "x" * 2049 + ".y"],
}


def test_every_format_has_cases():
    assert set(FORMATS) == set(VALID) == set(INVALID)


@pytest.mark.parametrize(("name", "value"), [(n, v) for n, vs in VALID.items() for v in vs])
def test_valid(name, value):
    assert FORMATS[name](value)
    assert FORMAT_CHECKER.conforms(value, name)


@pytest.mark.parametrize(("name", "value"), [(n, v) for n, vs in INVALID.items() for v in vs])
def test_invalid(name, value):
    assert not FORMATS[name](value)
    assert not FORMAT_CHECKER.conforms(value, name)


def test_checker_has_no_jsonschema_defaults_and_ignores_non_strings():
    assert set(FORMAT_CHECKER.checkers) == set(FORMATS)
    assert FORMAT_CHECKER.conforms(5, "uuid")  # type keyword handles non-strings


def test_catalog_schema_accepts_registered_and_rejects_unknown_formats_and_pattern():
    ok = {"type": "object", "properties": {"b": {"type": "string", "format": "git-branch"}}}
    assert validate_schema(ok, settings())
    for bad in ({"type": "string", "format": "uri"}, {"type": "string", "format": ["uuid"]}):
        with pytest.raises(ValueError, match="unregistered string format"):
            validate_schema(bad, settings())
    with pytest.raises(ValueError, match="unsupported schema keyword"):
        validate_schema({"type": "string", "pattern": "^a$"}, settings())


def test_catalog_instance_validation_enforces_formats():
    schema = {"type": "object", "properties": {"p": {"type": "string", "format": "relative-path"}}}
    validate_instance(schema, {"p": "src/a.py"}, settings())
    with pytest.raises(ValueError, match="does not match"):
        validate_instance(schema, {"p": "../etc/passwd"}, settings())


def test_contract_validate_input_enforces_formats_without_echo():
    schema = strict_object(
        {"repo_url": {"type": "string", "maxLength": 512, "format": "repo-url-https"}}
    )
    validate_input(schema, {"repo_url": "https://github.com/o/r"})
    with pytest.raises(InputRejectedError, match="/repo_url violates format") as info:
        validate_input(schema, {"repo_url": "https://user:secretpw@github.com/o/r"})
    assert "secretpw" not in str(info.value)
