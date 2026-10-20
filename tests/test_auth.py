"""Login needs a password and an authenticator code; wrong codes fail and cannot be replayed; enrolment
stores the secret only after a valid code; throttles; the CSRF guard; sessions die with the password
hash; a generated password must be changed; /healthz."""
from __future__ import annotations

import re

import pyotp
from fastapi.testclient import TestClient

from app import auth, config
from app.main import app
from app.models import AuditLog, User
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, TEST_TOTP_SECRET, forget_last_code, login, session, totp_now


def _admin(db) -> User:
    user = auth.find_user_by_email(db, ADMIN_EMAIL)
    assert user is not None
    return user


def _wrong_code(secret: str = TEST_TOTP_SECRET) -> str:
    """A six-digit code that is not the current one (nor, in all likelihood, a neighbour)."""
    return str((int(totp_now(secret)) + 500_000) % 1_000_000).zfill(6)


# ---- health ------------------------------------------------------------------------
def test_healthz_reports_the_database(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "db": True}


# ---- the two steps ----------------------------------------------------------------------
def test_dashboard_needs_a_session(client):
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/"
    assert client.get("/login").status_code == 200


def test_login_needs_password_then_code(client):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"email": ADMIN_EMAIL, "password": "wrong"})
    assert r.status_code == 401
    assert config.SESSION_COOKIE not in c.cookies and config.PENDING_COOKIE not in c.cookies

    # the right password (however the email is spelt) only gets you to the code step
    r = c.post("/login", data={"email": "  " + ADMIN_EMAIL.upper() + " ", "password": ADMIN_PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/login/code"
    assert config.PENDING_COOKIE in c.cookies
    assert config.SESSION_COOKIE not in c.cookies
    assert c.get("/").status_code == 303, "a password alone is not a login"

    page = c.get("/login/code")
    assert page.status_code == 200
    assert "Authenticator code" in page.text
    assert "totp-secret" not in page.text, "an enrolled user is never shown a secret"

    r = c.post("/login/code", data={"code": totp_now()})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert config.SESSION_COOKIE in c.cookies
    assert not c.cookies.get(config.PENDING_COOKIE)

    home = c.get("/")
    assert home.status_code == 200
    assert "Group" in home.text and "HQ" in home.text and ADMIN_EMAIL in home.text
    with session() as db:
        user = _admin(db)
        assert user.last_login is not None
        assert user.totp_last_used is not None
        actions = [row.action for row in db.query(AuditLog).all()]
        assert "login" in actions


def test_wrong_code_fails_then_the_right_one_works(client):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    r = c.post("/login/code", data={"code": _wrong_code()})
    assert r.status_code == 401
    assert "not accepted" in r.text
    assert config.SESSION_COOKIE not in c.cookies
    for bad in ("", "12345", "abcdef", "1234567"):
        assert c.post("/login/code", data={"code": bad}).status_code == 401, bad
    r = c.post("/login/code", data={"code": totp_now()})
    assert r.status_code == 303 and config.SESSION_COOKIE in c.cookies


def test_a_code_cannot_be_replayed(client):
    code = totp_now()
    first = TestClient(app, follow_redirects=False)
    first.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert first.post("/login/code", data={"code": code}).status_code == 303
    second = TestClient(app, follow_redirects=False)
    second.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    r = second.post("/login/code", data={"code": code})
    assert r.status_code == 401
    assert config.SESSION_COOKIE not in second.cookies


def test_code_step_without_the_password_step_is_refused(client):
    c = TestClient(app, follow_redirects=False)
    r = c.get("/login/code")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?err=")
    r = c.post("/login/code", data={"code": totp_now()})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert config.SESSION_COOKIE not in c.cookies


def test_pending_step_dies_with_the_password_hash(client):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    with session() as db:
        user = _admin(db)
        user.password_hash = auth.hash_password("rotated-by-someone-else-1")
        db.commit()
    r = c.post("/login/code", data={"code": totp_now()})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert config.SESSION_COOKIE not in c.cookies


# ---- enrolment ----------------------------------------------------------------------------
def test_first_login_enrols_the_authenticator(client):
    with session() as db:
        user = _admin(db)
        user.totp_secret = None
        user.totp_last_used = None
        db.commit()
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/login/code"

    page = c.get("/login/code")
    assert page.status_code == 200
    assert "Set up your authenticator" in page.text
    assert "<svg" in page.text, "the QR code is rendered inline"
    assert "otpauth://totp/" in page.text
    assert page.headers["cache-control"] == "no-store", "the enrolment page must never be cached"
    secret = re.search(r'id="totp-secret"[^>]*>([A-Z2-7]+)<', page.text).group(1)
    assert len(secret) >= 32
    assert secret not in c.cookies.get(config.PENDING_COOKIE, ""), "the secret stays server side"
    with session() as db:
        assert _admin(db).totp_pending_secret == secret and _admin(db).totp_secret is None

    # a wrong code stores nothing
    r = c.post("/login/code", data={"code": _wrong_code(secret)})
    assert r.status_code == 401
    with session() as db:
        assert _admin(db).totp_secret is None
    # the same secret is offered again until a code is accepted
    again = c.get("/login/code")
    assert re.search(r'id="totp-secret"[^>]*>([A-Z2-7]+)<', again.text).group(1) == secret

    r = c.post("/login/code", data={"code": pyotp.TOTP(secret).now()})
    assert r.status_code == 303 and config.SESSION_COOKIE in c.cookies
    with session() as db:
        user = _admin(db)
        assert user.totp_secret == secret
        assert user.totp_pending_secret is None and user.totp_pending_at is None
        assert "totp.enrol" in [row.action for row in db.query(AuditLog).all()]

    # from now on every login needs a code from that secret, and the old test secret is useless
    other = TestClient(app, follow_redirects=False)
    other.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert other.post("/login/code", data={"code": totp_now(TEST_TOTP_SECRET)}).status_code == 401
    login(other, secret=secret)


def test_verify_totp_window_and_replay():
    secret = pyotp.random_base32()
    now = 1_900_000_000
    code = pyotp.TOTP(secret).at(now)
    counter = now // 30
    assert auth.verify_totp(secret, code, now=now) == counter
    assert auth.verify_totp(secret, "  " + code[:3] + " " + code[3:] + " ", now=now) == counter   # spaces are fine
    assert auth.verify_totp(secret, code, now=now + 30) == counter        # one step of drift either way
    assert auth.verify_totp(secret, code, now=now - 30) == counter
    assert auth.verify_totp(secret, code, now=now + 60) is None
    assert auth.verify_totp(secret, code, now=now, last_used=counter) is None        # replay
    assert auth.verify_totp(secret, code, now=now, last_used=counter - 1) == counter
    assert auth.verify_totp(secret, code[:5], now=now) is None
    assert auth.verify_totp("", code, now=now) is None


# ---- throttles ---------------------------------------------------------------------------
def test_password_throttle_shares_one_budget_across_spellings(client):
    c = TestClient(app, follow_redirects=False)
    for i in range(auth.MAX_ATTEMPTS):
        r = c.post("/login", data={"email": ADMIN_EMAIL + " " * i, "password": "wrong"})
        assert r.status_code == 401, i
    for padded in (ADMIN_EMAIL, " " + ADMIN_EMAIL, ADMIN_EMAIL.upper()):
        assert c.post("/login", data={"email": padded, "password": "wrong"}).status_code == 429, padded
    # even the right password waits out the window
    assert c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 429
    assert config.PENDING_COOKIE not in c.cookies


def test_code_attempts_share_the_same_budget(client):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    for i in range(auth.MAX_ATTEMPTS):
        assert c.post("/login/code", data={"code": _wrong_code()}).status_code == 401, i
    assert c.post("/login/code", data={"code": totp_now()}).status_code == 429
    assert config.SESSION_COOKIE not in c.cookies
    # and the password step is closed for that address and email too
    assert c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 429


def test_throttle_caps_the_address_across_emails(client):
    c = TestClient(app, follow_redirects=False)
    for i in range(auth.MAX_IP_ATTEMPTS):
        assert c.post("/login", data={"email": f"guess{i}@example.test", "password": "wrong"}).status_code == 401, i
    assert c.post("/login", data={"email": "another@example.test", "password": "wrong"}).status_code == 429
    assert c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 429


def test_throttle_caps_the_account_whatever_address_is_claimed(client):
    """A forged X-Forwarded-For buys nothing: the address is the right-most entry (the one the proxy appends)
    and the account has a budget of its own."""
    c = TestClient(app, follow_redirects=False)
    for i in range(auth.MAX_ATTEMPTS):
        r = c.post("/login", data={"email": ADMIN_EMAIL, "password": "wrong"}, headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"})
        assert r.status_code == 401, i
    r = c.post("/login", data={"email": ADMIN_EMAIL, "password": "wrong"}, headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.9"})
    assert r.status_code == 429, "the left-hand entries are the caller's own and change nothing"
    # a different real address gets its own per-address budget but shares the account budget
    for i in range(auth.MAX_ACCOUNT_ATTEMPTS - auth.MAX_ATTEMPTS):
        r = c.post("/login", data={"email": ADMIN_EMAIL, "password": "wrong"}, headers={"X-Forwarded-For": f"198.51.100.{i % 200}"})
        assert r.status_code == 401, i
    r = c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers={"X-Forwarded-For": "192.0.2.77"})
    assert r.status_code == 429, "twenty failures against the account close it from every address"
    with session() as db:
        from app.models import LoginAttempt
        assert db.query(LoginAttempt).filter(LoginAttempt.key == f"acct:{ADMIN_EMAIL}").count() == auth.MAX_ACCOUNT_ATTEMPTS
    # another account is untouched
    assert c.post("/login", data={"email": "other@example.test", "password": "wrong"}, headers={"X-Forwarded-For": "192.0.2.78"}).status_code == 401


def test_security_headers_on_every_page(client, admin_client):
    for c, path in ((client, "/login"), (admin_client, "/password"), (admin_client, "/")):
        r = c.get(path)
        assert r.status_code == 200, path
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert "strict-transport-security" not in r.headers, "HSTS only with Secure cookies"
    assert client.get("/login").headers["cache-control"] == "no-store"
    assert admin_client.get("/password").headers["cache-control"] == "no-store"
    assert "cache-control" not in admin_client.get("/").headers or admin_client.get("/").headers["cache-control"] != "no-store"


# ---- CSRF ------------------------------------------------------------------------------
def test_csrf_guard_blocks_cross_site_posts(admin_client):
    assert admin_client.get("/").status_code == 200
    for headers in ({"Origin": "https://evil.example"}, {"Referer": "https://evil.example/page"}):
        r = admin_client.post("/logout", headers=headers)
        assert r.status_code == 403, headers
        assert "Cross-site" in r.text
    assert admin_client.get("/").status_code == 200, "a blocked request changes nothing"
    r = admin_client.post("/logout", headers={"Origin": "http://testserver"})
    assert r.status_code == 303
    assert admin_client.get("/").status_code == 303, "logged out"


def test_cross_site_login_is_blocked_too(client):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert config.PENDING_COOKIE not in c.cookies


# ---- sessions --------------------------------------------------------------------------
def test_session_dies_with_the_password_hash(admin_client):
    assert admin_client.get("/").status_code == 200
    with session() as db:
        user = _admin(db)
        user.password_hash = auth.hash_password("another-password-12345")
        db.commit()
    assert admin_client.get("/").status_code == 303


def test_deactivated_user_loses_the_session(admin_client):
    with session() as db:
        user = _admin(db)
        user.is_active = False
        db.commit()
    assert admin_client.get("/").status_code == 303
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 401


def test_next_must_be_a_local_path(client):
    for target in ("https://evil.example", "//evil.example", "/\\evil.example"):
        page = client.get("/login", params={"next": target})
        assert page.status_code == 200
        assert 'name="next" value="/"' in page.text, target
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "/mentees"})
    r = c.post("/login/code", data={"code": totp_now()})
    assert r.status_code == 303 and r.headers["location"] == "/mentees"
    forget_last_code()
    c2 = TestClient(app, follow_redirects=False)
    c2.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "https://evil.example"})
    r = c2.post("/login/code", data={"code": totp_now()})
    assert r.status_code == 303 and r.headers["location"] == "/"


