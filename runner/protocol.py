"""Protocol v1: bounded strict NDJSON. No identities or transport knobs in API frames."""

import json
import math
import re

VERSION = 1
FIELDS = {
    "init": (
        {"type", "protocol", "files", "digest", "entrypoint", "inputs", "context", "limits"},
        set(),
    ),
    "ready": ({"type", "protocol"}, set()),
    "api_call": ({"type", "sequence", "operation_id", "arguments"}, {"idempotency_key"}),
    "api_result": ({"type", "sequence", "result", "error"}, set()),
    "result": ({"type", "result"}, set()),
    "error": ({"type", "code"}, set()),
    "log": ({"type", "message"}, set()),
    "cancel": ({"type"}, set()),
}


class ProtocolError(ValueError):
    pass


def pairs(entries):
    value = {}
    for key, item in entries:
        if key in value:
            raise ProtocolError("duplicate field")
        value[key] = item
    return value


def json_value(value, depth=0, budget=None):
    budget = [0] if budget is None else budget
    budget[0] += 1
    if depth > 32 or budget[0] > 10000:
        raise ProtocolError("JSON complexity")
    if type(value) is dict:
        for item in value.values():
            json_value(item, depth + 1, budget)
    elif type(value) is list:
        for item in value:
            json_value(item, depth + 1, budget)
    elif type(value) is float and not math.isfinite(value):
        raise ProtocolError("nonfinite JSON")
    elif value is not None and type(value) not in {str, int, float, bool}:
        raise ProtocolError("invalid JSON")


def validate(frame):
    if type(frame) is not dict or frame.get("type") not in FIELDS:
        raise ProtocolError("unknown frame")
    required, optional = FIELDS[frame["type"]]
    if not required <= frame.keys() or not frame.keys() <= required | optional:
        raise ProtocolError("unknown or missing field")
    json_value(frame)
    kind = frame["type"]
    if kind in {"init", "ready"} and (
        type(frame["protocol"]) is not int or frame["protocol"] != VERSION
    ):
        raise ProtocolError("incompatible protocol")
    if kind in {"api_call", "api_result"} and (
        type(frame["sequence"]) is not int or not 1 <= frame["sequence"] <= 100
    ):
        raise ProtocolError("invalid sequence")
    if kind == "api_call":
        from uuid import UUID

        try:
            valid = type(frame["operation_id"]) is str and UUID(frame["operation_id"]).version == 7
        except ValueError:
            valid = False
        key = frame.get("idempotency_key")
        if (
            not valid
            or type(frame["arguments"]) is not dict
            or (
                key is not None
                and (type(key) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key))
            )
        ):
            raise ProtocolError("invalid API call")
    if kind == "init":
        if (
            type(frame["files"]) is not list
            or type(frame["inputs"]) is not dict
            or type(frame["context"]) is not dict
            or type(frame["limits"]) is not dict
            or type(frame["digest"]) is not str
            or type(frame["entrypoint"]) is not str
        ):
            raise ProtocolError("invalid init")
    if kind == "error" and (
        type(frame["code"]) is not str or not re.fullmatch(r"[a-z_]{1,64}", frame["code"])
    ):
        raise ProtocolError("invalid error")
    if kind == "log" and (
        type(frame["message"]) is not str or len(frame["message"].encode()) > 8192
    ):
        raise ProtocolError("invalid log")
    if kind == "api_result" and frame["error"] is not None:
        error = frame["error"]
        if error != {"code": "api_call_denied"} and not (
            type(error) is dict
            and error.keys() == {"code", "status", "body"}
            and error["code"] == "http_status"
            and type(error["status"]) is int
            and 400 <= error["status"] <= 499
            and type(error["body"]) in {dict, list, type(None)}
        ):
            raise ProtocolError("invalid API error")
    return frame


def encode(frame, maximum):
    validate(frame)
    data = (
        json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )
    if len(data) > maximum:
        raise ProtocolError("frame too large")
    return data


def decode(data, maximum):
    if len(data) > maximum or not data.endswith(b"\n") or b"\n" in data[:-1]:
        raise ProtocolError("invalid frame size or boundary")
    try:
        return validate(
            json.loads(
                data,
                object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("nonfinite JSON")),
            )
        )
    except ValueError, TypeError, RecursionError, UnicodeError:
        raise ProtocolError("invalid frame") from None


class Decoder:
    def __init__(self, frame_bytes, total_bytes, max_frames):
        self.frame_bytes, self.total_bytes, self.max_frames = frame_bytes, total_bytes, max_frames
        self.buffer = bytearray()
        self.total = self.count = 0

    def feed(self, chunk):
        self.total += len(chunk)
        if self.total > self.total_bytes:
            raise ProtocolError("output limit")
        self.buffer.extend(chunk)
        result = []
        while b"\n" in self.buffer:
            end = self.buffer.index(b"\n") + 1
            self.count += 1
            if self.count > self.max_frames:
                raise ProtocolError("frame count")
            result.append(decode(bytes(self.buffer[:end]), self.frame_bytes))
            del self.buffer[:end]
        if len(self.buffer) >= self.frame_bytes:
            raise ProtocolError("unterminated frame")
        return result
