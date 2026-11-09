"""Owner editor and a separately authenticated group adapter; no Meta credentials."""
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import KeywordDelivery, User
from ..services import whatsapp_keywords as service
from ..templating import go, render

router = APIRouter()
PAGE = "/crm/channels/whatsapp/keywords"


@router.get(PAGE, response_class=HTMLResponse)
def editor(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    saved = service.rules(db)
    edit = next((r for r in saved["rules"] if r["id"] == request.query_params.get("edit")), None)
    response = render(request, "whatsapp_keywords.html", {"user": user, "section": "whatsapp", "saved": saved,
        "edit": edit, "brands": service.BRANDS, "inventory": service.setting(db, service.GROUPS_KEY, {}),
        "connected": 32 <= len(config.WHATSAPP_KEYWORD_ADAPTER_SECRET) <= 256,
        "deliveries": db.scalars(select(KeywordDelivery).order_by(KeywordDelivery.created_at.desc()).limit(50)).all()})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post(PAGE, dependencies=[Depends(check_origin)])
def save(revision: int = Form(...), name: str = Form(...), brand: str = Form(...), keywords: str = Form(...),
         message: str = Form(...), match_mode: str = Form("exact"), group_ids: list[str] = Form([]),
         enabled: bool = Form(False), requested_reply: bool = Form(False), rule_id: str = Form(""),
         db: Session = Depends(get_db), user: User = Depends(require_admin)):
    service.save_rule(db, user, {"revision": revision, "id": rule_id, "name": name, "brand": brand,
        "keywords": keywords, "message": message, "matchMode": match_mode, "groups": group_ids,
        "enabled": enabled, "requestedReply": requested_reply})
    return go(PAGE, ok="Rule saved. The listener will use it for new requests; existing deliveries are not repeated.")


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
        result = {"schemaVersion": 1, "listenerId": service.LISTENER, **service.rules(db)}
    elif request.method == "GET" and action == "pending":
        result = {"deliveryIds": list(db.scalars(select(KeywordDelivery.id).where(KeywordDelivery.state == "pending")
                                               .order_by(KeywordDelivery.created_at).limit(30)).all())}
    elif request.method == "POST" and action == "groups":
        result = service.sync_groups(db, service.json_object(body))
    elif request.method == "POST" and action == "events":
        result = service.ingest(db, service.json_object(body), body)
    elif request.method == "POST" and action == "opt-outs":
        result = service.opt_out(db, service.json_object(body), body)
    elif request.method == "POST" and action.startswith("deliveries/"):
        parts = action.split("/")
        if len(parts) != 3:
            raise HTTPException(404)
        try:
            import uuid
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
