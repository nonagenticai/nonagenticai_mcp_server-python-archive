"""Grant catalog DML to a pre-created runtime role using the migration connection."""

import argparse
import os

import psycopg
from psycopg import sql

TENANT_TABLES = (
    "mcp_tools",
    "mcp_tool_versions",
    "mcp_tool_version_files",
    "mcp_tool_version_operations",
    "mcp_toolkits",
    "mcp_toolkit_versions",
    "mcp_toolkit_version_tools",
    "mcp_toolkit_inclusions",
    "mcp_credential_profiles",
    "mcp_credential_refs",
    "mcp_bindings",
    "mcp_binding_tools",
    "mcp_binding_toolkits",
    "mcp_protocol_sessions",
    "mcp_access_events",
    "mcp_binding_ensures",
)
READ_TABLES = (
    "alembic_version",
    "mcp_catalog_state",
    "mcp_services",
    "mcp_service_versions",
    "mcp_operation_versions",
    "mcp_tool_shares",
    # Runtime tool sources (0009): ciphertext only; the key lives in worker/platform settings.
    "mcp_sources",
    # Tenant builder (0010): readable by the API, the worker (reference re-check) and platform.
    "mcp_service_grants",
    "mcp_operation_keys",
    "mcp_source_specs",
)
#: The runtime role writes tenant builder data ONLY through these SECURITY DEFINER functions.
BUILDER_FUNCTIONS = (
    "mcp_builder_source_create(uuid,uuid,text,text,jsonb,bytea,text,text,integer,integer,jsonb,integer)",
    "mcp_builder_source_update(uuid,text,bigint,text,jsonb,bytea,text,text,integer,integer,jsonb)",
    "mcp_builder_source_set_secret(uuid,text,bigint,bytea,jsonb)",
    "mcp_builder_source_set_status(uuid,text,bigint,text,jsonb)",
    "mcp_builder_service_create(uuid,jsonb,jsonb)",
    "mcp_builder_service_append(uuid,jsonb,bigint,jsonb)",
    "mcp_builder_operation_create(uuid,jsonb,text,jsonb,integer)",
    "mcp_builder_set_status(uuid,text,uuid,integer,text,jsonb)",
    "mcp_builder_publish(uuid,jsonb,jsonb,jsonb)",
    "mcp_builder_spec_put(uuid,uuid,text,integer,jsonb,integer,bigint)",
)
#: Evaluated by tool-version triggers, the broker and the builder's catalog reads.
REFERENCE_FUNCTION = "mcp_tenant_may_use_service(uuid,uuid,integer)"


