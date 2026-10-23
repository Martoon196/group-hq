"""Mentee earnings: the affiliate grant kind, what a product told us about a grant, and the mentorship's two new items.

- ck_entitlement_grants_kind gains "affiliate": a mentee enrolled in Property Predator's affiliate programme or Ordris's
  partner programme through the products' Group HQ routes (docs/mentee-earnings.md). Both programmes are public and
  membership is permanent, so revoking one is a recorded no-op, never a call.
- entitlement_grants.remote_info (JSONB, never a credential): an affiliate's code and link, the Property Predator
  account Key Properties confirmed on a sourcer, and a Hello New Home partner's area, links and admin page as the
  recompute last read them, so the "Needs a region" queue needs no live call.
- people.pp_account_id becomes VARCHAR(80). Property Predator's users.id is a UUID string, so the integer column it was
  created as (0001) could never hold a real account id: the recompute could not fill it and Key Properties was never
  sent it (contract 4). Any whole number already typed in is kept as its text; the unique constraint stays.
- A data step appends {pp affiliate} and {ordris affiliate} to the existing Mentorship template. Programmes are seeded
  once and then edited in the app, so an item is appended only when the template has no item of that product and kind
  already: a second run (or a re-upgrade) appends nothing, and an edited template keeps its edits. The change is an
  audit row like any other programme edit. A fresh database has no programmes when this runs; app.seed creates the
  Mentorship template with both items already in it.

downgrade() takes the affiliate items out of every template and deletes the affiliate grants (their connector calls
keep their history with grant_id set to null), because the constraint it restores refuses them; then drops the column.
It turns people.pp_account_id back into an integer: a whole number without a leading zero is kept, and any other id
(every real PP id) is cleared, with one audit row per person cleared so the id can be put back by hand. Nothing is
called in a product either way.

Revision ID: 0008_mentee_earnings
Revises: 0007_growth
Create Date: 2026-10-23
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_mentee_earnings"
down_revision = "0007_growth"
branch_labels = None
depends_on = None

GRANT_KINDS_BEFORE = ("comp_tier", "org_plan", "sourcer_link", "partner_seat", "flag")
GRANT_KINDS = GRANT_KINDS_BEFORE + ("affiliate",)          # mirrored in app/models.py
# What the Mentorship programme gains, in this order (mirrored by app.seed.PROGRAMMES for a fresh database).
AFFILIATE_ITEMS: tuple[dict, ...] = (
    {"product": "pp", "kind": "affiliate", "value": "affiliate"},
    {"product": "ordris", "kind": "affiliate", "value": "partner"},
)
TEMPLATE_CODE = "mentorship"
ACTOR = "migration:0008_mentee_earnings"

_programmes = sa.table(
    "programmes",
    sa.column("id", sa.Integer()),
    sa.column("code", sa.String(40)),
    sa.column("grant_template", postgresql.JSONB(astext_type=sa.Text())),
    sa.column("updated_at", sa.DateTime()),
)
_people = sa.table(
    "people",
    sa.column("id", sa.Integer()),
    sa.column("pp_account_id", sa.String(80)),
)
_audit_log = sa.table(
    "audit_log",
    sa.column("created_at", sa.DateTime()),
    sa.column("actor", sa.String(200)),
    sa.column("action", sa.String(80)),
    sa.column("entity", sa.String(200)),
    sa.column("before", postgresql.JSONB(astext_type=sa.Text())),
    sa.column("after", postgresql.JSONB(astext_type=sa.Text())),
)


def _in(column: str, values: tuple[str, ...]) -> str:
    """The condition of a vocabulary CheckConstraint, written as app.models._one_of writes it."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def with_affiliate_items(template) -> tuple[list, list]:
    """(the template with each missing affiliate item appended, the items appended). An item counts as present when the
    template already has an item of the same product and kind, whatever its value or extra keys, so a template the
    founder has edited is never given a second one. A template that is not a list is returned as it is."""
    if not isinstance(template, list):
        return template, []
    present = {(item.get("product"), item.get("kind")) for item in template if isinstance(item, dict)}
    added = [dict(item) for item in AFFILIATE_ITEMS if (item["product"], item["kind"]) not in present]
    return [*template, *added], added


