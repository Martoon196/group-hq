"""Owner editor and a separately authenticated group adapter; no Meta credentials."""
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import KeywordDelivery, User
from ..services import whatsapp_keywords as service
from ..templating import go, render

router = APIRouter()
PAGE = "/crm/channels/whatsapp/keywords"
SAVED = "Rule saved. The listener will use it for new requests; existing deliveries are not repeated."
CATCHUP_CHOICES = ((6, "6 hours"), (24, "24 hours"), (48, "48 hours"), (72, "72 hours"), (168, "7 days"))
CATCHUP_STARTED = ("Catch-up requested. The listener on your computer will read these groups' earlier messages and queue one "
                   "private reply for each person who typed the keyword; replies go out about one every "
                   f"{service.SEND_GAP} seconds while it runs.")


def editor_context(db: Session) -> dict:
    """What the editor shows, for the page and for the operator's whatsapp_keyword_rules tool: the saved rules and their
    revision, the groups the listener reported, whether its adapter secret is set, and the latest 50 enquiries."""
    inventory = service.setting(db, service.GROUPS_KEY, {})
    return {"saved": service.rules(db), "brands": service.BRANDS, "inventory": inventory,
            "connected": 32 <= len(config.WHATSAPP_KEYWORD_ADAPTER_SECRET) <= 256,
            "deliveries": db.scalars(select(KeywordDelivery).order_by(KeywordDelivery.created_at.desc()).limit(50)).all(),
            "catchups": catchup_rows(db, {g["id"]: g["name"] for g in inventory.get("groups", [])}),
            "catchup_choices": CATCHUP_CHOICES, "catchup_cap": service.CATCHUP_CAP, "send_gap": service.SEND_GAP}


def catchup_rows(db: Session, names: dict, limit: int = 5) -> list[dict]:
    """The open catch-up and the most recent ones, for the editor and the operator's whatsapp_keyword_rules tool: each
    as stored (app.services.whatsapp_keywords.request_catchup), its groups named, and where its queued replies stand."""
    rows = []
    for item in service.catchups(db)[:limit]:
        states = dict(db.execute(select(KeywordDelivery.state, func.count()).where(KeywordDelivery.id.in_(item["deliveryIds"]))
                                 .group_by(KeywordDelivery.state)).all()) if item["deliveryIds"] else {}
        rows.append({**{k: v for k, v in item.items() if k != "deliveryIds"}, "replies": states,
                     "requested": datetime.fromisoformat(item["requestedAt"]),
                     "groups": [{**g, "name": names.get(g["id"])} for g in item["groups"]]})
    return rows


def save_rule(db: Session, user: User, *, revision: int, name: str, brand: str, keywords: str, message: str,
              match_mode: str = "exact", group_ids=(), enabled: bool = False, requested_reply: bool = False,
              rule_id: str = "") -> tuple[dict, str]:
    """Save the editor's form (the Save button and the operator's keyword tools). The rule as saved and the flash; a
    refusal is the service's HTTPException with the screen's sentence, and nothing is saved."""
    item = service.save_rule(db, user, {"revision": revision, "id": rule_id, "name": name, "brand": brand,
        "keywords": keywords, "message": message, "matchMode": match_mode, "groups": list(group_ids),
        "enabled": enabled, "requestedReply": requested_reply})
    return item, SAVED


def request_catchup(db: Session, user: User, *, rule_id: str, hours: int = service.CATCHUP_DEFAULT_HOURS,
                    requested_reply: bool = False) -> tuple[dict, str]:
    """The "Catch up on earlier messages" button and the operator's whatsapp_keyword_catchup tool. The catch-up as
    recorded and the flash; a refusal is the service's HTTPException with the screen's sentence, and nothing starts."""
    return service.request_catchup(db, user, rule_id, hours, requested_reply), CATCHUP_STARTED


def cancel_catchup(db: Session, user: User, *, catchup_id: str) -> tuple[dict, str]:
    """The Cancel button and the operator's whatsapp_keyword_catchup_cancel tool: the catch-up as cancelled and the flash."""
    item, held = service.cancel_catchup(db, user, catchup_id)
    return {**item, "held": held}, ("Catch-up cancelled. No more earlier messages will be answered"
                                    + (f", and {held} repl{'y' if held == 1 else 'ies'} not yet sent will not go out." if held else "."))


def import_requests(db: Session, user: User, *, rule_id: str, entries: list, requested_reply: bool = False) -> tuple[dict, str]:
    """The operator's whatsapp_keyword_import tool: earlier requests read from an exported group chat, queued as the
    rule's replies. What was queued and the sentence; a refusal is the service's HTTPException and nothing is queued."""
    result = service.import_requests(db, user, rule_id, entries, requested_reply)
    queued = result["counts"]["queued"]
    return result, (f"{queued} repl{'y' if queued == 1 else 'ies'} queued. They leave about one every {service.SEND_GAP} "
                    "seconds while the listener on your computer runs.")


