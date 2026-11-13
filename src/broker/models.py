"""Broker result types. Destinations live in the runtime source registry (src.sources)."""


class UpstreamStatusError(Exception):  # noqa: N818 -- a result, not a failure
    """An upstream answered an operation's declared ``expected_error_statuses`` entry. ``body`` is
    the projected and redacted JSON, or None when it could not be projected or was unsafe."""

    def __init__(self, status: int, body) -> None:
        super().__init__(status)
        self.status, self.body = status, body
