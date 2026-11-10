"""The OAuth 2.1 authorization server in front of /mcp, for claude.ai custom connectors (docs/operator-api.md).

claude.ai authenticates a remote MCP server only through OAuth: it registers itself as a client (RFC 7591), sends
the admin here to sign in and press Allow, and exchanges the code it gets back for tokens with PKCE (S256).
The client id is a signed description of its redirect URIs (app.services.operator_tokens), because registration is
unauthenticated and must not grow a table. Codes, access tokens and refresh tokens are opaque values whose hashes live
in Postgres (app.services.operator_oauth_store): a code is redeemable once across every instance, refresh tokens are
bound to their client and rotated on use, reuse ends the connection, and the admin can revoke any connection at
/oauth/connections.

Only /oauth/authorize needs the admin's session (it is the consent page); the discovery documents, registration
and the token endpoint are public, speak JSON only and never redirect to a URI the client did not register.
acting_user() is the bearer check /mcp uses: the static GHQ_OPERATOR_TOKEN, or a live stored access token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, current_user, find_user_by_email, require_admin
from ..db import get_db
from ..models import User
from ..services import operator_oauth_store as store
from ..services import operator_tokens as tokens
from ..templating import render

router = APIRouter()

SCOPE = "operator"
MAX_BODY = 16 * 1024
MAX_REDIRECT_URIS = 5
MAX_URI_LENGTH = 2000
MAX_CLIENT_NAME = 100
MAX_STATE = 512
CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")          # an S256 challenge is one base64url SHA-256 digest
VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _json(payload, status_code: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={**NO_STORE, **(headers or {})})


def _error(code: str, status_code: int = 400, description: str = "") -> JSONResponse:
    body: dict = {"error": code}
    if description:
        body["error_description"] = description
    return _json(body, status_code)


def resource_url() -> str:
    return config.BASE_URL + "/mcp"


def resource_metadata() -> dict:
    return {"resource": resource_url(), "authorization_servers": [config.BASE_URL],
            "bearer_methods_supported": ["header"], "scopes_supported": [SCOPE]}


def server_metadata() -> dict:
    base = config.BASE_URL
    return {"issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
            "registration_endpoint": base + "/oauth/register", "revocation_endpoint": base + "/oauth/revoke",
            "response_types_supported": ["code"], "revocation_endpoint_auth_methods_supported": ["none"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE]}


def unauthorised() -> JSONResponse:
    """401 with the pointer a client follows to find this authorization server (RFC 9728)."""
    challenge = (f'Bearer realm="Group HQ", '
                 f'resource_metadata="{config.BASE_URL}/.well-known/oauth-protected-resource"')
    return _json({"error": "unauthorised"}, 401, {"WWW-Authenticate": challenge})


# ---- discovery -------------------------------------------------------------------------------------------------
# The path-suffixed spellings are what a client derives from the resource path (/mcp) under RFC 8414 and 9728.
@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
def protected_resource():
    return JSONResponse(resource_metadata())


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get("/.well-known/oauth-authorization-server/mcp", include_in_schema=False)
def authorization_server():
    return JSONResponse(server_metadata())


# ---- dynamic client registration ---------------------------------------------------------------------------------
def valid_redirect_uri(uri: object) -> bool:
    """https anywhere, or plain http on the loopback for a local MCP client; never a fragment."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_URI_LENGTH or not uri.isascii() or "#" in uri:
        return False
    parts = urlsplit(uri)
    if parts.fragment or not parts.netloc or parts.username or parts.password:
        return False
    try:
        host = (parts.hostname or "").lower()
        _ = parts.port                   # raises ValueError on a port that is not a number
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    if parts.scheme == "http":
        return host in ("127.0.0.1", "localhost")
    return False


