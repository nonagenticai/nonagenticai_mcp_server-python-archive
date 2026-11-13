"""Exact toolkit snapshots, credential aliases, binding authority and session identity.

No tokens or credential values are stored. Self-contained; no application imports.
"""

from alembic import op

revision = "0002_toolkits_bindings"
down_revision = "0001_executable_catalog"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
CREATE TABLE public.mcp_toolkits (
    tenant_id uuid NOT NULL, toolkit_id uuid NOT NULL, name text NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,toolkit_id), UNIQUE (tenant_id,name)
);
CREATE TABLE public.mcp_toolkit_versions (
    tenant_id uuid NOT NULL, toolkit_id uuid NOT NULL, version integer NOT NULL CHECK (version > 0),
    manifest jsonb NOT NULL, tools jsonb NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','disabled','retired')),
    created_by jsonb NOT NULL, updated_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    creation_tx text NOT NULL DEFAULT pg_current_xact_id()::text,
    PRIMARY KEY (tenant_id,toolkit_id,version),
    FOREIGN KEY (tenant_id,toolkit_id) REFERENCES public.mcp_toolkits(tenant_id,toolkit_id),
    CHECK (manifest ?& ARRAY['schema_version','tenant_id','toolkit_id','version','tools','includes']
        AND manifest->>'schema_version'='1' AND manifest->>'tenant_id'=tenant_id::text
        AND manifest->>'toolkit_id'=toolkit_id::text AND (manifest->>'version')::integer=version),
    CHECK (jsonb_typeof(tools)='array' AND jsonb_array_length(tools) BETWEEN 1 AND 128)
);
CREATE TABLE public.mcp_toolkit_version_tools (
    tenant_id uuid NOT NULL, toolkit_id uuid NOT NULL, version integer NOT NULL,
    owner_tenant_id uuid NOT NULL, tool_id uuid NOT NULL, tool_version integer NOT NULL,
    public_name text NOT NULL,
    PRIMARY KEY (tenant_id,toolkit_id,version,public_name),
    FOREIGN KEY (tenant_id,toolkit_id,version) REFERENCES public.mcp_toolkit_versions(tenant_id,toolkit_id,version),
    FOREIGN KEY (owner_tenant_id,tool_id,tool_version) REFERENCES public.mcp_tool_versions(tenant_id,tool_id,version)
);
CREATE TABLE public.mcp_toolkit_inclusions (
    tenant_id uuid NOT NULL, toolkit_id uuid NOT NULL, version integer NOT NULL,
    included_id uuid NOT NULL, included_version integer NOT NULL,
    PRIMARY KEY (tenant_id,toolkit_id,version,included_id,included_version),
    FOREIGN KEY (tenant_id,toolkit_id,version) REFERENCES public.mcp_toolkit_versions(tenant_id,toolkit_id,version),
    FOREIGN KEY (tenant_id,included_id,included_version) REFERENCES public.mcp_toolkit_versions(tenant_id,toolkit_id,version),
    CHECK (toolkit_id <> included_id)
);
CREATE TABLE public.mcp_credential_profiles (
    tenant_id uuid NOT NULL, profile_id uuid NOT NULL, name text NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0), manifest jsonb NOT NULL,
    created_by jsonb NOT NULL, updated_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,profile_id), UNIQUE (tenant_id,name),
    CHECK (manifest ?& ARRAY['schema_version','tenant_id','profile_id','gateway','services']
        AND manifest->>'schema_version'='1' AND manifest->>'tenant_id'=tenant_id::text
        AND manifest->>'profile_id'=profile_id::text)
);
CREATE TABLE public.mcp_credential_refs (
    tenant_id uuid NOT NULL, profile_id uuid NOT NULL, service_key text NOT NULL,
    alias text NOT NULL CHECK (alias ~ '^[a-z][a-z0-9_]{0,63}$'),
    license_id uuid NOT NULL, app_key text NOT NULL,
    PRIMARY KEY (tenant_id,profile_id,service_key),
    FOREIGN KEY (tenant_id,profile_id) REFERENCES public.mcp_credential_profiles(tenant_id,profile_id)
);
CREATE TABLE public.mcp_bindings (
    tenant_id uuid NOT NULL, binding_id uuid NOT NULL, gateway_license_id uuid NOT NULL,
    credential_profile_id uuid NOT NULL, revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[a-f0-9]{64}$'),
    spec jsonb NOT NULL, tools jsonb NOT NULL,
    enabled boolean NOT NULL, revoked boolean NOT NULL DEFAULT false, expires_at timestamptz NOT NULL,
    created_by jsonb NOT NULL, updated_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id,binding_id),
    FOREIGN KEY (tenant_id,credential_profile_id) REFERENCES public.mcp_credential_profiles(tenant_id,profile_id),
    CHECK (spec ?& ARRAY['schema_version','credential_profile_id','context','expires_at','enabled']
        AND spec->>'schema_version'='1' AND spec->>'credential_profile_id'=credential_profile_id::text
        AND spec->'context'->>'tenant_id'=tenant_id::text AND (spec->>'enabled')::boolean=enabled
        AND (spec->>'expires_at')::timestamptz=expires_at),
    CHECK (jsonb_typeof(tools)='array' AND jsonb_array_length(tools) <= 128)
);
CREATE TABLE public.mcp_binding_toolkits (
    tenant_id uuid NOT NULL, binding_id uuid NOT NULL, toolkit_id uuid NOT NULL, version integer NOT NULL,
    PRIMARY KEY (tenant_id,binding_id,toolkit_id,version),
    FOREIGN KEY (tenant_id,binding_id) REFERENCES public.mcp_bindings(tenant_id,binding_id),
    FOREIGN KEY (tenant_id,toolkit_id,version) REFERENCES public.mcp_toolkit_versions(tenant_id,toolkit_id,version)
);
CREATE TABLE public.mcp_binding_tools (
    tenant_id uuid NOT NULL, binding_id uuid NOT NULL, owner_tenant_id uuid NOT NULL,
    tool_id uuid NOT NULL, version integer NOT NULL, public_name text NOT NULL,
    PRIMARY KEY (tenant_id,binding_id,public_name),
    FOREIGN KEY (tenant_id,binding_id) REFERENCES public.mcp_bindings(tenant_id,binding_id),
    FOREIGN KEY (owner_tenant_id,tool_id,version) REFERENCES public.mcp_tool_versions(tenant_id,tool_id,version)
);
CREATE TABLE public.mcp_protocol_sessions (
    session_sha256 text PRIMARY KEY CHECK (session_sha256 ~ '^[a-f0-9]{64}$'),
    tenant_id uuid NOT NULL, binding_id uuid NOT NULL, gateway_license_id uuid NOT NULL,
    token_sha256 text NOT NULL, expires_at timestamptz NOT NULL,
    FOREIGN KEY (tenant_id,binding_id) REFERENCES public.mcp_bindings(tenant_id,binding_id)
);
CREATE INDEX mcp_sessions_expiry_idx ON public.mcp_protocol_sessions(expires_at);
CREATE INDEX mcp_bindings_expiry_idx ON public.mcp_bindings(tenant_id,expires_at) WHERE enabled AND NOT revoked;
CREATE INDEX mcp_toolkit_members_reverse_idx ON public.mcp_toolkit_version_tools(owner_tenant_id,tool_id,tool_version);
CREATE TABLE public.mcp_access_events (
    event_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, subject text NOT NULL,
    license_id uuid NOT NULL, entity_id uuid NOT NULL, revision bigint NOT NULL,
    action text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX mcp_access_events_tenant_idx ON public.mcp_access_events(tenant_id,event_id);

CREATE TRIGGER toolkit_identity_guard BEFORE UPDATE OR DELETE ON public.mcp_toolkits
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_identity();
CREATE TRIGGER toolkit_snapshot_guard BEFORE UPDATE OR DELETE ON public.mcp_toolkit_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_snapshot();
CREATE TRIGGER toolkit_visibility AFTER UPDATE ON public.mcp_toolkit_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_bump_generation();
CREATE FUNCTION public.mcp_guard_access_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'access history cannot be deleted' USING ERRCODE='23514'; END IF;
    IF NEW.revision <> OLD.revision+1 OR NEW.tenant_id <> OLD.tenant_id THEN
        RAISE EXCEPTION 'access revision conflict' USING ERRCODE='23514';
    END IF;
    IF TG_TABLE_NAME='mcp_bindings' THEN
        IF (to_jsonb(NEW)-ARRAY['revision','token_sha256','spec','tools','enabled','revoked','expires_at','credential_profile_id','updated_by','updated_at']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['revision','token_sha256','spec','tools','enabled','revoked','expires_at','credential_profile_id','updated_by','updated_at']) OR
           (OLD.revoked AND NOT NEW.revoked) THEN
            RAISE EXCEPTION 'immutable binding identity or revoked binding' USING ERRCODE='23514';
        END IF;
    ELSIF (to_jsonb(NEW)-ARRAY['revision','manifest','updated_by','updated_at']) IS DISTINCT FROM
          (to_jsonb(OLD)-ARRAY['revision','manifest','updated_by','updated_at']) THEN
        RAISE EXCEPTION 'immutable profile identity' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER binding_update_guard BEFORE UPDATE OR DELETE ON public.mcp_bindings
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_access_update();
CREATE TRIGGER profile_update_guard BEFORE UPDATE OR DELETE ON public.mcp_credential_profiles
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_access_update();
CREATE FUNCTION public.mcp_access_generation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
    UPDATE public.mcp_catalog_state SET generation=generation+1 WHERE singleton;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.mcp_access_generation() FROM PUBLIC;
CREATE TRIGGER binding_generation AFTER INSERT OR UPDATE ON public.mcp_bindings
    FOR EACH ROW EXECUTE FUNCTION public.mcp_access_generation();
CREATE TRIGGER profile_generation AFTER INSERT OR UPDATE ON public.mcp_credential_profiles
    FOR EACH ROW EXECUTE FUNCTION public.mcp_access_generation();

CREATE FUNCTION public.mcp_guard_toolkit_child() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v public.mcp_toolkit_versions;
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'toolkit children are immutable' USING ERRCODE='23514'; END IF;
    SELECT * INTO v FROM public.mcp_toolkit_versions
        WHERE tenant_id=NEW.tenant_id AND toolkit_id=NEW.toolkit_id AND version=NEW.version FOR UPDATE;
    IF v.status <> 'draft' OR v.creation_tx <> pg_current_xact_id()::text THEN
        RAISE EXCEPTION 'toolkit children must be atomic' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER toolkit_tool_guard BEFORE INSERT OR UPDATE OR DELETE ON public.mcp_toolkit_version_tools
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_toolkit_child();
CREATE TRIGGER toolkit_inclusion_guard BEFORE INSERT OR UPDATE OR DELETE ON public.mcp_toolkit_inclusions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_toolkit_child();
CREATE FUNCTION public.mcp_toolkit_complete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM public.mcp_toolkit_version_tools WHERE tenant_id=NEW.tenant_id AND toolkit_id=NEW.toolkit_id AND version=NEW.version)
        <> jsonb_array_length(NEW.tools) OR
       (SELECT count(*) FROM public.mcp_toolkit_inclusions WHERE tenant_id=NEW.tenant_id AND toolkit_id=NEW.toolkit_id AND version=NEW.version)
        <> jsonb_array_length(NEW.manifest->'includes') THEN
        RAISE EXCEPTION 'incomplete toolkit snapshot' USING ERRCODE='23514';
    END IF;
    IF EXISTS (SELECT 1 FROM public.mcp_toolkit_version_tools t
        WHERE t.tenant_id=NEW.tenant_id AND t.toolkit_id=NEW.toolkit_id AND t.version=NEW.version
        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.tools) e
            WHERE e->>'public_name'=t.public_name AND e->'tool'->>'tenant_id'=t.owner_tenant_id::text
                AND e->'tool'->>'id'=t.tool_id::text AND (e->'tool'->>'version')::integer=t.tool_version)) THEN
        RAISE EXCEPTION 'toolkit reference mismatch' USING ERRCODE='23514';
    END IF;
    IF EXISTS (SELECT 1 FROM public.mcp_toolkit_inclusions r
        WHERE r.tenant_id=NEW.tenant_id AND r.toolkit_id=NEW.toolkit_id AND r.version=NEW.version
        AND NOT NEW.manifest->'includes' @> jsonb_build_array(jsonb_build_object('id',r.included_id::text,'version',r.included_version))) THEN
        RAISE EXCEPTION 'toolkit inclusion mismatch' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER toolkit_complete AFTER INSERT ON public.mcp_toolkit_versions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.mcp_toolkit_complete();
