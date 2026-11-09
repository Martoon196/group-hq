from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..db import get_db
from ..models import User
from ..services import inbox
from ..templating import render

router = APIRouter()


@router.get("/inbox/history")
def history(request: Request, account: str = "", thread: str = "", q: str = Query("", max_length=200),
            page: int = Query(1, ge=1), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    accounts = [a for a in inbox.accounts(db) if not a.owner_email or a.owner_email == user.email]
    allowed = {a.identity: a for a in accounts if not account or a.key == account}
    if account and not allowed:
        raise HTTPException(404)
    params = {"owner": user.id, "scopes": list(allowed), "q": q.strip(), "offset": (page-1)*50}
    common = "owner_id=:owner AND scope=ANY(CAST(:scopes AS text[]))"
    current, items, live_link = None, [], None
    if thread:
        if not account:
            raise HTTPException(400, "Choose a history account.")
        params["thread"] = thread
        current = db.execute(text("SELECT * FROM inbox_history_threads WHERE " + common + " AND thread_key=:thread"), params).mappings().first()
        if not current:
            from ..templating import go
            return go("/inbox/history?account=" + account, ok="This conversation has no imported history yet.")
        items = db.execute(text("SELECT sent_at,direction,body,name FROM inbox_history_messages WHERE " + common +
                  " AND thread_key=:thread AND strpos(lower(body),lower(:q))>0 ORDER BY sent_at DESC,message_key DESC LIMIT 51 OFFSET :offset"), params).mappings().all()
        live_link = inbox.ticket(allowed[current.scope], current.native_id, user.id, current.name, current.participant)
    else:
        items = db.execute(text("""SELECT t.*,s.total,s.oldest FROM inbox_history_threads t CROSS JOIN LATERAL (
              SELECT count(*) AS total,min(m.sent_at) AS oldest FROM inbox_history_messages m
              WHERE (m.owner_id,m.scope,m.thread_key)=(t.owner_id,t.scope,t.thread_key)
            ) s WHERE t.owner_id=:owner AND t.scope=ANY(CAST(:scopes AS text[])) AND
            (strpos(lower(t.name),lower(:q))>0 OR EXISTS (SELECT 1 FROM inbox_history_messages m
             WHERE (m.owner_id,m.scope,m.thread_key)=(t.owner_id,t.scope,t.thread_key) AND strpos(lower(m.body),lower(:q))>0))
            ORDER BY t.name,t.scope,t.thread_key LIMIT 51 OFFSET :offset"""), params).mappings().all()
    response = render(request, "inbox_history.html", {"user": user, "section": "inbox", "accounts": accounts,
            "account_key": account, "scope_map": allowed, "items": items[:50], "current": current, "live_link": live_link,
            "q": q, "older": request.url.include_query_params(page=page+1) if len(items)>50 else None,
            "newer": request.url.include_query_params(page=page-1) if page>1 else None})
    response.headers["Cache-Control"] = "no-store"
    return response
