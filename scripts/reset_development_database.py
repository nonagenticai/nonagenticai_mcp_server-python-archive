"""One-time reset of the explicitly named dedicated MCP development database.

Never invoked by application startup, Compose, or a recurring init container.
"""

import argparse
import os

import psycopg
from psycopg import sql


def reset(dsn: str, expected_database: str, *, writers_stopped: bool) -> None:
    if not writers_stopped or expected_database in {"postgres", "template0", "template1", ""}:
        raise ValueError("stop all MCP writers and name the dedicated non-system database")
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        row = conn.execute("SELECT current_database(), current_user").fetchone()
        if row is None or row[0] != expected_database:
            raise ValueError("connected database does not match explicit reset target")
        # Dedicated database only: refuse unknown non-system schemas or foreign owners.
        schemas = conn.execute(
            "SELECT nspname,pg_get_userbyid(nspowner) FROM pg_namespace "
            "WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'"
        ).fetchall()
        if any(
            name not in {"public", "archive"} or owner not in {row[1], "pg_database_owner"}
            for name, owner in schemas
        ):
            raise ValueError(
                "unexpected schema or owner; review the dedicated target with its owner"
            )
        conn.execute("SET LOCAL lock_timeout='5s'")
        for name, _ in schemas:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
        conn.execute("CREATE SCHEMA public")
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    reset(os.environ["DATABASE_URL"], args.expected_database, writers_stopped=args.writers_stopped)


if __name__ == "__main__":
    main()
