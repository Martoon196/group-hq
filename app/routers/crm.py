"""Founder CRM front door; product lead data is read only from Relaunch72."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..auth import check_origin, require_admin
from ..models import User
from ..services.relaunch72_crm import CrmUnavailable, fetch_leads
from ..templating import render

router = APIRouter(dependencies=[Depends(check_origin)])


@router.get("/crm", response_class=HTMLResponse)
def crm(request: Request, user: User = Depends(require_admin)):
    cursor = request.query_params.get("cursor")
    try:
        page = fetch_leads(cursor)
        unavailable = False
    except CrmUnavailable:
        page = None
        unavailable = True
    response = render(request, "crm.html", {"user": user, "section": "crm", "page": page,
                                            "unavailable": unavailable})
    response.headers["Cache-Control"] = "no-store"
    return response
