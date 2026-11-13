"""Tenant-owned sources and services (the tool builder). Fix forward only.

Ownership is by query scoping plus SECURITY DEFINER ``mcp_builder_*`` write functions that the
runtime role may EXECUTE; the runtime role gets no INSERT/UPDATE on any of these tables. Every
function rejects the platform tenant, takes the per-tenant advisory lock before it counts a quota
and raises one of these SQLSTATEs (psycopg exposes them as ``exc.sqlstate``):

    MB001 conflict (stale revision, version not next, duplicate 1:1 service)
    MB002 not found, or not owned by that tenant
    MB003 quota exceeded
    MB004 the platform tenant may not use the builder

Everything else (CHECK violations) is ``23514``; unique violations stay ``23505``.

What changes:
1. ``mcp_sources``/``mcp_services`` gain ``owner_tenant_id`` (default: the platform tenant, so every
   existing row stays valid). Source keys are unique per owner. Auth kinds widen and tenant rows
   must be https without a private CA. The guard freezes the owner.
2. ``mcp_service_grants`` shares static-token platform services with tenants; licence-kind
   platform services are shared by default (``mcp_tenant_may_use_service``). A trigger on
   ``mcp_tool_version_operations`` and ``mcp_check_publication`` enforce it for tool versions.
3. ``mcp_operation_keys`` (stable key per operation), ``mcp_source_specs`` (last 3 specs).
4. ``mcp_runs.mode`` bound|draft with the binding columns nullable for drafts; the journal
   gains a request/response trace. (The UUIDv7 CHECKs on tenant and licence identities were
   already dropped by 0007; nothing to relax here.)
5. Audit ``principal_kind`` gains ``tenant_author``.

Self-contained; no application imports.
"""

from alembic import op

revision = "0010_tenant_builder"
down_revision = "0009_tool_sources"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
-- Fail fast instead of queueing behind (and blocking) live traffic; audit writes are dropped
-- after 0.5 s, so a long ACCESS EXCLUSIVE wait must never happen.
SET LOCAL lock_timeout = '5s';

-- 1. ownership ------------------------------------------------------------------------------
ALTER TABLE public.mcp_sources
    ADD COLUMN owner_tenant_id uuid NOT NULL DEFAULT public.mcp_platform_tenant(),
    ADD COLUMN spec_path text;
ALTER TABLE public.mcp_sources DROP CONSTRAINT mcp_sources_source_key_key;
ALTER TABLE public.mcp_sources DROP CONSTRAINT mcp_sources_auth_check;
ALTER TABLE public.mcp_sources DROP CONSTRAINT mcp_sources_check;
ALTER TABLE public.mcp_sources
    ADD CONSTRAINT mcp_sources_owner_key UNIQUE (owner_tenant_id, source_key),
    ADD CONSTRAINT mcp_sources_owner_id_key UNIQUE (source_id, owner_tenant_id),
    ADD CONSTRAINT mcp_sources_auth_check CHECK (
        jsonb_typeof(auth)='object' AND auth->>'kind' IN
        ('licence','none','static_bearer','static_header','query_param','basic',
         'oauth2_client_credentials')),
    ADD CONSTRAINT mcp_sources_token_presence CHECK (
        (auth->>'kind' IN ('licence','none')) = (token_ciphertext IS NULL)),
    ADD CONSTRAINT mcp_sources_tenant_https CHECK (
        owner_tenant_id=public.mcp_platform_tenant()
        OR (base_url LIKE 'https://%' AND NOT allow_internal_http AND ca_file IS NULL)),
    ADD CONSTRAINT mcp_sources_spec_path_check CHECK (
        spec_path IS NULL OR (spec_path ~ '^/[A-Za-z0-9._~/-]{0,255}$'
            AND spec_path !~ '(^|/)\.{1,2}(/|$)' AND spec_path !~ '//'));
CREATE INDEX mcp_sources_owner_idx ON public.mcp_sources(owner_tenant_id, status);

CREATE OR REPLACE FUNCTION public.mcp_guard_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'sources cannot be deleted; disable them' USING ERRCODE = '23514';
    END IF;
    IF NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.source_key IS DISTINCT FROM OLD.source_key
       OR NEW.owner_tenant_id IS DISTINCT FROM OLD.owner_tenant_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'source identity immutable; revision must advance once' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;

