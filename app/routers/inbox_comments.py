"""Separate public surface. Mount once from app.main; no private composer reuse."""
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import inbox, inbox_comments as service
from ..services.inbox_providers import BRANDS, InboxError, client
from ..services.linkedin_comments_provider import reply_target
from ..templating import go, render

router = APIRouter()


@router.get(service.PAGE)
def comments(request: Request, user: User = Depends(require_admin)):
    ctx = {"user": user, "section": "inbox", "brands": BRANDS, "accounts": [], "posts": [],
           "comments": [], "error": None, "account": None, "post_ticket": None,
           "posts_next": None, "comments_next": None}
    try:
        ctx["accounts"] = service.accounts(user.email)
        key = request.query_params.get("account", "")
        post_value = request.query_params.get("post")
        if post_value:
            account, bound = service.resolve(post_value, user.id, user.email)
            if key and key != account.key:
                raise HTTPException(409, "Refresh public LinkedIn comments before continuing.")
            ctx.update({"account": account, "post_ticket": post_value, "post_id": bound["post"]})
            comments, ctx["comments_next"] = client(account).comments(bound["post"], request.query_params.get("cursor"))
            for comment in comments:
                if comment["can_reply"] and account.can_reply:
                    comment["reply_ticket"] = service.ticket(account, bound["post"], user.id,
                        comment_id=comment["id"], name=comment["name"])
                    comment["request_id"] = str(uuid.uuid4())
            ctx["comments"] = comments
        elif key:
            account = next((a for a in ctx["accounts"] if a.key == key), None)
            if account is None:
                raise HTTPException(400, "Choose an available company account.")
            ctx["account"] = account
            posts, ctx["posts_next"] = client(account).posts(request.query_params.get("cursor"))
            for post in posts:
                post["ticket"] = service.ticket(account, post["id"], user.id)
            ctx["posts"] = posts
    except InboxError as exc:
        ctx["error"] = inbox.MESSAGES.get(exc.code, "Public comments configuration needs checking.")
    response = render(request, "inbox_comments.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@router.post(service.PAGE + "/reply", dependencies=[Depends(check_origin)])
def reply(target: str = Form(...), request_id: str = Form(...), message: str = Form(...),
          audience: str = Form(""), acknowledge: bool = Form(False),
          db: Session = Depends(get_db), user: User = Depends(require_admin)):
    service.validate_audience(audience, acknowledge)
    account, bound = service.resolve(target, user.id, user.email, for_reply=True)
    post_ticket = service.ticket(account, bound["post"], user.id)
    destination = service.PAGE + "?" + urlencode({"account": account.key, "post": post_ticket})
    try:
        receipt = inbox.send(db, user, account, reply_target(bound["post"], bound["comment"]), message, request_id)
    except HTTPException as error:
        if error.status_code not in (400, 409):
            raise
        return go(destination, err=error.detail)
    if receipt.state == "accepted":
        return go(destination, ok="LinkedIn accepted your public comment reply. It is visible on LinkedIn.")
    status = inbox.MESSAGES.get(receipt.reason, "The public reply was refused.")
    if receipt.state in ("calling", "outcome_unknown"):
        status = "The public reply's outcome is unknown. Check LinkedIn before writing another reply; it will not be retried automatically."
    return go(destination, err=status)
