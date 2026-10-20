"""Review fixes: a durable login throttle, a programme price, and a server-side enrolment secret.

- login_attempts: every failed password or authenticator attempt, keyed by address, address+email and
  account, so the throttle survives a deploy and a second instance.
- programmes.price: what the programme costs; a full-mode mentee only has "a cleared payment covering the
  programme" once the cleared total reaches it. Nullable: without a price any cleared payment counts.
- users.totp_pending_secret / totp_pending_at: the authenticator secret offered during enrolment is kept
  here until a code made from it is accepted, instead of travelling in the pending cookie.

Revision ID: 0002_review_fixes
Revises: 0001_baseline
Create Date: 2026-10-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_review_fixes"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(320), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_login_attempts_key_attempted_at", "login_attempts", ["key", "attempted_at"])
    op.add_column("programmes", sa.Column("price", sa.Numeric(10, 2), nullable=True))
    op.add_column("users", sa.Column("totp_pending_secret", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("totp_pending_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "totp_pending_at")
    op.drop_column("users", "totp_pending_secret")
    op.drop_column("programmes", "price")
    op.drop_index("ix_login_attempts_key_attempted_at", table_name="login_attempts")
    op.drop_table("login_attempts")
