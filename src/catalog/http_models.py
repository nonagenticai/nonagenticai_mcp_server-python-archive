"""Strict management envelopes, separate from immutable artifact contracts."""

from typing import Annotated

from pydantic import Field

from src.catalog.models import Contract, PositiveVersion, ToolManifest
from src.catalog.package import PackageFile


class ToolUpload(Contract):
    manifest: ToolManifest
    # Keep entries until validation so duplicate paths cannot disappear in a map.
    files: Annotated[tuple[PackageFile, ...], Field(min_length=1)]


class ToolAppend(ToolUpload):
    expected_revision: PositiveVersion
