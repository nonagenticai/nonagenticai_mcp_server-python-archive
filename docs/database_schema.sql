-- Reference only: generated from 0001_executable_catalog through 0003_catalog_publication.
-- Alembic alone owns DDL; do not use this file to bootstrap a database.
CREATE TABLE public.mcp_catalog_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0)
);
INSERT INTO public.mcp_catalog_state DEFAULT VALUES;

CREATE TABLE public.mcp_services (
    service_id uuid PRIMARY KEY,
    service_key text NOT NULL UNIQUE,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.mcp_service_versions (
    service_id uuid NOT NULL REFERENCES public.mcp_services(service_id),
    version integer NOT NULL CHECK (version > 0),
    manifest jsonb NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','disabled','retired')),
    created_by jsonb NOT NULL,
    updated_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (service_id, version),
    CHECK (manifest ? 'service_id' AND manifest->>'service_id' = service_id::text AND (manifest->>'version')::integer = version),
    CHECK (jsonb_typeof(manifest) = 'object' AND manifest ?& ARRAY['schema_version','version']
        AND manifest->>'schema_version' = '1')
);
CREATE TABLE public.mcp_operation_versions (
    operation_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    service_id uuid NOT NULL,
    service_version integer NOT NULL,
    manifest jsonb NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','disabled','retired')),
    created_by jsonb NOT NULL,
    updated_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (operation_id, version),
    FOREIGN KEY (service_id, service_version) REFERENCES public.mcp_service_versions(service_id, version),
    CHECK (manifest ?& ARRAY['operation_id','service'] AND manifest->>'operation_id' = operation_id::text AND (manifest->>'version')::integer = version),
    CHECK (manifest->'service'->>'id' = service_id::text
        AND (manifest->'service'->>'version')::integer = service_version),
    CHECK (jsonb_typeof(manifest) = 'object' AND manifest ?& ARRAY['schema_version','version']
        AND manifest->>'schema_version' = '1')
);
CREATE TABLE public.mcp_tools (
    tenant_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    public_name text NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    retired boolean NOT NULL DEFAULT false,
    created_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, tool_id),
    UNIQUE (tenant_id, public_name)
);
-- Sharing is granted by a distinct platform operation, never tenant content.
CREATE TABLE public.mcp_tool_shares (
    owner_tenant_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    grantee_tenant_id uuid NOT NULL,
    granted_by jsonb NOT NULL,
    PRIMARY KEY (owner_tenant_id, tool_id, grantee_tenant_id),
    FOREIGN KEY (owner_tenant_id, tool_id) REFERENCES public.mcp_tools(tenant_id, tool_id),
    CHECK (owner_tenant_id <> grantee_tenant_id)
);
CREATE TABLE public.mcp_tool_versions (
    tenant_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    manifest jsonb NOT NULL,
    package_sha256 text NOT NULL CHECK (package_sha256 ~ '^[a-f0-9]{64}$'),
    file_count integer NOT NULL CHECK (file_count > 0),
    operation_count integer NOT NULL CHECK (operation_count >= 0),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','disabled','retired')),
    created_by jsonb NOT NULL,
    updated_by jsonb NOT NULL,
    source_tool_id uuid,
    source_version integer,
    creation_tx text NOT NULL DEFAULT pg_current_xact_id()::text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, tool_id, version),
    FOREIGN KEY (tenant_id, tool_id) REFERENCES public.mcp_tools(tenant_id, tool_id),
    FOREIGN KEY (tenant_id, source_tool_id, source_version)
        REFERENCES public.mcp_tool_versions(tenant_id, tool_id, version),
    CHECK ((source_tool_id IS NULL) = (source_version IS NULL)),
    CHECK (manifest ?& ARRAY['tenant_id','tool_id','package_sha256','allowed_operations'] AND manifest->>'tenant_id' = tenant_id::text AND manifest->>'tool_id' = tool_id::text
        AND (manifest->>'version')::integer = version AND manifest->>'package_sha256' = package_sha256),
    CHECK (jsonb_typeof(manifest) = 'object' AND manifest ?& ARRAY['schema_version','version']
        AND manifest->>'schema_version' = '1')
);
CREATE TABLE public.mcp_tool_version_files (
    tenant_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    version integer NOT NULL,
    path text NOT NULL CHECK (path <> '' AND path !~ '(^/|\\|:|(^|/)\.{1,2}(/|$)|//|/$)'),
    content text NOT NULL,
    PRIMARY KEY (tenant_id, tool_id, version, path),
    FOREIGN KEY (tenant_id, tool_id, version) REFERENCES public.mcp_tool_versions(tenant_id, tool_id, version)
);
CREATE TABLE public.mcp_tool_version_operations (
    tenant_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    version integer NOT NULL,
    operation_id uuid NOT NULL,
    operation_version integer NOT NULL,
    PRIMARY KEY (tenant_id, tool_id, version, operation_id, operation_version),
    FOREIGN KEY (tenant_id, tool_id, version) REFERENCES public.mcp_tool_versions(tenant_id, tool_id, version),
    FOREIGN KEY (operation_id, operation_version) REFERENCES public.mcp_operation_versions(operation_id, version)
);
CREATE INDEX mcp_tools_tenant_name_idx ON public.mcp_tools(tenant_id, public_name);
CREATE INDEX mcp_tool_versions_status_idx ON public.mcp_tool_versions(tenant_id, status);
CREATE INDEX mcp_operations_service_idx ON public.mcp_operation_versions(service_id, service_version);
CREATE INDEX mcp_tool_operations_reverse_idx ON public.mcp_tool_version_operations(operation_id, operation_version);

