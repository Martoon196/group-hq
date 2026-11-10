"""Group HQ -> Relaunch72 sign-in handoff tokens.

The founder is already signed in here (password + authenticator). To open a Relaunch72 portal page
without a second login, Group HQ signs a two-minute, single-use token bound to one brand host and one
portal path with a secret only the two web services hold. Relaunch72 verifies it at
/portal/auth/group-hq, maps the login email to a portal user from its own server-side allowlist and
mints its ordinary session; a token is never authority on its own and never reaches a browser as
anything but a redirect target. The byte format mirrors orchestrator/src/portal/group-hq-sso.ts.
"""
from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256

from .. import config

VERSION = "ghq-handoff-1"
ROUTE = "/portal/auth/group-hq"
LIFETIME_SECONDS = 90          # Relaunch72 accepts at most 120
BRANDS = {"pp": "Property Predator", "mhp": "Martin Howard Property"}
EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
# Only portal pages on the brand host: never a scheme, another host or a protocol-relative path.
PATH = re.compile(r"^/portal(?:/[A-Za-z0-9._~!$&'()*+,;=:@%-]+)*/?(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$")


class HandoffUnavailable(Exception):
    """The handoff is not configured, or the inputs could not be signed. Nothing was sent anywhere."""


@dataclass(frozen=True)
class Handoff:
    brand: str
    host: str
    path: str
    url: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def host_for(brand: str) -> str:
    if brand == "pp":
        return config.R72_PP_HOST
    if brand == "mhp":
        return config.R72_MHP_HOST
    raise HandoffUnavailable()


def valid_path(path: str) -> bool:
    return bool(path) and len(path) <= 400 and "//" not in path and PATH.fullmatch(path) is not None


def sign(email: str, brand: str, path: str, *, secret: str | None = None, now: float | None = None,
         nonce: str | None = None) -> str:
    """The exact token Relaunch72 verifies: base64url(canonical JSON) . base64url(HMAC-SHA256)."""
    key = config.R72_SSO_SECRET if secret is None else secret
    if not key or len(key) < 32 or len(key) > 256:
        raise HandoffUnavailable()
    login = (email or "").strip().lower()
    host = host_for(brand)
    if not EMAIL.fullmatch(login) or len(login) > 320 or not host or not valid_path(path):
        raise HandoffUnavailable()
    issued = int(time.time() if now is None else now)
    claims = {"v": VERSION, "email": login, "brand": brand, "host": host, "path": path,
              "iat": issued, "exp": issued + LIFETIME_SECONDS, "nonce": nonce or secrets.token_urlsafe(24)}
    # Fixed key order and compact separators so both sides sign identical bytes (JSON.stringify shape).
    body = _b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    mac = hmac.new(key.encode("utf-8"), b"group-hq-handoff\x00" + body.encode("ascii"), sha256).digest()
    return f"{body}.{_b64url(mac)}"


def handoff(email: str, brand: str, path: str, **kw) -> Handoff:
    """A redirect target for the browser; the token lives only in that one-time URL."""
    token = sign(email, brand, path, **kw)
    host = host_for(brand)
    return Handoff(brand=brand, host=host, path=path, url=f"https://{host}{ROUTE}?t={token}")


def configured() -> bool:
    return 32 <= len(config.R72_SSO_SECRET) <= 256


def verify_for_tests(token: str, secret: str) -> dict:
    """Decode a token this module signed. Tests only: Relaunch72 is the verifier in production."""
    body, _, mac = token.partition(".")
    expected = hmac.new(secret.encode("utf-8"), b"group-hq-handoff\x00" + body.encode("ascii"), sha256).digest()
    if not hmac.compare_digest(_b64url(expected), mac):
        raise HandoffUnavailable()
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
