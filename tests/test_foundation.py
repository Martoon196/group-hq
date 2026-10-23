"""The schema is what Alembic says it is: migrated to head, programmes seeded idempotently, enumerated
columns enforced by the database, emails and person keys unique case-insensitively, audit rows
written with JSON snapshots."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app import migrate, seed
from app.models import PROGRAMME_CODES, AuditLog, EntitlementGrant, Mentee, Person, Programme, User, utcnow
from app.services.audit import REDACTED, audit, changes
from tests.conftest import ADMIN_EMAIL, session


def test_database_is_at_head():
    assert migrate.current_revision() == migrate.head_revision() == "0007_growth"


def test_grants_is_mounted_and_needs_a_login(client):
    r = client.get("/grants")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/grants"
    assert client.get("/grants/calls").status_code == 303


def test_programmes_are_seeded_once_and_never_overwritten(db):
    rows = {p.code: p for p in db.query(Programme).all()}
    assert set(rows) == set(PROGRAMME_CODES)
    items = rows["mentorship"].grant_template
    assert {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"} in items
    assert {"product": "kp", "kind": "sourcer_link", "value": "mentee"} in items
    assert {"product": "pp", "kind": "flag", "value": "kpuk.submit"} in items, "KP forwarding is a separate right"
    assert {"product": "hnh", "kind": "partner_seat", "value": "area"} in items
    assert {"product": "ordris", "kind": "org_plan", "value": "starter"} in items
    assert [i for i in rows["hnh_franchise"].grant_template if i["product"] == "pp"][0]["months"] == 3
    assert [i for i in rows["kp_mentee"].grant_template if i["product"] == "pp"][0]["months"] == 12
    for item in (i for p in rows.values() for i in p.grant_template):
        assert item["product"] in ("pp", "ordris", "kp", "hnh")
        assert item["kind"] in ("comp_tier", "org_plan", "sourcer_link", "partner_seat", "flag")

    # editing a template in the database survives the next boot
    rows["mentorship"].grant_template = [{"product": "pp", "kind": "comp_tier", "value": "pro_buyer"}]
    db.commit()
    assert seed.ensure_programmes(db) == 0
    assert seed.seed(db)["programmes_added"] == 0
    db.expire_all()
    assert len(db.get(Programme, rows["mentorship"].id).grant_template) == 1


def test_admin_is_ensured_but_never_duplicated(db):
    assert seed.ensure_admin(db, quiet=True)["admin_created"] is False      # already there (from conftest)
    db.query(User).delete()
    db.commit()
    report = seed.ensure_admin(db, quiet=True)
    db.commit()
    assert report["admin_created"] is True
    assert db.query(User).filter(User.email == ADMIN_EMAIL).count() == 1
    user = db.query(User).filter(User.email == ADMIN_EMAIL).one()
    assert user.totp_secret is None, "the authenticator is enrolled at the first login, never pre-set"
    assert user.must_change_password is True, "even a supplied ADMIN_PASSWORD is changed at the first login"


def test_emails_and_person_keys_are_unique_case_insensitively(db):
    db.add(Person(person_key="jo@example.test", name="Jo"))
    db.commit()
    db.add(Person(person_key="JO@example.test", name="Jo again"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(User(email=ADMIN_EMAIL.upper(), password_hash="x"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_enumerated_columns_are_enforced_by_the_database(db):
    person = Person(person_key="amy@example.test")
    db.add(person)
    db.commit()
    programme = db.query(Programme).filter(Programme.code == "mentorship").one()
    db.add(Mentee(person_id=person.id, programme_id=programme.id, payment_mode="weekly", start_date=date(2026, 10, 1)))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(EntitlementGrant(person_id=person.id, product="xx", kind="comp_tier", value="pro_buyer", starts_at=utcnow()))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(EntitlementGrant(person_id=person.id, product="pp", kind="comp_tier", value="pro_buyer", starts_at=utcnow(),
                            state="maybe"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    grant = EntitlementGrant(person_id=person.id, product="pp", kind="comp_tier", value="pro_buyer", starts_at=utcnow())
    db.add(grant)
    db.commit()
    assert grant.state == "pending" and grant.created_at.tzinfo is None


def test_audit_rows_carry_who_what_and_the_change():
    with session() as db:
        user = db.query(User).filter(User.email == ADMIN_EMAIL).one()
        before, after = changes({"status": "active", "cohort": "A"}, {"status": "lapsed", "cohort": "A"})
        assert before == {"status": "active"} and after == {"status": "lapsed"}
        audit(db, user, "mentee.update", "mentee:7", before=before, after=after)
        audit(db, "job:recompute", "grant.revoke", "grant:3", before={"ends_at": utcnow()}, after=None)
        db.commit()
    with session() as db:
        rows = db.query(AuditLog).order_by(AuditLog.id).all()
        assert [r.action for r in rows] == ["mentee.update", "grant.revoke"]
        assert rows[0].actor == ADMIN_EMAIL and rows[0].user_id == user.id
        assert rows[0].before == {"status": "active"} and rows[0].after == {"status": "lapsed"}
        assert rows[1].actor == "job:recompute" and rows[1].user_id is None
        assert isinstance(rows[1].before["ends_at"], str) and rows[1].after is None


def test_audit_never_stores_a_password_hash_or_secret():
    with session() as db:
        user = db.query(User).filter(User.email == ADMIN_EMAIL).one()
        audit(db, user, "user.snapshot", f"user:{user.id}", after=user)
        audit(db, user, "call.snapshot", "call:1", after={"response": {"token": "test-abc", "nested": {"api_secret": "x"}}, "ok": True})
        db.commit()
    with session() as db:
        rows = db.query(AuditLog).filter(AuditLog.action.in_(("user.snapshot", "call.snapshot"))).order_by(AuditLog.id).all()
        snapshot, call = rows[0].after, rows[1].after
        assert snapshot["password_hash"] == REDACTED and snapshot["totp_secret"] == REDACTED
        assert snapshot["email"] == ADMIN_EMAIL
        assert "argon2" not in str(snapshot) and "JBSWY3DP" not in str(snapshot)
        assert call["response"]["token"] == REDACTED and call["response"]["nested"]["api_secret"] == REDACTED and call["ok"] is True
