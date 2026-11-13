"""Version-specific tenant catalog audit, preserving all phase-2 data."""

from alembic import op

revision = "0003_catalog_publication"
down_revision = "0002_toolkits_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.mcp_access_events
          ADD COLUMN artifact_version integer CHECK (artifact_version > 0),
          ADD CONSTRAINT tool_event_version_required CHECK (
            action NOT IN ('tool.created','tool.version_created','tool.published',
                           'tool.disabled','tool.retired') OR artifact_version IS NOT NULL
          )
    """)


def downgrade() -> None:
    raise RuntimeError("catalog migrations are forward-only; fix forward")
