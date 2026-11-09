from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import User
from ..services import inbox, inbox_automations as service
from ..templating import go, render

router = APIRouter(prefix="/inbox/automations")
signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="ghq-inbox-automation-account-v1")


def account_ticket(user, account):
    return signer.dumps([user.id, account.key, account.binding])


def resolve_account(db, user, value):
    try:
        user_id, key, binding = signer.loads(value, max_age=7200)
        account = next((a for a in inbox.accounts(db) if a.key == key and a.binding == binding), None)
        if user_id == user.id and account and service.available(account, user):
            return account
    except (BadData, ValueError, TypeError):
        pass
    raise HTTPException(409, "Refresh the rules page and choose the account again.")


@router.get("")
def page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    cards = []
    for account in inbox.accounts(db):
        if not account.owner_email or account.owner_email == user.email.lower():
            settings = service.settings(db, user, account)
            own = settings["owner_id"] == user.id
            cards.append({"account": account, "config": settings if own else None, "own": own,
                "supported": service.available(account, user), "ticket": account_ticket(user, account)})
    response = render(request, "inbox_automations.html", {"user": user, "section": "inbox", "cards": cards})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/rules", dependencies=[Depends(check_origin)])
def change(account_ticket: str = Form(...), action: str = Form(...), rule_id: str = Form(""),
           keyword: str = Form(""), reply: str = Form(""), topics: str = Form(""),
           db: Session = Depends(get_db), user: User = Depends(require_admin)):
    account = resolve_account(db, user, account_ticket)
    service.update(db, user, account, action, rule_id=rule_id, keyword=keyword, reply=reply, topics=topics)
    return go("/inbox/automations#account-" + account.key, ok="Account rules updated.")


@router.post("/handoff", dependencies=[Depends(check_origin)])
def handoff(thread_ticket: str = Form(...), action: str = Form(...),
            db: Session = Depends(get_db), user: User = Depends(require_admin)):
    account, bound = inbox.resolve(thread_ticket, user.id, user.email, db)
    if action not in {"takeover", "resume"}:
        raise HTTPException(400, "Choose take over or resume.")
    service.set_handoff(db, user, account, bound["thread"], action == "takeover")
    message = "You are handling this conversation." if action == "takeover" else "Enabled rules can handle new incoming messages."
    return go("/inbox?" + urlencode({"account": account.key, "thread": thread_ticket}), ok=message)
