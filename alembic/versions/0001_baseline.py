"""Baseline: people, organisations, person_organisations, programmes, mentees, payments, users, audit_log,
entitlement_grants, connector_calls (the Group HQ plan, Phase 1).

Revision ID: 0001_baseline
Revises: None
Create Date: 2026-10-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

PRODUCTS = ("pp", "ordris", "kp", "hnh")
GRANT_KINDS = ("comp_tier", "org_plan", "sourcer_link", "partner_seat", "flag")
GRANT_STATES = ("pending", "active", "revoked", "failed")
PAYMENT_MODES = ("full", "monthly")
PAYMENT_METHODS = ("manual", "stripe_link", "invoice")
MENTEE_STATUSES = ("active", "lapsed", "ended")
CALL_STATUSES = ("pending", "succeeded", "failed")


def _one_of(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_key", sa.String(254), nullable=False),
        sa.Column("pp_account_id", sa.Integer(), nullable=True),
        sa.Column("ordris_user_id", sa.String(64), nullable=True),
        sa.Column("ordris_org_id", sa.String(64), nullable=True),
        sa.Column("kp_user_id", sa.Integer(), nullable=True),
        sa.Column("kp_investor_id", sa.Integer(), nullable=True),
        sa.Column("hnh_partner_id", sa.Integer(), nullable=True),
        sa.Column("hnh_user_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(120), nullable=False, server_default=""),
        sa.Column("phone", sa.String(40), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.UniqueConstraint("pp_account_id", name="uq_people_pp_account_id"),
    )
    op.create_index("ux_people_person_key_ci", "people", [sa.text("lower(person_key)")], unique=True)

    op.create_table(
        "organisations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("companies_house_number", sa.String(16), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        sa.UniqueConstraint("companies_house_number", name="uq_organisations_companies_house_number"),
    )

    op.create_table(
        "person_organisations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organisation_id", sa.Integer(), sa.ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(40), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("person_id", "organisation_id", name="uq_person_organisations_pair"),
    )

    op.create_table(
        "programmes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("grant_template", _jsonb(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("code", name="uq_programmes_code"),
    )

    op.create_table(
        "mentees",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("programme_id", sa.Integer(), sa.ForeignKey("programmes.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("payment_mode", sa.String(10), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("cohort", sa.String(60), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        _one_of("payment_mode", PAYMENT_MODES, "ck_mentees_payment_mode"),
        _one_of("status", MENTEE_STATUSES, "ck_mentees_status"),
    )
    op.create_index("ix_mentees_person_id", "mentees", ["person_id"])
    op.create_index("ix_mentees_programme_id", "mentees", ["programme_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mentee_id", sa.Integer(), sa.ForeignKey("mentees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("method", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("reference", sa.String(120), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        _one_of("method", PAYMENT_METHODS, "ck_payments_method"),
        sa.CheckConstraint("amount >= 0", name="ck_payments_amount_positive"),
    )
    op.create_index("ix_payments_mentee_id", "payments", ["mentee_id"])

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(300), nullable=False),
        sa.Column("totp_secret", sa.String(64), nullable=True),
        sa.Column("totp_last_used", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ux_users_email_ci", "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor", sa.String(200), nullable=False, server_default="system"),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity", sa.String(200), nullable=False, server_default=""),
        sa.Column("before", _jsonb(), nullable=True),
        sa.Column("after", _jsonb(), nullable=True),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity"])

    op.create_table(
        "entitlement_grants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("mentee_id", sa.Integer(), sa.ForeignKey("mentees.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("value", sa.String(120), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("source", sa.String(60), nullable=False, server_default=""),
        sa.Column("remote_ref", sa.String(200), nullable=True),
        sa.Column("state", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        *_timestamps(),
        _one_of("product", PRODUCTS, "ck_entitlement_grants_product"),
        _one_of("kind", GRANT_KINDS, "ck_entitlement_grants_kind"),
        _one_of("state", GRANT_STATES, "ck_entitlement_grants_state"),
    )
    op.create_index("ix_entitlement_grants_person_product_kind", "entitlement_grants", ["person_id", "product", "kind"])
    op.create_index("ix_entitlement_grants_mentee_id", "entitlement_grants", ["mentee_id"])
    op.create_index("ix_entitlement_grants_state", "entitlement_grants", ["state"])

    op.create_table(
        "connector_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("grant_id", sa.Integer(), sa.ForeignKey("entitlement_grants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product", sa.String(10), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("response", _jsonb(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        _one_of("product", PRODUCTS, "ck_connector_calls_product"),
        _one_of("status", CALL_STATUSES, "ck_connector_calls_status"),
        sa.UniqueConstraint("idempotency_key", name="uq_connector_calls_idempotency_key"),
    )
    op.create_index("ix_connector_calls_status", "connector_calls", ["status"])
    op.create_index("ix_connector_calls_grant_id", "connector_calls", ["grant_id"])


def downgrade() -> None:
    op.drop_table("connector_calls")
    op.drop_table("entitlement_grants")
    op.drop_table("audit_log")
    op.drop_table("users")
    op.drop_table("payments")
    op.drop_table("mentees")
    op.drop_table("programmes")
    op.drop_table("person_organisations")
    op.drop_table("organisations")
    op.drop_table("people")
