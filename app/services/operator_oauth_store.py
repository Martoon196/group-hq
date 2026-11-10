"""Durable OAuth state for the operator API (alembic/versions/0017_operator_oauth.py).

Codes, access tokens and refresh tokens are opaque random strings; only their SHA-256 hashes are stored. Redemption
and refresh lock their row (SELECT ... FOR UPDATE), so two instances racing one code or one refresh token cannot both
win: the second sees the row already consumed or rotated and revokes the whole grant. Every function commits its own
transaction and returns plain values; nothing here ever logs a code or a token.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import OperatorOAuthCode, OperatorOAuthGrant, OperatorOAuthToken, User, utcnow

CODE_LIFETIME = timedelta(minutes=2)
ACCESS_LIFETIME = timedelta(hours=1)
REFRESH_LIFETIME = timedelta(days=30)


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def opaque() -> str:
    """43 characters of base64url: the shape every code and token has."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str


def _active_user(db: Session, user_id: int) -> User | None:
    user = db.get(User, user_id)
    return user if user is not None and user.is_active else None


def _prune(db: Session, now: datetime) -> None:
    """Bounded housekeeping on each new code: day-old expired codes and tokens go."""
    cutoff = now - timedelta(days=1)
    stale_codes = select(OperatorOAuthCode.code_hash).where(OperatorOAuthCode.expires_at < cutoff).limit(200)
    db.execute(delete(OperatorOAuthCode).where(OperatorOAuthCode.code_hash.in_(stale_codes)))
    stale_tokens = select(OperatorOAuthToken.token_hash).where(OperatorOAuthToken.expires_at < cutoff).limit(200)
    db.execute(delete(OperatorOAuthToken).where(OperatorOAuthToken.token_hash.in_(stale_tokens)))


def issue_code(db: Session, *, user: User, client_id: str, client_name: str, redirect_uri: str, code_challenge: str,
               now: datetime | None = None) -> str | None:
    """Store a consented code and return it; None when the admin is no longer active."""
    now = now or utcnow()
    if _active_user(db, user.id) is None:
        return None
    _prune(db, now)
    code = opaque()
    db.add(OperatorOAuthCode(code_hash=digest(code), client_id_hash=digest(client_id), client_name=(client_name or "MCP client")[:100],
                             redirect_uri_hash=digest(redirect_uri), code_challenge=code_challenge, user_id=user.id,
                             created_at=now, expires_at=now + CODE_LIFETIME))
    db.commit()
    return code


def _issue_tokens(db: Session, grant: OperatorOAuthGrant, now: datetime) -> Tokens:
    access, refresh = opaque(), opaque()
    db.add_all([OperatorOAuthToken(token_hash=digest(access), grant_id=grant.id, kind="access", created_at=now, expires_at=now + ACCESS_LIFETIME),
                OperatorOAuthToken(token_hash=digest(refresh), grant_id=grant.id, kind="refresh", created_at=now, expires_at=now + REFRESH_LIFETIME)])
    return Tokens(access, refresh)


def _revoke(grant: OperatorOAuthGrant | None, reason: str, now: datetime) -> None:
    if grant is not None and grant.revoked_at is None:
        grant.revoked_at, grant.revoked_reason = now, reason


def redeem_code(db: Session, *, code: str, client_id: str, redirect_uri: str, verifier_challenge: str,
                now: datetime | None = None) -> tuple[str, Tokens | None]:
    """('issued', tokens) once; ('replayed', None) on any later presentation, which also ends what the code issued;
    ('invalid', None) for an unknown, expired or mismatched code."""
    now = now or utcnow()
    row = db.execute(select(OperatorOAuthCode).where(OperatorOAuthCode.code_hash == digest(code)).with_for_update()).scalar_one_or_none()
    if row is None:
        db.rollback()
        return "invalid", None
    if row.consumed_at is not None:
        _revoke(db.get(OperatorOAuthGrant, row.grant_id, with_for_update=True), "code_replay", now)
        db.commit()
        return "replayed", None
    if (row.expires_at <= now or row.client_id_hash != digest(client_id) or row.redirect_uri_hash != digest(redirect_uri)
            or row.code_challenge != verifier_challenge or _active_user(db, row.user_id) is None):
        db.rollback()
        return "invalid", None
    grant = OperatorOAuthGrant(user_id=row.user_id, client_id_hash=row.client_id_hash, client_name=row.client_name, created_at=now, last_used_at=now)
    db.add(grant)
    db.flush()
    row.consumed_at, row.grant_id = now, grant.id
    tokens = _issue_tokens(db, grant, now)
    db.commit()
    return "issued", tokens


