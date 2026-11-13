"""Tool-facing API and recursively immutable, non-secret context."""

import asyncio
from types import MappingProxyType

from runner.protocol import ProtocolError, decode, encode


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


class ApiStatusError(Exception):
    """The operation's upstream answered a status the operation declared as expected."""

    def __init__(self, status, body):
        super().__init__(status)
        self.status, self.body = status, body


class API:
    def __init__(self, reader, writer, maximum):
        self.reader, self.writer, self.maximum = reader, writer, maximum
        self.sequence = 0
        self.lock = asyncio.Lock()

    async def call(self, operation_id, arguments, idempotency_key=None):
        async with self.lock:
            self.sequence += 1
            self.writer.write(
                encode(
                    {
                        "type": "api_call",
                        "sequence": self.sequence,
                        "operation_id": str(operation_id),
                        "arguments": arguments,
                        "idempotency_key": idempotency_key,
                    },
                    self.maximum,
                )
            )
            self.writer.flush()
            response = decode(
                await asyncio.to_thread(self.reader.readline, self.maximum + 1), self.maximum
            )
            if response["type"] == "cancel":
                raise asyncio.CancelledError()
            if response["type"] != "api_result" or response["sequence"] != self.sequence:
                raise ProtocolError("unexpected response")
            error = response["error"]
            if error and error.get("code") == "http_status":
                raise ApiStatusError(error["status"], error["body"])
            if error:
                raise RuntimeError("api_call_denied")
            return response["result"]


class ToolContext:
    __slots__ = ("_values", "_api")

    def __init__(self, values, api):
        object.__setattr__(self, "_values", freeze(values))
        object.__setattr__(self, "_api", api)

    @property
    def values(self):
        return self._values

    @property
    def api(self):
        return self._api

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        raise TypeError("context is immutable")
