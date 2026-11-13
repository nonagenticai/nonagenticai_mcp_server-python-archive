"""Licence-authority identities may be any UUID version; per-(tenant, product) binding ensures.

1. licences mints tenant and licence rows by SQL migration with gen_random_uuid() (UUIDv4), so
   the UUIDv7 CHECKs 0002 and 0004 put on tenant/licence columns would reject real tenants on the
   binding path. They are dropped. Identities mcp mints itself (run, worker, permit, ensure) stay v7.
2. mcp_binding_ensures records the one binding that POST
   /mcp/internal/tenants/{t}/bindings/ensure provisions per (tenant, product). It stores no token
   or token digest (the binding row holds the digest). Rows are never deleted.

Self-contained; no application imports. Fix forward only.
"""

from alembic import op

revision = "0007_binding_ensures"
down_revision = "0006_tool_call_audit"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
-- 0002 added <table>_<column>_v7 CHECKs to every uuid column existing then; keep them for
-- mcp-minted identities, drop them for licence-authority tenant/licence identities.
DO $$ DECLARE c record; BEGIN
    FOR c IN SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name LIKE 'mcp_%' AND udt_name='uuid'
          AND column_name IN ('tenant_id','owner_tenant_id','grantee_tenant_id','license_id',
                              'gateway_license_id')
    LOOP
        EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT IF EXISTS %I',
            c.table_name, c.table_name || '_' || c.column_name || '_v7');
    END LOOP;
END $$;
ALTER TABLE public.mcp_runs DROP CONSTRAINT mcp_runs_tenant_id_check;
ALTER TABLE public.mcp_runs DROP CONSTRAINT mcp_runs_gateway_license_id_check;
ALTER TABLE public.mcp_tenant_execution_locks DROP CONSTRAINT mcp_tenant_execution_locks_tenant_id_check;
ALTER TABLE public.mcp_run_api_calls DROP CONSTRAINT mcp_run_api_calls_license_id_check;
ALTER TABLE public.mcp_api_rate_windows DROP CONSTRAINT mcp_api_rate_windows_tenant_id_check;

CREATE TABLE public.mcp_binding_ensures (
    ensure_id uuid PRIMARY KEY CHECK (uuid_extract_version(ensure_id)=7),
    tenant_id uuid NOT NULL,
    product text NOT NULL CHECK (product ~ '^[a-z][a-z0-9-]{0,63}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    gateway_license_id uuid,
    credential_profile_id uuid,
    binding_id uuid,
    toolkits jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(toolkits)='array'
        AND jsonb_array_length(toolkits) <= 32),
    skipped jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(skipped)='array'
        AND jsonb_array_length(skipped) <= 32),
    token_issued_at timestamptz,
    created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 256),
    updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id,product),
    FOREIGN KEY (tenant_id,binding_id) REFERENCES public.mcp_bindings(tenant_id,binding_id),
    FOREIGN KEY (tenant_id,credential_profile_id)
        REFERENCES public.mcp_credential_profiles(tenant_id,profile_id),
    CHECK ((binding_id IS NULL) = (token_issued_at IS NULL)),
    CHECK (binding_id IS NULL OR (gateway_license_id IS NOT NULL AND credential_profile_id IS NOT NULL))
);
CREATE FUNCTION public.mcp_guard_binding_ensure() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'binding ensures cannot be deleted' USING ERRCODE='23514';
    END IF;
    IF NEW.ensure_id <> OLD.ensure_id OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.product <> OLD.product OR NEW.created_by <> OLD.created_by
       OR NEW.created_at <> OLD.created_at OR NEW.revision <> OLD.revision+1 THEN
        RAISE EXCEPTION 'binding ensure identity is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.mcp_guard_binding_ensure() FROM PUBLIC;
CREATE TRIGGER binding_ensure_guard BEFORE UPDATE OR DELETE ON public.mcp_binding_ensures
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_binding_ensure();
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("binding ensures are fix forward only")