def append_affiliate_items(conn) -> int:
    """The data step: append the missing affiliate items to the Mentorship template, with an audit row. Returns how
    many programmes changed (0 or 1)."""
    changed = 0
    rows = conn.execute(sa.select(_programmes.c.id, _programmes.c.grant_template)
                        .where(_programmes.c.code == TEMPLATE_CODE)).all()
    for programme_id, template in rows:
        new, added = with_affiliate_items(template)
        if not added:
            continue
        now = _now()
        conn.execute(_programmes.update().where(_programmes.c.id == programme_id)
                     .values(grant_template=new, updated_at=now))
        conn.execute(_audit_log.insert().values(
            created_at=now, actor=ACTOR, action="programme.update", entity=f"programme:{programme_id}",
            before={"grant_template": template}, after={"grant_template": new, "appended": added}))
        changed += 1
    return changed


def remove_affiliate_items(conn) -> int:
    """downgrade's data step: every template loses its affiliate items. Returns how many programmes changed."""
    changed = 0
    for programme_id, template in conn.execute(sa.select(_programmes.c.id, _programmes.c.grant_template)).all():
        if not isinstance(template, list):
            continue
        kept = [item for item in template if not (isinstance(item, dict) and item.get("kind") == "affiliate")]
        if len(kept) == len(template):
            continue
        conn.execute(_programmes.update().where(_programmes.c.id == programme_id).values(grant_template=kept, updated_at=_now()))
        changed += 1
    return changed


# What survives downgrade's return to an integer column: a whole number with no leading zero (so two ids can never
# become the same number) that fits a Postgres integer.
INTEGER_ID = r"^[1-9][0-9]{0,8}$"


def clear_non_integer_ids(conn) -> int:
    """downgrade's data step before the column becomes an integer again: clear every PP account id that is not a whole
    number, with an audit row for each, so the old column type can take what is left. Returns how many were cleared."""
    rows = conn.execute(sa.select(_people.c.id, _people.c.pp_account_id)
                        .where(_people.c.pp_account_id.isnot(None), sa.not_(_people.c.pp_account_id.op("~")(INTEGER_ID)))).all()
    for person_id, account in rows:
        conn.execute(_people.update().where(_people.c.id == person_id).values(pp_account_id=None))
        conn.execute(_audit_log.insert().values(
            created_at=_now(), actor=ACTOR, action="person.links", entity=f"person:{person_id}",
            before={"pp_account_id": account}, after={"pp_account_id": None, "via": "downgrade of 0008: not a whole number"}))
    return len(rows)


def upgrade() -> None:
    op.alter_column("people", "pp_account_id", existing_type=sa.Integer(), type_=sa.String(80), existing_nullable=True,
                    postgresql_using="pp_account_id::text")
    op.drop_constraint("ck_entitlement_grants_kind", "entitlement_grants", type_="check")
    op.create_check_constraint("ck_entitlement_grants_kind", "entitlement_grants", _in("kind", GRANT_KINDS))
    op.add_column("entitlement_grants", sa.Column("remote_info", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                                  server_default=sa.text("'{}'::jsonb")))
    append_affiliate_items(op.get_bind())


def downgrade() -> None:
    conn = op.get_bind()
    remove_affiliate_items(conn)
    conn.execute(sa.text("DELETE FROM entitlement_grants WHERE kind = 'affiliate'"))
    op.drop_column("entitlement_grants", "remote_info")
    op.drop_constraint("ck_entitlement_grants_kind", "entitlement_grants", type_="check")
    op.create_check_constraint("ck_entitlement_grants_kind", "entitlement_grants", _in("kind", GRANT_KINDS_BEFORE))
    clear_non_integer_ids(conn)
    op.alter_column("people", "pp_account_id", existing_type=sa.String(80), type_=sa.Integer(), existing_nullable=True,
                    postgresql_using="pp_account_id::integer")
