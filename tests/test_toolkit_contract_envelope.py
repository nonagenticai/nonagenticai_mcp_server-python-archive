"""Result envelope invariants and the verdict helper (product plan §1)."""

import pytest
from pydantic import ValidationError

from src.catalog.validation import validate_schema
from src.toolkit_contract import (
    RESULT_SCHEMA,
    Check,
    CheckStatus,
    NextStep,
    Status,
    ToolResult,
    Verdict,
    verdict,
)
from src.toolkit_contract.envelope import (
    backend_error,
    clip,
    completed,
    pending,
    precondition_failed,
)

FIX = (NextStep(tool="coding_provision_repo", reason="provision first"),)


def check(name: str, status: CheckStatus) -> Check:
    return Check(name=name, status=status, summary="s")


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), Verdict.PASS),
        ((CheckStatus.PASS,), Verdict.PASS),
        ((CheckStatus.PASS, CheckStatus.DEGRADED), Verdict.DEGRADED),
        ((CheckStatus.DEGRADED, CheckStatus.FAIL), Verdict.FAIL),
        ((CheckStatus.PASS, CheckStatus.ERROR), Verdict.FAIL),
    ],
)
def test_verdict(statuses, expected):
    checks = [check(f"c{i}", s) for i, s in enumerate(statuses)]
    assert verdict(checks) is expected


def test_completed_picks_ok_or_check_failed():
    assert completed({"x": 1}).status is Status.OK
    assert completed(None, (check("lint", CheckStatus.DEGRADED),)).status is Status.OK
    failed = completed(None, (check("lint", CheckStatus.FAIL),))
    assert failed.status is Status.CHECK_FAILED
    assert failed.verdict is Verdict.FAIL


def test_verdict_absent_without_checks_and_serialized_with_them():
    assert completed(1).verdict is None
    assert "verdict" not in completed(1).to_json()
    body = completed(1, (check("lint", CheckStatus.PASS),)).to_json()
    assert body == {
        "status": "ok",
        "data": 1,
        "checks": [{"name": "lint", "status": "pass", "summary": "s"}],
        "next": [],
        "verdict": "pass",
    }


def test_ok_cannot_hide_a_failing_check():
    with pytest.raises(ValidationError, match="ok result cannot carry a failing check"):
        ToolResult(status=Status.OK, checks=(check("lint", CheckStatus.FAIL),))


def test_check_failed_needs_a_failure():
    with pytest.raises(ValidationError, match="check_failed requires a failing check"):
        ToolResult(status=Status.CHECK_FAILED, checks=(check("lint", CheckStatus.PASS),))


def test_precondition_failed_needs_reason_and_next():
    with pytest.raises(ValidationError, match="requires reason and next"):
        ToolResult(status=Status.PRECONDITION_FAILED, reason="not_provisioned")
    with pytest.raises(ValidationError, match="requires reason and next"):
        ToolResult(status=Status.PRECONDITION_FAILED, next=FIX)
    result = precondition_failed("not_provisioned", FIX)
    assert result.to_json()["next"][0]["tool"] == "coding_provision_repo"


def test_pending_needs_token_and_next():
    with pytest.raises(ValidationError, match="requires resume_token and next"):
        ToolResult(status=Status.PENDING, next=FIX)
    assert pending("a.b", FIX).resume_token == "a.b"


def test_backend_error_needs_error_and_clips_message():
    with pytest.raises(ValidationError, match="backend_error requires error"):
        ToolResult(status=Status.BACKEND_ERROR)
    result = backend_error("x" * 2000, retryable=True)
    assert result.error is not None
    assert len(result.error.message) == 500
    assert result.error.message.endswith("...")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("resume_token", "a.b", "resume_token is only valid on pending"),
        ("reason", "not_indexed", "reason is only valid on precondition_failed"),
        ("error", {"message": "m", "retryable": False}, "error is only valid on backend_error"),
    ],
)
def test_fields_only_on_their_status(field, value, message):
    with pytest.raises(ValidationError, match=message):
        ToolResult(status=Status.OK, **{field: value})


def test_check_names_unique_and_typed():
    with pytest.raises(ValidationError, match="unique"):
        ToolResult(
            status=Status.OK,
            checks=(check("lint", CheckStatus.PASS), check("lint", CheckStatus.PASS)),
        )
    with pytest.raises(ValidationError):
        Check(name="Bad Name", status=CheckStatus.PASS)
    with pytest.raises(ValidationError):
        NextStep(tool="coding.provision_repo")


def test_envelope_rejects_unknown_fields_and_is_frozen():
    with pytest.raises(ValidationError):
        ToolResult(status=Status.OK, verdict="pass")  # computed, never supplied
    result = completed(1)
    with pytest.raises(ValidationError):
        result.status = Status.PENDING  # ty: ignore[invalid-assignment]


def test_clip():
    assert clip("abc", 10) == "abc"
    assert clip("abcdefghijk", 10) == "abcdefg..."


def test_result_schema_fits_catalog_subset_and_describes_envelopes():
    from jsonschema import Draft202012Validator

    from tests.fixtures import settings

    assert validate_schema(RESULT_SCHEMA, settings()) > 0
    validator = Draft202012Validator(RESULT_SCHEMA)
    samples = [
        completed({"a": [1]}, (check("lint", CheckStatus.FAIL),)),
        precondition_failed("dirty_tree", FIX),
        pending("a.b", FIX),
        backend_error("boom", retryable=False),
    ]
    for sample in samples:
        assert list(validator.iter_errors(sample.to_json())) == []