ALTER TABLE public.mcp_source_events
    ADD COLUMN owner_tenant_id uuid NOT NULL DEFAULT public.mcp_platform_tenant(),
    ADD CONSTRAINT mcp_source_events_owner_fkey FOREIGN KEY (source_id, owner_tenant_id)
        REFERENCES public.mcp_sources(source_id, owner_tenant_id);

ALTER TABLE public.mcp_services
    ADD COLUMN owner_tenant_id uuid NOT NULL DEFAULT public.mcp_platform_tenant();
CREATE INDEX mcp_services_owner_idx ON public.mcp_services(owner_tenant_id);

CREATE TABLE public.mcp_service_grants (
    service_id uuid NOT NULL REFERENCES public.mcp_services(service_id),
    grantee_tenant_id uuid NOT NULL,
    granted_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (service_id, grantee_tenant_id),
    CHECK (grantee_tenant_id <> public.mcp_platform_tenant())
);
CREATE INDEX mcp_service_grants_grantee_idx ON public.mcp_service_grants(grantee_tenant_id);
CREATE FUNCTION public.mcp_guard_service_grant() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'grants are replaced, not updated' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'INSERT' AND NOT EXISTS (SELECT 1 FROM public.mcp_services
        WHERE service_id = NEW.service_id AND owner_tenant_id = public.mcp_platform_tenant()) THEN
        RAISE EXCEPTION 'only platform services can be granted' USING ERRCODE = '23514';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
REVOKE ALL ON FUNCTION public.mcp_guard_service_grant() FROM PUBLIC;
CREATE TRIGGER service_grant_guard BEFORE INSERT OR UPDATE ON public.mcp_service_grants
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_service_grant();

-- A grant changes what an in-flight run may still reach: bump the authority generation.
CREATE OR REPLACE FUNCTION public.mcp_bump_generation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_TABLE_NAME IN ('mcp_tool_shares','mcp_service_grants') THEN
        UPDATE public.mcp_catalog_state SET generation = generation + 1 WHERE singleton;
        RETURN NULL;
    END IF;
    IF (to_jsonb(NEW)->>'status') IS DISTINCT FROM (to_jsonb(OLD)->>'status') OR
       (to_jsonb(NEW)->>'retired') IS DISTINCT FROM (to_jsonb(OLD)->>'retired') THEN
        UPDATE public.mcp_catalog_state SET generation = generation + 1 WHERE singleton;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER grant_visibility AFTER INSERT OR UPDATE OR DELETE ON public.mcp_service_grants
    FOR EACH ROW EXECUTE FUNCTION public.mcp_bump_generation();

-- May ``p_tenant`` reference this exact service version from a tool?  Own services: yes.
-- Platform services: with a grant, or when the service's source is licence kind (or not
-- registered at all, so there is no stored credential to expose). Anything else: no.
CREATE FUNCTION public.mcp_tenant_may_use_service(p_tenant uuid, p_service uuid, p_version integer)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.mcp_services s
        WHERE s.service_id = p_service AND (
            s.owner_tenant_id = p_tenant
            OR (s.owner_tenant_id = public.mcp_platform_tenant() AND (
                EXISTS (SELECT 1 FROM public.mcp_service_grants g
                        WHERE g.service_id = s.service_id AND g.grantee_tenant_id = p_tenant)
                OR NOT EXISTS (
                    SELECT 1 FROM public.mcp_service_versions v
                    JOIN public.mcp_sources src ON src.owner_tenant_id = s.owner_tenant_id
                        AND src.source_key = v.manifest->>'destination_alias'
                    WHERE v.service_id = s.service_id AND v.version = p_version
                      AND src.auth->>'kind' <> 'licence'))))
    )
$$;
REVOKE ALL ON FUNCTION public.mcp_tenant_may_use_service(uuid,uuid,integer) FROM PUBLIC;

CREATE FUNCTION public.mcp_check_operation_reference() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o public.mcp_operation_versions;
BEGIN
    SELECT * INTO o FROM public.mcp_operation_versions
        WHERE operation_id = NEW.operation_id AND version = NEW.operation_version;
    -- A missing operation is the foreign key's error to raise.
    IF FOUND AND NOT public.mcp_tenant_may_use_service(NEW.tenant_id, o.service_id, o.service_version) THEN
        RAISE EXCEPTION 'operation service is not available to this tenant' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.mcp_check_operation_reference() FROM PUBLIC;
