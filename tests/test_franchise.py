"""The franchise pipeline: an opportunity created from a person (or a person made inline) with districts, a
band and a quote copied from the territory model; seller attribution by tracked-link code; stage changes that
are audited and that create the day 2, 5, 9 and 14 follow-ups at pack_sent and the upfront payment at paid;
the commission hook with and without the ledger; the packs, which carry a [CONFIRM] watermark and no money
until every assumption is confirmed; the follow-up emails, rendered for copying and sent only through SMTP;
and the Hello New Home hand-off through the connector in dry run and live."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import respx

from app import config
from app.models import (Assumption, CommissionEntry, CommissionRule, ConnectorCall, FranchiseOpportunity, FranchisePayment, Person,
                        PostcodeDistrict, Seller, Task, utcnow)
from app.routers import franchise as franchise_router
from app.routers.franchise import LEDGER_MISSING, highest_band, quote_for
from app.services import followups, packs
from app.services import territory_rules as tr
from app.services.packs import markdown_to_html
from tests.conftest import ADMIN_EMAIL, session
from tests.test_mentees import enrol
from tests.test_people import audit_rows, create_person, flash_of, id_of

TODAY = utcnow().date()
HNH = "https://hnh.example.test"

# (district, area, nation, la_gss, la_name, prs_households, rent_2bed, state, partner_id)
DISTRICTS = [
    ("SP10", "SP", "england", "E07000093", "Test Valley", 3500, "1200.00", "open", None),
    ("SP11", "SP", "england", "E07000093", "Test Valley", 3200, "1200.00", "open", None),
    ("SP9", "SP", "england", "E06000054", "Wiltshire", 900, "950.00", "reserved", None),
    ("DH7", "DH", "england", "E06000047", "County Durham", None, None, "open", None),
    ("SA1", "SA", "wales", "W06000011", "Swansea", 8000, "838.00", "hq", None),
    ("SE1", "SE", "england", "E09000028", "Southwark", 20000, "2332.00", "core", 5),
]


def seed_districts() -> None:
    with session() as db:
        for district, area, nation, gss, la, prs, rent, state, partner in DISTRICTS:
            modelled = tr.model_district(district=district, la_name=la, prs_households=prs, rent_2bed=rent, la_gss=gss)
            db.add(PostcodeDistrict(
                district=district, area=area, nation=nation, la_gss=gss, la_name=la, dwellings=(prs * 5) if prs else None,
                prs_households=prs, prs_share=Decimal("0.2000") if prs else None, rent_2bed=Decimal(rent) if rent else None,
                rent_1bed=Decimal(rent) - 300 if rent else None, rent_3bed=Decimal(rent) + 300 if rent else None,
                rent_date=date(2026, 6, 1) if rent else None, licensing="mandatory_only", licensing_note="No additional scheme.",
                band=modelled["band"], achievable_fee=modelled["achievable_fee"], cost_to_serve=modelled["cost_to_serve"],
                units_y1=modelled["units_y1"], units_y2=modelled["units_y2"], units_y3=modelled["units_y3"],
                partner_net_y1=modelled["partner_net_y1"], hq_contribution_y1=modelled["hq_contribution_y1"], state=state, partner_id=partner,
            ))
        db.commit()


def confirm_everything() -> None:
    with session() as db:
        tr.ensure_assumptions(db)
        db.flush()                      # the session does not autoflush, so the new rows must be written before the query
        for row in db.query(Assumption).all():
            row.confirmed = True
            row.confirmed_at = utcnow()
        db.commit()


def create_opportunity(c, districts: list[str], *, person_id: int | None = None, email: str = "", name: str = "", phone: str = "",
                       seller_code: str = "", source: str = "", fee_waived: str = "", notes: str = "") -> int:
    data = {"person_id": str(person_id) if person_id else "", "email": email, "name": name, "phone": phone, "districts": districts,
            "seller_code": seller_code, "source": source, "fee_waived": fee_waived, "notes": notes}
    r = c.post("/franchise/new", data=data)
    assert r.status_code == 303, r.text[:600]
    return id_of(r.headers["location"].split("?")[0])


def move(c, oid: int, stage: str, note: str = "", *, override: bool = False, paid_at: str = ""):
    data = {"stage": stage, "note": note, "paid_at": paid_at}
    if override:
        data["override"] = "1"
    return c.post(f"/franchise/{oid}/stage", data=data)


def pay_and_clear(c, oid: int, amount: str = "13000.00", *, paid_at: date | None = None, cleared_at: date | None = None) -> int:
    """Record the upfront fee through the payments route, cleared on the same day, and return the payment id."""
    paid_at = paid_at or TODAY
    r = c.post(f"/franchise/{oid}/payments", data={"basis": "upfront", "amount": amount, "paid_at": paid_at.isoformat(),
                                                   "cleared_at": (cleared_at or paid_at).isoformat(), "method": "manual"})
    assert r.status_code == 303, r.text[:600]
    with session() as db:
        return db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == oid, FranchisePayment.basis == "upfront").one().id


def make_seller(email: str, code: str, kind: str = "mentee", active: bool = True) -> int:
    with session() as db:
        person = Person(person_key=email, name="Sam Seller")
        db.add(person)
        db.flush()
        seller = Seller(person_id=person.id, kind=kind, code=code, active=active)
        db.add(seller)
        db.commit()
        return seller.id


def load(oid: int) -> FranchiseOpportunity:
    with session() as db:
        o = db.get(FranchiseOpportunity, oid)
        _ = o.person, o.activities, o.tasks, o.seller
        return o


# ---- creation and the quote --------------------------------------------------------------------------------
def test_create_from_an_inline_person_quotes_the_territory(admin_client):
    seed_districts()
    page = admin_client.get("/franchise/new")
    assert page.status_code == 200 and 'value="SP10"' in page.text and 'value="SP9"' in page.text
    assert 'value="SA1"' not in page.text and 'value="SE1"' not in page.text, "HQ and core districts are not offered"
    assert "[CONFIRM]" in page.text

    oid = create_opportunity(admin_client, ["SP10", "sp11"], email=" Jo.Andover@Example.test ", name="Jo Andover", phone="07000 000000",
                             source="web form", notes="Met at the Swansea meetup.")
    o = load(oid)
    assert o.stage == "enquiry" and o.districts == ["SP10", "SP11"] and o.band == "town"
    assert o.quoted_upfront_pence == 1_300_000, "the plan's worked check: 30 x 144 x 50% = 2,160; 6 x 2,160 to the nearest 500 is 13,000"
    assert o.quoted_monthly_pence == 7_900 and o.source == "web form" and o.seller_id is None and o.notes == "Met at the Swansea meetup."
    assert o.person.person_key == "jo.andover@example.test" and o.person.name == "Jo Andover" and o.person.phone == "07000 000000"
    notes = [a.body for a in o.activities if a.kind == "note"]
    assert any(n.startswith("Quoted: band Town, upfront £13,000.00, monthly £79.00, for SP10, SP11") and "[CONFIRM]" in n for n in notes)
    assert any("weighted by private-rented households" in n for n in notes)
    created = audit_rows("franchise.create")
    assert len(created) == 1 and created[0].entity == f"opportunity:{oid}" and created[0].after["quoted_upfront_pence"] == 1_300_000
    assert created[0].actor == ADMIN_EMAIL and len(audit_rows("person.create")) == 1

    detail = admin_client.get(f"/franchise/{oid}")
    assert detail.status_code == 200
    assert "Jo Andover" in detail.text and "£13,000.00" in detail.text and "£79.00" in detail.text and "Town" in detail.text
    assert "[CONFIRM]" in detail.text and "stage-enquiry" in detail.text and "Test Valley" in detail.text
    board = admin_client.get("/franchise")
    assert board.status_code == 200 and f'href="/franchise/{oid}"' in board.text and "Jo Andover" in board.text
    listing = admin_client.get("/franchise/list", params={"q": "sp11"})
    assert "1 opportunity" in listing.text and "Jo Andover" in listing.text
    assert "No opportunities match" in admin_client.get("/franchise/list", params={"q": "nobody"}).text
    assert admin_client.get("/franchise/9999").status_code == 404

    # creating again for the same address reuses the person rather than making a second one
    second = admin_client.post("/franchise/new", data={"email": "jo.andover@example.test", "districts": ["SP9"]})
    assert second.status_code == 303 and "already on the register" in flash_of(second.headers["location"])["ok"]
    with session() as db:
        assert db.query(Person).count() == 1 and db.query(FranchiseOpportunity).count() == 2


def test_single_district_quote_matches_the_model_and_a_mixed_band_is_weighted(admin_client):
    seed_districts()
    jo = create_person(admin_client, "jo@example.test", "Jo")
    one = load(create_opportunity(admin_client, ["SP10"], person_id=jo))
    modelled = tr.model_district(district="SP10", la_name="Test Valley", prs_households=3500, rent_2bed="1200.00")
    assert one.quoted_upfront_pence == packs.to_pence(modelled["upfront_fee"]) == 1_300_000
    assert one.quoted_monthly_pence == packs.to_pence(modelled["monthly_fee"]) == 7_900

    mixed = load(create_opportunity(admin_client, ["SP9", "SP10"], person_id=jo))
    assert mixed.band == "town", "the highest band among the districts wins"
    a = tr.DEFAULT_VALUES
    fee = tr.money((Decimal("114.00") * 900 + Decimal("144.00") * 3500) / 4400)
    at_30 = tr.partner_net_month(30, fee, a["partner_split_own"], 0, 1)
    expected = tr.upfront_fee(at_30, a["upfront_multiplier"], tr.upfront_floor("town"), round_to_step=a["upfront_round_to"])
    assert mixed.quoted_upfront_pence == packs.to_pence(expected) == 1_250_000
    assert mixed.quoted_monthly_pence == 7_900

    with session() as db:
        rows = [db.get(PostcodeDistrict, "DH7")]
        q = quote_for(rows, tr.load_assumptions(db))
    assert q["band"] == "rural" and q["fee"] is None and q["floor_applied"] and q["upfront"] == Decimal("3000.00") and q["monthly"] == Decimal("49.00")
    floor_only = load(create_opportunity(admin_client, ["DH7"], person_id=jo))
    assert floor_only.quoted_upfront_pence == 300_000 and floor_only.quoted_monthly_pence == 4_900
    assert any("the band floor applies" in x.body for x in floor_only.activities)
    assert highest_band(["rural", "london", "town"]) == "london" and highest_band([]) == "rural"


def test_create_refuses_bad_input(admin_client):
    seed_districts()
    jo = create_person(admin_client, "jo@example.test", "Jo")
    make_seller("sam@example.test", "SAM1")
    make_seller("dormant@example.test", "OLD1", active=False)
    cases = [
        ({"person_id": str(jo), "districts": []}, "at least one postcode district"),
        ({"person_id": str(jo), "districts": ["ZZ9"]}, "Not in the territory model: ZZ9"),
        ({"person_id": str(jo), "districts": ["SA1", "SP10"]}, "Not available: SA1"),
        ({"person_id": str(jo), "districts": ["SE1"]}, "Not available: SE1"),
        ({"person_id": "", "email": "", "districts": ["SP10"]}, "Choose a person from the register or enter"),
        ({"person_id": "", "email": "not an email", "districts": ["SP10"]}, "does not look right"),
        ({"person_id": "9999", "districts": ["SP10"]}, "not on the register"),
        ({"person_id": str(jo), "districts": ["SP10"], "seller_code": "NOPE"}, "No seller has the code NOPE"),
        ({"person_id": str(jo), "districts": ["SP10"], "seller_code": "old1"}, "is not active"),
    ]
    for data, words in cases:
        r = admin_client.post("/franchise/new", data=data)
        assert r.status_code == 400 and words in r.text, (data, r.text[:300])
    # a seller cannot sell to themself
    r = admin_client.post("/franchise/new", data={"email": "sam@example.test", "districts": ["SP10"], "seller_code": "SAM1"})
    assert r.status_code == 400 and "own purchase" in r.text
    with session() as db:
        assert db.query(FranchiseOpportunity).count() == 0 and db.query(Person).count() == 3
    assert not audit_rows("franchise.create")
    # the register is admin-only
    assert admin_client.get("/franchise/packs/prospectus").status_code == 200


def test_mentee_fees_are_waived(admin_client):
    seed_districts()
    jo = create_person(admin_client, "jo@example.test", "Jo Mentee")
    enrol(admin_client, jo, "full", TODAY)
    page = admin_client.get("/franchise/new", params={"person_id": jo})
    assert 'name="fee_waived" value="1" checked' in page.text, "an active mentee's fees are waived by default"
    oid = create_opportunity(admin_client, ["SP10", "SP11"], person_id=jo, fee_waived="1")
    o = load(oid)
    assert o.band == "town" and o.quoted_upfront_pence == 0 and o.quoted_monthly_pence == 0
    assert any("fees waived" in a.body for a in o.activities)
    detail = admin_client.get(f"/franchise/{oid}")
    assert "Waived" in detail.text and "£13,000.00" not in detail.text
    r = move(admin_client, oid, "paid")
    assert r.status_code == 303 and "no payment was recorded" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        assert db.query(FranchisePayment).count() == 0
    # the hand-off carries fee_waived from the quote
    r = admin_client.post(f"/franchise/{oid}/hnh", data={"dry_run": "1"})
    assert r.status_code == 200 and "Fee waived</dt><dd>yes" in r.text


# ---- seller attribution ------------------------------------------------------------------------------------
def test_seller_attribution_by_tracked_link_code(admin_client):
    seed_districts()
    sid = make_seller("sam@example.test", "SAM1")
    oid = create_opportunity(admin_client, ["SP10"], email="buyer@example.test", name="Bea Buyer", seller_code="sam1")
    o = load(oid)
    assert o.seller_id == sid and o.seller.code == "SAM1" and o.source == "seller:SAM1", "the code is matched whatever its case"
    assert any("Attributed to seller SAM1" in a.body for a in o.activities)
    assert audit_rows("franchise.create")[0].after["seller_id"] == sid
    listing = admin_client.get("/franchise/list", params={"seller": sid})
    assert "1 opportunity" in listing.text and "Bea Buyer" in listing.text and "<code>SAM1</code>" in listing.text
    assert "No opportunities match" in admin_client.get("/franchise/list", params={"seller": sid + 1}).text
    detail = admin_client.get(f"/franchise/{oid}")
    assert "Seller <code>SAM1</code>" in detail.text and "No entries yet" in detail.text

    # a source given explicitly is kept; the seller code is still attributed
    other = load(create_opportunity(admin_client, ["SP11"], email="second@example.test", seller_code="SAM1", source="Durham meetup"))
    assert other.source == "Durham meetup" and other.seller_id == sid

    # attribution can be corrected afterwards, and is audited
    r = admin_client.post(f"/franchise/{oid}/details", data={"notes": "Sam introduced Bea.", "source": "referral", "seller_code": ""})
    assert r.status_code == 303
    o = load(oid)
    assert o.seller_id is None and o.source == "referral" and o.notes == "Sam introduced Bea."
    rows = audit_rows("franchise.update")
    assert rows[-1].before["seller_id"] == sid and rows[-1].after["seller_id"] is None
    r = admin_client.post(f"/franchise/{oid}/details", data={"notes": "", "source": "", "seller_code": "nobody"})
    assert r.status_code == 400 and "No seller has the code nobody" in r.text
    r = admin_client.post(f"/franchise/{oid}/details", data={"notes": "", "source": "", "seller_code": "SAM1"})
    assert r.status_code == 303 and load(oid).seller_id == sid


# ---- stages, tasks and follow-ups -----------------------------------------------------------------------------
def test_stage_changes_are_audited_and_pack_sent_creates_the_follow_ups(admin_client):
    seed_districts()
    oid = create_opportunity(admin_client, ["SP10", "SP11"], email="jo@example.test", name="Jo Andover")
    before = load(oid).stage_changed_at

    r = move(admin_client, oid, "discovery", "Call booked for Thursday")
    assert r.status_code == 303 and "Moved to Discovery" in flash_of(r.headers["location"])["ok"]
    o = load(oid)
    assert o.stage == "discovery" and o.stage_changed_at >= before
    assert any(a.body == "Stage moved to Discovery: Call booked for Thursday" for a in o.activities)
    rows = audit_rows("franchise.stage")
    assert len(rows) == 1 and rows[0].before["stage"] == "enquiry" and rows[0].after["stage"] == "discovery"
    assert rows[0].after["note"] == "Call booked for Thursday" and rows[0].entity == f"opportunity:{oid}"
    assert "Choose a stage" in flash_of(move(admin_client, oid, "won").headers["location"])["err"]
    assert "Already at Discovery" in flash_of(move(admin_client, oid, "discovery").headers["location"])["ok"]

    r = move(admin_client, oid, "pack_sent")
    assert r.status_code == 303 and "4 follow-up tasks created" in flash_of(r.headers["location"])["ok"]
    o = load(oid)
    tasks = sorted(o.tasks, key=lambda t: t.due_on)
    assert [(t.template_key, t.due_on, t.state) for t in tasks] == [
        ("followup_day2", TODAY + timedelta(days=2), "open"), ("followup_day5", TODAY + timedelta(days=5), "open"),
        ("followup_day9", TODAY + timedelta(days=9), "open"), ("followup_day14", TODAY + timedelta(days=14), "open")]
    assert audit_rows("franchise.stage")[-1].after["tasks_created"] == list(followups.TEMPLATE_KEYS)
    assert any(f"/franchise/{oid}/pack/territory" in a.body for a in o.activities), "the stage change points at the territory report"

    # sending the pack again while the cadence runs adds nothing
    move(admin_client, oid, "follow_up")
    r = move(admin_client, oid, "pack_sent")
    assert "already running" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        assert db.query(Task).filter(Task.opportunity_id == oid).count() == 4

    # done and skipped, with an activity for done
    day2 = tasks[0]
    r = admin_client.post(f"/franchise/{oid}/tasks/{day2.id}/done")
    assert r.status_code == 303 and "Done: Day 2" in flash_of(r.headers["location"])["ok"]
    r = admin_client.post(f"/franchise/{oid}/tasks/{tasks[1].id}/skip")
    assert r.status_code == 303
    o = load(oid)
    states = {t.template_key: t.state for t in o.tasks}
    assert states == {"followup_day2": "done", "followup_day5": "skipped", "followup_day9": "open", "followup_day14": "open"}
    assert any(a.kind == "task_done" and "Day 2" in a.body for a in o.activities)
    assert audit_rows("franchise.task.done")[0].entity == f"task:{day2.id}" and audit_rows("franchise.task.skipped")
    assert "already done" in flash_of(admin_client.post(f"/franchise/{oid}/tasks/{day2.id}/done").headers["location"])["ok"]
    assert admin_client.post(f"/franchise/{oid}/tasks/9999/done").status_code == 404

    # an overdue task shows on the board and the opportunity
    with session() as db:
        db.query(Task).filter(Task.template_key == "followup_day9", Task.opportunity_id == oid).one().due_on = TODAY - timedelta(days=1)
        db.commit()
    board = admin_client.get("/franchise")
    assert "1 overdue" in board.text and "Day 9: answer the usual objections" in board.text
    detail = admin_client.get(f"/franchise/{oid}")
    assert "(overdue)" in detail.text and "is-overdue" in detail.text

    # the timeline takes calls, emails and notes
    r = admin_client.post(f"/franchise/{oid}/activities", data={"kind": "call", "body": "Spoke for twenty minutes; keen on SP11."})
    assert r.status_code == 303
    assert any(a.kind == "call" and "keen on SP11" in a.body for a in load(oid).activities)
    assert admin_client.post(f"/franchise/{oid}/activities", data={"kind": "call", "body": ""}).status_code == 400
    assert admin_client.post(f"/franchise/{oid}/activities", data={"kind": "visit", "body": "x"}).status_code == 400

    # lost and back again is allowed; the quote never moves on its own
    assert move(admin_client, oid, "lost", "Timing wrong").status_code == 303
    assert load(oid).stage == "lost" and load(oid).quoted_upfront_pence == 1_300_000


def test_follow_up_emails_render_without_money_until_confirmed_and_send_only_through_smtp(admin_client, monkeypatch):
    seed_districts()
    oid = create_opportunity(admin_client, ["SP10", "SP11"], email="jo@example.test", name="Jo Andover")
    move(admin_client, oid, "pack_sent")
    detail = admin_client.get(f"/franchise/{oid}")
    assert "Subject: Your Hello New Home territory pack for SP10, SP11" in detail.text
    assert "Hi Jo," in detail.text and "will follow from head office in writing" in detail.text
    assert "SMTP is not configured" in detail.text and "Send email" in detail.text
    day5 = detail.text.split("The numbers behind the SP10, SP11 territory report")[2]
    assert "£" not in day5.split("</textarea>")[0], "the day 5 email carries no figure while the model is unconfirmed"

    for key in followups.TEMPLATE_KEYS:
        email = followups.email_for(key, load(oid), confirmed=False, pack_sent_on=TODAY, today=TODAY)
        assert email is not None and "!" not in email.body and "—" not in email.body and "!" not in email.subject
        assert "Hello New Home" in email.body and "Martin Howard" in email.body
    assert followups.email_for("something_else", load(oid), confirmed=False) is None
    assert "by " + (TODAY + timedelta(days=21)).strftime("%d %B %Y") in followups.email_for("followup_day14", load(oid), confirmed=False, pack_sent_on=TODAY).body

    confirm_everything()
    email = followups.email_for("followup_day5", load(oid), confirmed=True, pack_sent_on=TODAY)
    assert "£13,000.00 upfront and £79.00 a month" in email.body and "The band is town" in email.body

    # not configured: nothing is sent and the task stays open
    task_id = next(t.id for t in load(oid).tasks if t.template_key == "followup_day2")
    r = admin_client.post(f"/franchise/{oid}/tasks/{task_id}/send")
    assert r.status_code == 303 and "not configured" in flash_of(r.headers["location"])["err"]
    assert next(t.state for t in load(oid).tasks if t.id == task_id) == "open"

    # configured: a fake SMTP server records the message, the task is done and the email is on the timeline
    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, message):
            sent.append(("message", message))

    monkeypatch.setattr(followups.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_PORT", 2525)
    monkeypatch.setattr(config, "SMTP_USER", "mailer")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "secret-pass")
    monkeypatch.setattr(config, "MAIL_FROM", "hello@hellonewhome.example")
    assert followups.smtp_configured()
    r = admin_client.post(f"/franchise/{oid}/tasks/{task_id}/send")
    assert r.status_code == 303 and "Sent to jo@example.test" in flash_of(r.headers["location"])["ok"]
    assert sent[0] == ("connect", "smtp.example.test", 2525) and ("starttls",) in sent and ("login", "mailer") in sent
    message = sent[-1][1]
    assert message["To"] == "jo@example.test" and message["From"] == "hello@hellonewhome.example"
    assert message["Subject"] == "Your Hello New Home territory pack for SP10, SP11" and "Hi Jo," in message.get_content()
    o = load(oid)
    assert next(t.state for t in o.tasks if t.id == task_id) == "done"
    assert any(a.kind == "email" and a.body.startswith("Emailed jo@example.test: Your Hello New Home") for a in o.activities)
    row = audit_rows("franchise.email_sent")[0]
    assert row.entity == f"task:{task_id}" and row.after["to"] == "jo@example.test" and "secret-pass" not in json.dumps(row.after)
    assert "already done" in flash_of(admin_client.post(f"/franchise/{oid}/tasks/{task_id}/send").headers["location"])["err"]

    # a refusal by the server reaches the screen and the audit log
    def broken(*a, **kw):
        raise followups.smtplib.SMTPException("relay denied")

    monkeypatch.setattr(followups.smtplib, "SMTP", broken)
    day9 = next(t.id for t in load(oid).tasks if t.template_key == "followup_day9")
    r = admin_client.post(f"/franchise/{oid}/tasks/{day9}/send")
    assert "relay denied" in flash_of(r.headers["location"])["err"]
    assert audit_rows("franchise.email_failed")[0].after["error"] == "relay denied"
    assert next(t.state for t in load(oid).tasks if t.id == day9) == "open"


# ---- paid, payments and the commission hook ----------------------------------------------------------------------
def test_paid_records_the_upfront_fee_and_clearing_it_drives_the_commission_hook(admin_client, monkeypatch):
    seed_districts()
    sid = make_seller("sam@example.test", "SAM1")
    oid = create_opportunity(admin_client, ["SP10", "SP11"], email="bea@example.test", name="Bea Buyer", seller_code="SAM1")

    # Paid needs a cleared upfront fee: without one the move is refused, and an override needs a note and is audited
    r = move(admin_client, oid, "paid")
    assert r.status_code == 303 and "Paid needs a cleared upfront payment" in flash_of(r.headers["location"])["err"]
    assert load(oid).stage == "enquiry" and audit_rows("franchise.stage") == []
    r = move(admin_client, oid, "paid", override=True)
    assert "override needs a note" in flash_of(r.headers["location"])["err"] and load(oid).stage == "enquiry"
    r = move(admin_client, oid, "paid", "Bank transfer promised for Friday", override=True, paid_at="2026-10-19")
    assert r.status_code == 303 and "Upfront payment of £13,000.00 recorded as paid on 19 Oct 2026" in flash_of(r.headers["location"])["ok"]
    assert "Moved without a cleared payment" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        payment = db.query(FranchisePayment).one()
        assert payment.opportunity_id == oid and payment.basis == "upfront" and payment.trail_month is None
        assert payment.amount_pence == 1_300_000 and payment.cleared_at is None and payment.method == "manual"
        assert payment.paid_at == datetime(2026, 10, 19)
    assert audit_rows("franchise.payment.create")[0].after["amount_pence"] == 1_300_000
    stage_row = audit_rows("franchise.stage")[-1]
    assert stage_row.after["payment_id"] == payment.id and stage_row.after["override"] is True and stage_row.after["note"] == "Bank transfer promised for Friday"
    assert any("WITHOUT a cleared payment (override by founder@example.test)" in a.body for a in load(oid).activities)
    r = move(admin_client, oid, "onboarding")
    assert "Onboarding needs a cleared upfront payment" in flash_of(r.headers["location"])["err"] and load(oid).stage == "paid"
    detail = admin_client.get(f"/franchise/{oid}")
    assert "Mark cleared" in detail.text and "£0.00</strong><span>cleared" in detail.text
    assert "the ledger service (app.services.commission) is not installed" not in detail.text or franchise_router.commission_ledger is None
    assert "keeps the quote it was sold on" in flash_of(admin_client.post(f"/franchise/{oid}/requote").headers["location"])["err"]

    # the ledger is absent: clearing leaves a pending note and an audit row, and the payment stays cleared
    monkeypatch.setattr(franchise_router, "commission_ledger", None)
    r = admin_client.post(f"/franchise/{oid}/payments/{payment.id}/clear")
    assert r.status_code == 303 and LEDGER_MISSING in flash_of(r.headers["location"])["ok"]
    with session() as db:
        assert db.get(FranchisePayment, payment.id).cleared_at is not None
    o = load(oid)
    assert any(a.body.startswith("Commission pending") and "not installed" in a.body for a in o.activities)
    pending = audit_rows("franchise.commission_pending")
    assert len(pending) == 1 and pending[0].after == {"payment_id": payment.id, "seller_id": sid, "basis": "upfront",
                                                      "basis_pence": 1_300_000, "reason": "ledger not installed"}
    assert "already cleared" in flash_of(admin_client.post(f"/franchise/{oid}/payments/{payment.id}/clear").headers["location"])["ok"]
    assert "£13,000.00</strong><span>cleared" in admin_client.get(f"/franchise/{oid}").text

    # a second upfront payment is refused; a monthly fee needs its month
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "upfront", "amount": "13000", "paid_at": TODAY.isoformat(), "method": "manual"})
    assert r.status_code == 400 and "already on record" in r.text
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "amount": "79", "paid_at": TODAY.isoformat(), "method": "manual"})
    assert r.status_code == 400 and "month number" in r.text
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "trail_month": "1", "amount": "abc", "paid_at": "", "method": "cheque"})
    assert r.status_code == 400 and "for example 1250.00" in r.text and "paid date is required" in r.text and "how the payment arrived" in r.text

    # the ledger is present: it is called with what its signature names
    calls: list = []

    def on_payment_cleared(db, opportunity, payment, basis_pence, trail_month, actor):
        calls.append((opportunity.id, payment.basis, basis_pence, trail_month, actor))
        return {"entries": 1}

    monkeypatch.setattr(franchise_router, "commission_ledger", SimpleNamespace(on_payment_cleared=on_payment_cleared))
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "trail_month": "1", "amount": "79.00", "paid_at": TODAY.isoformat(),
                                                             "cleared_at": TODAY.isoformat(), "method": "stripe_link", "reference": "pi_1"})
    assert r.status_code == 303 and "accrued through the ledger" in flash_of(r.headers["location"])["ok"]
    assert calls == [(oid, "monthly", 7_900, 1, ADMIN_EMAIL)]
    row = audit_rows("franchise.commission")[0]
    assert row.after["basis"] == "monthly" and row.after["basis_pence"] == 7_900 and row.after["result"] == {"entries": 1}
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "trail_month": "1", "amount": "79", "paid_at": TODAY.isoformat(), "method": "manual"})
    assert r.status_code == 400 and "month 1 is already on record" in r.text

    # a ledger failure is recorded as pending and does not undo the cleared payment
    def refusing(db, **kw):
        raise RuntimeError("rule version 1 is unconfirmed")

    monkeypatch.setattr(franchise_router, "commission_ledger", SimpleNamespace(accrue=refusing))
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "trail_month": "2", "amount": "79", "paid_at": TODAY.isoformat(),
                                                             "cleared_at": TODAY.isoformat(), "method": "manual"})
    assert "noted as pending: rule version 1 is unconfirmed" in flash_of(r.headers["location"])["err" if "err" in flash_of(r.headers["location"]) else "ok"]
    with session() as db:
        month2 = db.query(FranchisePayment).filter(FranchisePayment.trail_month == 2).one()
        assert month2.cleared_at is not None
    assert audit_rows("franchise.commission_failed")[0].after["error"] == "rule version 1 is unconfirmed"

    # no seller, no commission
    plain = create_opportunity(admin_client, ["SP9"], email="solo@example.test", name="Solo")
    assert move(admin_client, plain, "paid", "cash on the day", override=True).status_code == 303
    with session() as db:
        pid = db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == plain).one().id
    r = admin_client.post(f"/franchise/{plain}/payments/{pid}/clear", data={"cleared_at": TODAY.isoformat()})
    assert "No seller is attributed" in flash_of(r.headers["location"])["ok"]
    assert len(audit_rows("franchise.commission_pending")) == 1
    # with the fee cleared the money stages open without an override, and the audit row carries no override flag
    r = move(admin_client, plain, "onboarding", override=True)
    assert r.status_code == 303 and "Moved to Onboarding" in flash_of(r.headers["location"])["ok"]
    assert "override" not in audit_rows("franchise.stage")[-1].after

    # the ledger says nothing accrued: the flash says so instead of claiming an accrual
    monkeypatch.setattr(franchise_router, "commission_ledger",
                        SimpleNamespace(accrue=lambda db, **kw: {"entries": 0, "amount_pence": 0, "reason": "month 3 is outside the 0-month trail"}))
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "monthly", "trail_month": "3", "amount": "79", "paid_at": TODAY.isoformat(),
                                                             "cleared_at": TODAY.isoformat(), "method": "manual"})
    ok = flash_of(r.headers["location"])["ok"]
    assert "No seller commission accrued on the monthly month 3 fee of £79.00: month 3 is outside the 0-month trail." in ok and "accrued through" not in ok


def test_payment_dates_are_sane_and_a_refund_claws_back_inside_ninety_days(admin_client):
    seed_districts()
    make_seller("sam@example.test", "SAM1", kind="outsider")
    with session() as db:          # version 1 is seeded from 20 October 2026; the fees here cleared in the summer
        tr.ensure_commission_rules(db)
        db.flush()
        for rule in db.query(CommissionRule).all():
            rule.active_from = date(2026, 1, 1)
        db.commit()
    oid = create_opportunity(admin_client, ["SP10", "SP11"], email="bea@example.test", name="Bea Buyer", seller_code="SAM1")
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "upfront", "amount": "13000", "paid_at": tomorrow, "method": "manual"})
    assert r.status_code == 400 and "paid date cannot be in the future" in r.text
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "upfront", "amount": "13000", "paid_at": "2026-07-10", "cleared_at": "2026-07-09", "method": "manual"})
    assert r.status_code == 400 and "cleared date cannot be before the paid date" in r.text
    r = admin_client.post(f"/franchise/{oid}/payments", data={"basis": "upfront", "amount": "13000", "paid_at": "2026-07-10", "method": "manual"})
    assert r.status_code == 303
    with session() as db:
        pid = db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == oid).one().id
    for bad, words in (("2026-07-09", "before the paid date"), (tomorrow, "in the future")):
        r = admin_client.post(f"/franchise/{oid}/payments/{pid}/clear", data={"cleared_at": bad})
        assert words in flash_of(r.headers["location"])["err"], bad
    assert "cannot be in the future" in flash_of(move(admin_client, oid, "paid", "cash", override=True, paid_at=tomorrow).headers["location"])["err"]
    assert load(oid).stage == "enquiry"
    r = admin_client.post(f"/franchise/{oid}/payments/{pid}/clear", data={"cleared_at": "2026-07-15"})
    assert "£1,300.00 accrued through the ledger" in flash_of(r.headers["location"])["ok"], "outsider: 10% of 13,000"
    with session() as db:
        assert db.query(CommissionEntry).filter(CommissionEntry.opportunity_id == oid).count() == 1
    assert "already cleared" in flash_of(admin_client.post(f"/franchise/{oid}/payments/{pid}/clear").headers["location"])["ok"]

    # a cleared fee can no longer be requoted at Agreement, and the seller cannot be swapped once commission exists
    assert move(admin_client, oid, "agreement", "Heads of terms signed").status_code == 303
    assert "keeps the quote it was sold on" in flash_of(admin_client.post(f"/franchise/{oid}/requote").headers["location"])["err"]
    make_seller("tom@example.test", "TOM1")
    r = admin_client.post(f"/franchise/{oid}/details", data={"notes": "", "source": "", "seller_code": "TOM1"})
    assert r.status_code == 400 and "Commission has already accrued on this sale" in r.text and load(oid).seller.code == "SAM1"
    r = admin_client.post(f"/franchise/{oid}/details", data={"notes": "kept", "source": "", "seller_code": "SAM1"})
    assert r.status_code == 303 and load(oid).notes == "kept"

    # refund: needs a reason and a sane date; 57 days after clearing it claws the commission back, once
    r = admin_client.post(f"/franchise/{oid}/payments/{pid}/refund", data={"refunded_at": "2026-09-10", "reason": ""})
    assert "Say why" in flash_of(r.headers["location"])["err"]
    r = admin_client.post(f"/franchise/{oid}/payments/{pid}/refund", data={"refunded_at": "2026-07-01", "reason": "x"})
    assert "before the paid date" in flash_of(r.headers["location"])["err"]
    r = admin_client.post(f"/franchise/{oid}/payments/{pid}/refund", data={"refunded_at": tomorrow, "reason": "x"})
    assert "in the future" in flash_of(r.headers["location"])["err"]
    r = admin_client.post(f"/franchise/{oid}/payments/{pid}/refund", data={"refunded_at": "2026-09-10", "reason": "Withdrew inside the cooling-off period"})
    ok = flash_of(r.headers["location"])["ok"]
    assert "marked refunded" in ok and "Commission of £1,300.00 clawed back through the ledger" in ok
    with session() as db:
        payment = db.get(FranchisePayment, pid)
        assert payment.refunded_at == datetime(2026, 9, 10)
        rows = db.query(CommissionEntry).filter(CommissionEntry.opportunity_id == oid).order_by(CommissionEntry.id).all()
        assert [(e.amount_pence, e.clawback_of is not None) for e in rows] == [(130_000, False), (-130_000, True)]
    assert audit_rows("franchise.payment.refund")[0].after["reason"] == "Withdrew inside the cooling-off period"
    assert audit_rows("franchise.commission_clawback")[0].after["result"]["clawbacks"] == 1
    assert "57 days after it cleared" in audit_rows("commission.clawback")[0].after["reason"]
    assert "already refunded" in flash_of(admin_client.post(f"/franchise/{oid}/payments/{pid}/refund", data={"reason": "again"}).headers["location"])["ok"]
    assert "cannot be marked cleared" not in admin_client.get(f"/franchise/{oid}").text
    detail = admin_client.get(f"/franchise/{oid}")
    assert "refunded 10 Sep 2026" in detail.text and "£0.00</strong><span>cleared" in detail.text
    # the refunded fee is no longer a cleared upfront fee, so the hand-off and the money stages close again
    r = admin_client.post(f"/franchise/{oid}/hnh", data={"dry_run": "1"})
    assert r.status_code == 303 and "Move the opportunity to Paid or Onboarding first" in flash_of(r.headers["location"])["err"]
    assert "Onboarding needs a cleared upfront payment" in flash_of(move(admin_client, oid, "onboarding").headers["location"])["err"]

    # a refund after 90 days writes nothing and leaves the founder a note
    later = create_opportunity(admin_client, ["SP9"], email="cal@example.test", name="Cal", seller_code="SAM1")
    r = admin_client.post(f"/franchise/{later}/payments", data={"basis": "upfront", "amount": "3000", "paid_at": "2026-06-01", "cleared_at": "2026-06-01", "method": "manual"})
    assert "£300.00 accrued" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        lpid = db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == later).one().id
    r = admin_client.post(f"/franchise/{later}/payments/{lpid}/refund", data={"refunded_at": "2026-10-01", "reason": "Dispute settled"})
    ok = flash_of(r.headers["location"])["ok"]
    assert "No clawback written" in ok and "outside the 90-day window" in ok and "[CONFIRM]" in ok
    with session() as db:
        assert db.query(CommissionEntry).filter(CommissionEntry.opportunity_id == later).count() == 1
    assert any(a.body.startswith("Clawback left to the founder") for a in load(later).activities)
    # a fee that never cleared earns nothing, so its refund claws nothing back
    r = admin_client.post(f"/franchise/{later}/payments", data={"basis": "monthly", "trail_month": "1", "amount": "49", "paid_at": "2026-07-01", "method": "manual"})
    with session() as db:
        mpid = db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == later, FranchisePayment.basis == "monthly").one().id
    r = admin_client.post(f"/franchise/{later}/payments/{mpid}/refund", data={"refunded_at": "2026-07-02", "reason": "Charged twice"})
    assert "had not cleared, so no commission was earned" in flash_of(r.headers["location"])["ok"]
    assert "cannot be marked cleared" in flash_of(admin_client.post(f"/franchise/{later}/payments/{mpid}/clear").headers["location"])["err"]


def test_requote_follows_a_changed_assumption_and_is_audited(admin_client):
    seed_districts()
    oid = create_opportunity(admin_client, ["SP10", "SP11"], email="jo@example.test", name="Jo")
    assert "unchanged" in flash_of(admin_client.post(f"/franchise/{oid}/requote").headers["location"])["ok"]
    with session() as db:
        tr.ensure_assumptions(db)
        db.flush()
        db.get(Assumption, "upfront_multiplier").value = 5
        db.commit()
    r = admin_client.post(f"/franchise/{oid}/requote")
    assert r.status_code == 303 and "£11,000.00 upfront" in flash_of(r.headers["location"])["ok"], "5 x 2,160 = 10,800 to the nearest 500"
    o = load(oid)
    assert o.quoted_upfront_pence == 1_100_000 and o.band == "town"
    rows = audit_rows("franchise.requote")
    assert rows[0].before == {"quoted_upfront_pence": 1_300_000} and rows[0].after == {"quoted_upfront_pence": 1_100_000}
    assert any(a.body.startswith("Requoted.") for a in o.activities)
    r = admin_client.post(f"/franchise/{oid}/requote", data={"fee_waived": "1"})
    assert load(oid).quoted_upfront_pence == 0 and load(oid).quoted_monthly_pence == 0


# ---- the packs -----------------------------------------------------------------------------------------------
def test_territory_report_withholds_money_until_every_assumption_is_confirmed(admin_client, client):
    seed_districts()
    oid = create_opportunity(admin_client, ["SP10", "SP11"], email="jo@example.test", name="Jo Andover")
    assert client.get(f"/franchise/{oid}/pack/territory").status_code == 303, "packs are admin-only"

    draft = admin_client.get(f"/franchise/{oid}/pack/territory")
    assert draft.status_code == 200
    assert "DRAFT [CONFIRM]" in draft.text and "Draft." in draft.text and packs.WITHHELD in draft.text
    assert "£13,000.00" not in draft.text and "£79.00" not in draft.text and "£144.00" not in draft.text
    assert "Fee schedule" not in draft.text and "Partner share" not in draft.text
    assert "Territory report: SP10, SP11" in draft.text and "Jo Andover" in draft.text and "Test Valley" in draft.text
    assert "3,500" in draft.text and "6,700" in draft.text and "£1,200.00" in draft.text and "June 2026" in draft.text, "public data is shown"
    assert "mandatory only" in draft.text and "capped at £150.00" in draft.text
    assert "HQ contribution" not in draft.text and "hq_contribution" not in draft.text

    confirm_everything()
    final = admin_client.get(f"/franchise/{oid}/pack/territory")
    assert final.status_code == 200
    assert "[CONFIRM]" not in final.text and "DRAFT" not in final.text and packs.WITHHELD not in final.text
    assert "Fee schedule" in final.text and "£13,000.00" in final.text and "£79.00 a month" in final.text
    assert "£144.00" in final.text and "Partner share, year 1" in final.text
    assert "HQ contribution" not in final.text, "head office's own figure is never in a pack"

    # a district that has left the model is reported, not dropped
    with session() as db:
        db.query(PostcodeDistrict).filter(PostcodeDistrict.district == "SP11").delete()
        db.commit()
    gone = admin_client.get(f"/franchise/{oid}/pack/territory")
    assert "Not in the current territory model" in gone.text and "SP11" in gone.text
    assert "Not in the territory model any more: SP11" in admin_client.get(f"/franchise/{oid}").text


def test_prospectus_print_view_renders_from_the_generated_body(admin_client):
    page = admin_client.get("/franchise/packs/prospectus")
    assert page.status_code == 200
    assert "Local Partner Programme" in page.text and "Draft for founder sign-off" in page.text and "DRAFT [CONFIRM]" in page.text
    assert "<h2>3. The two tiers</h2>" in page.text and "<table>" in page.text and "Rent Smart Wales" in page.text
    assert "window.print()" in page.text and "Hello New Home 2 Limited" in page.text
    confirm_everything()
    page = admin_client.get("/franchise/packs/prospectus")
    assert "Draft for founder sign-off" not in page.text and "DRAFT [CONFIRM]" not in page.text
    assert "[CONFIRM]" in page.text, "the document's own markers are its author's and stay"


def test_markdown_routine_covers_what_the_prospectus_uses():
    source = ("# Title — with dash\n\nA paragraph with **bold**, *italic*, `code` and a [link](https://example.test/x).\n"
              "Second line of the same paragraph.\n\n> Quoted **note**\n\n---\n\n- one\n- two\n  continued\n\n1. first\n2. second\n\n"
              "| Service | Fee |\n|---|---|\n| Tenant Find | £495 |\n| Full Management | 12% |\n\n"
              "<script>alert(1)</script> [bad](javascript:alert(1)) and a_b_c not italic.\n")
    html = markdown_to_html(source)
    assert "<h1>Title — with dash</h1>" in html
    assert "<p>A paragraph with <strong>bold</strong>, <em>italic</em>, <code>code</code> and a <a href=\"https://example.test/x\">link</a>. Second line of the same paragraph.</p>" in html
    assert "<blockquote><p>Quoted <strong>note</strong></p></blockquote>" in html and "<hr>" in html
    assert "<ul><li>one</li><li>two continued</li></ul>" in html and "<ol><li>first</li><li>second</li></ol>" in html
    assert "<thead><tr><th>Service</th><th>Fee</th></tr></thead>" in html and "<td>Tenant Find</td><td>£495</td>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html and "javascript:" not in html and "bad" in html
    assert "a_b_c" in html and "<em>b</em>" not in html
    assert packs.pounds(1_300_000) == "£13,000.00" and packs.pounds(None) == "" and packs.to_pence(Decimal("13000.00")) == 1_300_000
    assert packs.money(Decimal("144")) == "£144.00" and packs.number(3500) == "3,500" and packs.number(None) == ""


# ---- the Hello New Home hand-off ----------------------------------------------------------------------------------
def test_hnh_handoff_in_dry_run_then_live(admin_client, monkeypatch):
    seed_districts()
    oid = create_opportunity(admin_client, ["SP10", "SP11"], email="jo@example.test", name="Jo Andover")
    key = f"franchise:{oid}:hnh_partner"

    # no partner is created in Hello New Home until the territory is sold: the stage and the cleared fee are checked first
    with respx.mock(assert_all_called=False) as mock:
        r = admin_client.post(f"/franchise/{oid}/hnh", data={"dry_run": "1"})
        assert mock.calls.call_count == 0
    assert r.status_code == 303 and "Move the opportunity to Paid or Onboarding first" in flash_of(r.headers["location"])["err"]
    assert move(admin_client, oid, "paid", "paying on Friday", override=True).status_code == 303
    r = admin_client.post(f"/franchise/{oid}/hnh", data={"dry_run": "1"})
    assert r.status_code == 303 and "upfront fee has not cleared" in flash_of(r.headers["location"])["err"]
    with session() as db:
        assert db.query(ConnectorCall).count() == 0 and db.query(FranchisePayment).one().cleared_at is None
        db.query(FranchisePayment).one().cleared_at = utcnow()
        db.commit()
    assert move(admin_client, oid, "onboarding").status_code == 303 and load(oid).stage == "onboarding"

    with respx.mock(assert_all_called=False) as mock:
        r = admin_client.post(f"/franchise/{oid}/hnh", data={"dry_run": "1"})
        assert mock.calls.call_count == 0, "a dry run must not touch the network"
    assert r.status_code == 200 and "Dry run: nothing was sent" in r.text and "SP10, SP11" in r.text
    with session() as db:
        call = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == key).one()
        assert call.product == "hnh" and call.action == "franchise_partner" and call.status == "pending" and call.grant_id is None
        assert call.attempts == 1 and call.payload["postcodes"] == ["SP10", "SP11"] and call.payload["franchise"] == {"fee_waived": False}
        plan = call.response["plan"]
        assert plan["method"] == "POST" and plan["url"] == f"{HNH}/api/partner/partners"
        assert plan["json"]["tier"] == "area" and plan["json"]["postcodes"] == ["SP10", "SP11"] and plan["json"]["email"] == "jo@example.test"
        assert plan["json"]["franchise"] == {"fee_waived": False, "reason": f"grouphq:franchise:{oid}"} and plan["json"]["create_login"] is True
        assert "test-" not in json.dumps(call.response), "the token never reaches the stored plan"
    assert audit_rows("franchise.hnh_partner")[0].after["dry_run"] is True
    assert any("Dry run of the Hello New Home hand-off" in a.body for a in load(oid).activities)

    # live: the same row is updated, the ids come back onto the person, the one-time password is shown once and stored nowhere
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset())
    with respx.mock(base_url=HNH) as mock:
        route = mock.post("/api/partner/partners").mock(return_value=httpx.Response(
            201, json={"ok": True, "partner": {"id": 42, "user_id": 7, "email": "jo@example.test"}, "one_time_password": "tmp-pass-123"}))
        r = admin_client.post(f"/franchise/{oid}/hnh")
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["postcodes"] == ["SP10", "SP11"] and body["name"] == "Jo Andover" and body["franchise"]["fee_waived"] is False
    assert route.calls.last.request.headers["x-partner-client"] == "grouphq"
    assert r.status_code == 200 and "Created in Hello New Home as partner 42" in r.text and "tmp-pass-123" in r.text
    with session() as db:
        call = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == key).one()
        assert call.status == "succeeded" and call.attempts == 2 and call.response["remote_ref"] == "42"
        assert "tmp-pass-123" not in json.dumps(call.response)
        person = db.query(Person).filter(Person.person_key == "jo@example.test").one()
        assert person.hnh_partner_id == 42 and person.hnh_user_id == 7
    assert "tmp-pass-123" not in json.dumps([row.after for row in audit_rows("franchise.hnh_partner")])
    assert any("Local Partner created in Hello New Home (partner id 42)" in a.body for a in load(oid).activities)
    detail = admin_client.get(f"/franchise/{oid}")
    assert "Partner id 42 in Hello New Home" in detail.text

    # never twice
    with respx.mock(assert_all_called=False) as mock:
        r = admin_client.post(f"/franchise/{oid}/hnh")
        assert mock.calls.call_count == 0
    assert r.status_code == 303 and "Already created in Hello New Home (connector call #1). Nothing was sent again." in flash_of(r.headers["location"])["err"]
    with session() as db:
        db.query(ConnectorCall).delete()
        db.commit()
    r = admin_client.post(f"/franchise/{oid}/hnh")
    assert r.status_code == 303 and "already has a Hello New Home partner id (42)" in flash_of(r.headers["location"])["err"]


def test_hnh_handoff_failures_are_recorded(admin_client, monkeypatch):
    seed_districts()
    oid = create_opportunity(admin_client, ["SP9"], email="nameless@example.test")
    pay_and_clear(admin_client, oid, "3000.00")
    assert move(admin_client, oid, "paid").status_code == 303 and load(oid).stage == "paid"
    r = admin_client.post(f"/franchise/{oid}/hnh", data={"dry_run": "1"})
    assert r.status_code == 200 and "Hello New Home refused: missing_name" in r.text
    with session() as db:
        call = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == f"franchise:{oid}:hnh_partner").one()
        assert call.status == "failed" and call.response["error"] == "missing_name"
    assert audit_rows("franchise.hnh_partner_failed")[0].after["error"]["error"] == "missing_name"

    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset())
    with session() as db:
        db.query(Person).filter(Person.person_key == "nameless@example.test").one().name = "Now Named"
        db.commit()
    with respx.mock(base_url=HNH) as mock:
        mock.post("/api/partner/partners").mock(return_value=httpx.Response(409, json={"error": "email_in_use", "message": "already a partner"}))
        r = admin_client.post(f"/franchise/{oid}/hnh")
    assert r.status_code == 200 and "email_in_use" in r.text and "already a partner" in r.text
    with session() as db:
        call = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == f"franchise:{oid}:hnh_partner").one()
        assert call.status == "failed" and call.attempts == 2 and call.response["status"] == 409
        assert db.query(Person).filter(Person.person_key == "nameless@example.test").one().hnh_partner_id is None
