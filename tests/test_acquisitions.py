"""Property deals: the buying pipeline on the LAPS machinery, its checklist, sums and screens."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models import Acquisition, AuditLog
from app.services import acquisitions as service
from app.services import laps

NOW = datetime(2026, 11, 11, 15, 0)


def make(db, title="12 Sample Street", stage="lead", at=NOW, **extra):
    row = Acquisition(title=title, stage="lead", lead_at=at, checks={}, **extra)
    if stage != "lead":
        service.move(row, stage, at)
    db.add(row)
    db.commit()
    return row


def test_pipeline_moves_stamp_and_clear_like_the_laps_board():
    row = Acquisition(title="A", stage="lead", lead_at=NOW)
    later = NOW + timedelta(days=30)
    service.move(row, "legals", later)
    assert row.stage == "legals" and row.appraisal_at == row.offer_at == row.agreed_at == row.legals_at == later
    assert row.exchanged_at is None and row.completed_at is None
    service.move(row, "offer", later)
    assert row.stage == "offer" and row.agreed_at is None and row.legals_at is None and row.offer_at == later
    service.move(row, "lost", later)
    service.move(row, "reopen", later)
    assert row.stage == "offer" and row.lost_at is None
    service.move(row, "completed", later)
    with pytest.raises(ValueError):
        service.move(row, "lost")
    with pytest.raises(ValueError):
        service.move(row, "sale")          # a LAPS stage is not a stage of this pipeline


def test_numbers_use_the_best_known_price():
    row = Acquisition(title="A", asking_pence=20000000)
    assert service.numbers(row) == {"price": 20000000, "below_asking": None, "headroom": None}
    row.offer_pence, row.value_pence, row.works_pence = 16000000, 25000000, 3000000
    assert service.numbers(row) == {"price": 16000000, "below_asking": 0.2, "headroom": 6000000}
    row.agreed_pence = 17000000
    assert service.numbers(row)["price"] == 17000000 and service.numbers(row)["headroom"] == 5000000
    assert service.parse_price("45,000,000") == 4500000000, "a portfolio can pass what an INTEGER of pence holds"


def test_checklist_keys_are_unique_and_toggle_both_ways():
    assert set(service.CHECKLIST) == set(service.PIPELINE)
    row = Acquisition(title="A", stage="lead", checks={})
    assert service.toggle(row, "replied", "2026-11-11") is True and row.checks == {"replied": "2026-11-11"}
    assert service.progress(row)["done"] == 1 and service.progress(row)["total"] == 3
    assert service.toggle(row, "replied", "2026-11-12") is False and row.checks == {}
    with pytest.raises(ValueError):
        service.toggle(row, "not-a-step", "2026-11-11")


def test_stage_totals_and_period_counts(db):
    make(db, "A", asking_pence=10000000)
    make(db, "B", stage="offer", asking_pence=30000000, offer_pence=25000000)
    make(db, "C", stage="completed", agreed_pence=40000000)
    make(db, "D", stage="appraisal")
    lost = make(db, "E", stage="offer")
    service.move(lost, "lost", NOW)
    db.commit()
    totals = {t["stage"]: (t["count"], t["pence"]) for t in service.stage_totals(db)}
    assert totals == {"lead": (1, 10000000), "appraisal": (1, 0), "offer": (1, 25000000), "agreed": (0, 0),
                      "legals": (0, 0), "exchanged": (0, 0)}
    month = laps.reached_in(db, Acquisition, service.PIPELINE, *laps.periods(NOW)["this_month"])
    assert (month["lead"], month["offer"], month["completed"]) == (5, 3, 1)
    assert service.completed_between(db, NOW - timedelta(days=1), NOW + timedelta(days=1)) == {"count": 1, "pence": 40000000}


def test_board_add_move_check_and_audit(admin_client, db):
    page = admin_client.get("/acquisitions")
    assert page.status_code == 200 and "Property deals" in page.text and "Nothing here." in page.text
    assert 'href="/acquisitions"' in admin_client.get("/").text and "Buying · Property deals" in admin_client.get("/laps").text

    added = admin_client.post("/acquisitions/deals", data={
        "title": " Sample  portfolio ", "postcode": "sa1 1aa", "kind": "portfolio", "units": "6", "asking": "£900,000",
        "seller_name": "Sample Seller", "seller_type": "portfolio_landlord", "source": "WhatsApp", "situation": "Retiring"})
    row = db.query(Acquisition).one()
    assert added.status_code == 303 and added.headers["location"].startswith(f"/acquisitions/deals/{row.id}")
    assert (row.title, row.postcode, row.kind, row.units, row.asking_pence, row.stage, row.plan) == (
        "Sample portfolio", "SA1 1AA", "portfolio", 6, 90000000, "lead", "undecided")

    deal_page = admin_client.get(f"/acquisitions/deals/{row.id}")
    assert deal_page.status_code == 200 and "Asked why they are selling" in deal_page.text and "Not legal advice" in deal_page.text
    assert admin_client.post(f"/acquisitions/deals/{row.id}/check", data={"key": "replied"}).status_code == 303
    assert "err=" in admin_client.post(f"/acquisitions/deals/{row.id}/check", data={"key": "nope"}).headers["location"]
    admin_client.post(f"/acquisitions/deals/{row.id}/stage", data={"to": "appraisal"})
    admin_client.post(f"/acquisitions/deals/{row.id}/stage", data={"to": "offer", "price": "750000", "back": "board"})
    admin_client.post(f"/acquisitions/deals/{row.id}/stage", data={"to": "agreed", "price": "780,000"})
    db.expire_all()
    assert (row.stage, row.offer_pence, row.agreed_pence, row.checks) == ("agreed", 75000000, 78000000, {"replied": row.checks["replied"]})
    board = admin_client.get("/acquisitions").text
    assert "Sample portfolio" in board and "£780k" in board and "Solicitors instructed" in board

    admin_client.post(f"/acquisitions/deals/{row.id}/stage", data={"to": "lost", "lost_reason": "Seller withdrew"})
    db.expire_all()
    assert row.stage == "lost" and row.lost_reason == "Seller withdrew" and row.agreed_at is not None
    assert "Seller withdrew" in admin_client.get("/acquisitions").text
    admin_client.post(f"/acquisitions/deals/{row.id}/stage", data={"to": "reopen"})
    db.expire_all()
    assert row.stage == "agreed"
    actions = [a.action for a in db.query(AuditLog).filter(AuditLog.action.like("acquisition.%")).order_by(AuditLog.id)]
    assert actions == ["acquisition.add", "acquisition.check"] + ["acquisition.move"] * 5


def test_edit_validates_and_saves(admin_client, db):
    row = make(db, "<b>Site</b>", at=laps.utcnow())
    page = admin_client.get(f"/acquisitions/deals/{row.id}")
    assert "<b>Site</b>" not in page.text and "&lt;b&gt;Site&lt;/b&gt;" in page.text
    for bad in ({"title": ""}, {"title": "A", "kind": "castle"}, {"title": "A", "units": "0"}, {"title": "A", "asking": "lots"},
                {"title": "A", "target_completion": "soon"}):
        assert "err=" in admin_client.post(f"/acquisitions/deals/{row.id}", data=bad).headers["location"], bad
    saved = admin_client.post(f"/acquisitions/deals/{row.id}", data={
        "title": "Sample site", "kind": "land", "plan": "development", "seller_type": "developer", "asking": "1200000",
        "works": "800000", "value": "2600000", "solicitor": "Sample LLP", "target_completion": "2027-03-31"})
    assert "ok=" in saved.headers["location"]
    db.expire_all()
    assert (row.title, row.kind, row.plan, row.works_pence, row.target_completion.isoformat()) == (
        "Sample site", "land", "development", 80000000, "2027-03-31")
    assert "£600,000" in admin_client.get(f"/acquisitions/deals/{row.id}").text, "2.6m less 1.2m asking and 0.8m works"
    assert admin_client.get("/acquisitions/deals/999999").status_code == 404


def test_add_refuses_bad_input(admin_client, db):
    for data in ({"title": ""}, {"title": "A", "stage": "lost"}, {"title": "A", "arrived": "2999-01-01"},
                 {"title": "A", "seller_type": "king"}):
        assert "err=" in admin_client.post("/acquisitions/deals", data=data).headers["location"], data
    assert db.query(Acquisition).count() == 0


def test_starting_from_an_inbox_conversation_prefills_and_saves_nothing(admin_client, db):
    page = admin_client.post("/acquisitions/start", data={"seller_name": 'Sample "Landlord"', "source": "WhatsApp"})
    assert page.status_code == 200 and 'value="Sample &#34;Landlord&#34;"' in page.text and 'value="WhatsApp"' in page.text
    lead = admin_client.post("/laps/start", data={"name": "Sample Buyer", "source": "LinkedIn"})
    assert lead.status_code == 200 and 'value="Sample Buyer"' in lead.text and 'value="LinkedIn"' in lead.text
    assert db.query(Acquisition).count() == 0


def test_needs_a_signed_in_admin_and_blocks_cross_site_posts(client, admin_client, db):
    assert client.get("/acquisitions").status_code in (303, 307)
    blocked = admin_client.post("/acquisitions/deals", data={"title": "A"}, headers={"Origin": "https://evil.test"})
    assert blocked.status_code == 403 and db.query(Acquisition).count() == 0