CREATE TRIGGER operation_tenancy_guard BEFORE INSERT ON public.mcp_tool_version_operations
    FOR EACH ROW EXECUTE FUNCTION public.mcp_check_operation_reference();

CREATE OR REPLACE FUNCTION public.mcp_check_publication() RETURNS trigger
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
            SELECT o.status AS operation_status,s.status AS service_status,
                   public.mcp_tenant_may_use_service(NEW.tenant_id,o.service_id,o.service_version) AS allowed
            FROM public.mcp_tool_version_operations r
            JOIN public.mcp_operation_versions o ON (o.operation_id,o.version)=(r.operation_id,r.operation_version)
            JOIN public.mcp_service_versions s ON (s.service_id,s.version)=(o.service_id,o.service_version)
            WHERE r.tenant_id=NEW.tenant_id AND r.tool_id=NEW.tool_id AND r.version=NEW.version
            ORDER BY o.operation_id,o.version FOR SHARE OF o,s
        LOOP
            IF dependency.operation_status <> 'published' OR dependency.service_status <> 'published' THEN
                RAISE EXCEPTION 'catalog dependency is not published' USING ERRCODE = '23514';
            END IF;
            IF NOT dependency.allowed THEN
                RAISE EXCEPTION 'catalog dependency is not available to this tenant' USING ERRCODE = '23514';
            END IF;
        END LOOP;
    END IF;
    RETURN NEW;
END $$;

-- 2. operation keys and cached specs --------------------------------------------------------
CREATE TABLE public.mcp_operation_keys (
    service_id uuid NOT NULL REFERENCES public.mcp_services(service_id),
    op_key text NOT NULL CHECK (op_key ~ '^[a-z][a-z0-9_]{0,63}$'),
    operation_id uuid NOT NULL UNIQUE CHECK (uuid_extract_version(operation_id)=7),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (service_id, op_key)
);
CREATE FUNCTION public.mcp_guard_operation_key() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'operation keys are append-only' USING ERRCODE = '23514';
    END IF;
    -- The operation's versions, if any exist yet, must belong to the keyed service.
    IF EXISTS (SELECT 1 FROM public.mcp_operation_versions
               WHERE operation_id = NEW.operation_id AND service_id <> NEW.service_id) THEN
        RAISE EXCEPTION 'operation belongs to another service' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.mcp_guard_operation_key() FROM PUBLIC;
CREATE TRIGGER operation_key_guard BEFORE INSERT OR UPDATE OR DELETE ON public.mcp_operation_keys
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_operation_key();

CREATE TABLE public.mcp_source_specs (
    owner_tenant_id uuid NOT NULL,
    source_id uuid NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    fetched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    bytes integer NOT NULL CHECK (bytes > 0),
    document jsonb NOT NULL,
    PRIMARY KEY (source_id, sha256),
    FOREIGN KEY (source_id, owner_tenant_id)
        REFERENCES public.mcp_sources(source_id, owner_tenant_id)
);
CREATE INDEX mcp_source_specs_owner_idx ON public.mcp_source_specs(owner_tenant_id, source_id, fetched_at);

-- 3. runs: bound (binding) or draft (author test run) ------------------------------------------
ALTER TABLE public.mcp_runs
    ADD COLUMN mode text NOT NULL DEFAULT 'bound',
    ALTER COLUMN binding_id DROP NOT NULL,
    ALTER COLUMN binding_revision DROP NOT NULL,
    ALTER COLUMN credential_profile_id DROP NOT NULL,
    ADD CONSTRAINT mcp_runs_mode_check CHECK (mode IN ('bound','draft')) NOT VALID,
    ADD CONSTRAINT mcp_runs_mode_binding CHECK (
        (mode='bound' AND binding_id IS NOT NULL AND binding_revision IS NOT NULL
            AND credential_profile_id IS NOT NULL)
        OR (mode='draft' AND binding_id IS NULL AND binding_revision IS NULL
            AND credential_profile_id IS NULL AND owner_tenant_id=tenant_id)) NOT VALID;
-- Index builds below scan their (small, today) tables inside this transaction; not CONCURRENTLY
-- because alembic runs the migration in one transaction.
CREATE UNIQUE INDEX mcp_runs_draft_idempotency
    ON public.mcp_runs(tenant_id,tool_id,tool_version,idempotency_key)
    WHERE mode='draft' AND idempotency_key IS NOT NULL;

