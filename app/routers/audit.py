"""The audit log: a paged, searchable view of every recorded change. Read-only."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import AuditLog, User
from ..templating import render
from .people import like_pattern, page_of, qs

router = APIRouter(dependencies=[Depends(check_origin)])

PAGE_SIZE = 50


def _compact(value) -> str:
    """A before/after snapshot as one short JSON line for the table; None stays blank."""
    if value is None:
        return ""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= 600 else text[:597] + "..."


@router.get("/audit", response_class=HTMLResponse)
def audit_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    qp = request.query_params
    filters = {"q": qp.get("q", "").strip()[:120], "action": qp.get("action", "").strip()[:80]}
    q = db.query(AuditLog)
    if filters["q"]:
        like = like_pattern(filters["q"].lower())
        q = q.filter(or_(func.lower(AuditLog.entity).like(like, escape="\\"), func.lower(AuditLog.actor).like(like, escape="\\")))
    if filters["action"]:
        q = q.filter(AuditLog.action == filters["action"])
    q = q.order_by(AuditLog.id.desc())
    rows, total, page, pages = page_of(q, request, PAGE_SIZE)
    actions = [a for (a,) in db.query(AuditLog.action).distinct().order_by(AuditLog.action).all()]
    ctx = {"user": user, "section": "audit", "rows": [(r, _compact(r.before), _compact(r.after)) for r in rows], "total": total,
           "page": page, "pages": pages, "filters": filters, "actions": actions, "qs": qs(filters),
           "qs_prev": qs(filters, page=page - 1), "qs_next": qs(filters, page=page + 1), "page_size": PAGE_SIZE}
    return render(request, "audit.html", ctx)
