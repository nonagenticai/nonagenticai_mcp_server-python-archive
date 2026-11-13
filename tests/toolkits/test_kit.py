"""toolkits/_kit/kit.py: envelope invariants, bounded polling, resume tokens, call()."""

import base64
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from runner.sdk import ApiStatusError
from src.catalog.validation import validate_instance
from src.toolkit_contract.envelope import RESULT_SCHEMA
from tests.fixtures import settings
from tests.toolkits.harness import ROOT


@pytest.fixture
def kit(tmp_path, monkeypatch):
    (tmp_path / "ops.py").write_text('OPS = {"op": ["0195f48b-d000-7000-8000-000000000001", 1]}\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("ops", None)
    spec = importlib.util.spec_from_file_location("kit_under_test", ROOT / "toolkits/_kit/kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid(result):
    validate_instance(RESULT_SCHEMA, result, settings())


def test_envelope_shapes_validate_against_the_result_schema(kit):
    step = kit.step("coding_repo_status", "why", {"container_name": "x"})
    ok = kit.completed({"a": 1}, [kit.check("c1", "pass", "fine")])
    degraded = kit.completed(None, [kit.check("c1", "degraded", "meh")])
    failed = kit.completed(None, [kit.check("c1", "fail", "bad")])
    pre = kit.precondition_failed("not_indexed", [step])
    pend = kit.pending("a.b", [step], {"x": 1})
    err = kit.backend_error("x" * 900, True)
    assert [r["status"] for r in (ok, degraded, failed, pre, pend, err)] == [
        "ok",
        "ok",
        "check_failed",
        "precondition_failed",
        "pending",
        "backend_error",
    ]
    assert (ok["verdict"], degraded["verdict"], failed["verdict"]) == ("pass", "degraded", "fail")
    assert "verdict" not in kit.completed({}, [])
    assert len(err["error"]["message"]) == 500
    for result in (ok, degraded, failed, pre, pend, err):
        valid(result)


def test_envelope_invariants_are_enforced(kit):
    step = kit.step("coding_repo_status")
    fail = [kit.check("c", "fail", "")]
    bad = [
        lambda: kit.envelope("ok", checks=fail),
        lambda: kit.envelope("check_failed", checks=[kit.check("c", "pass")]),
        lambda: kit.envelope("precondition_failed", reason="x", next_steps=[]),
        lambda: kit.envelope("precondition_failed", next_steps=[step]),
        lambda: kit.envelope("precondition_failed", reason="Bad Reason", next_steps=[step]),
        lambda: kit.envelope("pending", token="a.b", next_steps=[]),
        lambda: kit.envelope("pending", next_steps=[step]),
        lambda: kit.envelope("ok", token="a.b"),
        lambda: kit.envelope("ok", reason="x"),
        lambda: kit.envelope("backend_error"),
        lambda: kit.envelope("ok", error={"message": "m", "retryable": False}),
        lambda: kit.envelope("ok", checks=[kit.check("c", "pass"), kit.check("c", "pass")]),
        lambda: kit.check("Bad Name", "pass"),
        lambda: kit.check("c", "unknown"),
        lambda: kit.step("Bad Tool"),
    ]
    for build in bad:
        with pytest.raises(ValueError):
            build()


async def test_poll_is_bounded_by_attempts_and_budget(kit):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    calls = []

    async def fetch():
        calls.append(1)
        return len(calls)

    value, finished = await kit.poll(
        fetch, lambda v: v == 3, SimpleNamespace(remaining=lambda: 99), sleep=sleep
    )
    assert (value, finished, slept) == (3, True, [3.0, 3.0])

    calls.clear()
    value, finished = await kit.poll(
        fetch, lambda v: False, SimpleNamespace(remaining=lambda: 99), attempts=4, sleep=sleep
    )
    assert (value, finished, len(calls)) == (4, False, 4)

    calls.clear()  # not enough time left after reserving for what comes next: one look only
    value, finished = await kit.poll(
        fetch,
        lambda v: False,
        SimpleNamespace(remaining=lambda: 50),
        reserve=49,
        interval=3,
        sleep=sleep,
    )
    assert (finished, len(calls)) == (False, 1)


def test_budget_subtracts_the_margin(kit):
    now = [100.0]
    budget = kit.Budget(300, 25, clock=lambda: now[0])
    assert budget.remaining() == 275 and budget.allows(275) and not budget.allows(276)
    now[0] += 200
    assert budget.remaining() == 75


def test_resume_token_roundtrip_and_checks(kit):
    token = kit.encode_token("tool_a", {"cn": "x"}, now=1000)
    assert kit.decode_token(token, "tool_a", now=1100) == {"cn": "x"}
    assert token.count(".") == 1 and len(token) <= 2048
    body, _crc = token.split(".")
    with pytest.raises(ValueError, match="another tool"):
        kit.decode_token(token, "tool_b", now=1100)
    with pytest.raises(ValueError, match="expired"):
        kit.decode_token(token, "tool_a", now=1000 + 3601)
    with pytest.raises(ValueError, match="expired"):
        kit.decode_token(token, "tool_a", now=999)  # issued in the future
    for broken in ("", "nodot", token + ".x", body + ".AAAAAAAA", "x" * 3000 + ".y", 5):
        with pytest.raises(ValueError, match="malformed"):
            kit.decode_token(broken, "tool_a", now=1100)
    forged = (
        base64.urlsafe_b64encode(b'{"v":2,"tool":"tool_a","iat":1000,"state":{}}')
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(ValueError):
        kit.decode_token(f"{forged}.{kit._crc(forged)}", "tool_a", now=1100)
    with pytest.raises(ValueError, match="too large"):
        kit.encode_token("tool_a", {"blob": "x" * 2000})


async def test_call_returns_status_for_expected_errors_only(kit):
    class API:
        async def call(self, operation_id, arguments, idempotency_key=None):
            assert operation_id == "0195f48b-d000-7000-8000-000000000001"
            mode = arguments["mode"]
            if mode == "ok":
                return {"fine": True}
            if mode == "status":
                raise ApiStatusError(404, {"detail": "gone"})
            raise RuntimeError("api_call_denied")

    ctx = SimpleNamespace(api=API())
    assert await kit.call(ctx, "op", {"mode": "ok"}) == (200, {"fine": True})
    assert await kit.call(ctx, "op", {"mode": "status"}) == (404, {"detail": "gone"})
    with pytest.raises(RuntimeError):
        await kit.call(ctx, "op", {"mode": "denied"})
    with pytest.raises(KeyError):
        await kit.call(ctx, "missing", {})
    assert kit.describe(404, {"detail": "gone"}) == "HTTP 404: gone"
    assert kit.describe(500, None) == "HTTP 500"


async def test_guarded_turns_aborts_and_denials_into_envelopes(kit):
    async def aborts():
        raise kit.Abort(kit.backend_error("stop", False))

    async def denied():
        raise RuntimeError("api_call_denied")

    async def other():
        raise RuntimeError("boom")

    assert (await kit.guarded(aborts()))["error"]["message"] == "stop"
    assert "read the state back" in (await kit.guarded(denied()))["error"]["message"]
    with pytest.raises(RuntimeError, match="boom"):
        await kit.guarded(other())
