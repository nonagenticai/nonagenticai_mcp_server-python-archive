"""Static gate: no asyncpg driver API may reappear in the tree.

WHY THIS FILE EXISTS. This project migrated from asyncpg to psycopg v3. In the sibling
enterprise_mcp_server three modules were never converted and kept calling asyncpg's API.
Because psycopg has no `pool.acquire()` / `conn.fetch()` / `conn.fetchval()` /
`conn.fetchrow()`, every one of those call sites raised AttributeError at runtime -- and it
went unnoticed for months because no check could see it (ported from enterprise #152).

tests/test_audit_db_integration.py runs the audit retention path against real PostgreSQL.
This file is the cheap, fast complement: it catches an asyncpg-style call the day someone
adds it, anywhere in the tree, without needing a database.

`pyproject.toml` declares `psycopg[binary,pool]`; asyncpg is not a dependency at all,
so any of these names is a bug and never a deliberate choice of driver.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# asyncpg-only names. psycopg's equivalents are pool.connection(), conn.execute()
# (which returns a cursor), cur.fetchall(), cur.fetchone() and cur.rowcount.
FORBIDDEN_METHODS = {"fetchval", "fetchrow"}
FORBIDDEN_ON_POOL = {"acquire"}


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    files = [REPO_ROOT / p for p in out]
    assert len(files) >= 15, (
        f"only {len(files)} tracked .py files discovered; the glob is broken and this "
        "gate would pass without inspecting anything"
    )
    return files


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            attr = func.attr
            if attr in FORBIDDEN_METHODS:
                self.hits.append((node.lineno, f".{attr}()"))
            elif attr in FORBIDDEN_ON_POOL:
                base = func.value
                # flag `<anything>.pool.acquire()` and a bare `pool.acquire()`
                if (isinstance(base, ast.Attribute) and base.attr == "pool") or (
                    isinstance(base, ast.Name) and base.id == "pool"
                ):
                    self.hits.append((node.lineno, f".pool.{attr}()"))
        self.generic_visit(node)


@pytest.mark.parametrize(
    "path", _tracked_python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_asyncpg_api(path: Path) -> None:
    visitor = _Visitor()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    rel = path.relative_to(REPO_ROOT)
    assert not visitor.hits, "asyncpg API in a psycopg codebase: " + ", ".join(
        f"{rel}:{line} {what}" for line, what in visitor.hits
    )


def test_asyncpg_is_not_a_dependency() -> None:
    """The driver itself must stay gone, so the names above can never be valid."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "asyncpg" not in pyproject
    assert "psycopg" in pyproject