CREATE FUNCTION public.mcp_guard_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'catalog history cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF (to_jsonb(OLD) - ARRAY['status','updated_by','updated_at']) IS DISTINCT FROM
       (to_jsonb(NEW) - ARRAY['status','updated_by','updated_at']) THEN
        RAISE EXCEPTION 'version content is immutable; append a snapshot' USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'draft' AND NEW.status IN ('published','disabled','retired')) OR
        (OLD.status = 'published' AND NEW.status IN ('disabled','retired')) OR
        (OLD.status = 'disabled' AND NEW.status = 'retired')
    ) THEN
        RAISE EXCEPTION 'invalid lifecycle transition' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER service_snapshot_guard BEFORE UPDATE OR DELETE ON public.mcp_service_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_snapshot();
CREATE TRIGGER operation_snapshot_guard BEFORE UPDATE OR DELETE ON public.mcp_operation_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_snapshot();
CREATE TRIGGER tool_snapshot_guard BEFORE UPDATE OR DELETE ON public.mcp_tool_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_snapshot();

CREATE FUNCTION public.mcp_guard_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'catalog identity cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF (to_jsonb(OLD) - ARRAY['revision','retired']) IS DISTINCT FROM
       (to_jsonb(NEW) - ARRAY['revision','retired']) OR NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'identity immutable; revision must advance once' USING ERRCODE = '23514';
    END IF;
    IF to_jsonb(OLD)->>'retired' = 'true' AND to_jsonb(NEW)->>'retired' <> 'true' THEN
        RAISE EXCEPTION 'retired identity cannot be enabled' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tool_identity_guard BEFORE UPDATE OR DELETE ON public.mcp_tools
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_identity();
CREATE TRIGGER service_identity_guard BEFORE UPDATE OR DELETE ON public.mcp_services
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_identity();

CREATE FUNCTION public.mcp_guard_children() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v public.mcp_tool_versions;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'version children cannot be changed or deleted' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO v FROM public.mcp_tool_versions
        WHERE tenant_id = NEW.tenant_id AND tool_id = NEW.tool_id AND version = NEW.version FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tenant-owned version not found' USING ERRCODE = '23503';
    END IF;
    IF v.status <> 'draft' OR v.creation_tx <> pg_current_xact_id()::text THEN
        RAISE EXCEPTION 'children must be created atomically with draft snapshot' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER file_content_guard BEFORE INSERT OR UPDATE OR DELETE ON public.mcp_tool_version_files
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_children();
CREATE TRIGGER operation_reference_guard BEFORE INSERT OR UPDATE OR DELETE ON public.mcp_tool_version_operations
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_children();

