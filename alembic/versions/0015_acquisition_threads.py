"""Inbox conversations attached to property deals: references only, no message text. Additive.

Revision ID: 0015_acquisition_threads
Revises: 0014_acquisitions
Create Date: 2026-11-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_acquisition_threads"
down_revision = "0014_acquisitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acquisition_threads",
        sa.Column("deal_id", sa.Integer(), sa.ForeignKey("acquisitions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("thread_sha256", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("thread_id", sa.String(500), nullable=False),
        sa.Column("participant_id", sa.String(500)),
        sa.Column("name", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_acquisition_threads_thread", "acquisition_threads", ["account_key", "thread_sha256"])


def downgrade() -> None:
    raise RuntimeError("Keep the additive conversation links when rolling application code back.")
