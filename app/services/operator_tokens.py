"""Signed client ids for the operator API's OAuth server (docs/operator-api.md).

Dynamic client registration is unauthenticated, so a registered client is not stored: its id is a signed
description of its redirect URIs in the byte format app.services.relaunch72_sso uses, base64url(compact JSON) "."
base64url(HMAC-SHA256), keyed by GHQ_OPERATOR_OAUTH_SECRET. Codes and tokens are not signed blobs: they are opaque
values kept as hashes in Postgres by app.services.operator_oauth_store, which makes codes single use across every
instance and lets refresh tokens rotate and connections be revoked one at a time.
"""
from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256

from .. import config

CLIENT = "ghq-oauth-client"
KINDS = (CLIENT,)

MAX_TOKEN_LENGTH = 4096


class TokenError(Exception):
    """The value is not one of ours, is of another kind or has expired."""


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