CREATE FUNCTION public.mcp_check_package_complete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE files bigint; operations bigint;
BEGIN
    SELECT count(*) INTO files FROM public.mcp_tool_version_files
        WHERE tenant_id = NEW.tenant_id AND tool_id = NEW.tool_id AND version = NEW.version;
    SELECT count(*) INTO operations FROM public.mcp_tool_version_operations
        WHERE tenant_id = NEW.tenant_id AND tool_id = NEW.tool_id AND version = NEW.version;
    IF files <> NEW.file_count OR operations <> NEW.operation_count OR
        operations <> jsonb_array_length(NEW.manifest->'allowed_operations') THEN
        RAISE EXCEPTION 'incomplete version snapshot' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.mcp_tool_version_operations r
        WHERE r.tenant_id = NEW.tenant_id AND r.tool_id = NEW.tool_id AND r.version = NEW.version
        AND NOT NEW.manifest->'allowed_operations' @> jsonb_build_array(
            jsonb_build_object('id',r.operation_id::text,'version',r.operation_version))
    ) THEN
        RAISE EXCEPTION 'manifest/reference mismatch' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER complete_tool_snapshot AFTER INSERT ON public.mcp_tool_versions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.mcp_check_package_complete();

-- Lock publication dependencies with owner privileges, without granting platform
-- UPDATE to the runtime role. Triggers also enforce this for direct SQL writes.
CREATE FUNCTION public.mcp_check_publication() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE dependency record; retired_parent boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'new snapshots must be drafts' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status <> 'published' OR OLD.status = 'published' THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME = 'mcp_operation_versions' THEN
        SELECT status INTO dependency FROM public.mcp_service_versions
            WHERE service_id = NEW.service_id AND version = NEW.service_version FOR SHARE;
        IF dependency.status <> 'published' THEN
            RAISE EXCEPTION 'service dependency is not published' USING ERRCODE = '23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'mcp_tool_versions' THEN
        SELECT retired INTO retired_parent FROM public.mcp_tools
            WHERE tenant_id = NEW.tenant_id AND tool_id = NEW.tool_id FOR SHARE;
        IF retired_parent THEN
            RAISE EXCEPTION 'tool identity is retired' USING ERRCODE = '23514';
        END IF;
        FOR dependency IN
            SELECT o.status AS operation_status,s.status AS service_status
            FROM public.mcp_tool_version_operations r
            JOIN public.mcp_operation_versions o ON (o.operation_id,o.version)=(r.operation_id,r.operation_version)
            JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version)
            WHERE r.tenant_id=NEW.tenant_id AND r.tool_id=NEW.tool_id AND r.version=NEW.version
            ORDER BY o.operation_id,o.version FOR SHARE OF o,s
        LOOP
            IF dependency.operation_status <> 'published' OR dependency.service_status <> 'published' THEN
                RAISE EXCEPTION 'catalog dependency is not published' USING ERRCODE = '23514';
            END IF;
        END LOOP;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER service_publication BEFORE INSERT OR UPDATE ON public.mcp_service_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_check_publication();
CREATE TRIGGER operation_publication BEFORE INSERT OR UPDATE ON public.mcp_operation_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_check_publication();
CREATE TRIGGER tool_publication BEFORE INSERT OR UPDATE ON public.mcp_tool_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_check_publication();
REVOKE ALL ON FUNCTION public.mcp_check_publication() FROM PUBLIC;

