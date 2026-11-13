"""Trusted sandbox supervisor. This module is never called by API or worker."""

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from runner.protocol import ProtocolError, decode, encode
from runner.sdk import API, ToolContext

INIT_BYTES = 8388608


def extract(frame, root):
    files = frame["files"]
    paths = set()
    total = 0
    if not 1 <= len(files) <= 64:
        raise ProtocolError("file count")
    for file in files:
        if (
            type(file) is not dict
            or set(file) != {"path", "content"}
            or any(type(v) is not str for v in file.values())
        ):
            raise ProtocolError("invalid file")
        path = file["path"]
        if (
            not path
            or path in paths
            or len(path.encode()) > 512
            or "\\" in path
            or ":" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or unicodedata.normalize("NFC", path) != path
            or any(p in {"", ".", ".."} for p in path.split("/"))
        ):
            raise ProtocolError("invalid package path")
        paths.add(path)
        size = len(file["content"].encode())
        total += size
        if size > 262144 or total > 1048576:
            raise ProtocolError("package size")
    if any(
        any("/".join(path.split("/")[:i]) in paths for i in range(1, len(path.split("/"))))
        for path in paths
    ):
        raise ProtocolError("package collision")
    pairs = sorted([[f["path"], f["content"]] for f in files])
    digest = hashlib.sha256(
        json.dumps(pairs, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    if digest != frame["digest"] or not re.fullmatch(
        r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", frame["entrypoint"], re.ASCII
    ):
        raise ProtocolError("package identity")
    if frame["entrypoint"].split(":")[0].replace(".", "/") + ".py" not in paths:
        raise ProtocolError("missing entrypoint")
    for file in files:
        target = root / file["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        # Fresh emptyDir, exclusive file creation, no archives/symlinks.
        with target.open("x", encoding="utf-8", newline="") as stream:
            stream.write(file["content"])


async def run(reader, writer):
    frame = decode(await asyncio.to_thread(reader.readline, INIT_BYTES + 1), INIT_BYTES)
    if frame["type"] != "init":
        raise ProtocolError("init required")
    root = Path("/scratch/package")
    root.mkdir(mode=0o700)
    extract(frame, root)
    maximum = frame["limits"].get("frame_bytes")
    if type(maximum) is not int or not 128 <= maximum <= 1048576:
        raise ProtocolError("frame limit")
    writer.write(encode({"type": "ready", "protocol": 1}, maximum))
    writer.flush()
    sys.path.insert(0, str(root))
    module_name, function_name = frame["entrypoint"].split(":")
    # Loading uploaded code happens only here, in the isolated runner pod.
    function = getattr(importlib.import_module(module_name), function_name)
    if not inspect.iscoroutinefunction(function):
        raise ProtocolError("async callable required")
    result = await function(
        frame["inputs"], ToolContext(frame["context"], API(reader, writer, maximum))
    )
    writer.write(encode({"type": "result", "result": result}, maximum))
    writer.flush()


def main():
    # Reserve the original protocol FD before redirecting even low-level tool
    # writes to stdout/stderr onto the separately bounded stderr channel.
    writer = os.fdopen(os.dup(1), "wb", buffering=0)
    reader = sys.stdin.buffer
    os.dup2(2, 1)
    try:
        asyncio.run(run(reader, writer))
    except BaseException:
        writer.write(encode({"type": "error", "code": "runner_failed"}, 65536))
        writer.flush()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