def grant(dsn: str, role: str) -> None:
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION public.mcp_lock_access_tool(uuid,uuid,uuid,integer) TO {}"
            ).format(sql.Identifier(role))
        )
        # Platform toolkits (0008): the constant the binding-toolkit owner CHECK compares with.
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION public.mcp_platform_tenant() TO {}").format(
                sql.Identifier(role)
            )
        )
        for table in TENANT_TABLES + READ_TABLES:
            conn.execute(
                sql.SQL("GRANT SELECT ON public.{} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
        for table in TENANT_TABLES:
            conn.execute(
                sql.SQL("GRANT INSERT ON public.{} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
        for table, columns in {
            "mcp_tools": ("revision", "retired"),
            "mcp_tool_versions": ("status", "updated_by", "updated_at"),
            "mcp_toolkits": ("revision",),
            "mcp_toolkit_versions": ("status", "updated_by", "updated_at"),
            "mcp_credential_profiles": ("revision", "manifest", "updated_by", "updated_at"),
            "mcp_bindings": (
                "revision",
                "token_sha256",
                "spec",
                "tools",
                "enabled",
                "revoked",
                "expires_at",
                "credential_profile_id",
                "updated_by",
                "updated_at",
            ),
            # bindings/ensure (0007): identity columns stay immutable (also trigger-guarded).
            "mcp_binding_ensures": (
                "revision",
                "gateway_license_id",
                "credential_profile_id",
                "binding_id",
                "toolkits",
                "skipped",
                "token_issued_at",
                "updated_by",
                "updated_at",
            ),
        }.items():
            conn.execute(
                sql.SQL("GRANT UPDATE ({}) ON public.{} TO {}").format(
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.Identifier(table),
                    sql.Identifier(role),
                )
            )
        for table in (
            "mcp_credential_refs",
            "mcp_binding_tools",
            "mcp_binding_toolkits",
            "mcp_protocol_sessions",
        ):
            conn.execute(
                sql.SQL("GRANT DELETE ON public.{} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
        # Builtin provider per-call audit (0006): append-only, no read-back from the API.
        conn.execute(
            sql.SQL("GRANT INSERT ON public.mcp_tool_call_audit TO {}").format(sql.Identifier(role))
        )
        grant_builder(conn, role)
        grant_execution(conn, role, worker=False)


def grant_builder(conn, role: str) -> None:
    """API role: EXECUTE on the builder write functions and the reference rule (no table DML)."""
    for function in (*BUILDER_FUNCTIONS, REFERENCE_FUNCTION):
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION public.{} TO {}").format(
                sql.SQL(function), sql.Identifier(role)
            )
        )


def grant_execution(conn, role: str, *, worker: bool) -> None:
    role_sql = sql.Identifier(role)
    for table in (
        "mcp_runs",
        "mcp_run_events",
        "mcp_execution_slots",
        "mcp_tenant_execution_locks",
    ):
        conn.execute(
            sql.SQL("GRANT SELECT ON public.{} TO {}").format(sql.Identifier(table), role_sql)
        )
    for table in ("mcp_runs", "mcp_run_events", "mcp_tenant_execution_locks"):
        conn.execute(
            sql.SQL("GRANT INSERT ON public.{} TO {}").format(sql.Identifier(table), role_sql)
        )
    conn.execute(
        sql.SQL("GRANT USAGE ON SEQUENCE public.mcp_run_events_event_id_seq TO {}").format(role_sql)
    )
    conn.execute(
        sql.SQL("GRANT EXECUTE ON FUNCTION public.mcp_lock_execution_authority() TO {}").format(
            role_sql
        )
    )
    conn.execute(
        sql.SQL("GRANT UPDATE(cancel_requested) ON public.mcp_runs TO {}").format(role_sql)
    )
    if not worker:
        # MCP tasks (0011): the API surfaces a status message, records what fr reports about a
        # project start (external_ref only grows, guarded) and answers task inputs.
        conn.execute(
            sql.SQL("GRANT UPDATE(status_message,external_ref) ON public.mcp_runs TO {}").format(
                role_sql
            )
        )
        conn.execute(
            sql.SQL(
                "GRANT SELECT,INSERT,UPDATE(state,response,actor,answered_at,closed_at) "
                "ON public.mcp_task_inputs TO {}"
            ).format(role_sql)
        )
        # Run activity (0013): the API records fr channel events (approvals) on the feed.
        conn.execute(
            sql.SQL("GRANT SELECT,INSERT ON public.mcp_run_activity TO {}").format(role_sql)
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE public.mcp_run_activity_seq_seq TO {}").format(
                role_sql
            )
        )
    if worker:
        conn.execute(
            sql.SQL("GRANT SELECT,UPDATE ON public.mcp_task_inputs TO {}").format(role_sql)
        )
        # Run activity (0013): the external driver records phases and terminal outcomes.
        conn.execute(
            sql.SQL("GRANT SELECT,INSERT ON public.mcp_run_activity TO {}").format(role_sql)
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE public.mcp_run_activity_seq_seq TO {}").format(
                role_sql
            )
        )
        conn.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION public.mcp_expire_run_payloads(integer,integer) TO {}"
            ).format(role_sql)
        )
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION public.mcp_platform_tenant() TO {}").format(role_sql)
        )
        # The worker re-evaluates the reference rule per call; it writes run trace columns of
        # mcp_run_api_calls (method, path, http_status, duration_ms, reason, error_excerpt) via the table grant
        # below and reads sources, services, grants and operation keys via READ_TABLES.
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION public.{} TO {}").format(
                sql.SQL(REFERENCE_FUNCTION), role_sql
            )
        )
        for table in READ_TABLES + TENANT_TABLES:
            conn.execute(
                sql.SQL("GRANT SELECT ON public.{} TO {}").format(sql.Identifier(table), role_sql)
            )
        for table in (
            "mcp_runs",
            "mcp_execution_slots",
            "mcp_tenant_execution_locks",
            "mcp_run_api_calls",
            "mcp_api_permits",
            "mcp_api_rate_windows",
        ):
            conn.execute(
                sql.SQL("GRANT SELECT,INSERT,UPDATE ON public.{} TO {}").format(
                    sql.Identifier(table), role_sql
                )
            )
        for table in ("mcp_api_permits", "mcp_api_rate_windows"):
            conn.execute(
                sql.SQL("GRANT DELETE ON public.{} TO {}").format(sql.Identifier(table), role_sql)
            )


