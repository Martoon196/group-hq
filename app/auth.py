"""Passwords (argon2), authenticator codes (TOTP), signed-cookie sessions, throttles and route guards.

Login is two steps. The password step issues a short-lived "pending" cookie; the code step turns it
into the session cookie. A user with no authenticator yet is shown an enrolment secret at the code
step and it is stored only once a code made from it has been accepted.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from urllib.parse import urlparse

import pyotp
import qrcode
import qrcode.image.svg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import config
from .db import get_db
from .models import LoginAttempt, User, utcnow
from .services.keys import normalise_email

_session_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="grouphq-session")
_pending_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="grouphq-pending")
_hasher = PasswordHasher()      # argon2id with the library's current defaults (64 MiB, 3 passes, 4 lanes)


# ---- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored: str | None) -> bool:
    try:
        return _hasher.verify(stored or "", password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(stored: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored)
    except (InvalidHashError, ValueError):
        return True


# A hash to verify against when the email is unknown, so a wrong email costs as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def generate_password(length: int = 14) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def find_user_by_email(db: Session, email: str) -> User | None:
    norm = normalise_email(email)
    if not norm:
        return None
    return db.query(User).filter(func.lower(User.email) == norm).first()


# ---- login throttle ---------------------------------------------------------
# Failed attempts are rows in login_attempts, so the budget survives a deploy and a second instance.
# Three keys are charged on every failure: the address plus the email, the address alone, and the
# account alone. The account key cannot be reset by changing the address a request claims to come from.
MAX_ATTEMPTS = 8            # failed password or code attempts per address + email in the window
MAX_IP_ATTEMPTS = 40        # failed attempts per address, whatever the email
MAX_ACCOUNT_ATTEMPTS = 20   # failed attempts against one account, whatever the address
WINDOW = 15 * 60
THROTTLED_MESSAGE = "Too many attempts. Try again in 15 minutes."


def client_ip(request: Request) -> str:
    """The caller's address. Behind Render's proxy the real address is the RIGHT-most X-Forwarded-For entry,
    the one the proxy appended; anything further left was supplied by the caller and is not trusted. Without
    the header (local development) the socket address is used."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip() if forwarded else ""
    if last:
        return last[:64]
    return request.client.host if request.client else "x"


def throttle_check(db: Session, key: str, limit: int = MAX_ATTEMPTS, window: int = WINDOW) -> bool:
    since = utcnow() - timedelta(seconds=window)
    hits = db.query(func.count(LoginAttempt.id)).filter(LoginAttempt.key == key, LoginAttempt.attempted_at >= since).scalar()
    return int(hits or 0) < limit


def throttle_hit(db: Session, key: str, window: int = WINDOW) -> None:
    """Record a failure and drop this key's rows that have left the window, so the table never grows past
    the number of attempts in one window."""
    since = utcnow() - timedelta(seconds=window)
    db.query(LoginAttempt).filter(LoginAttempt.key == key, LoginAttempt.attempted_at < since).delete(synchronize_session=False)
    db.add(LoginAttempt(key=key[:320], attempted_at=utcnow()))


def login_keys(request: Request, email: str) -> tuple[str, str, str, str]:
    """(normalised email, per-address+email key, per-address key, per-account key). The email is normalised
    exactly as the user lookup normalises it, so padding or capitalising it cannot buy a fresh attempt budget."""
    norm = normalise_email(email)
    ip = client_ip(request)
    return norm, f"{ip}:{norm}", f"ip:{ip}", f"acct:{norm}"


def login_allowed(db: Session, request: Request, email: str) -> tuple[str, list[str], bool]:
    norm, key, ip_key, acct_key = login_keys(request, email)
    allowed = (throttle_check(db, key) and throttle_check(db, ip_key, MAX_IP_ATTEMPTS)
               and throttle_check(db, acct_key, MAX_ACCOUNT_ATTEMPTS))
    return norm, [key, ip_key, acct_key], allowed


def record_failure(db: Session, keys: list[str]) -> None:
    """Commits: a failed attempt must be on record even though the request itself changes nothing else."""
    for key in keys:
        throttle_hit(db, key)
    db.commit()


# ---- authenticator codes -----------------------------------------------------
TOTP_STEP = 30


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=config.TOTP_ISSUER)


def qr_svg(data: str) -> str:
    """An inline SVG QR code (no raster library needed)."""
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return image.to_string(encoding="unicode")


