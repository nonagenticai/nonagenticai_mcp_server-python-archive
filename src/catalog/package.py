"""Canonical packages; inspecting a package never imports uploaded code."""

import hashlib
import json
import re
import unicodedata
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from src.config import Settings


class PackageFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    path: str
    content: str


def canonical_path(path: str) -> str:
    if (
        not path
        or len(path.encode("utf-8")) > 512
        or "\\" in path
        or ":" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or unicodedata.normalize("NFC", path) != path
        or any(p in {"", ".", ".."} for p in path.split("/"))
    ):
        raise ValueError("package paths must be canonical relative POSIX paths")
    return path


def package_digest(files: tuple[PackageFile, ...]) -> str:
    """SHA-256 of compact UTF-8 JSON of sorted [path, exact content] pairs."""
    pairs = sorted([[f.path, f.content] for f in files])
    return hashlib.sha256(
        json.dumps(pairs, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def validate_package(
    files: tuple[PackageFile, ...], entrypoint: str, digest: str, settings: Settings
) -> None:
    if not 1 <= len(files) <= settings.max_package_files:
        raise ValueError("package file count outside configured limit")
    paths: set[str] = set()
    total = 0
    for file in files:
        path = canonical_path(file.path)
        if path in paths:
            raise ValueError("duplicate package path")
        paths.add(path)
        size = len(file.content.encode("utf-8"))
        if size > settings.max_package_file_bytes:
            raise ValueError("package file too large")
        total += size
    if total > settings.max_package_bytes:
        raise ValueError("package too large")
    for path in paths:
        parts = path.split("/")
        if any("/".join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError("package file/directory collision")
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entrypoint, re.ASCII):
        raise ValueError("entrypoint must be package.module:function")
    module = entrypoint.split(":")[0].replace(".", "/")
    if module + ".py" not in paths:
        raise ValueError("entrypoint module missing")
    if package_digest(files) != digest:
        raise ValueError("package digest mismatch")
