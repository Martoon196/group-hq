"""Idempotent reference data: the four programmes, the growth sources' health rows and the first admin user. Safe to
run on every boot.

The schema itself comes from Alembic (app.migrate), never from here.
"""
from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from . import config
from .auth import find_user_by_email, generate_password, hash_password
from .db import SessionLocal
from .models import SOURCES, MetricSource, Programme, User
from .services import territory_rules

# Package table from the plan (Phase 2). Items are {product, kind, value} and may carry "months" when the
# item runs for a fixed term rather than following the mentee's computed end date. Existing rows are
# never overwritten: edit a template in the app, not here.
PROGRAMMES: list[dict] = [
    {
        "code": "mentorship",
        "name": "Mentorship",
        "description": ("Paid in full gives 12 months; monthly gives access while paying, with 7 days of grace "
                        "after each period ends. The HNH partner seat is fee-waived while the mentorship is active "
                        "and the KP sourcer gets the KP forwarding right as a separate flag beside the PP tier."),
        "grant_template": [
            {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
            {"product": "ordris", "kind": "org_plan", "value": "starter"},
            {"product": "hnh", "kind": "partner_seat", "value": "area"},
            {"product": "kp", "kind": "sourcer_link", "value": "mentee"},
            {"product": "pp", "kind": "flag", "value": "kpuk.submit"},
        ],
    },
    {
        "code": "hnh_franchise",
        "name": "HNH franchise purchase",
        "description": ("A paid Hello New Home territory. Ordris Starter for 3 months (the HNH50 coupon is the "
                        "alternative once it exists in the Ordris Stripe account) and PP Hunter for 3 months."),
        "grant_template": [
            {"product": "hnh", "kind": "partner_seat", "value": "area"},
            {"product": "ordris", "kind": "org_plan", "value": "starter", "months": 3},
            {"product": "pp", "kind": "comp_tier", "value": "pro_buyer", "months": 3},
        ],
    },
    {
        "code": "kp_mentee",
        "name": "KP mentee",
        "description": "Key Properties sourcer (source mentee) plus PP Hunter for 12 months.",
        "grant_template": [
            {"product": "kp", "kind": "sourcer_link", "value": "mentee"},
            {"product": "pp", "kind": "comp_tier", "value": "pro_buyer", "months": 12},
        ],
    },
    {
        "code": "ordris_pro_bundle",
        "name": "Ordris Pro bundle",
        "description": ("Marks the person as an active Ordris customer in PP. The PP Stripe coupon (50% for 3 "
                        "months, from ordris_discount_from_pp_pct and _months) is applied by hand until its id is "
                        "confirmed and recorded here."),
        "grant_template": [
            {"product": "pp", "kind": "flag", "value": "ordris_active"},
        ],
    },
]


def ensure_programmes(db: Session) -> int:
    existing = {p.code for p in db.query(Programme).all()}
    added = 0
    for raw in PROGRAMMES:
        if raw["code"] in existing:
            continue
        db.add(Programme(code=raw["code"], name=raw["name"], description=raw["description"],
                         grant_template=raw["grant_template"], active=True))
        added += 1
    return added


def ensure_metric_sources(db: Session) -> int:
    """One metric_sources row per growth source (models.SOURCES), created once and never overwritten. Built sources
    start "off" (every growth switch starts Off); the ones not built yet, and Hello New Home's Stripe account, which
    does not exist yet, start "not_configured", with what they wait on in config.note. The switch itself is the
    app_settings row growth_source.<source>, never this row."""
    from .services.growth.collectors import COLLECTORS, SOURCE_INFO, env_names

    existing = {row.source for row in db.query(MetricSource).all()}
    added = 0
    for source in SOURCES:
        if source in existing:
            continue
        info = SOURCE_INFO[source]
        note = info.get("waiting") or ("Hello New Home 2 Ltd has no Stripe account yet." if source == "stripe_hnh" else "")
        status = "off" if source in COLLECTORS and source != "stripe_hnh" else "not_configured"
        db.add(MetricSource(source=source, status=status, tier=info["tier"], credential_env=list(env_names(source)),
                            config={"note": note} if note else {}, updated_by="seed"))
        added += 1
    return added


def ensure_admin(db: Session, *, quiet: bool = False) -> dict:
    """Create the admin named by ADMIN_EMAIL when no login with that address exists. ADMIN_PASSWORD is used only
    at creation; an empty one is generated and printed once to the log. Either way the password must be changed
    at the first login, so a value that lived in a deploy log or in the Render environment never stays the
    working password. Clear ADMIN_PASSWORD from the environment after the first boot."""
    report = {"admin_created": False, "admin_password": None}
    if not config.ADMIN_EMAIL:
        if not quiet and db.query(User).count() == 0:
            print("[seed] ADMIN_EMAIL is not set and there are no users: nobody can log in.", file=sys.stderr)
        return report
    if find_user_by_email(db, config.ADMIN_EMAIL) is not None:
        return report
    password = config.ADMIN_PASSWORD or generate_password(14)
    db.add(User(email=config.ADMIN_EMAIL, password_hash=hash_password(password), is_active=True,
                must_change_password=True))
    report["admin_created"] = True
    report["admin_password"] = password
    if not quiet:
        # Only a generated password is printed; one supplied through ADMIN_PASSWORD never goes to the log.
        shown = "(from ADMIN_PASSWORD)" if config.ADMIN_PASSWORD else password
        print(f"[seed] Admin account created: {config.ADMIN_EMAIL}  password: {shown}  "
              "(must be changed at the first login; clear ADMIN_PASSWORD from the environment now)", file=sys.stderr)
    return report


def seed(db: Session | None = None, *, quiet: bool = False) -> dict:
    own = db is None
    db = db or SessionLocal()
    try:
        report: dict = {"programmes_added": ensure_programmes(db)}
        # Territory-model assumptions and commission rule version 1 (both unconfirmed proposals until the founder
        # presses Confirm): idempotent, never overwrite, so a database that predates a new key gets it at boot.
        report["assumptions_added"] = territory_rules.ensure_assumptions(db)
        report["commission_rules_added"] = territory_rules.ensure_commission_rules(db)
        report["metric_sources_added"] = ensure_metric_sources(db)
        report.update(ensure_admin(db, quiet=quiet))
        db.commit()
        return report
    finally:
        if own:
            db.close()


if __name__ == "__main__":
    print(seed())
