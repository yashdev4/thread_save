"""init_schema

Revision ID: ddd36e757df0
Revises: 
Create Date: 2026-10-04 17:20:24.994172

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'ddd36e757df0'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Accounts
    op.execute("""
    CREATE TABLE accounts (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        oauth_sub   text UNIQUE NOT NULL,
        slug        text UNIQUE NOT NULL,
        created_at  timestamptz DEFAULT now()
    );
    """)

    # 2. Threads
    op.execute("""
    CREATE TABLE threads (
        id                  text PRIMARY KEY,
        account_id          uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        title               text,
        slug                text,
        created_at          timestamptz DEFAULT now(),
        updated_at          timestamptz DEFAULT now(),
        open_turn           int,
        paused              boolean DEFAULT false,
        max_n               int NOT NULL DEFAULT 0,
        current_page        int NOT NULL DEFAULT 1,
        current_page_turns  int NOT NULL DEFAULT 0,
        current_page_bytes  int NOT NULL DEFAULT 0,
        delim               char(4) NOT NULL
    );
    """)

    # 3. Turns
    op.execute("""
    CREATE TABLE turns (
        thread_id     text NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        n             int NOT NULL,
        role          text NOT NULL CHECK (role IN ('user', 'assistant')),
        body          text NOT NULL,
        fidelity      smallint NOT NULL,
        chars         int NOT NULL,
        hash          text NOT NULL,
        recovered     boolean NOT NULL DEFAULT false,
        created_at    timestamptz DEFAULT now(),
        updated_at    timestamptz DEFAULT now(),
        tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,
        page          int NOT NULL,
        turn_key      text,
        hash_version  smallint NOT NULL DEFAULT 1,
        PRIMARY KEY (thread_id, n, role)
    );
    CREATE INDEX turns_tsv ON turns USING gin(tsv);
    CREATE UNIQUE INDEX turns_turn_key ON turns(thread_id, turn_key) WHERE role = 'user' AND turn_key IS NOT NULL;
    """)

    # 4. Gaps
    op.execute("""
    CREATE TABLE gaps (
        thread_id   text NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        n           int NOT NULL,
        state       text NOT NULL,
        requested   boolean NOT NULL DEFAULT false,
        first_seen  timestamptz DEFAULT now(),
        PRIMARY KEY (thread_id, n)
    );
    """)

    # 5. Turn Chunks
    op.execute("""
    CREATE TABLE turn_chunks (
        thread_id    text NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
        n            int NOT NULL,
        role         text NOT NULL,
        idx          int NOT NULL,
        body         text NOT NULL,
        received_at  timestamptz DEFAULT now(),
        PRIMARY KEY (thread_id, n, role, idx)
    );
    """)

    # 6. Outbox
    op.execute("""
    CREATE TABLE outbox (
        thread_id  text PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
        target     text NOT NULL DEFAULT 'default',
        due_at     timestamptz NOT NULL,
        last_hash  text,
        attempts   int NOT NULL DEFAULT 0
    );
    """)

    # 7. Deleted Threads (Tombstones)
    op.execute("""
    CREATE TABLE deleted_threads (
        thread_id   text PRIMARY KEY,
        account_id  uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        deleted_at  timestamptz DEFAULT now()
    );
    """)

    # 8. Events
    op.execute("""
    CREATE TABLE events (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        account_id  uuid REFERENCES accounts(id) ON DELETE CASCADE,
        thread_id   text,
        event_type  text NOT NULL,
        payload     jsonb,
        created_at  timestamptz DEFAULT now()
    );
    """)

    # 9. Row-Level Security
    op.execute("""
    ALTER TABLE threads ENABLE ROW LEVEL SECURITY;
    ALTER TABLE turns ENABLE ROW LEVEL SECURITY;
    ALTER TABLE gaps ENABLE ROW LEVEL SECURITY;
    ALTER TABLE outbox ENABLE ROW LEVEL SECURITY;
    ALTER TABLE deleted_threads ENABLE ROW LEVEL SECURITY;
    ALTER TABLE turn_chunks ENABLE ROW LEVEL SECURITY;

    ALTER TABLE threads FORCE ROW LEVEL SECURITY;
    ALTER TABLE turns FORCE ROW LEVEL SECURITY;
    ALTER TABLE gaps FORCE ROW LEVEL SECURITY;
    ALTER TABLE outbox FORCE ROW LEVEL SECURITY;
    ALTER TABLE deleted_threads FORCE ROW LEVEL SECURITY;
    ALTER TABLE turn_chunks FORCE ROW LEVEL SECURITY;

    CREATE POLICY threads_account_isolation ON threads
      FOR ALL
      USING (account_id = nullif(current_setting('app.account_id', true), '')::uuid)
      WITH CHECK (account_id = nullif(current_setting('app.account_id', true), '')::uuid);

    CREATE POLICY turns_account_isolation ON turns
      FOR ALL
      USING (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid))
      WITH CHECK (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid));

    CREATE POLICY gaps_account_isolation ON gaps
      FOR ALL
      USING (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid))
      WITH CHECK (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid));

    CREATE POLICY outbox_account_isolation ON outbox
      FOR ALL
      USING (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid))
      WITH CHECK (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid));

    CREATE POLICY deleted_threads_account_isolation ON deleted_threads
      FOR ALL
      USING (account_id = nullif(current_setting('app.account_id', true), '')::uuid)
      WITH CHECK (account_id = nullif(current_setting('app.account_id', true), '')::uuid);

    CREATE POLICY turn_chunks_account_isolation ON turn_chunks
      FOR ALL
      USING (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid))
      WITH CHECK (thread_id IN (SELECT id FROM threads WHERE account_id = nullif(current_setting('app.account_id', true), '')::uuid));
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS events CASCADE;
    DROP TABLE IF EXISTS deleted_threads CASCADE;
    DROP TABLE IF EXISTS outbox CASCADE;
    DROP TABLE IF EXISTS turn_chunks CASCADE;
    DROP TABLE IF EXISTS gaps CASCADE;
    DROP TABLE IF EXISTS turns CASCADE;
    DROP TABLE IF EXISTS threads CASCADE;
    DROP TABLE IF EXISTS accounts CASCADE;
    """)