# ---- password change -----------------------------------------------------------------------
def test_generated_password_must_be_changed_before_anything_else(admin_client):
    with session() as db:
        user = _admin(db)
        user.must_change_password = True
        db.commit()
    r = admin_client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/password?first=1"
    page = admin_client.get("/password", params={"first": "1"})
    assert page.status_code == 200 and "Change password" in page.text

    new = "a-much-longer-passphrase-1"
    r = admin_client.post("/password", data={"current_password": ADMIN_PASSWORD, "new_password": "short", "confirm_password": "short"})
    assert r.status_code == 400
    r = admin_client.post("/password", data={"current_password": ADMIN_PASSWORD, "new_password": new, "confirm_password": new + "x"})
    assert r.status_code == 400
    r = admin_client.post("/password", data={"current_password": "wrong", "new_password": new, "confirm_password": new})
    assert r.status_code == 401
    r = admin_client.post("/password", data={"current_password": ADMIN_PASSWORD, "new_password": new, "confirm_password": new})
    assert r.status_code == 303 and r.headers["location"].startswith("/?ok=")
    assert admin_client.get("/").status_code == 200, "this browser keeps its session on a fresh cookie"
    with session() as db:
        user = _admin(db)
        assert user.must_change_password is False
        assert auth.verify_password(new, user.password_hash)
        assert "password.change" in [row.action for row in db.query(AuditLog).all()]

    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 401
    login(c, password=new)
