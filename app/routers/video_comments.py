import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import inbox, video_comments as service
from ..services.inbox_providers import BRANDS, InboxError
from ..services.video_comments_provider import CHANNELS, PROFILES, VideoComments, VideoSetup, target
from ..templating import go, render

router = APIRouter()


def location(base):
    return service.PAGE + "?" + urlencode({"brand": base.brand, "channel": base.channel})


def error_text(error):
    if error.code == "business_connection_needed":
        return "TikTok skipped this posting-only connection. Reconnect through its Business-app flow to enable public comments. UK direct messages remain unavailable."
    return inbox.MESSAGES.get(error.code, "This public channel needs its connection checked.")


AUTH_ERRORS = {
    "missing_google_permissions": "Google authorization did not include the required permissions. Connect again and keep them selected.",
    "missing_tiktok_permissions": "TikTok authorization did not include the required permissions. Reconnect and keep them selected.",
    "no_youtube_channel": "Choose the Google identity or Brand Account that owns your YouTube channel.",
    "account_mismatch": "The provider returned a different account. Reconnect using the expected channel.",
    "reconnect_account_mismatch": "Reconnect the original account shown for this business.",
    "account_limit_exceeded": "Zernio's current account limit prevented this connection. Review its existing plan before trying again.",
    "profile_limit_exceeded": "Zernio's current profile limit prevented this connection. Review its existing plan before trying again.",
    "payment_required": "Zernio requires an account change before this connection. Review it in Zernio; Growth HQ has not purchased anything.",
}


@router.get(service.PAGE)
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    base = service.scope(request.query_params.get("brand", "mhp"), request.query_params.get("channel", "youtube"), user)
    ctx = {"user": user, "section": "inbox", "brands": BRANDS, "profiles": PROFILES, "channels": CHANNELS,
        "base": base, "account": None, "candidates": [], "posts": [], "comments": [], "error": None,
        "post_ticket": None, "posts_next": None, "comments_next": None}
    if request.query_params.get("error"):
        ctx["error"] = AUTH_ERRORS.get(request.query_params["error"], "Provider authorization did not complete. Check the chosen account and try connecting again.")
    try:
        current = service.configured(db, user, base)
        ctx["account"] = current
        if request.query_params.get("post"):
            current, bound = service.resolve(request.query_params["post"], user, "post", db)
            if (current.brand, current.channel) != (base.brand, base.channel):
                raise HTTPException(409, "Choose the original public channel before opening its comments.")
            ctx["post_ticket"] = request.query_params["post"]
            ctx["post_id"] = bound["post"]
            comments, ctx["comments_next"] = VideoComments(current).comments(bound["post"], request.query_params.get("cursor"))
            for comment in comments:
                if comment["can_reply"] and current.can_reply:
                    comment["ticket"] = service.ticket(current, user, "reply", post=bound["post"], comment=comment["id"])
                    comment["request_id"] = str(uuid.uuid4())
            ctx["comments"] = comments
        else:
            for candidate in VideoSetup(base).inventory():
                candidate["ticket"] = service.ticket(base, user, "enable", id=candidate["id"])
                ctx["candidates"].append(candidate)
            if current:
                posts, ctx["posts_next"] = VideoComments(current).posts(request.query_params.get("cursor"))
                for post in posts:
                    post["ticket"] = service.ticket(current, user, "post", post=post["id"])
                ctx["posts"] = posts
    except InboxError as error:
        ctx["error"] = error_text(error)
    response = render(request, "video_comments.html", ctx)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})
    return response


@router.post(service.PAGE + "/connect", dependencies=[Depends(check_origin)])
def connect(brand: str = Form(...), channel: str = Form(...), reconnect: str = Form(""), user: User = Depends(require_admin)):
    base = service.scope(brand, channel, user)
    try:
        url = VideoSetup(base).connect_url(config.BASE_URL + location(base), reconnect or None)
    except InboxError as error:
        return go(location(base), err=error_text(error))
    response = RedirectResponse(url, status_code=303)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return response


@router.post(service.PAGE + "/enable", dependencies=[Depends(check_origin)])
def enable(value: str = Form(...), acknowledge: bool = Form(False), replies: bool = Form(False),
           db: Session = Depends(get_db), user: User = Depends(require_admin)):
    base, _ = service.resolve(value, user, "enable")
    try:
        service.enable(db, user, value, acknowledge, replies)
    except InboxError as error:
        return go(location(base), err=error_text(error))
    return go(location(base), ok="Public channel enabled for this business. Its comments are separate from private messages.")


@router.post(service.PAGE + "/reply", dependencies=[Depends(check_origin)])
def reply(value: str = Form(...), request_id: str = Form(...), message: str = Form(...),
          audience: str = Form(""), acknowledge: bool = Form(False),
          db: Session = Depends(get_db), user: User = Depends(require_admin)):
    current, bound = service.resolve(value, user, "reply", db)
    if audience != "public_" + current.channel or acknowledge is not True:
        raise HTTPException(400, "Confirm that this reply is public on the selected video channel.")
    destination = location(current) + "&" + urlencode({"post": service.ticket(current, user, "post", post=bound["post"])})
    try:
        receipt = inbox.send(db, user, current, target(current, bound["post"], bound["comment"]), message, request_id)
    except HTTPException as error:
        if error.status_code not in (400, 409):
            raise
        return go(destination, err=error.detail)
    if receipt.state == "accepted":
        return go(destination, ok=CHANNELS[current.channel] + " accepted your public reply. Check the comment on the channel.")
    return go(destination, err=(inbox.MESSAGES["outcome_unknown"] if receipt.state in ("calling", "outcome_unknown")
              else inbox.MESSAGES.get(receipt.reason, "The channel refused this public reply.")))
