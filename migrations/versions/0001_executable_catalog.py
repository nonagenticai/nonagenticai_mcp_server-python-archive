"""Fresh executable catalog. Old development revisions are intentionally unsupported.

This revision is self-contained. Never import application models here.
"""

from alembic import op

revision = "0001_executable_catalog"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
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
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("Destructive baseline downgrade is unsupported; fix forward")
