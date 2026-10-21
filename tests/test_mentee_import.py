"""Existing users to mentees: the Key Properties list method, the import page (live rows with badges, dry run and
failure notices), pasted lines in both forms with junk refused, link ids filled only where empty, a mentee already
on the programme skipped, payment and grants recorded (planned in dry run, sent live, an HNH temporary password
shown once and never stored), one person's failure not stopping the batch, the admin and CSRF guards, and the
batch cap. respx stands in for every product; conftest keeps every connector in dry run unless a test says live."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import text

from app import config
from app.models import AuditLog, ConnectorCall, EntitlementGrant, Mentee, Payment, Person, Programme, User, utcnow
from app.routers import mentees as mentees_router
from app.services import mentee_import as imp
from app.services.connectors import ConnectorError, KPConnector, open_connectors
from tests.conftest import ADMIN_EMAIL, session
from tests.test_people import audit_rows, create_person

TODAY = utcnow().date()
PP = "https://pp.example.test"
ORDRIS = "https://ordris.example.test"
KP = "https://kp.example.test"
HNH = "https://hnh.example.test"
PASSWORD = "Tq7xK2mN9pLw"


# ---- helpers -------------------------------------------------------------------------------------------
def live(monkeypatch, dry: tuple[str, ...] = ("pp",)) -> None:
    """Production's shape by default: Ordris, Key Properties and Hello New Home live, Property Predator in dry run."""
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset(dry))


LIST_FIELDS = ("id", "email", "name", "company", "source", "pp_account_id", "active", "created_at", "deals")


def kp_row(sid: int, email: str, name: str = "", company: str = "", source: str = "invite", deals: int = 0, **kw) -> dict:
    """A sourcer as the portal holds it: sourcerOut (the lookup and the POST) plus the deal count the list adds.
    pp_account_id comes back as a string, as the portal sends it."""
    row = {"ok": True, "id": sid, "email": email, "name": name, "company": company, "source": source, "pp_account_id": None,
           "fee_share_pct": 50, "active": True, "created_at": "2026-09-01T10:00:00.000Z", "last_login": None, "deals": deals}
    row.update(kw)
    return row


