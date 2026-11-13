"""Run activity for external runs and expiring task inputs. Fix forward in prod.

``mcp_run_activity`` is the append-only progress feed the platform reads through
``tasks/get`` ``_meta["ai.nonagentic/fr"]`` (the Rust client keeps ``_meta``; it drops unknown
top-level fields). It is written by the API (fr channel events) and by the external driver, so
both runtime roles insert and read it. ``mcp_task_inputs.expires_at`` lets an fr approval that
already expired never surface as an open input.
"""

from alembic import op

revision = "0013_run_activity"
down_revision = "0012_runtime_fr_command"
branch_labels = None
depends_on = None

UPGRADE_SQL = r"""
CREATE TABLE public.mcp_run_activity (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    -- Monotonic per run: the wire contract's ordering key, newest = greatest seq.
    seq bigserial NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'phase','started','approval_requested','approval_closed','refused','failed','delivered')),
    -- The wire contract caps text at 512 characters; kind is the fallback when nothing was said.
    text text NOT NULL CHECK (char_length(text)<=512),
    severity text NOT NULL CHECK (
        severity IN ('info','low','medium','high','critical')),
    source_ref text CHECK (source_ref IS NULL OR char_length(source_ref)<=200),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (tenant_id, run_id) REFERENCES public.mcp_runs(tenant_id, run_id)
);
CREATE INDEX mcp_run_activity_run ON public.mcp_run_activity(run_id);

ALTER TABLE public.mcp_task_inputs ADD COLUMN expires_at timestamptz;
"""

DOWNGRADE_SQL = r"""
ALTER TABLE public.mcp_task_inputs DROP COLUMN expires_at;
DROP TABLE public.mcp_run_activity;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