CREATE FUNCTION public.mcp_toolkit_publication() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE d record;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status <> 'draft' THEN RAISE EXCEPTION 'new toolkit must be draft' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
    IF NEW.status <> 'published' OR OLD.status='published' THEN RETURN NEW; END IF;
    FOR d IN SELECT v.status,t.retired,r.owner_tenant_id,r.tool_id
        FROM public.mcp_toolkit_version_tools r JOIN public.mcp_tool_versions v
        ON (v.tenant_id,v.tool_id,v.version)=(r.owner_tenant_id,r.tool_id,r.tool_version)
        JOIN public.mcp_tools t ON (t.tenant_id,t.tool_id)=(v.tenant_id,v.tool_id)
        WHERE r.tenant_id=NEW.tenant_id AND r.toolkit_id=NEW.toolkit_id AND r.version=NEW.version
        ORDER BY v.tenant_id,v.tool_id,v.version FOR SHARE OF v,t
    LOOP
        IF d.status <> 'published' OR d.retired OR (d.owner_tenant_id <> NEW.tenant_id AND NOT EXISTS (
            SELECT 1 FROM public.mcp_tool_shares WHERE owner_tenant_id=d.owner_tenant_id AND tool_id=d.tool_id AND grantee_tenant_id=NEW.tenant_id
        )) THEN RAISE EXCEPTION 'unavailable toolkit tool' USING ERRCODE='23514'; END IF;
    END LOOP;
    FOR d IN SELECT v.status FROM public.mcp_toolkit_inclusions r JOIN public.mcp_toolkit_versions v
        ON (v.tenant_id,v.toolkit_id,v.version)=(r.tenant_id,r.included_id,r.included_version)
        WHERE r.tenant_id=NEW.tenant_id AND r.toolkit_id=NEW.toolkit_id AND r.version=NEW.version
        ORDER BY v.toolkit_id,v.version FOR SHARE OF v
    LOOP
        IF d.status <> 'published' THEN RAISE EXCEPTION 'unavailable toolkit inclusion' USING ERRCODE='23514'; END IF;
    END LOOP;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.mcp_toolkit_publication() FROM PUBLIC;
