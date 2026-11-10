"""Operator API OAuth state for claude.ai connectors (docs/operator-api.md). Additive: three new tables.

Only SHA-256 hashes of codes, tokens, client ids and redirect URIs are stored. An authorisation code is redeemed
once across every instance (the redemption locks its row; a replay revokes the grant it created). Refresh tokens are
bound to their client and rotated on every use; reuse of a rotated token, or presentation by another client, revokes
the grant. Access tokens are checked against this table on every call, so one connection can be revoked alone.

Revision ID: 0017_operator_oauth
Revises: 0016_laps_all_brands
Create Date: 2026-11-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_operator_oauth"
down_revision = "0016_laps_all_brands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operator_oauth_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("client_id_hash", sa.LargeBinary(), nullable=False),
        sa.Column("client_name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.Column("revoked_reason", sa.String(20)),
        sa.CheckConstraint("octet_length(client_id_hash) = 32", name="ck_operator_oauth_grants_client_hash"),
        sa.CheckConstraint("revoked_reason IS NULL OR revoked_reason IN ('owner', 'client', 'code_replay', 'refresh_reuse', 'client_mismatch')",
                           name="ck_operator_oauth_grants_reason"),
        sa.CheckConstraint("(revoked_at IS NULL) = (revoked_reason IS NULL)", name="ck_operator_oauth_grants_revoked"),
    )
    op.create_index("ix_operator_oauth_grants_user", "operator_oauth_grants", ["user_id"])
    op.create_table(
        "operator_oauth_codes",
        sa.Column("code_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("client_id_hash", sa.LargeBinary(), nullable=False),
        sa.Column("client_name", sa.String(100), nullable=False),
        sa.Column("redirect_uri_hash", sa.LargeBinary(), nullable=False),
        sa.Column("code_challenge", sa.String(43), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("grant_id", sa.Integer(), sa.ForeignKey("operator_oauth_grants.id")),
        sa.CheckConstraint("octet_length(code_hash) = 32 AND octet_length(client_id_hash) = 32 AND octet_length(redirect_uri_hash) = 32",
                           name="ck_operator_oauth_codes_hashes"),
        sa.CheckConstraint("code_challenge ~ '^[A-Za-z0-9_-]{43}$'", name="ck_operator_oauth_codes_challenge"),
        sa.CheckConstraint("expires_at > created_at AND expires_at <= created_at + interval '10 minutes'", name="ck_operator_oauth_codes_expiry"),
        sa.CheckConstraint("(consumed_at IS NULL) = (grant_id IS NULL)", name="ck_operator_oauth_codes_consumed"),
    )
    op.create_index("ix_operator_oauth_codes_expires", "operator_oauth_codes", ["expires_at"])
    op.create_table(
        "operator_oauth_tokens",
        sa.Column("token_hash", sa.LargeBinary(), primary_key=True),
        sa.Column("grant_id", sa.Integer(), sa.ForeignKey("operator_oauth_grants.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("rotated_at", sa.DateTime()),
        sa.CheckConstraint("octet_length(token_hash) = 32", name="ck_operator_oauth_tokens_hash"),
        sa.CheckConstraint("kind IN ('access', 'refresh')", name="ck_operator_oauth_tokens_kind"),
        sa.CheckConstraint("kind = 'refresh' OR rotated_at IS NULL", name="ck_operator_oauth_tokens_rotation"),
        sa.CheckConstraint("expires_at > created_at", name="ck_operator_oauth_tokens_expiry"),
    )
    op.create_index("ix_operator_oauth_tokens_grant", "operator_oauth_tokens", ["grant_id"])
    op.create_index("ix_operator_oauth_tokens_expires", "operator_oauth_tokens", ["expires_at"])


def downgrade() -> None:
    raise RuntimeError("Keep the operator OAuth tables when rolling application code back; the recovery build "
                       "(main plus this migration file) runs on them unchanged.")
