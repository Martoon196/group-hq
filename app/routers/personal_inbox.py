from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import personal_inbox as service
from ..services.audit import audit
from ..services.inbox_providers import BRANDS, InboxError
from ..templating import go, render

router = APIRouter()


def require_personal_owner(user):
    if user.email != config.GHQ_PERSONAL_INBOX_OWNER_EMAIL:
        raise HTTPException(403, "Personal connections belong to their account owner.")


@router.get("/inbox/connect")
def connect(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    require_personal_owner(user)
    response = render(request, "personal_inbox_connect.html", {"user": user, "section": "inbox",
        "catalog": service.catalog(db), "brands": BRANDS,
        "configured": 32 <= len(config.GHQ_PERSONAL_INBOX_SECRET) <= 256})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/inbox/connect", dependencies=[Depends(check_origin)])
def save(account_key: str = Form(...), enabled: bool = Form(False), brand: str = Form(...),
         db: Session = Depends(get_db), user: User = Depends(require_admin)):
    require_personal_owner(user)
    db.execute(text("SELECT pg_advisory_xact_lock(741935834)"))
    saved = service.catalog(db)
    row = next((r for r in saved["accounts"] if r["key"] == account_key), None)
    if row is None or brand not in BRANDS:
        raise HTTPException(400, "Choose a discovered personal account and business.")
    before = {"enabled": row["enabled"], "brand": row["brand"]}
    row.update({"enabled": enabled, "brand": brand})
    service.save_catalog(db, saved, user.email)
    audit(db, user, "inbox.personal.configure", "inbox-account:" + account_key,
          before=before, after={"enabled": enabled, "brand": brand})
    db.commit()
    return go("/inbox/connect", ok="Personal account settings saved.")


@router.api_route(service.BASE + "/{action:path}", methods=["GET", "POST"])
async def desktop(action: str, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    data = await service.authenticated_body(request)
    try:
        if request.method == "POST" and action == "accounts":
            db.execute(text("SELECT pg_advisory_xact_lock(741935834)"))
            result = service.discover(db, data)
        elif request.method == "POST" and action == "threads":
            result = service.ingest(db, data)
            from ..services.inbox_crm import sync_thread
            background_tasks.add_task(sync_thread, data["account_key"], result["thread_sha256"], service.owner(db).id)
        elif request.method == "GET" and action == "pending":
            result = service.pending(db)
        elif request.method == "POST" and action == "pending":
            result = service.pending(db, service.connector(data))
        elif request.method == "POST" and action.startswith("replies/"):
            parts = action.split("/")
            if len(parts) != 3:
                raise HTTPException(404)
            if parts[2] == "begin" and set(data).issubset({"connector_id"}):
                result = service.begin(db, parts[1], service.connector(data))
            elif parts[2] == "settle":
                result = service.settle(db, parts[1], data)
            else:
                raise HTTPException(404)
        else:
            raise HTTPException(404)
    except InboxError:
        raise HTTPException(400, "Invalid personal inbox evidence") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