def grant_platform_sources(conn, role: str) -> None:
    """The platform operator role registers sources and appends their audit events."""
    role_sql = sql.Identifier(role)
    for table in ("mcp_sources", "mcp_source_events"):
        conn.execute(
            sql.SQL("GRANT SELECT,INSERT ON public.{} TO {}").format(
                sql.Identifier(table), role_sql
            )
        )
    conn.execute(
        sql.SQL("GRANT UPDATE({}) ON public.mcp_sources TO {}").format(
            sql.SQL(",").join(
                map(
                    sql.Identifier,
                    (
                        "revision",
                        "status",
                        "base_url",
                        "allow_internal_http",
                        "auth",
                        "token_ciphertext",
                        "token_set_at",
                        "idempotency_header",
                        "ca_file",
                        "spec_path",
                        "max_request_bytes",
                        "max_response_bytes",
                        "updated_by",
                        "updated_at",
                    ),
                )
            ),
            role_sql,
        )
    )


def grant_platform_builder(conn, role: str) -> None:
    """The operator shares platform services (grants) and names platform operations."""
    role_sql = sql.Identifier(role)
    conn.execute(sql.SQL("GRANT INSERT,DELETE ON public.mcp_service_grants TO {}").format(role_sql))
    conn.execute(sql.SQL("GRANT INSERT ON public.mcp_operation_keys TO {}").format(role_sql))
    conn.execute(
        sql.SQL("GRANT EXECUTE ON FUNCTION public.{} TO {}").format(
            sql.SQL(REFERENCE_FUNCTION), role_sql
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--worker", action="store_true", help="grant worker privileges only")
    parser.add_argument(
        "--platform", action="store_true", help="grant platform contract privileges only"
    )
    args = parser.parse_args()
    if args.worker and args.platform:
        parser.error("choose one dedicated role type")
    if args.worker or args.platform:
        with psycopg.connect(
            os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
        ) as conn:
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(args.role))
            )
            if args.worker:
                grant_execution(conn, args.role, worker=True)
            else:
                for table in READ_TABLES + TENANT_TABLES:
                    conn.execute(
                        sql.SQL("GRANT SELECT ON public.{} TO {}").format(
                            sql.Identifier(table), sql.Identifier(args.role)
                        )
                    )
                grant_platform_sources(conn, args.role)
                grant_platform_builder(conn, args.role)
                for table, columns in {
                    "mcp_services": ("revision",),
                    "mcp_service_versions": ("status", "updated_by", "updated_at"),
                    "mcp_operation_versions": ("status", "updated_by", "updated_at"),
                }.items():
                    conn.execute(
                        sql.SQL("GRANT INSERT ON public.{} TO {}").format(
                            sql.Identifier(table), sql.Identifier(args.role)
                        )
                    )
                    conn.execute(
                        sql.SQL("GRANT UPDATE({}) ON public.{} TO {}").format(
                            sql.SQL(",").join(map(sql.Identifier, columns)),
                            sql.Identifier(table),
                            sql.Identifier(args.role),
                        )
                    )
    else:
        grant(os.environ["DATABASE_URL"], args.role)


if __name__ == "__main__":
    main()
