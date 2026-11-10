"""Operator API OAuth on Postgres (alembic/versions/0017_operator_oauth.py): a code is redeemed once even when two
sessions race it, a replayed code ends what it issued, refresh tokens rotate and are bound to their client, reuse ends
the connection, and the admin and the client can each revoke it. Only hashes are stored."""
from __future__ import annotations

import base64
import hashlib
import threading
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.auth import current_user, find_user_by_email
from app.db import SessionLocal
from app.main import http_error
from app.models import OperatorOAuthCode, OperatorOAuthGrant, OperatorOAuthToken, utcnow
from app.routers import operator_mcp, operator_oauth
from app.services import operator_oauth_store as store
from tests.conftest import ADMIN_EMAIL

SECRET = "operator-oauth-secret-synthetic-0123456789abcdef"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce() -> tuple[str, str]:
    verifier = store.opaque() + "-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture
def oauth_app(monkeypatch):
    monkeypatch.setattr(config, "GHQ_OPERATOR_OAUTH_SECRET", SECRET)
    monkeypatch.setattr(config, "BASE_URL", "https://hq.example.test")
    app = FastAPI()
    app.include_router(operator_oauth.router)
    app.include_router(operator_mcp.router)
    app.add_exception_handler(StarletteHTTPException, http_error)

    def admin():
        with SessionLocal() as db:
            user = find_user_by_email(db, ADMIN_EMAIL)
            db.expunge(user)
            return user
    app.dependency_overrides[current_user] = admin
    return TestClient(app, follow_redirects=False)


def register(c: TestClient, name: str = "Claude") -> str:
    r = c.post("/oauth/register", json={"redirect_uris": [CALLBACK], "client_name": name})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def consent(c: TestClient, client_id: str, challenge: str) -> str:
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "code_challenge": challenge,
             "code_challenge_method": "S256", "state": "st", "scope": "operator"}
    page = c.get("/oauth/authorize", params=query)
    assert page.status_code == 200, page.text[:300]
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    r = c.post("/oauth/authorize", data={**query, "csrf": csrf, "decision": "allow"})
    assert r.status_code == 303, r.text[:300]
    return parse_qs(urlsplit(r.headers["location"]).query)["code"][0]


def exchange(c: TestClient, client_id: str, code: str, verifier: str):
    return c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                                        "client_id": client_id, "code_verifier": verifier})


def refresh(c: TestClient, client_id: str, token: str):
    return c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id})


def ping(c: TestClient, bearer: str) -> int:
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {bearer}"}).status_code


