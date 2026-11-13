"""Strict protocol, SDK and extraction checks; uploaded Python is not imported here."""

import io
import json
from uuid import uuid7

import pytest

from runner.entrypoint import extract
from runner.protocol import Decoder, ProtocolError, decode, encode
from runner.sdk import API, ToolContext
from src.broker.policy import enforce, project, transport
from src.catalog.models import ContextEquality, NumericBounds, RelativePath, SetMembership
from src.credentials.resolver import FileCredentialResolver
from src.execution.models import ExecutionDeniedError
from src.sources.guard import checked_base_url
from tests.fixtures import FILES, operation, tool


def init():
    return {
        "type": "init",
        "protocol": 1,
        "files": [f.model_dump() for f in FILES],
        "digest": tool().package_sha256,
        "entrypoint": tool().entrypoint,
        "inputs": {},
        "context": {},
        "limits": {"frame_bytes": 65536},
    }


@pytest.mark.parametrize(
    "data",
    [
        b'{"type":"ready","protocol":1,"protocol":1}\n',
        b'{"type":"ready","protocol":true}\n',
        b'{"type":"unknown"}\n',
        b'{"type":"ready","protocol":2}\n',
        b'{"type":"ready","protocol":1,"tenant_id":"forged"}\n',
        b'{"type":"result","result":NaN}\n',
        b'{"type":"result","result":Infinity}\n',
        b'{"type":"result","result":null}',
        b"{}\n{}\n",
        b"\xff\n",
        b'{"type":"api_call","sequence":true,"operation_id":"bad","arguments":{}}\n',
        b'{"type":"api_call","sequence":1,"operation_id":"bad","arguments":{}}\n',
        b'{"type":"api_result","sequence":1,"result":null,"error":{"code":"secret"}}\n',
        b'{"type":"error","code":"raw secret exception"}\n',
        b'{"type":"log","message":1}\n',
        b'{"type":"init","protocol":1,"files":{},"digest":"x","entrypoint":"x","inputs":{},"context":{},"limits":{}}\n',
    ],
)
def test_reject_malformed_frames(data):
    with pytest.raises(ProtocolError):
        decode(data, 65536)


def test_split_utf8_and_aggregate_bounds():
    data = encode({"type": "result", "result": "café ☕"}, 65536)
    decoder = Decoder(65536, 65536, 2)
    output = []
    for byte in data:
        output.extend(decoder.feed(bytes([byte])))
    assert output == [{"type": "result", "result": "café ☕"}]
    for decoder, payload in [
        (Decoder(10, 100, 2), b"x" * 10),
        (Decoder(100, 2, 2), b"xxx"),
        (Decoder(100, 1000, 1), encode({"type": "cancel"}, 100) * 2),
    ]:
        with pytest.raises(ProtocolError):
            decoder.feed(payload)
    with pytest.raises(ProtocolError):
        encode({"type": "result", "result": "x" * 100}, 10)


