import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import inbox as service
from ..services.inbox_providers import InboxError, client
from ..templating import go, render

router = APIRouter()


def thread_context(db, user, value, cursor=None):
    account, bound = service.resolve(value, user.id)
    messages, next_cursor, error = [], None, None
    try:
        messages, next_cursor = client(account).messages(bound["thread"], cursor)
    except InboxError as exc:
        error = service.MESSAGES.get(exc.code, "Connection unavailable.")
    return {"account": account, "conversation_name": bound["name"], "thread_ticket": value,
            "messages": messages, "next_cursor": next_cursor, "thread_error": error,
            "reply_receipts": service.replies(db, account, bound["thread"]), "request_id": str(uuid.uuid4())}


@router.get(service.PAGE, response_class=HTMLResponse)
def inbox(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    selected = request.query_params.getlist("account")
    brand, channel = request.query_params.get("brand", ""), request.query_params.get("channel", "")
    ctx = {"user": user, "section": "inbox", "brands": service.BRANDS, "channels": service.CHANNELS,
           "selected": selected, "brand_filter": brand, "channel_filter": channel,
           "accounts": [], "results": [], "threads": [], "setup_error": None}
    try:
        ctx["accounts"], ctx["results"], ctx["threads"] = service.snapshot(user.id, selected, brand, channel)
    except InboxError:
        ctx["setup_error"] = "Inbox account configuration needs checking. Existing channels remain available in their workspaces."
    value = request.query_params.get("thread")
    if value:
        ctx.update(thread_context(db, user, value, request.query_params.get("cursor")))
    response = render(request, "inbox.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(service.PAGE + "/reply", dependencies=[Depends(check_origin)])
def reply(request: Request, thread_ticket: str = Form(...), request_id: str = Form(...),
          message: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    account, bound = service.resolve(thread_ticket, user.id)
    try:
        receipt = service.send(db, user, account, bound["thread"], message, request_id)
    except HTTPException as error:
        if error.status_code not in (400, 409):
            raise
        ctx = thread_context(db, user, thread_ticket)
        ctx.update({"user": user, "section": "inbox", "accounts": service.accounts(), "results": [], "threads": [],
            "brands": service.BRANDS, "channels": service.CHANNELS, "selected": [account.key], "brand_filter": "",
            "channel_filter": "", "draft": message, "request_id": request_id, "flash": error.detail, "flash_kind": "err"})
        response = render(request, "inbox.html", ctx, status_code=error.status_code)
        response.headers["Cache-Control"] = "no-store"
        return response
    from urllib.parse import urlencode
    status = {"accepted": "The channel accepted your reply.",
              "bridge_accepted": "Your connector accepted the reply. Delivery to the original app is not confirmed.",
              "calling": service.MESSAGES["outcome_unknown"],
              "outcome_unknown": service.MESSAGES["outcome_unknown"],
              "rejected": service.MESSAGES.get(receipt.reason, "The channel refused the reply.")}[receipt.state]
    return go(service.PAGE + "?" + urlencode({"account": account.key, "thread": thread_ticket}),
              **({"ok": status} if receipt.state in ("accepted", "bridge_accepted") else {"err": status}))