def refresh(db: Session, *, refresh_token: str, client_id: str, now: datetime | None = None) -> tuple[str, Tokens | None]:
    """Rotate: ('issued', new pair). A rotated token presented again ('reused') or a token from another client
    ('client_mismatch') ends the whole grant; anything else unknown or expired is ('invalid', None)."""
    now = now or utcnow()
    token = db.execute(select(OperatorOAuthToken).where(OperatorOAuthToken.token_hash == digest(refresh_token),
                                                        OperatorOAuthToken.kind == "refresh").with_for_update()).scalar_one_or_none()
    if token is None:
        db.rollback()
        return "invalid", None
    grant = db.get(OperatorOAuthGrant, token.grant_id, with_for_update=True)
    if grant is None or grant.revoked_at is not None:
        db.rollback()
        return "invalid", None
    if grant.client_id_hash != digest(client_id):
        _revoke(grant, "client_mismatch", now)
        db.commit()
        return "client_mismatch", None
    if token.rotated_at is not None:
        _revoke(grant, "refresh_reuse", now)
        db.commit()
        return "reused", None
    if token.expires_at <= now or _active_user(db, grant.user_id) is None:
        db.rollback()
        return "invalid", None
    token.rotated_at = now
    grant.last_used_at = now
    tokens = _issue_tokens(db, grant, now)
    db.commit()
    return "issued", tokens


def resolve(db: Session, access_token: str, now: datetime | None = None) -> User | None:
    """The active admin behind a live access token of a live grant, else None."""
    now = now or utcnow()
    row = db.execute(select(OperatorOAuthGrant.user_id).join(OperatorOAuthToken, OperatorOAuthToken.grant_id == OperatorOAuthGrant.id)
                     .where(OperatorOAuthToken.token_hash == digest(access_token), OperatorOAuthToken.kind == "access",
                            OperatorOAuthToken.expires_at > now, OperatorOAuthGrant.revoked_at.is_(None))).first()
    return _active_user(db, row[0]) if row else None


def revoke_token(db: Session, *, token: str, client_id: str, now: datetime | None = None) -> bool:
    """RFC 7009: the client's own token ends its grant."""
    now = now or utcnow()
    row = db.execute(select(OperatorOAuthGrant).join(OperatorOAuthToken, OperatorOAuthToken.grant_id == OperatorOAuthGrant.id)
                     .where(OperatorOAuthToken.token_hash == digest(token), OperatorOAuthGrant.client_id_hash == digest(client_id),
                            OperatorOAuthGrant.revoked_at.is_(None)).with_for_update(of=OperatorOAuthGrant)).scalar_one_or_none()
    if row is None:
        db.rollback()
        return False
    _revoke(row, "client", now)
    db.commit()
    return True


def list_grants(db: Session, user: User) -> list[OperatorOAuthGrant]:
    return list(db.execute(select(OperatorOAuthGrant).where(OperatorOAuthGrant.user_id == user.id)
                           .order_by(OperatorOAuthGrant.created_at.desc(), OperatorOAuthGrant.id.desc()).limit(50)).scalars())


def revoke_grant(db: Session, user: User, grant_id: int, now: datetime | None = None) -> bool:
    now = now or utcnow()
    grant = db.get(OperatorOAuthGrant, grant_id, with_for_update=True)
    if grant is None or grant.user_id != user.id or grant.revoked_at is not None:
        db.rollback()
        return False
    _revoke(grant, "owner", now)
    db.commit()
    return True
