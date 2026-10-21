"""Login (password, then authenticator code), logout and password change."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup
from sqlalchemy.orm import Session

from ..auth import (THROTTLED_MESSAGE, _DUMMY_HASH, check_origin, clear_pending, clear_session, current_user,
                    find_user_by_email, hash_password, login_allowed, password_needs_rehash, password_problem,
                    pending_secret, provisioning_uri, qr_svg, read_pending, record_failure, require_admin, safe_next,
                    set_pending, set_session, touch_login, verify_password, verify_totp)
from .. import config
from ..db import get_db
from ..models import User
from ..services.audit import audit
from ..templating import go, render

router = APIRouter(dependencies=[Depends(check_origin)])

CODE_MESSAGE = "That code was not accepted. Check the time on your phone and try the next one."


# ---- step 1: password ---------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", user: User | None = Depends(current_user)):
    next = safe_next(next, "/")
    if user:
        return RedirectResponse(next, status_code=303)
    return render(request, "login.html", {"next": next, "error": ""})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    next = safe_next(next, "/")
    norm, keys, allowed = login_allowed(db, request, email)
    if not allowed:
        return render(request, "login.html", {"next": next, "error": THROTTLED_MESSAGE}, 429)
    user = find_user_by_email(db, norm)
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not user.is_active or not ok:
        record_failure(db, keys)
        return render(request, "login.html", {"next": next, "error": "Email or password not recognised."}, 401)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)     # the library's parameters moved on; keep the hash current
        db.add(user)
        db.commit()
    resp = RedirectResponse("/login/code", status_code=303)
    set_pending(resp, user, next_path=next)
    return resp


# ---- step 2: authenticator code (enrolment on the first login) --------------------
def _enrolment_ctx(user: User, secret: str) -> dict:
    uri = provisioning_uri(secret, user.email)
    return {"enrol": True, "secret": secret, "uri": uri, "qr": Markup(qr_svg(uri))}


@router.get("/login/code", response_class=HTMLResponse)
def code_form(request: Request, db: Session = Depends(get_db)):
    pending = read_pending(request, db)
    if pending is None:
        return go("/login", err="Start by entering your email and password.")
    user, data = pending
    if user.totp_secret:
        return render(request, "totp.html", {"enrol": False, "error": "", "email": user.email})
    secret = pending_secret(db, user)      # kept on the user row, never in the cookie
    db.commit()
    return render(request, "totp.html", {"error": "", "email": user.email, **_enrolment_ctx(user, secret)})


@router.post("/login/code", response_class=HTMLResponse)
def code_submit(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    pending = read_pending(request, db)
    if pending is None:
        return go("/login", err="That took too long. Start again with your email and password.")
    user, data = pending
    norm, keys, allowed = login_allowed(db, request, user.email)
    enrolling = not user.totp_secret
    ctx = {"error": "", "email": user.email}
    if enrolling:
        secret = pending_secret(db, user)
        db.commit()
        ctx.update(_enrolment_ctx(user, secret))
    else:
        secret = user.totp_secret
        ctx["enrol"] = False
    if not allowed:
        ctx["error"] = THROTTLED_MESSAGE
        return render(request, "totp.html", ctx, 429)
    counter = verify_totp(secret, code, last_used=None if enrolling else user.totp_last_used)
    if counter is None:
        record_failure(db, keys)
        ctx["error"] = CODE_MESSAGE
        return render(request, "totp.html", ctx, 401)
    if enrolling:
        user.totp_secret = secret
        user.totp_pending_secret = None
        user.totp_pending_at = None
        audit(db, user, "totp.enrol", f"user:{user.id}")
    user.totp_last_used = counter
    touch_login(db, user)
    audit(db, user, "login", f"user:{user.id}")
    db.commit()
    resp = RedirectResponse(safe_next(data.get("next"), "/"), status_code=303)
    clear_pending(resp)
    set_session(resp, user)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    clear_session(resp)
    return resp


# ---- password change ---------------------------------------------------------------
@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, first: str = "", user: User = Depends(require_admin)):
    return render(request, "password.html", {"user": user, "section": "password", "first": bool(first) and user.must_change_password,
                                             "error": "", "min_len": config.MIN_PASSWORD_LENGTH})


@router.post("/password", response_class=HTMLResponse)
def password_change(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                    confirm_password: str = Form(""), db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    ctx = {"user": user, "section": "password", "first": user.must_change_password, "min_len": config.MIN_PASSWORD_LENGTH}
    if not verify_password(current_password, user.password_hash):
        return render(request, "password.html", {**ctx, "error": "Your current password was not recognised."}, 401)
    problem = password_problem(new_password, confirm_password, current_password)
    if problem:
        return render(request, "password.html", {**ctx, "error": problem}, 400)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.add(user)
    audit(db, user, "password.change", f"user:{user.id}")
    db.commit()
    resp = go("/", ok="Password changed. Other devices have been signed out.")
    set_session(resp, user)       # the old cookie died with the old hash; keep this browser signed in
    return resp


@router.post("/password/keep", response_class=HTMLResponse)
def password_keep(request: Request, current_password: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    """First login only: the admin chose ADMIN_PASSWORD themselves and keeps it. The password is checked again,
    the forced-change flag is cleared and the choice is audited; the reminder to delete ADMIN_PASSWORD from the
    environment is the page's job."""
    ctx = {"user": user, "section": "password", "first": user.must_change_password, "min_len": config.MIN_PASSWORD_LENGTH}
    if not user.must_change_password:
        return go("/password")
    if not verify_password(current_password, user.password_hash):
        return render(request, "password.html", {**ctx, "error": "Your current password was not recognised."}, 401)
    user.must_change_password = False
    db.add(user)
    audit(db, user, "password.keep", f"user:{user.id}")
    db.commit()
    return go("/", ok="Password kept. Now delete ADMIN_PASSWORD from the service's Environment page in Render.")