@router.get(PAGE, response_class=HTMLResponse)
def editor(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    ctx = editor_context(db)
    edit = next((r for r in ctx["saved"]["rules"] if r["id"] == request.query_params.get("edit")), None)
    response = render(request, "whatsapp_keywords.html", {"user": user, "section": "whatsapp", "edit": edit, **ctx})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(PAGE, dependencies=[Depends(check_origin)])
def save(revision: int = Form(...), name: str = Form(...), brand: str = Form(...), keywords: str = Form(...),
         message: str = Form(...), match_mode: str = Form("exact"), group_ids: list[str] = Form([]),
         enabled: bool = Form(False), requested_reply: bool = Form(False), rule_id: str = Form(""),
         db: Session = Depends(get_db), user: User = Depends(require_admin)):
    _, message_saved = save_rule(db, user, revision=revision, name=name, brand=brand, keywords=keywords, message=message,
                                 match_mode=match_mode, group_ids=group_ids, enabled=enabled,
                                 requested_reply=requested_reply, rule_id=rule_id)
    return go(PAGE, ok=message_saved)


@router.post(PAGE + "/catchup", dependencies=[Depends(check_origin)])
def catchup(rule_id: str = Form(...), hours: int = Form(service.CATCHUP_DEFAULT_HOURS), requested_reply: bool = Form(False),
            db: Session = Depends(get_db), user: User = Depends(require_admin)):
    _, started = request_catchup(db, user, rule_id=rule_id, hours=hours, requested_reply=requested_reply)
    return go(PAGE, ok=started)


@router.post(PAGE + "/catchup/cancel", dependencies=[Depends(check_origin)])
def catchup_cancel(catchup_id: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    _, cancelled = cancel_catchup(db, user, catchup_id=catchup_id)
    return go(PAGE, ok=cancelled)


@router.api_route(service.BASE + "/{action:path}", methods=["GET", "POST"])
def adapter(action: str, request: Request, background_tasks: BackgroundTasks,
            body: bytes = Depends(service.authenticated_body), db: Session = Depends(get_db)):
    # Release database locks in the dispatching thread before response/background processing.
    with db:
        return dispatch_adapter(action, request, background_tasks, db, body)


def dispatch_adapter(action, request, background_tasks, db, body):
    if request.method == "POST" and action.startswith("direct/"):
        from ..services import whatsapp_direct
        result = whatsapp_direct.dispatch(db, action[7:], service.json_object(body))
        if action == "direct/event" and result.get("stored"):
            from ..services.inbox_crm import sync_thread
            background_tasks.add_task(sync_thread, whatsapp_direct.ACCOUNT_KEY, result["thread_sha256"], whatsapp_direct.personal_inbox.owner(db).id)
    elif request.method == "GET" and action == "rules":
        # catchups is an addition to contract v1: a listener that predates it reads the rules and ignores it.
        result = {"schemaVersion": 1, "listenerId": service.LISTENER, **service.rules(db), "catchups": service.catchup_snapshot(db)}
    elif request.method == "GET" and action == "pending":
        result = service.pending(db)
    elif request.method == "POST" and action == "groups":
        result = service.sync_groups(db, service.json_object(body))
    elif request.method == "POST" and action == "events":
        result = service.ingest(db, service.json_object(body), body)
    elif request.method == "POST" and action == "catchup-events":
        result = service.ingest_catchup(db, service.json_object(body), body)
    elif request.method == "POST" and action == "opt-outs":
        result = service.opt_out(db, service.json_object(body), body)
    elif request.method == "POST" and action.startswith("catchups/"):
        parts = action.split("/")
        if len(parts) != 3 or parts[2] != "progress":
            raise HTTPException(404)
        try:
            uuid.UUID(parts[1])
        except ValueError:
            raise HTTPException(400, "Invalid catch-up")
        result = service.catchup_progress(db, parts[1], service.json_object(body))
    elif request.method == "POST" and action.startswith("deliveries/"):
        parts = action.split("/")
        if len(parts) != 3:
            raise HTTPException(404)
        try:
            uuid.UUID(parts[1])
        except ValueError:
            raise HTTPException(400, "Invalid delivery")
        if parts[2] == "begin" and body in (b"", b"{}"):
            result = service.begin(db, parts[1])
        elif parts[2] == "settle":
            result = service.settle(db, parts[1], service.json_object(body))
        else:
            raise HTTPException(404)
    else:
        raise HTTPException(404)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
