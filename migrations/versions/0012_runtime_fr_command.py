"""Allow the fr_command external runtime on runs. Fix forward in prod; downgrade restores 0011."""

from alembic import op

revision = "0012_runtime_fr_command"
down_revision = "0011_tasks_external_runs"
branch_labels = None
depends_on = None

UPGRADE_SQL = r"""
-- 0011 added the column with an inline CHECK whose name PostgreSQL chose. Drop whichever
-- runtime CHECK exists instead of assuming that name, then re-add it with fr_command.
DO $$
DECLARE constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
      FROM pg_constraint
     WHERE conrelid = 'public.mcp_runs'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%runtime%';
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.mcp_runs DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;

ALTER TABLE public.mcp_runs
    ADD CONSTRAINT mcp_runs_runtime_check
        CHECK (runtime IN ('kata_python','n8n_workflow','fr_project','fr_command'));
"""

DOWNGRADE_SQL = r"""
ALTER TABLE public.mcp_runs DROP CONSTRAINT IF EXISTS mcp_runs_runtime_check;
ALTER TABLE public.mcp_runs
    ADD CONSTRAINT mcp_runs_runtime_check
        CHECK (runtime IN ('kata_python','n8n_workflow','fr_project'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