ALTER TABLE public.mcp_run_api_calls
    ADD COLUMN method text,
    ADD COLUMN path text,
    ADD COLUMN http_status integer,
    ADD COLUMN duration_ms integer,
    ADD COLUMN reason text,
    ADD COLUMN error_excerpt text,
    ADD CONSTRAINT mcp_run_api_calls_method_check
        CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')) NOT VALID,
    ADD CONSTRAINT mcp_run_api_calls_path_check CHECK (char_length(path) <= 1024) NOT VALID,
    ADD CONSTRAINT mcp_run_api_calls_http_status_check
        CHECK (http_status BETWEEN 100 AND 599) NOT VALID,
    ADD CONSTRAINT mcp_run_api_calls_duration_ms_check CHECK (duration_ms >= 0) NOT VALID,
    ADD CONSTRAINT mcp_run_api_calls_reason_check CHECK (reason IN ('ssrf_blocked','redirect',
        'status','too_large','schema_mismatch','auth_failed','timeout','transport')) NOT VALID,
    ADD CONSTRAINT mcp_run_api_calls_error_excerpt_check
        CHECK (char_length(error_excerpt) <= 2048) NOT VALID;
CREATE OR REPLACE FUNCTION public.mcp_guard_execution_journal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR TG_TABLE_NAME='mcp_run_events' THEN RAISE EXCEPTION 'execution history is immutable'; END IF;
    IF (to_jsonb(NEW)-ARRAY['status','finished_at','http_status','duration_ms','reason','error_excerpt']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','finished_at','http_status','duration_ms','reason','error_excerpt']) OR OLD.status<>'intent' OR
       NEW.status NOT IN ('succeeded','failed','outcome_unknown') OR NEW.finished_at IS NULL THEN
       RAISE EXCEPTION 'journal intent is immutable; only its outcome may be finalized';
    END IF;
    RETURN NEW;
END $$;

-- 4. audit -----------------------------------------------------------------------------------
ALTER TABLE public.mcp_tool_call_audit DROP CONSTRAINT mcp_tool_call_audit_principal_kind_check;
ALTER TABLE public.mcp_tool_call_audit ADD CONSTRAINT mcp_tool_call_audit_principal_kind_check
    CHECK (principal_kind IN ('carebus_licence','keycloak_service','tenant_author')) NOT VALID;

-- 5. builder write functions ---------------------------------------------------------------
CREATE FUNCTION public.mcp_builder_guard_tenant(p_tenant uuid) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF p_tenant IS NULL OR p_tenant = public.mcp_platform_tenant() THEN
        RAISE EXCEPTION 'the platform tenant cannot use the builder' USING ERRCODE = 'MB004';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('builder:' || p_tenant::text));
END $$;
REVOKE ALL ON FUNCTION public.mcp_builder_guard_tenant(uuid) FROM PUBLIC;

CREATE FUNCTION public.mcp_builder_source_create(
    p_tenant uuid, p_source_id uuid, p_key text, p_base_url text, p_auth jsonb, p_token bytea,
    p_spec_path text, p_idempotency_header text, p_max_request_bytes integer,
    p_max_response_bytes integer, p_actor jsonb, p_max_sources integer)
RETURNS public.mcp_sources LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r public.mcp_sources;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    IF (SELECT count(*) FROM public.mcp_sources
        WHERE owner_tenant_id = p_tenant AND status = 'active') >= p_max_sources THEN
        RAISE EXCEPTION 'source quota exceeded' USING ERRCODE = 'MB003';
    END IF;
    INSERT INTO public.mcp_sources(source_id,owner_tenant_id,source_key,base_url,allow_internal_http,
        auth,token_ciphertext,token_set_at,spec_path,idempotency_header,ca_file,max_request_bytes,
        max_response_bytes,created_by,updated_by)
    VALUES (p_source_id,p_tenant,p_key,p_base_url,false,p_auth,p_token,
        CASE WHEN p_token IS NULL THEN NULL ELSE clock_timestamp() END,p_spec_path,
        p_idempotency_header,NULL,p_max_request_bytes,p_max_response_bytes,p_actor,p_actor)
    RETURNING * INTO r;
    INSERT INTO public.mcp_source_events(source_id,owner_tenant_id,source_key,revision,event,actor)
        VALUES (r.source_id,r.owner_tenant_id,r.source_key,r.revision,'created',p_actor);
    RETURN r;
