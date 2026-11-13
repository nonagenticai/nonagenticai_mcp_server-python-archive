"""Bounded payload retention and non-disruptive capacity draining. Fix forward only."""

from alembic import op

revision = "0005_execution_controls"
down_revision = "0004_execution_runtime"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
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
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("execution controls are fix forward only")
