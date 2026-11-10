"""Webinars front door: both brands' webinars listed here; every page opens in Relaunch72 through the
sign-in handoff, so the founder signs in once (here) and never again on the brand hosts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import check_origin, require_admin
from ..models import User
from ..services import relaunch72_sso as sso
from ..services.relaunch72_webinars import WebinarsUnavailable, fetch_webinars
from ..templating import go, render

router = APIRouter(dependencies=[Depends(check_origin)])

OPEN_PATHS = {"list": "/portal/webinars", "new": "/portal/webinars/new", "setup": "/portal/webinars"}


def _brand(request: Request, allowed: set[str] = frozenset({"brand"})) -> str:
    brand = request.query_params.get("brand", "mhp")
    if brand not in sso.BRANDS or len(request.query_params.getlist("brand")) > 1 \
            or any(key not in allowed for key in request.query_params):
        raise HTTPException(status_code=400, detail="Choose Property Predator or Martin Howard Property")
    return brand


@router.get("/webinars", response_class=HTMLResponse)
def webinars(request: Request, user: User = Depends(require_admin)):
    brand = _brand(request)
    try:
        listing = fetch_webinars(brand)
        unavailable = False
    except WebinarsUnavailable:
        listing = None
        unavailable = True
    response = render(request, "webinars.html", {
        "user": user, "section": "webinars", "brand": brand, "brands": sso.BRANDS, "listing": listing,
        "unavailable": unavailable, "handoff_ready": sso.configured(), "host": sso.host_for(brand),
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/webinars/open")
def open_page(request: Request, user: User = Depends(require_admin)):
    """303 to the brand host with a fresh two-minute token; the token exists only in this one redirect."""
    brand = _brand(request, {"brand", "path"})
    path = request.query_params.get("path", OPEN_PATHS["list"])
    if len(request.query_params.getlist("path")) > 1 or not sso.valid_path(path):
        raise HTTPException(status_code=400, detail="That is not a Relaunch72 portal page")
    try:
        target = sso.handoff(user.email, brand, path)
    except sso.HandoffUnavailable:
        return go(f"/webinars?brand={brand}", err="Sign-in handoff is not configured (R72_SSO_SECRET); open the brand host and sign in there.")
    response = RedirectResponse(target.url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