END $$;

CREATE FUNCTION public.mcp_builder_source_update(
    p_tenant uuid, p_key text, p_expected_revision bigint, p_base_url text, p_auth jsonb,
    p_token bytea, p_spec_path text, p_idempotency_header text, p_max_request_bytes integer,
    p_max_response_bytes integer, p_actor jsonb)
RETURNS public.mcp_sources LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE cur public.mcp_sources; r public.mcp_sources;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    SELECT * INTO cur FROM public.mcp_sources
        WHERE owner_tenant_id = p_tenant AND source_key = p_key FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'source not found' USING ERRCODE = 'MB002'; END IF;
    IF cur.revision <> p_expected_revision THEN
        RAISE EXCEPTION 'source revision mismatch' USING ERRCODE = 'MB001';
    END IF;
    UPDATE public.mcp_sources SET revision = revision + 1, base_url = p_base_url, auth = p_auth,
        token_ciphertext = CASE WHEN p_auth->>'kind' IN ('licence','none') THEN NULL
                                WHEN p_token IS NOT NULL THEN p_token ELSE token_ciphertext END,
        token_set_at = CASE WHEN p_auth->>'kind' IN ('licence','none') THEN NULL
                            WHEN p_token IS NOT NULL THEN clock_timestamp() ELSE token_set_at END,
        spec_path = p_spec_path, idempotency_header = p_idempotency_header,
        max_request_bytes = p_max_request_bytes, max_response_bytes = p_max_response_bytes,
        updated_by = p_actor, updated_at = clock_timestamp()
        WHERE source_id = cur.source_id RETURNING * INTO r;
    INSERT INTO public.mcp_source_events(source_id,owner_tenant_id,source_key,revision,event,actor)
        VALUES (r.source_id,r.owner_tenant_id,r.source_key,r.revision,'updated',p_actor);
    RETURN r;
END $$;

CREATE FUNCTION public.mcp_builder_source_set_secret(
    p_tenant uuid, p_key text, p_expected_revision bigint, p_token bytea, p_actor jsonb)
RETURNS public.mcp_sources LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE cur public.mcp_sources; r public.mcp_sources;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    SELECT * INTO cur FROM public.mcp_sources
        WHERE owner_tenant_id = p_tenant AND source_key = p_key FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'source not found' USING ERRCODE = 'MB002'; END IF;
    IF cur.revision <> p_expected_revision THEN
        RAISE EXCEPTION 'source revision mismatch' USING ERRCODE = 'MB001';
    END IF;
    IF cur.auth->>'kind' IN ('licence','none') OR p_token IS NULL THEN
        RAISE EXCEPTION 'this source carries no stored secret' USING ERRCODE = '23514';
    END IF;
    UPDATE public.mcp_sources SET revision = revision + 1, token_ciphertext = p_token,
        token_set_at = clock_timestamp(), updated_by = p_actor, updated_at = clock_timestamp()
        WHERE source_id = cur.source_id RETURNING * INTO r;
    INSERT INTO public.mcp_source_events(source_id,owner_tenant_id,source_key,revision,event,actor)
        VALUES (r.source_id,r.owner_tenant_id,r.source_key,r.revision,'token_set',p_actor);
    RETURN r;
END $$;

CREATE FUNCTION public.mcp_builder_source_set_status(
    p_tenant uuid, p_key text, p_expected_revision bigint, p_status text, p_actor jsonb)
RETURNS public.mcp_sources LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE cur public.mcp_sources; r public.mcp_sources;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    IF p_status NOT IN ('active','disabled') THEN
        RAISE EXCEPTION 'invalid source status' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO cur FROM public.mcp_sources
        WHERE owner_tenant_id = p_tenant AND source_key = p_key FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'source not found' USING ERRCODE = 'MB002'; END IF;
    IF cur.revision <> p_expected_revision OR cur.status = p_status THEN
        RAISE EXCEPTION 'source revision mismatch or already in that state' USING ERRCODE = 'MB001';
    END IF;
    UPDATE public.mcp_sources SET revision = revision + 1, status = p_status,
        updated_by = p_actor, updated_at = clock_timestamp()
        WHERE source_id = cur.source_id RETURNING * INTO r;
    INSERT INTO public.mcp_source_events(source_id,owner_tenant_id,source_key,revision,event,actor)
        VALUES (r.source_id,r.owner_tenant_id,r.source_key,r.revision,
            CASE WHEN p_status = 'disabled' THEN 'disabled' ELSE 'enabled' END,p_actor);
    RETURN r;
