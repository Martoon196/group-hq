import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import inbox as service, inbox_crm, inbox_organisation
from ..services.inbox_providers import InboxError, client
from ..templating import go, render

router = APIRouter()


@router.get("/inbox/channels")
def channels(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    accounts = [a for a in service.accounts(db) if not a.owner_email or a.owner_email == user.email]
    response = render(request, "inbox_channels.html", {"user": user, "section": "inbox", "accounts": accounts,
        "brands": service.BRANDS, "channels": service.CHANNELS})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/inbox/channels/brand", dependencies=[Depends(check_origin)])
def channel_brand(account_key: str = Form(...), brand: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from sqlalchemy.dialects.postgresql import insert
    from ..models import AppSetting, utcnow
    from ..services.audit import audit
    account = next((a for a in service.accounts(db) if a.key == account_key and not a.owner_email), None)
    if account is None or brand not in service.BRANDS:
        raise HTTPException(400, "Choose a business account and valid brand.")
    key = service.account_brand_key(account)
    db.execute(insert(AppSetting).values(key=key, value={"brand": brand}, updated_by=user.email, updated_at=utcnow())
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": {"brand": brand}, "updated_by": user.email, "updated_at": utcnow()}))
    audit(db, user, "inbox.account.brand", account.key, after={"brand": brand})
    db.commit()
    return go("/inbox/channels", ok="Default brand saved. Existing conversation choices and CRM links remain separate.")


def thread_context(db, user, value, cursor=None):
    account, bound = service.resolve(value, user.id, user.email, db)
    messages, next_cursor, error = [], None, None
    try:
        if account.provider == "local":
            cached = service.local_thread(db, account, bound["thread"], user.id)
            messages, next_cursor = cached.messages, None
            if (service.utcnow() - cached.seen_at).total_seconds() > 120:
                error = "Your personal connector is offline or catching up. These are the last synced messages."
        else:
            messages, next_cursor = client(account).messages(bound["thread"], cursor)
    except InboxError as exc:
        error = service.MESSAGES.get(exc.code, "Connection unavailable.")
    linked = inbox_crm.links(db, account, bound["thread"])
    return {"account": account, "conversation_name": bound["name"], "thread_ticket": value,
            "messages": messages, "next_cursor": next_cursor, "thread_error": error,
            "message_window_limited": account.provider == "local" and cached.has_more,
            "crm_links": linked, "organisation": inbox_organisation.choices(db, user.id, account, bound["thread"], linked),
            "kinds": inbox_organisation.KINDS, "crm_configured": bool(service.config.R72_GROUP_HQ_INBOX_SECRET),
            "piuk_crm_enabled": service.config.GHQ_INBOX_PIUK_CRM_ENABLED,
            "reply_receipts": service.replies(db, account, bound["thread"]), "request_id": str(uuid.uuid4())}


@router.get(service.PAGE, response_class=HTMLResponse)
def inbox(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    selected = request.query_params.getlist("account")
    brand, channel = request.query_params.get("brand", ""), request.query_params.get("channel", "")
    kind = request.query_params.get("kind", "")
    ctx = {"user": user, "section": "inbox", "brands": service.BRANDS, "channels": service.CHANNELS,
           "selected": selected, "brand_filter": brand, "channel_filter": channel,
           "accounts": [], "results": [], "threads": [], "setup_error": None, "kind_filter": kind, "kinds": inbox_organisation.KINDS}
    try:
        ctx["accounts"], ctx["results"], ctx["threads"] = service.snapshot(user.id, selected, brand, channel, db=db, user_email=user.email, kind=kind)
    except InboxError:
        ctx["setup_error"] = "Inbox account configuration needs checking. Existing channels remain available in their workspaces."
    value = request.query_params.get("thread")
    if value:
        ctx.update(thread_context(db, user, value, request.query_params.get("cursor")))
        if ctx['account'].provider != 'local' and not ctx['thread_error'] and ctx['crm_links']:
            account, bound = service.resolve(value, user.id, user.email, db)
            background_tasks.add_task(inbox_crm.sync_official_window, account.key, account.binding, bound['thread'], user.id, ctx['messages'])
    response = render(request, "inbox.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(service.PAGE + "/reply", dependencies=[Depends(check_origin)])
def reply(request: Request, thread_ticket: str = Form(...), request_id: str = Form(...),
          message: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    account, bound = service.resolve(thread_ticket, user.id, user.email, db)
    try:
        receipt = service.send(db, user, account, bound["thread"], message, request_id)
    except HTTPException as error:
        if error.status_code not in (400, 409):
            raise
        ctx = thread_context(db, user, thread_ticket)
        ctx.update({"user": user, "section": "inbox", "accounts": [a for a in service.accounts(db) if not a.owner_email or a.owner_email == user.email], "results": [], "threads": [],
            "brands": service.BRANDS, "channels": service.CHANNELS, "selected": [account.key], "brand_filter": "",
            "channel_filter": "", "draft": message, "request_id": request_id, "flash": error.detail, "flash_kind": "err"})
        response = render(request, "inbox.html", ctx, status_code=error.status_code)
        response.headers["Cache-Control"] = "no-store"
        return response
    from urllib.parse import urlencode
    status = {"queued": "Your reply is queued for your personal connector.", "accepted": "The channel accepted your reply.",
              "bridge_accepted": "Your connector accepted the reply. Delivery to the original app is not confirmed.",
              "calling": service.MESSAGES["outcome_unknown"],
              "outcome_unknown": service.MESSAGES["outcome_unknown"],
              "rejected": service.MESSAGES.get(receipt.reason, "The channel refused the reply.")}[receipt.state]
    return go(service.PAGE + "?" + urlencode({"account": account.key, "thread": thread_ticket}),
              **({"ok": status} if receipt.state in ("queued", "accepted", "bridge_accepted") else {"err": status}))


@router.post("/inbox/crm", dependencies=[Depends(check_origin)])
def crm_connect(thread_ticket: str = Form(...), brand: str = Form(...), display_name: str = Form(""),
                contact_id: str = Form(""), copy_messages: bool = Form(False), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from urllib.parse import urlencode
    account, bound = service.resolve(thread_ticket, user.id, user.email, db)
    try:
        inbox_crm.connect(db, user, thread_ticket, brand, display_name, contact_id, copy_messages=copy_messages)
    except inbox_crm.CrmUnavailable as error:
        return go("/inbox?" + urlencode({"account": account.key, "thread": thread_ticket}), err=str(error))
    if account.provider == "local":
        from hashlib import sha256
        inbox_crm.sync_thread(account.key, sha256(bound["thread"].encode()).hexdigest(), user.id)
    return go("/inbox?" + urlencode({"account": account.key, "thread": thread_ticket}), ok="Conversation linked to the selected business CRM.")


@router.post("/inbox/organise", dependencies=[Depends(check_origin)])
def organise(thread_ticket: str = Form(...), kind: str = Form(...), brands: list[str] = Form([]),
             sync_brands: list[str] = Form([]), note: str = Form(""),
             db: Session = Depends(get_db), user: User = Depends(require_admin)):
    from urllib.parse import urlencode
    account, bound = service.resolve(thread_ticket, user.id, user.email, db)
    linked = inbox_crm.links(db, account, bound["thread"])
    if set(sync_brands) - {link.brand for link in linked}:
        raise HTTPException(400, "Link a CRM contact before choosing message copying.")
    inbox_organisation.save(db, user, account, bound["thread"], kind, brands, sync_brands, note)
    return go("/inbox?" + urlencode({"account": account.key, "thread": thread_ticket}), ok="Conversation choices saved. Your reply channel has not changed.")


@router.get("/inbox/crm/search")
def crm_search(request: Request, thread: str, brand: str, q: str,
               db: Session = Depends(get_db), user: User = Depends(require_admin)):
    ctx = thread_context(db, user, thread)
    try:
        found = inbox_crm.command(user, brand, "search", query=q)
        matches, error = found["contacts"], None
    except inbox_crm.CrmUnavailable as exc:
        matches, error = [], str(exc)
    response = render(request, "inbox_crm_search.html", {**ctx, "user": user, "section": "inbox", "matches": matches,
        "crm_brand": brand, "query": q, "search_error": error, "brands": service.BRANDS})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/inbox/crm/contact")
def crm_contact(request: Request, brand: str, contact: str, user: User = Depends(require_admin)):
    try:
        result, error = inbox_crm.command(user, brand, "contact", contactId=contact), None
    except inbox_crm.CrmUnavailable as exc:
        result, error = None, str(exc)
    response = render(request, "inbox_crm_contact.html", {"user": user, "section": "inbox", "result": result,
        "crm_error": error, "brands": service.BRANDS, "crm_brand": brand})
    response.headers["Cache-Control"] = "no-store"
    return response