CREATE TRIGGER toolkit_publication BEFORE INSERT OR UPDATE ON public.mcp_toolkit_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_toolkit_publication();
CREATE FUNCTION public.mcp_guard_access_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'access events are append only' USING ERRCODE='23514'; END $$;
CREATE TRIGGER access_event_guard BEFORE UPDATE OR DELETE ON public.mcp_access_events
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_access_event();

CREATE FUNCTION public.mcp_access_complete() RETURNS trigger LANGUAGE plpgsql AS $$
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
                AND NOT b.spec->'toolkits' @> jsonb_build_array(jsonb_build_object('id',r.toolkit_id::text,'version',r.version))) THEN
            RAISE EXCEPTION 'incomplete binding selection' USING ERRCODE='23514';
        END IF;
    ELSE
        SELECT * INTO p FROM public.mcp_credential_profiles WHERE tenant_id=(data->>'tenant_id')::uuid AND profile_id=(data->>'profile_id')::uuid;
        IF (SELECT count(*) FROM public.mcp_credential_refs WHERE tenant_id=p.tenant_id AND profile_id=p.profile_id) <> jsonb_array_length(p.manifest->'services')+1
           OR EXISTS (SELECT 1 FROM public.mcp_credential_refs r WHERE r.tenant_id=p.tenant_id AND r.profile_id=p.profile_id
               AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements((p.manifest->'services') || jsonb_build_array((p.manifest->'gateway') || '{"service":"__gateway"}'::jsonb)) e
                   WHERE e->>'service'=r.service_key AND e->>'alias'=r.alias AND e->>'license_id'=r.license_id::text AND e->>'app_key'=r.app_key)) THEN
            RAISE EXCEPTION 'incomplete credential references' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER binding_complete AFTER INSERT OR UPDATE ON public.mcp_bindings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.mcp_access_complete('binding');
