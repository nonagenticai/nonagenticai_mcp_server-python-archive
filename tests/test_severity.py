"""Severity mapping and the wait/stall signals of the fr activity feed. Pure, no I/O."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from src.config import Settings
from src.tasks.severity import (
    DEFAULT_SEVERITY,
    DETAIL_LIMIT,
    SEVERITIES,
    severity_for,
    signals,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SETTINGS = Settings(database_url=SecretStr("postgresql://unused@localhost/unused"))


def config(**overrides):
    return Settings(
        database_url=SecretStr("postgresql://unused@localhost/unused"),
        **overrides,
    )


def entry(seq, kind, seconds_ago=0, source_ref=None, text="x", severity=None):
    return {
        "seq": seq,
        "kind": kind,
        "text": text,
        "severity": severity or severity_for(kind),
        "source_ref": source_ref,
        "created_at": NOW - timedelta(seconds=seconds_ago),
    }


def run(status="running", **extra):
    return {
        "run_id": "r",
        "runtime": "fr_project",
        "status": status,
        "error": None,
        "status_message": None,
        "external_ref": {"kind": "fr", "id": "s", "project_id": "p"},
        **extra,
    }


def codes(found):
    return [signal["code"] for signal in found]


@pytest.mark.parametrize(
    ("kind", "severity"),
    [
        ("failed", "critical"),
        ("start_refused", "critical"),
        ("project_failed", "critical"),
        ("approval_waiting", "high"),
        ("approval_requested", "high"),
        ("refused", "medium"),
        ("phase_stalled", "medium"),
        ("phase", "low"),
        ("started", "low"),
        ("delivered", "info"),
        ("approval_closed", "info"),
    ],
)
def test_severity_of_every_documented_kind(kind, severity):
    assert severity_for(kind) == severity
    assert severity in SEVERITIES


def test_unknown_kind_is_informational():
    assert severity_for("something_new") == DEFAULT_SEVERITY == "info"
    assert severity_for("") == "info"


def test_healthy_running_run_has_no_signal():
    activity = [entry(1, "started", seconds_ago=600), entry(2, "phase", seconds_ago=30)]
    assert signals(run(), activity, SETTINGS, NOW) == []


def test_run_without_activity_has_no_signal():
    assert signals(run(), [], SETTINGS, NOW) == []


def test_failed_run_signals_project_failed_with_the_public_code():
    (signal,) = signals(
        run("failed", error={"code": "project_failed", "message": "boom"}),
        [],
        SETTINGS,
        NOW,
    )
    assert signal["code"] == "project_failed"
    assert signal["severity"] == "critical"
    assert signal["detail"] == "project_failed"
    assert "boom" not in signal["detail"]  # upstream messages never travel


def test_failed_run_without_a_code_uses_the_status_message():
    (signal,) = signals(
        run("timed_out", status_message="  the deadline\npassed "), [], SETTINGS, NOW
    )
    assert signal["detail"] == "the deadline passed"


@pytest.mark.parametrize("status", ["failed", "timed_out", "interrupted"])
def test_every_terminal_failure_is_critical(status):
    """``interrupted`` is a terminal failure whose outcome is unknown — the most ambiguous one
    must not be the only silent one (it is ``isError`` on the wire)."""
    (signal,) = signals(run(status, error={"code": "outcome_unknown"}), [], SETTINGS, NOW)
    assert signal["code"] == "project_failed"
    assert signal["severity"] == "critical"


@pytest.mark.parametrize("outcome", ["rejected", "voided", "expired"])
def test_start_refused_outcome_is_critical(outcome):
    reference = {"kind": "fr", "id": "s", "start_outcome": outcome}
    (signal,) = signals(run(external_ref=reference), [], SETTINGS, NOW)
    assert signal == {
        "code": "start_refused",
        "severity": "critical",
        "detail": f"the project start was {outcome}",
    }


def test_approval_waiting_fires_at_the_threshold_and_not_before():
    activity = [entry(1, "approval_requested", seconds_ago=899, source_ref="ap-1")]
    assert signals(run(), activity, SETTINGS, NOW) == []
    activity = [entry(1, "approval_requested", seconds_ago=900, source_ref="ap-1")]
    (signal,) = signals(run(), activity, SETTINGS, NOW)
    assert signal == {
        "code": "approval_waiting",
        "severity": "high",
        "detail": "an approval has waited 15 min",
    }


def test_a_closed_approval_is_not_waiting():
    activity = [
        entry(1, "approval_requested", seconds_ago=7200, source_ref="ap-1"),
        entry(2, "approval_closed", seconds_ago=7100, source_ref="ap-1"),
    ]
    assert "approval_waiting" not in codes(signals(run(), activity, SETTINGS, NOW))


def test_a_newer_open_approval_still_waits():
    activity = [
        entry(1, "approval_requested", seconds_ago=7200, source_ref="ap-1"),
        entry(2, "approval_closed", seconds_ago=7100, source_ref="ap-1"),
        entry(3, "approval_requested", seconds_ago=1000, source_ref="ap-2"),
        entry(4, "phase", seconds_ago=10),
    ]
    (signal,) = signals(run(), activity, SETTINGS, NOW)
    assert signal["code"] == "approval_waiting"


def test_phase_stalled_fires_at_the_threshold_and_not_before():
    activity = [entry(1, "phase", seconds_ago=3599)]
    assert signals(run(), activity, SETTINGS, NOW) == []
    activity = [entry(1, "phase", seconds_ago=3600)]
    (signal,) = signals(run(), activity, SETTINGS, NOW)
    assert signal == {
        "code": "phase_stalled",
        "severity": "medium",
        "detail": "no progress for 60 min",
    }


def test_stall_follows_the_newest_progress_entry():
    activity = [
        entry(1, "phase", seconds_ago=10_000),
        entry(2, "phase", seconds_ago=60, text="build"),
    ]
    assert signals(run(), activity, SETTINGS, NOW) == []


def test_thresholds_come_from_settings():
    activity = [
        entry(1, "approval_requested", seconds_ago=120, source_ref="ap-1"),
        entry(2, "phase", seconds_ago=120),
    ]
    found = signals(
        run(), activity, config(fr_approval_wait_seconds=60, fr_phase_stall_seconds=60), NOW
    )
    assert codes(found) == ["approval_waiting", "phase_stalled"]


def test_terminal_runs_have_no_wait_or_stall_signal():
    activity = [
        entry(1, "approval_requested", seconds_ago=7200, source_ref="ap-1"),
        entry(2, "phase", seconds_ago=7200),
    ]
    # `interrupted` is NOT here: it is a terminal FAILURE (see
    # test_every_terminal_failure_is_critical), so it signals `project_failed` instead.
    for status in ("succeeded", "cancelled"):
        assert signals(run(status), activity, SETTINGS, NOW) == []


def test_signals_are_ordered_most_severe_first():
    reference = {"kind": "fr", "id": "s", "start_outcome": "expired"}
    failed = signals(run("failed", external_ref=reference), [], SETTINGS, NOW)
    assert codes(failed) == ["project_failed", "start_refused"]
    assert [s["severity"] for s in failed] == ["critical", "critical"]
    activity = [
        entry(1, "approval_requested", seconds_ago=7200, source_ref="ap-1"),
        entry(2, "phase", seconds_ago=7200),
    ]
    waiting = signals(run(), activity, SETTINGS, NOW)
    assert codes(waiting) == ["approval_waiting", "phase_stalled"]
    assert [s["severity"] for s in waiting] == ["high", "medium"]


def test_signal_details_are_bounded_one_line_text():
    long_status = "boom\n" * 500
    (signal,) = signals(run("failed", status_message=long_status), [], SETTINGS, NOW)
    assert len(signal["detail"]) <= DETAIL_LIMIT
    assert "\n" not in signal["detail"]


def test_naive_created_at_is_read_as_utc():
    naive = NOW.replace(tzinfo=None) - timedelta(seconds=7200)
    activity = [{**entry(1, "phase"), "created_at": naive}]
    (signal,) = signals(run(), activity, SETTINGS, NOW)
    assert signal["code"] == "phase_stalled"
