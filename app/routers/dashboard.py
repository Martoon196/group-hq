"""The home screen is Today: everything waiting for the founder and the health of what feeds it. It opens from this
database alone; the inbox part loads into it afterwards (/today/replies). The older overview
(counts, the "Needs a region" queue from what the recompute last read, and the latest audit entries) moved to
/overview."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import AuditLog, ConnectorCall, EntitlementGrant, Mentee, Person, Programme, User
from ..services import earnings, inbox_scanner, today as today_service
from ..templating import render

REGION_CARD_ROWS = 8            # the dashboard lists this many; /mentees?region=needed lists them all

router = APIRouter(dependencies=[Depends(check_origin)])


@router.get("/", response_class=HTMLResponse)
def today(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    owner = user.email.lower() == config.GHQ_PERSONAL_INBOX_OWNER_EMAIL
    scanning = inbox_scanner.enabled(db, user) if owner else None
    ctx = today_service.build(db, scanning, earnings.needs_region(db))
    return render(request, "today.html", {"user": user, "section": "today", **ctx})


@router.get("/today/replies", response_class=HTMLResponse)
def today_replies(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """The inbox part of Today, fetched after the page shows: reading every connected account takes seconds."""
    from .inbox import conversation_list
    threads, results, error = [], [], None
    try:
        _, results, threads, error = conversation_list(db, user, [])
    except Exception:   # noqa: BLE001  (a broken inbox read is reported in the panel, never an error page)
        error = "The inbox could not be read just now."
    response = render(request, "_today_inbox.html", {"user": user, "today": today_service.uk_today(),
                                                     **today_service.build_inbox(threads, results, error)})
    response.headers["Cache-Control"] = "no-store"      # it carries message previews, like the inbox
    return response


@router.get("/overview", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    stats = {
        "people": db.query(Person).count(),
        "mentees_active": db.query(Mentee).filter(Mentee.status == "active").count(),
        "mentees_lapsed": db.query(Mentee).filter(Mentee.status == "lapsed").count(),
        "grants_active": db.query(EntitlementGrant).filter(EntitlementGrant.state == "active").count(),
        "grants_pending": db.query(EntitlementGrant).filter(EntitlementGrant.state == "pending").count(),
        "grants_failed": db.query(EntitlementGrant).filter(EntitlementGrant.state == "failed").count(),
        "calls_failed": db.query(ConnectorCall).filter(ConnectorCall.status == "failed").count(),
        "calls_pending": db.query(ConnectorCall).filter(ConnectorCall.status == "pending").count(),
        "programmes": db.query(Programme).filter(Programme.active.is_(True)).count(),
    }
    recent = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(12).all()
    regions = earnings.needs_region(db)
    return render(request, "dashboard.html", {"user": user, "section": "dashboard", "stats": stats, "recent": recent,
                                              "regions": regions[:REGION_CARD_ROWS], "regions_total": len(regions)})