CREATE CONSTRAINT TRIGGER binding_tools_complete AFTER INSERT OR UPDATE OR DELETE ON public.mcp_binding_tools
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.mcp_access_complete('binding');
CREATE CONSTRAINT TRIGGER binding_toolkits_complete AFTER INSERT OR UPDATE OR DELETE ON public.mcp_binding_toolkits
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.mcp_access_complete('binding');
CREATE CONSTRAINT TRIGGER profile_complete AFTER INSERT OR UPDATE ON public.mcp_credential_profiles
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.mcp_access_complete('profile');
CREATE CONSTRAINT TRIGGER profile_refs_complete AFTER INSERT OR UPDATE OR DELETE ON public.mcp_credential_refs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.mcp_access_complete('profile');

-- Lock exact published dependencies without granting platform UPDATE to API roles.
CREATE FUNCTION public.mcp_lock_access_tool(request_tenant uuid, owner_tenant uuid, selected_tool uuid, selected_version integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE d record;
BEGIN
    IF request_tenant <> owner_tenant THEN
        PERFORM 1 FROM public.mcp_tool_shares WHERE owner_tenant_id=owner_tenant
            AND tool_id=selected_tool AND grantee_tenant_id=request_tenant FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'tool not shared' USING ERRCODE='23514'; END IF;
    END IF;
    SELECT v.status,t.retired INTO d FROM public.mcp_tool_versions v JOIN public.mcp_tools t USING(tenant_id,tool_id)
        WHERE v.tenant_id=owner_tenant AND v.tool_id=selected_tool AND v.version=selected_version FOR SHARE OF v,t;
    IF NOT FOUND OR d.status <> 'published' OR d.retired THEN
        RAISE EXCEPTION 'tool unavailable' USING ERRCODE='23514';
    END IF;
    FOR d IN SELECT o.status AS op,s.status AS service FROM public.mcp_tool_version_operations r
        JOIN public.mcp_operation_versions o ON (o.operation_id,o.version)=(r.operation_id,r.operation_version)
        JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version)
        WHERE r.tenant_id=owner_tenant AND r.tool_id=selected_tool AND r.version=selected_version
        ORDER BY o.operation_id,o.version FOR SHARE OF o,s
    LOOP
        IF d.op <> 'published' OR d.service <> 'published' THEN
            RAISE EXCEPTION 'operation unavailable' USING ERRCODE='23514';
        END IF;
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION public.mcp_lock_access_tool(uuid,uuid,uuid,integer) FROM PUBLIC;

-- All identities in the replacement use UUIDv7. No conversion of old data.
DO $$ DECLARE c record; BEGIN
    FOR c IN SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name LIKE 'mcp_%' AND udt_name='uuid'
    LOOP
        EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I CHECK (get_byte(uuid_send(%I),6) >> 4 = 7 AND get_byte(uuid_send(%I),8) >> 6 = 2)',
            c.table_name, c.table_name || '_' || c.column_name || '_v7', c.column_name, c.column_name);
    END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("Access history downgrade is unsupported; fix forward")
