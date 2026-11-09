"""Durable owner-scoped conversation history and resumable provider reads."""
from alembic import op

revision = "0012_inbox_history"
down_revision = "0011_unified_inbox"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE inbox_history_accounts (
            owner_id integer NOT NULL REFERENCES users(id), scope text NOT NULL,
            cursor text, next_attempt timestamptz NOT NULL DEFAULT now(), error text,
            PRIMARY KEY(owner_id,scope)
        )
    """)
    op.execute("""
        CREATE TABLE inbox_history_threads (
            owner_id integer NOT NULL REFERENCES users(id), scope text NOT NULL, thread_key text NOT NULL,
            native_id text NOT NULL, participant text NOT NULL, name text NOT NULL,
            last_seen timestamptz NOT NULL DEFAULT now(), last_attempt timestamptz,
            older_cursor text, reached_start boolean NOT NULL DEFAULT false,
            provider_updated text, scanned_version text, incremental boolean NOT NULL DEFAULT false,
            incomplete boolean NOT NULL DEFAULT false, error text,
            PRIMARY KEY (owner_id,scope,thread_key)
        )
    """)
    op.execute("""
        CREATE TABLE inbox_history_messages (
            owner_id integer NOT NULL, scope text NOT NULL, thread_key text NOT NULL,
            message_key text NOT NULL, native_id text NOT NULL, sent_at timestamptz NOT NULL,
            direction text NOT NULL CHECK (direction IN ('incoming','outgoing')),
            body text NOT NULL, name text NOT NULL, revision text NOT NULL,
            PRIMARY KEY(owner_id,scope,thread_key,message_key),
            FOREIGN KEY(owner_id,scope,thread_key) REFERENCES inbox_history_threads
        )
    """)
    op.execute("CREATE INDEX inbox_history_timeline ON inbox_history_messages(owner_id,scope,thread_key,sent_at DESC,message_key DESC)")
    op.execute("""
        CREATE TABLE inbox_history_chunks (
            owner_id integer NOT NULL, scope text NOT NULL, thread_key text NOT NULL,
            chunk_id text NOT NULL, content_hash text NOT NULL, stored integer NOT NULL,
            PRIMARY KEY(owner_id,scope,thread_key,chunk_id),
            FOREIGN KEY(owner_id,scope,thread_key) REFERENCES inbox_history_threads
        )
    """)


def downgrade():
    raise RuntimeError("Keep the additive history tables when rolling application code back.")
