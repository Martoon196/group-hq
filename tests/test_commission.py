"""The seller commission ledger: commission accrues only on a cleared fee and only once; the percentage follows
the rule for the seller's kind (mentee and franchisee 20% upfront plus 10% of twelve months' trail, outsider 10%
upfront only, all [CONFIRM]); the trail stops after the rule's months; a refund inside 90 days claws back; the
10th-of-the-month statements collect every unpaid entry per seller, regenerate without duplicating, carry a
negative month forward and are stamped paid with their entries; the seller screens, the statement print view
and the nightly job; and every figure badged [CONFIRM] until the founder confirms the rules."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest

from app.jobs import statements as statements_job
from app.models import AuditLog, CommissionEntry, CommissionRule, FranchiseOpportunity, FranchisePayment, Person, Seller, Statement, utcnow
from app.services import commission as c
from app.services import territory_rules as tr
from tests.conftest import ADMIN_EMAIL, session
from tests.test_franchise import create_opportunity, move, seed_districts
from tests.test_people import audit_rows, create_person, flash_of, id_of

TODAY = utcnow().date()
SEPT = (date(2026, 9, 1), date(2026, 9, 30))
GENERATED_ON = datetime(2026, 10, 10, 3, 15)


# ---- helpers ---------------------------------------------------------------------------------------------------
def make_seller(c_, email: str, code: str = "", kind: str = "mentee", name: str = "Sam Seller", self_billing: str = "1",
                self_billing_date: date | None = None, bank: str = "Sort 00-00-00 acct 12345678") -> int:
    data = {"person_id": "", "email": email, "name": name, "phone": "", "kind": kind, "code": code, "bank_reference": bank,
            "self_billing": self_billing, "self_billing_date": (self_billing_date or date(2026, 9, 1)).isoformat(), "active": "1"}
    r = c_.post("/sellers/new", data=data)
    assert r.status_code == 303, r.text[:600]
    return id_of(r.headers["location"].split("?")[0])


def seller_row(seller_id: int) -> Seller:
    with session() as db:
        s = db.get(Seller, seller_id)
        _ = s.person
        return s


def opportunity_row(oid: int) -> FranchiseOpportunity:
    with session() as db:
        o = db.get(FranchiseOpportunity, oid)
        _ = o.person, o.seller
        return o


def add_payment(oid: int, basis: str, amount_pence: int, *, trail_month: int | None = None, paid: datetime | None = None,
                cleared: datetime | None = None) -> int:
    with session() as db:
        row = FranchisePayment(opportunity_id=oid, basis=basis, trail_month=trail_month, amount_pence=amount_pence,
                               paid_at=paid or cleared or datetime.combine(TODAY, datetime.min.time()), cleared_at=cleared, method="manual")
        db.add(row)
        db.commit()
        return row.id


def accrue_payment(oid: int, payment_id: int, actor: str = "test") -> dict:
    with session() as db:
        o = db.get(FranchiseOpportunity, oid)
        p = db.get(FranchisePayment, payment_id)
        result = c.accrue(db, o, p, actor=actor)
        db.commit()
        return result


def entries_for(oid: int | None = None, seller_id: int | None = None) -> list[CommissionEntry]:
    with session() as db:
        q = db.query(CommissionEntry)
        if oid is not None:
            q = q.filter(CommissionEntry.opportunity_id == oid)
        if seller_id is not None:
            q = q.filter(CommissionEntry.seller_id == seller_id)
        rows = q.order_by(CommissionEntry.id).all()
        for e in rows:
            _ = e.rule
        return rows


def sold_territory(admin_client, seller_code: str, buyer: str = "buyer@example.test", districts: list[str] | None = None) -> int:
    """An opportunity attributed to the seller, moved to Paid: the upfront fee is on record and not yet cleared."""
    oid = create_opportunity(admin_client, districts or ["SP10", "SP11"], email=buyer, name="Bea Buyer", seller_code=seller_code)
    r = move(admin_client, oid, "agreement", "Heads of terms signed")
    assert r.status_code == 303
    r = move(admin_client, oid, "paid", "Bank transfer expected; moved ahead of the money for the test", override=True, paid_at="2026-09-01")
    assert r.status_code == 303, r.text[:300]
    assert opportunity_row(oid).stage == "paid"
    return oid


def upfront_payment_id(oid: int) -> int:
    with session() as db:
        return db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == oid, FranchisePayment.basis == "upfront").one().id


def confirm_rules(admin_client) -> None:
    with session() as db:
        tr.ensure_commission_rules(db)
        db.commit()
        ids = [r.id for r in db.query(CommissionRule).all()]
    for rid in ids:
        assert admin_client.post(f"/sellers/rules/{rid}/confirm").status_code == 303


# ---- pure helpers ------------------------------------------------------------------------------------------------
def test_money_helpers_round_half_up_and_format_negatives():
    assert c.pence_share(1_300_000, Decimal("0.2000")) == 260_000 and c.pence_share(7_900, "0.1000") == 790
    assert c.pence_share(5, "0.1") == 1, "half a penny rounds up"
    assert c.pence_share(4, "0.1") == 0 and c.pence_share(0, "0.2") == 0
    assert c.pounds(260_000) == "£2,600.00" and c.pounds(-260_000) == "-£2,600.00" and c.pounds(None) == "" and c.pounds(0) == "£0.00"
    assert c.percent(Decimal("0.2000")) == "20%" and c.percent("0.125") == "12.5%" and c.percent(0) == "0%"
    assert c.parse_period("2026-10") == (date(2026, 10, 1), date(2026, 10, 31)) and c.parse_period(" 2024-2 ") == (date(2024, 2, 1), date(2024, 2, 29))
    for bad in ("2026", "2026-13", "October 2026", "", "2026-00"):
        with pytest.raises(c.CommissionError):
            c.parse_period(bad)
    assert c.previous_month(date(2026, 11, 10)) == (date(2026, 10, 1), date(2026, 10, 31))
    assert c.previous_month(date(2027, 1, 3)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert c.statement_due(date(2026, 11, 10)) and not c.statement_due(date(2026, 11, 9))
    assert c.payment_due_on(SimpleNamespace(period_end=date(2026, 10, 31))) == date(2026, 11, 20)
    assert c.payment_due_on(SimpleNamespace(period_end=date(2026, 12, 31))) == date(2027, 1, 20)
    assert c.code_prefix(SimpleNamespace(name="Jo Andover", person_key="jo@example.test")) == "JA"
    assert c.code_prefix(SimpleNamespace(name="Mary-Jane de la Cruz", person_key="x@example.test")) == "MJD"
    assert c.code_prefix(SimpleNamespace(name="", person_key="sam.seller@example.test")) == "SA"
    assert c.code_prefix(SimpleNamespace(name="X", person_key="9@example.test")) == "XN" and c.code_prefix(None) == "HN"
    assert c.code_problem("") and c.code_problem("a" * 25) and c.code_problem("JA 7K3M") and c.code_problem("-JA") and not c.code_problem("JA-7K3M")


def test_generated_codes_are_short_unambiguous_and_unique(db):
    person = Person(person_key="jo@example.test", name="Jo Andover")
    db.add(person)
    db.flush()
    seen = set()
    for _ in range(25):
        code = c.generate_code(db, person)
        assert code.startswith("JA-") and len(code) == 7 and all(ch in c.CODE_ALPHABET for ch in code[3:]), code
        assert not any(ch in code[3:] for ch in "01OIL")
        seen.add(code)
    assert len(seen) > 20, "the suffix is random"
    db.add(Seller(person_id=person.id, kind="mentee", code="JA-TAKEN"))
    db.flush()
    assert c.generate_code(db, person).lower() != "ja-taken"
    db.rollback()


# ---- accrual --------------------------------------------------------------------------------------------------
def test_commission_accrues_only_when_the_fee_clears_and_only_once(admin_client):
    seed_districts()
    sid = make_seller(admin_client, "sam@example.test", "SAM1")
    oid = sold_territory(admin_client, "SAM1")
    pid = upfront_payment_id(oid)
    assert entries_for(oid) == [], "Paid records the fee; nothing is earned until it clears"

    # the service refuses to accrue on an uncleared fee
    result = accrue_payment(oid, pid)
    assert result["entries"] == 0 and "not cleared" in result["reason"] and entries_for(oid) == []

    # clearing it through the pipeline drives the hook, which finds this ledger by name
    r = admin_client.post(f"/franchise/{oid}/payments/{pid}/clear", data={"cleared_at": "2026-09-05"})
    assert r.status_code == 303 and "accrued through the ledger" in flash_of(r.headers["location"])["ok"]
    rows = entries_for(oid)
    assert len(rows) == 1
    e = rows[0]
    assert e.seller_id == sid and e.basis == "upfront" and e.trail_month is None and e.basis_pence == 1_300_000 and e.amount_pence == 260_000
    assert e.rule.version == 1 and e.rule.seller_kind == "mentee" and e.earned_at == datetime(2026, 9, 5) and e.cleared_at == datetime(2026, 9, 5)
    assert e.paid_at is None and e.statement_id is None and e.clawback_of is None
    accrued = audit_rows("commission.accrue")
    assert len(accrued) == 1 and accrued[0].actor == ADMIN_EMAIL and accrued[0].entity == f"commission_entry:{e.id}"
    assert accrued[0].after["amount_pence"] == 260_000 and accrued[0].after["rule_confirmed"] is False and accrued[0].after["pct"] == "0.2000"
    hook = audit_rows("franchise.commission")[0].after
    assert hook["result"]["entries"] == 1 and hook["result"]["amount_pence"] == 260_000 and hook["result"]["confirmed"] is False

    # a second accrual for the same fee is a no-op, through the service and through the hook
    again = accrue_payment(oid, pid)
    assert again["entries"] == 0 and again["reason"] == "already accrued" and again["entry_id"] == e.id
    assert "already cleared" in flash_of(admin_client.post(f"/franchise/{oid}/payments/{pid}/clear").headers["location"])["ok"]
    assert len(entries_for(oid)) == 1 and len(audit_rows("commission.accrue")) == 1

    # a trail month recorded and cleared in one go accrues 10% of it; the detail page shows the totals
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "trail_month": "1", "amount": "79.00", "paid_at": "2026-10-01",
                                                             "cleared_at": "2026-10-03", "method": "stripe_link", "reference": "pi_1"})
    assert r.status_code == 303 and "accrued through the ledger" in flash_of(r.headers["location"])["ok"]
    rows = entries_for(oid)
    assert [(x.basis, x.trail_month, x.basis_pence, x.amount_pence) for x in rows] == [("upfront", None, 1_300_000, 260_000), ("monthly", 1, 7_900, 790)]
    detail = admin_client.get(f"/sellers/{sid}")
    assert detail.status_code == 200 and "£2,607.90" in detail.text and "[CONFIRM]" in detail.text and "awaiting a statement" in detail.text
    assert "Monthly fee, month 1" in detail.text and "20% <span" in detail.text and "10% <span" in detail.text
    with session() as db:
        totals = c.seller_totals(db, sid)
    assert totals == {"earned": 260_790, "clawed_back": 0, "cleared": 260_790, "paid": 0, "owed": 260_790, "on_statement": 0, "entries": 2}

    # a fee recorded but not cleared earns nothing until it is; a refunded fee earns nothing at all
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "trail_month": "2", "amount": "79.00", "paid_at": "2026-10-11", "method": "manual"})
    assert r.status_code == 303 and "Mark it cleared" in flash_of(r.headers["location"])["ok"] and len(entries_for(oid)) == 2
    with session() as db:
        month2 = db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == oid, FranchisePayment.trail_month == 2).one()
        month2.cleared_at = datetime(2026, 10, 13)
        month2.refunded_at = datetime(2026, 10, 14)
        db.commit()
        pid2 = month2.id
    result = accrue_payment(oid, pid2)
    assert result["entries"] == 0 and "refunded" in result["reason"] and len(entries_for(oid)) == 2


def test_percentages_follow_the_rule_for_the_sellers_kind(admin_client):
    seed_districts()
    sellers = {kind: make_seller(admin_client, f"{kind}@example.test", kind.upper(), kind=kind, name=f"{kind.title()} Seller") for kind in ("mentee", "franchisee", "outsider")}
    expected = {"mentee": (260_000, 790), "franchisee": (260_000, 790), "outsider": (130_000, None)}
    for kind, sid in sellers.items():
        oid = sold_territory(admin_client, kind.upper(), buyer=f"buyer-{kind}@example.test")
        up = accrue_payment(oid, upfront_payment_id(oid))
        assert up["entries"] == 0, "the upfront fee recorded by Paid is not cleared yet"
        with session() as db:
            p = db.get(FranchisePayment, upfront_payment_id(oid))
            p.cleared_at = datetime(2026, 9, 5)
            db.commit()
        up = accrue_payment(oid, upfront_payment_id(oid))
        assert up["entries"] == 1 and up["amount_pence"] == expected[kind][0] and up["rule"] == f"v1 {kind}", kind
        month1 = add_payment(oid, "monthly", 7_900, trail_month=1, cleared=datetime(2026, 10, 3))
        trail = accrue_payment(oid, month1)
        if expected[kind][1] is None:
            assert trail["entries"] == 0 and "outside the 0-month trail" in trail["reason"], kind
        else:
            assert trail["entries"] == 1 and trail["amount_pence"] == expected[kind][1], kind
        rows = entries_for(oid)
        assert [e.amount_pence for e in rows] == [v for v in expected[kind] if v is not None]
        assert all(e.seller_id == sid for e in rows)
    page = admin_client.get("/sellers")
    assert page.status_code == 200 and "3 sellers" in page.text
    assert "£2,607.90" in page.text and "£1,300.00" in page.text and page.text.count('class="confirm"') >= 3
    assert "10% of the upfront fee" in page.text and "20% of the upfront fee" in page.text and "10% of the monthly fee" in page.text


def test_trail_stops_after_the_rules_months_and_needs_a_month_number(admin_client):
    seed_districts()
    make_seller(admin_client, "sam@example.test", "SAM1")
    oid = sold_territory(admin_client, "SAM1")
    with session() as db:
        p = db.get(FranchisePayment, upfront_payment_id(oid))
        p.cleared_at = datetime(2026, 9, 5)
        db.commit()
    accrue_payment(oid, upfront_payment_id(oid))
    for month in range(1, 15):
        when = datetime(2026, 9, 5) + timedelta(days=30 * month)
        result = accrue_payment(oid, add_payment(oid, "monthly", 7_900, trail_month=month, cleared=when))
        if month <= 12:
            assert result["entries"] == 1 and result["amount_pence"] == 790, month
        else:
            assert result["entries"] == 0 and f"month {month} is outside the 12-month trail" in result["reason"], month
    rows = entries_for(oid)
    assert [e.trail_month for e in rows] == [None] + list(range(1, 13))
    assert sum(e.amount_pence for e in rows) == 260_000 + 12 * 790 == 269_480, "docs/commission-rules.md section 5: 2,694.80 over the year"
    with session() as db:
        o = db.get(FranchiseOpportunity, oid)
        with pytest.raises(c.CommissionError):
            c.accrue(db, o, SimpleNamespace(basis="monthly", trail_month=None, amount_pence=7_900, cleared_at=datetime(2026, 9, 5), refunded_at=None))
        with pytest.raises(c.CommissionError):
            c.accrue(db, o, SimpleNamespace(basis="bonus", trail_month=None, amount_pence=1, cleared_at=datetime(2026, 9, 5), refunded_at=None))
        db.rollback()


def test_no_seller_waived_fee_or_own_purchase_earns_nothing(admin_client):
    seed_districts()
    sid = make_seller(admin_client, "sam@example.test", "SAM1")
    plain = create_opportunity(admin_client, ["SP9"], email="solo@example.test", name="Solo")
    pid = add_payment(plain, "upfront", 300_000, cleared=datetime(2026, 9, 5))
    assert accrue_payment(plain, pid)["reason"].startswith("no seller")
    # the pipeline refuses a seller's own purchase; the ledger refuses it too if attribution is forced
    own = create_opportunity(admin_client, ["SP10"], email="sam@example.test", name="Sam Seller")
    with session() as db:
        db.get(FranchiseOpportunity, own).seller_id = sid
        db.commit()
    assert "own purchase" in accrue_payment(own, add_payment(own, "upfront", 1_300_000, cleared=datetime(2026, 9, 5)))["reason"]
    waived = add_payment(plain, "monthly", 0, trail_month=1, cleared=datetime(2026, 9, 5))
    with session() as db:
        db.get(FranchiseOpportunity, plain).seller_id = sid
        db.commit()
    assert "waived" in accrue_payment(plain, waived)["reason"]
    assert entries_for() == []


def test_an_opportunity_keeps_the_rule_it_was_sold_under_and_a_new_sale_takes_the_rule_of_its_agreement_date(admin_client):
    seed_districts()
    make_seller(admin_client, "sam@example.test", "SAM1")
    first = sold_territory(admin_client, "SAM1", buyer="first@example.test")
    with session() as db:
        p = db.get(FranchisePayment, upfront_payment_id(first))
        p.cleared_at = datetime(2026, 9, 5)
        db.commit()
    assert accrue_payment(first, upfront_payment_id(first))["rule"] == "v1 mentee"
    with session() as db:
        assert c.agreement_date(db, db.get(FranchiseOpportunity, first)) == TODAY, "the agreement date comes from the audit trail of the stage move"
        assert c.agreement_date(db, db.get(FranchiseOpportunity, create_opportunity(admin_client, ["DH7"], email="nobody@example.test"))) is None

    # version 2 for mentees, in force from the agreement date: a later sale uses it, the earlier sale does not
    with session() as db:
        db.add(CommissionRule(version=2, seller_kind="mentee", upfront_pct=Decimal("0.2500"), trail_pct=Decimal("0.0500"), trail_months=6,
                              active_from=TODAY, note="Test version 2.", confirmed=True))
        db.commit()
    trail = accrue_payment(first, add_payment(first, "monthly", 7_900, trail_month=1, cleared=datetime(2026, 10, 3)))
    assert trail["rule"] == "v1 mentee" and trail["amount_pence"] == 790, "the trail follows the upfront entry's rule"

    second = sold_territory(admin_client, "SAM1", buyer="second@example.test", districts=["SP9"])
    with session() as db:
        p = db.get(FranchisePayment, upfront_payment_id(second))
        p.cleared_at = datetime(2026, 10, 5)
        db.commit()
    up = accrue_payment(second, upfront_payment_id(second))
    quoted = opportunity_row(second).quoted_upfront_pence
    assert quoted > 0 and up["rule"] == "v2 mentee" and up["amount_pence"] == c.pence_share(quoted, "0.25")
    month7 = accrue_payment(second, add_payment(second, "monthly", 4_900, trail_month=7, cleared=datetime(2027, 5, 5)))
    assert month7["entries"] == 0 and "outside the 6-month trail of rule version 2" in month7["reason"]
    month1 = accrue_payment(second, add_payment(second, "monthly", 4_900, trail_month=1, cleared=datetime(2026, 11, 5)))
    assert month1["rule"] == "v2 mentee" and month1["amount_pence"] == 245

    # no rule in force yet is an error the hook records as pending, not a silent skip
    with session() as db:
        db.query(CommissionRule).update({CommissionRule.active_from: TODAY + timedelta(days=400)})
        db.commit()
    third = sold_territory(admin_client, "SAM1", buyer="third@example.test", districts=["DH7"])
    with session() as db:
        p = db.get(FranchisePayment, upfront_payment_id(third))
        p.cleared_at = datetime(2026, 10, 6)
        db.commit()
    with session() as db:
        with pytest.raises(c.CommissionError, match="No commission rule for a mentee seller is in force"):
            c.accrue(db, db.get(FranchiseOpportunity, third), db.get(FranchisePayment, upfront_payment_id(third)))
        db.rollback()


# ---- clawback --------------------------------------------------------------------------------------------------------
def test_a_refund_inside_ninety_days_claws_back_and_a_later_one_is_left_to_the_founder(admin_client):
    seed_districts()
    sid = make_seller(admin_client, "sam@example.test", "SAM1")
    oid = sold_territory(admin_client, "SAM1")
    with session() as db:
        p = db.get(FranchisePayment, upfront_payment_id(oid))
        p.cleared_at = datetime(2026, 9, 5)
        db.commit()
    accrue_payment(oid, upfront_payment_id(oid))
    m1 = add_payment(oid, "monthly", 7_900, trail_month=1, cleared=datetime(2026, 10, 3))
    accrue_payment(oid, m1)
    original = entries_for(oid)[0]

    with session() as db:
        o = db.get(FranchiseOpportunity, oid)
        p = db.get(FranchisePayment, upfront_payment_id(oid))
        p.refunded_at = datetime(2026, 11, 1)
        result = c.on_refund(db, o, p, actor="test")
        db.commit()
    assert result == {"clawbacks": 1, "amount_pence": -260_000, "reason": ""}
    rows = entries_for(oid)
    assert len(rows) == 3
    claw = rows[-1]
    assert claw.clawback_of == original.id and claw.amount_pence == -260_000 and claw.basis == "upfront" and claw.basis_pence == 1_300_000
    assert claw.rule_id == original.rule_id and claw.earned_at == datetime(2026, 11, 1) and claw.cleared_at == datetime(2026, 11, 1) and claw.statement_id is None
    row = audit_rows("commission.clawback")[0]
    assert row.entity == f"commission_entry:{claw.id}" and row.after["clawback_of"] == original.id and "57 days after it cleared" in row.after["reason"]
    with session() as db:
        assert c.seller_totals(db, sid) == {"earned": 260_790, "clawed_back": 260_000, "cleared": 790, "paid": 0, "owed": 790, "on_statement": 0, "entries": 3}
        # asking again changes nothing
        o = db.get(FranchiseOpportunity, oid)
        p = db.get(FranchisePayment, upfront_payment_id(oid))
        again = c.on_refund(db, o, p, actor="test")
        assert again["clawbacks"] == 0 and again["reason"] == "already clawed back"
        assert c.clawback(db, db.get(CommissionEntry, original.id), "twice", actor="test").id == claw.id
        with pytest.raises(c.CommissionError):
            c.clawback(db, db.get(CommissionEntry, claw.id), "a clawback of a clawback")
        with pytest.raises(c.CommissionError):
            c.clawback(db, db.get(CommissionEntry, rows[1].id), "   ")
        db.rollback()
    assert len(entries_for(oid)) == 3

    # the trail month refunded 106 days after clearing is outside the window: nothing is written
    with session() as db:
        o = db.get(FranchiseOpportunity, oid)
        p = db.get(FranchisePayment, m1)
        late = c.on_refund(db, o, p, refunded_at=datetime(2027, 1, 17), actor="test")
        db.rollback()
    assert late["clawbacks"] == 0 and "106 days after clearing, outside the 90-day window" in late["reason"] and "[CONFIRM]" in late["reason"]
    assert len(entries_for(oid)) == 3
    # exactly 90 days is inside it; a fee that never cleared has nothing to claw back
    with session() as db:
        o = db.get(FranchiseOpportunity, oid)
        edge = c.on_refund(db, o, db.get(FranchisePayment, m1), refunded_at=datetime(2026, 10, 3) + timedelta(days=90), actor="test")
        assert edge["clawbacks"] == 1 and edge["amount_pence"] == -790
        uncleared = add_payment(oid, "monthly", 7_900, trail_month=2)
        assert c.on_refund(db, o, db.get(FranchisePayment, uncleared), refunded_at=datetime(2026, 11, 1))["clawbacks"] == 0
        db.rollback()

    # the button on the seller page claws back by hand, with a reason, and never twice
    detail = admin_client.get(f"/sellers/{sid}")
    assert "Clawback</span> of #" in detail.text and "clawed back" in detail.text and "£2,600.00" in detail.text and "-£2,600.00" in detail.text
    r = admin_client.post(f"/sellers/{sid}/entries/{rows[1].id}/clawback", data={"reason": "Fee written off after a dispute"})
    assert r.status_code == 303 and "Clawed back £7.90 from entry" in flash_of(r.headers["location"])["ok"]
    r = admin_client.post(f"/sellers/{sid}/entries/{rows[1].id}/clawback", data={"reason": "again"})
    assert "already clawed back" in flash_of(r.headers["location"])["ok"]
    assert admin_client.post(f"/sellers/{sid}/entries/{rows[1].id + 99}/clawback", data={"reason": "x"}).status_code == 404
    assert "Fee written off after a dispute" in admin_client.get(f"/sellers/{sid}").text
    with session() as db:
        assert c.seller_totals(db, sid)["cleared"] == 0 and len(entries_for(oid)) == 4


# ---- statements --------------------------------------------------------------------------------------------------
def _ledger_for_statements(admin_client) -> tuple[int, int, int]:
    """Sam (self-billing agreed) and Olly (no agreement) each sell a territory in September 2026; Sam also earns a
    trail month in October. Returns (sam, olly, sam's opportunity)."""
    seed_districts()
    sam = make_seller(admin_client, "sam@example.test", "SAM1")
    olly = make_seller(admin_client, "olly@example.test", "OLLY", kind="outsider", name="Olly Outsider", self_billing="")
    sam_oid = sold_territory(admin_client, "SAM1", buyer="bea@example.test")
    olly_oid = sold_territory(admin_client, "OLLY", buyer="cal@example.test", districts=["SP9"])
    for oid, when in ((sam_oid, datetime(2026, 9, 5)), (olly_oid, datetime(2026, 9, 12))):
        with session() as db:
            db.get(FranchisePayment, upfront_payment_id(oid)).cleared_at = when
            db.commit()
        accrue_payment(oid, upfront_payment_id(oid))
    accrue_payment(sam_oid, add_payment(sam_oid, "monthly", 7_900, trail_month=1, cleared=datetime(2026, 10, 3)))
    return sam, olly, sam_oid


def test_statements_collect_unpaid_entries_per_seller_and_regenerate_without_duplicating(admin_client):
    sam, olly, sam_oid = _ledger_for_statements(admin_client)
    with session() as db:
        run = c.generate_statements(db, "2026-09", actor="test", as_of=GENERATED_ON)
        db.commit()
        assert len(run.generated) == 1 and run.regenerated == [] and run.paid_already == []
        assert [(s.id, n, pence) for s, n, pence in run.held] == [(olly, 1, 105_000)], "no self-billing agreement: held, not paid"
        statement = run.generated[0]
        assert statement.seller_id == sam and (statement.period_start, statement.period_end) == SEPT and statement.generated_at == GENERATED_ON
        assert statement.total_pence == 260_000, "September holds the upfront commission; October's trail month waits for October's statement"
        assert run.line().startswith("September 2026: 1 statement generated, 0 regenerated, 1 seller held")
        sid = statement.id
    rows = entries_for(sam_oid)
    assert [(e.basis, e.statement_id) for e in rows] == [("upfront", sid), ("monthly", None)]
    generated = audit_rows("statement.generate")
    assert len(generated) == 1 and generated[0].entity == f"statement:{sid}" and generated[0].after["total_pence"] == 260_000 and generated[0].after["entries_added"] == 1

    # the same period again: the same statement, the same total, nothing attached twice
    with session() as db:
        again = c.generate_statements(db, SEPT, actor="test", as_of=GENERATED_ON + timedelta(days=1))
        db.commit()
        assert again.generated == [] and [s.id for s in again.regenerated] == [sid] and again.regenerated[0].total_pence == 260_000
        assert db.query(Statement).count() == 1 and len(audit_rows("statement.regenerate")) == 1
    assert [(e.basis, e.statement_id) for e in entries_for(sam_oid)] == [("upfront", sid), ("monthly", None)]

    # September is paid; the buyer is then refunded. October's statement takes the trail month and the clawback, which
    # joins it even though it is dated after the period, because it was written before generation day
    with session() as db:
        with pytest.raises(c.CommissionError, match="rule the founder has not confirmed"):
            c.mark_paid(db, db.get(Statement, sid), datetime(2026, 10, 18), actor="test")
        db.rollback()
    confirm_rules(admin_client)
    with session() as db:
        c.mark_paid(db, db.get(Statement, sid), datetime(2026, 10, 18), actor="test")
        db.commit()
    with session() as db:
        o = db.get(FranchiseOpportunity, sam_oid)
        up = db.get(FranchisePayment, upfront_payment_id(sam_oid))
        up.refunded_at = datetime(2026, 11, 2)
        assert c.on_refund(db, o, up, actor="test")["clawbacks"] == 1
        october = c.generate_statements(db, "2026-10", actor="test", as_of=datetime(2026, 11, 10, 3, 15))
        db.commit()
        assert len(october.generated) == 1
        oct_id = october.generated[0].id
        assert october.generated[0].total_pence == 790 - 260_000 == -259_210
    assert [(e.basis, e.clawback_of is not None, e.statement_id) for e in entries_for(sam_oid)] == [("upfront", False, sid), ("monthly", False, oct_id), ("upfront", True, oct_id)]

    # a negative statement cannot be paid; it carries forward into the next period and nets against new entries
    with session() as db:
        with pytest.raises(c.CommissionError, match="carries forward"):
            c.mark_paid(db, db.get(Statement, oct_id), actor="test")
        db.rollback()
    accrue_payment(sam_oid, add_payment(sam_oid, "monthly", 7_900, trail_month=2, cleared=datetime(2026, 11, 3)))
    with session() as db:
        november = c.generate_statements(db, "2026-11", actor="test", as_of=datetime(2026, 12, 10))
        db.commit()
        nov = november.generated[0]
        assert nov.total_pence == -259_210 + 790 == -258_420 and db.query(Statement).filter(Statement.id == oct_id).first() is None
        assert db.query(CommissionEntry).filter(CommissionEntry.statement_id == nov.id).count() == 3
    carried = audit_rows("statement.carry_forward")
    assert len(carried) == 1 and carried[0].entity == f"statement:{oct_id}" and carried[0].after["into_statement_id"] == nov.id

    # once Olly signs the self-billing agreement, a regeneration of September picks the held entry up
    r = admin_client.post(f"/sellers/{olly}", data={"kind": "outsider", "code": "OLLY", "bank_reference": "ref", "self_billing": "1",
                                                   "self_billing_date": "2026-10-01", "active": "1"})
    assert r.status_code == 303
    with session() as db:
        rerun = c.generate_statements(db, "2026-09", actor="test", as_of=GENERATED_ON + timedelta(days=3))
        db.commit()
        assert [s.seller_id for s in rerun.generated] == [olly] and rerun.generated[0].total_pence == 105_000 and rerun.held == []
        assert [s.id for s in rerun.paid_already] == [sid] and rerun.regenerated == []
        assert db.query(Statement).count() == 3


def test_a_clawback_joins_the_unpaid_statement_that_holds_the_original(admin_client):
    sam, olly, sam_oid = _ledger_for_statements(admin_client)
    with session() as db:
        run = c.generate_statements(db, "2026-09", actor="test", as_of=GENERATED_ON)
        db.commit()
        sid = run.generated[0].id
    original = entries_for(sam_oid)[0]
    r = admin_client.post(f"/sellers/{sam}/entries/{original.id}/clawback", data={"reason": "Refunded before the statement was paid"})
    assert r.status_code == 303 and f"on statement #{sid}" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        statement = db.get(Statement, sid)
        assert statement.total_pence == 0, "the statement nets to nothing"
        claw = db.query(CommissionEntry).filter(CommissionEntry.clawback_of == original.id).one()
        assert claw.statement_id == sid and claw.amount_pence == -260_000
        with pytest.raises(c.CommissionError, match="Nothing to pay"):
            c.mark_paid(db, statement, actor="test")
        db.rollback()
    assert "carries forward" in admin_client.get(f"/sellers/statements/{sid}").text
    # the empty month carries forward into October, which then holds the trail month and the two netted lines
    with session() as db:
        october = c.generate_statements(db, "2026-10", actor="test", as_of=datetime(2026, 11, 10))
        db.commit()
        oct_id = october.generated[0].id
        assert october.generated[0].total_pence == 790 and db.get(Statement, sid) is None
        assert sorted(e.amount_pence for e in db.query(CommissionEntry).filter(CommissionEntry.statement_id == oct_id).all()) == [-260_000, 790, 260_000]
    page = admin_client.get(f"/sellers/statements/{oct_id}")
    assert "£7.90" in page.text and "Refunded before the statement was paid" in page.text
    assert "Not payable yet" in page.text and "Mark paid" not in page.text, "unconfirmed rules: the button is withheld"


def test_marking_a_statement_paid_stamps_its_entries_and_never_twice(admin_client):
    sam, olly, sam_oid = _ledger_for_statements(admin_client)
    r = admin_client.post("/sellers/statements/generate", data={"period": "2026-09"})
    assert r.status_code == 303
    ok = flash_of(r.headers["location"])["ok"]
    assert "1 statement generated" in ok and "OLLY held (1 entry, £1,050.00): no self-billing agreement on record." in ok
    with session() as db:
        statement = db.query(Statement).one()
        sid = statement.id
        assert statement.total_pence == 260_000 and statement.generated_at.date() == TODAY
    assert audit_rows("statement.generate")[0].actor == ADMIN_EMAIL

    page = admin_client.get(f"/sellers/statements/{sid}")
    assert page.status_code == 200 and "£2,600.00" in page.text and "[CONFIRM]" in page.text
    assert "Not payable yet" in page.text and "Mark paid" not in page.text, "a proposal cannot be paid"
    assert "Pay by 20 Oct 2026" in page.text and "Sort 00-00-00 acct 12345678" in page.text
    printable = admin_client.get(f"/sellers/statements/{sid}/print")
    assert printable.status_code == 200 and "DRAFT [CONFIRM]" in printable.text and "Draft." in printable.text
    assert "Commission statement: September 2026" in printable.text and "self-billing basis" in printable.text and "£2,600.00 [CONFIRM]" in printable.text
    assert "Hello New Home 2 Limited is not VAT-registered" in printable.text and "window.print()" in printable.text

    # refused while the rules are unconfirmed, whatever the date
    r = admin_client.post(f"/sellers/statements/{sid}/paid", data={"paid_at": "2026-10-18"})
    assert r.status_code == 303 and "1 line on this statement was earned under a commission rule the founder has not confirmed" in flash_of(r.headers["location"])["err"]
    with session() as db:
        assert db.get(Statement, sid).paid_at is None
    confirm_rules(admin_client)
    page = admin_client.get(f"/sellers/statements/{sid}")
    assert "Mark paid" in page.text and "Not payable yet" not in page.text

    # refused: a future date, a date before the period ended, a bad date
    for bad, words in ((TODAY + timedelta(days=1)).isoformat(), "in the future"), ("2026-09-15", "before the period ended"), ("yesterday", "does not look like a date"):
        r = admin_client.post(f"/sellers/statements/{sid}/paid", data={"paid_at": bad})
        assert r.status_code == 303 and words in flash_of(r.headers["location"])["err"], bad
    r = admin_client.post(f"/sellers/statements/{sid}/paid", data={"paid_at": "2026-10-18"})
    assert r.status_code == 303 and "marked paid: £2,600.00 to SAM1 on 18 Oct 2026" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        statement = db.get(Statement, sid)
        assert statement.paid_at == datetime(2026, 10, 18)
        paid = [e for e in db.query(CommissionEntry).all() if e.paid_at is not None]
        assert [(e.basis, e.statement_id, e.paid_at) for e in paid] == [("upfront", sid, datetime(2026, 10, 18))]
        assert c.seller_totals(db, sam) == {"earned": 260_790, "clawed_back": 0, "cleared": 260_790, "paid": 260_000, "owed": 790, "on_statement": 0, "entries": 2}
    assert audit_rows("statement.paid")[0].after["entries"] == 1
    r = admin_client.post(f"/sellers/statements/{sid}/paid", data={"paid_at": "2026-10-19"})
    assert "already paid on 18 October 2026" in flash_of(r.headers["location"])["err"]
    page = admin_client.get(f"/sellers/statements/{sid}")
    assert "Paid on 18 Oct 2026" in page.text and "Mark paid" not in page.text
    assert "paid 18 Oct 2026" in admin_client.get(f"/sellers/{sam}").text

    # regenerating a paid period touches nothing; the next period carries on from the unpaid trail entry
    with session() as db:
        rerun = c.generate_statements(db, "2026-09", actor="test")
        assert rerun.statements == [] and [s.id for s in rerun.paid_already] == [sid]
        db.rollback()
    r = admin_client.post("/sellers/statements/generate", data={"period": "2026-10"})
    ok = flash_of(r.headers["location"])["ok"]
    assert "1 statement generated" in ok
    if TODAY <= date(2026, 10, 31):
        assert "October 2026 has not ended" in ok and "generate it again on the 10th before paying it" in ok
    with session() as db:
        october = db.query(Statement).filter(Statement.period_start == date(2026, 10, 1)).one()
        assert october.total_pence == 790 and october.paid_at is None
    listing = admin_client.get("/sellers/statements")
    assert listing.status_code == 200 and "2 statements" in listing.text and "£7.90 on unpaid statements" in listing.text and "OLLY" in listing.text
    assert "none: held" in listing.text
    assert "1 statement" in admin_client.get("/sellers/statements", params={"state": "paid"}).text
    r = admin_client.post("/sellers/statements/generate", data={"period": "2099-01"})
    assert "has not started" in flash_of(r.headers["location"])["err"]
    r = admin_client.post("/sellers/statements/generate", data={"period": "nope"})
    assert "YYYY-MM" in flash_of(r.headers["location"])["err"]
    r = admin_client.post("/sellers/statements/generate", data={"period": "2025-01"})
    assert "nothing to put on a statement" in flash_of(r.headers["location"])["ok"]


def test_the_nightly_job_runs_on_the_tenth_or_for_a_named_period(admin_client):
    sam, olly, sam_oid = _ledger_for_statements(admin_client)
    out = StringIO()
    assert statements_job.main(["--today", "2026-10-09", "--dry-run"], out=out) == 0
    assert "is not the 10th" in out.getvalue()
    with session() as db:
        assert db.query(Statement).count() == 0

    out = StringIO()
    assert statements_job.main(["--today", "2026-10-10", "--dry-run"], out=out) == 0
    text = out.getvalue()
    assert "would generate" in text and "DRY RUN: September 2026: 1 statement generated" in text and "held" in text and "OLLY" in text
    with session() as db:
        assert db.query(Statement).count() == 0, "a dry run writes nothing"
        assert db.query(CommissionEntry).filter(CommissionEntry.statement_id.isnot(None)).count() == 0

    out = StringIO()
    assert statements_job.main(["--period", "2026-09"], out=out) == 0
    text = out.getvalue()
    assert text.lstrip().startswith("generated") and "statement #" in text and "£2,600.00" in text
    assert "September 2026: 1 statement generated, 0 regenerated, 1 seller held" in text
    with session() as db:
        statement = db.query(Statement).one()
        assert statement.total_pence == 260_000 and statement.seller_id == sam
    assert audit_rows("statement.generate")[0].actor == "job:statements"

    out = StringIO()
    assert statements_job.main(["--period", "2026-09"], out=out) == 0 and "recomputed" in out.getvalue()
    with session() as db:
        assert db.query(Statement).count() == 1
    assert statements_job.main(["--period", "2026-9x"], out=StringIO()) == 1
    assert statements_job.main(["--period", "2099-01"], out=StringIO()) == 1
    with pytest.raises(SystemExit):
        statements_job.main(["--today", "2026-10-10"], out=StringIO())
    with pytest.raises(SystemExit):
        statements_job.main(["--today", "soon", "--dry-run"], out=StringIO())


# ---- the register ----------------------------------------------------------------------------------------------------
def test_seller_register_creates_updates_and_refuses_bad_input(admin_client, client):
    assert client.get("/sellers").status_code == 303 and client.get("/sellers/statements").status_code == 303, "admin only"
    empty = admin_client.get("/sellers")
    assert empty.status_code == 200 and "No sellers yet" in empty.text and "[CONFIRM]" in empty.text and "confirm=false" in empty.text
    form = admin_client.get("/sellers/new")
    assert form.status_code == 200 and "generated if blank" in form.text

    # from an inline email: the person is created, the code generated from the initials, self-billing recorded
    r = admin_client.post("/sellers/new", data={"email": " Jo.Andover@Example.test ", "name": "Jo Andover", "kind": "franchisee", "code": "",
                                               "bank_reference": "Jo's account", "self_billing": "1", "self_billing_date": "2026-09-01", "active": "1"})
    assert r.status_code == 303
    sid = id_of(r.headers["location"].split("?")[0])
    s = seller_row(sid)
    assert s.code.startswith("JA-") and len(s.code) == 7 and s.kind == "franchisee" and s.bank_reference == "Jo's account" and s.active
    assert s.self_billing_agreed_at == datetime(2026, 9, 1) and s.person.person_key == "jo.andover@example.test" and s.person.name == "Jo Andover"
    assert f"partners-programme?seller={s.code}" in flash_of(r.headers["location"])["ok"]
    created = audit_rows("seller.create")
    assert len(created) == 1 and created[0].entity == f"seller:{sid}" and created[0].after["code"] == s.code and len(audit_rows("person.create")) == 1
    detail = admin_client.get(f"/sellers/{sid}")
    assert detail.status_code == 200 and f"?seller={s.code}" in detail.text and "No entries yet" in detail.text and "Franchisee" in detail.text
    assert "No self-billing agreement" not in detail.text and "v1 for franchisee sellers" in detail.text

    # from the register, with a typed code; the same person twice and a taken code are refused
    pid = create_person(admin_client, "sam@example.test", "Sam Seller")
    r = admin_client.post("/sellers/new", data={"person_id": str(pid), "kind": "mentee", "code": "sam1", "self_billing": "", "active": "1"})
    assert r.status_code == 303
    sam = seller_row(id_of(r.headers["location"].split("?")[0]))
    assert sam.code == "sam1" and sam.self_billing_agreed_at is None and sam.person_id == pid
    assert "No self-billing agreement is on record" in admin_client.get(f"/sellers/{sam.id}").text
    cases = [
        ({"person_id": str(pid), "kind": "mentee"}, "already seller sam1"),
        ({"email": "jo.andover@example.test", "kind": "mentee"}, "already seller"),
        ({"email": "new@example.test", "kind": "mentee", "code": "SAM1"}, "already in use"),
        ({"email": "new@example.test", "kind": "mentee", "code": "bad code!"}, "letters, digits, dashes and underscores"),
        ({"email": "new@example.test", "kind": "partner"}, "what kind of seller"),
        ({"email": "not an email", "kind": "mentee"}, "does not look right"),
        ({"person_id": "", "email": "", "kind": "mentee"}, "Choose a person from the register or enter"),
        ({"person_id": "9999", "kind": "mentee"}, "not on the register"),
        ({"email": "new@example.test", "kind": "mentee", "self_billing": "1", "self_billing_date": ""}, "or untick it"),
        ({"email": "new@example.test", "kind": "mentee", "self_billing": "1", "self_billing_date": (TODAY + timedelta(days=2)).isoformat()}, "in the future"),
    ]
    for data, words in cases:
        r = admin_client.post("/sellers/new", data=data)
        assert r.status_code == 400 and words in r.text, (data, r.text[:300])
    with session() as db:
        assert db.query(Seller).count() == 2 and db.query(Person).count() == 2

    # the list filters and searches; the detail form updates with an audit of only the change
    assert "2 sellers" in admin_client.get("/sellers").text
    assert "1 seller" in admin_client.get("/sellers", params={"kind": "mentee"}).text and "sam1" in admin_client.get("/sellers", params={"q": "sam"}).text
    assert "No sellers match" in admin_client.get("/sellers", params={"q": "nobody"}).text
    r = admin_client.post(f"/sellers/{sam.id}", data={"kind": "outsider", "code": "SAM1", "bank_reference": "ref", "self_billing": "1",
                                                     "self_billing_date": "2026-10-01", "active": ""})
    assert r.status_code == 303 and "Seller saved" in flash_of(r.headers["location"])["ok"] and "new kind applies to the next sale" in flash_of(r.headers["location"])["ok"]
    sam = seller_row(sam.id)
    assert sam.kind == "outsider" and sam.code == "SAM1" and sam.bank_reference == "ref" and sam.self_billing_agreed_at == datetime(2026, 10, 1) and not sam.active
    rows = audit_rows("seller.update")
    assert rows[0].before == {"kind": "mentee", "code": "sam1", "bank_reference": "", "self_billing_agreed_at": None, "active": True}
    assert set(rows[0].after) == {"kind", "code", "bank_reference", "self_billing_agreed_at", "active"}
    assert "No changes to save" in flash_of(admin_client.post(f"/sellers/{sam.id}", data={"kind": "outsider", "code": "SAM1", "bank_reference": "ref", "self_billing": "1",
                                                                                           "self_billing_date": "2026-10-01", "active": ""}).headers["location"])["ok"]
    r = admin_client.post(f"/sellers/{sam.id}", data={"kind": "outsider", "code": s.code, "bank_reference": "", "self_billing": "", "active": "1"})
    assert r.status_code == 400 and "already in use" in r.text
    assert admin_client.get("/sellers/9999").status_code == 404
    assert "Not active" in admin_client.get("/sellers", params={"active": "0"}).text and "SAM1" in admin_client.get("/sellers", params={"active": "0"}).text
    # an inactive seller is refused on a new opportunity (the pipeline's rule) but keeps what they earned
    seed_districts()
    r = admin_client.post("/franchise/new", data={"email": "buyer@example.test", "districts": ["SP10"], "seller_code": "SAM1"})
    assert r.status_code == 400 and "not active" in r.text


def test_confirming_the_rules_removes_the_badges(admin_client):
    seed_districts()
    sid = make_seller(admin_client, "sam@example.test", "SAM1")
    oid = sold_territory(admin_client, "SAM1")
    with session() as db:
        db.get(FranchisePayment, upfront_payment_id(oid)).cleared_at = datetime(2026, 9, 5)
        db.commit()
    accrue_payment(oid, upfront_payment_id(oid))
    with session() as db:
        assert not c.rules_confirmed(db)
        rules = {r.seller_kind: r.id for r in db.query(CommissionRule).all()}
    assert len(rules) == 3
    page = admin_client.get("/sellers")
    assert page.text.count('class="confirm"') >= 4 and "Confirm</button>" in page.text and "proposal" in page.text

    r = admin_client.post(f"/sellers/rules/{rules['mentee']}/confirm")
    assert r.status_code == 303 and "confirmed by founder@example.test. 2 rules still to confirm." in flash_of(r.headers["location"])["ok"]
    with session() as db:
        rule = db.get(CommissionRule, rules["mentee"])
        assert rule.confirmed and rule.note.startswith(f"Confirmed by {ADMIN_EMAIL} on {TODAY:%d %B %Y}. Proposal from the plan")
        assert "confirm=false" not in rule.note and not c.rules_confirmed(db)
    row = audit_rows("commission.rule_confirmed")[0]
    assert row.entity == f"commission_rule:{rules['mentee']}" and row.before["confirmed"] is False and row.after["confirmed"] is True
    assert "was already confirmed" in flash_of(admin_client.post(f"/sellers/rules/{rules['mentee']}/confirm").headers["location"])["ok"]
    assert admin_client.post("/sellers/rules/999/confirm").status_code == 404
    for kind in ("franchisee", "outsider"):
        r = admin_client.post(f"/sellers/rules/{rules[kind]}/confirm")
        assert r.status_code == 303
    assert "Every rule is now confirmed" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        assert c.rules_confirmed(db)
    for path in ("/sellers", f"/sellers/{sid}", "/sellers/statements"):
        page = admin_client.get(path)
        assert page.status_code == 200 and 'class="confirm"' not in page.text and '<div class="confirm-banner">' not in page.text, path
    with session() as db:
        c.generate_statements(db, "2026-09", actor="test")
        db.commit()
        st = db.query(Statement).one()
    printable = admin_client.get(f"/sellers/statements/{st.id}/print")
    assert "DRAFT [CONFIRM]" not in printable.text and "Draft." not in printable.text and "£2,600.00 [CONFIRM]" not in printable.text
    assert "[CONFIRM: self-billing agreement text" in printable.text, "the agreement text itself is still the solicitor's to confirm"
    # a later accrual records that it was earned under a confirmed rule
    result = accrue_payment(oid, add_payment(oid, "monthly", 7_900, trail_month=1, cleared=datetime(2026, 10, 3)))
    assert result["confirmed"] is True
    # a rule whose flag is set but whose note still opens confirm=false is not confirmed
    with session() as db:
        rule = db.get(CommissionRule, rules["outsider"])
        rule.note = "confirm=false. Reopened."
        db.commit()
        assert not c.rule_is_confirmed(rule) and not c.rules_confirmed(db)


def test_audit_rows_carry_no_more_than_the_change(admin_client):
    make_seller(admin_client, "sam@example.test", "SAM1")
    with session() as db:
        rows = db.query(AuditLog).filter(AuditLog.action.like("seller.%")).all()
        assert all(row.before is None for row in rows) and all("code" in row.after for row in rows)
