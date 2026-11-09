"""Bind a verified archive-only DM without turning old messages into fresh notifications."""
import hashlib

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert

from ..models import InboxThread, utcnow
from .inbox_providers import clean, opaque


def ensure_thread(db, user, account, thread):
    if not isinstance(thread, dict) or thread.get("is_group") is not False or thread.get("is_merged") is not False:
        raise HTTPException(400, "An original one-to-one conversation is required.")
    peer, ident = opaque(thread.get("participant_id")), opaque(thread.get("id"))
    digest = hashlib.sha256(ident.encode()).hexdigest()
    db.execute(insert(InboxThread).values(account_key=account.key, thread_sha256=digest, thread_id=ident,
        participant_id=peer, owner_user_id=user.id, binding_sha256=account.binding,
        name=clean(thread.get("name") or "Unnamed contact", 500), preview="", provider_updated_at="",
        messages=[], has_more=True, seen_at=utcnow()).on_conflict_do_nothing(
        index_elements=[InboxThread.account_key, InboxThread.thread_sha256]))
    row = db.get(InboxThread, (account.key, digest))
    if (row.owner_user_id != user.id or row.binding_sha256 != account.binding or
            row.thread_id != ident or row.participant_id != peer):
        raise HTTPException(409, "The archived conversation identity changed.")
    return row