CREATE FUNCTION public.mcp_bump_generation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_TABLE_NAME = 'mcp_tool_shares' THEN
        UPDATE public.mcp_catalog_state SET generation = generation + 1 WHERE singleton;
        RETURN NULL;
    END IF;
    IF (to_jsonb(NEW)->>'status') IS DISTINCT FROM (to_jsonb(OLD)->>'status') OR
       (to_jsonb(NEW)->>'retired') IS DISTINCT FROM (to_jsonb(OLD)->>'retired') THEN
        UPDATE public.mcp_catalog_state SET generation = generation + 1 WHERE singleton;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tool_visibility AFTER UPDATE ON public.mcp_tool_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_bump_generation();
CREATE TRIGGER service_visibility AFTER UPDATE ON public.mcp_service_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_bump_generation();
CREATE TRIGGER operation_visibility AFTER UPDATE ON public.mcp_operation_versions
    FOR EACH ROW EXECUTE FUNCTION public.mcp_bump_generation();
CREATE TRIGGER tool_retirement AFTER UPDATE ON public.mcp_tools
    FOR EACH ROW EXECUTE FUNCTION public.mcp_bump_generation();
CREATE TRIGGER share_visibility AFTER INSERT OR UPDATE OR DELETE ON public.mcp_tool_shares
    FOR EACH ROW EXECUTE FUNCTION public.mcp_bump_generation();
REVOKE ALL ON FUNCTION public.mcp_bump_generation() FROM PUBLIC;

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

-- 0003_catalog_publication (forward-only addition; phase-2 rows stay null).
ALTER TABLE public.mcp_access_events
  ADD COLUMN artifact_version integer CHECK (artifact_version > 0),
  ADD CONSTRAINT tool_event_version_required CHECK (
    action NOT IN ('tool.created','tool.version_created','tool.published',
                   'tool.disabled','tool.retired') OR artifact_version IS NOT NULL
  );

-- 0004_execution_runtime

