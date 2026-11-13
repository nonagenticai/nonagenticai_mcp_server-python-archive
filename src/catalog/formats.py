"""Named string formats: the only way a schema constrains string shape.

Raw ``pattern`` is never accepted: a schema author's regex runs on untrusted input (ReDoS).
Each format here is fixed code: anchored, length-checked before any regex, and using only
linear-time patterns (single character classes, no nested quantifiers). ``format: "<name>"``
must name an entry; the validator rejects unknown names and enforces known ones
(``validate_instance`` and ``src/toolkit_contract`` both use ``FORMAT_CHECKER``).
"""

import re
from collections.abc import Callable
from uuid import UUID

from jsonschema import FormatChecker

_CONTAINER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_BRANCH_CHARS = re.compile(r"[A-Za-z0-9._/-]+")
_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_URL_PATH = re.compile(r"[A-Za-z0-9._~-]+")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._@+=,~-]+")
_PACKAGE_CHARS = re.compile(r"[A-Za-z0-9@][A-Za-z0-9._/@+-]*")
_KEY = re.compile(r"[A-Za-z0-9._:-]+")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def container_name(value: str) -> bool:
    """DNS-label style workspace name: 1..63 lowercase alphanumerics and inner hyphens."""
    return len(value) <= 63 and _CONTAINER.fullmatch(value) is not None


def git_branch(value: str) -> bool:
    """A conservative subset of git check-ref-format for a branch name."""
    if not 1 <= len(value) <= 200 or not _BRANCH_CHARS.fullmatch(value):
        return False
    if value.startswith(("-", "/", ".")) or value.endswith(("/", ".", ".lock")):
        return False
    return ".." not in value and "//" not in value and "/." not in value


def repo_url_https(value: str) -> bool:
    """https://host[:port]/owner/repo[.git], no credentials, query, fragment or traversal."""
    if len(value) > 512 or not value.startswith("https://"):
        return False
    authority, _, path = value[len("https://") :].partition("/")
    host, _, port = authority.partition(":")
    if not host or len(host) > 253 or not _HOST.fullmatch(host):
        return False
    if port and not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        return False
    segments = path.split("/")
    if len(segments) < 2:
        return False
    return all(s not in {".", ".."} and _URL_PATH.fullmatch(s) for s in segments)


def relative_path(value: str) -> bool:
    """Workspace-relative file path: no leading '/', no '..', '.', or empty segments."""
    if not 1 <= len(value) <= 1024 or value.startswith("/"):
        return False
    return all(s not in {".", ".."} and _PATH_SEGMENT.fullmatch(s) for s in value.split("/"))


def package_name(value: str) -> bool:
    """Package-manager name (pypi, npm incl. @scope/name, go, cargo): never starts with '-'."""
    if not 1 <= len(value) <= 128 or not _PACKAGE_CHARS.fullmatch(value):
        return False
    return ".." not in value and "//" not in value and not value.endswith("/")


def uuid(value: str) -> bool:
    """Canonical hyphenated UUID (any version), lowercase or uppercase hex."""
    if len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except ValueError:
        return False


def idempotency_key(value: str) -> bool:
    return 8 <= len(value) <= 128 and _KEY.fullmatch(value) is not None


def resume_token(value: str) -> bool:
    return 3 <= len(value) <= 2048 and _TOKEN.fullmatch(value) is not None


FORMATS: dict[str, Callable[[str], bool]] = {
    "container-name": container_name,
    "git-branch": git_branch,
    "repo-url-https": repo_url_https,
    "relative-path": relative_path,
    "package-name": package_name,
    "uuid": uuid,
    "idempotency-key": idempotency_key,
    "resume-token": resume_token,
}

# Only registered formats; jsonschema's defaults (e.g. regex, uri) are deliberately absent.
FORMAT_CHECKER = FormatChecker(formats=())
for _name, _check in FORMATS.items():
    FORMAT_CHECKER.checks(_name)(lambda v, _c=_check: not isinstance(v, str) or _c(v))
