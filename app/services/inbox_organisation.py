"""Owner conversation choices, independent of the source account and CRM identity."""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert
from ..models import AppSetting, utcnow
from .inbox_providers import BRANDS
from .audit import audit

KINDS = {"unclassified": "Unclassified", "personal": "Friends & personal", "pitch": "Selling to me", "business": "Business for me"}


def key(user_id, account, thread_id):
    return "inbox.conv.v1." + hashlib.sha256(json.dumps([user_id, account.identity, thread_id]).encode()).hexdigest()


def choices(db, user_id, account, thread_id, links=()):
    row = db.get(AppSetting, key(user_id, account, thread_id)) if db is not None else None
    if row is not None:
        return row.value
    # Existing explicit CRM links retain their previously enabled projection.
    brands = sorted({link.brand for link in links})
    return {"kind": "business" if brands else "unclassified", "brands": brands, "sync_brands": brands, "note": ""}


def save(db, user, account, thread_id, kind, brands, sync_brands, note=""):
    brands, sync_brands = sorted(set(brands)), sorted(set(sync_brands))
    if kind not in KINDS or set(brands) - set(BRANDS) or set(sync_brands) - set(brands):
        raise HTTPException(400, "Choose a conversation type and valid businesses.")
    if len(note) > 2000:
        raise HTTPException(400, "Keep the private note within 2,000 characters.")
    value = {"kind": kind, "brands": brands, "sync_brands": sync_brands, "note": note.strip()}
    from .inbox_automations import lock_account
    lock_account(db, account)
    setting_key = key(user.id, account, thread_id)
    db.execute(insert(AppSetting).values(key=setting_key, value=value, updated_by=user.email, updated_at=utcnow())
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value, "updated_by": user.email, "updated_at": utcnow()}))
    audit(db, user, "inbox.conversation.organise", setting_key,
          after={"kind": kind, "brands": brands, "sync_brands": sync_brands})
    db.commit()
    return value


def permits_projection(choices, brand):
    return choices["kind"] == "business" and brand in choices["sync_brands"]
