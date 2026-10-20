"""The home screen: counts that matter and the latest audit entries."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import AuditLog, ConnectorCall, EntitlementGrant, Mentee, Person, Programme, User
from ..templating import render

router = APIRouter(dependencies=[Depends(check_origin)])


@router.get("/", response_class=HTMLResponse)
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
    return render(request, "dashboard.html", {"user": user, "section": "dashboard", "stats": stats, "recent": recent})