END $$;

-- A builder service is the 1:1 catalog identity of one tenant source (adapter ``source``).
CREATE FUNCTION public.mcp_builder_service_create(p_tenant uuid, p_manifest jsonb, p_actor jsonb)
RETURNS public.mcp_service_versions LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE sid uuid := (p_manifest->>'service_id')::uuid; r public.mcp_service_versions;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    IF (p_manifest->>'version')::integer <> 1 OR p_manifest->>'auth_adapter' <> 'source' THEN
        RAISE EXCEPTION 'a builder service starts at version 1 with the source adapter'
            USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.mcp_sources WHERE owner_tenant_id = p_tenant
        AND source_key = p_manifest->>'destination_alias' AND status = 'active') THEN
        RAISE EXCEPTION 'source not found' USING ERRCODE = 'MB002';
    END IF;
    IF EXISTS (SELECT 1 FROM public.mcp_services s
        JOIN public.mcp_service_versions v ON v.service_id = s.service_id AND v.version = 1
        WHERE s.owner_tenant_id = p_tenant
          AND v.manifest->>'destination_alias' = p_manifest->>'destination_alias') THEN
        RAISE EXCEPTION 'this source already has a service' USING ERRCODE = 'MB001';
    END IF;
    INSERT INTO public.mcp_services(service_id,service_key,owner_tenant_id,created_by)
        VALUES (sid,p_manifest->>'service_key',p_tenant,p_actor);
    INSERT INTO public.mcp_service_versions(service_id,version,manifest,created_by,updated_by)
        VALUES (sid,1,p_manifest,p_actor,p_actor) RETURNING * INTO r;
    RETURN r;
END $$;

CREATE FUNCTION public.mcp_builder_service_append(
    p_tenant uuid, p_manifest jsonb, p_expected_revision bigint, p_actor jsonb)
RETURNS public.mcp_service_versions LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE sid uuid := (p_manifest->>'service_id')::uuid; s public.mcp_services;
        nxt integer; first_manifest jsonb; r public.mcp_service_versions;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    SELECT * INTO s FROM public.mcp_services
        WHERE service_id = sid AND owner_tenant_id = p_tenant FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'service not found' USING ERRCODE = 'MB002'; END IF;
    SELECT max(version) + 1 INTO nxt FROM public.mcp_service_versions WHERE service_id = sid;
    SELECT manifest INTO first_manifest FROM public.mcp_service_versions
        WHERE service_id = sid AND version = 1;
    IF s.revision <> p_expected_revision OR (p_manifest->>'version')::integer <> nxt
       OR p_manifest->>'service_key' <> s.service_key
       OR p_manifest->>'destination_alias' <> first_manifest->>'destination_alias'
       OR p_manifest->>'auth_adapter' <> 'source' THEN
        RAISE EXCEPTION 'service identity, revision or version mismatch' USING ERRCODE = 'MB001';
    END IF;
    UPDATE public.mcp_services SET revision = revision + 1 WHERE service_id = sid;
    INSERT INTO public.mcp_service_versions(service_id,version,manifest,created_by,updated_by)
        VALUES (sid,nxt,p_manifest,p_actor,p_actor) RETURNING * INTO r;
    RETURN r;
END $$;

CREATE FUNCTION public.mcp_builder_operation_create(
    p_tenant uuid, p_manifest jsonb, p_op_key text, p_actor jsonb, p_max_operations integer)
