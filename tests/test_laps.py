"""The LAPS board: stage moves, the weekly scoreboard, cohort conversion rates, the income plan and the screens."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.models import AuditLog, CampaignLead, LapsDeal, Mentee, Payment, Person, Programme
from app.services import laps

NOW = datetime(2026, 11, 11, 15, 0)          # a Wednesday; the UK is on GMT, so UTC and local agree


def deal(db, name="Sample lead", brand="mhp", stage="lead", at=NOW, value=None, **extra):
    row = LapsDeal(name=name, brand=brand, stage="lead", lead_at=at, value_pence=value, **extra)
    laps.move(row, stage, at) if stage != "lead" else None
    db.add(row)
    db.commit()
    return row


def test_parse_pounds_accepts_money_and_refuses_everything_else():
    assert laps.parse_pounds("") is None and laps.parse_pounds("  ") is None
    assert laps.parse_pounds("1500") == 150000 and laps.parse_pounds("£1,250.50") == 125050
    for bad in ("abc", "-5", "1.999", "NaN", "Infinity", "99999999999"):
        with pytest.raises(ValueError):
            laps.parse_pounds(bad)


def test_forward_move_stamps_skipped_stages_and_backward_move_clears_later_ones():
    row = LapsDeal(name="A", brand="mhp", stage="lead", lead_at=NOW)
    later = NOW + timedelta(days=3)
    laps.move(row, "sale", later)
    assert (row.stage, row.appointment_at, row.presentation_at, row.sale_at) == ("sale", later, later, later)
    assert row.lead_at == NOW, "an existing stamp is never rewritten"
    laps.move(row, "appointment", later + timedelta(days=1))
    assert (row.stage, row.appointment_at, row.presentation_at, row.sale_at) == ("appointment", later, None, None)


def test_lost_keeps_earned_stamps_reopens_at_furthest_stage_and_a_sale_cannot_be_lost():
    row = LapsDeal(name="A", brand="mhp", stage="lead", lead_at=NOW)
    laps.move(row, "presentation", NOW)
    laps.move(row, "lost", NOW + timedelta(days=1))
    assert row.stage == "lost" and row.presentation_at == NOW and row.lost_at == NOW + timedelta(days=1)
    laps.move(row, "reopen", NOW + timedelta(days=2))
    assert row.stage == "presentation" and row.lost_at is None
    laps.move(row, "sale", NOW + timedelta(days=3))
    with pytest.raises(ValueError):
        laps.move(row, "lost")
    with pytest.raises(ValueError):
        laps.move(row, "elsewhere")


def test_periods_are_cut_on_uk_midnights_including_summer_time():
    winter = laps.periods(NOW)
    assert winter["this_week"] == (datetime(2026, 11, 9), datetime(2026, 11, 16))
    assert winter["last_week"] == (datetime(2026, 11, 2), datetime(2026, 11, 9))
    assert winter["this_month"] == (datetime(2026, 11, 1), datetime(2026, 12, 1))
    assert winter["last_month"] == (datetime(2026, 9, 30, 23), datetime(2026, 11, 1)), "October starts in BST"
    summer = laps.periods(datetime(2026, 7, 5, 23, 30))      # 00:30 on Monday 6 July in the UK
    assert summer["this_week"] == (datetime(2026, 7, 5, 23), datetime(2026, 7, 12, 23))
    weeks = laps.week_starts(NOW, weeks=3)
    assert [w[0].isoformat() for w in weeks] == ["2026-10-26", "2026-11-02", "2026-11-09"]
    assert weeks[-1][1:] == winter["this_week"]


def test_counts_use_the_moment_each_stage_was_reached_and_filter_by_brand(db):
    start, end = laps.periods(NOW)["this_week"]
    old = deal(db, "Old lead", at=NOW - timedelta(days=20))
    laps.move(old, "sale", NOW)
    old.value_pence = 250000
    deal(db, "Fresh", brand="pp", at=NOW)
    deal(db, "Lost after appointment", stage="appointment", at=NOW)
    db.commit()
    lost = db.query(LapsDeal).filter_by(name="Lost after appointment").one()
    laps.move(lost, "lost", NOW)
    db.commit()
    assert laps.counts(db, start, end) == {"lead": 2, "appointment": 2, "presentation": 1, "sale": 1, "income_pence": 250000}
    assert laps.counts(db, start, end, "pp") == {"lead": 1, "appointment": 0, "presentation": 0, "sale": 0, "income_pence": 0}
    assert laps.counts(db, *laps.periods(NOW)["last_week"])["lead"] == 0


def test_cohort_rates_need_enough_deals_and_never_exceed_one(db):
    for i in range(4):
        deal(db, f"L{i}")
    assert laps.cohort(db, NOW)["rates"] == {"appointment": None, "presentation": None, "sale": None}
    for i in range(6):
        deal(db, f"A{i}", stage="appointment")
    for i in range(5):
        deal(db, f"S{i}", stage="sale", value=100000 * (i + 1))
    deal(db, "Ancient sale", stage="sale", at=NOW - timedelta(days=200), value=900000000)
    stats = laps.cohort(db, NOW)
    assert stats["reached"] == {"lead": 15, "appointment": 11, "presentation": 5, "sale": 5}
    assert stats["rates"] == {"appointment": 11 / 15, "presentation": 5 / 11, "sale": 1.0}
    assert stats["average_sale_pence"] == 300000, "sales outside the window are not part of the average"


def test_plan_works_back_from_the_goal_and_stops_where_a_rate_is_unknown():
    stats = {"rates": {"appointment": 0.2, "presentation": 0.8, "sale": 0.25}, "average_sale_pence": 500000}
    plan = laps.plan(2000000, None, stats)
    assert plan["month"] == {"sale": 4, "presentation": 16, "appointment": 20, "lead": 100}
    assert plan["week"] == {"sale": 1, "presentation": 4, "appointment": 5, "lead": 24}
    assert plan["missing"] is None and plan["from_typical"] is False
    typical = laps.plan(2000000, 250000, stats)
    assert typical["month"]["sale"] == 8 and typical["from_typical"] is True
    gap = laps.plan(2000000, None, {"rates": {"appointment": None, "presentation": 0.5, "sale": 0.5}, "average_sale_pence": 500000})
    assert gap["month"] == {"sale": 4, "presentation": 8, "appointment": 16, "lead": None} and gap["missing"] == "appointment"
    assert laps.plan(None, None, stats) is None
    assert laps.plan(100000, None, {"rates": {}, "average_sale_pence": None})["missing"] == "value"


def test_money_in_reads_cleared_money_only(db):
    start, end = laps.periods(NOW)["this_month"]
    person = Person(person_key="p@example.test", name="P")
    db.add(person)
    db.flush()
    mentee = Mentee(person_id=person.id, programme_id=db.query(Programme).first().id, payment_mode="full", start_date=NOW.date())
    db.add(mentee)
    db.flush()
    db.add_all([Payment(mentee_id=mentee.id, amount=Decimal("1200.50"), paid_at=NOW, cleared_at=NOW),
                Payment(mentee_id=mentee.id, amount=Decimal("999"), paid_at=NOW, cleared_at=None),
                Payment(mentee_id=mentee.id, amount=Decimal("50"), paid_at=NOW, cleared_at=NOW - timedelta(days=40))])
    db.commit()
    assert laps.money_in(db, start, end) == {"mentees": 120050, "territory": 0, "products": 0, "total": 120050}


def test_board_page_add_move_and_audit(admin_client, db):
    page = admin_client.get("/laps")
    assert page.status_code == 200 and "LAPS board" in page.text and "Nothing here." in page.text
    assert 'href="/laps"' in admin_client.get("/").text

    added = admin_client.post("/laps/deals", data={"name": "  Sample   Buyer ", "brand": "mhp", "offer": "Mentorship",
                                                    "value": "£3,000", "source": "LinkedIn", "next_step": "Book a call"})
    assert added.status_code == 303 and "ok=" in added.headers["location"]
    row = db.query(LapsDeal).one()
    assert (row.name, row.stage, row.value_pence, row.source) == ("Sample Buyer", "lead", 300000, "LinkedIn")

    page = admin_client.get("/laps")
    assert "Sample Buyer" in page.text and "Appointment booked" in page.text and "£3,000" in page.text
    assert admin_client.post(f"/laps/deals/{row.id}/stage", data={"to": "presentation"}).status_code == 303
    assert admin_client.post(f"/laps/deals/{row.id}/stage", data={"to": "sale", "value": "3500"}).status_code == 303
    db.expire_all()
    assert row.stage == "sale" and row.value_pence == 350000 and row.appointment_at and row.sale_at
    assert admin_client.post(f"/laps/deals/{row.id}/stage", data={"to": "lost"}).headers["location"].count("err=") == 1
    db.expire_all()
    assert row.stage == "sale", "a refused move changes nothing"
    actions = [a.action for a in db.query(AuditLog).filter(AuditLog.action.like("laps.%")).order_by(AuditLog.id)]
    assert actions == ["laps.deal.add", "laps.deal.move", "laps.deal.move"]
    assert "£3,500" in admin_client.get("/laps").text


def test_add_refuses_bad_input_without_creating_a_deal(admin_client, db):
    for data in ({"name": "", "brand": "mhp"}, {"name": "A", "brand": "nope"}, {"name": "A", "brand": "mhp", "value": "lots"},
                 {"name": "A", "brand": "mhp", "arrived": "2999-01-01"}, {"name": "A", "brand": "mhp", "next_step_on": "soon"},
                 {"name": "A", "brand": "mhp", "stage": "lost"}):
        response = admin_client.post("/laps/deals", data=data)
        assert response.status_code == 303 and "err=" in response.headers["location"], data
    assert db.query(LapsDeal).count() == 0


def test_backdated_lead_added_at_a_later_stage_keeps_its_date(admin_client, db):
    admin_client.post("/laps/deals", data={"name": "Earlier", "brand": "kp", "arrived": "2026-10-01", "stage": "appointment"})
    row = db.query(LapsDeal).one()
    assert row.stage == "appointment" and row.lead_at == row.appointment_at == datetime(2026, 10, 1, 11), "midday BST"


def test_deal_page_edits_fields_and_escapes_text(admin_client, db):
    row = deal(db, "<script>alert(1)</script>", at=laps.utcnow())
    page = admin_client.get(f"/laps/deals/{row.id}")
    assert page.status_code == 200 and "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text
    saved = admin_client.post(f"/laps/deals/{row.id}", data={"name": "Renamed", "brand": "pp", "value": "", "notes": "Called twice",
                                                             "next_step_on": "2026-12-01"})
    assert saved.status_code == 303 and "ok=" in saved.headers["location"]
    db.expire_all()
    assert (row.name, row.brand, row.notes, row.next_step_on.isoformat()) == ("Renamed", "pp", "Called twice", "2026-12-01")
    assert admin_client.post(f"/laps/deals/{row.id}", data={"name": "", "brand": "pp"}).headers["location"].count("err=") == 1
    assert admin_client.get("/laps/deals/999999").status_code == 404
    assert "&lt;script&gt;" not in admin_client.get("/laps").text and "Renamed" in admin_client.get("/laps").text


def test_crm_lead_is_offered_and_added_to_the_board_once(admin_client, db):
    lead = CampaignLead(brand="pp", source_system="r72", external_id="x1", display_name="CRM Person", source="Webinar",
                        lifecycle="lead")
    db.add(lead)
    db.commit()
    assert "CRM leads not on the board" in admin_client.get("/laps").text
    assert "ok=" in admin_client.post("/laps/import", data={"lead_id": lead.id}).headers["location"]
    assert "err=" in admin_client.post("/laps/import", data={"lead_id": lead.id}).headers["location"]
    row = db.query(LapsDeal).one()
    assert (row.name, row.brand, row.source, row.origin) == ("CRM Person", "pp", "Webinar", f"crm:{lead.id}")
    assert "CRM leads not on the board" not in admin_client.get("/laps").text


def test_targets_are_saved_and_shown_as_a_plan(admin_client, db):
    assert "Set a monthly income goal" in admin_client.get("/laps").text
    assert "err=" in admin_client.post("/laps/targets", data={"monthly_goal": "plenty"}).headers["location"]
    assert "ok=" in admin_client.post("/laps/targets", data={"monthly_goal": "10,000", "typical_sale": "2500"}).headers["location"]
    assert laps.targets(db) == {"monthly_goal_pence": 1000000, "typical_sale_pence": 250000}
    page = admin_client.get("/laps").text
    assert "4 sales a month" in page and "£10,000" in page
    admin_client.post("/laps/targets", data={"monthly_goal": "", "typical_sale": ""})
    db.expire_all()
    assert laps.targets(db) == {"monthly_goal_pence": None, "typical_sale_pence": None}


def test_board_needs_a_signed_in_admin_and_blocks_cross_site_posts(client, admin_client, db):
    assert client.get("/laps").status_code in (303, 307)
    assert client.post("/laps/deals", data={"name": "A", "brand": "mhp"}).status_code in (303, 307, 401, 403)
    blocked = admin_client.post("/laps/deals", data={"name": "A", "brand": "mhp"}, headers={"Origin": "https://evil.test"})
    assert blocked.status_code == 403 and db.query(LapsDeal).count() == 0
