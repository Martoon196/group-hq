"""The LAPS board fills itself from the CRM bridge and the franchise pipeline, and never undoes the owner."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import config
from app.models import AuditLog, CampaignLead, FranchiseOpportunity, LapsDeal, Person, User
from app.services import inbox_crm, laps, laps_sync, relaunch72_crm
from app.services.relaunch72_crm import CrmUnavailable, Lead, LeadPage

NOW = datetime(2026, 11, 11, 15, 0)
IDS = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 9)]


def crm_lead(i, name, lifecycle="lead", stage=None, activity="2026-11-01T09:00:00.000Z"):
    return Lead(id=IDS[i], name=name, source="Website", lifecycle=lifecycle, stage=stage, next_task_at=None,
                last_activity_at=activity, detail_url="https://hq.propertypredator.com/portal/crm/contacts/" + IDS[i])


def bridge(monkeypatch, *pages):
    """Serve pages by cursor: None, "page-1", "page-2"..."""
    calls = []

    def fetch(cursor=None):
        calls.append(cursor)
        index = 0 if cursor is None else int(cursor.split("-")[1])
        leads = pages[index]
        return LeadPage(tuple(leads), f"page-{index + 1}" if index + 1 < len(pages) else None)
    monkeypatch.setattr(relaunch72_crm, "fetch_leads", fetch)
    return calls


def opportunity(db, name, stage, upfront=None, days_ago=10):
    person = Person(person_key=name.lower().replace(" ", ".") + "@example.test", name=name)
    db.add(person)
    db.flush()
    row = FranchiseOpportunity(person_id=person.id, stage=stage, districts=["SA1", "SA2"], quoted_upfront_pence=upfront,
                               source="Web form", created_at=laps.utcnow() - timedelta(days=days_ago),
                               stage_changed_at=laps.utcnow() - timedelta(days=2))
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize("lifecycle,stage,expected", [
    ("lead", None, "lead"), ("lead", "New lead", "lead"), ("lead", "Qualified", "appointment"),
    ("lead", "Call booked", "appointment"), ("lead", "Proposal", "presentation"), ("lead", "Offer sent", "presentation"),
    ("customer", "Proposal", "sale"), ("customer", None, "sale"), ("archived", "Qualified", "lost"),
    ("lead", "Nurture", "lead"),
])
def test_crm_stage_names_map_onto_the_board(lifecycle, stage, expected):
    assert laps_sync.crm_stage(lifecycle, stage) == expected


def test_a_source_creates_once_moves_forward_and_never_undoes_the_owner(db):
    existing = {}
    fields = dict(name="A", brand="pp", source="CRM")
    assert laps_sync.place(db, existing, "crm:1", stage="lost", first_seen=NOW, now=NOW, **fields) is None
    assert laps_sync.place(db, existing, "crm:1", stage="appointment", first_seen=NOW - timedelta(days=5), now=NOW, **fields) == "created"
    deal = existing["crm:1"]
    assert deal.stage == "appointment" and deal.lead_at == deal.appointment_at == NOW - timedelta(days=5)
    assert laps_sync.place(db, existing, "crm:1", stage="appointment", first_seen=NOW, now=NOW, **fields) is None
    assert laps_sync.place(db, existing, "crm:1", stage="lead", first_seen=NOW, now=NOW, **fields) is None
    assert deal.stage == "appointment", "a source behind the board does not pull the card back"
    assert laps_sync.place(db, existing, "crm:1", stage="sale", first_seen=NOW, now=NOW, **fields, value_pence=5000) == "moved"
    assert deal.stage == "sale" and deal.sale_at == NOW and deal.value_pence == 5000
    assert laps_sync.place(db, existing, "crm:1", stage="lost", first_seen=NOW, now=NOW, **fields) is None
    assert deal.stage == "sale", "a sale is not undone by the source"

    laps_sync.place(db, existing, "crm:2", stage="lead", first_seen=NOW, now=NOW, **fields)
    laps.move(existing["crm:2"], "lost", NOW)                 # the owner gave up on this one
    assert laps_sync.place(db, existing, "crm:2", stage="presentation", first_seen=NOW, now=NOW, **fields) is None
    assert existing["crm:2"].stage == "lost"
    laps_sync.place(db, existing, "crm:3", stage="presentation", first_seen=NOW, now=NOW, **fields)
    assert laps_sync.place(db, existing, "crm:3", stage="lost", first_seen=NOW, now=NOW, **fields) == "moved"
    db.rollback()


def test_the_board_fills_itself_from_the_franchise_pipeline_on_a_visit(admin_client, db):
    opportunity(db, "Sample Enquirer", "enquiry")
    paid = opportunity(db, "Sample Buyer", "paid", upfront=950000)
    opportunity(db, "Sample Gone", "lost")
    page = admin_client.get("/laps")
    assert page.status_code == 200 and "Sample Enquirer" in page.text and "Sample Buyer" in page.text
    assert "Sample Gone" not in page.text and "Franchise pipeline" in page.text
    deals = {d.name: d for d in db.query(LapsDeal)}
    assert set(deals) == {"Sample Enquirer", "Sample Buyer"}
    assert (deals["Sample Buyer"].stage, deals["Sample Buyer"].value_pence, deals["Sample Buyer"].brand,
            deals["Sample Buyer"].origin) == ("sale", 950000, "hnh", f"franchise:{paid.id}")
    assert deals["Sample Buyer"].sale_at == paid.stage_changed_at and deals["Sample Buyer"].offer == "Territory: SA1, SA2"
    admin_client.get("/laps")
    assert db.query(LapsDeal).count() == 2, "a second visit adds nothing"
    assert db.query(AuditLog).filter_by(action="laps.sync").count() == 1


def test_refresh_reads_every_crm_page_and_follows_later_stage_changes(admin_client, db, monkeypatch):
    calls = bridge(monkeypatch, [crm_lead(1, "Sample One"), crm_lead(2, "Sample Two", stage="Qualified")],
                   [crm_lead(3, "Sample Three", lifecycle="customer"), crm_lead(4, "Sample Old", lifecycle="archived")])
    done = admin_client.post("/laps/refresh")
    assert done.status_code == 303 and "ok=" in done.headers["location"] and calls == [None, "page-1"]
    deals = {d.name: d for d in db.query(LapsDeal)}
    assert {n: d.stage for n, d in deals.items()} == {"Sample One": "lead", "Sample Two": "appointment", "Sample Three": "sale"}
    assert deals["Sample One"].lead_at == datetime(2026, 11, 1, 9), "dated by its last CRM activity, not by the import"
    assert deals["Sample One"].origin == f"crm:{db.query(CampaignLead).filter_by(display_name='Sample One').one().id}"
    assert laps_sync.state(db)["checked"] == 4 and not laps_sync.state(db)["problem"]

    bridge(monkeypatch, [crm_lead(1, "Sample One", stage="Proposal"), crm_lead(2, "Sample Two", lifecycle="archived"),
                         crm_lead(3, "Sample Three", lifecycle="customer")])
    admin_client.post("/laps/refresh")
    db.expire_all()
    assert (deals["Sample One"].stage, deals["Sample Two"].stage, deals["Sample Three"].stage) == ("presentation", "lost", "sale")
    assert deals["Sample One"].lead_at == datetime(2026, 11, 1, 9) and deals["Sample One"].presentation_at > datetime(2026, 11, 1, 9)
    assert db.query(LapsDeal).count() == 3
    page = admin_client.get("/laps").text
    assert "Where the board gets its leads" in page and "/laps/source/" in page and "older connection" in page
    assert laps_sync.state(db)["brands"]["pp"]["via"] == "bridge" and laps_sync.state(db)["brands"]["mhp"]["via"] is None


def test_a_silent_crm_is_reported_and_changes_nothing_else(admin_client, db, monkeypatch):
    opportunity(db, "Sample Enquirer", "discovery")

    def down(cursor=None):
        raise CrmUnavailable()
    monkeypatch.setattr(relaunch72_crm, "fetch_leads", down)
    done = admin_client.post("/laps/refresh")
    assert "err=" in done.headers["location"]
    assert laps_sync.state(db)["problem"] and db.query(LapsDeal).one().stage == "appointment"


def test_opening_the_board_checks_the_crm_in_the_background_when_stale(admin_client, db, monkeypatch):
    calls = bridge(monkeypatch, [crm_lead(1, "Sample One")])
    assert laps_sync.due(db)
    admin_client.get("/laps")                       # the test client runs the background task before returning
    assert calls == [None] and not laps_sync.due(db)
    assert "Sample One" in admin_client.get("/laps").text and calls == [None], "fresh enough: not asked again"
    assert laps_sync.due(db, laps.utcnow() + laps_sync.STALE)


def test_owner_changes_on_an_automatic_deal_survive_a_refresh(admin_client, db, monkeypatch):
    bridge(monkeypatch, [crm_lead(1, "Sample One")])
    admin_client.post("/laps/refresh")
    deal = db.query(LapsDeal).one()
    admin_client.post(f"/laps/deals/{deal.id}", data={"name": "Sample One (Durham)", "brand": "pp", "value": "2490",
                                                      "next_step": "Send the pack", "notes": "Keen"})
    admin_client.post(f"/laps/deals/{deal.id}/stage", data={"to": "presentation"})
    admin_client.post("/laps/refresh")
    db.expire_all()
    assert (deal.name, deal.value_pence, deal.next_step, deal.notes, deal.stage) == (
        "Sample One (Durham)", 249000, "Send the pack", "Keen", "presentation")
    assert "Brought in automatically" in admin_client.get(f"/laps/deals/{deal.id}").text


# ---- every business's CRM, through the signed lead listing ---------------------------------------------

def listed(i, name, lifecycle="lead", created="2026-10-01T08:00:00.000Z", **deal):
    row = {"id": IDS[i], "name": name, "source": "Webinar", "lifecycle": lifecycle, "createdAt": created,
           "updatedAt": created, "opportunity": None}
    if deal:
        row["opportunity"] = {"status": "open", "stage": "New lead", "stagePosition": 1, "valueMinor": 0, "currency": "GBP",
                              "stageChangedAt": "2026-10-05T09:00:00.000Z", "closedAt": None, **deal}
    return row


def listing(monkeypatch, answers):
    """answers: brand -> list of pages (each a list of raw rows), or an exception to raise for that brand."""
    asked = []

    def command(user, brand, action, **payload):
        asked.append((user.email, brand, action, payload.get("cursor")))
        answer = answers.get(brand, inbox_crm.CrmUnavailable("down"))
        if isinstance(answer, Exception):
            raise answer
        index = 0 if payload.get("cursor") is None else int(payload["cursor"].split("-")[1])
        return inbox_crm._leads_page({"brand": brand, "leads": answer[index],
                                      "nextCursor": f"page-{index + 1}" if index + 1 < len(answer) else None}, brand)
    monkeypatch.setattr(inbox_crm, "command", command)
    return asked


def test_listing_answer_is_validated_before_anything_is_stored():
    good = {"brand": "mhp", "leads": [listed(1, "A", status="won", valueMinor=300000, closedAt="2026-11-02T10:00:00.000Z"),
                                      listed(2, "B", status="open", currency="EUR", valueMinor=500)], "nextCursor": None}
    page = inbox_crm._leads_page(good, "mhp")
    assert (page["leads"][0].opportunity_status, page["leads"][0].value_minor, page["leads"][0].stage_changed_at) == (
        "won", 300000, "2026-11-02T10:00:00.000Z")
    assert page["leads"][1].value_minor is None, "a value that is not in pounds is not counted as pounds"
    assert page["leads"][0].source_created_at == "2026-10-01T08:00:00.000Z" and page["next_cursor"] is None
    for bad in ({**good, "brand": "kp"}, {**good, "leads": [{**good["leads"][0], "id": "not-a-uuid"}]},
                {**good, "leads": [good["leads"][0]] * 2}, {**good, "leads": [{**good["leads"][0], "lifecycle": "vip"}]},
                {**good, "leads": [listed(1, "A", status="pending")]}, {**good, "nextCursor": "has spaces"},
                {**good, "leads": [listed(i % 8, "x") for i in range(201)]}, {**good, "leads": [listed(1, "A", valueMinor=-1)]}):
        with pytest.raises((ValueError, KeyError, TypeError)):
            inbox_crm._leads_page(bad, "mhp")


def test_every_connected_business_fills_the_board_with_the_crms_own_dates(admin_client, db, monkeypatch):
    def bridge_must_not_run(cursor=None):
        raise AssertionError("the listing answered for Property Predator; the old bridge is not needed")
    monkeypatch.setattr(relaunch72_crm, "fetch_leads", bridge_must_not_run)
    asked = listing(monkeypatch, {
        "pp": [[listed(1, "Sample Subscriber", lifecycle="customer")]],
        "mhp": [[listed(2, "Sample Mentee", status="won", stage="Won", valueMinor=300000, closedAt="2026-11-02T10:00:00.000Z"),
                 listed(3, "Sample Webinar Lead")],
                [listed(4, "Sample Booked", stage="Call booked"), listed(5, "Sample Gone", status="lost", stage="Lost")]],
        "piuk": [[listed(6, "Sample Borrower", stage="Proposal")]],
    })
    done = admin_client.post("/laps/refresh")
    assert "ok=" in done.headers["location"]
    assert [(b, c) for _, b, _, c in asked] == [("pp", None), ("mhp", None), ("mhp", "page-1"), ("kp", None), ("piuk", None)]
    assert {email for email, *_ in asked} == {db.query(User).one().email}
    deals = {d.name: d for d in db.query(LapsDeal)}
    assert {n: (d.brand, d.stage) for n, d in deals.items()} == {
        "Sample Subscriber": ("pp", "sale"), "Sample Mentee": ("mhp", "sale"), "Sample Webinar Lead": ("mhp", "lead"),
        "Sample Booked": ("mhp", "appointment"), "Sample Borrower": ("piuk", "presentation")}
    mentee = deals["Sample Mentee"]
    assert (mentee.lead_at, mentee.sale_at, mentee.value_pence) == (datetime(2026, 10, 1, 8), datetime(2026, 11, 2, 10), 300000)
    assert deals["Sample Booked"].appointment_at == datetime(2026, 10, 5, 9), "the day the CRM says the stage changed"
    state = laps_sync.state(db)
    assert {b: (v["via"], v["checked"]) for b, v in state["brands"].items()} == {
        "pp": ("listing", 1), "mhp": ("listing", 4), "kp": (None, 0), "piuk": ("listing", 1)}
    assert db.query(CampaignLead).filter_by(brand="piuk").one().stage == "Proposal"

    page = admin_client.get("/laps").text
    assert page.count("Connected") == 3 and "Not arriving yet." in page and "Key Properties UK CRM" in page
    lead = db.query(CampaignLead).filter_by(brand="mhp", display_name="Sample Mentee").one()
    assert admin_client.get(f"/laps/source/{lead.id}").headers["location"] == f"/inbox/crm/contact?brand=mhp&contact={lead.external_id}"
    pp = db.query(CampaignLead).filter_by(brand="pp").one()
    assert admin_client.get(f"/laps/source/{pp.id}").headers["location"] == f"/crm/leads/{pp.id}"


def test_the_background_refresh_asks_the_crm_as_the_inbox_owner(admin_client, db, monkeypatch):
    owner = db.query(User).one()
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", owner.email.upper().lower())
    asked = listing(monkeypatch, {"kp": [[listed(1, "Sample Vendor")]]})
    monkeypatch.setattr(relaunch72_crm, "fetch_leads", lambda cursor=None: (_ for _ in ()).throw(CrmUnavailable()))
    admin_client.get("/laps")
    assert ("kp", "leads") in [(b, a) for _, b, a, _ in asked] and db.query(LapsDeal).one().brand == "kp"
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", "")
    assert laps_sync.owner(db) is None and laps_sync.pull_brand(db, "kp", None, "system")["problem"]


# ---- one person, several businesses -------------------------------------------------------------------

def test_a_lead_can_be_added_under_several_businesses_each_with_its_own_card(admin_client, db):
    assert "err=" in admin_client.post("/laps/deals", data={"name": "Sample Person"}).headers["location"]
    added = admin_client.post("/laps/deals", data={"name": "Sample Person", "brand": ["mhp", "pp", "mhp"], "source": "LinkedIn"})
    assert "ok=" in added.headers["location"] and "2+businesses" in added.headers["location"]
    cards = db.query(LapsDeal).order_by(LapsDeal.id).all()
    assert [c.brand for c in cards] == ["mhp", "pp"] and cards[0].party and cards[0].party == cards[1].party
    admin_client.post(f"/laps/deals/{cards[1].id}/stage", data={"to": "sale", "value": "39"})
    board = admin_client.get("/laps").text
    assert "Also:" in board and "Property Predator · Sale" in board and "Martin Howard Property · Lead" in board
    db.expire_all()
    assert cards[0].stage == "lead", "a sale for one business leaves the other card where it was"

    page = admin_client.get(f"/laps/deals/{cards[0].id}").text
    assert "Other businesses" in page and "Do they want something from another business too?" in page
    more = admin_client.post(f"/laps/deals/{cards[0].id}/also", data={"brand": ["kp", "pp", "nope"]})
    assert "ok=" in more.headers["location"]
    kp = db.query(LapsDeal).filter_by(brand="kp").one()
    assert (kp.name, kp.source, kp.stage, kp.party) == ("Sample Person", "LinkedIn", "lead", cards[0].party)
    assert "err=" in admin_client.post(f"/laps/deals/{cards[0].id}/also", data={"brand": ["kp"]}).headers["location"]
    assert db.query(LapsDeal).count() == 3

    single = LapsDeal(name="Solo", brand="hnh", stage="lead", lead_at=laps.utcnow())
    db.add(single)
    db.commit()
    admin_client.post(f"/laps/deals/{single.id}/also", data={"brand": "ordris"})
    db.expire_all()
    assert single.party and db.query(LapsDeal).filter_by(party=single.party).count() == 2
