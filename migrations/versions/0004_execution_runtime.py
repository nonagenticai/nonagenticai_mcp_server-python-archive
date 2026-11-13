"""Durable runs, fenced leases, journal and shared capacity. Fix forward only."""

from alembic import op

revision = "0004_execution_runtime"
down_revision = "0003_catalog_publication"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
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
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("execution history is immutable; fix forward")