def kp_portal(rows: list[dict], calls: list | None = None):
    """GET /api/partner/sourcers as the grouphq-connector branch answers it: without ?email= one page of list rows
    (sourcerListRow: no fee share, no last sign-in) with the total; with it, that sourcer's sourcerOut or 404."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(dict(request.url.params))
        email = request.url.params.get("email")
        if email is None:
            limit, offset = int(request.url.params.get("limit", 200)), int(request.url.params.get("offset", 0))
            page = [{k: r.get(k) for k in LIST_FIELDS} for r in rows[offset:offset + limit]]
            return httpx.Response(200, json={"ok": True, "sourcers": page, "total": len(rows), "limit": limit, "offset": offset})
        found = next((r for r in rows if r["email"] == email), None)
        if found is None:
            return httpx.Response(404, json={"ok": False, "error": "not_found", "message": "No sourcer with that email."})
        return httpx.Response(200, json={k: v for k, v in found.items() if k != "deals"})
    return handler


def ordris_portal(known: dict[str, dict], fail: set[str] = frozenset()):
    def handler(request: httpx.Request) -> httpx.Response:
        email = request.url.params.get("email")
        if email in fail:
            raise httpx.ConnectError("connection refused", request=request)
        if email in known:
            return httpx.Response(200, json={"ok": True, "email": email, **known[email]})
        return httpx.Response(404, json={"ok": False, "reason": "no_account"})
    return handler


def hnh_portal(known: dict[str, dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        email = request.url.params.get("email")
        if email in known:
            return httpx.Response(200, json={"ok": True, "found": True, "partner": {"email": email, **known[email]}})
        return httpx.Response(200, json={"ok": True, "found": False, "partner": None})
    return handler


def mock_products(mock, *, kp_rows=(), ordris=None, ordris_fail=frozenset(), hnh=None, kp_calls=None):
    mock.get(f"{KP}/api/partner/sourcers").mock(side_effect=kp_portal(list(kp_rows), kp_calls))
    mock.get(f"{ORDRIS}/api/partner/grouphq/lookup").mock(side_effect=ordris_portal(ordris or {}, set(ordris_fail)))
    mock.get(f"{HNH}/api/partner/partners").mock(side_effect=hnh_portal(hnh or {}))


def programme_id(code: str = "mentorship") -> int:
    with session() as db:
        return db.query(Programme).filter(Programme.code == code).one().id


def form(lines: str = "", *, code: str = "mentorship", mode: str = "full", amount: str = "", cleared: bool = True,
         period_end: str = "", grant_now: bool = True, picks: list[tuple[str, str]] = (), **extra) -> dict:
    data = {"lines": lines, "programme_id": str(programme_id(code)), "payment_mode": mode, "start_date": TODAY.isoformat(),
            "amount": amount, "paid_on": TODAY.isoformat(), "period_end": period_end, "offset": "0"}
    if cleared:
        data["cleared"] = "1"
    if grant_now:
        data["grant_now"] = "1"
    if picks:
        data["kp_email"] = [e for e, _ in picks]
        data["kp_name"] = [n for _, n in picks]
        data["pick"] = [str(i) for i in range(len(picks))]
    data.update(extra)
    return data


def person_by(email: str) -> Person | None:
    with session() as db:
        return db.query(Person).filter(Person.person_key == email).first()


def set_links(c, person_id: int, **links) -> None:
    data = {col: "" for col in ("pp_account_id", "ordris_user_id", "ordris_org_id", "kp_user_id", "kp_investor_id",
                                "hnh_partner_id", "hnh_user_id")}
    data.update({k: str(v) for k, v in links.items()})
    assert c.post(f"/people/{person_id}/links", data=data).status_code == 303


# ---- the Key Properties list method ----------------------------------------------------------------------
def test_kp_list_sourcers_dry_run_plans_the_call_and_live_reads_every_shape():
    plan = KPConnector.from_config(dry_run=True).list_sourcers(limit=500, offset=-3)
    assert plan.dry_run and plan.data is None
    assert plan.plan["method"] == "GET" and plan.plan["url"] == f"{KP}/api/partner/sourcers"
    assert plan.plan["params"] == {"limit": 200, "offset": 0}, "limit is clamped to 200, offset to zero, and no email is sent"
    assert plan.plan["headers"]["Authorization"] == "Bearer [redacted]"

    rows = [kp_row(41, " Amy@Example.TEST ", "Amy Archer", "Archer Homes Ltd", "mentee", 3),
            kp_row(42, "bob@example.test", deals=[{"id": 1}, {"id": 2}], pp_account_id="77"),
            {"id": 43, "email": "", "name": "No email"}, "junk",
            kp_row(44, "a" * 242 + "@example.test", "Too Long")]           # 255 characters: left out, never cut short
    with respx.mock(base_url=KP) as mock:
        route = mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(200, json={"ok": True, "sourcers": rows, "total": 12}))
        result = KPConnector.from_config().list_sourcers(limit=2, offset=10)
        request = route.calls.last.request
        assert dict(request.url.params) == {"limit": "2", "offset": "10"} and "email" not in request.url.params
        assert request.headers["authorization"] == f"Bearer {config.KP_PARTNER_TOKEN}" and request.headers["x-partner-client"] == "grouphq"
        assert result.data["total"] == 12 and result.data["limit"] == 2 and result.data["offset"] == 10
        amy, bob = result.data["sourcers"]
        assert amy == {"id": 41, "email": "amy@example.test", "name": "Amy Archer", "company": "Archer Homes Ltd", "source": "mentee",
                       "active": True, "deals": 3, "pp_account_id": None, "created_at": "2026-09-01T10:00:00.000Z"}
        assert bob["deals"] == 2 and bob["pp_account_id"] == 77, "a list of deals counts; the account id arrives as a string"
        # a bare list, and the count under another name
        mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(200, json=[{"id": 5, "email": "c@example.test", "deal_count": "7"}]))
        only = KPConnector.from_config().list_sourcers()
        assert only.data["sourcers"][0]["deals"] == 7 and only.data["total"] is None
        # the portal before the list existed: 422 invalid, raised with its reason
        mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(422, json={"ok": False, "error": "invalid", "message": "Send ?email= with the address to look up."}))
        with pytest.raises(ConnectorError) as raised:
            KPConnector.from_config().list_sourcers()
        assert raised.value.reason == "invalid" and raised.value.status == 422
        # a single row where a list belongs is a bad answer, never an empty page
        mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(200, json=kp_row(41, "amy@example.test")))
        with pytest.raises(ConnectorError) as raised:
            KPConnector.from_config().list_sourcers()
        assert raised.value.reason == "bad_response"
        mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(200, json={"ok": False, "error": "paused"}))
        with pytest.raises(ConnectorError) as raised:
            KPConnector.from_config().list_sourcers()
        assert raised.value.reason == "paused"


# ---- the page ------------------------------------------------------------------------------------------
def test_page_lists_kp_sourcers_with_register_and_mentee_badges(admin_client, monkeypatch):
    live(monkeypatch)
    amy = create_person(admin_client, "amy@example.test", "Amy Archer")
    with session() as db:
        db.add(Mentee(person_id=amy, programme_id=programme_id(), payment_mode="full", start_date=TODAY, status="active"))
        db.commit()
    create_person(admin_client, "bob@example.test")
    rows = [kp_row(41, "amy@example.test", "Amy Archer", "Archer Homes Ltd", "mentee", 3),
            kp_row(42, "bob@example.test", "Bob Brown", "", "pp", 0),
            kp_row(43, "cara@example.test", "Cara Lee", "Lee Lettings", "invite", 11, active=False)]
    calls: list = []
    with respx.mock(assert_all_called=False) as mock:
        mock_products(mock, kp_rows=rows, kp_calls=calls)
        r = admin_client.get("/mentees/import")
    assert r.status_code == 200, r.text[:400]
    assert calls == [{"limit": "100", "offset": "0"}], "one live list call, with no email"
    page = r.text
    for expected in ("Amy Archer", "amy@example.test", "Archer Homes Ltd", "Bob Brown", "Cara Lee", "Lee Lettings", ">11<",
                     "closed on Key Properties", "1 to 3 of 3"):
        assert expected in page, expected
    assert page.count("already on the register") == 2
    assert page.count("already a mentee on Mentorship") == 1 and 'href="/mentees/1"' in page
    assert 'name="kp_email" value="cara@example.test"' in page and 'name="pick" value="2"' in page
    assert "Property Predator: dry run" in page and "Key Properties: live" in page and "Ordris: live" in page
    assert 'name="lines"' in page and 'name="grant_now" value="1" checked' in page and 'name="cleared" value="1" checked' in page
    assert f'value="{TODAY.isoformat()}"' in page
    # the way in: the Mentees list and the side navigation
    listing = admin_client.get("/mentees").text
    assert listing.count('href="/mentees/import"') == 2, "one link in the page actions, one in the side navigation"


def test_kp_in_dry_run_or_unreachable_says_so_and_the_rest_still_works(admin_client, monkeypatch):
    with respx.mock(assert_all_called=False) as mock:
        r = admin_client.get("/mentees/import")
        assert mock.calls.call_count == 0, "a product in dry run is never called"
    assert r.status_code == 200
    assert "Key Properties is in dry run here" in r.text and "/api/partner/sourcers" in r.text
    assert 'name="lines"' in r.text and "Import and enrol" in r.text and 'name="pick"' not in r.text

    live(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{KP}/api/partner/sourcers").mock(side_effect=httpx.ConnectError("refused"))
        r = admin_client.get("/mentees/import")
    assert r.status_code == 200 and "could not list its sourcers just now" in r.text and "unreachable" in r.text
    assert 'name="lines"' in r.text
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{KP}/api/partner/sourcers").mock(return_value=httpx.Response(422, json={"ok": False, "error": "invalid"}))
        assert "invalid (HTTP 422)" in admin_client.get("/mentees/import").text
    monkeypatch.setattr(config, "KP_PARTNER_TOKEN", "")
    assert "has no base URL or token" in admin_client.get("/mentees/import").text


# ---- pasted lines ----------------------------------------------------------------------------------------
def test_pasted_lines_take_both_forms_and_refuse_junk_with_a_reason():
    entries, problems = imp.parse_lines("jo@example.test\n  Jo Two , JO2@Example.TEST \n\n\"Bloggs, Jo\", jo3@example.test\n"
                                        "not an email\nJo Bloggs jo4@example.test\njo5@example.test, Jo Five\n"
                                        "a@example.test, b@example.test\nJo Six,\nJo Seven, jo7@exämple.test\n")
    assert [(e.email, e.name, e.origin) for e in entries] == [
        ("jo@example.test", "", "pasted line 1"), ("jo2@example.test", "Jo Two", "pasted line 2"),
        ("jo3@example.test", "Bloggs, Jo", "pasted line 4")]
    assert len(problems) == 6
    assert problems[0].startswith("Line 5 (not an email):") and "does not look right" in problems[0]
    assert problems[1].startswith("Line 6") and "put a comma between the name and the email" in problems[1]
    assert problems[2].startswith("Line 7") and "the email comes last" in problems[2]
    assert problems[3].startswith("Line 8") and "one person per line" in problems[3]
    assert problems[4].startswith("Line 9") and "no email after the comma" in problems[4]
    assert problems[5].startswith("Line 10") and "plain ASCII" in problems[5]
    assert "longer than 120" in imp.parse_line("x" * 121 + ", long@example.test")[1]


def test_a_junk_line_stops_the_batch_before_anything_is_written(admin_client):
    lines = "Jo Bloggs, jo@example.test\nnot an email\n"
    r = admin_client.post("/mentees/import", data=form(lines, amount="100"))
    assert r.status_code == 400
    assert "Line 2 (not an email)" in r.text and "nothing was imported" in r.text
    assert "Jo Bloggs, jo@example.test" in r.text, "what was typed is kept"
    with session() as db:
        assert db.query(Person).count() == 0 and db.query(Mentee).count() == 0
    assert not audit_rows("mentee.import")
    r = admin_client.post("/mentees/import", data=form(""))
    assert r.status_code == 400 and "Tick at least one Key Properties user or paste at least one line" in r.text


def test_shared_fields_are_checked(admin_client):
    r = admin_client.post("/mentees/import", data=form("jo@example.test", mode="monthly", amount="100"))
    assert r.status_code == 400 and "monthly payment needs a period end" in r.text
    end = (TODAY + timedelta(days=60)).isoformat()
    r = admin_client.post("/mentees/import", data=form("jo@example.test", mode="monthly", amount="100", period_end=end))
    assert r.status_code == 400 and "one month and a week" in r.text
    r = admin_client.post("/mentees/import", data=form("jo@example.test", amount="12.345"))
    assert r.status_code == 400 and "at most two decimal places" in r.text
    with session() as db:
        db.query(Programme).filter(Programme.code == "kp_mentee").one().active = False
        db.commit()
    r = admin_client.post("/mentees/import", data=form("jo@example.test", code="kp_mentee"))
    assert r.status_code == 400 and "is not active" in r.text
    with session() as db:
        assert db.query(Person).count() == 0


# ---- links ---------------------------------------------------------------------------------------------
def test_existing_person_keeps_hand_set_ids_and_empty_ids_are_filled(admin_client, monkeypatch):
    live(monkeypatch, dry=())
    amy = create_person(admin_client, "amy@example.test")                   # no name yet
    set_links(admin_client, amy, ordris_user_id="user_by_hand", kp_investor_id=5)
    bob = create_person(admin_client, "bob@example.test", "Robert Brown")
    cat = create_person(admin_client, "cat@example.test", "Cat Old Email")   # hand-linked to KP user 41 under an old email
    set_links(admin_client, cat, kp_user_id=41)
    with respx.mock(assert_all_called=False) as mock:
        mock_products(mock, kp_rows=[kp_row(41, "amy@example.test", "Amy Archer")],
                      ordris={"amy@example.test": {"user_id": "user_from_ordris", "org_id": "org_9"}},
                      hnh={"amy@example.test": {"id": 9, "user_id": 31}})
        pp = mock.get(f"{PP}/api/partner/grouphq/account").mock(
            side_effect=lambda request: httpx.Response(200, json={"account_id": 77, "tier": "free"}))
        r = admin_client.post("/mentees/import", data=form("Amy Archer, amy@example.test\nBob B, bob@example.test", code="kp_mentee"))
    assert r.status_code == 200, r.text[:600]
    assert pp.call_count == 2
    with session() as db:
        a = db.get(Person, amy)
        assert a.name == "Amy Archer", "a nameless person takes the name from the row"
        assert a.ordris_user_id == "user_by_hand", "an id set by hand is never overwritten"
        assert a.kp_investor_id == 5
        assert (a.ordris_org_id, a.kp_user_id, a.hnh_partner_id, a.hnh_user_id, a.pp_account_id) == ("org_9", None, 9, 31, 77)
        assert db.get(Person, cat).kp_user_id == 41, "a KP user id already linked to someone else is not linked twice"
        b = db.get(Person, bob)
        assert b.name == "Robert Brown", "a name already recorded is kept"
        assert b.pp_account_id is None, "a PP account already linked to someone else is not linked twice"
    page = r.text
    assert "kept the recorded Ordris clerk user id user_by_hand (Ordris answered user_from_ordris)" in page
    assert "Property Predator account id (users.id) 77 is already linked to amy@example.test" in page
    assert "Key Properties user id 41 is already linked to cat@example.test, so it was not linked here" in page
    assert "Ordris, Key Properties, Hello New Home, Property Predator" in page
    links = audit_rows("person.links")[-1]
    assert links.entity == f"person:{amy}" and links.after["via"] == "import lookup"
    assert links.after["hnh_partner_id"] == 9 and links.before["hnh_partner_id"] is None
    assert "ordris_user_id" not in links.after and "kp_user_id" not in links.after
    named = audit_rows("person.update")
    assert len(named) == 1 and named[0].before == {"name": ""} and named[0].after["name"] == "Amy Archer"


def test_dry_run_products_are_not_asked(admin_client):
    with respx.mock(assert_all_called=False) as mock:
        r = admin_client.post("/mentees/import", data=form("Jo Bloggs, jo@example.test"))
        assert mock.calls.call_count == 0
    assert r.status_code == 200 and "none of the live products" in r.text
    jo = person_by("jo@example.test")
    assert jo is not None and jo.name == "Jo Bloggs" and jo.kp_user_id is None
    created = audit_rows("person.create")
    assert len(created) == 1 and created[0].after["via"] == "import" and created[0].after["person_key"] == "jo@example.test"


# ---- mentees, payments and grants ----------------------------------------------------------------------
def test_a_person_already_on_the_programme_is_skipped_and_their_row_left_alone(admin_client):
    jo = create_person(admin_client, "jo@example.test", "Jo")
    with session() as db:
        existing = Mentee(person_id=jo, programme_id=programme_id(), payment_mode="monthly", start_date=TODAY, status="lapsed")
        db.add(existing)
        db.commit()
        existing_id = existing.id
    r = admin_client.post("/mentees/import", data=form("jo@example.test\nKim New, kim@example.test", amount="100"))
    assert r.status_code == 200
    assert f"already a mentee on Mentorship (mentee #{existing_id}, lapsed)" in r.text
    assert "not recorded: add it on that mentee&#39;s Payments tab if it is new" in r.text
    with session() as db:
        assert db.query(Mentee).filter(Mentee.person_id == jo).count() == 1
        assert db.get(Mentee, existing_id).payment_mode == "monthly" and db.get(Mentee, existing_id).status == "lapsed"
        kim = db.query(Person).filter(Person.person_key == "kim@example.test").one()
        assert db.query(Mentee).filter(Mentee.person_id == kim.id).count() == 1
        payments = db.query(Payment).all()
        assert len(payments) == 1 and payments[0].mentee.person_id == kim.id
    batch = audit_rows("mentee.import")
    assert len(batch) == 1 and batch[0].after["mentees_created"] == 1 and batch[0].after["mentees_skipped"] == 1


def test_payment_and_grants_are_created_in_dry_run(admin_client):
    r = admin_client.post("/mentees/import", data=form("Cara Lee, cara@example.test", amount="1,250.00"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    with session() as db:
        cara = db.query(Person).filter(Person.person_key == "cara@example.test").one()
        mentee = db.query(Mentee).filter(Mentee.person_id == cara.id).one()
        assert mentee.programme.code == "mentorship" and mentee.payment_mode == "full" and mentee.start_date == TODAY
        assert mentee.status == "active" and "existing-users import" in mentee.notes
        payment = db.query(Payment).one()
        midnight = datetime.combine(TODAY, datetime.min.time())
        assert payment.amount == Decimal("1250.00") and payment.paid_at == midnight and payment.cleared_at == midnight
        assert payment.method == "manual" and payment.period_end is None and "existing-users import" in payment.notes
        grants = db.query(EntitlementGrant).filter(EntitlementGrant.person_id == cara.id).all()
        assert len(grants) == 5 and {g.product for g in grants} == {"pp", "ordris", "hnh", "kp"}
        calls = db.query(ConnectorCall).all()
        assert calls and all(c.status == "pending" for c in calls), "planned in dry run, sent by the next live run"
    page = r.text
    assert "£1,250.00 paid" in page and "planned" in page and "Key Properties sourcer link mentee" in page
    assert audit_rows("mentee.create")[0].after["via"] == "import"
    assert audit_rows("payment.create")[0].after["pence"] == 125000
    recompute = audit_rows("mentee.recompute")
    assert len(recompute) == 1 and recompute[0].after["outcomes"] == {"planned": 5}
    assert audit_rows("mentee.import")[0].after["grants"] == {"planned": 5}


def test_monthly_payment_runs_from_the_start_date_and_grants_wait_when_asked(admin_client):
    end = (mentees_router.add_months(TODAY, 1) - timedelta(days=1)).isoformat()
    r = admin_client.post("/mentees/import", data=form("Mo Nthly, mo@example.test", mode="monthly", amount="99", period_end=end,
                                                       grant_now=False))
    assert r.status_code == 200 and "the nightly recompute at 03:10 will grant" in r.text
    with session() as db:
        payment = db.query(Payment).one()
        assert payment.period_start == TODAY and payment.period_end.isoformat() == end and payment.cleared_at is not None
        assert db.query(EntitlementGrant).count() == 0
    r = admin_client.post("/mentees/import", data=form("Un Cleared, un@example.test", amount="99", cleared=False))
    assert "nothing to grant until a payment is recorded and cleared" in r.text and "not yet cleared" in r.text
    r = admin_client.post("/mentees/import", data=form("No Pay, nopay@example.test"))
    assert "none entered" in r.text
    with session() as db:
        assert db.query(EntitlementGrant).count() == 0 and db.query(Payment).count() == 2
        assert db.query(Payment).filter(Payment.cleared_at.is_(None)).count() == 1


def test_live_grants_go_out_and_the_hnh_password_is_shown_once_and_never_stored(admin_client, monkeypatch):
    live(monkeypatch)
    created_sourcer = kp_row(41, "dee@example.test", "Dee New", source="mentee", created=True, linked=False)
    created_partner = {"ok": True, "created": True, "partner_id": 9, "user_id": 31, "one_time_password": PASSWORD,
                       "partner": {"id": 9, "user_id": 31, "email": "dee@example.test", "name": "Dee New", "status": "active"}}
    with respx.mock(assert_all_called=False) as mock:
        mock_products(mock)
        kp_post = mock.post(f"{KP}/api/partner/sourcers").mock(return_value=httpx.Response(201, json=created_sourcer))
        ordris_post = mock.post(f"{ORDRIS}/api/partner/grouphq/entitlements").mock(
            return_value=httpx.Response(200, json={"ok": True, "user_id": "user_1", "org_id": "org_1", "plan": "starter"}))
        hnh_post = mock.post(f"{HNH}/api/partner/partners").mock(return_value=httpx.Response(201, json=created_partner))
        r = admin_client.post("/mentees/import", data=form("Dee New, dee@example.test", amount="2000"))
    assert r.status_code == 200, r.text[:600]
    assert kp_post.call_count == ordris_post.call_count == hnh_post.call_count == 1
    assert json.loads(kp_post.calls.last.request.content)["email"] == "dee@example.test"
    page = r.text
    assert PASSWORD in page and "Pass this on now" in page
    assert page.count('class="status status-sent"') == 3, "Ordris, Hello New Home and Key Properties are live"
    assert page.count('class="status status-planned"') == 2, "PP is in dry run, so its two rights are planned"
    with session() as db:
        dee = db.query(Person).filter(Person.person_key == "dee@example.test").one()
        assert (dee.kp_user_id, dee.ordris_user_id, dee.hnh_partner_id) == (41, "user_1", 9), "the grants filled the empty ids"
        stored = db.execute(text("SELECT count(*) FROM audit_log WHERE coalesce(before::text, '') || coalesce(after::text, '') LIKE :p"),
                            {"p": f"%{PASSWORD}%"}).scalar()
        stored += db.execute(text("SELECT count(*) FROM connector_calls WHERE payload::text || coalesce(response::text, '') LIKE :p"),
                             {"p": f"%{PASSWORD}%"}).scalar()
        assert stored == 0, "the temporary password is never written to the database"


# ---- failures stay with the person they belong to ------------------------------------------------------
def test_a_connector_failure_for_one_person_does_not_stop_the_others(admin_client, monkeypatch):
    live(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        mock_products(mock, ordris={"cy@example.test": {"user_id": "user_cy", "org_id": "org_cy"}}, ordris_fail={"bo@example.test"})
        r = admin_client.post("/mentees/import", data=form("Al One, al@example.test\nBo Two, bo@example.test\nCy Three, cy@example.test",
                                                           code="kp_mentee"))
    assert r.status_code == 200
    assert "Ordris lookup failed: unreachable" in r.text
    with session() as db:
        for email in ("al@example.test", "bo@example.test", "cy@example.test"):
            person = db.query(Person).filter(Person.person_key == email).one()
            assert db.query(Mentee).filter(Mentee.person_id == person.id).count() == 1, email
        assert db.query(Person).filter(Person.person_key == "cy@example.test").one().ordris_org_id == "org_cy"
    assert audit_rows("mentee.import")[0].after["lookup_failures"] == 1


def test_a_product_that_keeps_failing_is_not_asked_again_in_the_batch(admin_client, monkeypatch):
    live(monkeypatch)
    emails = [f"p{i}@example.test" for i in range(5)]
    with respx.mock(assert_all_called=False) as mock:
        mock_products(mock)
        kp = mock.get(f"{KP}/api/partner/sourcers").mock(side_effect=httpx.ConnectTimeout("slow"))
        r = admin_client.post("/mentees/import", data=form("\n".join(emails), code="kp_mentee"))
    assert r.status_code == 200
    assert kp.call_count == imp.TRIP_AFTER
    assert r.text.count("Key Properties lookup failed: timeout") == imp.TRIP_AFTER
    assert r.text.count(f"it failed for the previous {imp.TRIP_AFTER} people in this batch") == 5 - imp.TRIP_AFTER
    with session() as db:
        assert db.query(Mentee).count() == 5


def test_a_product_that_keeps_failing_holds_back_the_later_grants(admin_client, monkeypatch):
    """Once Key Properties has failed for TRIP_AFTER people running, a kp_mentee's recompute (which would only wait on
    it again) is not run for the rest of the batch: the nightly recompute grants them instead."""
    live(monkeypatch)
    emails = [f"p{i}@example.test" for i in range(5)]
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ORDRIS}/api/partner/grouphq/lookup").mock(side_effect=ordris_portal({}))
        mock.get(f"{HNH}/api/partner/partners").mock(side_effect=hnh_portal({}))
        mock.route(host="kp.example.test").mock(side_effect=httpx.ConnectTimeout("slow"))
        r = admin_client.post("/mentees/import", data=form("\n".join(emails), code="kp_mentee", amount="500"))
    assert r.status_code == 200
    held = f"not run: Key Properties failed for {imp.TRIP_AFTER} people in a row in this batch; the nightly recompute at 03:10 will grant"
    assert r.text.count(held) == 5 - imp.TRIP_AFTER + 1, "from the person whose lookup tripped it onwards"
    assert len(audit_rows("mentee.recompute")) + len(audit_rows("mentee.recompute_failed")) == imp.TRIP_AFTER - 1
    with session() as db:
        assert db.query(Mentee).count() == 5 and db.query(Payment).count() == 5, "everyone is still enrolled and paid"
    assert audit_rows("mentee.import")[0].after["recomputed"] <= imp.TRIP_AFTER - 1


def test_a_double_submit_for_someone_already_on_the_register_enrols_and_records_the_payment_once(monkeypatch):
    """Two batches for the same existing person at once (a second click, or a retry after a proxy timeout): the
    person's row is locked, so the second waits for the first's commit, sees its enrolment and skips."""
    with session() as db:
        db.add(Person(person_key="jo@example.test", name="Jo"))
        db.commit()
    barrier = threading.Barrier(2, timeout=2)
    real = mentees_router.mentee_snapshot

    def meet(mentee):                   # after the mentee INSERT is flushed, before the per-person commit
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real(mentee)
    monkeypatch.setattr(mentees_router, "mentee_snapshot", meet)
    results: list[tuple[str, str]] = []

    def submit():
        with session() as db:
            user = db.query(User).filter(User.email == ADMIN_EMAIL).one()
            shared, errors = imp.check_shared(db, {"programme_id": str(programme_id()), "payment_mode": "full",
                                                   "start_date": TODAY.isoformat(), "amount": "1250", "paid_on": TODAY.isoformat(),
                                                   "cleared": "1", "grant_now": ""})
            assert not errors, errors
            with open_connectors() as connectors:
                out = imp.import_one(db, user, imp.Entry(email="jo@example.test"), shared, imp.LookupRunner(connectors), connectors)
            results.append((out.mentee, out.error))
    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == [("created", ""), ("skipped", "")], results
    with session() as db:
        assert db.query(Mentee).count() == 1 and db.query(Payment).count() == 1
    assert len(audit_rows("mentee.create")) == 1 and len(audit_rows("payment.create")) == 1


def test_a_write_or_recompute_failure_for_one_person_is_reported_and_the_rest_go_on(admin_client, monkeypatch):
    real_enrol = imp._enrol

    def enrol_or_fail(db, user, person, shared, out):
        if person.person_key == "bad@example.test":
            raise RuntimeError("database said no")
        return real_enrol(db, user, person, shared, out)

    def recompute(db, person, actor, **kw):
        if person.person_key == "slow@example.test":
            raise RuntimeError("Ordris did not answer")
        return []

    monkeypatch.setattr(imp, "_enrol", enrol_or_fail)
    monkeypatch.setattr(mentees_router, "recompute_person", recompute)
    lines = "Good One, good@example.test\nBad Two, bad@example.test\nSlow Three, slow@example.test"
    r = admin_client.post("/mentees/import", data=form(lines, amount="500"))
    assert r.status_code == 200
    assert "Not imported, nothing was saved for this person: database said no" in r.text
    assert "recompute failed: Ordris did not answer" in r.text
    with session() as db:
        assert db.query(Person).filter(Person.person_key == "bad@example.test").count() == 0, "the failed person's writes were rolled back"
        assert db.query(Mentee).count() == 2 and db.query(Payment).count() == 2
    assert audit_rows("mentee.recompute_failed")[0].after["error"] == "Ordris did not answer"
    assert audit_rows("mentee.import")[0].after["failed"] == ["bad@example.test"]


# ---- guards --------------------------------------------------------------------------------------------
def test_import_needs_an_admin_and_refuses_a_cross_site_post(client, admin_client):
    r = client.get("/mentees/import")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/mentees/import"
    r = client.post("/mentees/import", data=form("jo@example.test"))
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = admin_client.post("/mentees/import", data=form("jo@example.test"), headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    with session() as db:
        assert db.query(Person).count() == 0 and db.query(AuditLog).filter(AuditLog.action.like("mentee%")).count() == 0


def test_a_batch_is_capped_and_a_person_listed_twice_is_imported_once(admin_client):
    many = "\n".join(f"p{i}@example.test" for i in range(imp.MAX_BATCH + 1))
    r = admin_client.post("/mentees/import", data=form(many, picks=[("kp@example.test", "Kay Pee")]))
    assert r.status_code == 400 and f"capped at {imp.MAX_BATCH} people and this one has {imp.MAX_BATCH + 2}" in r.text
    with session() as db:
        assert db.query(Person).count() == 0
    # a ticked row and a pasted line for the same address are one person; exactly the cap goes through
    lines = "\n".join(f"p{i}@example.test" for i in range(imp.MAX_BATCH - 1)) + "\nKP@example.test"
    r = admin_client.post("/mentees/import", data=form(lines, picks=[("kp@example.test", "Kay Pee")], grant_now=False))
    assert r.status_code == 200
    assert f"also listed as pasted line {imp.MAX_BATCH}; imported once" in r.text
    with session() as db:
        assert db.query(Person).count() == imp.MAX_BATCH and db.query(Mentee).count() == imp.MAX_BATCH
        assert db.query(Person).filter(Person.person_key == "kp@example.test").one().name == "Kay Pee"
    assert audit_rows("mentee.import")[0].after["people"] == imp.MAX_BATCH