CREATE TABLE public.mcp_runs (
    run_id uuid PRIMARY KEY CHECK (uuid_extract_version(run_id)=7),
    tenant_id uuid NOT NULL CHECK (uuid_extract_version(tenant_id)=7),
    binding_id uuid NOT NULL,
    binding_revision bigint NOT NULL CHECK (binding_revision>0),
    gateway_license_id uuid NOT NULL CHECK (uuid_extract_version(gateway_license_id)=7),
    credential_profile_id uuid NOT NULL,
    profile_snapshot jsonb NOT NULL,
    owner_tenant_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    tool_version integer NOT NULL,
    authority jsonb NOT NULL,
    inputs jsonb NOT NULL,
    input_hash text NOT NULL CHECK (input_hash ~ '^[a-f0-9]{64}$'),
    idempotency_key text CHECK (idempotency_key ~ '^[A-Za-z0-9_.:-]{1,128}$'),
    created_by jsonb NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued','starting','running','cancelling','succeeded','failed','cancelled','timed_out','interrupted')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    queue_deadline timestamptz NOT NULL,
    start_deadline timestamptz,
    deadline timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    worker_id uuid CHECK (uuid_extract_version(worker_id)=7),
    fence bigint NOT NULL DEFAULT 0 CHECK (fence>=0),
    lease_until timestamptz,
    heartbeat_at timestamptz,
    pod_name text,
    pod_uid text,
    cancel_requested boolean NOT NULL DEFAULT false,
    api_calls integer NOT NULL DEFAULT 0 CHECK (api_calls>=0),
    result jsonb,
    error jsonb,
    UNIQUE (tenant_id,run_id),
    UNIQUE (tenant_id,binding_id,owner_tenant_id,tool_id,tool_version,idempotency_key),
    FOREIGN KEY (tenant_id,binding_id) REFERENCES public.mcp_bindings(tenant_id,binding_id),
    FOREIGN KEY (tenant_id,credential_profile_id) REFERENCES public.mcp_credential_profiles(tenant_id,profile_id),
    FOREIGN KEY (owner_tenant_id,tool_id,tool_version) REFERENCES public.mcp_tool_versions(tenant_id,tool_id,version)
);
CREATE INDEX mcp_runs_queue ON public.mcp_runs(created_at) WHERE status='queued';
CREATE INDEX mcp_runs_leases ON public.mcp_runs(lease_until) WHERE worker_id IS NOT NULL;
CREATE INDEX mcp_runs_tenant ON public.mcp_runs(tenant_id,binding_id,created_at);
CREATE TABLE public.mcp_execution_slots (
    slot_id integer PRIMARY KEY CHECK (slot_id>0),
    run_id uuid UNIQUE REFERENCES public.mcp_runs(run_id),
    quarantined boolean NOT NULL DEFAULT false
);
-- Capacity rows are provisioned by an explicit operator command, never DDL at startup.
CREATE TABLE public.mcp_tenant_execution_locks (
    tenant_id uuid PRIMARY KEY CHECK (uuid_extract_version(tenant_id)=7),
    last_claimed_at timestamptz NOT NULL DEFAULT '-infinity'
);
CREATE TABLE public.mcp_run_api_calls (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence>0),
    operation_id uuid NOT NULL,
    operation_version integer NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    idempotency_key text,
    license_id uuid NOT NULL CHECK (uuid_extract_version(license_id)=7),
    effect text NOT NULL CHECK (effect IN ('read_only','writes')),
    status text NOT NULL DEFAULT 'intent' CHECK (status IN ('intent','succeeded','failed','outcome_unknown')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    PRIMARY KEY (tenant_id,run_id,sequence),
    FOREIGN KEY (tenant_id,run_id) REFERENCES public.mcp_runs(tenant_id,run_id),
    FOREIGN KEY (operation_id,operation_version) REFERENCES public.mcp_operation_versions(operation_id,version)
);
CREATE TABLE public.mcp_run_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind ~ '^[a-z_]{1,64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id,run_id) REFERENCES public.mcp_runs(tenant_id,run_id)
);
CREATE INDEX mcp_run_events_page ON public.mcp_run_events(tenant_id,run_id,event_id);
CREATE INDEX mcp_api_calls_recent ON public.mcp_run_api_calls(started_at DESC);
CREATE TABLE public.mcp_api_permits (
    permit_id uuid PRIMARY KEY CHECK (uuid_extract_version(permit_id)=7),
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    fence bigint NOT NULL,
    service_id uuid NOT NULL REFERENCES public.mcp_services(service_id),
    expires_at timestamptz NOT NULL,
    FOREIGN KEY (tenant_id,run_id) REFERENCES public.mcp_runs(tenant_id,run_id)
);
CREATE INDEX mcp_api_permits_expiry ON public.mcp_api_permits(expires_at);
CREATE INDEX mcp_api_permits_service ON public.mcp_api_permits(service_id,expires_at);
CREATE TABLE public.mcp_api_rate_windows (
    tenant_id uuid NOT NULL CHECK (uuid_extract_version(tenant_id)=7),
    service_id uuid NOT NULL REFERENCES public.mcp_services(service_id),
    window_start timestamptz NOT NULL,
    calls integer NOT NULL CHECK (calls>0),
    PRIMARY KEY (tenant_id,service_id,window_start)
);
CREATE INDEX mcp_api_rate_windows_expiry ON public.mcp_api_rate_windows(window_start);

