"""Owner-approved video comment references; no OAuth credentials are stored here."""
import os
import re

from fastapi import HTTPException
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from .. import config
from ..models import AppSetting, utcnow
from .audit import audit
from .inbox_providers import Account, BRANDS, InboxError, opaque
from .linkedin_comments_provider import profile_token
from .video_comments_provider import CHANNELS, PROFILES, VideoCommentAccount, VideoComments, VideoSetup, target

PAGE = "/inbox/video-comments"


def require_owner(user):
    if not config.GHQ_PERSONAL_INBOX_OWNER_EMAIL or user.email != config.GHQ_PERSONAL_INBOX_OWNER_EMAIL:
        raise HTTPException(403, "These public channel connections belong to their account owner.")


def scope(brand, channel, user):
    require_owner(user)
    if brand not in PROFILES or channel not in CHANNELS:
        raise HTTPException(400, "Choose a known business and YouTube or TikTok.")
    return Account(key=brand + "-" + channel + "-setup", brand=brand, channel=channel,
        label=BRANDS[brand] + " · " + CHANNELS[channel], provider="zernio", account_id="setup",
        profile_id=PROFILES[brand], owner_email=user.email,
        token=profile_token(os.environ, brand, PROFILES[brand], user.email))


def account(base, ident, name, replies=False):
    if not re.fullmatch(r"[a-f0-9]{24}", ident):
        raise InboxError("unbound_target")
    return VideoCommentAccount(key=base.brand + "-" + base.channel + "-public", brand=base.brand,
        channel=base.channel, label=name[:200], provider="zernio", account_id=ident, profile_id=base.profile_id,
        owner_email=base.owner_email, token=base.token, can_reply=replies)


def registry_key(user):
    return "inbox.video.v1." + str(user.id)


def configured(db, user, base):
    row = db.get(AppSetting, registry_key(user))
    values = row.value.get("accounts", []) if row else []
    for value in values:
        if value.get("brand") == base.brand and value.get("channel") == base.channel:
            if value.get("profile_id") != base.profile_id or value.get("owner_email") != user.email:
                raise InboxError("unbound_target")
            return account(base, value["id"], value["name"], value.get("can_reply") is True)
    return None


def signer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="ghq-video-public-v1")


def ticket(base, user, kind, **values):
    return signer().dumps({**values, "surface": base.channel + "_public_comments", "user": user.id,
        "brand": base.brand, "channel": base.channel, "binding": base.binding, "kind": kind})


def resolve(value, user, kind, db=None):
    try:
        bound = signer().loads(value, max_age=1800 if kind == "enable" else 7200)
        base = scope(bound["brand"], bound["channel"], user)
        current = base if kind == "enable" else configured(db, user, base)
        if (current is None or bound.get("user") != user.id or bound.get("kind") != kind
                or bound.get("surface") != current.channel + "_public_comments" or bound.get("binding") != current.binding):
            raise ValueError()
        if kind in ("post", "reply"):
            opaque(bound["post"])
        if kind == "reply":
            if not current.can_reply:
                raise ValueError()
            target(current, bound["post"], bound["comment"])
        return current, bound
    except (BadData, ValueError, TypeError, KeyError, AttributeError, InboxError):
        raise HTTPException(409, "Refresh public video comments before continuing.") from None


def enable(db, user, value, acknowledge, replies=False):
    if acknowledge is not True:
        raise HTTPException(400, "Confirm this public channel and its business before enabling it.")
    base, bound = resolve(value, user, "enable")
    observed = next((r for r in VideoSetup(base).inventory() if r["id"] == bound["id"] and r["connected"]), None)
    if observed is None:
        raise InboxError("sign_in_needed")
    current = account(base, observed["id"], observed["name"], replies)
    VideoComments(current).posts()  # A posting-only/skipped account is not a connected comment inbox.
    db.execute(text("SELECT pg_advisory_xact_lock(741935836)"))
    key = registry_key(user)
    old = db.get(AppSetting, key)
    values = [r for r in (old.value.get("accounts", []) if old else [])
              if (r["brand"], r["channel"]) != (base.brand, base.channel)]
    values.append({"brand": base.brand, "channel": base.channel, "profile_id": base.profile_id,
        "owner_email": user.email, "id": current.account_id, "name": current.label, "can_reply": replies})
    db.execute(insert(AppSetting).values(key=key, value={"accounts": values}, updated_by=user.email, updated_at=utcnow())
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": {"accounts": values},
            "updated_by": user.email, "updated_at": utcnow()}))
    audit(db, user, "inbox.video.enable", current.key, after={"brand": base.brand, "channel": base.channel,
        "account": current.account_id, "profile": base.profile_id, "can_reply": replies})
    db.commit()
    return current