async def _read_body(request: Request, limit: int = MAX_BODY) -> bytes | None:
    body = await request.body()
    return None if len(body) > limit else body


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request):
    body = await _read_body(request)
    if body is None:
        return _error("invalid_client_metadata", 413, "request too large")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return _error("invalid_client_metadata", 400, "the body is not JSON")
    if not isinstance(data, dict):
        return _error("invalid_client_metadata")
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS or not all(valid_redirect_uri(u) for u in uris) \
            or len(set(uris)) != len(uris):
        return _error("invalid_client_metadata", 400, "redirect_uris must be 1 to 5 https (or loopback http) URIs")
    name = data.get("client_name", "")
    if name is None:
        name = ""
    if not isinstance(name, str) or len(name) > MAX_CLIENT_NAME:
        return _error("invalid_client_metadata", 400, "client_name is a string of up to 100 characters")
    name = " ".join(name.split())
    if not tokens.configured():
        return _error("invalid_client_metadata", 503, "the authorization server is not configured")
    issued = int(time.time())
    client_id = tokens.sign(tokens.CLIENT, {"uris": list(uris), "name": name, "iat": issued})
    return _json({"client_id": client_id, "client_id_issued_at": issued, "client_name": name, "redirect_uris": list(uris),
                  "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                  "response_types": ["code"], "scope": SCOPE}, 201)


def _client(client_id: str) -> dict | None:
    try:
        claims = tokens.verify(tokens.CLIENT, client_id)
    except (tokens.TokenError, ValueError):
        return None
    uris = claims.get("uris")
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        return None
    return {"id": client_id, "uris": uris, "name": str(claims.get("name") or "")}


# ---- consent ---------------------------------------------------------------------------------------------------
def consent_user(request: Request, user: User | None = Depends(current_user)) -> User:
    """require_admin's rule, with the whole authorize URL (query included) as the login's next, so the admin lands
    back on the consent page with the client's parameters intact. app.main turns the 303 into a redirect."""
    if user is None:
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        raise HTTPException(status_code=303, headers={"Location": "/login?" + urlencode({"next": target})})
    if user.must_change_password:
        raise HTTPException(status_code=303, headers={"Location": "/password?first=1"})
    return user


def csrf_token(request: Request) -> str:
    """A form token bound to the session cookie that opened the page, under the app's own secret."""
    cookie = request.cookies.get(config.SESSION_COOKIE, "")
    digest = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
    return hmac.new(config.SECRET_KEY.encode("utf-8"), b"ghq-operator-consent\x00" + digest.encode("ascii"),
                    hashlib.sha256).hexdigest()


def _authorize_params(params) -> tuple[dict | None, str]:
    """The validated authorize parameters, or (None, why). Nothing here may lead to a redirect: an unregistered
    URI or a bad client gets the error page, never a bounce."""
    def one(name: str, limit: int = 4096) -> str | None:
        values = params.getlist(name) if hasattr(params, "getlist") else [params.get(name)]
        values = [v for v in values if v is not None]
        if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > limit:
            return None
        return values[0]

    client_id = one("client_id")
    client = _client(client_id) if client_id else None
    if client is None:
        return None, "The client is not registered with Group HQ."
    redirect_uri = one("redirect_uri", MAX_URI_LENGTH)
    if not redirect_uri or redirect_uri not in client["uris"]:
        return None, "The redirect address is not one this client registered."
    if one("response_type", 20) != "code":
        return None, "Only the authorization code flow is supported."
    challenge = one("code_challenge", 128)
    if not challenge or not CHALLENGE.fullmatch(challenge) or one("code_challenge_method", 10) != "S256":
        return None, "A PKCE S256 code challenge is required."
    state = one("state", MAX_STATE) if "state" in params else ""
    if state is None:
        return None, "The state is longer than 512 characters."
    scope = one("scope", 200) or ""
    if scope.strip() not in ("", SCOPE):
        return None, "The only scope Group HQ offers is 'operator'."
    resource = one("resource", MAX_URI_LENGTH) or ""
    if resource and resource.rstrip("/") != resource_url():
        return None, "The resource is not this Group HQ."
    return {"client": client, "client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": challenge,
            "state": state, "scope": SCOPE}, ""


def _bad_request(request: Request, detail: str):
    return render(request, "error.html", {"status": 400, "detail": detail}, 400)


def _consent_page(request: Request, user: User, params: dict, status_code: int = 200):
    host = urlsplit(params["redirect_uri"]).netloc
    response = render(request, "operator_consent.html", {
        "user": user, "section": "", "client_name": params["client"]["name"] or "An MCP client", "redirect_host": host,
        "fields": {"client_id": params["client_id"], "redirect_uri": params["redirect_uri"], "response_type": "code",
                   "code_challenge": params["code_challenge"], "code_challenge_method": "S256", "state": params["state"],
                   "scope": params["scope"]},
        "csrf": csrf_token(request), "writes_enabled": config.operator_writes_enabled(),
        "messaging_enabled": config.operator_messaging_enabled(),
    }, status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/oauth/authorize", include_in_schema=False)
def authorize(request: Request, user: User = Depends(consent_user)):
    if not tokens.configured():
        return _bad_request(request, "The operator API's OAuth server is not configured on this deployment.")
    params, problem = _authorize_params(request.query_params)
    if params is None:
        return _bad_request(request, problem)
    return _consent_page(request, user, params)


@router.post("/oauth/authorize", include_in_schema=False, dependencies=[Depends(check_origin)])
async def decide(request: Request, user: User = Depends(consent_user), db: Session = Depends(get_db)):
    if not tokens.configured():
        return _bad_request(request, "The operator API's OAuth server is not configured on this deployment.")
    body = await _read_body(request)
    if body is None:
        return _bad_request(request, "The form was too large.")
    form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    params, problem = _authorize_params(form)
    if params is None:
        return _bad_request(request, problem)
    if not hmac.compare_digest(form.get("csrf", ""), csrf_token(request)):
        return _bad_request(request, "The consent form has expired. Open the connector's link again.")
    decision = form.get("decision", "")
    separator = "&" if "?" in params["redirect_uri"] else "?"
    if decision != "allow":
        query = {"error": "access_denied"}
        if params["state"]:
            query["state"] = params["state"]
        return _redirect(params["redirect_uri"] + separator + urlencode(query))
    code = store.issue_code(db, user=user, client_id=params["client_id"], client_name=params["client"]["name"] or "MCP client",
                            redirect_uri=params["redirect_uri"], code_challenge=params["code_challenge"])
    if code is None:
        return _bad_request(request, "This admin account is not active, so nothing was approved.")
    query = {"code": code}
    if params["state"]:
        query["state"] = params["state"]
    return _redirect(params["redirect_uri"] + separator + urlencode(query))


def _redirect(location: str) -> RedirectResponse:
    response = RedirectResponse(location, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ---- token endpoint -------------------------------------------------------------------------------------------
def _challenge_of(verifier: str) -> str:
    return tokens._b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _issued(pair: store.Tokens) -> JSONResponse:
    return _json({"access_token": pair.access, "token_type": "Bearer", "expires_in": int(store.ACCESS_LIFETIME.total_seconds()),
                  "refresh_token": pair.refresh, "scope": SCOPE})


async def _form(request: Request) -> dict[str, str] | JSONResponse:
    body = await _read_body(request)
    if body is None:
        return _error("invalid_request", 413, "request too large")
    form: dict[str, str] = {}
    for key, value in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True):
        if key in form:
            return _error("invalid_request", 400, f"{key} was sent more than once")
        form[key] = value
    return form


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request, db: Session = Depends(get_db)):
    if not tokens.configured():
        return _error("invalid_request", 503, "the authorization server is not configured")
    form = await _form(request)
    if isinstance(form, JSONResponse):
        return form
    grant = form.get("grant_type", "")
    if grant == "authorization_code":
        return _exchange_code(db, form)
    if grant == "refresh_token":
        return _refresh(db, form)
    return _error("unsupported_grant_type")


def _exchange_code(db: Session, form: dict[str, str]) -> JSONResponse:
    code, redirect_uri, client_id, verifier = (form.get(k, "") for k in ("code", "redirect_uri", "client_id", "code_verifier"))
    if not code or not redirect_uri or not client_id or not verifier:
        return _error("invalid_request", 400, "code, redirect_uri, client_id and code_verifier are required")
    if not OPAQUE.fullmatch(code) or not VERIFIER.fullmatch(verifier):
        return _error("invalid_grant")
    if _client(client_id) is None:
        return _error("invalid_client", 401)
    outcome, pair = store.redeem_code(db, code=code, client_id=client_id, redirect_uri=redirect_uri, verifier_challenge=_challenge_of(verifier))
    if outcome != "issued" or pair is None:
        return _error("invalid_grant")
    return _issued(pair)


def _refresh(db: Session, form: dict[str, str]) -> JSONResponse:
    presented, client_id = form.get("refresh_token", ""), form.get("client_id", "")
    if not presented or not client_id:
        return _error("invalid_request", 400, "refresh_token and client_id are required")
    if not OPAQUE.fullmatch(presented):
        return _error("invalid_grant")
    if _client(client_id) is None:
        return _error("invalid_client", 401)
    outcome, pair = store.refresh(db, refresh_token=presented, client_id=client_id)
    if outcome != "issued" or pair is None:
        return _error("invalid_grant")
    return _issued(pair)


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request, db: Session = Depends(get_db)):
    """RFC 7009: always 200 for a well-formed request; a matching token of this client ends its connection."""
    if not tokens.configured():
        return _error("invalid_request", 503, "the authorization server is not configured")
    form = await _form(request)
    if isinstance(form, JSONResponse):
        return form
    client_id = form.get("client_id", "")
    if _client(client_id) is None:
        return _error("invalid_client", 401)
    presented = form.get("token", "")
    if OPAQUE.fullmatch(presented):
        store.revoke_token(db, token=presented, client_id=client_id)
    return _json({})


# ---- the admin's connected apps ------------------------------------------------------------------------------
REVOKED = {"owner": "revoked by you", "client": "disconnected by the app", "code_replay": "stopped: a sign-in code was reused",
           "refresh_reuse": "stopped: an old renewal token was reused", "client_mismatch": "stopped: presented by a different app"}


@router.get("/oauth/connections", include_in_schema=False)
def connections(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    response = render(request, "operator_connections.html", {
        "user": user, "section": "", "grants": store.list_grants(db, user), "reasons": REVOKED,
        "csrf": csrf_token(request), "configured": tokens.configured()})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/oauth/connections/{grant_id}/revoke", include_in_schema=False, dependencies=[Depends(check_origin)])
async def revoke_connection(grant_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    body = await _read_body(request)
    form = dict(parse_qsl((body or b"").decode("utf-8", "replace"), keep_blank_values=True))
    if not hmac.compare_digest(form.get("csrf", ""), csrf_token(request)):
        return _bad_request(request, "The form has expired. Reload Connected apps and try again.")
    revoked = store.revoke_grant(db, user, grant_id)
    return _redirect("/oauth/connections?" + urlencode({"ok": "Revoked. That app must be connected again." if revoked else "That connection was already ended."}))


# ---- the bearer check /mcp uses ------------------------------------------------------------------------------
def acting_user(db: Session, authorization: str | None) -> User | None:
    """The admin a bearer acts as: GHQ_OPERATOR_USER_EMAIL for the static token, the subject of a live stored access
    token otherwise; in both cases an existing, active admin. None when the bearer is not acceptable."""
    header = (authorization or "").strip()
    if not header[:7].lower() == "bearer ":
        return None
    presented = header[7:].strip()
    if not presented or len(presented) > tokens.MAX_TOKEN_LENGTH or not presented.isascii():
        return None
    if config.operator_static_token_configured() and hmac.compare_digest(presented.encode(), config.GHQ_OPERATOR_TOKEN.encode()):
        user = find_user_by_email(db, config.GHQ_OPERATOR_USER_EMAIL)
        return user if user is not None and user.is_active else None
    if tokens.configured() and OPAQUE.fullmatch(presented):
        return store.resolve(db, presented)
    return None


def method_not_allowed() -> Response:
    return _json({"error": "method_not_allowed", "detail": "POST JSON-RPC to /mcp"}, 405, {"Allow": "POST"})