CREATE FUNCTION public.mcp_guard_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'run history is immutable'; END IF;
    IF (to_jsonb(NEW)-ARRAY['status','start_deadline','deadline','started_at','finished_at',
        'worker_id','fence','lease_until','heartbeat_at','pod_name','pod_uid','cancel_requested',
        'api_calls','result','error']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','start_deadline','deadline','started_at','finished_at',
        'worker_id','fence','lease_until','heartbeat_at','pod_name','pod_uid','cancel_requested',
        'api_calls','result','error']) THEN RAISE EXCEPTION 'run snapshot is immutable'; END IF;
    IF OLD.cancel_requested AND NOT NEW.cancel_requested THEN RAISE EXCEPTION 'cancellation is final'; END IF;
    IF NEW.fence<OLD.fence THEN RAISE EXCEPTION 'fence cannot decrease'; END IF;
    IF NEW.status<>OLD.status AND NOT (
        (OLD.status='queued' AND NEW.status IN ('starting','cancelled','timed_out','failed')) OR
        (OLD.status='starting' AND NEW.status IN ('running','cancelling','failed','timed_out','interrupted')) OR
        (OLD.status='starting' AND NEW.status='queued' AND OLD.pod_name IS NULL AND OLD.started_at IS NULL AND OLD.api_calls=0) OR
        (OLD.status='running' AND NEW.status IN ('succeeded','failed','timed_out','interrupted','cancelling')) OR
        (OLD.status='cancelling' AND NEW.status IN ('cancelled','interrupted'))
    ) THEN RAISE EXCEPTION 'invalid execution transition'; END IF;
    IF OLD.status IN ('succeeded','failed','cancelled','timed_out','interrupted') AND
       (NEW.result IS DISTINCT FROM OLD.result OR NEW.error IS DISTINCT FROM OLD.error) THEN
       RAISE EXCEPTION 'terminal result is immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER mcp_runs_guard BEFORE UPDATE OR DELETE ON public.mcp_runs
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_run();
REVOKE ALL ON FUNCTION public.mcp_guard_run() FROM PUBLIC;
CREATE FUNCTION public.mcp_lock_execution_authority() RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE value bigint;
BEGIN
    SELECT generation INTO value FROM public.mcp_catalog_state WHERE singleton FOR SHARE;
    RETURN value;