RETURNS public.mcp_operation_versions LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE op_id uuid := (p_manifest->>'operation_id')::uuid;
        sid uuid := (p_manifest->'service'->>'id')::uuid;
        sver integer := (p_manifest->'service'->>'version')::integer;
        nxt integer; svc_status text; keyed uuid; r public.mcp_operation_versions;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    SELECT v.status INTO svc_status FROM public.mcp_service_versions v
        JOIN public.mcp_services s USING (service_id)
        WHERE v.service_id = sid AND v.version = sver AND s.owner_tenant_id = p_tenant;
    IF NOT FOUND THEN RAISE EXCEPTION 'service not found' USING ERRCODE = 'MB002'; END IF;
    IF svc_status NOT IN ('draft','published') THEN
        RAISE EXCEPTION 'service version is not usable' USING ERRCODE = 'MB001';
    END IF;
    SELECT coalesce(max(version),0) + 1 INTO nxt FROM public.mcp_operation_versions
        WHERE operation_id = op_id;
    IF (p_manifest->>'version')::integer <> nxt THEN
        RAISE EXCEPTION 'operation version must append' USING ERRCODE = 'MB001';
    END IF;
    IF nxt = 1 THEN
        IF p_op_key IS NULL THEN
            RAISE EXCEPTION 'a new operation needs an operation key' USING ERRCODE = '23514';
        END IF;
        IF (SELECT count(*) FROM public.mcp_operation_keys k
            JOIN public.mcp_services s USING (service_id)
            WHERE s.owner_tenant_id = p_tenant) >= p_max_operations THEN
            RAISE EXCEPTION 'operation quota exceeded' USING ERRCODE = 'MB003';
        END IF;
        INSERT INTO public.mcp_operation_keys(service_id,op_key,operation_id) VALUES (sid,p_op_key,op_id);
    ELSE
        SELECT service_id INTO keyed FROM public.mcp_operation_keys WHERE operation_id = op_id;
        IF keyed IS DISTINCT FROM sid OR (p_op_key IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.mcp_operation_keys WHERE operation_id = op_id AND op_key = p_op_key)) THEN
            RAISE EXCEPTION 'operation key or service mismatch' USING ERRCODE = 'MB001';
        END IF;
    END IF;
    INSERT INTO public.mcp_operation_versions(operation_id,version,service_id,service_version,
        manifest,created_by,updated_by)
        VALUES (op_id,nxt,sid,sver,p_manifest,p_actor,p_actor) RETURNING * INTO r;
    RETURN r;
END $$;

