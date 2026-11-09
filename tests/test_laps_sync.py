"""The LAPS board fills itself from the CRM bridge and the franchise pipeline, and never undoes the owner."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models import AuditLog, CampaignLead, FranchiseOpportunity, LapsDeal, Person
from app.services import laps, laps_sync, relaunch72_crm
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
    assert "Where the board gets its leads" in page and "/crm/leads/" in page


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
