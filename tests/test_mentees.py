"""Mentees: enrolment, payments entered in pounds and stored to the penny, the displayed access rule,
the Grants tab with the entitlement engine missing (503) and with a stub engine, the Notes tab,
programme template validation and the audit page."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models import AuditLog, EntitlementGrant, Mentee, Payment, Programme, utcnow
from app.routers import mentees as mentees_router
from app.routers.mentees import (ENGINE_MISSING, LAPSED_BY_HAND, access_end, access_summary, add_months, money, pence_to_amount,
                                 pounds_to_pence)
from app.routers.programmes import parse_grant_template
from tests.conftest import ADMIN_EMAIL, session
from tests.test_people import audit_rows, create_person, flash_of, id_of

TODAY = utcnow().date()


def programme_id(code: str = "mentorship") -> int:
    with session() as db:
        return db.query(Programme).filter(Programme.code == code).one().id


def enrol(c, person_id: int, mode: str = "full", start: date | None = None, code: str = "mentorship", cohort: str = "") -> int:
    r = c.post("/mentees/new", data={"person_id": str(person_id), "programme_id": str(programme_id(code)), "payment_mode": mode,
                                     "start_date": (start or TODAY).isoformat(), "cohort": cohort, "notes": ""})
    assert r.status_code == 303, r.text[:400]
    return id_of(r.headers["location"].split("?")[0])


def pay(c, mentee_id: int, amount: str, *, paid: date | None = None, cleared: date | None = None, period: tuple[date, date] | None = None,
        method: str = "manual", reference: str = ""):
    data = {"amount": amount, "paid_at": (paid or TODAY).isoformat(), "cleared_at": cleared.isoformat() if cleared else "",
            "method": method, "period_start": period[0].isoformat() if period else "", "period_end": period[1].isoformat() if period else "",
            "reference": reference, "notes": ""}
    return c.post(f"/mentees/{mentee_id}/payments", data=data)


# ---- money ------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw, pence", [
    ("1250.50", 125050), ("1,250.50", 125050), ("£1,250.50", 125050), (" 1250.5 ", 125050), ("1250", 125000),
    ("0.01", 1), ("1 250.00", 125000), ("99999999.99", 9999999999),
])
def test_pounds_to_pence_is_exact(raw, pence):
    assert pounds_to_pence(raw) == pence


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12.345", "-5", "+5", "1e3", "0", "0.00", "100000000.00", "12,5", "1 2.50",
                                 "1,2500", "12,50", "1.250,00"])
def test_pounds_to_pence_refuses_what_it_cannot_read_exactly(raw):
    with pytest.raises(ValueError):
        pounds_to_pence(raw)


def test_pence_to_amount_and_money_formatting():
    assert pence_to_amount(125050) == Decimal("1250.50") and str(pence_to_amount(125050)) == "1250.50"
    assert pence_to_amount(1) == Decimal("0.01") and pence_to_amount(125000) == Decimal("1250.00")
    assert money(Decimal("1250.5")) == "£1,250.50" and money(Decimal("0")) == "£0.00" and money(None) == ""
    assert money(pence_to_amount(pounds_to_pence("1,000,000.07"))) == "£1,000,000.07"


# ---- the displayed access rule ------------------------------------------------------------------
def test_add_months_handles_short_months():
    assert add_months(date(2026, 10, 1), 12) == date(2027, 10, 1)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def _mentee_with(mode: str, start: date, payments: list[dict]) -> Mentee:
    m = Mentee(payment_mode=mode, start_date=start, status="active")
    m.payments = [Payment(amount=Decimal("100.00"), paid_at=datetime(2026, 1, 1), **p) for p in payments]
    return m


def test_access_end_follows_the_plan_rule():
    start = date(2026, 10, 1)
    assert access_end(_mentee_with("full", start, [])) is None
    assert access_end(_mentee_with("full", start, [{"cleared_at": None}])) is None, "an uncleared payment grants nothing"
    assert access_end(_mentee_with("full", start, [{"cleared_at": datetime(2026, 10, 2)}])) == date(2027, 10, 1)
    monthly = [{"cleared_at": datetime(2026, 10, 2), "period_start": date(2026, 10, 1), "period_end": date(2026, 10, 31)},
               {"cleared_at": datetime(2026, 11, 2), "period_start": date(2026, 11, 1), "period_end": date(2026, 11, 30)},
               {"cleared_at": None, "period_start": date(2026, 12, 1), "period_end": date(2026, 12, 31)}]
    assert access_end(_mentee_with("monthly", start, monthly)) == date(2026, 12, 7), "latest cleared period end plus 7 days of grace"
    assert access_end(_mentee_with("monthly", start, [{"cleared_at": datetime(2026, 10, 2)}])) is None, "monthly needs a period end"

    today = date(2026, 12, 1)
    assert access_summary(_mentee_with("monthly", start, monthly), today) == {"end": date(2026, 12, 7), "label": "To 07 Dec 2026 (6 days)", "tone": "warn"}
    assert access_summary(_mentee_with("full", start, [{"cleared_at": datetime(2026, 10, 2)}]), today)["tone"] == "ok"
    lapsed = access_summary(_mentee_with("monthly", start, monthly), date(2027, 1, 15))
    assert lapsed["tone"] == "bad" and lapsed["label"] == "Ended 07 Dec 2026"
    assert access_summary(_mentee_with("full", start, []), today)["label"] == "No cleared payment"


# ---- enrolment -------------------------------------------------------------------------------
def test_enrol_mentee_records_the_row_and_an_audit_entry(admin_client):
    jo = create_person(admin_client, "jo@example.test", "Jo Bloggs")
    page = admin_client.get("/mentees/new", params={"person_id": jo})
    assert page.status_code == 200 and f'<option value="{jo}" selected>' in page.text
    mid = enrol(admin_client, jo, "full", date(2026, 10, 1), cohort="Autumn 2026")
    with session() as db:
        m = db.get(Mentee, mid)
        assert m.person_id == jo and m.programme.code == "mentorship" and m.payment_mode == "full"
        assert m.start_date == date(2026, 10, 1) and m.status == "active" and m.cohort == "Autumn 2026"
    rows = audit_rows("mentee.create")
    assert len(rows) == 1 and rows[0].entity == f"mentee:{mid}" and rows[0].after["payment_mode"] == "full"
    assert rows[0].after["start_date"] == "2026-10-01"

    detail = admin_client.get(f"/mentees/{mid}")
    assert detail.status_code == 200
    assert "Jo Bloggs" in detail.text and "Mentorship" in detail.text and "Autumn 2026" in detail.text
    assert "No payments recorded yet" in detail.text and "no cleared payment" in detail.text.lower()
    listing = admin_client.get("/mentees")
    assert "1 mentee" in listing.text and "No cleared payment" in listing.text
    person_page = admin_client.get(f"/people/{jo}")
    assert "Mentorship" in person_page.text and f"/mentees/{mid}" in person_page.text


def test_enrol_refuses_bad_input(admin_client):
    jo = create_person(admin_client, "jo@example.test")
    r = admin_client.post("/mentees/new", data={"person_id": "", "programme_id": str(programme_id()), "payment_mode": "full", "start_date": "2026-10-01"})
    assert r.status_code == 400 and "Choose the person" in r.text
    r = admin_client.post("/mentees/new", data={"person_id": str(jo), "programme_id": str(programme_id()), "payment_mode": "weekly", "start_date": "2026-10-01"})
    assert r.status_code == 400 and "in full or monthly" in r.text
    r = admin_client.post("/mentees/new", data={"person_id": str(jo), "programme_id": str(programme_id()), "payment_mode": "full", "start_date": "next tuesday"})
    assert r.status_code == 400 and "does not look like a date" in r.text
    enrol(admin_client, jo)
    r = admin_client.post("/mentees/new", data={"person_id": str(jo), "programme_id": str(programme_id()), "payment_mode": "full", "start_date": "2026-10-01"})
    assert r.status_code == 400 and "already active on Mentorship" in r.text
    with session() as db:
        p = db.query(Programme).filter(Programme.code == "kp_mentee").one()
        p.active = False
        db.commit()
    r = admin_client.post("/mentees/new", data={"person_id": str(jo), "programme_id": str(programme_id("kp_mentee")), "payment_mode": "full", "start_date": "2026-10-01"})
    assert r.status_code == 400 and "is not active" in r.text
    with session() as db:
        assert db.query(Mentee).count() == 1
    assert admin_client.get("/mentees/9999").status_code == 404


# ---- payments ------------------------------------------------------------------------------------
def test_payment_is_stored_to_the_penny_and_cleared_by_hand(admin_client):
    jo = create_person(admin_client, "jo@example.test", "Jo")
    start = TODAY - timedelta(days=30)
    mid = enrol(admin_client, jo, "full", start)
    r = pay(admin_client, mid, "1,250.50", reference="FPS JO BLOGGS")
    assert r.status_code == 303
    assert "£1,250.50" in flash_of(r.headers["location"])["ok"] and "Mark it cleared" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        payment = db.query(Payment).one()
        assert payment.amount == Decimal("1250.50") and payment.cleared_at is None and payment.method == "manual"
        assert payment.paid_at == datetime.combine(TODAY, datetime.min.time()) and payment.reference == "FPS JO BLOGGS"
        assert payment.mentee_id == mid
    created = audit_rows("payment.create")
    assert len(created) == 1 and created[0].after["pence"] == 125050 and created[0].after["amount"] == "1250.50"

    page = admin_client.get(f"/mentees/{mid}", params={"tab": "payments"})
    assert page.status_code == 200 and "£1,250.50" in page.text and "Mark cleared" in page.text
    assert "£1,250.50 awaiting clearance" in page.text and "no cleared payment" in page.text

    r = admin_client.post(f"/mentees/{mid}/payments/{payment.id}/clear")
    assert r.status_code == 303
    expected_end = add_months(start, 12)
    assert f"Access now runs to {expected_end:%d %b %Y}" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        assert db.query(Payment).one().cleared_at is not None
    cleared = audit_rows("payment.clear")
    assert len(cleared) == 1 and cleared[0].before == {"cleared_at": None} and cleared[0].after["cleared_at"]
    page = admin_client.get(f"/mentees/{mid}")
    assert "Mark cleared" not in page.text and f"{expected_end:%d %b %Y}" in page.text
    listing = admin_client.get("/mentees")
    assert f"To {expected_end:%d %b %Y}" in listing.text and "1 of 1" in listing.text

    # clearing twice is harmless; clearing a payment that is not this mentee's is a 404
    r = admin_client.post(f"/mentees/{mid}/payments/{payment.id}/clear")
    assert "already cleared" in flash_of(r.headers["location"])["ok"]
    assert admin_client.post(f"/mentees/{mid}/payments/999/clear").status_code == 404

    # a payment can arrive already cleared, with a Stripe reference
    r = pay(admin_client, mid, "500", cleared=TODAY, method="stripe_link", reference="pi_123")
    assert "It is cleared" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        amounts = sorted(p.amount for p in db.query(Payment).all())
        assert amounts == [Decimal("500.00"), Decimal("1250.50")]


def test_payment_form_refuses_bad_amounts_and_periods(admin_client):
    jo = create_person(admin_client, "jo@example.test")
    mid = enrol(admin_client, jo, "monthly", TODAY)
    r = pay(admin_client, mid, "12.345", period=(TODAY, TODAY + timedelta(days=30)))
    assert r.status_code == 400 and "at most two decimal places" in r.text and 'value="12.345"' in r.text
    r = pay(admin_client, mid, "-5", period=(TODAY, TODAY + timedelta(days=30)))
    assert r.status_code == 400
    r = pay(admin_client, mid, "abc", period=(TODAY, TODAY + timedelta(days=30)))
    assert r.status_code == 400 and "for example 1250.00" in r.text
    r = pay(admin_client, mid, "100", period=(TODAY + timedelta(days=30), TODAY))
    assert r.status_code == 400 and "period end is before the period start" in r.text
    r = pay(admin_client, mid, "100")
    assert r.status_code == 400 and "monthly payment needs a period end" in r.text
    r = admin_client.post(f"/mentees/{mid}/payments", data={"amount": "100", "paid_at": "", "method": "manual", "period_end": TODAY.isoformat()})
    assert r.status_code == 400 and "paid date is required" in r.text
    r = pay(admin_client, mid, "100", method="cheque", period=(TODAY, TODAY + timedelta(days=30)))
    assert r.status_code == 400 and "how the payment arrived" in r.text
    with session() as db:
        assert db.query(Payment).count() == 0
    assert not audit_rows("payment.create")

    # the monthly form pre-fills the next period from the last one recorded
    r = pay(admin_client, mid, "100", cleared=TODAY, period=(TODAY, TODAY + timedelta(days=29)))
    assert r.status_code == 303
    page = admin_client.get(f"/mentees/{mid}")
    assert f'value="{(TODAY + timedelta(days=30)).isoformat()}"' in page.text
    listing = admin_client.get("/mentees", params={"status": "active"})
    assert f"To {(TODAY + timedelta(days=36)):%d %b %Y}" in listing.text


# ---- grants tab: engine missing, then a stub engine ------------------------------------------------
def _grant(person_id: int, mentee_id: int | None = None, **kw) -> int:
    with session() as db:
        g = EntitlementGrant(person_id=person_id, mentee_id=mentee_id, product=kw.pop("product", "pp"), kind=kw.pop("kind", "comp_tier"),
                             value=kw.pop("value", "pro_buyer"), starts_at=utcnow(), **kw)
        db.add(g)
        db.commit()
        return g.id


def test_grants_tab_renders_without_the_engine_and_the_buttons_answer_503(admin_client, monkeypatch):
    monkeypatch.setattr(mentees_router, "recompute_person", None)
    monkeypatch.setattr(mentees_router, "revoke_person", None)
    jo = create_person(admin_client, "jo@example.test", "Jo")
    mid = enrol(admin_client, jo)
    _grant(jo, mid, state="failed", last_error="Property Predator refused: paid subscription", ends_at=datetime(2027, 10, 1))

    page = admin_client.get(f"/mentees/{mid}", params={"tab": "grants"})
    assert page.status_code == 200
    assert "503" in page.text and ENGINE_MISSING in page.text
    assert "Recompute now" in page.text and "Revoke all" in page.text and "disabled" in page.text
    assert "Property Predator refused: paid subscription" in page.text and "01 Oct 2027" in page.text and "status-failed" in page.text

    for path in (f"/mentees/{mid}/recompute", f"/mentees/{mid}/revoke"):
        r = admin_client.post(path)
        assert r.status_code == 503, path
        assert "503" in r.text and "not available" in r.text
    assert not audit_rows("mentee.recompute") and not audit_rows("mentee.revoke_all")


def test_grants_tab_with_a_stub_engine(admin_client, monkeypatch):
    calls: list[tuple] = []

    def recompute_person(db, person, actor):
        calls.append(("recompute", person.id, actor))
        return {"extended": 1, "unchanged": 2}

    def revoke_person(db, person, reason):      # no actor parameter: the router must cope with either signature
        calls.append(("revoke", person.id, reason))
        return "3 grants revoked"

    monkeypatch.setattr(mentees_router, "recompute_person", recompute_person)
    monkeypatch.setattr(mentees_router, "revoke_person", revoke_person)
    jo = create_person(admin_client, "jo@example.test", "Jo")
    mid = enrol(admin_client, jo)
    _grant(jo, mid, product="pp", state="active", ends_at=datetime(2027, 10, 1), source="programme:mentorship")
    _grant(jo, None, product="kp", kind="sourcer_link", value="mentee", state="pending")

    page = admin_client.get(f"/mentees/{mid}", params={"tab": "grants"})
    assert page.status_code == 200
    assert ENGINE_MISSING not in page.text and 'disabled' not in page.text.split("Revoke all")[0].rsplit("<form", 1)[-1]
    assert "Property Predator" in page.text and "Key Properties" in page.text
    assert "status-active" in page.text and "status-pending" in page.text and "programme:mentorship" in page.text

    r = admin_client.post(f"/mentees/{mid}/recompute")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/mentees/{mid}?tab=grants")
    assert "extended 1, unchanged 2" in flash_of(r.headers["location"])["ok"]
    assert calls == [("recompute", jo, ADMIN_EMAIL)]
    rows = audit_rows("mentee.recompute")
    assert len(rows) == 1 and rows[0].entity == f"mentee:{mid}" and rows[0].after["result"] == {"extended": 1, "unchanged": 2}

    r = admin_client.post(f"/mentees/{mid}/revoke", data={"reason": "Refund agreed"})
    assert r.status_code == 303 and "3 grants revoked" in flash_of(r.headers["location"])["ok"]
    assert calls[-1] == ("revoke", jo, "Refund agreed")
    rows = audit_rows("mentee.revoke_all")
    assert len(rows) == 1 and rows[0].after["reason"] == "Refund agreed"

    # the real engine returns a list of Decision objects: the flash and the audit row carry counts, not the dump
    def real_shape(db, person, actor):
        return [SimpleNamespace(action="grant", outcome="sent", label="pp comp_tier pro_buyer", plan={"x": "y" * 9000}),
                SimpleNamespace(action="grant", outcome="failed", label="ordris org_plan starter", plan=None),
                SimpleNamespace(action="noop", outcome="skipped", label="kp sourcer_link mentee", plan=None)]

    monkeypatch.setattr(mentees_router, "recompute_person", real_shape)
    r = admin_client.post(f"/mentees/{mid}/recompute")
    assert r.status_code == 303 and "grant 2, noop 1" in flash_of(r.headers["location"])["ok"]
    row = audit_rows("mentee.recompute")[-1]
    assert row.after["actions"] == {"grant": 2, "noop": 1} and row.after["outcomes"] == {"sent": 1, "failed": 1, "skipped": 1}
    assert row.after["failed"] == ["ordris org_plan starter"] and "note" not in row.after

    # an engine failure reaches the screen and the audit log rather than vanishing
    def broken(db, person, **kw):
        raise RuntimeError("Ordris did not answer")

    monkeypatch.setattr(mentees_router, "recompute_person", broken)
    r = admin_client.post(f"/mentees/{mid}/recompute")
    assert r.status_code == 303 and "Ordris did not answer" in flash_of(r.headers["location"])["err"]
    assert audit_rows("mentee.recompute_failed")[0].after["error"] == "Ordris did not answer"


# ---- notes tab ------------------------------------------------------------------------------------
def test_notes_tab_updates_status_and_audits_the_change(admin_client):
    jo = create_person(admin_client, "jo@example.test")
    mid = enrol(admin_client, jo, "monthly", date(2026, 10, 1))
    page = admin_client.get(f"/mentees/{mid}", params={"tab": "notes"})
    assert page.status_code == 200 and "Notes and status" in page.text
    assert '<option value="lapsed"' not in page.text, "lapsed is worked out by the rule, not chosen"
    r = admin_client.post(f"/mentees/{mid}/details", data={"status": "lapsed", "cohort": "Autumn 2026", "payment_mode": "monthly",
                                                           "start_date": "2026-10-01", "notes": "Missed October; chasing."})
    assert r.status_code == 400 and LAPSED_BY_HAND in r.text
    r = admin_client.post(f"/mentees/{mid}/details", data={"status": "ended", "cohort": "Autumn 2026", "payment_mode": "monthly",
                                                           "start_date": "2026-10-01", "notes": "Missed October; chasing."})
    assert r.status_code == 303 and "Recompute now" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        m = db.get(Mentee, mid)
        assert m.status == "ended" and m.cohort == "Autumn 2026" and m.notes == "Missed October; chasing."
    rows = audit_rows("mentee.update")
    assert rows[0].before == {"status": "active", "cohort": "", "notes": ""}
    assert rows[0].after == {"status": "ended", "cohort": "Autumn 2026", "notes": "Missed October; chasing."}
    r = admin_client.post(f"/mentees/{mid}/details", data={"status": "paused", "cohort": "", "payment_mode": "monthly", "start_date": "2026-10-01"})
    assert r.status_code == 400 and "Choose a status" in r.text
    # a mentee the rule has lapsed shows that status, keeps it on save, and can be ended
    with session() as db:
        db.get(Mentee, mid).status = "lapsed"
        db.commit()
    page = admin_client.get(f"/mentees/{mid}", params={"tab": "notes"})
    assert '<option value="lapsed" selected>' in page.text
    r = admin_client.post(f"/mentees/{mid}/details", data={"status": "lapsed", "cohort": "Autumn 2026", "payment_mode": "monthly",
                                                           "start_date": "2026-10-01", "notes": "Still chasing."})
    assert r.status_code == 303
    listing = admin_client.get("/mentees", params={"status": "lapsed"})
    assert "1 mentee" in listing.text and "status-lapsed" in listing.text
    assert "No mentees match" in admin_client.get("/mentees", params={"status": "ended"}).text


# ---- programmes ---------------------------------------------------------------------------------------
def test_grant_template_item_shape_is_validated():
    good = '[{"product": "pp", "kind": "comp_tier", "value": " pro_buyer "}, {"product": "ordris", "kind": "org_plan", "value": "starter", "months": 3}]'
    items, problem = parse_grant_template(good)
    assert problem == "" and items == [{"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
                                       {"product": "ordris", "kind": "org_plan", "value": "starter", "months": 3}]
    assert parse_grant_template("") == ([], "") and parse_grant_template("[]") == ([], "")
    for bad, words in (
        ("{", "not valid JSON"),
        ('{"product": "pp"}', "must be a JSON list"),
        ('["pp"]', "Item 1 must be an object"),
        ('[{"product": "zz", "kind": "comp_tier", "value": "x"}]', "product must be one of pp, ordris, kp, hnh"),
        ('[{"product": "pp", "kind": "tier", "value": "x"}]', "kind must be one of"),
        ('[{"product": "pp", "kind": "comp_tier", "value": ""}]', "value must be"),
        ('[{"product": "pp", "kind": "comp_tier", "value": 5}]', "value must be"),
        ('[{"product": "pp", "kind": "comp_tier", "value": "x", "months": true}]', "months must be a whole number"),
        ('[{"product": "pp", "kind": "comp_tier", "value": "x", "months": 0}]', "months must be a whole number"),
        ('[{"product": "pp", "kind": "comp_tier", "value": "x", "months": "3"}]', "months must be a whole number"),
        ('[{"product": "pp", "kind": "comp_tier", "value": "x", "until": "2027"}]', "unexpected key"),
        ('[{"product": "pp", "kind": "comp_tier", "value": "x", "months": 14}]', "months must be a whole number from 1 to 13"),
        ('[{"product": "ordris", "kind": "org_plan", "value": "x"}, {"product": "ordris", "kind": "org_plan", "value": "x"}]', "repeats"),
        ('[{"product": "pp", "kind": "comp_tier", "value": "pro_buyer"}, {"product": "pp", "kind": "comp_tier", "value": "apex"}]', "one Property Predator tier only"),
        ('[{"product": "pp", "kind": "comp_tier", "value": "x", "fee_share_pct": 50}]', "unexpected key"),
        ('[{"product": "kp", "kind": "sourcer_link", "value": "mentee", "fee_share_pct": 150}]', "fee_share_pct must be"),
        ('[{"product": "kp", "kind": "sourcer_link", "value": "mentee", "fee_share_pct": "50"}]', "fee_share_pct must be"),
        ('[{"product": "hnh", "kind": "partner_seat", "value": "area", "area_slug": "Swansea Bay"}]', "area_slug must be a slug"),
        ('[{"product": "hnh", "kind": "partner_seat", "value": "area", "area_slug": "swansea", "postcodes": ["SA1"]}]', "not both"),
        ('[{"product": "hnh", "kind": "partner_seat", "value": "area", "postcodes": []}]', "postcodes must be a list"),
        ('[{"product": "hnh", "kind": "partner_seat", "value": "area", "postcodes": ["SA1 1AA"]}]', "outward code"),
        ('[{"product": "hnh", "kind": "partner_seat", "value": "area", "fee_waived": "yes"}]', "fee_waived must be"),
    ):
        items, problem = parse_grant_template(bad)
        assert items == [] and words in problem, (bad, problem)
    extras = ('[{"product": "kp", "kind": "sourcer_link", "value": "mentee", "fee_share_pct": 40},'
              ' {"product": "hnh", "kind": "partner_seat", "value": "area", "postcodes": ["sa1", "SA2 "], "fee_waived": false},'
              ' {"product": "hnh", "kind": "partner_seat", "value": "introducer", "area_slug": "south-wales"}]')
    items, problem = parse_grant_template(extras)
    assert problem == "" and items == [
        {"product": "kp", "kind": "sourcer_link", "value": "mentee", "fee_share_pct": 40},
        {"product": "hnh", "kind": "partner_seat", "value": "area", "postcodes": ["SA1", "SA2"], "fee_waived": False},
        {"product": "hnh", "kind": "partner_seat", "value": "introducer", "area_slug": "south-wales"},
    ]


def test_programme_edit_screen_saves_a_valid_template_and_refuses_a_bad_one(admin_client):
    pid = programme_id("hnh_franchise")
    assert admin_client.get("/programmes").status_code == 200 and "HNH franchise purchase" in admin_client.get("/programmes").text
    page = admin_client.get(f"/programmes/{pid}")
    assert page.status_code == 200 and '"months": 3' in page.text
    r = admin_client.post(f"/programmes/{pid}", data={"name": "HNH franchise purchase", "description": "x", "active": "1",
                                                      "grant_template": '[{"product": "hnh", "kind": "seat", "value": "area"}]'})
    assert r.status_code == 400 and "kind must be one of" in r.text and '&#34;seat&#34;' in r.text, "the typed JSON is kept"
    with session() as db:
        assert len(db.get(Programme, pid).grant_template) == 3, "nothing changed"
    new = '[{"product": "hnh", "kind": "partner_seat", "value": "area"}, {"product": "pp", "kind": "comp_tier", "value": "pro_buyer", "months": 6}]'
    r = admin_client.post(f"/programmes/{pid}", data={"name": "HNH franchise purchase", "description": "Territory plus six months of PP Hunter.",
                                                      "grant_template": new})
    assert r.status_code == 303 and "next recompute" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        p = db.get(Programme, pid)
        assert p.active is False and p.grant_template[1]["months"] == 6 and len(p.grant_template) == 2
    rows = audit_rows("programme.update")
    assert len(rows) == 1 and set(rows[0].after) == {"description", "active", "grant_template"}
    r = admin_client.post(f"/programmes/{pid}", data={"name": "", "description": "Territory plus six months of PP Hunter.", "grant_template": new})
    assert r.status_code == 400 and "name is required" in r.text
    assert admin_client.get("/programmes/9999").status_code == 404
    # the price
    r = admin_client.post(f"/programmes/{pid}", data={"name": "HNH franchise purchase", "description": "x", "grant_template": new, "price": "ten"})
    assert r.status_code == 400 and "pounds and pence" in r.text
    r = admin_client.post(f"/programmes/{pid}", data={"name": "HNH franchise purchase", "description": "x", "grant_template": new, "price": "£4,995"})
    assert r.status_code == 303 and "reach the price" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        assert db.get(Programme, pid).price == Decimal("4995.00")
    assert 'value="4995.00"' in admin_client.get(f"/programmes/{pid}").text


def test_a_full_payer_sees_the_shortfall_until_the_price_is_reached(admin_client):
    with session() as db:
        db.query(Programme).filter(Programme.code == "mentorship").one().price = Decimal("4995.00")
        db.commit()
    jo = create_person(admin_client, "jo@example.test", "Jo")
    mid = enrol(admin_client, jo, "full", TODAY - timedelta(days=10))
    r = pay(admin_client, mid, "100", cleared=TODAY)
    assert r.status_code == 303
    page = admin_client.get(f"/mentees/{mid}")
    assert "£4,895.00 more must clear before the term starts" in page.text and "£4,895.00 short of the price" in page.text
    r = pay(admin_client, mid, "4895")
    with session() as db:
        pid = [p.id for p in db.query(Payment).all() if p.cleared_at is None][0]
    r = admin_client.post(f"/mentees/{mid}/payments/{pid}/clear")
    assert "Access now runs to" in flash_of(r.headers["location"])["ok"]
    page = admin_client.get(f"/mentees/{mid}")
    assert "short of the price" not in page.text and "must clear before" not in page.text


def test_payment_periods_are_bounded(admin_client):
    jo = create_person(admin_client, "jo@example.test")
    monthly = enrol(admin_client, jo, "monthly", TODAY)
    r = pay(admin_client, monthly, "150", period=(TODAY, date(2206, 10, 31)))
    assert r.status_code == 400 and "400 days away is almost certainly a typo" in r.text
    r = pay(admin_client, monthly, "150", period=(TODAY, TODAY + timedelta(days=60)))
    assert r.status_code == 400 and "one month and a week" in r.text
    r = pay(admin_client, monthly, "150", period=(TODAY, TODAY + timedelta(days=36)))
    assert r.status_code == 303
    full = enrol(admin_client, jo, "full", TODAY, code="kp_mentee")
    r = pay(admin_client, full, "1000", period=(TODAY, TODAY + timedelta(days=401)))
    assert r.status_code == 400 and "almost certainly a typo" in r.text
    r = pay(admin_client, full, "1000", period=(TODAY, TODAY + timedelta(days=200)))
    assert r.status_code == 303, "a full payer may carry a longer period"
    with session() as db:
        assert db.query(Payment).count() == 2


# ---- audit page -----------------------------------------------------------------------------------------
def test_audit_page_lists_pages_and_filters(admin_client):
    jo = create_person(admin_client, "jo@example.test", "Jo")
    mid = enrol(admin_client, jo)
    pay(admin_client, mid, "100", cleared=TODAY)
    def cell(action: str) -> str:          # the action as a table cell, not as an option in the filter dropdown
        return f'<td class="action">{action}</td>'

    page = admin_client.get("/audit")
    assert page.status_code == 200
    assert cell("person.create") in page.text and cell("mentee.create") in page.text and cell("payment.create") in page.text
    assert ADMIN_EMAIL in page.text and "jo@example.test" in page.text, "the actor and the after snapshot are shown"
    only_mentee = admin_client.get("/audit", params={"q": f"mentee:{mid}"})
    assert cell("mentee.create") in only_mentee.text and cell("person.create") not in only_mentee.text and "1 entry" in only_mentee.text
    by_action = admin_client.get("/audit", params={"action": "payment.create"})
    assert cell("payment.create") in by_action.text and cell("mentee.create") not in by_action.text
    assert "Nothing matches" in admin_client.get("/audit", params={"q": "nobody:1"}).text
    with session() as db:
        for i in range(60):
            db.add(AuditLog(actor="job:recompute", action="grant.extend", entity=f"grant:{i}", after={"ends_at": "2027-01-01"}))
        db.commit()
    first = admin_client.get("/audit")
    assert "Page 1 of 2" in first.text and "grant:59" in first.text and cell("person.create") not in first.text
    second = admin_client.get("/audit", params={"page": "2"})
    assert "Page 2 of 2" in second.text and cell("person.create") in second.text
    assert "Page 2 of 2" in admin_client.get("/audit", params={"page": "50"}).text