END $$;
REVOKE ALL ON FUNCTION public.mcp_lock_execution_authority() FROM PUBLIC;
CREATE FUNCTION public.mcp_guard_execution_journal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR TG_TABLE_NAME='mcp_run_events' THEN RAISE EXCEPTION 'execution history is immutable'; END IF;
    IF (to_jsonb(NEW)-ARRAY['status','finished_at']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','finished_at']) OR OLD.status<>'intent' OR
       NEW.status NOT IN ('succeeded','failed','outcome_unknown') OR NEW.finished_at IS NULL THEN
       RAISE EXCEPTION 'journal intent is immutable; only its outcome may be finalized';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER mcp_api_call_guard BEFORE UPDATE OR DELETE ON public.mcp_run_api_calls
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_execution_journal();
CREATE TRIGGER mcp_run_event_guard BEFORE UPDATE OR DELETE ON public.mcp_run_events
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_execution_journal();
REVOKE ALL ON FUNCTION public.mcp_guard_execution_journal() FROM PUBLIC;

-- 0005_execution_controls
ALTER TABLE public.mcp_runs ADD COLUMN payload_expired_at timestamptz;
ALTER TABLE public.mcp_execution_slots ADD COLUMN draining boolean NOT NULL DEFAULT false;
CREATE INDEX mcp_runs_retention ON public.mcp_runs(finished_at)
    WHERE payload_expired_at IS NULL AND finished_at IS NOT NULL;
CREATE OR REPLACE FUNCTION public.mcp_guard_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'run history is immutable'; END IF;
    IF TG_OP='UPDATE' AND NEW.payload_expired_at IS NOT NULL AND OLD.payload_expired_at IS NULL THEN
        IF current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.mcp_runs'::regclass)
           OR OLD.status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')
           OR OLD.finished_at IS NULL OR OLD.worker_id IS NOT NULL
           OR EXISTS(SELECT 1 FROM public.mcp_execution_slots WHERE run_id=OLD.run_id)
           OR EXISTS(SELECT 1 FROM public.mcp_api_permits WHERE run_id=OLD.run_id)
           OR EXISTS(SELECT 1 FROM public.mcp_run_api_calls WHERE run_id=OLD.run_id AND status='intent')
           OR NEW.inputs<>'{}'::jsonb OR NEW.authority<>'{}'::jsonb
           OR NEW.profile_snapshot<>'{}'::jsonb OR NEW.result IS NOT NULL
           OR (to_jsonb(NEW)-ARRAY['inputs','authority','profile_snapshot','result','payload_expired_at'])
              IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['inputs','authority','profile_snapshot','result','payload_expired_at'])
        THEN RAISE EXCEPTION 'invalid payload expiration'; END IF;
        RETURN NEW;
    END IF;
    IF NEW.payload_expired_at IS DISTINCT FROM OLD.payload_expired_at THEN
        RAISE EXCEPTION 'payload expiration is final';
    END IF;
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'run history is immutable'; END IF;
    IF (to_jsonb(NEW)-ARRAY['status','start_deadline','deadline','started_at','finished_at',
        'worker_id','fence','lease_until','heartbeat_at','pod_name','pod_uid','cancel_requested',
        'api_calls','result','error']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','start_deadline','deadline','started_at','finished_at',
        'worker_id','fence','lease_until','heartbeat_at','pod_name','pod_uid','cancel_requested',
        'api_calls','result','error']) THEN RAISE EXCEPTION 'run snapshot is immutable'; END IF;
    IF OLD.cancel_requested AND NOT NEW.cancel_requested THEN RAISE EXCEPTION 'cancellation is final'; END IF;
    IF NEW.fence<OLD.fence THEN RAISE EXCEPTION 'fence cannot decrease'; END IF;
    IF NEW.status<>OLD.status AND NOT (
        (OLD.status='queued' AND NEW.status IN ('starting','cancelled','timed_out','failed')) OR
        (OLD.status='starting' AND NEW.status IN ('running','cancelling','failed','timed_out','interrupted')) OR
        (OLD.status='starting' AND NEW.status='queued' AND OLD.pod_name IS NULL AND OLD.started_at IS NULL AND OLD.api_calls=0) OR
        (OLD.status='running' AND NEW.status IN ('succeeded','failed','timed_out','interrupted','cancelling')) OR
        (OLD.status='cancelling' AND NEW.status IN ('cancelled','interrupted'))
    ) THEN RAISE EXCEPTION 'invalid execution transition'; END IF;
    IF OLD.status IN ('succeeded','failed','cancelled','timed_out','interrupted') AND
       (NEW.result IS DISTINCT FROM OLD.result OR NEW.error IS DISTINCT FROM OLD.error) THEN
       RAISE EXCEPTION 'terminal result is immutable'; END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION public.mcp_expire_run_payloads(retention_days integer, batch_size integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE expired integer;
BEGIN
    IF retention_days NOT BETWEEN 1 AND 365 OR batch_size NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'retention outside bounds';
    END IF;
    WITH candidates AS (
        SELECT r.run_id FROM public.mcp_runs r
        WHERE r.payload_expired_at IS NULL
          AND r.finished_at<clock_timestamp()-retention_days*interval '1 day'
          AND r.status IN ('succeeded','failed','cancelled','timed_out','interrupted')
          AND r.worker_id IS NULL
          AND NOT EXISTS(SELECT 1 FROM public.mcp_execution_slots s WHERE s.run_id=r.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.mcp_api_permits p WHERE p.run_id=r.run_id)
          AND NOT EXISTS(SELECT 1 FROM public.mcp_run_api_calls a WHERE a.run_id=r.run_id AND a.status='intent')
        ORDER BY r.finished_at,r.run_id LIMIT batch_size FOR UPDATE OF r SKIP LOCKED
    )
    UPDATE public.mcp_runs r SET inputs='{}',authority='{}',profile_snapshot='{}',result=NULL,
        payload_expired_at=clock_timestamp() FROM candidates c WHERE r.run_id=c.run_id;
    GET DIAGNOSTICS expired=ROW_COUNT;
    RETURN expired;
END $$;
REVOKE ALL ON FUNCTION public.mcp_expire_run_payloads(integer,integer) FROM PUBLIC;