-- Lifecycle of the tenant's own service/operation versions (draft -> published -> ...).
CREATE FUNCTION public.mcp_builder_set_status(
    p_tenant uuid, p_kind text, p_id uuid, p_version integer, p_status text, p_actor jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    IF p_status NOT IN ('published','disabled','retired') THEN
        RAISE EXCEPTION 'invalid lifecycle state' USING ERRCODE = '23514';
    END IF;
    IF p_kind = 'service' THEN
        IF NOT EXISTS (SELECT 1 FROM public.mcp_services WHERE service_id = p_id AND owner_tenant_id = p_tenant) THEN
            RAISE EXCEPTION 'service not found' USING ERRCODE = 'MB002';
        END IF;
        UPDATE public.mcp_service_versions SET status = p_status, updated_by = p_actor,
            updated_at = clock_timestamp() WHERE service_id = p_id AND version = p_version;
    ELSIF p_kind = 'operation' THEN
        IF NOT EXISTS (SELECT 1 FROM public.mcp_operation_versions o
            JOIN public.mcp_services s USING (service_id)
            WHERE o.operation_id = p_id AND o.version = p_version AND s.owner_tenant_id = p_tenant) THEN
            RAISE EXCEPTION 'operation not found' USING ERRCODE = 'MB002';
        END IF;
        UPDATE public.mcp_operation_versions SET status = p_status, updated_by = p_actor,
            updated_at = clock_timestamp() WHERE operation_id = p_id AND version = p_version;
    ELSE
        RAISE EXCEPTION 'unknown kind' USING ERRCODE = '23514';
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'version not found' USING ERRCODE = 'MB002'; END IF;
END $$;

-- Publish the given service versions, then operation versions, in the caller's transaction
-- (a JSON array of id/version objects). Idempotent for versions that are already published.
CREATE FUNCTION public.mcp_builder_publish(
    p_tenant uuid, p_services jsonb, p_operations jsonb, p_actor jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE e jsonb;
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    FOR e IN SELECT * FROM jsonb_array_elements(p_services) LOOP
        PERFORM public.mcp_builder_set_status(p_tenant,'service',(e->>'id')::uuid,(e->>'version')::integer,'published',p_actor);
    END LOOP;
    FOR e IN SELECT * FROM jsonb_array_elements(p_operations) LOOP
        PERFORM public.mcp_builder_set_status(p_tenant,'operation',(e->>'id')::uuid,(e->>'version')::integer,'published',p_actor);
    END LOOP;
END $$;

CREATE FUNCTION public.mcp_builder_spec_put(
    p_tenant uuid, p_source_id uuid, p_sha256 text, p_bytes integer, p_document jsonb, p_keep integer,
    p_max_total bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    PERFORM public.mcp_builder_guard_tenant(p_tenant);
    IF p_keep NOT BETWEEN 1 AND 10 THEN
        RAISE EXCEPTION 'spec retention outside bounds' USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.mcp_sources
        WHERE source_id = p_source_id AND owner_tenant_id = p_tenant) THEN
        RAISE EXCEPTION 'source not found' USING ERRCODE = 'MB002';
    END IF;
    INSERT INTO public.mcp_source_specs(owner_tenant_id,source_id,sha256,bytes,document)
        VALUES (p_tenant,p_source_id,p_sha256,p_bytes,p_document)
        ON CONFLICT (source_id,sha256) DO UPDATE SET fetched_at = clock_timestamp();
    DELETE FROM public.mcp_source_specs WHERE source_id = p_source_id AND sha256 NOT IN (
        SELECT sha256 FROM public.mcp_source_specs WHERE source_id = p_source_id
        ORDER BY fetched_at DESC, sha256 LIMIT p_keep);
    -- Per-tenant storage cap, counted after pruning, under the tenant advisory lock taken by the
    -- guard above; raising rolls the insert back.
    IF p_max_total < 1 OR (SELECT COALESCE(sum(bytes), 0) FROM public.mcp_source_specs
                           WHERE owner_tenant_id = p_tenant) > p_max_total THEN
        RAISE EXCEPTION 'spec storage quota exceeded' USING ERRCODE = 'MB003';
    END IF;
END $$;

REVOKE ALL ON FUNCTION public.mcp_builder_source_create(uuid,uuid,text,text,jsonb,bytea,text,text,integer,integer,jsonb,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_source_update(uuid,text,bigint,text,jsonb,bytea,text,text,integer,integer,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_source_set_secret(uuid,text,bigint,bytea,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_source_set_status(uuid,text,bigint,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_service_create(uuid,jsonb,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_service_append(uuid,jsonb,bigint,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_operation_create(uuid,jsonb,text,jsonb,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_set_status(uuid,text,uuid,integer,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_publish(uuid,jsonb,jsonb,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.mcp_builder_spec_put(uuid,uuid,text,integer,jsonb,integer,bigint) FROM PUBLIC;
"""


VALIDATE_SQL = """
ALTER TABLE public.mcp_tool_call_audit VALIDATE CONSTRAINT mcp_tool_call_audit_principal_kind_check;
ALTER TABLE public.mcp_runs VALIDATE CONSTRAINT mcp_runs_mode_check;
ALTER TABLE public.mcp_runs VALIDATE CONSTRAINT mcp_runs_mode_binding;
ALTER TABLE public.mcp_run_api_calls VALIDATE CONSTRAINT mcp_run_api_calls_method_check;
ALTER TABLE public.mcp_run_api_calls VALIDATE CONSTRAINT mcp_run_api_calls_path_check;
ALTER TABLE public.mcp_run_api_calls VALIDATE CONSTRAINT mcp_run_api_calls_http_status_check;
ALTER TABLE public.mcp_run_api_calls VALIDATE CONSTRAINT mcp_run_api_calls_duration_ms_check;
ALTER TABLE public.mcp_run_api_calls VALIDATE CONSTRAINT mcp_run_api_calls_reason_check;
ALTER TABLE public.mcp_run_api_calls VALIDATE CONSTRAINT mcp_run_api_calls_error_excerpt_check;
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)
    # VALIDATE only takes SHARE UPDATE EXCLUSIVE (writes continue), but it must run in its own
    # transaction or the ACCESS EXCLUSIVE lock from ADD CONSTRAINT would be held through the scan.
    with op.get_context().autocommit_block():
        for stmt in VALIDATE_SQL.strip().split(";\n"):
            op.execute(stmt.strip().rstrip(";"))


def downgrade() -> None:
    raise RuntimeError("the tenant builder is fix forward only")
