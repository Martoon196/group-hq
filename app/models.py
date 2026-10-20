"""Database models: exactly the tables in the Group HQ plan (Phase 1).

Conventions: integer ids; created_at/updated_at are naive UTC; every enumerated column carries a
CheckConstraint; emails and person keys are unique case-insensitively (functional index on lower()).
The schema is applied by Alembic, so a change here needs a migration in alembic/versions.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# ---- vocabularies (mirrored by the CheckConstraints in alembic/versions/0001_baseline.py) ----
PRODUCTS = ("pp", "ordris", "kp", "hnh")
GRANT_KINDS = ("comp_tier", "org_plan", "sourcer_link", "partner_seat", "flag")
GRANT_STATES = ("pending", "active", "revoked", "failed")
PAYMENT_MODES = ("full", "monthly")
PAYMENT_METHODS = ("manual", "stripe_link", "invoice")
MENTEE_STATUSES = ("active", "lapsed", "ended")
CALL_STATUSES = ("pending", "succeeded", "failed")
PROGRAMME_CODES = ("mentorship", "hnh_franchise", "kp_mentee", "ordris_pro_bundle")


def utcnow() -> datetime:
    """Naive UTC, the only timestamp convention in this database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Person(TimestampMixin, Base):
    """One human across the four products. person_key is the lower-cased, trimmed email."""
    __tablename__ = "people"
    __table_args__ = (
        UniqueConstraint("pp_account_id", name="uq_people_pp_account_id"),
        Index("ux_people_person_key_ci", text("lower(person_key)"), unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_key: Mapped[str] = mapped_column(String(254), nullable=False)
    pp_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ordris_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)   # Clerk user id
    ordris_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)    # Clerk organisation id
    kp_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kp_investor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hnh_partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hnh_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    mentees: Mapped[list["Mentee"]] = relationship(back_populates="person")
    grants: Mapped[list["EntitlementGrant"]] = relationship(back_populates="person")
    organisation_links: Mapped[list["PersonOrganisation"]] = relationship(back_populates="person")


