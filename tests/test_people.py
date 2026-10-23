"""The people register: the person key is the normalised email and is unique whatever the spelling;
search finds by email, name or phone; product ids are set by hand and a PP account id cannot be
held by two people; every write leaves an audit row with only the changed fields."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from app.models import AuditLog, Person
from tests.conftest import ADMIN_EMAIL, session


def flash_of(location: str) -> dict:
    """{'ok': '...'} or {'err': '...'} carried by a redirect."""
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def id_of(location: str) -> int:
    return int(urlsplit(location).path.rstrip("/").split("/")[-1])


def create_person(c, email: str, name: str = "", phone: str = "", notes: str = "") -> int:
    r = c.post("/people/new", data={"email": email, "name": name, "phone": phone, "notes": notes})
    assert r.status_code == 303, r.text[:400]
    return id_of(r.headers["location"])


def audit_rows(action: str) -> list[AuditLog]:
    with session() as db:
        return db.query(AuditLog).filter(AuditLog.action == action).order_by(AuditLog.id).all()


# ---- access -------------------------------------------------------------------------------
def test_register_needs_a_session(client):
    r = client.get("/people")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/people"
    assert client.post("/people/new", data={"email": "x@example.test"}).status_code == 303
    with session() as db:
        assert db.query(Person).count() == 0


def test_cross_site_post_is_blocked(admin_client):
    r = admin_client.post("/people/new", data={"email": "x@example.test"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    with session() as db:
        assert db.query(Person).count() == 0


# ---- create -------------------------------------------------------------------------------
def test_create_person_normalises_the_email(admin_client):
    form = admin_client.get("/people/new", params={"email": "Jo.Bloggs@Example.TEST"})
    assert form.status_code == 200 and 'value="Jo.Bloggs@Example.TEST"' in form.text and "Add to the register" in form.text
    r = admin_client.post("/people/new", data={"email": "  Jo.Bloggs@Example.TEST ", "name": " Jo Bloggs ", "phone": " 07700 900123 ",
                                               "notes": "Met at the Swansea meetup"})
    assert r.status_code == 303
    pid = id_of(r.headers["location"])
    assert "added to the register" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        person = db.get(Person, pid)
        assert person.person_key == "jo.bloggs@example.test"
        assert person.name == "Jo Bloggs" and person.phone == "07700 900123"
        assert person.pp_account_id is None
    rows = audit_rows("person.create")
    assert len(rows) == 1
    assert rows[0].actor == ADMIN_EMAIL and rows[0].entity == f"person:{pid}"
    assert rows[0].after["person_key"] == "jo.bloggs@example.test" and rows[0].before is None

    page = admin_client.get(f"/people/{pid}")
    assert page.status_code == 200
    assert "jo.bloggs@example.test" in page.text and "Jo Bloggs" in page.text
    assert "Linked product ids" in page.text and "Property Predator" in page.text and "Hello New Home" in page.text


def test_duplicate_email_is_refused_whatever_the_spelling(admin_client):
    jo = create_person(admin_client, "jo.bloggs@example.test", "Jo")
    for spelling in ("JO.BLOGGS@example.test", " jo.bloggs@EXAMPLE.test ", "jo.bloggs@example.test"):
        r = admin_client.post("/people/new", data={"email": spelling, "name": "Jo again"})
        assert r.status_code == 400, spelling
        assert "already on the register" in r.text and f"person #{jo}" in r.text
        assert 'value="Jo again"' in r.text, "what was typed is kept on the form"
    with session() as db:
        assert db.query(Person).count() == 1

    # nor can another record be edited onto the same key
    amy = create_person(admin_client, "amy@example.test", "Amy")
    r = admin_client.post(f"/people/{amy}", data={"email": "Jo.Bloggs@Example.test", "name": "Amy", "phone": "", "notes": ""})
    assert r.status_code == 400 and f"person #{jo}" in r.text
    with session() as db:
        assert db.get(Person, amy).person_key == "amy@example.test"
        assert db.query(Person).count() == 2


def test_bad_or_missing_email_is_refused(admin_client):
    for bad in ("", "   ", "not-an-email", "two@@example.test", "spaces in@example.test", "jos\u00e9@example.test",
                "\u0130stanbul@example.test"):
        r = admin_client.post("/people/new", data={"email": bad, "name": "Nobody"})
        assert r.status_code == 400, repr(bad)
    with session() as db:
        assert db.query(Person).count() == 0
    assert not audit_rows("person.create")


# ---- list and search ------------------------------------------------------------------------
def test_list_searches_by_email_name_and_phone(admin_client):
    create_person(admin_client, "jo.bloggs@example.test", "Jo Bloggs", "07700 900123")
    create_person(admin_client, "amy.pond@example.test", "Amy Pond", "07700 900999")
    page = admin_client.get("/people")
    assert page.status_code == 200 and "2 people" in page.text
    assert "jo.bloggs@example.test" in page.text and "amy.pond@example.test" in page.text

    by_email = admin_client.get("/people", params={"q": "BLOGGS"})
    assert "jo.bloggs@example.test" in by_email.text and "amy.pond@example.test" not in by_email.text
    by_name = admin_client.get("/people", params={"q": "pond"})
    assert "amy.pond@example.test" in by_name.text and "jo.bloggs@example.test" not in by_name.text
    by_phone = admin_client.get("/people", params={"q": "900999"})
    assert "amy.pond@example.test" in by_phone.text and "jo.bloggs@example.test" not in by_phone.text
    wildcard = admin_client.get("/people", params={"q": "%"})
    assert "Nobody matches" in wildcard.text, "LIKE wildcards in the search box are literal"
    assert admin_client.get("/people", params={"page": "99"}).status_code == 200


# ---- edit and linked ids -----------------------------------------------------------------------
def test_update_details_audits_only_the_change(admin_client):
    pid = create_person(admin_client, "jo.bloggs@example.test", "Jo", "07700 900123")
    r = admin_client.post(f"/people/{pid}", data={"email": "Jo.Bloggs@example.test", "name": "Jo Bloggs", "phone": "07700 900123", "notes": ""})
    assert r.status_code == 303 and "Person saved" in flash_of(r.headers["location"])["ok"]
    rows = audit_rows("person.update")
    assert len(rows) == 1
    assert rows[0].before == {"name": "Jo"} and rows[0].after == {"name": "Jo Bloggs"}
    r = admin_client.post(f"/people/{pid}", data={"email": "jo.bloggs@example.test", "name": "Jo Bloggs", "phone": "07700 900123", "notes": ""})
    assert r.status_code == 303 and "No changes" in flash_of(r.headers["location"])["ok"]
    assert len(audit_rows("person.update")) == 1
    assert admin_client.get("/people/9999").status_code == 404


def test_linked_ids_are_set_by_hand_and_a_pp_account_belongs_to_one_person(admin_client):
    jo = create_person(admin_client, "jo.bloggs@example.test", "Jo")
    amy = create_person(admin_client, "amy@example.test", "Amy")
    r = admin_client.post(f"/people/{jo}/links", data={"pp_account_id": "42", "ordris_user_id": " user_2abc ", "ordris_org_id": "",
                                                       "kp_user_id": "7", "kp_investor_id": "", "hnh_partner_id": "3", "hnh_user_id": ""})
    assert r.status_code == 303 and "Linked ids saved" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        person = db.get(Person, jo)
        assert person.pp_account_id == "42" and person.ordris_user_id == "user_2abc" and person.ordris_org_id is None
        assert person.kp_user_id == 7 and person.kp_investor_id is None and person.hnh_partner_id == 3 and person.hnh_user_id is None
    rows = audit_rows("person.links")
    assert rows[-1].after == {"pp_account_id": "42", "ordris_user_id": "user_2abc", "kp_user_id": 7, "hnh_partner_id": 3}
    assert rows[-1].before == {"pp_account_id": None, "ordris_user_id": None, "kp_user_id": None, "hnh_partner_id": None}

    # the same PP account on a second person is refused, by name
    r = admin_client.post(f"/people/{amy}/links", data={"pp_account_id": "42"})
    assert r.status_code == 400
    assert "already linked to jo.bloggs@example.test" in r.text
    with session() as db:
        assert db.get(Person, amy).pp_account_id is None

    # a non-numeric id for a numeric column is refused and the typed value stays on the form; a PP id is text (PP's
    # users.id is a UUID) but never one with a space in it
    r = admin_client.post(f"/people/{jo}/links", data={"pp_account_id": "forty two", "kp_user_id": "seven"})
    assert r.status_code == 400 and "whole number" in r.text and 'value="seven"' in r.text and 'value="forty two"' in r.text
    assert "letters, digits and hyphens" in r.text
    with session() as db:
        assert db.get(Person, jo).pp_account_id == "42" and db.get(Person, jo).kp_user_id == 7

    # blanks clear, and an unchanged submission writes nothing
    r = admin_client.post(f"/people/{jo}/links", data={})
    assert r.status_code == 303
    with session() as db:
        person = db.get(Person, jo)
        assert person.pp_account_id is None and person.ordris_user_id is None and person.kp_user_id is None and person.hnh_partner_id is None
    r = admin_client.post(f"/people/{jo}/links", data={})
    assert "unchanged" in flash_of(r.headers["location"])["ok"]
    assert len(audit_rows("person.links")) == 2


def test_email_cannot_change_while_a_grant_is_live(admin_client):
    from datetime import datetime
    from app.models import EntitlementGrant, utcnow
    jo = create_person(admin_client, "jo@example.test", "Jo")
    with session() as db:
        db.add(EntitlementGrant(person_id=jo, product="pp", kind="comp_tier", value="pro_buyer", starts_at=utcnow(),
                                ends_at=datetime(2027, 10, 1), state="active", source="programme:mentorship"))
        db.commit()
    r = admin_client.post(f"/people/{jo}", data={"email": "jo.bloggs@example.test", "name": "Jo", "phone": "", "notes": ""})
    assert r.status_code == 400 and "Revoke every grant before changing the email" in r.text
    with session() as db:
        assert db.get(Person, jo).person_key == "jo@example.test"
    assert not audit_rows("person.update")
    # other details still save, and a revoked grant no longer stands in the way
    r = admin_client.post(f"/people/{jo}", data={"email": "jo@example.test", "name": "Jo Bloggs", "phone": "", "notes": ""})
    assert r.status_code == 303
    with session() as db:
        grant = db.query(EntitlementGrant).filter(EntitlementGrant.person_id == jo).one()
        grant.state = "revoked"
        db.commit()
    r = admin_client.post(f"/people/{jo}", data={"email": "jo.bloggs@example.test", "name": "Jo Bloggs", "phone": "", "notes": ""})
    assert r.status_code == 303
    with session() as db:
        assert db.get(Person, jo).person_key == "jo.bloggs@example.test"
