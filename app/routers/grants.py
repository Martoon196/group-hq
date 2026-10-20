"""Grants: every entitlement grant with filters, the connector call outbox, and the nightly recompute
run on demand with a dry-run toggle.

    GET  /grants                     list with filters (product, kind, state, search) and the run form
    POST /grants/recompute           run the nightly recompute now (dry_run=1 records and shows the calls)
    GET  /grants/calls               the connector_calls outbox
    GET  /grants/{id}                one grant, its calls and its audit trail
    POST /grants/{id}/recompute      recompute that person only (dry_run=1 as above)
    POST /grants/{id}/retry          clear a failed or blocked grant and recompute the person

A run renders a report page (grants/run.html) rather than redirecting, because the point of a dry run
is to read what would have been sent. Same conventions as the rest of the app otherwise: require_admin
on every route, check_origin on every POST, audit rows written by the service layer.
"""
from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import GRANT_KINDS, PRODUCTS, AuditLog, ConnectorCall, EntitlementGrant, Person, User
from ..services import settings
from ..services.entitlements import (BLOCKED_ERROR, MAX_ATTEMPTS, RunSummary, display_state, is_blocked, recompute_all,
                                     recompute_person, reset_for_retry, utc_today)
from ..templating import go, render

router = APIRouter(prefix="/grants", dependencies=[Depends(check_origin)])

PAGE_SIZE = 50
STATES = ("pending", "active", "revoked", "failed", "blocked")
CALL_STATUSES = ("pending", "succeeded", "failed")


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _page(raw: str | int) -> int:
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _parse_today(raw: str, dry_run: bool) -> date:
    """The as-of date of a run. Any date is fine for a dry run; a live run can only be as of today, because a
    back-dated run would re-grant lapsed people and a forward-dated one would revoke everyone."""
    raw = (raw or "").strip()
    if not raw:
        return utc_today()
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="The date must look like 2026-11-07.")
    if not dry_run and day != utc_today():
        raise HTTPException(status_code=400, detail="A different as-of date is only allowed in a dry run.")
    return day


def _dry(flag: str) -> bool:
    return bool(flag) or bool(config.CONNECTORS_DRY_RUN)


def _latest_calls(db: Session, grant_ids: list[int]) -> dict[int, ConnectorCall]:
    """The newest connector call per grant, for the list page's attempts and error columns."""
    if not grant_ids:
        return {}
    rows = (db.query(ConnectorCall).filter(ConnectorCall.grant_id.in_(grant_ids))
            .order_by(ConnectorCall.grant_id, ConnectorCall.id.desc()).all())
    latest: dict[int, ConnectorCall] = {}
    for row in rows:
        latest.setdefault(row.grant_id, row)
    return latest


def _common(user: User) -> dict:
    return {"user": user, "section": "grants", "env_dry_run": bool(config.CONNECTORS_DRY_RUN),
            "product_name": settings.product_name, "display_state": display_state, "is_blocked": is_blocked,
            "max_attempts": MAX_ATTEMPTS}


