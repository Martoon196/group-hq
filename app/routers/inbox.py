import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import inbox as service, inbox_crm, inbox_organisation, inbox_scanner, inbox_claude, inbox_automations
from ..services import acquisitions
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
    if account is None or account.provider == "r72_whatsapp" or brand not in service.BRANDS:
        raise HTTPException(400, "Choose a business account and valid brand.")
    key = service.account_brand_key(account)
    db.execute(insert(AppSetting).values(key=key, value={"brand": brand}, updated_by=user.email, updated_at=utcnow())
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": {"brand": brand}, "updated_by": user.email, "updated_at": utcnow()}))
    audit(db, user, "inbox.account.brand", account.key, after={"brand": brand})
    db.commit()
    return go("/inbox/channels", ok="Default brand saved. Existing conversation choices and CRM links remain separate.")


def thread_context(db, user, value, cursor=None):
    from ..services.inbox_archive import digest
    account, bound = service.resolve(value, user.id, user.email, db)
    messages, next_cursor, error = [], None, None
    try:
        if account.provider == "local":
            cached = service.local_thread(db, account, bound["thread"], user.id)
            messages, next_cursor = cached.messages, None
            if not service.local_online(db, account, cached):
                error = "Your personal connector is offline or catching up. These are the last synced messages."
        elif account.provider == "r72_whatsapp":
            from ..services.inbox_r72_whatsapp import WhatsAppReader
            messages, next_cursor = WhatsAppReader(account, user.email).messages(bound["thread"], bound["participant"], cursor)
        else:
            messages, next_cursor = client(account).messages(bound["thread"], cursor)
    except InboxError as exc:
        error = service.MESSAGES.get(exc.code, "Connection unavailable.")
    linked = inbox_crm.links(db, account, bound["thread"])
    from ..models import AppSetting
    analysis = db.get(AppSetting, inbox_scanner.result_key(user.id, account, bound["thread"]))
    triage = analysis.value if analysis and not cursor else {}
    current = bool(triage and triage.get("message_stamp") == inbox_scanner.message_stamp(messages))
    return {"triage": triage, "triage_current": current, "triage_labels": inbox_scanner.LABELS,
            "handoff": inbox_automations.handoff_state(db, user.id, account, bound["thread"]),
            "account": account, "conversation_name": bound["name"], "thread_ticket": value,
            "messages": messages, "next_cursor": next_cursor, "thread_error": error,
            "canonical_contact": messages[0].get("contact_id") if account.provider == "r72_whatsapp" and messages else None,
            "whatsapp_window_open": bool(messages and messages[0].get("can_reply")),
            "history_key": digest(bound["thread"]),
            "message_window_limited": account.provider == "local" and cached.has_more,
            "property_deals": acquisitions.deals_for(db, account, bound["thread"]),
            "open_property_deals": acquisitions.open_deals(db),
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
    if brand and brand not in service.BRANDS:
        raise HTTPException(400, "Choose a valid business.")
    try:
        ctx["accounts"], ctx["results"], ctx["threads"] = service.snapshot(user.id, selected, channel=channel, db=db, user_email=user.email, kind=kind)
    except InboxError:
        ctx["setup_error"] = "Inbox account configuration needs checking. Existing channels remain available in their workspaces."
    triage_filter = request.query_params.get("triage", "")
    if triage_filter and triage_filter not in inbox_scanner.FILTERS:
        raise HTTPException(400, "Choose an inbox triage filter.")
    history = inbox_scanner.read_results(db, user.id, ctx["threads"])
    from sqlalchemy import select
    from ..models import AppSetting
    hold_keys = {inbox_automations._hold_key(user.id, t["account"], t["id"]) for t in ctx["threads"]}
    holds = {row.key for row in db.scalars(select(AppSetting).where(AppSetting.key.in_(hold_keys))) if row.value.get("held")} if hold_keys else set()
    visible = []
    for thread in ctx["threads"]:
        result = history.get(inbox_scanner.result_key(user.id, thread["account"], thread["id"]), {})
        thread["triage"] = result
        choice = thread["organisation"]
        manual = thread.get("owner_choice", False)
        thread["category"] = inbox_scanner.effective_kind(result, choice, manual) or "unscanned"
        related = set(choice["brands"])
        if not manual and thread["category"] == "business":
            related.update(result.get("brands", []))
        if not manual and not thread["account"].owner_email:
            related.add(thread["account"].brand)
        if brand and (thread["category"] in ("pitch", "personal") or brand not in related):
            continue
        thread["needs_owner"] = result.get("needs_owner") or inbox_automations._hold_key(user.id, thread["account"], thread["id"]) in holds
        if (not triage_filter or triage_filter == thread["category"] or
                (triage_filter == "handoff" and thread["needs_owner"])):
            visible.append(thread)
    try:
        page = max(1, min(int(request.query_params.get("page", "1")), max(1, (len(visible) + 49) // 50)))
    except ValueError:
        raise HTTPException(400, "Choose a valid page.") from None
    start = (page - 1) * 50
    ctx.update(threads=visible[start:start + 50], triage_filter=triage_filter, triage_filters=inbox_scanner.FILTERS,
               older_page="/inbox?" + request.url.include_query_params(page=page + 1).query if start + 50 < len(visible) else None,
               newer_page="/inbox?" + request.url.include_query_params(page=page - 1).query if page > 1 else None,
               triage_enabled=inbox_scanner.enabled(db, user), claude_configured=inbox_claude.configured(),
               triage_owner=user.email.lower() == service.config.GHQ_PERSONAL_INBOX_OWNER_EMAIL)
    value = request.query_params.get("thread")
    if value:
        ctx.update(thread_context(db, user, value, request.query_params.get("cursor")))
        if ctx['account'].provider != 'local' and not ctx['thread_error'] and ctx['crm_links']:
            account, bound = service.resolve(value, user.id, user.email, db)
            background_tasks.add_task(inbox_crm.sync_official_window, account.key, account.binding, bound['thread'], user.id, ctx['messages'])
    response = render(request, "inbox.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/inbox/scan", dependencies=[Depends(check_origin)])
def scan(enabled: bool = Form(...), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if user.email.lower() != service.config.GHQ_PERSONAL_INBOX_OWNER_EMAIL:
        raise HTTPException(403, "Only the personal inbox owner can change scanning.")
    from ..services.audit import audit
    key = inbox_scanner.setting_key(user.id)
    inbox_scanner.save_value(db, key, {"enabled": enabled}, user.email)
    audit(db, user, "inbox.scan.settings", key, after={"enabled": enabled})
    db.commit()
    return go("/inbox", ok="Scanning connected conversations." if enabled else "Inbox scanning paused.")


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
              "bridge_accepted": ("Growth HQ recorded your reply. Reopen the thread to see its WhatsApp status."
                                  if account.provider == "r72_whatsapp" else "Your connector accepted the reply. Delivery to the original app is not confirmed."),
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
