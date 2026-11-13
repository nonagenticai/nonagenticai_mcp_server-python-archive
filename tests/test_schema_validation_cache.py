"""validate_schema memoizes successful validations of identical schemas (resolve runs it per tool
per request; jsonschema's meta-validation dominated tools/list CPU in prod)."""

import pytest

import src.catalog.validation as validation
from src.catalog.validation import validate_schema
from tests.fixtures import settings

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}


@pytest.fixture(autouse=True)
def fresh_cache():
    validation._VALID.clear()
    yield
    validation._VALID.clear()


def counting(monkeypatch):
    calls = []
    real = validation.Draft202012Validator.check_schema
    monkeypatch.setattr(
        validation.Draft202012Validator,
        "check_schema",
        classmethod(lambda cls, schema: (calls.append(1), real(schema))[1]),
    )
    return calls


def test_identical_schema_is_meta_validated_once(monkeypatch):
    calls = counting(monkeypatch)
    cfg = settings()
    first = validate_schema(dict(SCHEMA), cfg)
    # Equal content in another dict (key order differs): still one meta-validation.
    reordered = {
        "additionalProperties": False,
        "properties": {"a": {"type": "string"}},
        "type": "object",
    }
    assert validate_schema(reordered, cfg) == first
    assert len(calls) == 1


def test_other_limits_or_other_schema_validate_again(monkeypatch):
    calls = counting(monkeypatch)
    validate_schema(SCHEMA, settings())
    validate_schema(SCHEMA, settings(max_schema_nodes=500))
    validate_schema({**SCHEMA, "title": "t"}, settings())
    assert len(calls) == 3


def test_invalid_schema_is_never_cached(monkeypatch):
    bad = {"type": "object", "pattern": "x"}
    for _ in range(2):
        with pytest.raises(ValueError):
            validate_schema(bad, settings())
    assert not validation._VALID


def test_limit_violation_is_rejected_even_after_success_under_larger_limits():
    big = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(40)}}
    validate_schema(big, settings())
    with pytest.raises(ValueError):
        validate_schema(big, settings(max_schema_nodes=10))


def test_cache_is_bounded():
    cfg = settings()
    for i in range(validation._VALID_MAX + 50):
        validate_schema({"type": "object", "title": str(i)}, cfg)
    assert len(validation._VALID) == validation._VALID_MAX
