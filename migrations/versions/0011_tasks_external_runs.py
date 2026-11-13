"""External-runtime runs and MCP task inputs. Fix forward in prod; downgrade restores 0005."""

from alembic import op

revision = "0011_tasks_external_runs"
down_revision = "0010_tenant_builder"
branch_labels = None
depends_on = None

TABLE_SQL = r"""
ALTER TABLE public.mcp_runs
    ADD COLUMN runtime text NOT NULL DEFAULT 'kata_python'
        CHECK (runtime IN ('kata_python','n8n_workflow','fr_project')),
    ADD COLUMN external_ref jsonb,
    ADD COLUMN status_message text CHECK (char_length(status_message)<=512),
    -- The human (or agent) the client says it acts for (_meta "ai.nonagentic/actor"); fr's
    -- channel address. Set at acceptance, immutable (not in the guard's mutable list).
    ADD COLUMN actor jsonb CHECK (actor IS NULL OR jsonb_typeof(actor)='object');

CREATE TABLE public.mcp_task_inputs (
    input_key text PRIMARY KEY CHECK (char_length(input_key)<=200),
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    source_ref text NOT NULL CHECK (char_length(source_ref)<=200),
    request jsonb NOT NULL CHECK (jsonb_typeof(request)='object'),
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','answered','forwarded','closed')),
    response jsonb CHECK (response IS NULL OR jsonb_typeof(response)='object'),
    actor jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    answered_at timestamptz,
    closed_at timestamptz,
    FOREIGN KEY (tenant_id,run_id) REFERENCES public.mcp_runs(tenant_id,run_id),
    UNIQUE (run_id,source_ref)
);
CREATE INDEX mcp_task_inputs_open ON public.mcp_task_inputs(run_id) WHERE state='open';
CREATE INDEX mcp_task_inputs_forward ON public.mcp_task_inputs(answered_at) WHERE state='answered';
"""

GUARD_0011 = r"""
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
    IF NEW.runtime IS DISTINCT FROM OLD.runtime THEN RAISE EXCEPTION 'run runtime is immutable'; END IF;
    -- An external reference only grows: keys can be added (fr's project id arrives after the
    -- start approval), never changed or removed.
    IF OLD.external_ref IS NOT NULL AND (NEW.external_ref IS NULL OR NOT NEW.external_ref @> OLD.external_ref) THEN
        RAISE EXCEPTION 'external reference only grows';
    END IF;
    IF NEW.payload_expired_at IS DISTINCT FROM OLD.payload_expired_at THEN
        RAISE EXCEPTION 'payload expiration is final';
    END IF;
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'run history is immutable'; END IF;
    IF (to_jsonb(NEW)-ARRAY['status','start_deadline','deadline','started_at','finished_at',
        'worker_id','fence','lease_until','heartbeat_at','pod_name','pod_uid','cancel_requested',
        'api_calls','result','error','external_ref','status_message']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','start_deadline','deadline','started_at','finished_at',
        'worker_id','fence','lease_until','heartbeat_at','pod_name','pod_uid','cancel_requested',
        'api_calls','result','error','external_ref','status_message']) THEN RAISE EXCEPTION 'run snapshot is immutable'; END IF;
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
"""

GUARD_0005 = r"""
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
"""


def upgrade() -> None:
    op.execute(TABLE_SQL)
    op.execute(GUARD_0011)


def downgrade() -> None:
    op.execute("DROP TABLE public.mcp_task_inputs")
    op.execute(GUARD_0005)
    op.execute(
        "ALTER TABLE public.mcp_runs DROP COLUMN actor, DROP COLUMN status_message, DROP COLUMN external_ref, DROP COLUMN runtime"
    )
