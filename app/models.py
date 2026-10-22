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
    # Pause all access (the mentee screen): while set, every grant the person holds is kept revoked by the
    # recompute, new ones included, until it is resumed (alembic/versions/0006_admin_switches.py).
    access_held_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    access_held_by: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    access_held_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

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
    # A manual hold (Pause on the grant): revoked in the product when it is set and kept revoked by the
    # recompute, whatever the payments say, until Resume clears it.
    held_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    held_by: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    held_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

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


class AppSetting(Base):
    """A switch flipped on the Settings screen, keyed by name, with its value as JSON: product_switch.pp holds
    "live" or "dry_run". Only what has been changed in the app lives here; everything else is a code default in
    app.services.settings. Every change is audited (alembic/versions/0006_admin_switches.py)."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | int | float | str | None] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(200), default="", nullable=False)


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


# =====================================================================================================
# Phase 4: territory model, franchise pipeline, seller commission (alembic/versions/0003_franchise.py).
#
# Money in these tables is an integer number of pence (quoted_upfront_pence, amount_pence) where the
# plan names it so, and Numeric(.., 2) pounds where the figure is a modelled estimate (achievable_fee,
# partner_net_y1). Every figure the model produces is a proposal until the founder confirms it: the
# assumptions table carries a `confirmed` flag per input, and the screens show a [CONFIRM] badge while
# it is false. Partners never see any of this; Group HQ is admin-only.
# =====================================================================================================

# ---- Phase 4 vocabularies (mirrored by the CheckConstraints in alembic/versions/0003_franchise.py) ----
NATIONS = ("england", "wales", "scotland", "northern_ireland")
BANDS = ("london", "major_city", "town", "rural")
DISTRICT_STATES = ("open", "reserved", "core", "hq")
# What the council adds on top of the mandatory HMO licensing that applies everywhere in England and Wales.
# Rent Smart Wales is implied by nation = wales and lives in licensing_note, not here.
LICENSING_SCHEMES = ("unknown", "mandatory_only", "additional", "selective", "selective_additional")
OPPORTUNITY_STAGES = ("enquiry", "discovery", "pack_sent", "follow_up", "agreement", "paid", "onboarding", "lost")
ACTIVITY_KINDS = ("call", "email", "note", "task_done")
TASK_STATES = ("open", "done", "skipped")
SELLER_KINDS = ("mentee", "franchisee", "outsider")
COMMISSION_BASES = ("upfront", "monthly")
FRANCHISE_PAYMENT_BASES = ("upfront", "monthly")


class ModelRun(Base):
    """One execution of scripts/load_territory_model.py: which files it read, the assumptions it used
    (a snapshot of the assumptions table as {key: value}) and how many district rows it wrote."""
    __tablename__ = "model_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    source_files: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)    # [{name, path, sha256, rows}, ...]
    assumptions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)     # {key: value} as used
    districts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    district_rows: Mapped[list["PostcodeDistrict"]] = relationship(back_populates="model_run")


class PostcodeDistrict(TimestampMixin, Base):
    """One UK postcode district (DH1, SP10, SE1): the unit of territory, with the public data behind it, the
    modelled economics for the band it falls in, and who holds it.

    Public inputs (loader, read-only): area and nation from the district itself; la_gss and la_name from the
    ONS Postcode Directory (majority local authority); dwellings from PP address points; prs_households and
    prs_share from Census 2021 TS054 apportioned by dwellings; rent_1bed..rent_3bed and rent_date from the
    ONS Price Index of Private Rents at local authority level. licensing and licensing_note are typed by hand.

    Modelled outputs (app.services.territory_rules): band, achievable_fee, cost_to_serve, units_y1..y3,
    partner_net_y1, hq_contribution_y1. All of them are proposals until the assumptions are confirmed.

    Holding: state is open (nobody), reserved (first refusal until reserved_until), core (exclusive) or hq
    (never offered). partner_id is the Hello New Home partner id (people.hnh_partner_id), not a foreign key:
    Group HQ never reads another app's tables."""
    __tablename__ = "postcode_districts"
    __table_args__ = (
        _one_of("nation", NATIONS, "ck_postcode_districts_nation"),
        _one_of("band", BANDS, "ck_postcode_districts_band"),
        _one_of("state", DISTRICT_STATES, "ck_postcode_districts_state"),
        _one_of("licensing", LICENSING_SCHEMES, "ck_postcode_districts_licensing"),
        CheckConstraint("dwellings IS NULL OR dwellings >= 0", name="ck_postcode_districts_dwellings"),
        CheckConstraint("prs_households IS NULL OR prs_households >= 0", name="ck_postcode_districts_prs_households"),
        CheckConstraint("prs_share IS NULL OR (prs_share >= 0 AND prs_share <= 1)", name="ck_postcode_districts_prs_share"),
        CheckConstraint("cost_share IS NULL OR (cost_share >= 0 AND cost_share <= 1)", name="ck_postcode_districts_cost_share"),
        Index("ix_postcode_districts_area", "area"),
        Index("ix_postcode_districts_state", "state"),
        Index("ix_postcode_districts_band", "band"),
        Index("ix_postcode_districts_partner_id", "partner_id"),
        Index("ix_postcode_districts_model_run_id", "model_run_id"),
    )
    district: Mapped[str] = mapped_column(String(4), primary_key=True)        # outward code, upper case: SA1, WC1A
    area: Mapped[str] = mapped_column(String(2), nullable=False)              # the letters: SA, WC
    nation: Mapped[str] = mapped_column(String(20), nullable=False)           # england | wales | scotland | northern_ireland
    la_gss: Mapped[str | None] = mapped_column(String(9), nullable=True)      # E06000023, W06000011
    la_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    dwellings: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prs_households: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prs_share: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)      # 0.0000 to 1.0000
    rent_1bed: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)      # pounds a month
    rent_2bed: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    rent_3bed: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    rent_date: Mapped[date | None] = mapped_column(Date, nullable=True)                 # the ONS reference month
    licensing: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    licensing_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    band: Mapped[str] = mapped_column(String(12), nullable=False)             # london | major_city | town | rural
    achievable_fee: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)      # min(cap, pct x rent_2bed)
    cost_to_serve: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)       # per unit per month
    units_y1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    units_y2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    units_y3: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_net_y1: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)     # pounds, the whole year
    hq_contribution_y1: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    # The share of its territory's fixed costs (portals, ads, letters) and of the one systems fee that the two
    # year-one figures above carry: units_y1 over the territory's units_y1 (alembic/versions/0005_district_cost_share.py).
    # NULL on a row loaded before that column existed, which the screens read as 1 (the whole area in one district).
    cost_share: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    state: Mapped[str] = mapped_column(String(10), default="open", nullable=False)     # open | reserved | core | hq
    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)             # HNH partner id, no FK
    reserved_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    model_run_id: Mapped[int | None] = mapped_column(ForeignKey("model_runs.id", ondelete="SET NULL"), nullable=True)

    model_run: Mapped[ModelRun | None] = relationship(back_populates="district_rows")