@pytest.mark.parametrize(
    "mutation",
    [
        "absolute",
        "traversal",
        "duplicate",
        "collision",
        "digest",
        "entrypoint",
        "file_count",
        "content",
        "unicode",
        "large",
        "missing_module",
    ],
)
def test_runner_package_is_bounded_and_canonical(tmp_path, mutation):
    frame = init()
    if mutation == "absolute":
        frame["files"][0]["path"] = "/tmp/escape.py"
    elif mutation == "traversal":
        frame["files"][0]["path"] = "../escape.py"
    elif mutation == "duplicate":
        frame["files"].append(frame["files"][0])
    elif mutation == "collision":
        frame["files"].append({"path": "package", "content": "x"})
    elif mutation == "digest":
        frame["digest"] = "0" * 64
    elif mutation == "entrypoint":
        frame["entrypoint"] = "arbitrary()"
    elif mutation == "file_count":
        frame["files"] = []
    elif mutation == "content":
        frame["files"][0]["content"] = 1
    elif mutation == "unicode":
        frame["files"][0]["path"] = "cafe\u0301.py"
    elif mutation == "large":
        frame["files"][0]["content"] = "x" * 262145
    elif mutation == "missing_module":
        frame["entrypoint"] = "missing:run"
    with pytest.raises((ProtocolError, ValueError)):
        extract(frame, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_runner_extract_verifies_digest_without_loading(tmp_path):
    extract(init(), tmp_path)
    assert (tmp_path / "data/message.txt").read_text() == "café ☕\n"


async def test_sdk_immutable_context_and_matching_response():
    reader = io.BytesIO(
        encode({"type": "api_result", "sequence": 1, "result": {"ok": True}, "error": None}, 65536)
    )
    writer = io.BytesIO()
    api = API(reader, writer, 65536)
    context = ToolContext({"project_id": "bound", "nested": {"list": [1]}}, api)
    assert context.project_id == "bound" and context.api is api
    with pytest.raises(TypeError):
        context.project_id = "other"
    with pytest.raises(TypeError):
        context.values["project_id"] = "other"
    with pytest.raises(TypeError):
        context.values["nested"]["list"][0] = 2
    with pytest.raises(AttributeError):
        _ = context.missing
    assert await context.api.call(uuid7(), {}) == {"ok": True}
    frame = json.loads(writer.getvalue())
    assert frame["sequence"] == 1 and "tenant_id" not in frame


@pytest.mark.parametrize(
    "response",
    [
        {"type": "cancel"},
        {"type": "api_result", "sequence": 2, "result": {}, "error": None},
        {"type": "api_result", "sequence": 1, "result": None, "error": {"code": "api_call_denied"}},
    ],
)
async def test_sdk_rejects_cancel_and_bad_responses(response):
    import asyncio

    api = API(io.BytesIO(encode(response, 65536)), io.BytesIO(), 65536)
    with pytest.raises((asyncio.CancelledError, ProtocolError, RuntimeError)):
        await api.call(uuid7(), {})


@pytest.mark.parametrize(
    "kind,value,valid",
    [
        ("context", "bound", True),
        ("context", "other", False),
        ("context", True, False),
        ("set", 1, True),
        ("set", True, False),
        ("number", 2, True),
        ("number", True, False),
        ("number", 9, False),
        ("path", "src/file.py", True),
        ("path", "src-other/file.py", False),
        ("path", "src/../escape.py", False),
    ],
)
async def test_concrete_scope_rules(kind, value, valid):
    rule = {
        "context": ContextEquality(kind="context_equal", argument="/value", context="/bound"),
        "set": SetMembership(kind="set_membership", argument="/value", values=(1, 2)),
        "number": NumericBounds(kind="numeric_bounds", argument="/value", minimum=1.0, maximum=3.0),
        "path": RelativePath(kind="relative_path", argument="/value", root="src"),
    }[kind]
    if valid:
        await enforce((rule,), {"value": value}, {"bound": "bound"}, None)
    else:
        with pytest.raises((ValueError, ExecutionDeniedError)):
            await enforce((rule,), {"value": value}, {"bound": "bound"}, None)


def test_transport_and_projection_are_declared_only():
    from src.catalog.models import ArgumentMapping, SourcePointer

    op = operation(
        path_template="/records/{record}",
        query_fields=("limit",),
        header_fields=("x_version",),
        mappings=(
            ArgumentMapping(
                location="path", target="record", value=SourcePointer(source="input", pointer="/id")
            ),
            ArgumentMapping(
                location="query",
                target="limit",
                value=SourcePointer(source="input", pointer="/limit"),
            ),
            ArgumentMapping(
                location="header",
                target="x_version",
                value=SourcePointer(source="context", pointer="/version"),
            ),
            ArgumentMapping(
                location="body",
                target="/nested",
                value=SourcePointer(source="input", pointer="/body"),
            ),
        ),
    )
    path, query, headers, body = transport(
        op, {"id": "abc", "limit": 5, "body": {"ok": True}}, {"version": "v1"}
    )
    assert (path, query, headers, body) == (
        "/records/abc",
        {"limit": "5"},
        {"x-version": "v1"},
        {"nested": {"ok": True}},
    )
    for bad in ["..", "evil/path", "%2f", "\r\nInjected", {"id": "object"}]:
        with pytest.raises(ExecutionDeniedError):
            transport(op, {"id": bad, "limit": 5, "body": {}}, {"version": "v1"})
    op = operation(response_projection=("/records",), redact_fields=("/records/0/token",))
    assert project(op, {"records": [{"token": "secret", "id": 1}], "hidden": "secret"}) == {
        "records": [{"id": 1}]
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://user:pass@example.com",  # pragma: allowlist secret -- synthetic rejection fixture
        "https://example.com/Path/..",
        "https://127.0.0.1",
        "https://[::1]",
        "https://2130706433",
        "https://example.com/?x=y",
        "https://example.com/#secret",
    ],
)
def test_destination_origin_policy(url):
    with pytest.raises(ValueError):
        checked_base_url(url)


def test_bundle_format_mode_and_aliases(tmp_path):
    from tests.test_bindings import profile

    credential = profile()
    ref = credential.gateway
    path = tmp_path / "bundle"
    entry = {
        "tenant_id": str(credential.tenant_id),
        "profile_id": str(credential.profile_id),
        **ref.model_dump(mode="json", exclude={"bundle"}),
        "token": "synthetic_value",
    }
    resolver = FileCredentialResolver(path)
    with pytest.raises(ExecutionDeniedError):
        resolver.read()
    path.write_text(json.dumps({"schema_version": 1, "entries": [entry]}))
    path.chmod(0o600)
    assert (
        resolver.resolve(credential.tenant_id, credential.profile_id, ref).get_secret_value()
        == "synthetic_value"
    )
    with pytest.raises(ExecutionDeniedError):
        resolver.resolve(uuid7(), credential.profile_id, ref)
    with pytest.raises(ExecutionDeniedError):
        resolver.resolve(
            credential.tenant_id,
            credential.profile_id,
            ref.model_copy(update={"license_id": uuid7()}),
        )
    path.chmod(0o666)
    with pytest.raises(ExecutionDeniedError):
        resolver.read()
    path.chmod(0o600)
    path.write_text(json.dumps({"schema_version": 1, "entries": [entry, entry]}))
    with pytest.raises(ExecutionDeniedError):
        resolver.read()