class Organisation(TimestampMixin, Base):
    __tablename__ = "organisations"
    __table_args__ = (UniqueConstraint("companies_house_number", name="uq_organisations_companies_house_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    companies_house_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    person_links: Mapped[list["PersonOrganisation"]] = relationship(back_populates="organisation")


class PersonOrganisation(Base):
    __tablename__ = "person_organisations"
    __table_args__ = (UniqueConstraint("person_id", "organisation_id", name="uq_person_organisations_pair"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="", nullable=False)      # director, owner, staff...
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    person: Mapped[Person] = relationship(back_populates="organisation_links")
    organisation: Mapped[Organisation] = relationship(back_populates="person_links")


class Programme(TimestampMixin, Base):
    """A package someone can be enrolled on. grant_template lists {product, kind, value[, months]} items;
    an item without months follows the mentee's computed end date."""
    __tablename__ = "programmes"
    __table_args__ = (UniqueConstraint("code", name="uq_programmes_code"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    grant_template: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # What the programme costs. A full-mode mentee has "a cleared payment covering the programme" only once
    # the cleared total reaches this; while it is unset any cleared payment counts.
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    mentees: Mapped[list["Mentee"]] = relationship(back_populates="programme")


class Mentee(TimestampMixin, Base):
    __tablename__ = "mentees"
    __table_args__ = (
        _one_of("payment_mode", PAYMENT_MODES, "ck_mentees_payment_mode"),
        _one_of("status", MENTEE_STATUSES, "ck_mentees_status"),
        Index("ix_mentees_person_id", "person_id"),
        Index("ix_mentees_programme_id", "programme_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    programme_id: Mapped[int] = mapped_column(ForeignKey("programmes.id", ondelete="RESTRICT"), nullable=False)
    payment_mode: Mapped[str] = mapped_column(String(10), nullable=False)          # full | monthly
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)   # active | lapsed | ended
    cohort: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    person: Mapped[Person] = relationship(back_populates="mentees")
    programme: Mapped[Programme] = relationship(back_populates="mentees")
    payments: Mapped[list["Payment"]] = relationship(back_populates="mentee", order_by="Payment.paid_at")
    grants: Mapped[list["EntitlementGrant"]] = relationship(back_populates="mentee")


class Payment(TimestampMixin, Base):
    """Money received for a mentee. Only a cleared payment counts towards an entitlement."""
    __tablename__ = "payments"
    __table_args__ = (
        _one_of("method", PAYMENT_METHODS, "ck_payments_method"),
        CheckConstraint("amount >= 0", name="ck_payments_amount_positive"),
        Index("ix_payments_mentee_id", "mentee_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mentee_id: Mapped[int] = mapped_column(ForeignKey("mentees.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    method: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)  # manual | stripe_link | invoice
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    reference: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    mentee: Mapped[Mentee] = relationship(back_populates="payments")


class EntitlementGrant(TimestampMixin, Base):
    """One right in one product for one person, with the window Group HQ believes it holds remotely."""
    __tablename__ = "entitlement_grants"
    __table_args__ = (
        _one_of("product", PRODUCTS, "ck_entitlement_grants_product"),
        _one_of("kind", GRANT_KINDS, "ck_entitlement_grants_kind"),
        _one_of("state", GRANT_STATES, "ck_entitlement_grants_state"),
        Index("ix_entitlement_grants_person_product_kind", "person_id", "product", "kind"),
        Index("ix_entitlement_grants_mentee_id", "mentee_id"),
        Index("ix_entitlement_grants_state", "state"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    mentee_id: Mapped[int | None] = mapped_column(ForeignKey("mentees.id", ondelete="SET NULL"), nullable=True)
    product: Mapped[str] = mapped_column(String(10), nullable=False)        # pp | ordris | kp | hnh
    kind: Mapped[str] = mapped_column(String(20), nullable=False)           # comp_tier | org_plan | sourcer_link | partner_seat | flag
    value: Mapped[str] = mapped_column(String(120), nullable=False)         # e.g. pro_buyer, starter, area, mentee, kpuk.submit
    starts_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    source: Mapped[str] = mapped_column(String(60), default="", nullable=False)   # programme:<code> | manual | ...
    remote_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)   # the product's id for this grant
    state: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)  # pending | active | revoked | failed
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    person: Mapped[Person] = relationship(back_populates="grants")
    mentee: Mapped[Mentee | None] = relationship(back_populates="grants")
    calls: Mapped[list["ConnectorCall"]] = relationship(back_populates="grant")


class ConnectorCall(TimestampMixin, Base):
    """Outbox row for one call to a product's server-to-server API. The idempotency key
    (for example grant:<id>:<ends_at>) is what stops a retry from granting twice."""
    __tablename__ = "connector_calls"
    __table_args__ = (
        _one_of("product", PRODUCTS, "ck_connector_calls_product"),
        _one_of("status", CALL_STATUSES, "ck_connector_calls_status"),
        UniqueConstraint("idempotency_key", name="uq_connector_calls_idempotency_key"),
        Index("ix_connector_calls_status", "status"),
        Index("ix_connector_calls_grant_id", "grant_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grant_id: Mapped[int | None] = mapped_column(ForeignKey("entitlement_grants.id", ondelete="SET NULL"), nullable=True)
    product: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)          # grant | revoke | lookup | ...
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)   # pending | succeeded | failed
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    grant: Mapped[EntitlementGrant | None] = relationship(back_populates="calls")


class AuditLog(Base):
    """Who changed what. before/after hold the changed fields as JSON; actor is an email or a job name."""
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity", "entity"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor: Mapped[str] = mapped_column(String(200), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity: Mapped[str] = mapped_column(String(200), default="", nullable=False)   # e.g. mentee:12
    before: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)

    user: Mapped["User | None"] = relationship()


class User(TimestampMixin, Base):
    """An admin login. Every user is an administrator; there is no other role yet."""
    __tablename__ = "users"
    __table_args__ = (Index("ux_users_email_ci", text("lower(email)"), unique=True),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)     # set only after a valid first code
    totp_last_used: Mapped[int | None] = mapped_column(Integer, nullable=True)     # last accepted 30-second counter
    totp_pending_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)   # offered at enrolment, not yet accepted
    totp_pending_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginAttempt(Base):
    """One failed password or authenticator attempt, so the login throttle is durable across deploys and
    instances. key is "ip:<address>", "<address>:<email>" or "acct:<email>"; rows older than the window
    are deleted as new ones are written."""
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_key_attempted_at", "key", "attempted_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(320), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