# ---- the list ----------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
def grants_list(request: Request, product: str = "", kind: str = "", state: str = "", q: str = "", page: str = "1",
                db: Session = Depends(get_db), user: User = Depends(require_admin)):
    filters = {"product": product if product in PRODUCTS else "", "kind": kind if kind in GRANT_KINDS else "",
               "state": state if state in STATES else "", "q": (q or "").strip()[:120]}
    query = db.query(EntitlementGrant).join(Person, EntitlementGrant.person_id == Person.id)
    if filters["product"]:
        query = query.filter(EntitlementGrant.product == filters["product"])
    if filters["kind"]:
        query = query.filter(EntitlementGrant.kind == filters["kind"])
    if filters["state"] == "blocked":
        query = query.filter(EntitlementGrant.state == "failed", EntitlementGrant.last_error == BLOCKED_ERROR)
    elif filters["state"] == "failed":
        query = query.filter(EntitlementGrant.state == "failed", EntitlementGrant.last_error != BLOCKED_ERROR)
    elif filters["state"]:
        query = query.filter(EntitlementGrant.state == filters["state"])
    if filters["q"]:
        pattern = _like(filters["q"].lower())
        query = query.filter(or_(func.lower(Person.person_key).like(pattern, escape="\\"),
                                 func.lower(Person.name).like(pattern, escape="\\"),
                                 func.lower(EntitlementGrant.value).like(pattern, escape="\\")))
    total = query.count()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    current = min(_page(page), pages)
    grants = query.order_by(EntitlementGrant.id.desc()).offset((current - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    active_filters = {k: v for k, v in filters.items() if v}
    qs = urlencode(active_filters)
    ctx = {
        **_common(user), "grants": grants, "latest": _latest_calls(db, [g.id for g in grants]), "filters": filters,
        "products": PRODUCTS, "kinds": GRANT_KINDS, "states": STATES, "total": total, "page": current, "pages": pages,
        "page_size": PAGE_SIZE, "qs": qs,
        "qs_prev": urlencode({**active_filters, "page": max(1, current - 1)}),
        "qs_next": urlencode({**active_filters, "page": min(pages, current + 1)}),
        "today": utc_today(),
        "counts": {s: _count_state(db, s) for s in STATES},
    }
    return render(request, "grants/list.html", ctx)


def _count_state(db: Session, state: str) -> int:
    query = db.query(EntitlementGrant)
    if state == "blocked":
        return query.filter(EntitlementGrant.state == "failed", EntitlementGrant.last_error == BLOCKED_ERROR).count()
    if state == "failed":
        return query.filter(EntitlementGrant.state == "failed", EntitlementGrant.last_error != BLOCKED_ERROR).count()
    return query.filter(EntitlementGrant.state == state).count()


# ---- running the recompute -----------------------------------------------------------------
def _run_page(request: Request, user: User, summary: RunSummary, *, scope: str, back: str = "/grants") -> HTMLResponse:
    ctx = {**_common(user), "summary": summary, "decisions": summary.decisions, "scope": scope, "back": back}
    return render(request, "grants/run.html", ctx)


@router.post("/recompute", response_class=HTMLResponse)
def run_now(request: Request, dry_run: str = Form(""), today: str = Form(""), db: Session = Depends(get_db),
            user: User = Depends(require_admin)):
    summary = recompute_all(db, today=_parse_today(today, _dry(dry_run)), dry_run=bool(dry_run), actor=user)
    return _run_page(request, user, summary, scope="everyone")


# ---- the outbox --------------------------------------------------------------------------
@router.get("/calls", response_class=HTMLResponse)
def calls_list(request: Request, status: str = "", product: str = "", page: str = "1", db: Session = Depends(get_db),
               user: User = Depends(require_admin)):
    filters = {"status": status if status in CALL_STATUSES else "", "product": product if product in PRODUCTS else ""}
    query = db.query(ConnectorCall)
    if filters["status"]:
        query = query.filter(ConnectorCall.status == filters["status"])
    if filters["product"]:
        query = query.filter(ConnectorCall.product == filters["product"])
    total = query.count()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    current = min(_page(page), pages)
    calls = query.order_by(ConnectorCall.id.desc()).offset((current - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    active_filters = {k: v for k, v in filters.items() if v}
    ctx = {
        **_common(user), "calls": calls, "filters": filters, "products": PRODUCTS, "statuses": CALL_STATUSES,
        "total": total, "page": current, "pages": pages, "qs": urlencode(active_filters),
        "qs_prev": urlencode({**active_filters, "page": max(1, current - 1)}),
        "qs_next": urlencode({**active_filters, "page": min(pages, current + 1)}),
    }
    return render(request, "grants/calls.html", ctx)


# ---- one grant ---------------------------------------------------------------------------
def _grant_or_404(db: Session, grant_id: int) -> EntitlementGrant:
    grant = db.get(EntitlementGrant, grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="That grant does not exist.")
    return grant


@router.get("/{grant_id}", response_class=HTMLResponse)
def grant_detail(request: Request, grant_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    grant = _grant_or_404(db, grant_id)
    calls = db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id).order_by(ConnectorCall.id.desc()).all()
    trail = db.query(AuditLog).filter(AuditLog.entity == f"grant:{grant.id}").order_by(AuditLog.id.desc()).limit(50).all()
    ctx = {**_common(user), "grant": grant, "person": grant.person, "mentee": grant.mentee, "calls": calls,
           "trail": trail, "today": utc_today()}
    return render(request, "grants/detail.html", ctx)


@router.post("/{grant_id}/recompute", response_class=HTMLResponse)
def recompute_one(request: Request, grant_id: int, dry_run: str = Form(""), today: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(require_admin)):
    grant = _grant_or_404(db, grant_id)
    person = grant.person
    day = _parse_today(today, _dry(dry_run))
    decisions = recompute_person(db, person, day, bool(dry_run), actor=user)
    summary = RunSummary(today=day, dry_run=_dry(dry_run), people=1, decisions=decisions)
    return _run_page(request, user, summary, scope=person.person_key, back=f"/grants/{grant.id}")


@router.post("/{grant_id}/retry", response_class=HTMLResponse)
def retry(request: Request, grant_id: int, dry_run: str = Form(""), db: Session = Depends(get_db),
          user: User = Depends(require_admin)):
    """Clear the failure and recompute the person. With dry_run=1 the attempt counter is still reset but the
    calls are only planned, so a blocked grant can be looked at before anything is sent."""
    grant = _grant_or_404(db, grant_id)
    if grant.state != "failed" and not grant.last_error:
        return go(f"/grants/{grant.id}", err="Only a failed or blocked grant can be retried.")
    reset_for_retry(db, grant, actor=user)
    db.commit()
    person = grant.person
    day = utc_today()
    decisions = recompute_person(db, person, day, bool(dry_run), actor=user)
    summary = RunSummary(today=day, dry_run=_dry(dry_run), people=1, decisions=decisions)
    return _run_page(request, user, summary, scope=f"retry for {person.person_key}", back=f"/grants/{grant.id}")
