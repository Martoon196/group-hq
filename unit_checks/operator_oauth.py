"""The OAuth server for claude.ai connectors, without a database: discovery, registration, consent, PKCE exchange, refresh."""
import base64
import hashlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("SECRET_KEY", "unit-test-only")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.auth import current_user  # noqa: E402
from app.main import http_error  # noqa: E402
from app.routers import operator_oauth as oauth  # noqa: E402
from app.services import operator_tokens as tokens  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

SECRET = "operator-oauth-secret-synthetic-0123456789abcdef"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
FOUNDER = SimpleNamespace(id=1, email="Founder@Example.test", is_active=True, must_change_password=False)


def make_app(user=FOUNDER):
    app = FastAPI()
    app.include_router(oauth.router)
    app.add_exception_handler(StarletteHTTPException, http_error)
    if user is not None:
        app.dependency_overrides[current_user] = lambda: user
    return app


class OperatorOAuthTests(unittest.TestCase):
    def setUp(self):
        tokens.reset_consumed_for_tests()
        self.patches = [patch.object(config, "GHQ_OPERATOR_OAUTH_SECRET", SECRET), patch.object(config, "BASE_URL", "https://hq.example.test")]
        for p in self.patches:
            p.start()
        self.client = TestClient(make_app(), follow_redirects=False)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def register(self, uris=(CALLBACK,), name="Claude"):
        return self.client.post("/oauth/register", json={"redirect_uris": list(uris), "client_name": name})

    def authorize_query(self, registered_client, **overrides):
        query = {"response_type": "code", "client_id": registered_client, "redirect_uri": CALLBACK, "code_challenge": CHALLENGE,
                 "code_challenge_method": "S256", "state": "st-1", "scope": "operator"}
        query.update(overrides)
        return query

    def consent(self, client_id, decision="allow", **overrides):
        page = self.client.get("/oauth/authorize", params=self.authorize_query(client_id, **overrides))
        self.assertEqual(page.status_code, 200, page.text[:200])
        csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
        form = {**self.authorize_query(client_id, **overrides), "csrf": csrf, "decision": decision}
        return self.client.post("/oauth/authorize", data=form)

    def test_well_known_documents(self):
        resource = self.client.get("/.well-known/oauth-protected-resource").json()
        self.assertEqual(resource, {"resource": "https://hq.example.test/mcp", "authorization_servers": ["https://hq.example.test"],
                                    "bearer_methods_supported": ["header"], "scopes_supported": ["operator"]})
        server = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(server["issuer"], "https://hq.example.test")
        self.assertEqual(server["authorization_endpoint"], "https://hq.example.test/oauth/authorize")
        self.assertEqual(server["token_endpoint"], "https://hq.example.test/oauth/token")
        self.assertEqual(server["registration_endpoint"], "https://hq.example.test/oauth/register")
        self.assertEqual(server["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(server["grant_types_supported"], ["authorization_code", "refresh_token"])
        self.assertEqual(server["token_endpoint_auth_methods_supported"], ["none"])
        self.assertEqual(self.client.get("/.well-known/oauth-authorization-server/mcp").json(), server)

    def test_registration_accepts_https_and_loopback_only(self):
        response = self.register()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["redirect_uris"], [CALLBACK])
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertEqual(tokens.verify(tokens.CLIENT, body["client_id"], secret=SECRET)["name"], "Claude")
        ok = self.register(uris=("http://127.0.0.1:6274/oauth/callback", "http://localhost/cb"))
        self.assertEqual(ok.status_code, 201)
        for uris in ((), ("http://evil.example/cb",), ("https://claude.ai/cb#frag",), ("ftp://x/",), [CALLBACK] * 6, ("javascript:alert(1)",)):
            bad = self.register(uris=uris)
            self.assertEqual(bad.status_code, 400, uris)
            self.assertEqual(bad.json()["error"], "invalid_client_metadata")
        self.assertEqual(self.register(name="n" * 101).status_code, 400)
        self.assertEqual(self.client.post("/oauth/register", content=b"{not json").status_code, 400)

    def test_consent_then_pkce_exchange_then_refresh(self):
        client_id = self.register().json()["client_id"]
        page = self.client.get("/oauth/authorize", params=self.authorize_query(client_id))
        self.assertEqual(page.status_code, 200)
        self.assertIn("Claude", page.text)
        self.assertIn("claude.ai", page.text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        redirect = self.consent(client_id)
        self.assertEqual(redirect.status_code, 303)
        parts = urlsplit(redirect.headers["location"])
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "claude.ai", "/api/mcp/auth_callback"))
        query = parse_qs(parts.query)
        self.assertEqual(query["state"], ["st-1"])
        code = query["code"][0]
        claims = tokens.verify(tokens.CODE, code, secret=SECRET)
        self.assertEqual((claims["sub"], claims["cid"], claims["uri"], claims["cc"]), ("founder@example.test", client_id, CALLBACK, CHALLENGE))
        self.assertLessEqual(claims["exp"] - claims["iat"], 120)

        wrong = self.client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                                                      "client_id": client_id, "code_verifier": "w" * 43})
        self.assertEqual((wrong.status_code, wrong.json()["error"]), (400, "invalid_grant"))
        wrong_uri = self.client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK + "x",
                                                          "client_id": client_id, "code_verifier": VERIFIER})
        self.assertEqual((wrong_uri.status_code, wrong_uri.json()["error"]), (400, "invalid_grant"))
        good = self.client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                                                     "client_id": client_id, "code_verifier": VERIFIER})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.headers["cache-control"], "no-store")
        grant = good.json()
        self.assertEqual((grant["token_type"], grant["expires_in"], grant["scope"]), ("Bearer", 3600, "operator"))
        access = tokens.verify(tokens.ACCESS, grant["access_token"], secret=SECRET)
        self.assertEqual(access["sub"], "founder@example.test")
        self.assertEqual(tokens.verify(tokens.REFRESH, grant["refresh_token"], secret=SECRET)["sub"], "founder@example.test")
        again = self.client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
                                                      "client_id": client_id, "code_verifier": VERIFIER})
        self.assertEqual((again.status_code, again.json()["error"]), (400, "invalid_grant"))

        refreshed = self.client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": grant["refresh_token"],
                                                          "client_id": client_id})
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["refresh_token"], grant["refresh_token"])
        self.assertEqual(tokens.verify(tokens.ACCESS, refreshed.json()["access_token"], secret=SECRET)["sub"], "founder@example.test")
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, grant["refresh_token"], secret=SECRET)
        self.assertEqual(self.client.post("/oauth/token", data={"grant_type": "password"}).json()["error"], "unsupported_grant_type")
        self.assertEqual(self.client.post("/oauth/token", data={"grant_type": "refresh_token"}).json()["error"], "invalid_request")

    def test_deny_and_bad_parameters(self):
        client_id = self.register().json()["client_id"]
        denied = self.consent(client_id, decision="deny")
        self.assertEqual(denied.status_code, 303)
        self.assertEqual(parse_qs(urlsplit(denied.headers["location"]).query), {"error": ["access_denied"], "state": ["st-1"]})
        for overrides in ({"redirect_uri": "https://evil.example/cb"}, {"code_challenge_method": "plain"}, {"code_challenge": "short"},
                          {"client_id": "forged"}, {"response_type": "token"}, {"scope": "admin"}, {"state": "s" * 513}):
            response = self.client.get("/oauth/authorize", params=self.authorize_query(client_id, **overrides))
            self.assertEqual(response.status_code, 400, overrides)
            self.assertNotIn("location", response.headers)
        page = self.client.get("/oauth/authorize", params=self.authorize_query(client_id))
        csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
        forged = self.client.post("/oauth/authorize", data={**self.authorize_query(client_id), "csrf": csrf + "x", "decision": "allow"})
        self.assertEqual(forged.status_code, 400)

    def test_signed_out_admin_is_sent_to_login_with_the_whole_url_as_next(self):
        client_id = self.register().json()["client_id"]
        anonymous = TestClient(make_app(user=None), follow_redirects=False)
        with patch("app.routers.operator_oauth.current_user", return_value=None):
            response = anonymous.get("/oauth/authorize", params=self.authorize_query(client_id), headers={"cookie": ""})
        self.assertEqual(response.status_code, 303)
        location = response.headers["location"]
        self.assertTrue(location.startswith("/login?next=%2Foauth%2Fauthorize%3F"), location)
        self.assertIn("code_challenge", parse_qs(urlsplit(location).query)["next"][0])

    def test_bearer_check_accepts_static_token_and_access_token_only_for_existing_admins(self):
        class Db:
            def __init__(self, user):
                self.user = user

            def query(self, model):
                return self

            def filter(self, *a):
                return self

            def first(self):
                return self.user

        with patch.object(config, "GHQ_OPERATOR_TOKEN", "static-operator-token-synthetic-0123456789"), \
                patch.object(config, "GHQ_OPERATOR_USER_EMAIL", "founder@example.test"):
            self.assertIs(oauth.acting_user(Db(FOUNDER), "Bearer static-operator-token-synthetic-0123456789"), FOUNDER)
            self.assertIsNone(oauth.acting_user(Db(FOUNDER), "Bearer static-operator-token-synthetic-012345678X"))
            self.assertIsNone(oauth.acting_user(Db(None), "Bearer static-operator-token-synthetic-0123456789"))
            self.assertIsNone(oauth.acting_user(Db(FOUNDER), "Basic abc"))
            self.assertIsNone(oauth.acting_user(Db(FOUNDER), None))
        access = tokens.sign(tokens.ACCESS, {"sub": "founder@example.test", "scope": "operator", "iat": 1, "exp": 10**10, "jti": "j"}, secret=SECRET)
        self.assertIs(oauth.acting_user(Db(FOUNDER), "Bearer " + access), FOUNDER)
        inactive = SimpleNamespace(**{**vars(FOUNDER), "is_active": False})
        self.assertIsNone(oauth.acting_user(Db(inactive), "Bearer " + access))
        self.assertIsNone(oauth.acting_user(Db(FOUNDER), "Bearer " + access[:-3] + "abc"))


if __name__ == "__main__":
    unittest.main()
