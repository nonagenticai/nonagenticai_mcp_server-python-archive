"""Runtime tool-source registry: a base URL plus its credential, owned by the platform.

``mcp_sources`` replaces the static destinations file. A service manifest's
``destination_alias`` names a ``source_key``; the broker resolves URL and credential from this
table at call time. A source authenticates either as the calling tenant's licence for an
application (``licence``, minted per call) or with one stored token (``static_bearer`` /
``static_header``). The token is AES-256-GCM ciphertext bound to ``source_id`` (the key is a
deployment secret, never in the database) and is never returned by any API. Rows are append-only
in identity: ``source_key`` is immutable, every change advances ``revision`` by one, nothing is
deleted (``status`` disables). ``mcp_source_events`` is the append-only audit trail and carries
no credential material.

Self-contained; no application imports. Fix forward only.
"""

from alembic import op

revision = "0009_tool_sources"
down_revision = "0008_platform_toolkits"
branch_labels = None
depends_on = None

SCHEMA_SQL = r"""
CREATE TABLE public.mcp_sources (
    source_id uuid PRIMARY KEY CHECK (uuid_extract_version(source_id)=7),
    source_key text NOT NULL UNIQUE CHECK (source_key ~ '^[a-z][a-z0-9_]{0,63}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    base_url text NOT NULL CHECK (char_length(base_url) BETWEEN 8 AND 512),
    allow_internal_http boolean NOT NULL DEFAULT false,
    auth jsonb NOT NULL CHECK (
        jsonb_typeof(auth)='object' AND auth->>'kind' IN ('licence','static_bearer','static_header')),
    token_ciphertext bytea CHECK (octet_length(token_ciphertext) BETWEEN 29 AND 4124),
    token_set_at timestamptz,
    idempotency_header text CHECK (idempotency_header IN ('Idempotency-Key','X-Idempotency-Key')),
    ca_file text CHECK (char_length(ca_file) <= 512),
    max_request_bytes integer CHECK (max_request_bytes > 0),
    max_response_bytes integer CHECK (max_response_bytes > 0),
    created_by jsonb NOT NULL,
    updated_by jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((auth->>'kind'='licence') = (token_ciphertext IS NULL)),
    CHECK ((token_ciphertext IS NULL) = (token_set_at IS NULL))
);

CREATE FUNCTION public.mcp_guard_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'sources cannot be deleted; disable them' USING ERRCODE = '23514';
    END IF;
    IF NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.source_key IS DISTINCT FROM OLD.source_key
       OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'source identity immutable; revision must advance once' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER source_guard BEFORE UPDATE OR DELETE ON public.mcp_sources
    FOR EACH ROW EXECUTE FUNCTION public.mcp_guard_source();

CREATE TABLE public.mcp_source_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id uuid NOT NULL REFERENCES public.mcp_sources(source_id),
    source_key text NOT NULL,
    revision bigint NOT NULL,
    event text NOT NULL CHECK (event IN ('created','updated','token_set','disabled','enabled')),
    actor jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX mcp_source_events_source_idx ON public.mcp_source_events(source_id, event_id);
"""


def upgrade() -> None:
    op.execute(SCHEMA_SQL)


def downgrade() -> None:
    raise RuntimeError("tool sources are fix forward only")