def verify_totp(secret: str, code: str, *, last_used: int | None = None, now: float | None = None) -> int | None:
    """The 30-second counter the code matched, or None. Accepts one step either side of now for clock drift
    and refuses any counter at or before the last one accepted, so a code can never be replayed."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    totp = pyotp.TOTP(secret)
    ts = int(now if now is not None else time.time())
    base = ts // TOTP_STEP
    for offset in (0, -1, 1):
        counter = base + offset
        if hmac.compare_digest(totp.at(counter * TOTP_STEP), code):
            if last_used is not None and counter <= last_used:
                return None
            return counter
    return None


# ---- sessions --------------------------------------------------------------
def _credential_version(user: User) -> str:
    """A short fingerprint of the password hash. It changes on every password change or reset, so every
    cookie issued before that stops working at once."""
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def _cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                        secure=config.SECURE_COOKIES, path="/")


def set_session(response: Response, user: User) -> None:
    token = _session_serializer.dumps({"uid": user.id, "pv": _credential_version(user)})
    _cookie(response, config.SESSION_COOKIE, token, config.SESSION_MAX_AGE)


def clear_session(response: Response) -> None:
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    response.delete_cookie(config.PENDING_COOKIE, path="/")


def set_pending(response: Response, user: User, *, next_path: str = "/") -> None:
    """After a correct password: a cookie that says who passed and where to go afterwards. The enrolment secret
    never travels in it; it is held in users.totp_pending_secret until a code is accepted (pending_secret)."""
    data = {"uid": user.id, "pv": _credential_version(user), "next": next_path}
    _cookie(response, config.PENDING_COOKIE, _pending_serializer.dumps(data), config.PENDING_MAX_AGE)


def pending_secret(db: Session, user: User) -> str:
    """The authenticator secret on offer to a user who has none yet: the one already offered within the pending
    window, or a fresh one stored against the user. Caller commits."""
    if user.totp_pending_secret and user.totp_pending_at and \
            utcnow() - user.totp_pending_at < timedelta(seconds=config.PENDING_MAX_AGE):
        return user.totp_pending_secret
    user.totp_pending_secret = new_totp_secret()
    user.totp_pending_at = utcnow()
    db.add(user)
    return user.totp_pending_secret


def clear_pending(response: Response) -> None:
    response.delete_cookie(config.PENDING_COOKIE, path="/")


def _load_user(db: Session, data: object) -> User | None:
    if not isinstance(data, dict):
        return None
    user = db.get(User, data.get("uid"))
    if not user or not user.is_active:
        return None
    if not hmac.compare_digest(str(data.get("pv", "")), _credential_version(user)):
        return None   # password changed or reset since this cookie was issued
    return user


def read_pending(request: Request, db: Session) -> tuple[User, dict] | None:
    token = request.cookies.get(config.PENDING_COOKIE)
    if not token:
        return None
    try:
        data = _pending_serializer.loads(token, max_age=config.PENDING_MAX_AGE)
    except BadSignature:
        return None
    user = _load_user(db, data)
    return (user, data) if user else None


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _session_serializer.loads(token, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        return None
    return _load_user(db, data)


def _redirect(location: str) -> HTTPException:
    # app.main turns an HTTPException with a Location header into a 303 redirect.
    return HTTPException(status_code=303, headers={"Location": location})


def safe_next(target: str | None, default: str = "/") -> str:
    """A post-login destination must be a path on this site: "//evil.example" and absolute URLs fall back."""
    t = (target or "").strip()
    if t.startswith("/") and not t.startswith(("//", "/\\")):
        return t
    return default


PASSWORD_PAGE = "/password"


def require_admin(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise _redirect(f"/login?next={request.url.path}")
    if user.must_change_password and not request.url.path.startswith(PASSWORD_PAGE):
        raise _redirect(f"{PASSWORD_PAGE}?first=1")
    return user


def check_origin(request: Request) -> None:
    """CSRF guard for state-changing requests: the Origin (or Referer) host must be ours."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return  # non-browser client (tests, curl); cookies are SameSite=Lax anyway
    host = request.headers.get("host", "")
    if urlparse(origin).netloc.split(":")[0] != host.split(":")[0]:
        raise HTTPException(status_code=403, detail="Cross-site request blocked")


def touch_login(db: Session, user: User) -> None:
    user.last_login = utcnow()
    db.add(user)


def password_problem(new: str, confirm: str, current: str) -> str:
    """Why a new password is refused, or '' when it is acceptable."""
    if len(new) < config.MIN_PASSWORD_LENGTH:
        return f"Use at least {config.MIN_PASSWORD_LENGTH} characters."
    if new != confirm:
        return "The two new passwords do not match."
    if new == current:
        return "Choose a password you have not used here before."
    return ""