class Assumption(Base):
    """One input to the territory model, keyed by name, with its value as JSON (a number, usually) and the
    paragraph it came from in the note. `confirmed` is false until the founder signs the figure off; the
    screens badge every unconfirmed input [CONFIRM]. The defaults are seeded by the migration and listed in
    app.services.territory_rules.DEFAULT_ASSUMPTIONS; a value edited in the app is never overwritten."""
    __tablename__ = "assumptions"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict | list | int | float | str | None] = mapped_column(JSONB, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Seller(TimestampMixin, Base):
    """Someone who may earn commission for selling a territory: a mentee, an existing franchisee or an
    outsider. code is the tracked-link code (?seller=<code> on /partners-programme), unique case-insensitively.
    self_billing_agreed_at is when the HMRC self-billing agreement was signed; statements are not issued
    without it."""
    __tablename__ = "sellers"
    __table_args__ = (
        _one_of("kind", SELLER_KINDS, "ck_sellers_kind"),
        UniqueConstraint("person_id", name="uq_sellers_person_id"),
        Index("ux_sellers_code_ci", text("lower(code)"), unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)             # mentee | franchisee | outsider
    code: Mapped[str] = mapped_column(String(24), nullable=False)
    bank_reference: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    self_billing_agreed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    person: Mapped[Person] = relationship()
    opportunities: Mapped[list["FranchiseOpportunity"]] = relationship(back_populates="seller")
    entries: Mapped[list["CommissionEntry"]] = relationship(back_populates="seller")
    statements: Mapped[list["Statement"]] = relationship(back_populates="seller")


class CommissionRule(Base):
    """What a seller of a given kind earns, versioned: upfront_pct of the upfront fee, trail_pct of the monthly
    fee for trail_months months. A rule applies to opportunities whose payment clears on or after active_from;
    an entry keeps the rule it was earned under. Version 1 is the plan's proposal and is seeded with
    confirmed = false: nothing in a pack may quote it until the founder confirms it."""
    __tablename__ = "commission_rules"
    __table_args__ = (
        _one_of("seller_kind", SELLER_KINDS, "ck_commission_rules_seller_kind"),
        CheckConstraint("upfront_pct >= 0 AND upfront_pct <= 1", name="ck_commission_rules_upfront_pct"),
        CheckConstraint("trail_pct >= 0 AND trail_pct <= 1", name="ck_commission_rules_trail_pct"),
        CheckConstraint("trail_months >= 0", name="ck_commission_rules_trail_months"),
        UniqueConstraint("version", "seller_kind", name="uq_commission_rules_version_kind"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    seller_kind: Mapped[str] = mapped_column(String(12), nullable=False)
    upfront_pct: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0"), nullable=False)   # 0.2000 = 20%
    trail_pct: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0"), nullable=False)
    trail_months: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_from: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Statement(Base):
    """A seller's commission statement for one period: generated on the 10th for the previous month and paid by
    the 20th (the heads-of-terms rhythm). total_pence is the sum of the entries attached to it, clawbacks
    included, so it can go negative and carry forward."""
    __tablename__ = "statements"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_statements_period"),
        UniqueConstraint("seller_id", "period_start", "period_end", name="uq_statements_seller_period"),
        Index("ix_statements_seller_id", "seller_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="RESTRICT"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    total_pence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    seller: Mapped[Seller] = relationship(back_populates="statements")
    entries: Mapped[list["CommissionEntry"]] = relationship(back_populates="statement")


class FranchiseOpportunity(TimestampMixin, Base):
    """One prospective territory sale, from enquiry to paid and onboarding (or lost). districts is the list of
    district codes being discussed; band and the quoted fees are copied from the territory model when the
    quote is made, so a later model run never silently changes a quote. seller_id is who gets the
    commission; source is where it came from (web form, seller code, referral, event). stage_changed_at
    drives the follow-up tasks (day 2, 5, 9 and 14 after pack_sent)."""
    __tablename__ = "franchise_opportunities"
    __table_args__ = (
        _one_of("stage", OPPORTUNITY_STAGES, "ck_franchise_opportunities_stage"),
        _one_of("band", BANDS, "ck_franchise_opportunities_band"),
        CheckConstraint("quoted_upfront_pence IS NULL OR quoted_upfront_pence >= 0", name="ck_franchise_opportunities_upfront"),
        CheckConstraint("quoted_monthly_pence IS NULL OR quoted_monthly_pence >= 0", name="ck_franchise_opportunities_monthly"),
        Index("ix_franchise_opportunities_person_id", "person_id"),
        Index("ix_franchise_opportunities_stage", "stage"),
        Index("ix_franchise_opportunities_seller_id", "seller_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    stage: Mapped[str] = mapped_column(String(12), default="enquiry", nullable=False)
    districts: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)       # ["SP10", "SP11"]
    band: Mapped[str | None] = mapped_column(String(12), nullable=True)
    quoted_upfront_pence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quoted_monthly_pence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stage_changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    person: Mapped[Person] = relationship()
    seller: Mapped[Seller | None] = relationship(back_populates="opportunities")
    activities: Mapped[list["Activity"]] = relationship(back_populates="opportunity", order_by="Activity.at")
    tasks: Mapped[list["Task"]] = relationship(back_populates="opportunity", order_by="Task.due_on")
    commission_entries: Mapped[list["CommissionEntry"]] = relationship(back_populates="opportunity")


class Activity(Base):
    """What happened on an opportunity: a call, an email, a note or a task marked done. actor is an email or a
    job name, as in audit_log."""
    __tablename__ = "activities"
    __table_args__ = (
        _one_of("kind", ACTIVITY_KINDS, "ck_activities_kind"),
        Index("ix_activities_opportunity_id", "opportunity_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("franchise_opportunities.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)             # call | email | note | task_done
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    opportunity: Mapped[FranchiseOpportunity] = relationship(back_populates="activities")


class Task(Base):
    """A follow-up to do on an opportunity, created from a template (template_key, for example followup_day2)
    when the stage changes. open until it is done or skipped."""
    __tablename__ = "tasks"
    __table_args__ = (
        _one_of("state", TASK_STATES, "ck_tasks_state"),
        Index("ix_tasks_opportunity_id", "opportunity_id"),
        Index("ix_tasks_state_due_on", "state", "due_on"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("franchise_opportunities.id", ondelete="CASCADE"), nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    template_key: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    state: Mapped[str] = mapped_column(String(10), default="open", nullable=False)     # open | done | skipped
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    opportunity: Mapped[FranchiseOpportunity] = relationship(back_populates="tasks")


class CommissionEntry(Base):
    """One line of commission: amount_pence = basis_pence x the rule's percentage, earned on a cleared payment
    (upfront) or one month of the monthly fee (trail_month 1..trail_months). An entry exists only once the
    payment behind it has cleared_at set; cleared_at here is when the entry became payable and paid_at when
    the statement it sits on was paid. A clawback is a second row with a negative amount whose clawback_of
    points at the original. At most one live entry per opportunity, seller, basis and trail month."""
    __tablename__ = "commission_entries"
    __table_args__ = (
        _one_of("basis", COMMISSION_BASES, "ck_commission_entries_basis"),
        CheckConstraint("basis_pence >= 0", name="ck_commission_entries_basis_pence"),
        CheckConstraint("(clawback_of IS NULL AND amount_pence >= 0) OR (clawback_of IS NOT NULL AND amount_pence <= 0)",
                        name="ck_commission_entries_clawback_sign"),
        # The IS NOT NULL is needed: a CHECK that evaluates to NULL passes, so "NULL >= 1" would let a monthly
        # entry through with no month.
        CheckConstraint("(basis = 'upfront' AND trail_month IS NULL) OR "
                        "(basis = 'monthly' AND trail_month IS NOT NULL AND trail_month >= 1)",
                        name="ck_commission_entries_trail_month"),
        Index("ix_commission_entries_opportunity_id", "opportunity_id"),
        Index("ix_commission_entries_seller_id", "seller_id"),
        Index("ix_commission_entries_statement_id", "statement_id"),
        Index("ux_commission_entries_once", "opportunity_id", "seller_id", "basis", text("coalesce(trail_month, 0)"),
              unique=True, postgresql_where=text("clawback_of IS NULL")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("franchise_opportunities.id", ondelete="RESTRICT"), nullable=False)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="RESTRICT"), nullable=False)
    rule_id: Mapped[int] = mapped_column(ForeignKey("commission_rules.id", ondelete="RESTRICT"), nullable=False)
    basis: Mapped[str] = mapped_column(String(10), nullable=False)            # upfront | monthly
    trail_month: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 1..trail_months for monthly, else NULL
    basis_pence: Mapped[int] = mapped_column(Integer, nullable=False)         # the fee the percentage applies to
    amount_pence: Mapped[int] = mapped_column(Integer, nullable=False)        # negative on a clawback
    earned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clawback_of: Mapped[int | None] = mapped_column(ForeignKey("commission_entries.id", ondelete="SET NULL"), nullable=True)
    statement_id: Mapped[int | None] = mapped_column(ForeignKey("statements.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    opportunity: Mapped[FranchiseOpportunity] = relationship(back_populates="commission_entries")
    seller: Mapped[Seller] = relationship(back_populates="entries")
    rule: Mapped[CommissionRule] = relationship()
    statement: Mapped[Statement | None] = relationship(back_populates="entries")
    clawback_source: Mapped["CommissionEntry | None"] = relationship(remote_side="CommissionEntry.id")


class FranchisePayment(TimestampMixin, Base):
    """A fee received for a territory: the upfront fee or one month of the monthly fee (alembic/versions/
    0004_franchise_payments.py). The payments table of Phase 1 belongs to a mentee, and a territory buyer need
    not be one, so a territory's money has its own table. Counts only once cleared_at is set, which is when the
    seller commission ledger accrues on it; refunded_at marks a refund (a clawback inside 90 days hangs off it,
    docs/commission-rules.md section 4). Pence, as the opportunity's quote is. At most one live upfront row and
    one live row per trail month per opportunity."""
    __tablename__ = "franchise_payments"
    __table_args__ = (
        _one_of("basis", FRANCHISE_PAYMENT_BASES, "ck_franchise_payments_basis"),
        _one_of("method", PAYMENT_METHODS, "ck_franchise_payments_method"),
        CheckConstraint("amount_pence >= 0", name="ck_franchise_payments_amount"),
        CheckConstraint("(basis = 'upfront' AND trail_month IS NULL) OR "
                        "(basis = 'monthly' AND trail_month IS NOT NULL AND trail_month >= 1)",
                        name="ck_franchise_payments_trail_month"),
        Index("ix_franchise_payments_opportunity_id", "opportunity_id"),
        Index("ux_franchise_payments_upfront_once", "opportunity_id", unique=True,
              postgresql_where=text("basis = 'upfront' AND refunded_at IS NULL")),
        Index("ux_franchise_payments_month_once", "opportunity_id", "trail_month", unique=True,
              postgresql_where=text("basis = 'monthly' AND refunded_at IS NULL")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("franchise_opportunities.id", ondelete="RESTRICT"), nullable=False)
    basis: Mapped[str] = mapped_column(String(10), nullable=False)             # upfront | monthly
    trail_month: Mapped[int | None] = mapped_column(Integer, nullable=True)    # 1.. for monthly, else NULL
    amount_pence: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    method: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    reference: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    opportunity: Mapped[FranchiseOpportunity] = relationship()
