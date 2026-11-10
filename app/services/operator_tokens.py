"""Signed, stateless tokens for the operator API's OAuth server (docs/operator-api.md).

Four kinds share one byte format, the one app.services.relaunch72_sso uses: base64url(compact JSON) "."
base64url(HMAC-SHA256), no padding, the MAC taken over b"<kind>\\x00" + the body, keyed by
GHQ_OPERATOR_OAUTH_SECRET. A token of one kind can never pass as another, every kind but the client id
carries an expiry, and an authorisation code is single use through a small in-memory set of consumed nonces
(codes live two minutes, so the set stays small; it is capped all the same). Nothing is stored: rotating the
secret invalidates every token at once, and that is the only revocation there is.
"""
from __future__ import annotations

import base64
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from hashlib import sha256

from .. import config

CLIENT = "ghq-oauth-client"
CODE = "ghq-oauth-code"
ACCESS = "ghq-oauth-access"
REFRESH = "ghq-oauth-refresh"
KINDS = (CLIENT, CODE, ACCESS, REFRESH)

CODE_LIFETIME = 120                  # seconds: long enough for the redirect, short enough to keep the nonce set small
ACCESS_LIFETIME = 60 * 60
REFRESH_LIFETIME = 30 * 24 * 60 * 60
MAX_TOKEN_LENGTH = 4096
CONSUMED_CAP = 5000


class TokenError(Exception):
    """The token is not one of ours, is of another kind, has expired or was already used."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(secret: str | None) -> bytes:
    key = config.GHQ_OPERATOR_OAUTH_SECRET if secret is None else secret
    if not key or len(key) < 32 or len(key) > 256:
        raise TokenError("the OAuth secret is not configured")
    return key.encode("utf-8")


def _mac(key: bytes, kind: str, body: str) -> bytes:
    return hmac.new(key, kind.encode("ascii") + b"\x00" + body.encode("ascii"), sha256).digest()


def configured() -> bool:
    return config.operator_oauth_configured()


def nonce() -> str:
    return secrets.token_urlsafe(18)


def sign(kind: str, claims: dict, *, secret: str | None = None) -> str:
    """base64url(compact JSON of claims) . base64url(HMAC-SHA256 over kind \\0 body)."""
    if kind not in KINDS:
        raise ValueError(f"unknown token kind {kind!r}")
    key = _key(secret)
    body = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return f"{body}.{_b64url(_mac(key, kind, body))}"


def verify(kind: str, token: str, *, secret: str | None = None, now: float | None = None) -> dict:
    """The claims of a token this module signed as `kind`, or TokenError. A kind other than the client id must
    carry an integer exp in the future."""
    if kind not in KINDS:
        raise ValueError(f"unknown token kind {kind!r}")
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
        raise TokenError("malformed token")
    key = _key(secret)
    body, dot, mac = token.partition(".")
    if not dot or not body or not mac:
        raise TokenError("malformed token")
    if not hmac.compare_digest(_b64url(_mac(key, kind, body)), mac):
        raise TokenError("bad signature")
    try:
        claims = json.loads(_b64url_decode(body))
    except (ValueError, UnicodeDecodeError):
        raise TokenError("malformed token") from None
    if not isinstance(claims, dict):
        raise TokenError("malformed token")
    if kind != CLIENT:
        exp = claims.get("exp")
        if not isinstance(exp, int) or isinstance(exp, bool):
            raise TokenError("token without an expiry")
        if exp <= int(time.time() if now is None else now):
            raise TokenError("token expired")
    return claims


# ---- single-use authorisation codes ------------------------------------------------------------------------------
# nonce -> exp, oldest first. A nonce is forgotten once its code has expired anyway, and the set is capped: under a
# flood the oldest entries go first, which can only ever re-admit a code that is about to expire.
_consumed: OrderedDict[str, int] = OrderedDict()
_lock = threading.Lock()


def consume_code(code_nonce: str, exp: int, *, now: float | None = None) -> bool:
    """True the first time a code's nonce is presented, False on any later presentation."""
    ts = int(time.time() if now is None else now)
    with _lock:
        for stale in [n for n, e in _consumed.items() if e <= ts]:
            del _consumed[stale]
        if code_nonce in _consumed:
            return False
        _consumed[code_nonce] = exp
        while len(_consumed) > CONSUMED_CAP:
            _consumed.popitem(last=False)
        return True


def reset_consumed_for_tests() -> None:
    with _lock:
        _consumed.clear()
