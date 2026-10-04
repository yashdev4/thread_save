"""add_oauth_persistence_tables

Revision ID: 7a1b2c3d4e5f
Revises: 35a36273444e
Create Date: 2026-10-04 22:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a1b2c3d4e5f'
down_revision: Union[str, Sequence[str], None] = '35a36273444e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Accounts email column if not exists
    op.execute("""
    ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email text DEFAULT NULL;
    """)

    # 2. OAuth Clients table
    op.execute("""
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id                   text PRIMARY KEY,
        client_secret               text,
        client_name                 text NOT NULL,
        redirect_uris               jsonb NOT NULL,
        grant_types                 jsonb NOT NULL,
        response_types              jsonb NOT NULL,
        token_endpoint_auth_method  text NOT NULL DEFAULT 'none',
        created_at                  timestamptz DEFAULT now()
    );
    """)

    # 3. OAuth Auth Codes table
    op.execute("""
    CREATE TABLE IF NOT EXISTS oauth_auth_codes (
        code                    text PRIMARY KEY,
        client_id               text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
        redirect_uri            text NOT NULL,
        code_challenge          text NOT NULL,
        code_challenge_method   text NOT NULL,
        account_id              text NOT NULL,
        scope                   text NOT NULL,
        expires_at              timestamptz NOT NULL,
        created_at              timestamptz DEFAULT now()
    );
    """)

    # 4. OAuth Refresh Tokens table
    op.execute("""
    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
        token                   text PRIMARY KEY,
        client_id               text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
        account_id              text NOT NULL,
        scope                   text NOT NULL,
        revoked                 boolean NOT NULL DEFAULT false,
        expires_at              timestamptz NOT NULL,
        created_at              timestamptz DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_account ON oauth_refresh_tokens(account_id);
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS oauth_refresh_tokens CASCADE;
    DROP TABLE IF EXISTS oauth_auth_codes CASCADE;
    DROP TABLE IF EXISTS oauth_clients CASCADE;
    ALTER TABLE accounts DROP COLUMN IF EXISTS email;
    """)