def test_full_flow_rotation_reuse_and_revocation(oauth_app, db):
    c = oauth_app
    client_id = register(c)
    verifier, challenge = pkce()
    code = consent(c, client_id, challenge)
    stored = db.query(OperatorOAuthCode).one()
    assert stored.code_hash == hashlib.sha256(code.encode()).digest() and code.encode() not in stored.code_hash

    assert exchange(c, client_id, code, pkce()[0]).json()["error"] == "invalid_grant", "wrong verifier"
    first = exchange(c, client_id, code, verifier)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    pair = first.json()
    assert (pair["token_type"], pair["expires_in"], pair["scope"]) == ("Bearer", 3600, "operator")
    assert ping(c, pair["access_token"]) == 200

    rotated = refresh(c, client_id, pair["refresh_token"])
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != pair["refresh_token"], "refresh tokens rotate"
    assert ping(c, second["access_token"]) == 200
    assert refresh(c, client_id, pair["refresh_token"]).json()["error"] == "invalid_grant", "a spent refresh token is refused"
    assert ping(c, second["access_token"]) == 401, "and its reuse ends the whole connection"
    assert refresh(c, client_id, second["refresh_token"]).json()["error"] == "invalid_grant"

    # Refresh tokens are bound to their client.
    other = register(c, "Other")
    v2, ch2 = pkce()
    third = exchange(c, client_id, consent(c, client_id, ch2), v2).json()
    assert refresh(c, other, third["refresh_token"]).json()["error"] == "invalid_grant"
    assert ping(c, third["access_token"]) == 401, "a refresh token from another client ends the connection"

    # The admin revokes from Connected apps; the client revokes through RFC 7009.
    v3, ch3 = pkce()
    fourth = exchange(c, client_id, consent(c, client_id, ch3), v3).json()
    page = c.get("/oauth/connections")
    assert page.status_code == 200 and "Claude" in page.text
    live = db.query(OperatorOAuthGrant).filter(OperatorOAuthGrant.revoked_at.is_(None)).one()
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    assert c.post(f"/oauth/connections/{live.id}/revoke", data={"csrf": "bad"}).status_code == 400
    assert c.post(f"/oauth/connections/{live.id}/revoke", data={"csrf": csrf}).status_code == 303
    assert ping(c, fourth["access_token"]) == 401
    v4, ch4 = pkce()
    fifth = exchange(c, client_id, consent(c, client_id, ch4), v4).json()
    assert c.post("/oauth/revoke", data={"token": fifth["access_token"], "client_id": other}).status_code == 200
    assert ping(c, fifth["access_token"]) == 200, "another client cannot revoke it"
    assert c.post("/oauth/revoke", data={"token": fifth["refresh_token"], "client_id": client_id}).status_code == 200
    assert ping(c, fifth["access_token"]) == 401

    db.expire_all()
    reasons = [g.revoked_reason for g in db.query(OperatorOAuthGrant).order_by(OperatorOAuthGrant.id)]
    assert reasons == ["refresh_reuse", "client_mismatch", "owner", "client"]


def test_a_code_raced_by_two_sessions_is_redeemed_once_and_the_replay_ends_it(oauth_app, db):
    client_id = register(oauth_app)
    verifier, challenge = pkce()
    code = consent(oauth_app, client_id, challenge)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    barrier, results = threading.Barrier(2), []

    def attempt():
        with SessionLocal() as session:
            barrier.wait()
            results.append(store.redeem_code(session, code=code, client_id=client_id, redirect_uri=CALLBACK, verifier_challenge=expected))
    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    outcomes = sorted(outcome for outcome, _ in results)
    assert outcomes == ["issued", "replayed"]
    issued = next(pair for outcome, pair in results if outcome == "issued")
    with SessionLocal() as session:
        assert store.resolve(session, issued.access) is None, "the replay revoked what the winner received"
        assert session.query(OperatorOAuthGrant).one().revoked_reason == "code_replay"


def test_expired_codes_tokens_and_inactive_admins_are_refused(oauth_app, db):
    client_id = register(oauth_app)
    verifier, challenge = pkce()
    code = consent(oauth_app, client_id, challenge)
    row = db.query(OperatorOAuthCode).one()
    row.created_at, row.expires_at = utcnow() - timedelta(minutes=5), utcnow() - timedelta(minutes=4)
    db.commit()
    assert exchange(oauth_app, client_id, code, verifier).json()["error"] == "invalid_grant"

    v2, ch2 = pkce()
    pair = exchange(oauth_app, client_id, consent(oauth_app, client_id, ch2), v2).json()
    db.query(OperatorOAuthToken).filter(OperatorOAuthToken.kind == "access").update(
        {"created_at": utcnow() - timedelta(hours=2), "expires_at": utcnow() - timedelta(seconds=1)})
    db.commit()
    assert ping(oauth_app, pair["access_token"]) == 401, "an expired access token is refused"
    renewed = refresh(oauth_app, client_id, pair["refresh_token"]).json()
    assert ping(oauth_app, renewed["access_token"]) == 200
    admin = find_user_by_email(db, ADMIN_EMAIL)
    admin.is_active = False
    db.commit()
    assert ping(oauth_app, renewed["access_token"]) == 401, "an inactive admin's token stops at once"
    assert refresh(oauth_app, client_id, renewed["refresh_token"]).json()["error"] == "invalid_grant"
