"""Growth HQ owner actions against the canonical Relaunch72 CRM command boundary."""
import hashlib
import hmac
import json
import time
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .. import config
from ..models import InboxCrmLink, InboxThread, utcnow
from . import inbox

ORIGIN = "https://hq.propertypredator.com"
PATH = "/api/group-hq/inbox/crm"


class CrmUnavailable(Exception):
    pass


def command(user, brand, action, *, transport=None, **payload):
    if brand == "piuk" and not config.GHQ_INBOX_PIUK_CRM_ENABLED:
        raise CrmUnavailable("The Prop Invest UK CRM workspace needs connecting.")
    secret = config.R72_GROUP_HQ_INBOX_SECRET
    if not 32 <= len(secret) <= 256:
        raise CrmUnavailable("CRM connection needs setting up.")
    value = {**payload, "brand": brand, "action": action, "actorEmail": user.email, "requestId": str(uuid.uuid4())}
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"{stamp}\nPOST\n{PATH}\n".encode() + body, hashlib.sha256).hexdigest()
    try:
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False, transport=transport) as client:
            response = client.post(ORIGIN + PATH, content=body, headers={"Content-Type": "application/json",
                "X-GHQ-Timestamp": stamp, "X-GHQ-Signature": signature})
        if response.status_code != 200:
            if response.status_code == 403:
                raise CrmUnavailable("Your CRM access for this business is unavailable.")
            if response.status_code == 409:
                raise CrmUnavailable("This conversation or request is already linked differently. Refresh and check the existing contact.")
            raise CrmUnavailable("CRM is temporarily unavailable. Your conversation is saved; try the CRM action again.")
        if len(response.content) > 1048576:
            raise ValueError()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError()
        if action == "search":
            rows = result.get("contacts")
            if not isinstance(rows, list) or len(rows) > 20:
                raise ValueError()
            for row in rows:
                uuid.UUID(row["id"])
                if not isinstance(row["name"], str) or len(row["name"]) > 200:
                    raise ValueError()
        else:
            uuid.UUID(result["contactId"])
            uuid.UUID(result["workspaceId"])
            if result.get("brand") != brand:
                raise ValueError()
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise CrmUnavailable("CRM did not return a confirmed result. Try the CRM action again; it will not create a duplicate.") from None


def source(db, user, thread_ticket):
    account, bound = inbox.resolve(thread_ticket, user.id, user.email, db)
    participant = bound.get("participant")
    if account.provider == "local":
        cached = inbox.local_thread(db, account, bound["thread"], user.id)
        if participant is not None and participant != cached.participant_id:
            raise HTTPException(409, "Refresh the conversation before choosing a CRM contact.")
        participant = cached.participant_id
    if not participant:
        raise HTTPException(409, "Refresh the inbox list before adding this conversation to CRM.")
    return account, bound, {"channel": account.channel, "accountId": account.account_id,
        "threadId": bound["thread"], "participantId": participant}


def links(db, account, thread_id):
    digest = hashlib.sha256(thread_id.encode()).hexdigest()
    return db.scalars(select(InboxCrmLink).where(InboxCrmLink.account_key == account.key,
        InboxCrmLink.thread_sha256 == digest, InboxCrmLink.account_identity_sha256 == account.identity)).all()


