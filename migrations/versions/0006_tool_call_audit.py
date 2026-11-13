"""Append-only per-call audit of builtin provider tool calls. Fix forward only.

One row per tools/call a non-binding principal (care-bus tenant licence, Keycloak service)
makes, including calls refused by scope (status 'denied'). Ported from main's audit_logs rows
(src/tool_call_audit.py); this schema has no audit_logs table. The runtime role gets INSERT only.
"""

from alembic import op

revision = "0006_tool_call_audit"
down_revision = "0005_execution_controls"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
CREATE TABLE public.mcp_tool_call_audit (
    audit_id uuid PRIMARY KEY DEFAULT uuidv7(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    tool text NOT NULL CHECK (length(tool) BETWEEN 1 AND 256),
    status text NOT NULL CHECK (status IN ('success','failure','denied')),
    principal_kind text NOT NULL CHECK (principal_kind IN ('carebus_licence','keycloak_service')),
    auth_method text NOT NULL CHECK (auth_method IN ('license','keycloak')),
    principal text NOT NULL CHECK (length(principal) BETWEEN 1 AND 256),
    tenant_id text CHECK (tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 256),
    latency_ms integer NOT NULL CHECK (latency_ms >= 0),
    details jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details)='object'
        AND octet_length(details::text) <= 16384)
);
CREATE INDEX mcp_tool_call_audit_created_idx ON public.mcp_tool_call_audit(created_at);
CREATE INDEX mcp_tool_call_audit_tenant_idx ON public.mcp_tool_call_audit(tenant_id,created_at)
    WHERE tenant_id IS NOT NULL;
CREATE FUNCTION public.mcp_guard_tool_call_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tool call audit is append-only' USING ERRCODE='23514';
END $$;
REVOKE ALL ON FUNCTION public.mcp_guard_tool_call_audit() FROM PUBLIC;
CREATE TRIGGER tool_call_audit_guard BEFORE UPDATE OR DELETE ON public.mcp_tool_call_audit
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_tool_call_audit();
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("tool call audit is fix forward only")
