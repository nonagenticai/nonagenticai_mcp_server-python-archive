"""Platform-owned toolkits in tenant bindings; canonical operation on every tool version.

1. First-party toolkits are published by one reserved platform tenant
   (``public.mcp_platform_tenant()``, the same constant as ``src.config.PLATFORM_TENANT_ID``).
   ``mcp_binding_toolkits`` gains ``owner_tenant_id``: a binding pins exact published toolkit
   versions owned by its own tenant or by the platform tenant, never by another tenant. The
   foreign key now targets the owner's toolkit version and a CHECK rejects any third owner.
   The binding completeness check matches the owner against the spec's toolkit references (an
   omitted ``tenant_id``, as in phase-2 specs, is the binding's own tenant).
2. Every tool version manifest carries ``operation``: the agent platform's lowercase
   ``object.verb`` (``src.catalog.models.OPERATION_PATTERN``).

Self-contained; no application imports. Fix forward only.
"""

from alembic import op

revision = "0008_platform_toolkits"
down_revision = "0007_binding_ensures"
branch_labels = None
depends_on = None

PLATFORM_TENANT_ID = "01900000-0000-7000-8000-00000000f001"

SCHEMA_SQL = rf"""
CREATE FUNCTION public.mcp_platform_tenant() RETURNS uuid
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT '{PLATFORM_TENANT_ID}'::uuid $$;

ALTER TABLE public.mcp_binding_toolkits ADD COLUMN owner_tenant_id uuid;
UPDATE public.mcp_binding_toolkits SET owner_tenant_id=tenant_id;
-- Fire the deferred completeness checks the backfill queued (with the phase-2 function), so the
-- table has no pending trigger events when it is altered below; then restore deferral.
SET CONSTRAINTS ALL IMMEDIATE;
SET CONSTRAINTS ALL DEFERRED;
ALTER TABLE public.mcp_binding_toolkits ALTER COLUMN owner_tenant_id SET NOT NULL;
DO $$ DECLARE c record; BEGIN
    FOR c IN SELECT conname FROM pg_constraint
        WHERE conrelid='public.mcp_binding_toolkits'::regclass AND contype IN ('f','p')
    LOOP
        EXECUTE format('ALTER TABLE public.mcp_binding_toolkits DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE public.mcp_binding_toolkits
    ADD PRIMARY KEY (tenant_id,binding_id,owner_tenant_id,toolkit_id,version),
    ADD FOREIGN KEY (tenant_id,binding_id) REFERENCES public.mcp_bindings(tenant_id,binding_id),
    ADD FOREIGN KEY (owner_tenant_id,toolkit_id,version)
        REFERENCES public.mcp_toolkit_versions(tenant_id,toolkit_id,version),
    ADD CONSTRAINT mcp_binding_toolkits_owner CHECK (
        owner_tenant_id=tenant_id OR owner_tenant_id=public.mcp_platform_tenant());
CREATE INDEX mcp_binding_toolkits_owner_idx
    ON public.mcp_binding_toolkits(owner_tenant_id,toolkit_id,version);

CREATE OR REPLACE FUNCTION public.mcp_access_complete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE data jsonb; b public.mcp_bindings; p public.mcp_credential_profiles;
BEGIN
    IF TG_OP='DELETE' THEN data=to_jsonb(OLD); ELSE data=to_jsonb(NEW); END IF;
    IF TG_ARGV[0]='binding' THEN
        SELECT * INTO b FROM public.mcp_bindings WHERE tenant_id=(data->>'tenant_id')::uuid AND binding_id=(data->>'binding_id')::uuid;
        IF (SELECT count(*) FROM public.mcp_binding_tools WHERE tenant_id=b.tenant_id AND binding_id=b.binding_id) <> jsonb_array_length(b.tools)
           OR (SELECT count(*) FROM public.mcp_binding_toolkits WHERE tenant_id=b.tenant_id AND binding_id=b.binding_id) <> jsonb_array_length(b.spec->'toolkits')
           OR EXISTS (SELECT 1 FROM public.mcp_binding_tools r WHERE r.tenant_id=b.tenant_id AND r.binding_id=b.binding_id
                AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(b.tools) e WHERE e->>'public_name'=r.public_name
                    AND e->'tool'->>'tenant_id'=r.owner_tenant_id::text AND e->'tool'->>'id'=r.tool_id::text AND (e->'tool'->>'version')::integer=r.version))
           OR EXISTS (SELECT 1 FROM public.mcp_binding_toolkits r WHERE r.tenant_id=b.tenant_id AND r.binding_id=b.binding_id
                AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(b.spec->'toolkits') e
                    WHERE e->>'id'=r.toolkit_id::text AND (e->>'version')::integer=r.version
                    -- An omitted owner is the binding's own tenant (ToolkitReference).
                    AND coalesce(e->>'tenant_id',b.tenant_id::text)=r.owner_tenant_id::text)) THEN
            RAISE EXCEPTION 'incomplete binding selection' USING ERRCODE='23514';
        END IF;
    ELSE
        SELECT * INTO p FROM public.mcp_credential_profiles WHERE tenant_id=(data->>'tenant_id')::uuid AND profile_id=(data->>'profile_id')::uuid;
        IF (SELECT count(*) FROM public.mcp_credential_refs WHERE tenant_id=p.tenant_id AND profile_id=p.profile_id) <> jsonb_array_length(p.manifest->'services')+1
           OR EXISTS (SELECT 1 FROM public.mcp_credential_refs r WHERE r.tenant_id=p.tenant_id AND r.profile_id=p.profile_id
               AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements((p.manifest->'services') || jsonb_build_array((p.manifest->'gateway') || '{{"service":"__gateway"}}'::jsonb)) e
                   WHERE e->>'service'=r.service_key AND e->>'alias'=r.alias AND e->>'license_id'=r.license_id::text AND e->>'app_key'=r.app_key)) THEN
            RAISE EXCEPTION 'incomplete credential references' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NULL;
END $$;

ALTER TABLE public.mcp_tool_versions ADD CONSTRAINT mcp_tool_versions_operation CHECK (
    jsonb_typeof(manifest->'operation')='string'
    AND manifest->>'operation' ~ '^[a-z][a-z0-9_]{{0,31}}\.[a-z][a-z0-9_]{{0,31}}$');
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("platform toolkits are fix forward only")