def connect(db, user, thread_ticket, brand, display_name, contact_id="", copy_messages=True):
    account, bound, origin = source(db, user, thread_ticket)
    if brand not in inbox.BRANDS:
        raise HTTPException(400, "Choose a business for this CRM contact.")
    if contact_id:
        try:
            uuid.UUID(contact_id)
        except ValueError:
            raise HTTPException(400, "Choose an existing CRM contact.") from None
    result = command(user, brand, "link" if contact_id else "create", source=origin,
                     **({"contactId": contact_id} if contact_id else {"displayName": display_name}))
    digest = hashlib.sha256(bound["thread"].encode()).hexdigest()
    existing = db.get(InboxCrmLink, (account.key, digest, brand))
    if existing and (existing.contact_id != result["contactId"] or existing.workspace_id != result["workspaceId"]
                     or existing.account_identity_sha256 != account.identity):
        raise HTTPException(409, "This conversation already has a different CRM link.")
    db.execute(insert(InboxCrmLink).values(account_key=account.key, thread_sha256=digest, brand=brand,
        contact_id=result["contactId"], workspace_id=result["workspaceId"], user_id=user.id, created_at=utcnow())
        .values(account_identity_sha256=account.identity, participant_id=origin["participantId"])
        .on_conflict_do_nothing(index_elements=[InboxCrmLink.account_key, InboxCrmLink.thread_sha256, InboxCrmLink.brand]))
    from . import inbox_organisation
    settings = inbox_organisation.choices(db, user.id, account, bound["thread"], links(db, account, bound["thread"]))
    # Newly added brands require an explicit message-copy choice from the UI.
    syncing = set(settings["sync_brands"])
    if copy_messages:
        syncing.add(brand)
    else:
        syncing.discard(brand)
    # Commit local link + projection choice together. A background importer
    # must never observe a new link before the unchecked copy choice exists.
    inbox_organisation.save(db, user, account, bound["thread"], "personal" if settings["kind"] == "personal" else "business",
        set(settings["brands"]) | {brand}, syncing, settings["note"])
    # Contacts/points are created only by Relaunch72; cache IDs here only after its confirmed response.
    return result


def record_messages(db, user, account, thread_id, messages, *, limit=10):
    """Safe to retry the native idempotent command. Never sends a provider message."""
    from ..models import InboxCrmActivityReceipt
    recorded = 0
    from . import inbox_organisation
    linked = links(db, account, thread_id)
    settings = inbox_organisation.choices(db, user.id, account, thread_id, linked)
    for link in linked:
        if not inbox_organisation.permits_projection(settings, link.brand):
            continue
        cached = db.get(InboxThread, (account.key, link.thread_sha256)) if account.provider == "local" else None
        participant = cached.participant_id if cached else link.participant_id
        if not participant:
            continue
        origin = {"channel": account.channel, "accountId": account.account_id, "threadId": thread_id, "participantId": participant}
        for m in messages:
            digest = hashlib.sha256(json.dumps([link.contact_id, m["id"], m.get("revision", "")]).encode()).hexdigest()
            key = (account.key, link.thread_sha256, link.brand, digest)
            if db.get(InboxCrmActivityReceipt, key):
                continue
            if recorded >= limit:
                return recorded
            result = command(user, link.brand, "activity", source=origin, contactId=link.contact_id,
                message={"id": m["id"], "body": m["body"], "direction": m["direction"], "occurredAt": m["occurred_at"],
                         "revision": m.get("revision", "")})
            if result["contactId"] != link.contact_id or result["workspaceId"] != link.workspace_id:
                raise CrmUnavailable("CRM contact mapping changed. Check the existing link.")
            db.execute(insert(InboxCrmActivityReceipt).values(account_key=key[0], thread_sha256=key[1], brand=key[2],
                message_sha256=key[3], created_at=utcnow()).on_conflict_do_nothing())
            db.commit()
            recorded += 1
    return recorded


def sync_thread(account_key, thread_sha256, user_id):
    from ..db import SessionLocal
    from ..models import User
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            return
        account = next((a for a in inbox.accounts(db) if a.key == account_key), None)
        cached = db.get(InboxThread, (account_key, thread_sha256))
        if (account is None or cached is None or cached.owner_user_id != user.id
                or account.owner_email != user.email or account.binding != cached.binding_sha256):
            return
        try:
            record_messages(db, user, account, cached.thread_id, cached.messages)
            for link in links(db, account, cached.thread_id):
                link.last_error = None
            db.commit()
        except CrmUnavailable as error:
            db.rollback()
            for link in links(db, account, cached.thread_id):
                link.last_error = str(error)[:400]
            db.commit()


def sync_official_window(account_key, binding, thread_id, user_id, messages):
    """Project an explicitly opened official conversation through existing CRM links."""
    from ..db import SessionLocal
    from ..models import User
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            return
        account = next((a for a in inbox.accounts(db) if a.key == account_key), None)
        if account is None or account.provider == 'local' or account.binding != binding or (account.owner_email and account.owner_email != user.email):
            return
        try:
            record_messages(db, user, account, thread_id, messages)
        except CrmUnavailable as error:
            db.rollback()
            for link in links(db, account, thread_id):
                link.last_error = str(error)[:400]
            db.commit()
